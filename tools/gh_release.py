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

BODY = """豆包通道的三处真机问题修复。

## 修复

- **找不到 Doubao.exe**：原来只猜 `%ProgramFiles%` 与 `%LOCALAPPDATA%`，装在
  `D:`、`F:` 这类非系统盘就找不到。现在按四层依次查找：界面里手填的
  「豆包可执行文件」→ 正在运行的进程镜像路径 → 安装器写入的注册表
  （App Paths / Uninstall）→ 所有固定磁盘的常见安装目录。实测本机
  `F:\\Program Files\\Doubao\\app\\Doubao.exe` 在 0.02 秒内命中。
  新增可选字段 `watch.doubao.executable`，界面上就是那一栏。
- **豆包在跑时无法转调试模式**：豆包是单实例，参数注不进去，只能重启。
  现在点「用调试端口启动豆包」会先弹窗确认，确认后自动关闭豆包
  （先优雅关闭，6 秒不退出再强制结束）并以调试端口重新打开，
  实测 16 秒左右端口就绪、CDP 可读。
- **日志看不懂**：`<urlopen error timed out>` 换成「调试端口 19226 未开放」，
  并且先做 0.25 秒 socket 探活，单轮探测从 0.8 秒降到 0.26 秒。

## 另外修掉

- CDP 目标列表里的 `chrome://doubao-background/` 这类内部页曾被当成聊天页，
  在没有打开任何会话时会报出空标题的假状态。现在只认 http(s) 页面。

## 说明

- 本版仅 Windows x64；macOS 仍停在 v2.2.0（豆包的窗口兜底依赖 Windows UIA）。
- 升级：先在托盘退出旧版，再用新包覆盖；自启动项若指向旧路径，取消后再勾选一次。
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
            "name": f"{tag} · 豆包通道修复",
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
