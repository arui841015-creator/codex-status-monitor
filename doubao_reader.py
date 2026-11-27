"""Read the Doubao (豆包) desktop app's live work state.

Two independent sources, tried in order:

1. **CDP** — when Doubao is started with ``--remote-debugging-port=19226`` its
   Chromium renderer answers the DevTools protocol. Reading the chat page's
   text tells us whether a reply is being generated, and the page title names
   the conversation. This works while the window is minimised or hidden.
2. **UI Automation** — with no debug port, the visible window's control tree is
   scanned for the same keywords. Zero configuration, but only sees an open
   window.

Everything here is read-only: one ``Runtime.evaluate`` per poll, no clicks, no
navigation, no writes.
"""
from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import logging
import os
import socket
import struct
import time
import urllib.request
from pathlib import Path
from typing import Any

try:  # ctypes.wintypes does not exist off Windows, and this module is imported
    import ctypes.wintypes as wt  # unconditionally by the engine.
except (ImportError, ValueError):  # pragma: no cover - POSIX only
    wt = None  # type: ignore[assignment]

DEFAULT_CDP_PORT = 19226
CDP_HOST = "127.0.0.1"
# A real chat window exposes hundreds of control names; the floating assistant
# pill exposes a few dozen. Below this, the window is not a conversation.
MIN_WINDOW_TEXT = 120
# Doubao's window titles read "<page> - 豆包"; both spellings are seen.
TITLE_SUFFIXES = (" - 豆包", " - Doubao", "-豆包", "- Doubao", " - doubao")
# The stop button only exists while the model is streaming, which makes it the
# single most reliable "is it working" signal in both sources.
STOP_WORDS = ("停止生成", "停止回答", "停止输出", "停止响应", "停止创作", "终止生成")
THINK_WORDS = (
    "深度思考", "正在思考", "思考中", "正在搜索", "正在阅读", "正在分析",
    "正在查找", "正在检索", "搜集资料", "正在浏览",
)
BUSY_WORDS = ("正在生成", "生成中", "正在输出", "正在写作")
# Window/page titles that name the app instead of a conversation.
APP_TITLES = ("豆包", "doubao", "豆包ai", "豆包 ai")
# Renderer pages that exist in every client but never hold a conversation:
# the background/service surface, the embedded site-support frame and the
# in-app drive frame. Reading one of these would report an empty title as if
# it were the current session.
NON_CHAT_URL_MARKERS = (
    "doubao-background",
    "cross-site-support",
    "drive-iframe",
    "devtools://",
    "about:blank",
)

PROBE_JS = """
(() => {
  const body = document.body ? (document.body.innerText || '') : '';
  const labels = [];
  for (const node of document.querySelectorAll('button,[role="button"],[aria-label]')) {
    const value = (node.getAttribute('aria-label') || node.innerText || '').trim();
    if (value && value.length < 40) labels.push(value);
    if (labels.length > 300) break;
  }
  return JSON.stringify({
    title: document.title || '',
    href: location.href || '',
    head: body.slice(0, 3000),
    tail: body.slice(-3000),
    labels: labels,
  });
})()
"""


def classify_text(text: str) -> str:
    """Map Doubao's on-screen wording to a state label.

    ``working`` wins over ``thinking`` because Doubao shows a deep-thinking hint
    and a stop button at the same time; the stop button means tokens flow.
    """
    if not text:
        return "idle"
    if any(word in text for word in STOP_WORDS) or any(word in text for word in BUSY_WORDS):
        return "working"
    if any(word in text for word in THINK_WORDS):
        return "thinking"
    return "idle"


def conversation_title(raw: object) -> str:
    """Strip Doubao's app suffix from a page or window title."""
    text = str(raw or "").strip()
    for suffix in TITLE_SUFFIXES:
        if text.lower().endswith(suffix.lower()):
            text = text[: -len(suffix)].strip()
            break
    return text


