"""Tk front-end for the standalone Codex Status Monitor.

Minimal flow: fill the device id, tick the platforms to watch, press start.
Tasks are discovered and tracked automatically (newest N per platform).
"""
from __future__ import annotations

import json
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from typing import Any

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

FROZEN = bool(getattr(sys, "frozen", False))
ROOT = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "monitor_config.json"


def default_config() -> dict[str, Any]:
    """Built-in defaults only; no external file is required at runtime."""
    return normalize_config(DEFAULT_CONFIG)


class MonitorApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Codex Status Monitor · Codex / Workbuddy → MQTT")
        self.geometry("1020x720")
        self.minsize(900, 620)
        self.config_data = self._load_config()
        self.manager: MonitorManager | None = None
        self.vars: dict[str, tk.Variable] = {}
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(1000, self._tick)

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

        actions = ttk.Frame(tab)
        actions.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        ttk.Button(actions, text="预览将跟踪的任务", command=self._preview).pack(side="left")
        ttk.Label(actions, text="（只读扫描，不影响监听）", foreground="#666").pack(side="left", padx=8)

        self.preview_output = ScrolledText(tab, height=7, state="disabled", wrap="word", font=("Consolas", 9))
        self.preview_output.grid(row=4, column=0, sticky="nsew", pady=(8, 0))
        tab.rowconfigure(4, weight=1)

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
        ttk.Label(tab, text="运行日志", font=("Segoe UI", 10, "bold")).grid(row=2, column=0, columnspan=2, sticky="w", pady=(10, 4))
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

    def _start(self) -> None:
        if self.manager and self.manager.running:
            return
        try:
            config = self._collect_config()
        except ValueError as exc:
            messagebox.showerror("配置无效", str(exc))
            return
        if not self._persist(config, silent=True):
            return
        manager = MonitorManager(
            config,
            lambda message: self.after(0, lambda: self._log(message)),
            lambda item: self.after(0, lambda: self._observation(item)),
        )
        try:
            manager.start()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("启动失败", str(exc))
            return
        self.manager = manager
        self.state_label.configure(text="● 监听中", foreground="#087f5b")

    def _stop(self) -> None:
        if self.manager:
            self.manager.stop()
            self.manager = None
        self.state_label.configure(text="● 已停止", foreground="#a33")
        self.link_label.configure(text="")

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

    def _close(self) -> None:
        self._stop()
        self.destroy()


if __name__ == "__main__":
    MonitorApp().mainloop()
