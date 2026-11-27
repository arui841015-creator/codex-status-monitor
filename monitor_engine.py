"""Standalone Codex / Workbuddy / Doubao auto-watch engine with MQTT publication.

Design (v3): the user only supplies a device_id and ticks the platforms to
watch. Codex and Workbuddy are polled and their newest ``max_tasks`` tasks are
tracked; Doubao is read from its debug port (or, failing that, its window) and
reports the one conversation on screen. A task is published whenever it enters
the window or changes state. Older tasks are evicted automatically, so no
per-task locking is required.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable

from codex_reader import CodexReadError, CodexReader
from doubao_reader import DEFAULT_CDP_PORT, DoubaoReader

DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$")
RESERVED_IDS = {"default", "public", "unknown", "none", "replace_me", "replace-me"}
STATUS_TOPIC_PREFIX = "sdgoods/codex/v1/devices/"
SCHEMA = "sdgoods.codex.project-status.v1"
SOURCE = "codex-xiaozhi-bridge"
CONFIG_VERSION = 3
# Display order everywhere: the UI, the log line, the tracked summary.
PLATFORMS = ("codex", "workbuddy", "doubao")
# Platforms that track a rolling window of tasks. Doubao has a single live
# conversation on screen, so a window size would be meaningless there.
WINDOW_PLATFORMS = ("codex", "workbuddy")
PLATFORM_CODES = {"codex": "CODEX", "workbuddy": "WB", "doubao": "DB"}
# One task per channel by default: the device only shows one line, so tracking
# more than the newest task mostly adds churn. Users can raise it to 1-10 in the UI.
DEFAULT_MAX_TASKS = 1
# Written with a leading "~" so the same default resolves on Windows and macOS.
# Older configs may still hold "%USERPROFILE%\..." — expand_path accepts both.
DEFAULT_DATABASE = "~/.workbuddy/workbuddy.db"
STATUS_LABELS = {
    "thinking": "THINKING",
    "working": "WORKING",
    "responding": "RESPONDING",
    "waiting": "WAITING",
    "worked": "COMPLETED",
    "failed": "FAILED",
    "canceled": "CANCELED",
}
FINAL_STATES = {"worked", "failed", "canceled"}
ACTIVE_STATES = {"thinking", "working", "responding", "waiting"}
WORKBUDDY_STATUS = {
    "pending": "thinking", "queued": "thinking", "created": "thinking",
    "running": "working", "working": "working", "processing": "working",
    "in_progress": "working", "inprogress": "working", "waiting": "waiting",
    "waiting_user": "waiting", "awaiting_approval": "waiting",
    "completed": "worked", "succeeded": "worked", "success": "worked", "done": "worked",
    "error": "failed", "failed": "failed", "failure": "failed",
    "terminated": "canceled", "cancelled": "canceled", "canceled": "canceled",
}
CODEX_FINAL = {
    "completed": "worked", "succeeded": "worked", "failed": "failed", "error": "failed",
    "interrupted": "canceled", "canceled": "canceled", "cancelled": "canceled",
}
CODEX_ACTIVE = {"active", "inprogress", "in_progress", "running", "started", "processing"}
# Threads listed by thread/list report {"type": "notLoaded"}; that is not a
# decision, so fall through to the item-level state instead of guessing.
CODEX_UNKNOWN = {"notloaded", "loaded", "unknown", "idle", "none"}
CODEX_ITEM_STATES = {
    "commandExecution": "working", "mcpToolCall": "working", "webSearch": "working",
    "fileChange": "working", "agentMessage": "responding", "reasoning": "thinking",
    "userMessage": "thinking",
}

DEFAULT_CONFIG: dict[str, Any] = {
    "version": CONFIG_VERSION,
    "identity": {"device_id": ""},
    "mqtt": {
        "host": "117.72.43.16",
        "port": 1883,
        "tls": False,
        "username": "",
        "password": "",
        "qos": 1,
        "keepalive": 45,
        "reconnect_delay_seconds": 5,
    },
    "watch": {
        "codex": {
            "enabled": True,
            "max_tasks": DEFAULT_MAX_TASKS,
            "interval": 1.5,
            "executable": "codex",
        },
        "workbuddy": {
            "enabled": True,
            "max_tasks": DEFAULT_MAX_TASKS,
            "interval": 2.0,
            "database": DEFAULT_DATABASE,
        },
        # Doubao needs either a debug port or an open window, so it ships off:
        # a fresh install should not start with a channel that cannot read.
        "doubao": {
            "enabled": False,
            "interval": 2.0,
            "port": DEFAULT_CDP_PORT,
            # Empty means "find it": the running process, the installer's own
            # registry records, then every fixed drive are searched in turn.
            # Users with a portable/custom install can pin the path here.
            "executable": "",
        },
    },
}


def validate_device_id(value: object) -> str:
    device_id = str(value or "").strip()
    if not device_id or device_id.lower() in RESERVED_IDS or device_id == "REPLACE_ME":
        raise ValueError("device_id 不能为空，且不能使用 default、public 或 REPLACE_ME")
    if not DEVICE_ID_RE.fullmatch(device_id):
        raise ValueError("device_id 只能包含字母、数字、点、下划线和短横线，长度 3-64")
    return device_id


def status_topic(device_id: str) -> str:
    return f"{STATUS_TOPIC_PREFIX}{validate_device_id(device_id)}/status"


def safe_text(value: object, fallback: str = "") -> str:
    text = str(value or fallback).replace("\r", " ").replace("\n", " ").strip()
    return text[:120] or fallback


def ascii_label(value: object, fallback: str) -> str:
    text = safe_text(value)
    if any(ord(char) >= 127 for char in text):
        return fallback[:80]
    result = "".join(char if 32 <= ord(char) < 127 else "_" for char in text).strip("_")
    return (result[:80] or fallback)[:80]


def make_detail(platform: str, project: str, task_name: str, state: str, active_projects: int, active_tasks: int, *, display_project: str | None = None, display_task_name: str | None = None, **extra: Any) -> dict[str, Any]:
    project_raw = safe_text(project, "UNKNOWN PROJECT")
    project_display = ascii_label(project_raw, display_project or "PROJECT")
    task_raw = safe_text(task_name, project_raw)
    task_display = ascii_label(task_raw, display_task_name or project_display)
    label = STATUS_LABELS.get(state, safe_text(state, "UNKNOWN").upper())
    detail: dict[str, Any] = {
        "platform": safe_text(platform, "UNKNOWN"),
        "project": project_display,
        "project_raw": project_raw,
        "task_name": task_display,
        "task_name_raw": task_raw,
        "status": state,
        "status_label": label,
        "display_message": f"{safe_text(platform, 'UNKNOWN').upper()} | {project_display} | {label}",
        "active_projects": int(active_projects),
        "active_tasks": int(active_tasks),
    }
    detail.update(extra)
    return detail


def normalize_state(value: object) -> str | None:
    normalized = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return normalized or None


def workbuddy_state(value: object) -> str | None:
    return WORKBUDDY_STATUS.get(normalize_state(value) or "")


def codex_state(turn: dict[str, Any]) -> str:
    raw = turn.get("status")
    normalized = normalize_state(raw.get("type") if isinstance(raw, dict) else raw)
    if normalized in CODEX_FINAL:
        return CODEX_FINAL[normalized]
    if normalized in {"waiting", "waiting_on_approval", "awaiting_approval", "paused"}:
        return "waiting"
    if normalized in CODEX_ACTIVE or normalized in CODEX_UNKNOWN or not normalized:
        for item in reversed(turn.get("items") or []):
            if isinstance(item, dict) and item.get("type") in CODEX_ITEM_STATES:
                return CODEX_ITEM_STATES[str(item["type"])]
        return "thinking"
    return "thinking"


def _as_int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def rank_items(items: Iterable[dict[str, Any]], limit: int, time_key: Callable[[dict[str, Any]], int]) -> list[dict[str, Any]]:
    """Newest-first window of at most ``limit`` items."""
    ordered = sorted(items, key=lambda item: time_key(item), reverse=True)
    return ordered[: max(1, int(limit or 1))]


def workbuddy_recency(row: dict[str, Any]) -> int:
    return max(_as_int(row.get("last_activity_at")), _as_int(row.get("updated_at")))


def codex_recency(thread: dict[str, Any]) -> int:
    return max(_as_int(thread.get("recencyAt")), _as_int(thread.get("updatedAt")))


def evicted_ids(previous: Iterable[str], current: Iterable[str]) -> list[str]:
    """Tracked ids that fell out of the current window."""
    current_set = set(current)
    return [key for key in previous if key not in current_set]


def normalize_config(raw: object) -> dict[str, Any]:
    """Coerce any historical config (including v1 ``channels``) into v2 ``watch``."""
    source = raw if isinstance(raw, dict) else {}
    config: dict[str, Any] = json.loads(json.dumps(DEFAULT_CONFIG))

    identity = source.get("identity")
    if isinstance(identity, dict):
        device_id = str(identity.get("device_id") or "").strip()
        # REPLACE_ME is a placeholder, not a real identity to carry over.
        config["identity"]["device_id"] = "" if device_id == "REPLACE_ME" else device_id

    mqtt = source.get("mqtt")
    if isinstance(mqtt, dict):
        for key in ("host", "port", "tls", "username", "password", "qos", "keepalive", "reconnect_delay_seconds"):
            if mqtt.get(key) is not None:
                config["mqtt"][key] = mqtt[key]

    watch = source.get("watch")
    if isinstance(watch, dict):
        for platform in PLATFORMS:
            section = watch.get(platform)
            if isinstance(section, dict):
                for key, value in section.items():
                    if value is not None:
                        config["watch"][platform][key] = value

    # v1 migration: a channel list becomes the enabled flags.
    channels = source.get("channels")
    if isinstance(channels, list) and channels and not isinstance(watch, dict):
        enabled = {"codex": False, "workbuddy": False}
        for channel in channels:
            if not isinstance(channel, dict) or not channel.get("enabled", True):
                continue
            kind = str(channel.get("type") or "").strip().lower()
            if kind not in enabled:
                continue
            enabled[kind] = True
            if kind == "workbuddy" and channel.get("database"):
                config["watch"]["workbuddy"]["database"] = channel["database"]
            if kind == "codex" and channel.get("executable"):
                config["watch"]["codex"]["executable"] = channel["executable"]
            try:
                if channel.get("interval"):
                    config["watch"][kind]["interval"] = max(0.2, float(channel["interval"]))
            except (TypeError, ValueError):
                pass
        for platform, flag in enabled.items():
            config["watch"][platform]["enabled"] = flag

    for platform in PLATFORMS:
        section = config["watch"][platform]
        # Doubao's default is None only if a hand-edited config dropped the key;
        # keep the shipped default (off) instead of switching it on by accident.
        default_enabled = bool(DEFAULT_CONFIG["watch"][platform]["enabled"])
        section["enabled"] = bool(section.get("enabled", default_enabled))
        if platform in WINDOW_PLATFORMS:
            raw_max = section.get("max_tasks")
            if raw_max is None or str(raw_max).strip() == "":
                value = DEFAULT_MAX_TASKS
            else:
                try:
                    value = int(raw_max)  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    value = DEFAULT_MAX_TASKS
            section["max_tasks"] = max(1, min(10, value))
            section.pop("port", None)
        else:
            section.pop("max_tasks", None)
            try:
                port = int(section.get("port") or DEFAULT_CDP_PORT)
            except (TypeError, ValueError):
                port = DEFAULT_CDP_PORT
            section["port"] = port if 1 <= port <= 65535 else DEFAULT_CDP_PORT
            section["executable"] = str(section.get("executable") or "").strip()
        try:
            section["interval"] = max(0.2, float(section.get("interval") or 1.0))
        except (TypeError, ValueError):
            section["interval"] = 1.0
    config["version"] = CONFIG_VERSION
    return config


def enabled_platforms(config: dict[str, Any]) -> list[str]:
    watch = config.get("watch") or {}
    return [platform for platform in PLATFORMS if (watch.get(platform) or {}).get("enabled")]


def expand_path(value: object) -> Path:
    text = os.path.expandvars(str(value or "").strip())
    if text.startswith("%USERPROFILE%"):
        text = text.replace("%USERPROFILE%", str(Path.home()), 1)
    if os.name != "nt":
        # Configs carried over from Windows store backslash separators, and on
        # POSIX a backslash is a legal filename character — the path would
        # silently resolve to a file that cannot exist.
        text = text.replace("\\", "/")
    return Path(text).expanduser().resolve()


def open_workbuddy(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
    connection.row_factory = sqlite3.Row
    return connection


def read_workbuddy_sessions(path: Path) -> list[dict[str, Any]]:
    connection = open_workbuddy(path)
    try:
        rows = connection.execute(
            "SELECT id,cwd,title,status,created_at,updated_at,last_activity_at "
            "FROM sessions WHERE deleted_at IS NULL"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def _basename(raw: str) -> str:
    """Last path segment, accepting Windows separators on any host.

    A session created on Windows keeps a backslash path even when the database
    is read on macOS, and POSIX treats "\\" as an ordinary filename character —
    Path(raw).name would then return the whole string. Normalising only when
    the string has backslashes and no forward slashes keeps real POSIX
    filenames that legally contain a backslash intact.
    """
    if not raw:
        return ""
    text = raw
    if "\\" in text and "/" not in text:
        text = text.replace("\\", "/")
    return Path(text).name


def project_name(cwd: object, title: object = None) -> str:
    raw = str(cwd or "").strip().rstrip("\\/")
    if _basename(raw):
        return _basename(raw)
    return safe_text(title, "workbuddy")


def active_counts(states: Iterable[tuple[str, str, str]]) -> tuple[int, int]:
    active = [state for state in states if state[2] in ACTIVE_STATES]
    return len({item[1] for item in active}), len(active)


def doubao_events(
    previous: dict[str, str] | None,
    snap: dict[str, Any] | None,
) -> tuple[dict[str, str] | None, list[tuple[str, dict[str, str]]]]:
    """State machine for the single Doubao conversation on screen.

    Returns ``(new_previous, events)`` where each event is ``(state, info)``.
    Only entering an active state and leaving one produce events, so a poll that
    keeps finding the same state publishes nothing.
    """
    if snap is None:
        return (None, [("worked", previous)] if previous else [])
    state = str(snap.get("state") or "idle")
    info = {
        "session": str(snap.get("session") or "main"),
        "title": str(snap.get("title") or "豆包"),
        "source": str(snap.get("source") or ""),
    }
    if state in ACTIVE_STATES:
        current = dict(info, state=state)
        if previous and previous.get("session") == info["session"] and previous.get("state") == state:
            return previous, []
        return current, [(state, info)]
    return (None, [("worked", previous)] if previous else [])


class MQTTOutput:
    """Non-blocking publisher with a replaceable Broker configuration."""

    def __init__(self, config: dict[str, Any], device_id: str, logger: logging.Logger, callback: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.device_id = validate_device_id(device_id)
        self.config = dict(config)
        self.log = logger
        self.topic = status_topic(self.device_id)
        self.callback = callback
        self._queue: queue.Queue[bytes] = queue.Queue(maxsize=200)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._client: Any = None
        self._connected = False
        self._last_error = ""

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def last_error(self) -> str:
        return self._last_error

    def start(self) -> None:
        if self._thread:
            return
        self._thread = threading.Thread(target=self._run, name="monitor-mqtt", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        deadline = time.monotonic() + 3
        while not self._queue.empty() and time.monotonic() < deadline:
            time.sleep(0.05)
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=4)
        self._disconnect()
        self._thread = None

    def publish(self, payload: dict[str, Any]) -> bool:
        try:
            data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self._queue.put_nowait(data)
            return True
        except queue.Full:
            self.log.warning("MQTT 发布队列已满")
            return False

    def _run(self) -> None:
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            self._last_error = "未安装 paho-mqtt，请运行 install.bat"
            self.log.error(self._last_error)
            return
        client_id = f"codex-status-monitor-{self.device_id}-{uuid.uuid4().hex[:8]}"
        try:
            client = mqtt.Client(client_id=client_id, protocol=mqtt.MQTTv311)
        except TypeError:
            client = mqtt.Client(client_id=client_id)
        username = str(self.config.get("username") or "")
        if username:
            client.username_pw_set(username, str(self.config.get("password") or ""))
        if bool(self.config.get("tls")):
            client.tls_set()
        self._client = client
        delay = max(1.0, float(self.config.get("reconnect_delay_seconds") or 5))
        while not self._stop.is_set():
            if not self._connected:
                try:
                    client.connect(str(self.config.get("host") or "117.72.43.16"), int(self.config.get("port") or 1883), max(10, int(self.config.get("keepalive") or 45)))
                    self._connected = True
                    self._last_error = ""
                    self.log.info("MQTT 已连接 %s:%s，主题=%s", self.config.get("host"), self.config.get("port"), self.topic)
                except Exception as exc:
                    self._last_error = str(exc)
                    self._stop.wait(delay)
                    continue
            try:
                if client.loop(timeout=0.2) != mqtt.MQTT_ERR_SUCCESS:
                    self._connected = False
                    continue
                while True:
                    try:
                        data = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    info = client.publish(self.topic, data, qos=max(0, min(int(self.config.get("qos") or 1), 2)))
                    if getattr(info, "rc", 0) != 0:
                        self._connected = False
                        self._queue.put(data)
                        break
                    if self.callback:
                        try:
                            self.callback(json.loads(data.decode("utf-8")))
                        except Exception:
                            pass
            except Exception as exc:
                self._last_error = str(exc)
                self._connected = False
                try:
                    client.disconnect()
                except Exception:
                    pass
                self._stop.wait(delay)

    def _disconnect(self) -> None:
        client, self._client = self._client, None
        self._connected = False
        if client is not None:
            try:
                client.disconnect()
            except Exception:
                pass


class MonitorManager:
    """Auto-discovers the newest tasks per platform and publishes their state."""

    def __init__(self, config: dict[str, Any], on_log: Callable[[str], None] | None = None, on_observation: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.config = normalize_config(config)
        self.on_log = on_log or (lambda message: None)
        self.on_observation = on_observation or (lambda item: None)
        self.log = logging.getLogger("codex-status-monitor")
        self.log.setLevel(logging.INFO)
        if not self.log.handlers:
            self.log.addHandler(logging.NullHandler())
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._readers: list[CodexReader] = []
        self._states: dict[str, tuple[str, str, str]] = {}
        self._tracked: dict[str, dict[str, dict[str, str]]] = {}
        self._lock = threading.Lock()
        self.publisher: MQTTOutput | None = None

    @property
    def running(self) -> bool:
        return any(thread.is_alive() for thread in self._threads)

    def start(self) -> None:
        if self.running:
            return
        device_id = validate_device_id((self.config.get("identity") or {}).get("device_id"))
        platforms = enabled_platforms(self.config)
        if not platforms:
            raise ValueError("请至少勾选一个监听渠道（Codex、Workbuddy 或豆包）")
        self._stop.clear()
        self.publisher = MQTTOutput(self.config.get("mqtt") or {}, device_id, self.log, lambda payload: self._emit("outgoing", "mqtt", payload))
        self.publisher.start()
        workers = {"codex": self._watch_codex, "workbuddy": self._watch_workbuddy, "doubao": self._watch_doubao}
        for platform in platforms:
            worker = threading.Thread(target=workers[platform], name=f"watch-{platform}", daemon=True)
            self._threads.append(worker)
            worker.start()
        detail = "、".join(
            f"{platform}(最近 {self.config['watch'][platform]['max_tasks']} 个)"
            if platform in WINDOW_PLATFORMS else f"{platform}"
            for platform in platforms
        )
        self._emit_log(f"已启动监听：{detail}；主题 {self.publisher.topic}")

    def stop(self) -> None:
        self._stop.set()
        for reader in list(self._readers):
            reader.close()
        for thread in self._threads:
            thread.join(timeout=2)
        self._threads.clear()
        self._readers.clear()
        if self.publisher:
            self.publisher.stop()
            self.publisher = None
        with self._lock:
            self._states.clear()
            self._tracked.clear()
        self._emit_log("监听已停止")

    def tracked_summary(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._tracked))

    # ---------------- discovery preview ----------------

    def preview(self, platform: str) -> list[dict[str, Any]]:
        """Read-only look at what would be tracked right now."""
        section = (self.config.get("watch") or {}).get(platform) or {}
        if platform == "doubao":
            snap = DoubaoReader(int(section.get("port") or DEFAULT_CDP_PORT), self.log).snapshot()
            if not snap:
                raise RuntimeError(
                    "读不到豆包：请用 --remote-debugging-port 启动豆包，或让豆包窗口保持打开"
                )
            return [{
                "task_id": snap.get("session"),
                "title": snap.get("title"),
                "project": snap.get("title") or "豆包",
                "status": snap.get("state"),
                "state": snap.get("state"),
                "source": snap.get("source"),
            }]
        limit = int(section.get("max_tasks") or DEFAULT_MAX_TASKS)
        if platform == "workbuddy":
            rows = read_workbuddy_sessions(expand_path(section.get("database")))
            window = rank_items(rows, limit, workbuddy_recency)
            return [
                {
                    "task_id": row.get("id"),
                    "title": row.get("title"),
                    "project": project_name(row.get("cwd"), row.get("title")),
                    "status": row.get("status"),
                    "state": workbuddy_state(row.get("status")),
                    "updated_at": workbuddy_recency(row),
                }
                for row in window
            ]
        reader = CodexReader(str(section.get("executable") or "codex"), self.log)
        try:
            threads = reader.list_threads(limit=max(10, limit * 4))
        finally:
            reader.close()
        window = rank_items(threads, limit, codex_recency)
        return [
            {
                "task_id": thread.get("thread_id"),
                "title": thread.get("name"),
                "project": thread.get("project"),
                "status": thread.get("status"),
                "state": "unknown",
                "updated_at": codex_recency(thread),
            }
            for thread in window
        ]

    # ---------------- workers ----------------

    def _watch_codex(self) -> None:
        section = self.config["watch"]["codex"]
        limit = int(section.get("max_tasks") or DEFAULT_MAX_TASKS)
        delay = max(0.2, float(section.get("interval") or 1.5))
        reader = CodexReader(str(section.get("executable") or "codex"), self.log)
        self._readers.append(reader)
        signatures: dict[str, tuple[str, str, str]] = {}
        announced = False
        while not self._stop.is_set():
            try:
                threads = reader.list_threads(limit=max(10, limit * 4))
                window = rank_items(threads, limit, codex_recency)
                keep = [str(thread.get("thread_id") or "") for thread in window]
                dropped = evicted_ids(list(signatures), keep)
                for thread_id in dropped:
                    signatures.pop(thread_id, None)
                    self._forget("Codex", thread_id)
                for thread in window:
                    thread_id = str(thread.get("thread_id") or "")
                    if not thread_id:
                        continue
                    try:
                        turn = reader.latest_turn(thread_id)
                    except LookupError:
                        continue
                    state = codex_state(turn)
                    signature = (str(turn.get("id") or ""), state, str(turn.get("status")))
                    if signatures.get(thread_id) == signature:
                        continue
                    project = str(thread.get("project") or "") or thread_id
                    title = str(thread.get("name") or project)
                    self._emit("incoming", "codex", {
                        "platform": "Codex", "thread_id": thread_id, "turn_id": turn.get("id"),
                        "status": turn.get("status"), "project": project, "title": title,
                    })
                    self._publish("Codex", thread_id, project, title, state, {"thread_id": thread_id, "turn_id": turn.get("id")})
                    signatures[thread_id] = signature
                if dropped:
                    self._emit_log(f"Codex 已顶掉较早任务：{', '.join(item[:8] for item in dropped)}")
                if not announced and window:
                    announced = True
                    self._emit_log(f"Codex 正在跟踪最新 {len(window)} 个任务")
            except CodexReadError as exc:
                self._emit_log(f"Codex 读取失败，稍后重试：{exc}")
                reader.close()
                self._stop.wait(delay)
            except Exception as exc:  # noqa: BLE001
                self._emit_log(f"Codex 通道重试：{exc}")
                reader.close()
                self._stop.wait(delay)
            self._stop.wait(delay)
        reader.close()

    def _watch_workbuddy(self) -> None:
        section = self.config["watch"]["workbuddy"]
        database = expand_path(section.get("database"))
        limit = int(section.get("max_tasks") or DEFAULT_MAX_TASKS)
        delay = max(0.2, float(section.get("interval") or 2.0))
        signatures: dict[str, tuple[str, int]] = {}
        announced = False
        while not self._stop.is_set():
            try:
                dropped, _ = self._workbuddy_pass(database, limit, signatures)
                if dropped:
                    self._emit_log(f"Workbuddy 已顶掉较早任务：{', '.join(item[:8] for item in dropped)}")
                if not announced and signatures:
                    announced = True
                    self._emit_log(f"Workbuddy 正在跟踪最新 {len(signatures)} 个会话")
            except Exception as exc:  # noqa: BLE001
                self._emit_log(f"Workbuddy 通道重试：{exc}")
            self._stop.wait(delay)

    def _workbuddy_pass(self, database: Path, limit: int, signatures: dict[str, tuple[str, int]]) -> tuple[list[str], int]:
        """One scan of the Workbuddy database. Returns (evicted ids, publish count)."""
        rows = read_workbuddy_sessions(database)
        window = rank_items(rows, limit, workbuddy_recency)
        keep = [str(row.get("id") or "") for row in window]
        dropped = evicted_ids(list(signatures), keep)
        for task_id in dropped:
            signatures.pop(task_id, None)
            self._forget("Workbuddy", task_id)
        published = 0
        for row in window:
            task_id = str(row.get("id") or "").strip()
            state = workbuddy_state(row.get("status"))
            if not task_id or not state:
                continue
            signature = (str(row.get("status") or ""), _as_int(row.get("updated_at")))
            if signatures.get(task_id) == signature:
                continue
            project = project_name(row.get("cwd"), row.get("title"))
            title = str(row.get("title") or "") or project
            self._emit("incoming", "workbuddy", {
                "platform": "Workbuddy", "task_id": task_id, "status": row.get("status"),
                "project": project, "task_name": title,
            })
            self._publish("Workbuddy", task_id, project, title, state, {
                "workbuddy_session_id": task_id,
                "workbuddy_status": str(row.get("status") or ""),
            })
            signatures[task_id] = signature
            published += 1
        return dropped, published

    def _watch_doubao(self) -> None:
        """Doubao shows one conversation at a time, so there is no window here.

        Only entry into an active state and the fall back to idle are published;
        a poll that finds Doubao unreadable or idle changes nothing, which keeps
        the device quiet instead of flapping.
        """
        section = self.config["watch"]["doubao"]
        port = int(section.get("port") or DEFAULT_CDP_PORT)
        delay = max(0.5, float(section.get("interval") or 2.0))
        reader = DoubaoReader(port, self.log)
        current: dict[str, str] | None = None
        announced = False
        warned = False
        while not self._stop.is_set():
            try:
                snap = reader.snapshot()
            except Exception as exc:  # noqa: BLE001
                snap = None
                reader.last_error = str(exc)
            current, events = doubao_events(current, snap)
            for state, info in events:
                extra = {
                    "doubao_session": info["session"],
                    "doubao_title": info["title"],
                    "read_via": info.get("source", ""),
                }
                if state != "worked":
                    self._emit("incoming", "doubao", {
                        "platform": "Doubao", "session": info["session"],
                        "title": info["title"], "state": state, "source": info.get("source", ""),
                    })
                self._publish("Doubao", info["session"], info["title"], info["title"], state, extra)
                if state != "worked" and not announced:
                    announced = True
                    source = "调试端口" if info.get("source") == "cdp" else "窗口读取"
                    self._emit_log(f"豆包已在跟踪（{source}）")
            if snap is None and not warned:
                warned = True
                reason = reader.last_error or "既没有调试端口，也没有打开的豆包窗口"
                self._emit_log(
                    f"豆包暂不可读：{reason}。"
                    f"要么让豆包窗口保持打开，要么点「用调试端口启动豆包」以调试模式重启它"
                )
            elif snap is not None:
                warned = False
            self._stop.wait(delay)

    # ---------------- publishing ----------------

    def _forget(self, platform: str, task_id: str) -> None:
        with self._lock:
            self._states.pop(f"{platform.lower()}:{task_id}", None)
            tracked = self._tracked.get(platform.lower()) or {}
            tracked.pop(task_id, None)

    def _publish(self, platform: str, task_id: str, project: str, task_name: str, state: str, extra: dict[str, Any]) -> None:
        if not self.publisher:
            return
        key = f"{platform.lower()}:{task_id}"
        with self._lock:
            self._states[key] = (platform, project, state)
            active_projects, active_tasks = active_counts(list(self._states.values()))
            tracked = self._tracked.setdefault(platform.lower(), {})
            tracked[task_id] = {"project": project, "task_name": task_name, "state": state}
        fallback = f"{PLATFORM_CODES.get(platform.lower(), 'TASK')}-{task_id[:8].upper()}"
        detail = make_detail(platform, project, task_name, state, active_projects, active_tasks, display_project=fallback, display_task_name=fallback, **extra)
        payload = {
            "schema": SCHEMA,
            "event": "project_status",
            "device_id": self.publisher.device_id,
            "task_id": task_id,
            "message": detail["display_message"],
            "beep": False,
            "ts": time.time(),
            "source": SOURCE,
            "detail": detail,
        }
        self.publisher.publish(payload)

    def _emit(self, direction: str, channel_id: object, payload: dict[str, Any]) -> None:
        try:
            self.on_observation({"direction": direction, "channel_id": str(channel_id), "payload": payload, "ts": time.time()})
        except Exception:
            pass

    def _emit_log(self, message: str) -> None:
        self.on_log(message)
        self.log.info(message)
