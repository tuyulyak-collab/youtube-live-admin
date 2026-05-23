#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_NAME="${SERVICE_NAME:-youtube-live-admin}"

cd "$APP_DIR"

echo "Pulling latest code..."
git pull

echo "Updating Python dependencies..."
. .venv/bin/activate
pip install -r requirements.txt

echo "Running syntax check..."
python -m py_compile main.py

if systemctl list-unit-files | grep -q "^${SERVICE_NAME}.service"; then
  echo "Restarting ${SERVICE_NAME}.service..."
  sudo systemctl restart "${SERVICE_NAME}"
  sudo systemctl status "${SERVICE_NAME}" --no-pager -l
else
  echo "Systemd service ${SERVICE_NAME}.service not found; restart the app manually if needed."
fi