def session_key(href: object, title: object) -> str:
    """Stable ASCII id for the conversation currently on screen.

    ``task_id`` reaches the device inside a single-line ASCII message, so a
    Chinese conversation title cannot be used directly — it is hashed and the
    readable text stays in ``detail.*_raw``.
    """
    url = str(href or "")
    marker = "/chat/"
    if marker in url:
        tail = url.split(marker, 1)[1].split("?", 1)[0].split("#", 1)[0].strip("/")
        if tail and tail.isascii():
            return tail[:64]
    name = conversation_title(title)
    if not name:
        return "main"
    if name.isascii():
        return name[:64].replace(" ", "-")
    return hashlib.sha1(name.encode("utf-8")).hexdigest()[:10]


# --------------------------------------------------------------------------
# minimal WebSocket client (RFC 6455, text frames only)
# --------------------------------------------------------------------------


class WebSocketError(RuntimeError):
    pass


class MiniWebSocket:
    """Just enough WebSocket to run one CDP command per poll.

    A dedicated dependency would be one more thing to bundle into a frozen
    executable; CDP only needs a single text frame in each direction, so the
    protocol is implemented inline. Fragmented and control frames are handled
    because Chromium does emit pings.
    """

    def __init__(self, url: str, timeout: float = 4.0) -> None:
        self.url = url
        self.timeout = timeout
        self._socket: socket.socket | None = None

    def __enter__(self) -> "MiniWebSocket":
        self.connect()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def connect(self) -> None:
        without_scheme = self.url.split("://", 1)[-1]
        authority, _, path = without_scheme.partition("/")
        host, _, port_text = authority.partition(":")
        port = int(port_text or 80)
        sock = socket.create_connection((host or CDP_HOST, port), timeout=self.timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET /{path or ''} HTTP/1.1\r\n"
            f"Host: {authority}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        sock.sendall(request.encode("ascii"))
        reply = b""
        while b"\r\n\r\n" not in reply:
            chunk = sock.recv(4096)
            if not chunk:
                sock.close()
                raise WebSocketError("CDP 握手被关闭")
            reply += chunk
            if len(reply) > 65536:
                sock.close()
                raise WebSocketError("CDP 握手响应过长")
        status_line = reply.split(b"\r\n", 1)[0].decode("latin-1")
        if "101" not in status_line:
            sock.close()
            raise WebSocketError(f"CDP 握手失败：{status_line}")
        self._socket = sock

    def send(self, text: str) -> None:
        if not self._socket:
            raise WebSocketError("WebSocket 未连接")
        payload = text.encode("utf-8")
        header = bytearray([0x81])  # FIN + text
        mask = os.urandom(4)
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", length)
        header += mask
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self._socket.sendall(bytes(header) + masked)

    def recv(self) -> str:
        if not self._socket:
            raise WebSocketError("WebSocket 未连接")
        fragments = bytearray()
        while True:
            first = self._read_exact(2)
            fin = bool(first[0] & 0x80)
            opcode = first[0] & 0x0F
            masked = bool(first[1] & 0x80)
            length = first[1] & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if masked else b""
            data = self._read_exact(length) if length else b""
            if mask:
                data = bytes(byte ^ mask[index % 4] for index, byte in enumerate(data))
            if opcode == 0x8:  # close
                raise WebSocketError("CDP 连接被关闭")
            if opcode == 0x9:  # ping
                self._send_control(0xA, data)
                continue
            if opcode == 0xA:  # pong
                continue
            fragments += data
            if fin:
                return fragments.decode("utf-8", "replace")

    def _send_control(self, opcode: int, data: bytes) -> None:
        if not self._socket:
            return
        mask = os.urandom(4)
        frame = bytearray([0x80 | opcode, 0x80 | len(data)]) + mask
        frame += bytes(byte ^ mask[index % 4] for index, byte in enumerate(data))
        try:
            self._socket.sendall(bytes(frame))
        except OSError:
            pass

    def _read_exact(self, count: int) -> bytes:
        if not self._socket:
            raise WebSocketError("WebSocket 未连接")
        buffer = b""
        while len(buffer) < count:
            chunk = self._socket.recv(count - len(buffer))
            if not chunk:
                raise WebSocketError("CDP 连接中断")
            buffer += chunk
        return buffer

    def close(self) -> None:
        sock, self._socket = self._socket, None
        if sock is None:
            return
        try:
            self._send_control(0x8, b"")
        except Exception:  # noqa: BLE001
            pass
        try:
            sock.close()
        except OSError:
            pass


# --------------------------------------------------------------------------
# CDP source
# --------------------------------------------------------------------------


def _no_proxy_opener() -> urllib.request.OpenerDirector:
    """Localhost must never go through the corporate/system proxy."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def port_open(port: int, timeout: float = 0.25, host: str = CDP_HOST) -> bool:
    """Cheap liveness probe before paying for an HTTP round trip.

    A closed port is refused instantly on most machines but some security
    stacks simply drop the SYN, so the probe has its own short budget: a poll
    must never wait a full HTTP timeout just to learn nothing is listening.
    """
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def cdp_targets(port: int, timeout: float = 0.8) -> list[dict[str, Any]]:
    """DevTools target list. Short timeout: a closed port must not stall a poll."""
    if not port_open(port, timeout=min(0.25, timeout)):
        raise WebSocketError(f"调试端口 {port} 未开放")
    url = f"http://{CDP_HOST}:{port}/json/list"
    try:
        with _no_proxy_opener().open(url, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8", "replace"))
    except (OSError, ValueError) as exc:
        raise WebSocketError(f"调试端口 {port} 无响应（{exc}）") from exc
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def _chat_target_score(url: str) -> int:
    """How likely this page is the conversation surface. 0 means "not one".

    The desktop client no longer serves its chat on https://www.doubao.com.
    Depending on the build the renderer is addressed as
    ``doubao://doubao-chat/chat``, ``doubao://doubao-launcher/chat?viewId=101``
    or ``chrome://doubao-chat/chat``; the old "http(s) only" rule therefore
    matched nothing at all and every poll fell through to the window reader.
    Ranking is by surface, because the launcher page is the same app but its
    text tail is dominated by the sidebar's conversation list.
    """
    if any(marker in url for marker in NON_CHAT_URL_MARKERS):
        return 0
    if "doubao-chat" in url:
        return 40
    if "doubao-launcher" in url:
        return 30
    if "doubao.com" in url and "/chat" in url:
        return 25
    if "doubao.com" in url:
        return 10
    return 0


def chat_targets(targets: list[dict[str, Any]], limit: int = 3) -> list[dict[str, Any]]:
    """Every page that could be the chat, best first.

    More than one is returned on purpose: the client keeps several renderer
    windows alive and which one carries the open conversation varies by build,
    so the caller probes them until one shows a live reply.
    """
    scored: list[tuple[int, int, dict[str, Any]]] = []
    for index, item in enumerate(targets):
        if item.get("type") != "page" or not item.get("webSocketDebuggerUrl"):
            continue
        score = _chat_target_score(str(item.get("url") or "").lower())
        if score:
            scored.append((score, -index, item))
    scored.sort(key=lambda pair: (pair[0], pair[1]), reverse=True)
    return [item for _score, _order, item in scored[:max(1, limit)]]


def pick_chat_target(targets: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The single best chat page, out of the targets a Chromium app exposes."""
    found = chat_targets(targets, limit=1)
    return found[0] if found else None


def evaluate(ws_url: str, expression: str, timeout: float = 4.0) -> Any:
    """Run one expression in the page and return its value."""
    with MiniWebSocket(ws_url, timeout=timeout) as socket_client:
        socket_client.send(json.dumps({
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {"expression": expression, "returnByValue": True, "awaitPromise": False},
        }))
        for _ in range(20):
            message = json.loads(socket_client.recv())
            if message.get("id") != 1:
                continue  # skip unsolicited events
            if "error" in message:
                raise WebSocketError(str(message["error"]))
            result = message.get("result") or {}
            value = (result.get("result") or {}).get("value")
            if result.get("exceptionDetails"):
                raise WebSocketError(str(result["exceptionDetails"])[:200])
            return value
    raise WebSocketError("CDP 未返回结果")


def _probe(target: dict[str, Any], timeout: float = 2.0) -> dict[str, Any]:
    """Run PROBE_JS in one page and decode its answer. Never raises fatally."""
    raw = evaluate(str(target["webSocketDebuggerUrl"]), PROBE_JS, timeout=timeout)
    try:
        return json.loads(raw) if isinstance(raw, str) else {}
    except ValueError:
        return {}


def _probe_text(probe: dict[str, Any]) -> str:
    return "\n".join([
        "\n".join(str(item) for item in (probe.get("labels") or [])),
        # Only the live tail is scanned, never the whole transcript: a user or
        # an earlier answer that happens to contain "正在生成" would otherwise
        # pin the state to WORKING forever.
        str(probe.get("tail") or "")[-600:],
    ])


def cdp_snapshot(port: int) -> dict[str, Any] | None:
    """Read every candidate page and report the most active state seen.

    ``working`` wins across pages: a stop button only exists where tokens are
    actually streaming, so one page showing it outweighs the others. The title
    comes from the first page that names a real conversation rather than the
    bare app name, since the sidebar window keeps the generic title.
    """
    targets = chat_targets(cdp_targets(port))
    if not targets:
        raise WebSocketError("调试端口已开，但没有可读的豆包对话页面")
    state = "idle"
    title = ""
    session = ""
    href = ""
    errors: list[str] = []
    for target in targets:
        try:
            probe = _probe(target)
        except Exception as exc:  # noqa: BLE001 - one dead page must not stop the poll
            errors.append(str(exc))
            continue
        if not probe:
            continue
        candidate = classify_text(_probe_text(probe))
        if candidate == "working":
            state = "working"
        elif candidate == "thinking" and state == "idle":
            state = "thinking"
        name = conversation_title(probe.get("title"))
        if name and name.lower() not in APP_TITLES and not title:
            title, session, href = name, session_key(probe.get("href"), name), str(probe.get("href") or "")
        elif not href:
            href = str(probe.get("href") or "")
        if state == "working":
            break  # already the strongest signal; stop paying for more pages
    if not href and errors and state == "idle":
        raise WebSocketError("；".join(errors[:2]))
    final_title = title or conversation_title(targets[0].get("title"))
    return {
        "source": "cdp",
        "state": state,
        "title": final_title,
        "session": session or session_key(href, final_title),
        "url": href,
    }


# --------------------------------------------------------------------------
# UI Automation source
# --------------------------------------------------------------------------


def doubao_pids() -> set[int]:
    """PIDs of the Doubao desktop processes."""
    if os.name != "nt":
        return set()
    import subprocess

    try:
        output = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, encoding="mbcs", errors="replace", timeout=6,
            creationflags=0x08000000,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    pids: set[int] = set()
    for line in output.splitlines():
        parts = [item.strip('" ') for item in line.split('","')]
        if len(parts) >= 2 and parts[0].lower().startswith("doubao"):
            try:
                pids.add(int(parts[1]))
            except ValueError:
                pass
    return pids


def doubao_windows(pids: set[int] | None = None) -> list[tuple[int, str]]:
    """Visible top-level Doubao windows as ``(hwnd, title)``, largest first."""
    if os.name != "nt":
        return []
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    wanted = pids if pids is not None else doubao_pids()
    if not wanted:
        return []

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    found: list[tuple[int, int, str]] = []
    callback_type = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def callback(hwnd, _lparam):
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value not in wanted or not user32.IsWindowVisible(hwnd):
            return True
        title = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, title, 512)
        rect = RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        area = max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)
        # The floating assistant pill is 150-odd pixels wide and carries no
        # chat text; require a real window before spending an accessibility call.
        if area >= 40000:
            found.append((area, int(hwnd), title.value))
        return True

    user32.EnumWindows(callback_type(callback), 0)
    found.sort(reverse=True)
    return [(hwnd, title) for _area, hwnd, title in found]


