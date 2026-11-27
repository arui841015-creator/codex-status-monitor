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

BODY = """豆包通道「读不到」的真正原因：页面换了 scheme。

## 根因

桌面版豆包的聊天页已经不是 `https://www.doubao.com` 了，而是
`doubao://doubao-chat/chat` 这类自定义 scheme（页面内部 `location.href`
会显示成 `chrome://doubao-chat/chat`）。

v2.3.0 为了排除 `chrome://doubao-background/` 这个后台页，把选页规则收紧成
「只认 http/https」，结果把真正的聊天页一起挡在门外：CDP 通道每次都是空，
只剩窗口兜底，而窗口关到托盘时兜底也读不到 —— 表现就是「还是无法监听豆包」。

## 修复

- **选页改为按页面类型打分**：`doubao-chat` > `doubao-launcher` >
  `doubao.com/chat`，同时排除 `doubao-background`、
  `cross-site-support`、`drive-iframe` 这几类永远没有会话的页面。
- **多页合并判定**：桌面端同时存在多个渲染页时逐个探针，任意一页出现
  「停止生成」就判定为 WORKING，不再依赖「哪个窗口在前台」。
- **界面加实时状态灯**：豆包那一行会显示「● 已读到（调试端口）· 空闲」或
  「● 读不到：<原因>」。之前空闲时不发布任何消息，用户无法区分
  「在监听但空闲」和「根本读不到」，这也是误判成没反应的原因之一。
- **首次读通打一条日志**：`豆包已读到（调试端口），当前 空闲`。

## 说明

- 本版仅 Windows x64；macOS 仍停在 v2.2.0（豆包的窗口兜底依赖 Windows UIA）。
- 升级：托盘退出旧版 → 新包覆盖 → 点「启动监听」，再看豆包行右侧的状态灯。
- 豆包仍需以调试端口启动（界面按钮可一键重启切换），否则最小化到托盘后读不到。
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
            "name": f"{tag} · 豆包读不到的真因修复",
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
