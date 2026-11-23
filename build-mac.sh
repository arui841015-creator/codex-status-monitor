#!/usr/bin/env bash
# Build CodexStatusMonitor.app on macOS.
#
# PyInstaller cannot cross-compile: a valid .app can only be produced on macOS.
# .github/workflows/build-macos.yml runs this same script on a macOS runner, so
# owning a Mac is not strictly required — the build host must be macOS.
#
#   ./build-mac.sh
#
# Output
#   build/dist-mac/CodexStatusMonitor.app
#   <parent>/CodexStatusMonitor-<version>-macos-<arch>.zip
#
# Environment
#   PYTHON    interpreter to build with (default: python3)
#   OUT_DIR   where the zip is written   (default: the folder above this repo)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

BUNDLE_ID="com.sdgods.codexstatusmonitor"
VERSION="$(sed -n 's/^APP_VERSION = "\(.*\)"$/\1/p' build_release.py | head -1)"
VERSION="${VERSION:-0.0.0}"
ARCH="$(uname -m)"
DIST="$ROOT/build/dist-mac"
APP="$DIST/CodexStatusMonitor.app"
PY="${PYTHON:-python3}"
OUT_DIR="${OUT_DIR:-$ROOT/..}"
NAME="CodexStatusMonitor-v${VERSION}-macos-${ARCH}"

echo "[1/6] 解释器：$("$PY" -V 2>&1) | 架构：$ARCH | 版本：$VERSION"

echo "[2/6] 检查 tkinter ..."
if ! "$PY" -c "import tkinter" 2>/dev/null; then
  echo "ERROR: 该 Python 缺少 tkinter，而本项目界面依赖它。" >&2
  echo "       用 python.org 的完整安装包，或：brew install python-tk" >&2
  exit 1
fi

echo "[3/6] 单元测试 ..."
"$PY" -m unittest -q test_monitor_engine test_app_helpers

echo "[4/6] PyInstaller 打包 ..."
"$PY" -m PyInstaller --noconfirm --clean --windowed \
  --name CodexStatusMonitor \
  --distpath "$DIST" \
  --workpath "$ROOT/build/work-mac" \
  --specpath "$ROOT/build" \
  --osx-bundle-identifier "$BUNDLE_ID" \
  --hidden-import paho.mqtt.client \
  --hidden-import pystray._darwin \
  --collect-submodules pystray \
  --exclude-module pystray._win32 \
  --add-data "$ROOT/monitor_config.example.json:." \
  monitor_app.py

if [ ! -d "$APP" ]; then
  echo "ERROR: 没有生成 $APP" >&2
  exit 1
fi

# The template must sit next to the executable: that is the first place
# monitor_app looks, and inside a bundle PyInstaller may put --add-data files
# somewhere else entirely.
cp -f "$ROOT/monitor_config.example.json" "$APP/Contents/MacOS/monitor_config.example.json"

echo "[5/6] 组织发布目录 ..."
STAGE="$ROOT/build/stage-mac"
rm -rf "$STAGE"
mkdir -p "$STAGE/$NAME"
# ditto, not cp -R: it keeps the symlinks and resource forks a bundle needs.
ditto "$APP" "$STAGE/$NAME/CodexStatusMonitor.app"
cp -f "$ROOT/README-macOS.txt" "$STAGE/$NAME/使用说明.txt"

# Ad-hoc signature: keeps the bundle internally consistent so Gatekeeper's
# "damaged" check passes. It is NOT notarised, so the first launch still needs
# right-click -> Open (documented in 使用说明.txt).
if ! codesign --force --deep --sign - "$STAGE/$NAME/CodexStatusMonitor.app" 2>/dev/null; then
  echo "WARN: codesign 失败，产物未签名；首次打开需右键 -> 打开。"
fi

echo "[6/6] 压缩 ..."
mkdir -p "$OUT_DIR"
OUT="$OUT_DIR/$NAME.zip"
rm -f "$OUT"
# --sequesterRsrc keeps the resource fork in a form that survives unzip on macOS.
ditto -c -k --sequesterRsrc --keepParent "$STAGE/$NAME" "$OUT"
rm -rf "$STAGE"

echo
echo "完成："
echo "  app : $APP"
echo "  zip : $OUT"
echo "  体积: $(du -sh "$OUT" | cut -f1)"