def uia_text(hwnd: int, max_controls: int = 900, max_depth: int = 24) -> str:
    """Concatenate visible control names under one window.

    A Chromium page only exposes its text once the tree is walked deep enough —
    a 260-control budget stops inside the browser chrome and sees no chat text
    at all — so the budget here is sized for a full page. The walk stops early
    as soon as a stop-word shows up, because that already decides the state.
    """
    import uiautomation as auto  # imported lazily: Windows only, optional

    root = auto.ControlFromHandle(hwnd)
    if root is None:
        return ""
    parts: list[str] = []
    stack: list[tuple[Any, int]] = [(root, 0)]
    seen = 0
    while stack and seen < max_controls:
        node, depth = stack.pop()
        if depth > max_depth:
            continue
        try:
            children = node.GetChildren()
        except Exception:  # noqa: BLE001 - a dying window raises here
            continue
        for child in children:
            seen += 1
            if seen > max_controls:
                break
            try:
                name = (child.Name or "").strip()
                kind = child.ControlTypeName or ""
            except Exception:  # noqa: BLE001
                continue
            if name:
                parts.append(f"{kind}\t{name[:120]}")
                if any(word in name for word in STOP_WORDS) or any(word in name for word in BUSY_WORDS):
                    return "\n".join(parts)
            stack.append((child, depth + 1))
    return "\n".join(parts)


