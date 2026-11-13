"""Tk front-end for the standalone Codex Status Monitor.

Minimal flow: fill the device id, tick the platforms to watch, press start.
Tasks are discovered and tracked automatically (newest N per platform).
"""
from __future__ import annotations

import json
import os
import sys
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from typing import Any

import autostart
from monitor_engine import (
    DEFAULT_CONFIG,
    DEFAULT_DATABASE,
    DEFAULT_MAX_TASKS,
    MonitorManager,
    enabled_platforms,
    normalize_config,
    status_topic,
    validate_device_id,
)
from tray_icon import AVAILABLE as TRAY_AVAILABLE
from tray_icon import IMPORT_ERROR as TRAY_IMPORT_ERROR
from tray_icon import TrayIcon

FROZEN = bool(getattr(sys, "frozen", False))
ROOT = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "monitor_config.json"
LOG_DIR = Path(os.environ.get("LOCALAPPDATA") or ROOT) / "CodexStatusMonitor"
LOG_PATH = LOG_DIR / "monitor.log"
LOG_MAX_BYTES = 512 * 1024

LOCK_NAME = "Local\\CodexStatusMonitor.SingleInstance"
MINIMIZED_FLAGS = {"--minimized", "--tray", "--startup", "-m"}
_lock_handle = None


def default_config() -> dict[str, Any]:
    """Built-in defaults only; no external file is required at runtime."""
    return normalize_config(DEFAULT_CONFIG)


def parse_args(argv: list[str]) -> bool:
    """True when the process should boot hidden into the tray (autostart)."""
    return any(str(item).strip().lower() in MINIMIZED_FLAGS for item in argv)


def acquire_instance_lock() -> bool:
    """Hold a named mutex so a second copy cannot double-publish to the broker.

    Autostart plus a manual launch is the realistic collision: two instances on
    the same device topic make the state flap. Returns True for the lucky one.
    """
    global _lock_handle
    if sys.platform != "win32":
        return True
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.CreateMutexW(None, 0, LOCK_NAME)
        if not handle:
            return True
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(handle)
            return False
        _lock_handle = handle
        return True
    except Exception:  # noqa: BLE001 - never block startup on this
        return True


def warn_already_running() -> None:
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(
            None,
            "Codex Status Monitor 已在运行（可能已最小化到系统托盘）。\n"
            "请在托盘图标上右键打开主界面。",
            "Codex Status Monitor",
            0x00000040,
        )
    except Exception:  # noqa: BLE001
        pass


