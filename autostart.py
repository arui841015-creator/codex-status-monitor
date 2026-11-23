"""Per-user "start with system" registration for Windows and macOS.

======================  ======================================================
Windows                 HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run
macOS                   ~/Library/LaunchAgents/com.sdgods.codexstatusmonitor.plist
======================  ======================================================

Both mechanisms are user-scoped: no administrator rights, nothing installed
system-wide, and disabling removes the entry completely so the "just delete the
folder" uninstall story stays intact.

The registered command always carries ``--minimized``: at login the app starts
hidden and parks itself in the notification area instead of opening a window.

On macOS the plist is written but deliberately **not** bootstrapped with
``launchctl``: bootstrapping would start a second copy immediately, while the
user is still looking at the window whose checkbox they just ticked. The entry
takes effect at the next login, which is exactly what "start with system"
means.
"""
from __future__ import annotations

import plistlib
import shlex
import sys
from pathlib import Path

try:
    import winreg
except ImportError:  # pragma: no cover - non-Windows platforms
    winreg = None  # type: ignore[assignment]

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "CodexStatusMonitor"
START_FLAG = "--minimized"

APP_LABEL = "com.sdgods.codexstatusmonitor"
LAUNCH_AGENT = Path.home() / "Library" / "LaunchAgents" / f"{APP_LABEL}.plist"

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"


def supported() -> bool:
    """True when this platform has a user-scoped startup entry we can write."""
    if IS_WINDOWS:
        return winreg is not None
    return IS_MACOS


def platform_name() -> str:
    if IS_WINDOWS:
        return "Windows"
    if IS_MACOS:
        return "macOS"
    return sys.platform


def location_hint() -> str:
    """Where the entry lives, for the UI hint. Kept short."""
    if IS_WINDOWS:
        return "当前用户的启动项（HKCU\\...\\Run）"
    if IS_MACOS:
        return "~/Library/LaunchAgents 下的用户级 LaunchAgent"
    return "当前系统不支持"


def _is_frozen() -> bool:
    """True inside a PyInstaller build.

    Split out so tests can pin it: behaviour differs meaningfully between a
    bundled app and a `python monitor_app.py` run.
    """
    return bool(getattr(sys, "frozen", False))


def launch_argv() -> list[str]:
    """Exact argument vector the startup entry should hold.

    Frozen builds point straight at the executable (inside
    ``CodexStatusMonitor.app/Contents/MacOS`` on macOS). Source runs prefer
    ``pythonw.exe`` on Windows so that no console window flashes at login.
    """
    if _is_frozen():
        return [str(Path(sys.executable).resolve()), START_FLAG]
    interpreter = Path(sys.executable).resolve()
    if IS_WINDOWS and interpreter.name.lower() == "python.exe":
        windowed = interpreter.with_name("pythonw.exe")
        if windowed.is_file():
            interpreter = windowed
    script = Path(__file__).resolve().parent / "monitor_app.py"
    return [str(interpreter), str(script), START_FLAG]


def launch_command() -> str:
    """The argv rendered as a single line, for logging and the UI hint."""
    argv = launch_argv()
    if IS_WINDOWS:
        # Windows registry values are parsed with the MSVC rules, not POSIX ones.
        return f'"{argv[0]}" ' + " ".join(argv[1:])
    return shlex.join(argv)


# ---------------------------------------------------------------- Windows


def _windows_raw() -> str | None:
    if winreg is None:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
    except (FileNotFoundError, OSError):
        return None
    return str(value)


# ---------------------------------------------------------------- macOS


def _mac_argv() -> list[str] | None:
    try:
        with LAUNCH_AGENT.open("rb") as handle:
            payload = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
    argv = payload.get("ProgramArguments") if isinstance(payload, dict) else None
    if not isinstance(argv, list) or not argv:
        return None
    return [str(item) for item in argv]


# ---------------------------------------------------------------- public API


def current_command() -> str | None:
    """The stored command, or None when no startup entry exists."""
    if IS_WINDOWS:
        return _windows_raw()
    if IS_MACOS:
        argv = _mac_argv()
        return shlex.join(argv) if argv else None
    return None


def is_enabled() -> bool:
    return current_command() is not None


def _up_to_date() -> bool:
    """Does the stored entry point at the current executable and flag?"""
    if IS_WINDOWS:
        raw = _windows_raw()
        return raw is not None and raw.strip() == launch_command()
    if IS_MACOS:
        return _mac_argv() == launch_argv()
    return False


def enable() -> str:
    """Write the startup entry and return the command that was stored."""
    if not supported():
        raise RuntimeError(f"当前系统不支持自动启动（{platform_name()}）")
    if IS_WINDOWS:
        if winreg is None:  # pragma: no cover - guarded by supported()
            raise RuntimeError("当前系统不支持自动启动（缺少 winreg）")
        command = launch_command()
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command)
        return command
    payload = {
        "Label": APP_LABEL,
        "ProgramArguments": launch_argv(),
        "RunAtLoad": True,
        "KeepAlive": False,
        # Aqua-only: never try to launch this GUI from an ssh session.
        "LimitLoadToSessionType": "Aqua",
        "ProcessType": "Interactive",
    }
    LAUNCH_AGENT.parent.mkdir(parents=True, exist_ok=True)
    with LAUNCH_AGENT.open("wb") as handle:
        plistlib.dump(payload, handle, fmt=plistlib.FMT_XML)
    return launch_command()


def disable() -> None:
    """Remove the startup entry. Missing entries are not an error."""
    if IS_WINDOWS:
        if winreg is None:
            return
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, VALUE_NAME)
        except (FileNotFoundError, OSError):
            pass
        return
    if IS_MACOS:
        try:
            LAUNCH_AGENT.unlink()
        except OSError:
            pass


def sync() -> bool:
    """Repair a stale entry after the program folder was moved.

    Returns True when an entry exists after syncing. A stale path silently
    disables autostart at the next login, which is confusing, so it is fixed
    as soon as the program runs again.
    """
    if not supported():
        return False
    if current_command() is None:
        return False
    # A source run must never rewrite an installed copy's entry: the command it
    # would write points at a project venv interpreter that the login session
    # may not be able to start at all. Repair only from the frozen app.
    if not _is_frozen():
        return True
    if not _up_to_date():
        try:
            enable()
        except (OSError, RuntimeError):
            pass
    return True
