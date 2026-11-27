"""Diagnose the Doubao channel: what is visible, and what would be published.

    .venv\\Scripts\\python.exe tools\\doubao_probe.py [--port 19226] [--tree]

Prints the Doubao processes, its visible windows, whether the debug port
answers, and the snapshot the monitor itself would use. With ``--tree`` it
also dumps the accessibility tree of the biggest window, which is the quickest
way to see why a state was or was not detected.

Read-only: no clicks, no navigation, one ``Runtime.evaluate`` at most.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import doubao_reader as dr  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Doubao channel diagnostics")
    parser.add_argument("--port", type=int, default=dr.DEFAULT_CDP_PORT)
    parser.add_argument("--tree", action="store_true", help="dump the UIA tree of the largest window")
    parser.add_argument("--depth", type=int, default=3, help="tree depth for --tree")
    args = parser.parse_args()

    pids = dr.doubao_pids()
    print(f"豆包进程: {sorted(pids) or '未运行'}")
    for hwnd, title in dr.doubao_windows(pids):
        print(f"  窗口 0x{hwnd:X} {title!r}")

    print(f"\n调试端口 {args.port}: ", end="")
    try:
        targets = dr.cdp_targets(args.port)
    except Exception as exc:  # noqa: BLE001
        print(f"不可用（{exc}）")
        targets = []
    else:
        print(f"共 {len(targets)} 个 target")
        for item in targets:
            print(f"  {item.get('type'):<15} {str(item.get('url'))[:70]}")
        print(f"  选中: {dr.pick_chat_target(targets) and 'chat 页面'}")

    if targets:
        try:
            print("\nCDP 快照:")
            print(json.dumps(dr.cdp_snapshot(args.port), ensure_ascii=False, indent=2))
        except Exception as exc:  # noqa: BLE001
            print(f"  读取失败: {exc}")

    if args.tree:
        windows = dr.doubao_windows(pids)
        if windows:
            hwnd = windows[0][0]
            print(f"\nUIA 树（0x{hwnd:X}，前 60 行）:")
            for line in dr.uia_text(hwnd).splitlines()[:60]:
                print("  " + line[:110])
        else:
            print("\n没有可见的豆包窗口，UIA 无内容")

    print("\n即将发布的状态:")
    print(json.dumps(dr.DoubaoReader(args.port).snapshot(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