def uia_snapshot() -> dict[str, Any] | None:
    try:
        import uiautomation  # noqa: F401
    except ImportError as exc:
        # Surfaced instead of swallowed: "no window" and "no uiautomation" look
        # identical from outside, and the second one is a packaging bug.
        raise ImportError("未安装 uiautomation，窗口读取不可用") from exc
    windows = doubao_windows()
    if not windows:
        raise LookupError("豆包窗口未打开，改用调试端口才能读到后台状态")
    best: tuple[int, dict[str, Any]] | None = None
    for hwnd, title in windows:
        text = uia_text(hwnd)
        # The floating "工作精灵" pill is a real window but carries almost no
        # text; treating it as the chat surface produced a bogus idle reading
        # with "豆包工作精灵" as the conversation name.
        if len(text) < MIN_WINDOW_TEXT:
            continue
        state = classify_text(text)
        score = len(text) + (100000 if state != "idle" else 0)
        if best is None or score > best[0]:
            best = (score, {
                "source": "uia",
                "state": state,
                "title": conversation_title(title),
                "session": session_key("", title),
                "url": "",
            })
    if best is None:
        raise LookupError("没有可读的豆包聊天窗口（浮窗不算），改用调试端口才能读到后台状态")
    return best[1]


# --------------------------------------------------------------------------
# facade
# --------------------------------------------------------------------------


