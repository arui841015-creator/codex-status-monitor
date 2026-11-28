"""Create the v2.3.0 GitHub Release and upload its assets (api-only path).

Same reason as gh_push_via_api: github.com is blocked, api.github.com and
uploads.github.com are not.

Usage: python tools/gh_release.py --tag v2.3.0 [--draft]
"""

from __future__ import annotations

import json
import mimetypes
import os
import subprocess
import sys
import urllib.request

API = "https://api.github.com"
UPLOADS = "https://uploads.github.com"
OWNER = "arui841015-creator"
REPO = "codex-status-monitor"

BODY = """豆包能读到了，但状态一直是「空闲」—— 因为这版界面根本没有「生成中」这几个字。

## 根因

豆包桌面版的「工作」模式界面里，判断是否在执行的常规信号全部失效：

| 原以为的信号 | 实测 |
|---|---|
| 「停止生成」按钮出现 | 停止控件 `chat_input_local_break_button` 常显，空闲时也在 |
| 发送按钮变成停止 | 发送按钮外层永久带 `!hidden`，一直是 0×0 |
| 正文出现「正在生成」 | 整页搜索 `停止/中断/生成中` 命中数为 0 |

所以按文案分类永远返回 idle，表现就是「实际在跑任务，程序说空闲」。

## 真信号：最新一条回复的计时器动词

同一个元素 `preamble-elapsed-timer`，两种文案：

- 执行中：`已处理 2 分 22 秒`，回复里还有「继续处理任务中」
- 已完成：`共用时 3 分 2 秒`

注意页面里每条回复都有自己的计时器，早先那条会一直冻结在「共用时」——
所以只能看**最新一条**（v2.3.2 的检查取的是第一条，因此也没命中）。

## 本次改动

- 采集所有 `preamble-elapsed-timer` 与最后一条 `receive_message` 的文本
  （正文 `body.innerText` 的尾部是侧边栏，读不到最新回复）
- `classify_probe()`：最新计时器含「已处理」即判定为执行中
- 兜底：消息数、提问数、计时器文本、回复长度的**增长**也算推进；
  只认增长，切会话、折叠步骤都不算
- 10 秒宽限期：两次可见变化之间不报空闲，避免任务停顿瞬间抖动
- 诊断分三种情况说清楚：豆包未运行 / 在运行但没开调试端口 / 页面读不到
- 界面状态灯显示会话名，例如「已读到（调试端口）· 空闲：豆包工作精灵」

## 验证

真机同一会话连续采样：执行中 `state=working`（11:09），任务结束后
`共用时 3 分 2 秒` → `state=idle`（11:10）。单测 39 项通过。

## 说明

- 本版仅 Windows x64；macOS 仍停在 v2.2.0。
- 升级：托盘退出旧版 → 新包覆盖 → 点「启动监听」。
- 豆包需以调试端口启动（界面按钮可一键切换，会重启豆包）。
"""


def token() -> str:
    payload = "protocol=https\nhost=github.com\n\n".encode()
    out = subprocess.run(
        ["git", "credential", "fill"], input=payload, capture_output=True, check=True
    ).stdout.decode()
    for line in out.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1]
    raise SystemExit("no stored GitHub credential")


def call(method: str, url: str, tok: str, body: dict | None = None) -> dict:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {tok}")
    req.add_header("Accept", "application/vnd.github+json")
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read()
    return json.loads(raw) if raw.strip() else {}


def upload(url: str, tok: str, path: str) -> dict:
    with open(path, "rb") as handle:
        payload = handle.read()
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Authorization", f"Bearer {tok}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("Content-Type", mimetypes.guess_type(path)[0] or "application/octet-stream")
    with urllib.request.urlopen(req, timeout=600) as resp:
        return json.loads(resp.read().decode())


def main() -> int:
    tag = "v2.3.0"
    draft = "--draft" in sys.argv
    args = sys.argv[1:]
    if "--tag" in args:
        tag = args[args.index("--tag") + 1]
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    files = [
        os.path.join(root, "releases", "Windows", tag if tag.startswith("v") else f"v{tag}",
                     f"CodexStatusMonitor-{tag}-win64.zip"),
        os.path.join(root, "releases", "Source", tag,
                     f"codex-status-monitor-source-{tag}.zip"),
    ]
    missing = [f for f in files if not os.path.isfile(f)]
    if missing:
        raise SystemExit("missing asset: " + "; ".join(missing))

    tok = token()
    try:
        existing = call("GET", f"{API}/repos/{OWNER}/{REPO}/releases/tags/{tag}", tok)
        raise SystemExit(f"release {tag} already exists: {existing.get('html_url')}")
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise

    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=os.path.join(root, "codex-status-monitor"),
        capture_output=True, check=True,
    ).stdout.decode().strip()
    release = call(
        "POST",
        f"{API}/repos/{OWNER}/{REPO}/releases",
        tok,
        {
            "tag_name": tag,
            "target_commitish": commit,
            "name": f"{tag} · 修掉豆包一直显示空闲",
            "body": BODY,
            "draft": draft,
            "prerelease": False,
        },
    )
    print(f"release={release['html_url']} id={release['id']}")

    for path in files:
        name = os.path.basename(path)
        size = os.path.getsize(path)
        asset = upload(
            f"{UPLOADS}/repos/{OWNER}/{REPO}/releases/{release['id']}/assets?name={name}",
            tok,
            path,
        )
        state = asset.get("state")
        print(f"  {name}: local={size} remote={asset.get('size')} state={state} "
              f"{'OK' if asset.get('size') == size and state == 'uploaded' else 'MISMATCH'}")
    return 0


if __name__ == "__main__":
    import urllib.error  # noqa: PLC0415 - only needed for the 404 probe

    raise SystemExit(main())
