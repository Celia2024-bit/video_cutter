#!/bin/bash
# Build Video Cutter.app on a Mac (PyInstaller cannot cross-compile from Windows).
#   chmod +x packaging/macos/build.sh
#   ./packaging/macos/build.sh

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

VENV="$ROOT/packaging/.venv"
PY="$VENV/bin/python"
if [[ ! -x "$PY" ]]; then
  python3 -m venv "$VENV"
fi
"$PY" -m pip install --upgrade pip
"$PY" -m pip install flask groq faster-whisper pyinstaller

CACHE="$ROOT/packaging/cache"
mkdir -p "$CACHE"

"$PY" -m PyInstaller packaging/VideoCutter.spec --noconfirm --clean

APP="$ROOT/dist/Video Cutter.app"
MACOS="$APP/Contents/MacOS"
if [[ ! -d "$MACOS" ]]; then
  echo "Expected $APP after PyInstaller BUNDLE" >&2
  exit 1
fi

mkdir -p "$MACOS/ffmpeg"
copy_ffmpeg() {
  local src="$1"
  cp "$src" "$MACOS/ffmpeg/"
  local probe
  probe="$(dirname "$src")/ffprobe"
  if [[ -x "$probe" ]]; then
    cp "$probe" "$MACOS/ffmpeg/"
  elif command -v ffprobe >/dev/null; then
    cp "$(command -v ffprobe)" "$MACOS/ffmpeg/"
  fi
  chmod +x "$MACOS/ffmpeg/"*
}

if command -v ffmpeg >/dev/null; then
  copy_ffmpeg "$(command -v ffmpeg)"
elif [[ -x "$CACHE/ffmpeg" ]]; then
  copy_ffmpeg "$CACHE/ffmpeg"
else
  echo "Downloading static ffmpeg for macOS..."
  curl -L "https://evermeet.cx/ffmpeg/getrelease/ffmpeg/zip" -o "$CACHE/ffmpeg.zip"
  curl -L "https://evermeet.cx/ffmpeg/getrelease/ffprobe/zip" -o "$CACHE/ffprobe.zip"
  unzip -o "$CACHE/ffmpeg.zip" -d "$CACHE"
  unzip -o "$CACHE/ffprobe.zip" -d "$CACHE"
  copy_ffmpeg "$CACHE/ffmpeg"
fi

chmod +x "$MACOS/VideoCutter"

DMG="$ROOT/dist/VideoCutter-macos.dmg"
rm -f "$DMG"
hdiutil create -volname "Video Cutter" -srcfolder "$APP" -ov -format UDZO "$DMG"

echo "App: $APP"
echo "Disk image: $DMG"
echo "On a new Mac: right-click the app -> Open the first time (Gatekeeper)."
