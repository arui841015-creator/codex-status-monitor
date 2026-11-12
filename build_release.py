"""Create distributable packages for Codex Status Monitor.

Two artifacts are produced in the parent directory:

1. ``CodexStatusMonitor-v<version>-win64.zip`` -- end-user portable package.
   Extract, double-click the exe. No Python required.
2. ``codex-status-monitor-release.zip`` -- source + binary for developers.

The end-user package is skipped (with a warning) when no frozen build exists.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP_VERSION = "2.0.0"
OUT_SOURCE = ROOT.parent / "codex-status-monitor-release.zip"
OUT_USER = ROOT.parent / f"CodexStatusMonitor-v{APP_VERSION}-win64.zip"
FILES = [
    "README.md",
    "requirements.txt",
    "requirements-dev.txt",
    "monitor_config.example.json",
    "monitor_app.py",
    "start-monitor.pyw",
    "monitor_engine.py",
    "codex_reader.py",
    "test_monitor_engine.py",
    "install.bat",
    "start-monitor.bat",
    "build-exe.bat",
    "build_release.py",
]
# Places a frozen onedir build may live. build-exe.bat writes build/dist;
# earlier packaging runs staged into ../codex-status-monitor-bin.
# Set CSM_DIST to package a build from a non-standard directory, which is
# required when a running instance keeps build/dist locked.
BINARY_CANDIDATES = [Path(os.environ["CSM_DIST"])] if os.environ.get("CSM_DIST") else [
    ROOT / "build" / "dist" / "CodexStatusMonitor",
    ROOT.parent / "codex-status-monitor-bin" / "CodexStatusMonitor",
]

GUIDE = """Codex Status Monitor v{version}
================================

这是什么
--------
一个 Windows 小程序：读取本机 Codex Desktop / Workbuddy 的任务状态，
通过 MQTT 上报给设备（小智等）。它只读状态，不改动任何任务数据。

怎么用（三步）
--------------
1. 解压本压缩包到任意目录（例如 D:\\Tools\\CodexStatusMonitor）。
   注意：请解压后运行，不要在压缩包里直接双击。
2. 双击 CodexStatusMonitor.exe。
3. 填写 Device ID，勾选要监听的程序（Codex Desktop / Workbuddy），
   点「启动监听」。

Device ID 就是设备的唯一编号（例如 sdg-ecda3b6717f0），
必须和订阅端一致，否则上报了对端也收不到。

界面说明
--------
- ① 设备身份：填 Device ID，下方会实时显示最终上报主题。
- ② 勾选渠道：Codex Desktop / Workbuddy 可多选。
  「跟踪任务数」默认 1，即每个渠道只跟踪最新的那条任务；
  改成 N 则跟踪最新 N 条，出现新任务时自动接管、最旧的自动移出。
- ③ MQTT Broker：已预填默认服务器，需要时才改。

点「预览将跟踪的任务」可以先只读看看当前会监听哪几条，不影响运行。

配置文件
--------
首次点「启动监听」后，同目录会生成 monitor_config.json。
删除该文件即恢复默认设置。该文件可能包含 MQTT 密码，请勿外发。

卸载
----
直接删除整个文件夹即可，不写注册表、不装服务、不留残留。

常见问题
--------
- 双击后没有窗口：可能被杀毒软件拦截，把该文件夹加入白名单后重试。
- 只勾 Codex 但本机没装 Codex：会提示找不到 Codex，取消勾选 Codex 即可。
- 上报不到设备：先确认 Device ID 与订阅端完全一致，再看「运行日志」页
  是否显示 MQTT 已连接。
- 程序不会弹出黑色命令行窗口；如果看到 CMD 窗口闪一下，说明用的是旧版
  本，请使用本包内的 exe。

系统要求
--------
Windows 10 / 11（64 位）。不需要安装 Python 或任何运行库。
"""


def find_binary() -> Path | None:
    for candidate in BINARY_CANDIDATES:
        if (candidate / "CodexStatusMonitor.exe").is_file():
            return candidate
    return None


def _reset(path: Path) -> Path:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    return path


def build_source_package(binary: Path | None) -> Path:
    staging = _reset(ROOT.parent / "_csm_stage_source")
    package = staging / "codex-status-monitor"
    package.mkdir()
    for name in FILES:
        source = ROOT / name
        if not source.is_file():
            raise SystemExit(f"missing release file: {source}")
        shutil.copy2(source, package / name)
    if binary is None:
        print("WARNING: no frozen build found, packaging sources only.")
        print("Run build-exe.bat first, then re-run this script for a bundled release.")
        for candidate in BINARY_CANDIDATES:
            print(f"  looked in: {candidate}")
    else:
        shutil.copytree(binary, package / "app")
        # Frozen builds resolve persistent settings beside the executable.
        shutil.copy2(ROOT / "monitor_config.example.json", package / "app" / "monitor_config.example.json")
        print(f"bundled binary from: {binary}")
    if OUT_SOURCE.exists():
        OUT_SOURCE.unlink()
    shutil.make_archive(str(OUT_SOURCE.with_suffix("")), "zip", staging, "codex-status-monitor")
    shutil.rmtree(staging)
    return OUT_SOURCE


def build_user_package(binary: Path) -> Path:
    """Portable package: one folder, exe on top, nothing to install."""
    staging = _reset(ROOT.parent / "_csm_stage_user")
    app = staging / "CodexStatusMonitor"
    shutil.copytree(binary, app)
    # Nothing that belongs to this machine may ship to end users.
    for junk in ("monitor_config.json", "smoke_err.log"):
        leftover = app / junk
        if leftover.exists():
            leftover.unlink()
    shutil.copy2(ROOT / "monitor_config.example.json", app / "monitor_config.example.json")
    # UTF-8 with BOM so Windows Notepad shows Chinese correctly. Kept inside the
    # app folder so extracting the zip yields exactly one top-level directory.
    (app / "README.txt").write_text(GUIDE.format(version=APP_VERSION), encoding="utf-8-sig")
    if OUT_USER.exists():
        OUT_USER.unlink()
    shutil.make_archive(str(OUT_USER.with_suffix("")), "zip", staging)
    shutil.rmtree(staging)
    return OUT_USER


def main() -> int:
    binary = find_binary()
    source_zip = build_source_package(binary)
    print(f"source package: {source_zip} ({source_zip.stat().st_size / 1048576:.1f} MB)")
    if binary is None:
        print("user package: SKIPPED (build the exe first)")
        return 0
    user_zip = build_user_package(binary)
    print(f"user package:   {user_zip} ({user_zip.stat().st_size / 1048576:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
