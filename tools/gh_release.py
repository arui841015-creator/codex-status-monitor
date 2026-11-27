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

BODY = """豆包（Doubao）工作监听通道上线。

## 新增

- **豆包渠道**：与 Codex、Workbuddy 并列的第三个监听渠道，默认不勾选。
  - **CDP 优先**：连 `127.0.0.1:19226`，跑一次 `Runtime.evaluate` 读会话状态，
    豆包最小化、切后台都能读。界面里有「用调试端口启动豆包」一键完成（需先退出豆包）。
  - **窗口兜底**：读不到调试端口时扫可见窗口的控件树，零配置；豆包关到托盘则读不到。
- 状态由界面措辞判定：`停止生成` / `正在生成` → WORKING，
  `深度思考` / `正在搜索` / `正在阅读` → THINKING，都不在 → 补发一次 COMPLETED。
- 中文会话标题哈希成 `DB-XXXXXXXX` 形式，原文保留在 `detail.project_raw`，
  `message` 仍然只有单行 ASCII。

## 修复

- 历史消息里的「正在生成中」曾让设备永久卡在 WORKING —— 现只扫按钮标签与页面末尾 600 字。
- 「豆包工作精灵」浮窗曾被误认成聊天窗口，导致状态误判、项目名被污染。
- 探测本地调试端口原来超时 2 秒，拖慢整轮轮询 —— 已缩短。
- `ctypes.wintypes` 在 macOS 上无法导入，会让 mac 版起不来 —— 已改为按平台惰性导入。

## 说明

- 本版仅提供 Windows x64。豆包的窗口兜底依赖 Windows UIA，macOS 只能走调试端口且未真机验证，
  所以 macOS 仍停在 v2.2.0。
- 升级时先在托盘退出旧版，再用新包覆盖；「随系统自动启动」需取消后再勾选一次。
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
            "name": f"v2.3.0 · 增加豆包工作监听",
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
