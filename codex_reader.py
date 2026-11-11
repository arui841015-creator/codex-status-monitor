"""Small read-only Codex app-server client for the standalone monitor."""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any


def resolve_executable(value: str = "codex") -> str:
    candidate = str(value or "codex").strip()
    if Path(candidate).is_file():
        return candidate
    found = shutil.which(candidate)
    if found:
        return found
    local_root = Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI" / "Codex" / "bin"
    candidates = list(local_root.glob("*/codex.exe")) if local_root.is_dir() else []
    if candidates:
        return str(max(candidates, key=lambda item: item.stat().st_mtime))
    raise FileNotFoundError(f"找不到 Codex 可执行文件：{candidate}")


class CodexReadError(RuntimeError):
    pass


class CodexReader:
    """Own one app-server process and expose only read operations."""

    def __init__(self, executable: str, logger: logging.Logger) -> None:
        self.executable = executable or "codex"
        self.log = logger
        self._process: subprocess.Popen[str] | None = None
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._next_id = 1
        self._responses: dict[int, tuple[threading.Event, dict[str, Any] | None]] = {}

    def ensure_started(self) -> None:
        with self._lock:
            if self._process and self._process.poll() is None:
                return
            exe = resolve_executable(self.executable)
            args = [exe, "app-server", "-c", 'mcp_servers.codex_xiaozhi={command="codex-status-monitor-disabled",enabled=false}', "--stdio"]
            try:
                process = subprocess.Popen(
                    args,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                )
            except OSError as exc:
                raise CodexReadError(f"无法启动 Codex app-server: {exc}") from exc
            self._process = process
            threading.Thread(target=self._read_loop, name="status-codex-reader", daemon=True).start()
            threading.Thread(target=self._read_stderr, args=(process,), name="status-codex-stderr", daemon=True).start()
        response = self.request(
            "initialize",
            {"clientInfo": {"name": "codex-status-monitor", "title": "Codex Status Monitor", "version": "1.0.0"}, "capabilities": {"experimentalApi": True}},
            timeout=30,
            _skip_start=True,
        )
        if "error" in response:
            raise CodexReadError(f"Codex 初始化失败：{response['error']}")
        self.notify("initialized", {})

    def request(self, method: str, params: dict[str, Any], timeout: float = 30, _skip_start: bool = False) -> dict[str, Any]:
        if not _skip_start:
            self.ensure_started()
        with self._lock:
            process = self._process
            if not process or process.poll() is not None or not process.stdin:
                raise CodexReadError("Codex app-server 未连接")
            request_id = self._next_id
            self._next_id += 1
            event = threading.Event()
            self._responses[request_id] = (event, None)
        try:
            with self._write_lock:
                process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}) + "\n")
                process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            with self._lock:
                self._responses.pop(request_id, None)
            self.close()
            raise CodexReadError(f"Codex app-server 连接断开：{exc}") from exc
        if not event.wait(timeout):
            with self._lock:
                self._responses.pop(request_id, None)
            raise TimeoutError(f"Codex 请求超时：{method}")
        with self._lock:
            _, response = self._responses.pop(request_id, (event, None))
        if response is None:
            raise CodexReadError(f"Codex 未返回响应：{method}")
        return response

    def notify(self, method: str, params: dict[str, Any]) -> None:
        with self._lock:
            process = self._process
        if not process or process.poll() is not None or not process.stdin:
            return
        try:
            with self._write_lock:
                process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method, "params": params}) + "\n")
                process.stdin.flush()
        except (BrokenPipeError, OSError):
            self.close()

    def read_thread(self, thread_id: str) -> dict[str, Any]:
        response = self.request("thread/read", {"threadId": thread_id, "includeTurns": False})
        if "error" in response:
            raise CodexReadError(str(response["error"]))
        return (response.get("result") or {}).get("thread") or {}

    def latest_turn(self, thread_id: str) -> dict[str, Any]:
        response = self.request("thread/turns/list", {"threadId": thread_id, "sortDirection": "desc", "itemsView": "full", "limit": 5})
        if "error" in response:
            raise CodexReadError(str(response["error"]))
        rows = (response.get("result") or {}).get("data") or []
        if not rows:
            raise LookupError("指定 Codex 线程尚无回合")
        turn = rows[0]
        if not isinstance(turn, dict) or not turn.get("id"):
            raise LookupError("Codex 最新回合缺少真实 id")
        return turn

    def probe(self, thread_id: str) -> dict[str, str]:
        thread = self.read_thread(thread_id)
        cwd = str(thread.get("cwd") or "").rstrip("\\/")
        project = Path(cwd).name if cwd else str(thread.get("name") or thread_id)
        return {"thread_id": thread_id, "project": project or thread_id, "cwd": cwd}

    def list_threads(self, limit: int = 50) -> list[dict[str, Any]]:
        """Explore Desktop-visible threads without starting or resuming one."""
        response = self.request("thread/list", {"limit": max(1, min(limit, 100))})
        if "error" in response:
            raise CodexReadError(str(response["error"]))
        result = response.get("result") or {}
        rows = result.get("data") or result.get("threads") or []
        if not isinstance(rows, list):
            return []
        found: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict) or not row.get("id"):
                continue
            cwd = str(row.get("cwd") or "").rstrip("\\/")
            project = Path(cwd).name if cwd else str(row.get("name") or row["id"])
            found.append({
                "thread_id": row["id"],
                "name": row.get("name") or row.get("title") or row["id"],
                "project": project or row["id"],
                "cwd": cwd,
                "status": row.get("status"),
                "recencyAt": row.get("recencyAt"),
                "updatedAt": row.get("updatedAt"),
                "createdAt": row.get("createdAt"),
            })
        return found

    def close(self) -> None:
        with self._lock:
            process, self._process = self._process, None
        if not process:
            return
        try:
            if process.stdin:
                process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                process.terminate()
            except OSError:
                pass

    def _read_loop(self) -> None:
        with self._lock:
            process = self._process
        if not process or not process.stdout:
            return
        try:
            for line in process.stdout:
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "id" in message and ("result" in message or "error" in message):
                    with self._lock:
                        item = self._responses.get(int(message["id"]))
                        if item:
                            self._responses[int(message["id"])] = (item[0], message)
                            item[0].set()
        finally:
            with self._lock:
                for event, _ in self._responses.values():
                    event.set()

    def _read_stderr(self, process: subprocess.Popen[str]) -> None:
        if not process.stderr:
            return
        for line in process.stderr:
            if line.strip():
                self.log.debug("codex app-server: %s", line.strip()[:1000])

