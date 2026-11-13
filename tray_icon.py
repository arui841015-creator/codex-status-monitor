"""Notification-area icon, backed by pystray.

Deliberately thin: the tray is a remote control for the Tk window, never a
second source of truth. Every callback runs on the tray thread, so callers must
marshal back to Tk themselves.

Import failures are swallowed into ``AVAILABLE = False``: a missing tray must
degrade to "closing the window quits" rather than crash the app.
"""
from __future__ import annotations

import threading
from typing import Callable

try:
    import pystray
    from PIL import Image, ImageDraw
except Exception as exc:  # noqa: BLE001 - tray is an optional capability
    pystray = None  # type: ignore[assignment]
    Image = None  # type: ignore[assignment]
    ImageDraw = None  # type: ignore[assignment]
    IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
else:
    IMPORT_ERROR = ""

AVAILABLE = pystray is not None

# Ring colour mirrors the header state lamp.
COLORS = {
    "running": (8, 127, 91),
    "warning": (186, 117, 23),
    "stopped": (163, 51, 51),
}

MAX_TITLE = 120


def _glyph(color: tuple[int, int, int], size: int = 64):
    """Draw a flat ring icon; no external .ico file needed."""
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((3, 3, size - 3, size - 3), fill=color)
    draw.ellipse((18, 18, size - 18, size - 18), fill=(255, 255, 255, 255))
    draw.ellipse((27, 27, size - 27, size - 27), fill=color)
    return image


class TrayIcon:
    def __init__(
        self,
        title: str,
        is_listening: Callable[[], bool],
        on_show: Callable[[], None],
        on_toggle: Callable[[], None],
        on_quit: Callable[[], None],
    ) -> None:
        self.title = title
        self._is_listening = is_listening
        self._on_show = on_show
        self._on_toggle = on_toggle
        self._on_quit = on_quit
        self._icon = None
        self._thread: threading.Thread | None = None
        self._state = "stopped"

    # ---------------- lifecycle ----------------

    def start(self) -> bool:
        """Create the icon and pump its message loop on a daemon thread."""
        if not AVAILABLE or self._icon is not None:
            return self._icon is not None
        try:
            menu = pystray.Menu(
                pystray.MenuItem("显示主界面", self._handle_show, default=True),
                pystray.MenuItem(self._toggle_text, self._handle_toggle),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出", self._handle_quit),
            )
            self._icon = pystray.Icon(
                "codex-status-monitor", _glyph(COLORS["stopped"]), self.title, menu
            )
            self._thread = threading.Thread(target=self._loop, name="csm-tray", daemon=True)
            self._thread.start()
        except Exception:  # noqa: BLE001
            self._icon = None
            return False
        return True

    def _loop(self) -> None:
        icon = self._icon
        try:
            icon.run()
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._icon = None

    def stop(self) -> None:
        icon = self._icon
        self._icon = None
        if icon is not None:
            try:
                icon.stop()
            except Exception:  # noqa: BLE001
                pass

    @property
    def active(self) -> bool:
        return self._icon is not None

    # ---------------- menu ----------------

    def _toggle_text(self, _item) -> str:
        try:
            return "暂停监听" if self._is_listening() else "启动监听"
        except Exception:  # noqa: BLE001
            return "启动监听"

    def _handle_show(self, _icon=None, _item=None) -> None:
        self._on_show()

    def _handle_toggle(self, _icon=None, _item=None) -> None:
        self._on_toggle()

    def _handle_quit(self, _icon=None, _item=None) -> None:
        self._on_quit()

    # ---------------- presentation ----------------

    def set_state(self, state: str, tooltip: str | None = None) -> None:
        icon = self._icon
        if icon is None:
            return
        changed = state != self._state
        self._state = state
        try:
            if changed:
                icon.icon = _glyph(COLORS.get(state, COLORS["stopped"]))
            if tooltip:
                icon.title = tooltip[:MAX_TITLE]
        except Exception:  # noqa: BLE001
            pass

    def notify(self, message: str) -> None:
        icon = self._icon
        if icon is None:
            return
        try:
            icon.notify(message, self.title)
        except Exception:  # noqa: BLE001
            pass
