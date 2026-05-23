# Ubuntu VPS Deployment

This guide deploys YouTube Live Admin to an Ubuntu VPS using FastAPI, Uvicorn, systemd, Nginx, SQLite, and FFmpeg.

The Windows local workflow is unchanged.

## 1. Clone To /opt

```bash
sudo apt-get update
sudo apt-get install -y git
cd /opt
sudo git clone https://github.com/tuyulyak-collab/youtube-live-admin.git
sudo chown -R "$USER":"$USER" /opt/youtube-live-admin
cd /opt/youtube-live-admin
```

## 2. Run Install Script

```bash
chmod +x deploy/install_ubuntu.sh
./deploy/install_ubuntu.sh
```

The script installs Python, FFmpeg, Nginx, creates `.venv`, installs `requirements.txt`, creates runtime folders, and copies `.env.example` to `.env` if needed.

## 3. Edit .env

```bash
nano /opt/youtube-live-admin/.env
```

Change at least:

```text
ADMIN_USERNAME=your-admin-user
ADMIN_PASSWORD=your-strong-password
APP_SECRET_KEY=long-random-secret
```

Keep `.env` private. Never commit it.

## 4. Install systemd Service

```bash
sudo cp deploy/youtube-live-admin.service.example /etc/systemd/system/youtube-live-admin.service
sudo systemctl daemon-reload
sudo systemctl enable youtube-live-admin
sudo systemctl start youtube-live-admin
sudo systemctl status youtube-live-admin --no-pager -l
```

If you use a different install path or Linux user, edit:

```bash
sudo nano /etc/systemd/system/youtube-live-admin.service
```

The default service uses:

```text
WorkingDirectory=/opt/youtube-live-admin
EnvironmentFile=/opt/youtube-live-admin/.env
ExecStart=/opt/youtube-live-admin/.venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

## 5. Install Nginx Config

```bash
sudo cp deploy/nginx.conf.example /etc/nginx/sites-available/youtube-live-admin
sudo ln -s /etc/nginx/sites-available/youtube-live-admin /etc/nginx/sites-enabled/youtube-live-admin
sudo nginx -t
sudo systemctl reload nginx
```

The example reverse proxies port 80 to `127.0.0.1:8000` and sets:

```text
client_max_body_size 2G
```

This allows large video/audio uploads.

## 6. Open Firewall Port 80

If UFW is enabled:

```bash
sudo ufw allow 80/tcp
sudo ufw status
```

Open your VPS provider firewall/security group for TCP port 80 as well.

Then open:

```text
http://YOUR_SERVER_IP
```

## 7. Restore Backup From Local App

Stop the service before replacing SQLite data or upload folders:

```bash
sudo systemctl stop youtube-live-admin
```

Copy your backup files into:

```text
/opt/youtube-live-admin/data/
/opt/youtube-live-admin/uploads/
/opt/youtube-live-admin/logs/
/opt/youtube-live-admin/backups/
```

Then fix ownership if needed:

```bash
sudo chown -R www-data:www-data /opt/youtube-live-admin/data /opt/youtube-live-admin/uploads /opt/youtube-live-admin/logs /opt/youtube-live-admin/backups
sudo systemctl start youtube-live-admin
```

## 8. Update App From GitHub

```bash
cd /opt/youtube-live-admin
chmod +x deploy/update_app.sh
./deploy/update_app.sh
```

The update script runs:

- `git pull`
- activates `.venv`
- `pip install -r requirements.txt`
- `python -m py_compile main.py`
- restarts `youtube-live-admin.service` if installed

## 9. Check Logs

App logs from systemd:

```bash
sudo journalctl -u youtube-live-admin -f
```

Nginx logs:

```bash
sudo tail -f /var/log/nginx/access.log
sudo tail -f /var/log/nginx/error.log
```

FFmpeg job logs are stored by the app in:

```text
/opt/youtube-live-admin/uploads/logs/
```

## Security Notes

- Change default admin username and password before exposing the app.
- Do not expose the app without login.
- Keep `.env` private and never commit it.
- Do not commit SQLite databases, uploads, ready files, backups, or logs.
- Use a domain and HTTPS later, for example with Certbot.
- Keep VPS packages updated with `sudo apt-get update && sudo apt-get upgrade`.
