#!/bin/zsh
set -e

echo "===== Start environment setup ====="

# Install Homebrew if missing
if ! command -v brew &> /dev/null; then
    echo "Homebrew not found, installing Homebrew..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
fi

# Load brew environment for both M(arm64) and Intel
eval "$(/opt/homebrew/bin/brew shellenv 2>/dev/null)"
eval "$(/usr/local/bin/brew shellenv 2>/dev/null)"

# Install python3 only if python3 not found
if ! command -v python3 &> /dev/null; then
    echo "python3 not found, install python3 via brew"
    brew install python3
else
    echo "✅ python3 already exists, skip python3 install"
fi

# Install ffmpeg (contains ffprobe)
echo "Install ffmpeg (ffprobe included)"
brew update
brew install ffmpeg

# Install flask
echo "Install flask by pip3"
pip3 install flask
pip install faster-whisper

echo ""
echo "===== ALL DONE ====="
echo "Verify ffmpeg:        ffmpeg -version"
echo "Verify ffprobe:       ffprobe -version"
echo "Verify flask:         python3 -c 'import flask; print(flask.__version__)' "
