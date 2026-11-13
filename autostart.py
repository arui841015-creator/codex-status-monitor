"""Windows "start with system" support via the per-user Run key.

Only ``HKCU`` is touched, so no administrator rights are required and nothing
is installed system-wide. Disabling removes the value completely, which keeps
the "just delete the folder" uninstall story intact.

The launched command always carries ``--minimized``: at login the app starts
hidden and parks itself in the notification area instead of opening a window.
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    import winreg
except ImportError:  # pragma: no cover - non-Windows platforms
    winreg = None  # type: ignore[assignment]

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "CodexStatusMonitor"
START_FLAG = "--minimized"


def supported() -> bool:
    """True when this platform has a per-user Run key we can write."""
    return winreg is not None and sys.platform == "win32"


def launch_command() -> str:
    """Exact command line the Run key should hold.

    Frozen builds point straight at the exe. Source runs prefer ``pythonw.exe``
    so that no console window flashes at login.
    """
    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable).resolve()}" {START_FLAG}'
    interpreter = Path(sys.executable).resolve()
    if interpreter.name.lower() == "python.exe":
        windowed = interpreter.with_name("pythonw.exe")
        if windowed.is_file():
            interpreter = windowed
    script = Path(__file__).resolve().parent / "monitor_app.py"
    return f'"{interpreter}" "{script}" {START_FLAG}'


def current_command() -> str | None:
    """The stored command, or None when no startup entry exists."""
    if not supported():
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
    except (FileNotFoundError, OSError):
        return None
    return str(value)


def is_enabled() -> bool:
    return current_command() is not None


def enable() -> str:
    """Write the startup entry and return the command that was stored."""
    if not supported():
        raise RuntimeError("当前系统不支持自动启动（仅 Windows 可用）")
    command = launch_command()
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command)
    return command


def disable() -> None:
    """Remove the startup entry. Missing entries are not an error."""
    if not supported():
        return
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, VALUE_NAME)
    except (FileNotFoundError, OSError):
        pass


def sync() -> bool:
    """Repair a stale entry after the program folder was moved.

    Returns True when an entry exists after syncing. A stale path silently
    disables autostart at the next login, which is confusing, so it is fixed
    as soon as the program runs again.
    """
    if not supported():
        return False
    existing = current_command()
    if existing is None:
        return False
    if existing.strip() != launch_command():
        try:
            enable()
        except (OSError, RuntimeError):
            pass
    return True
