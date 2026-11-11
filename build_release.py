"""Create a clean, self-contained source release zip for Codex Status Monitor."""
from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT.parent / "codex-status-monitor-release.zip"
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
]
# Places a frozen onedir build may live. build-exe.bat writes build/dist;
# earlier packaging runs staged into ../codex-status-monitor-bin.
BINARY_CANDIDATES = [
    ROOT / "build" / "dist" / "CodexStatusMonitor",
    ROOT.parent / "codex-status-monitor-bin" / "CodexStatusMonitor",
]


def find_binary() -> Path | None:
    for candidate in BINARY_CANDIDATES:
        if (candidate / "CodexStatusMonitor.exe").is_file():
            return candidate
    return None


def main() -> int:
    staging = ROOT.parent / "_codex-status-monitor-release"
    if staging.exists():
        shutil.rmtree(staging)
    package = staging / "codex-status-monitor"
    package.mkdir(parents=True)
    for name in FILES:
        source = ROOT / name
        if not source.is_file():
            raise SystemExit(f"missing release file: {source}")
        shutil.copy2(source, package / name)
    binary = find_binary()
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
    if OUT.exists():
        OUT.unlink()
    shutil.make_archive(str(OUT.with_suffix("")), "zip", staging, "codex-status-monitor")
    shutil.rmtree(staging)
    print(OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
