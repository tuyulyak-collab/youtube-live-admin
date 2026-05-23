# YouTube Live Admin Dashboard

Local FastAPI admin dashboard for managing FFmpeg-based YouTube live streams. It uses SQLite for persistent data, Jinja2 templates, Tailwind CSS CDN, and Uvicorn.

This version streams directly to YouTube RTMP. It does not use the YouTube API.

## GitHub Safety

Do not commit real secrets or runtime data. The `.gitignore` excludes:

- `.env`
- `.venv/`
- SQLite database files in `data/`
- uploaded videos in `uploads/videos/`
- prepared upload files in `uploads/ready/`
- FFmpeg logs in `uploads/logs/`
- app logs in `logs/`
- common stream key, secret, key, and certificate file names

The empty runtime folders are kept with `.gitkeep` files. Keep real stream keys in `.env` or enter them in the local admin UI only.

## Windows Setup

Open PowerShell from this project folder and run:

```powershell
.\install.ps1
```

The installer creates `.venv`, installs `requirements.txt`, checks for FFmpeg, and installs `Gyan.FFmpeg` with winget when winget is available. If winget is not available, it prints manual FFmpeg install steps.

Create your local environment file:

```powershell
Copy-Item .env.example .env
notepad .env
```

Change at least `ADMIN_PASSWORD` and `APP_SECRET_KEY` before using the app beyond local testing.

## FFmpeg Configuration

By default, the app runs `ffmpeg` from the system `PATH`. This works when `ffmpeg -version` succeeds in a new terminal.

You can optionally set `FFMPEG_PATH` in `.env` to use a specific executable. Leave it empty to use the system `PATH`.

Windows example:

```text
FFMPEG_PATH=C:\ffmpeg\bin\ffmpeg.exe
```

Ubuntu example:

```text
FFMPEG_PATH=/usr/bin/ffmpeg
```

The dashboard shows FFmpeg status, the path used, and the detected version. Use the `Test FFmpeg` button to run `ffmpeg -version` with the same executable that Start Live and the scheduler use.

## Ubuntu Setup

On Ubuntu, run:

```bash
chmod +x install.sh
./install.sh
```

The installer runs `apt update`, installs `python3`, `python3-pip`, `python3-venv`, and `ffmpeg`, then creates `.venv` and installs the Python dependencies.

Create your local environment file:

```bash
cp .env.example .env
nano .env
```

Change at least `ADMIN_PASSWORD` and `APP_SECRET_KEY`.

## Run The App

Windows:

```powershell
.\.venv\Scripts\uvicorn.exe main:app --host 0.0.0.0 --port 8000 --env-file .env
```

Ubuntu:

```bash
.venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000 --env-file .env
```

Open on the same machine:

```text
http://127.0.0.1:8000
```

Default local test login from `.env.example`:

```text
admin / admin123
```

Uploaded videos are saved to `uploads/videos`. FFmpeg logs are saved to `uploads/logs`. SQLite data is saved to `data/app.db`.

## Scheduling Modes

Live jobs support three scheduling modes:

- `Manual`: no start or end time is required. You can start the live stream later with the `Start` button. Duration is optional.
- `Start & selesai`: set a start datetime and an end datetime. The app calculates the duration automatically, and the end datetime must be after the start datetime.
- `Start & durasi`: set a start datetime and duration in minutes. The app previews the automatic finish time.

Existing jobs remain compatible because the app still stores scheduling data in `start_at`, `end_at`, and `duration_minutes`.

## Access From Another Device On The Same WiFi

1. Start the app with `--host 0.0.0.0`.
2. Find your computer IP address.

Windows:

```powershell
ipconfig
```

Ubuntu:

```bash
hostname -I
```

3. From a phone or laptop on the same WiFi, open:

```text
http://YOUR_COMPUTER_IP:8000
```

Example:

```text
http://192.168.1.50:8000
```

If it does not open on Windows, allow Python/Uvicorn through Windows Defender Firewall for private networks.

## Streaming Behavior

Starting a job runs FFmpeg in the background with:

```text
ffmpeg -re -stream_loop -1 -i selected-video.mp4 ... -f flv rtmp://a.rtmp.youtube.com/live2/{stream_key}
```

The stream key is entered per job and is not hardcoded in the app. The UI masks stream keys in tables and logs.