def _fixed_drive_roots() -> list[str]:
    """Roots of the fixed disks, so an install on D:/F: is still found."""
    if os.name != "nt":
        return []
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetLogicalDrives.restype = ctypes.c_uint
    kernel32.GetDriveTypeW.argtypes = [ctypes.c_wchar_p]
    mask = int(kernel32.GetLogicalDrives())
    roots: list[str] = []
    for index in range(26):
        if not mask & (1 << index):
            continue
        root = f"{chr(ord('A') + index)}:\\"
        if kernel32.GetDriveTypeW(root) == 3:  # DRIVE_FIXED
            roots.append(root)
    return roots


def _exe_from(raw: object) -> Path | None:
    """Turn a loose value (exe, folder, or ``"path",0``) into Doubao.exe."""
    text = str(raw or "").strip().strip('"')
    if not text:
        return None
    text = text.split(",")[0].strip().strip('"')
    path = Path(text)
    if path.name.lower() == "doubao.exe":
        return path
    for suffix in ("Doubao.exe", "app/Doubao.exe", "Doubao/Doubao.exe", "Doubao/app/Doubao.exe"):
        candidate = path / suffix
        if candidate.is_file():
            return candidate
    return None


def _reg_value(key: Any, name: str) -> str | None:
    import winreg

    try:
        value = winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None
    return str(value) if value else None


