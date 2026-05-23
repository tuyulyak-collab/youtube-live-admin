#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "Installing Ubuntu packages..."
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip ffmpeg nginx git

cd "$APP_DIR"

echo "Creating Python virtual environment..."
python3 -m venv .venv
. .venv/bin/activate

echo "Installing Python dependencies..."
python -m pip install --upgrade pip
pip install -r requirements.txt

echo "Creating runtime folders..."
mkdir -p \
  data \
  uploads/videos \
  uploads/audio \
  uploads/ready \
  uploads/logs \
  logs \
  backups

if [ ! -f .env ]; then
  echo "Creating .env from .env.example..."
  cp .env.example .env
else
  echo ".env already exists; leaving it unchanged."
fi

cat <<'EOF'

Install finished.

Next manual steps:
1. Edit .env and change ADMIN_USERNAME, ADMIN_PASSWORD, and APP_SECRET_KEY.
2. Copy deploy/youtube-live-admin.service.example to /etc/systemd/system/youtube-live-admin.service.
3. Copy deploy/nginx.conf.example to /etc/nginx/sites-available/youtube-live-admin.
4. Enable the systemd service and nginx site.
5. Open firewall port 80.

See deploy/README_DEPLOY.md for exact commands.
EOF