class MonitorApp(tk.Tk):
    def __init__(self, start_minimized: bool = False) -> None:
        super().__init__()
        # Withdraw before building widgets so an autostart launch never flashes
        # a window on the desktop.
        if start_minimized:
            self.withdraw()
        self.title("Codex Status Monitor · Codex / Workbuddy → MQTT")
        self.geometry("1020x720")
        self.minsize(900, 620)
        self.config_data = self._load_config()
        self.manager: MonitorManager | None = None
        self.vars: dict[str, tk.Variable] = {}
        self.tray: TrayIcon | None = None
        self._build()
        self._build_tray()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(1000, self._tick)
        if start_minimized:
            self.after(400, self._startup)

    # ---------------- lifecycle ----------------

    def _load_config(self) -> dict[str, Any]:
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        return normalize_config(raw)

    def _build(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        header = ttk.Frame(self, padding=(14, 10))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(2, weight=1)
        ttk.Label(header, text="Codex Status Monitor", font=("Segoe UI", 16, "bold")).grid(row=0, column=0, sticky="w")
        self.state_label = ttk.Label(header, text="● 已停止", foreground="#a33")
        self.state_label.grid(row=0, column=1, sticky="w", padx=16)
        ttk.Button(header, text="保存配置", command=self._save_interactive).grid(row=0, column=3, padx=4)
        ttk.Button(header, text="启动监听", command=self._start).grid(row=0, column=4, padx=4)
        ttk.Button(header, text="停止监听", command=self._stop).grid(row=0, column=5, padx=4)
        self.link_label = ttk.Label(header, text="", foreground="#666")
        self.link_label.grid(row=1, column=0, columnspan=6, sticky="w", pady=(6, 0))

        notebook = ttk.Notebook(self)
        notebook.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))
        self.setup_tab = ttk.Frame(notebook, padding=12)
        self.live_tab = ttk.Frame(notebook, padding=12)
        notebook.add(self.setup_tab, text="设备 / 监听")
        notebook.add(self.live_tab, text="上报与日志")
        self._build_setup_tab()
        self._build_live_tab()

    # ---------------- setup tab ----------------

    def _build_setup_tab(self) -> None:
        tab = self.setup_tab
        tab.columnconfigure(0, weight=1)

        identity = ttk.LabelFrame(tab, text="① 设备身份（唯一必填）", padding=12)
        identity.grid(row=0, column=0, sticky="ew")
        identity.columnconfigure(1, weight=1)
        mqtt = self.config_data.get("mqtt") or {}
        identity_cfg = self.config_data.get("identity") or {}
        self.vars["device_id"] = tk.StringVar(value=identity_cfg.get("device_id", ""))
        ttk.Label(identity, text="Device / Driver ID").grid(row=0, column=0, sticky="w")
        entry = ttk.Entry(identity, textvariable=self.vars["device_id"], width=32, font=("Consolas", 10))
        entry.grid(row=0, column=1, sticky="w", padx=(10, 0))
        entry.focus_set()
        self.topic_label = ttk.Label(identity, text="主题：待填写", foreground="#666")
        self.topic_label.grid(row=1, column=0, columnspan=3, sticky="w", pady=(8, 0))
        self.vars["device_id"].trace_add("write", lambda *_: self._update_topic())
        self._update_topic()

        channels = ttk.LabelFrame(tab, text="② 勾选要监听的程序（可多选，自动跟踪最近任务）", padding=12)
        channels.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        for col, weight in ((2, 0), (3, 0), (4, 1)):
            channels.columnconfigure(col, weight=weight)

        codex = self.config_data["watch"]["codex"]
        workbuddy = self.config_data["watch"]["workbuddy"]
        self.vars["codex_enabled"] = tk.BooleanVar(value=bool(codex.get("enabled", True)))
        self.vars["codex_max"] = tk.StringVar(value=str(codex.get("max_tasks", DEFAULT_MAX_TASKS)))
        self.vars["codex_interval"] = tk.StringVar(value=str(codex.get("interval", 1.5)))
        self.vars["codex_exe"] = tk.StringVar(value=str(codex.get("executable") or "codex"))
        self.vars["wb_enabled"] = tk.BooleanVar(value=bool(workbuddy.get("enabled", True)))
        self.vars["wb_max"] = tk.StringVar(value=str(workbuddy.get("max_tasks", DEFAULT_MAX_TASKS)))
        self.vars["wb_interval"] = tk.StringVar(value=str(workbuddy.get("interval", 2.0)))
        self.vars["wb_db"] = tk.StringVar(value=str(workbuddy.get("database") or DEFAULT_DATABASE))

        ttk.Checkbutton(channels, text="Codex Desktop", variable=self.vars["codex_enabled"]).grid(row=0, column=0, sticky="w")
        ttk.Label(channels, text="跟踪任务数").grid(row=0, column=2, sticky="e", padx=(18, 4))
        ttk.Spinbox(channels, from_=1, to=10, width=5, textvariable=self.vars["codex_max"]).grid(row=0, column=3, sticky="w")
        ttk.Label(channels, text="轮询秒").grid(row=0, column=4, sticky="w", padx=(12, 4))
        ttk.Entry(channels, width=6, textvariable=self.vars["codex_interval"]).grid(row=0, column=5, sticky="w")
        ttk.Label(channels, text="codex 可执行文件").grid(row=1, column=2, sticky="e", padx=(18, 4))
        ttk.Entry(channels, textvariable=self.vars["codex_exe"]).grid(row=1, column=3, columnspan=3, sticky="ew")

        ttk.Separator(channels).grid(row=2, column=0, columnspan=6, sticky="ew", pady=8)
        ttk.Checkbutton(channels, text="Workbuddy", variable=self.vars["wb_enabled"]).grid(row=3, column=0, sticky="w")
        ttk.Label(channels, text="跟踪任务数").grid(row=3, column=2, sticky="e", padx=(18, 4))
        ttk.Spinbox(channels, from_=1, to=10, width=5, textvariable=self.vars["wb_max"]).grid(row=3, column=3, sticky="w")
        ttk.Label(channels, text="轮询秒").grid(row=3, column=4, sticky="w", padx=(12, 4))
        ttk.Entry(channels, width=6, textvariable=self.vars["wb_interval"]).grid(row=3, column=5, sticky="w")
        ttk.Label(channels, text="数据库").grid(row=4, column=2, sticky="e", padx=(18, 4))
        ttk.Entry(channels, textvariable=self.vars["wb_db"]).grid(row=4, column=3, columnspan=3, sticky="ew")

        hint = ttk.Label(
            channels,
            text="无需选择具体任务：程序会自动取每个渠道最新的 N 个任务，出现新任务时自动接管，并把最旧的顶掉。",
            foreground="#666",
        )
        hint.grid(row=5, column=0, columnspan=6, sticky="w", pady=(10, 0))

        advanced = ttk.LabelFrame(tab, text="③ MQTT Broker（默认可用，可按需修改）", padding=12)
        advanced.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        for col in (1, 3, 5):
            advanced.columnconfigure(col, weight=1)
        self.vars["host"] = tk.StringVar(value=str(mqtt.get("host") or "117.72.43.16"))
        self.vars["port"] = tk.StringVar(value=str(mqtt.get("port") or 1883))
        self.vars["username"] = tk.StringVar(value=str(mqtt.get("username") or ""))
        self.vars["password"] = tk.StringVar(value=str(mqtt.get("password") or ""))
        self.vars["tls"] = tk.BooleanVar(value=bool(mqtt.get("tls")))
        ttk.Label(advanced, text="Host").grid(row=0, column=0, sticky="w")
        ttk.Entry(advanced, textvariable=self.vars["host"]).grid(row=0, column=1, sticky="ew", padx=(8, 14))
        ttk.Label(advanced, text="Port").grid(row=0, column=2, sticky="w")
        ttk.Entry(advanced, textvariable=self.vars["port"], width=8).grid(row=0, column=3, sticky="ew", padx=(8, 14))
        ttk.Checkbutton(advanced, text="TLS", variable=self.vars["tls"]).grid(row=0, column=4, columnspan=2, sticky="w")
        ttk.Label(advanced, text="用户名").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(advanced, textvariable=self.vars["username"]).grid(row=1, column=1, sticky="ew", padx=(8, 14), pady=(8, 0))
        ttk.Label(advanced, text="密码").grid(row=1, column=2, sticky="w", pady=(8, 0))
        ttk.Entry(advanced, textvariable=self.vars["password"], show="*").grid(row=1, column=3, sticky="ew", padx=(8, 14), pady=(8, 0))

        startup = ttk.LabelFrame(tab, text="④ 启动方式", padding=12)
        startup.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        startup.columnconfigure(0, weight=1)
        self.vars["autostart"] = tk.BooleanVar(value=autostart.is_enabled())
        ttk.Checkbutton(
            startup,
            text="随系统自动启动（登录后在后台静默运行，只驻留系统托盘）",
            variable=self.vars["autostart"],
            command=self._apply_autostart,
        ).grid(row=0, column=0, sticky="w")
        self.autostart_hint = ttk.Label(startup, text="", foreground="#666", wraplength=880, justify="left")
        self.autostart_hint.grid(row=1, column=0, sticky="w", pady=(6, 0))
        self._refresh_autostart_hint()

        actions = ttk.Frame(tab)
        actions.grid(row=4, column=0, sticky="ew", pady=(12, 0))
        ttk.Button(actions, text="预览将跟踪的任务", command=self._preview).pack(side="left")
        ttk.Label(actions, text="（只读扫描，不影响监听）", foreground="#666").pack(side="left", padx=8)

        self.preview_output = ScrolledText(tab, height=7, state="disabled", wrap="word", font=("Consolas", 9))
        self.preview_output.grid(row=5, column=0, sticky="nsew", pady=(8, 0))
        tab.rowconfigure(5, weight=1)

    def _build_live_tab(self) -> None:
        tab = self.live_tab
        tab.columnconfigure(0, weight=1)
        tab.columnconfigure(1, weight=1)
        tab.rowconfigure(1, weight=1)
        tab.rowconfigure(3, weight=1)
        ttk.Label(tab, text="读取到的任务状态", font=("Segoe UI", 10, "bold")).grid(row=0, column=0, sticky="w", pady=(0, 4))
        ttk.Label(tab, text="已上报到 MQTT 的内容", font=("Segoe UI", 10, "bold")).grid(row=0, column=1, sticky="w", pady=(0, 4))
        self.incoming_output = ScrolledText(tab, state="disabled", wrap="word", font=("Consolas", 9))
        self.incoming_output.grid(row=1, column=0, sticky="nsew", padx=(0, 6))
        self.outgoing_output = ScrolledText(tab, state="disabled", wrap="word", font=("Consolas", 9))
        self.outgoing_output.grid(row=1, column=1, sticky="nsew", padx=(6, 0))
        ttk.Label(tab, text="运行日志", font=("Segoe UI", 10, "bold")).grid(row=2, column=0, sticky="w", pady=(10, 4))
        ttk.Label(tab, text=f"后台运行时写入 {LOG_PATH}", foreground="#888780").grid(
            row=2, column=1, sticky="e", pady=(10, 4)
        )
        self.log_output = ScrolledText(tab, height=9, state="disabled", wrap="word", font=("Consolas", 9))
        self.log_output.grid(row=3, column=0, columnspan=2, sticky="nsew")

    # ---------------- helpers ----------------

    def _update_topic(self) -> None:
        value = self.vars.get("device_id")
        device_id = value.get().strip() if value else ""
        try:
            self.topic_label.configure(text=f"上报主题：{status_topic(device_id)}")
        except ValueError:
            self.topic_label.configure(text="上报主题：请填写有效 Device ID（3-64 位字母数字 . _ -）")

    def _collect_config(self) -> dict[str, Any]:
        device_id = str(self.vars["device_id"].get()).strip()
        validate_device_id(device_id)
        try:
            port = int(str(self.vars["port"].get()).strip())
        except ValueError as exc:
            raise ValueError("MQTT 端口必须是数字") from exc
        if not 1 <= port <= 65535:
            raise ValueError("MQTT 端口范围为 1-65535")
        try:
            codex_max = max(1, min(10, int(str(self.vars["codex_max"].get()).strip())))
            wb_max = max(1, min(10, int(str(self.vars["wb_max"].get()).strip())))
            codex_interval = max(0.2, float(str(self.vars["codex_interval"].get()).strip() or 1.5))
            wb_interval = max(0.2, float(str(self.vars["wb_interval"].get()).strip() or 2.0))
        except ValueError as exc:
            raise ValueError("跟踪任务数或轮询秒必须是数字") from exc
        config = normalize_config(self.config_data)
        config["identity"] = {"device_id": device_id}
        config["mqtt"] = {
            "host": str(self.vars["host"].get()).strip(),
            "port": port,
            "tls": bool(self.vars["tls"].get()),
            "username": str(self.vars["username"].get()),
            "password": str(self.vars["password"].get()),
            "qos": 1,
            "keepalive": 45,
            "reconnect_delay_seconds": 5,
        }
        config["watch"]["codex"] = {
            "enabled": bool(self.vars["codex_enabled"].get()),
            "max_tasks": codex_max,
            "interval": codex_interval,
            "executable": str(self.vars["codex_exe"].get()).strip() or "codex",
        }
        config["watch"]["workbuddy"] = {
            "enabled": bool(self.vars["wb_enabled"].get()),
            "max_tasks": wb_max,
            "interval": wb_interval,
            "database": str(self.vars["wb_db"].get()).strip() or DEFAULT_DATABASE,
        }
        if not enabled_platforms(config):
            raise ValueError("请至少勾选一个监听渠道")
        return config

    def _persist(self, config: dict[str, Any], silent: bool = False) -> bool:
        try:
            CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            if not silent:
                messagebox.showerror("保存失败", str(exc))
            return False
        self.config_data = config
        if not silent:
            self._log(f"配置已保存：{CONFIG_PATH}")
        return True

    def _save_interactive(self) -> None:
        try:
            config = self._collect_config()
        except ValueError as exc:
            messagebox.showerror("配置无效", str(exc))
            return
        self._persist(config)

    # ---------------- autostart ----------------

    def _refresh_autostart_hint(self) -> None:
        label = getattr(self, "autostart_hint", None)
        if label is None:
            return
        if not autostart.supported():
            label.configure(text="当前系统不支持自动启动（仅 Windows 可用）。")
            return
        command = autostart.current_command()
        if command:
            label.configure(
                text=f"已启用，登录后自动执行：{command}\n移动或重命名程序目录后，取消再重新勾选一次即可修正。"
            )
        else:
            label.configure(
                text="勾选后只写入当前用户的启动项（HKCU\\...\\Run），不需要管理员权限；取消勾选即完全移除。"
            )

    def _apply_autostart(self) -> None:
        wanted = bool(self.vars["autostart"].get())
        try:
            if wanted:
                command = autostart.enable()
                self._log(f"已启用随系统自动启动：{command}")
            else:
                autostart.disable()
                command = ""
                self._log("已取消随系统自动启动")
        except (OSError, RuntimeError) as exc:
            self.vars["autostart"].set(autostart.is_enabled())
            self._refresh_autostart_hint()
            messagebox.showerror("设置失败", f"无法修改启动项：{exc}")
            return
        self._refresh_autostart_hint()
        if wanted and command:
            self._log("下次登录将自动在后台监听，窗口只驻留系统托盘")

    # ---------------- actions ----------------

    def _preview(self) -> None:
        try:
            config = self._collect_config()
        except ValueError as exc:
            messagebox.showerror("配置无效", str(exc))
            return
        platforms = enabled_platforms(config)
        self._write_preview("正在扫描 ...", replace=True)

        def task() -> None:
            results: dict[str, Any] = {}
            for platform in platforms:
                manager = MonitorManager(config)
                try:
                    results[platform] = manager.preview(platform)
                except Exception as exc:  # noqa: BLE001
                    results[platform] = {"error": f"{type(exc).__name__}: {exc}"}
            text = json.dumps(results, ensure_ascii=False, indent=2)
            self.after(0, lambda: self._write_preview(text, replace=True))

        threading.Thread(target=task, daemon=True).start()

    def _write_preview(self, text: str, replace: bool = False) -> None:
        self.preview_output.configure(state="normal")
        if replace:
            self.preview_output.delete("1.0", "end")
        self.preview_output.insert("end", text + "\n")
        self.preview_output.see("end")
        self.preview_output.configure(state="disabled")

    def _start(self, quiet: bool = False) -> bool:
        if self.manager and self.manager.running:
            return True
        try:
            config = self._collect_config()
        except ValueError as exc:
            if quiet:
                self._log(f"自启动跳过：{exc}")
            else:
                messagebox.showerror("配置无效", str(exc))
            return False
        if not self._persist(config, silent=True):
            return False
        manager = MonitorManager(
            config,
            lambda message: self.after(0, lambda: self._log(message)),
            lambda item: self.after(0, lambda: self._observation(item)),
        )
        try:
            manager.start()
        except Exception as exc:  # noqa: BLE001
            if quiet:
                self._log(f"自启动失败：{exc}")
            else:
                messagebox.showerror("启动失败", str(exc))
            return False
        self.manager = manager
        self.state_label.configure(text="● 监听中", foreground="#087f5b")
        self._sync_tray()
        return True

    def _stop(self) -> None:
        if self.manager:
            self.manager.stop()
            self.manager = None
        self.state_label.configure(text="● 已停止", foreground="#a33")
        self.link_label.configure(text="")
        self._sync_tray()

    def _tick(self) -> None:
        """Refresh the header with live tracking / broker status."""
        try:
            if self.manager and self.manager.running:
                summary = self.manager.tracked_summary()
                parts = []
                for platform, label in (("codex", "Codex"), ("workbuddy", "Workbuddy")):
                    items = summary.get(platform) or {}
                    if not items:
                        continue
                    labels = "、".join(f"{item['project']}[{item['state']}]" for item in items.values())
                    parts.append(f"{label} {len(items)}: {labels}")
                publisher = self.manager.publisher
                broker = "MQTT 已连接" if publisher and publisher.connected else "MQTT 连接中"
                if publisher and publisher.last_error:
                    broker += f"（{publisher.last_error}）"
                self.link_label.configure(text=f"{broker} · " + ("；".join(parts) if parts else "等待首个任务 ..."))
            else:
                self.link_label.configure(text="填写 Device ID，勾选渠道，点「启动监听」即可。")
        except Exception:  # noqa: BLE001
            pass
        self._sync_tray()
        self.after(1200, self._tick)

    def _observation(self, item: dict[str, Any]) -> None:
        target = self.incoming_output if item.get("direction") == "incoming" else self.outgoing_output
        text = json.dumps(item.get("payload"), ensure_ascii=False, indent=2)
        target.configure(state="normal")
        target.insert("end", f"[{item.get('channel_id')}] {text}\n\n")
        target.see("end")
        target.configure(state="disabled")

    def _log(self, message: str) -> None:
        self.log_output.configure(state="normal")
        self.log_output.insert("end", message + "\n")
        self.log_output.see("end")
        self.log_output.configure(state="disabled")
        self._append_log_file(message)

    @staticmethod
    def _append_log_file(message: str) -> None:
        """Mirror the in-app log to disk so autostart runs are diagnosable."""
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            if LOG_PATH.is_file() and LOG_PATH.stat().st_size > LOG_MAX_BYTES:
                LOG_PATH.unlink()
            with LOG_PATH.open("a", encoding="utf-8") as handle:
                handle.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}\n")
        except OSError:
            pass

    # ---------------- tray ----------------

    def _build_tray(self) -> None:
        if not TRAY_AVAILABLE:
            self._log(f"系统托盘不可用（{TRAY_IMPORT_ERROR}），关闭窗口将直接退出")
            return
        tray = TrayIcon(
            "Codex Status Monitor",
            is_listening=lambda: bool(self.manager and self.manager.running),
            on_show=self._show_from_tray,
            on_toggle=self._toggle_from_tray,
            on_quit=self._quit_from_tray,
        )
        if not tray.start():
            self._log("系统托盘初始化失败，关闭窗口将直接退出")
            return
        self.tray = tray
        self._sync_tray()

    def _sync_tray(self) -> None:
        tray = self.tray
        if tray is None:
            return
        if not (self.manager and self.manager.running):
            tray.set_state("stopped", "Codex Status Monitor · 已停止")
            return
        publisher = self.manager.publisher
        if publisher is not None and not publisher.connected:
            tray.set_state("warning", "Codex Status Monitor · 监听中（MQTT 未连接）")
        else:
            tray.set_state("running", "Codex Status Monitor · 监听中")

    # All tray callbacks arrive on the tray thread; hop back to Tk.

    def _show_from_tray(self) -> None:
        self.after(0, self._do_show)

    def _do_show(self) -> None:
        self.deiconify()
        self.state("normal")
        self.lift()
        try:
            self.focus_force()
        except tk.TclError:
            pass

    def _toggle_from_tray(self) -> None:
        self.after(0, self._do_toggle)

    def _do_toggle(self) -> None:
        if self.manager and self.manager.running:
            self._stop()
        else:
            self._start()

    def _quit_from_tray(self) -> None:
        self.after(0, self._quit)

    def _quit(self) -> None:
        self._stop()
        if self.tray is not None:
            self.tray.stop()
            self.tray = None
        self.destroy()

    def _on_close(self) -> None:
        """The X button parks the app in the tray while listening continues."""
        if self.tray is not None:
            self.withdraw()
            self._log("窗口已最小化到系统托盘，监听继续运行（右键托盘图标可恢复或退出）")
            self.tray.notify("已最小化到系统托盘，监听继续运行。")
            return
        self._quit()

    def _startup(self) -> None:
        """Autostart path: start listening and stay hidden."""
        if self._start(quiet=True):
            self._log("自启动完成：已在后台监听，窗口驻留系统托盘")
            return
        self._do_show()
        self._log("自启动未完成（配置不完整），已打开主界面等待设置")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    minimized = parse_args(args)
    if not acquire_instance_lock():
        if not minimized:
            warn_already_running()
        return 0
    autostart.sync()
    MonitorApp(start_minimized=minimized).mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
