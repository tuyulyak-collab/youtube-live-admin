#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

if ! command -v apt >/dev/null 2>&1; then
    echo "This installer expects Ubuntu or another apt-based system."
    exit 1
fi

if [ "$(id -u)" -eq 0 ]; then
    APT="apt"
else
    if ! command -v sudo >/dev/null 2>&1; then
        echo "sudo is required when not running as root."
        exit 1
    fi
    APT="sudo apt"
fi

echo "Installing system packages..."
$APT update
$APT install -y python3 python3-pip python3-venv ffmpeg

echo "Setting up Python virtual environment..."
python3 -m venv .venv

echo "Installing Python dependencies..."
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

echo "Checking FFmpeg..."
ffmpeg -version | head -n 1

echo ""
echo "Setup complete."
echo "Copy .env.example to .env, edit the values, then run:"
echo ".venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000 --env-file .env"
