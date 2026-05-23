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

Uploaded videos are saved to `uploads/videos`. Uploaded audio is saved to `uploads/audio`. Prepared audio playlists are saved to `uploads/ready`. FFmpeg logs are saved to `uploads/logs`. SQLite data is saved to `data/app.db`.

## Admin Layout

The admin UI is split into tabs with a responsive dark dashboard layout:

- `Dashboard`: summary stats, History summary for today, FFmpeg status, quick live job overview, and latest log preview.
- `Channels`: channel records used by Live Jobs. A placeholder note is shown while the full channel workflow continues to evolve.
- `Video Library`: MP4 upload and uploaded video list.
- `Audio Library`: global MP3, WAV, and M4A uploads with duration, size, preview, and delete controls.
- `Playlists`: per-channel song queues built from the global Audio Library.
- `Live Jobs`: create live job form, filters, queue controls, bulk actions, Start/Stop/Delete controls, and archive actions for completed jobs.
- `Scheduler`: scheduling mode explanation and scheduled jobs overview.
- `History`: completed, stopped, failed, deleted, and archived live job records with filters, logs, duplicate-as-new, delete history record, and CSV export.
- `Logs`: latest FFmpeg log and links to per-job logs.
- `Settings`: app paths, FFmpeg diagnostics, detailed System Monitor, and health details link.

## Channel Management

Add channels from the `Channels` tab before creating a Live Job. The Create Live Job form only shows active channels.

Each channel can store:

- name
- optional handle
- optional niche
- optional notes
- optional default stream key
- active or inactive status

Channel names are required and must be unique.

If a channel has a default stream key, selecting that channel auto-fills the stream key field in the Live Job form. You can still edit the stream key before creating the job. Stream keys are hidden by default in forms, can be revealed with the eye button when you need to check them, and are masked in tables/logs where possible.

Never share stream keys publicly and do not commit stream keys to GitHub. Keep them in `.env`, in the local admin UI, or in another private password manager.

Inactive channels stay in the database but are hidden from the Create Live Job dropdown. If a channel is already used by live jobs, deleting it will set it inactive instead of removing it, so existing jobs keep working and can still display their channel name.

## Video Library

The `Video Library` tab stores uploaded MP4 files in `uploads/videos` and tracks them in SQLite.

Each uploaded video row shows:

- original filename
- stored filename
- file size
- upload date
- usage status
- delete action

Videos that are used by one or more Live Jobs cannot be deleted from the Video Library. The app blocks deletion and shows:

```text
Video tidak bisa dihapus karena masih dipakai oleh Live Job.
```

The message includes the number of Live Jobs using the video when available. Delete or archive the related Live Jobs first if you want to remove an uploaded video.

When a video is unused, deleting it removes both the physical file from `uploads/videos` and the SQLite `videos` record. The delete route validates that the stored path is inside `uploads/videos` before removing anything, so it cannot be used to delete arbitrary system files.

If a Live Job points to a video file that is missing on disk, the Live Jobs table shows `File video tidak ditemukan.` and Start Live will fail until the file is restored or a new job is created with an available video.

## Audio Library And Playlists

The Audio Library is global. Upload an audio file once, then reuse the same file in any number of channel playlists without duplicating the file on disk.

The `Audio Library` upload control supports bulk upload. You can select one song or many songs at once, then click `Upload Audio Files`. After upload, the app shows a summary with:

- total selected
- uploaded successfully
- skipped/failed

Invalid formats are skipped with a friendly message. Valid files from the same batch are still saved, so a mixed upload can partially succeed.

Supported upload formats:

- MP3
- WAV
- M4A

Each uploaded audio file shows its original filename, stored filename, file size, created date, browser preview player, and duration when FFprobe can read it.

Playlists belong to channels. Every channel can have a different playlist and song queue. In the `Playlists` tab:

- create a playlist for a channel
- add songs from the global Audio Library
- reorder songs with Up and Down controls
- remove a song from the playlist without deleting the original audio file
- shuffle the queue
- duplicate a playlist
- prepare the playlist into one ready audio file

Preparing a playlist writes:

```text
uploads/ready/audio_playlist_<playlist_id>.m4a
uploads/logs/audio_playlist_<playlist_id>.log
```

The prepared file can be selected in the Live Jobs form. When selected, FFmpeg streams the looping video with the prepared audio playlist. If no prepared playlist is selected, the app keeps the current behavior and uses the video's original audio/input.

## Scheduling Modes

Live jobs support three scheduling modes:

- `Manual start`: no start or end time is required. You can start the live stream later with the `Start` button. Duration is optional.
- `Start & End datetime`: set a start datetime and an end datetime. The app calculates the duration automatically, and the end datetime must be after the start datetime.
- `Start datetime + Duration`: set a start datetime and duration in minutes. The app previews the automatic finish time.

Existing jobs remain compatible because the app still stores scheduling data in `start_at`, `end_at`, and `duration_minutes`.

## Live Jobs Control Panel

The `Live Jobs` tab includes advanced filtering and bulk controls for managing many streams.

Filters:

- channel
- status
- date from
- date to
- search by live name, channel name, or video filename
- sort by newest, oldest, channel A-Z, status, or scheduled start time

Bulk selection:

- use each row checkbox to select jobs
- use `Select All` to select every job currently visible in the filtered table
- use `Unselect All` to clear the selection
- the toolbar shows the current selected count

Bulk actions:

- `Start Selected`: starts selected jobs with status `stopped`, `queued`, `scheduled`, or `error`; running jobs are skipped.
- `Stop Selected`: stops selected jobs only when they are running and have a saved PID; other jobs are skipped.
- `Move Selected to Queue`: changes selected non-running jobs to `queued` without starting them.
- `Archive Selected`: archives selected `done`, `stopped`, or `error` jobs to History.
- `Delete Selected`: asks for confirmation, removes selected non-running live jobs from the active list, keeps the records in History, and skips running jobs.
- `Archive completed jobs`: archives all visible or hidden active jobs with status `done`, `stopped`, or `error`.

Deleting live jobs from the Live Jobs tab now archives the job record to History instead of deleting the row immediately. It does not delete uploaded videos, uploaded audio, prepared playlists, or playlist records. Use the History tab delete action when you want to remove a history record from SQLite.

The `queued` status is useful for preparing a group of jobs before starting them manually or letting scheduled queued jobs start later when their start datetime is reached.

## System Monitor

The dashboard and Settings tab include a lightweight System Monitor powered by `psutil`.

It shows:

- CPU usage percentage
- RAM usage percentage and used / total memory
- disk usage percentage and used / total disk space
- running FFmpeg process count
- app uptime
- server local time
- Python version
- OS info
- FFmpeg detected status, path, and version
- FFmpeg PID list with live job links when a PID matches a running job in SQLite

Install dependencies with:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

The monitor shows friendly warnings when:

- CPU usage is high, which can cause stream lag or dropped frames.
- RAM usage is high, which can make FFmpeg or the web app unstable.
- disk usage is high, which can break uploads, ready audio files, logs, or SQLite writes.
- FFmpeg is not detected, so live jobs cannot start.
- many FFmpeg processes are running, which may exceed VPS capacity.
- a live job is marked `running` but its FFmpeg PID is no longer active.

Before scaling many live jobs, check CPU, RAM, disk, and FFmpeg process count from Dashboard or Settings. Start a small number of streams first, watch resource usage for several minutes, then increase gradually.

The detailed health endpoint is available after login:

```text
http://127.0.0.1:8000/health/details
```

It reports app, database, FFmpeg, disk space, active jobs count, FFmpeg process count, and current warnings.

## History And Job Statuses

The `History` tab shows jobs that are finished, stopped, failed, deleted from Live Jobs, or archived. It includes summary cards, filters by channel/status/date/search, per-job logs, duplicate-as-new, delete history record, and CSV export for the current filters.

Runtime lifecycle fields are stored in SQLite on `live_jobs`:

- `started_at`
- `stopped_at`
- `expected_end_at`
- `exit_code`
- `stop_reason`
- `last_error`
- `archived_at`

Status meanings:

- `running`: FFmpeg was started and the app still expects the process to be alive.
- `queued`: job is waiting to be started manually or by schedule.
- `scheduled`: job has a future start time.
- `stopped`: job was manually stopped or never started before its schedule ended.
- `done`: job reached its expected end time, duration, or scheduler end.
- `error`: FFmpeg could not start or exited unexpectedly before the expected end.
- `archived`: not a separate stream result; the job has `archived_at` set, is hidden from Live Jobs, and remains visible in History with its final status.

Stop reasons:

- `manual_stop`: stopped by a user action.
- `completed_duration`: stopped after the configured duration.
- `scheduler_end`: stopped by the configured end datetime.
- `process_error`: FFmpeg failed to start or exited unexpectedly.

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

The stream key is entered per job and is not hardcoded in the app. The UI masks stream keys in tables and logs. In the Live Jobs and Channels forms, stream key inputs are hidden by default; click the eye button only when you need to verify a key, then hide it again before sharing your screen.