def _registry_candidates() -> list[Path]:
    """Paths Doubao's installer recorded (App Paths and Uninstall entries)."""
    if os.name != "nt":
        return []
    import winreg

    found: list[Path] = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        key = r"Software\Microsoft\Windows\CurrentVersion\App Paths\Doubao.exe"
        try:
            with winreg.OpenKey(hive, key) as handle:
                value = _reg_value(handle, "")
                if value:
                    found.append(Path(value.strip('"')))
        except OSError:
            continue
    for hive, key in (
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
    ):
        try:
            with winreg.OpenKey(hive, key) as root:
                names = []
                for index in range(winreg.QueryInfoKey(root)[0]):
                    try:
                        names.append(winreg.EnumKey(root, index))
                    except OSError:
                        break
        except OSError:
            continue
        for name in names:
            try:
                with winreg.OpenKey(hive, f"{key}\\{name}") as sub:
                    display = _reg_value(sub, "DisplayName") or ""
                    if "豆包" not in display and "doubao" not in display.lower():
                        continue
                    for field in ("DisplayIcon", "InstallLocation"):
                        value = _reg_value(sub, field)
                        if value:
                            found.append(Path(value.strip('"')))
            except OSError:
                continue
    return found


def _running_doubao_exe() -> str | None:
    """Image path of the running Doubao — always the truth when it is up."""
    if os.name != "nt":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.QueryFullProcessImageNameW.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_ulong)
    ]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    for pid in sorted(doubao_pids()):
        handle = kernel32.OpenProcess(0x1000, 0, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            continue
        try:
            size = ctypes.c_ulong(1024)
            buffer = ctypes.create_unicode_buffer(1024)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                path = buffer.value
                if path and path.lower().endswith("doubao.exe"):
                    return path
        finally:
            kernel32.CloseHandle(handle)
    return None


def doubao_exe_path(configured: object = None) -> str | None:
    """Full path of the Doubao executable.

    Order matters: an explicit setting wins, then the running process (always
    right), then the installer's own records, then the usual folders on every
    fixed drive — Doubao is often installed on a spare disk, so ``%ProgramFiles%``
    alone is not enough.
    """
    if os.name != "nt":
        return None
    explicit = _exe_from(configured)
    if explicit and explicit.is_file():
        return str(explicit)
    running = _running_doubao_exe()
    if running:
        return running
    guesses: list[Path] = _registry_candidates()
    for root in _fixed_drive_roots():
        guesses.append(Path(root) / "Program Files" / "Doubao" / "app" / "Doubao.exe")
        guesses.append(Path(root) / "Program Files (x86)" / "Doubao" / "app" / "Doubao.exe")
        guesses.append(Path(root) / "Program Files" / "Doubao" / "Doubao.exe")
        guesses.append(Path(root) / "Doubao" / "app" / "Doubao.exe")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        guesses.append(Path(local) / "Doubao" / "Application" / "Doubao.exe")
        guesses.append(Path(local) / "Programs" / "Doubao" / "Doubao.exe")
    roaming = os.environ.get("APPDATA")
    if roaming:
        guesses.append(Path(roaming) / "Doubao" / "Doubao.exe")
    for guess in guesses:
        resolved = _exe_from(guess)
        if resolved and resolved.is_file():
            return str(resolved)
    return None


class DoubaoRunning(RuntimeError):
    """Doubao is up without a debug port, so it has to be restarted."""


def debug_port_ready(port: int, timeout: float = 1.0) -> bool:
    try:
        return bool(cdp_targets(port, timeout=timeout))
    except WebSocketError:
        return False


def quit_doubao(timeout: float = 6.0) -> bool:
    """Close Doubao so it can come back with the debug flag.

    Tries a graceful close first — the tray icon honours it — and only falls
    back to a forced kill if the app refuses to go. Returns True when no Doubao
    process is left.
    """
    import subprocess

    if not doubao_pids():
        return True
    flags = 0x08000000  # CREATE_NO_WINDOW: no console flash
    subprocess.run(["taskkill", "/IM", "Doubao.exe"], capture_output=True,
                   creationflags=flags, timeout=20)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not doubao_pids():
            return True
        time.sleep(0.4)
    subprocess.run(["taskkill", "/F", "/IM", "Doubao.exe"], capture_output=True,
                   creationflags=flags, timeout=20)
    deadline = time.time() + 6
    while time.time() < deadline:
        if not doubao_pids():
            return True
        time.sleep(0.3)
    return not doubao_pids()


def launch_debug(
    port: int = DEFAULT_CDP_PORT,
    configured_exe: object = None,
    allow_restart: bool = False,
    wait: float = 2.5,
) -> str:
    """Start Doubao with its debug port open. Returns a human readable status.

    Doubao is single-instance, so the flag cannot be injected into a running
    copy. With ``allow_restart`` the caller has already asked the user and the
    running app is closed first; otherwise ``DoubaoRunning`` lets the UI explain
    the choice instead of silently killing something the user is typing in.
    """
    import subprocess

    if debug_port_ready(port):
        return f"豆包调试端口 {port} 已在监听，无需重新启动"
    if doubao_pids():
        if not allow_restart:
            raise DoubaoRunning("豆包已在运行，但不是调试模式。需要先关闭它再以调试模式重启。")
        if not quit_doubao():
            raise RuntimeError("无法关闭正在运行的豆包，请从托盘退出后重试")
    exe = doubao_exe_path(configured_exe)
    if not exe:
        raise FileNotFoundError(
            "找不到 Doubao.exe。请在「豆包可执行文件」里填写完整路径（例如 "
            r"F:\Program Files\Doubao\app\Doubao.exe）"
        )
    subprocess.Popen(
        [exe, "--remote-debugging-address=127.0.0.1", f"--remote-debugging-port={port}"],
        cwd=os.path.dirname(exe) or None,
        creationflags=0x00000008,  # DETACHED_PROCESS: Doubao is its own app
        close_fds=True,
    )
    deadline = time.time() + max(0.0, wait)
    while time.time() < deadline:
        if debug_port_ready(port, timeout=0.3):
            return f"已用调试端口 {port} 重启豆包，可以最小化或收进托盘了"
        time.sleep(0.3)
    return f"已启动豆包，正在等待调试端口 {port} 就绪（首次通常几秒）"


class DoubaoReader:
    """Best available read-only view of what Doubao is doing right now."""

    def __init__(self, port: int = DEFAULT_CDP_PORT, logger: logging.Logger | None = None) -> None:
        self.port = int(port or DEFAULT_CDP_PORT)
        self.log = logger or logging.getLogger("codex-status-monitor")
        self.last_source = ""
        self.last_error = ""

    def snapshot(self) -> dict[str, Any] | None:
        """Current state, or None when Doubao cannot be read at all."""
        errors: list[str] = []
        try:
            cdp = cdp_snapshot(self.port)
        except Exception as exc:  # noqa: BLE001 - fall through to the window read
            cdp = None
            errors.append(str(exc))
        if cdp:
            self.last_source = "cdp"
            self.last_error = ""
            return cdp
        try:
            window = uia_snapshot()
        except Exception as exc:  # noqa: BLE001
            window = None
            errors.append(str(exc))
        if window:
            self.last_source = "uia"
            self.last_error = ""
            return window
        if not errors:
            errors.append("没有可见的豆包窗口")
        self.last_error = "；".join(errors)
        return None
