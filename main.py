import asyncio
import csv
import io
import os
import random
import re
import secrets
import shutil
import signal
import sqlite3
import subprocess
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
VIDEO_DIR = BASE_DIR / "uploads" / "videos"
AUDIO_DIR = BASE_DIR / "uploads" / "audio"
READY_DIR = BASE_DIR / "uploads" / "ready"
LOG_DIR = BASE_DIR / "uploads" / "logs"
DB_PATH = Path(os.getenv("DATABASE_PATH", DATA_DIR / "app.db"))

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")
SESSION_SECRET = os.getenv("APP_SECRET_KEY") or os.getenv("SESSION_SECRET") or "change-this-local-dev-secret"

YOUTUBE_RTMP_BASE = "rtmp://a.rtmp.youtube.com/live2"
STATUSES = {"queued", "scheduled", "running", "stopped", "done", "error"}
STARTABLE_STATUSES = {"queued", "scheduled", "stopped", "error"}
FINAL_STATUSES = {"stopped", "done", "error"}

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def local_now() -> datetime:
    return datetime.now().replace(microsecond=0)


def dt_to_str(value: datetime | None) -> str | None:
    return value.isoformat(sep=" ") if value else None


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def display_dt(value: str | None) -> str:
    if not value:
        return "-"
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value


def display_dt_local(value: str | None) -> str:
    if not value:
        return "-"
    try:
        return datetime.fromisoformat(value).strftime("%d %b %Y %H:%M")
    except ValueError:
        return value

def duration_between_minutes(start_value: datetime, end_value: datetime) -> int:
    seconds = int((end_value - start_value).total_seconds())
    if seconds <= 0:
        return 0
    return max(1, seconds // 60)

def format_duration_minutes(minutes: int | str | None) -> str:
    if minutes in (None, ""):
        return "-"
    try:
        total_minutes = max(0, int(minutes))
    except (TypeError, ValueError):
        return "-"
    days, remainder = divmod(total_minutes, 1440)
    hours, mins = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days} hari")
        parts.append(f"{hours} jam")
    elif hours:
        parts.append(f"{hours} jam")
    if mins or not parts:
        parts.append(f"{mins} menit")
    return " ".join(parts)

def format_runtime_seconds(seconds: float | int | None) -> str:
    if seconds in (None, ""):
        return "-"
    try:
        total_seconds = max(0, int(seconds))
    except (TypeError, ValueError):
        return "-"
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days} hari")
    if hours:
        parts.append(f"{hours} jam")
    if minutes:
        parts.append(f"{minutes} menit")
    if secs or not parts:
        parts.append(f"{secs} detik")
    return " ".join(parts)

def job_runtime_seconds(job: dict[str, Any]) -> int | None:
    started_at = parse_dt(job.get("started_at"))
    stopped_at = parse_dt(job.get("stopped_at"))
    if not started_at:
        return None
    end_value = stopped_at or (local_now() if job.get("status") == "running" else None)
    if not end_value:
        return None
    return max(0, int((end_value - started_at).total_seconds()))

def job_schedule_lines(job: dict[str, Any]) -> list[str]:
    start_value = parse_dt(job.get("start_at"))
    end_value = parse_dt(job.get("end_at"))
    duration = job.get("duration_minutes")
    if start_value and end_value:
        return [
            f"Scheduled: {display_dt(job.get('start_at'))} → {display_dt(job.get('end_at'))}",
            f"Duration: {format_duration_minutes(duration_between_minutes(start_value, end_value))}",
        ]
    if start_value and duration:
        calculated_end = start_value + timedelta(minutes=int(duration))
        return [
            f"Scheduled: {display_dt(job.get('start_at'))} → {display_dt(dt_to_str(calculated_end))}",
            f"Duration: {format_duration_minutes(duration)}",
        ]
    if start_value:
        return [f"Scheduled: {display_dt(job.get('start_at'))}"]
    if end_value:
        return [
            f"Scheduled: until {display_dt(job.get('end_at'))}",
            f"Duration: {format_duration_minutes(duration)}",
        ]
    if duration:
        return ["Manual start", f"Duration: {int(duration)} menit"]
    return ["Manual start"]

def running_duration(job: dict[str, Any]) -> str:
    if job.get("status") != "running":
        return "-"
    started_at = parse_dt(job.get("started_at"))
    if not started_at:
        return "-"
    minutes = max(0, int((local_now() - started_at).total_seconds() // 60))
    return format_duration_minutes(minutes)

def status_badge_class(status: str | None) -> str:
    return {
        "running": "bg-emerald-950 text-emerald-300 border-emerald-800",
        "queued": "bg-sky-950 text-sky-300 border-sky-800",
        "scheduled": "bg-indigo-950 text-indigo-300 border-indigo-800",
        "stopped": "bg-zinc-800 text-zinc-300 border-zinc-700",
        "done": "bg-teal-950 text-teal-300 border-teal-800",
        "error": "bg-red-950 text-red-300 border-red-800",
    }.get(status or "", "bg-zinc-800 text-zinc-300 border-zinc-700")

def safe_filename(filename: str) -> str:
    name = Path(filename).name
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return name or f"video_{int(local_now().timestamp())}.mp4"

def safe_audio_filename(filename: str) -> str:
    safe = safe_filename(filename)
    if Path(safe).suffix.lower() not in {".mp3", ".wav", ".m4a"}:
        return f"{Path(safe).stem}.mp3"
    return safe


def mask_secret(value: str | None) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def mask_log(text: str, stream_key: str | None = None) -> str:
    if stream_key:
        text = text.replace(stream_key, mask_secret(stream_key))
    text = re.sub(r"(live2/)([A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+)", r"\1****", text)
    return text


def ensure_directories() -> None:
    for path in (DATA_DIR, VIDEO_DIR, AUDIO_DIR, READY_DIR, LOG_DIR):
        path.mkdir(parents=True, exist_ok=True)


def connect_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    ensure_directories()
    with connect_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                handle TEXT,
                niche TEXT,
                notes TEXT,
                default_stream_key TEXT,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL,
                original_name TEXT NOT NULL,
                path TEXT NOT NULL,
                uploaded_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS live_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                live_name TEXT NOT NULL,
                channel_name TEXT NOT NULL,
                channel_id INTEGER,
                video_id INTEGER NOT NULL,
                audio_playlist_id INTEGER,
                stream_key TEXT NOT NULL,
                start_at TEXT,
                end_at TEXT,
                duration_minutes INTEGER,
                status TEXT NOT NULL DEFAULT 'stopped',
                pid INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT,
                stopped_at TEXT,
                expected_end_at TEXT,
                exit_code INTEGER,
                stop_reason TEXT,
                last_error TEXT,
                archived_at TEXT,
                FOREIGN KEY(channel_id) REFERENCES channels(id),
                FOREIGN KEY(audio_playlist_id) REFERENCES audio_playlists(id),
                FOREIGN KEY(video_id) REFERENCES videos(id)
            );

            CREATE TABLE IF NOT EXISTS audio_assets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL,
                original_filename TEXT NOT NULL,
                path TEXT NOT NULL,
                file_size INTEGER NOT NULL,
                duration_seconds REAL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS audio_playlists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'draft',
                prepared_audio_path TEXT,
                total_duration_seconds REAL,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(channel_id) REFERENCES channels(id)
            );

            CREATE TABLE IF NOT EXISTS audio_playlist_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                playlist_id INTEGER NOT NULL,
                audio_asset_id INTEGER NOT NULL,
                sort_order INTEGER NOT NULL,
                FOREIGN KEY(playlist_id) REFERENCES audio_playlists(id) ON DELETE CASCADE,
                FOREIGN KEY(audio_asset_id) REFERENCES audio_assets(id)
            );
            """
        )
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(live_jobs)").fetchall()}
        if "channel_id" not in columns:
            conn.execute("ALTER TABLE live_jobs ADD COLUMN channel_id INTEGER")
        if "audio_playlist_id" not in columns:
            conn.execute("ALTER TABLE live_jobs ADD COLUMN audio_playlist_id INTEGER")
        if "started_at" not in columns:
            conn.execute("ALTER TABLE live_jobs ADD COLUMN started_at TEXT")
        if "stopped_at" not in columns:
            conn.execute("ALTER TABLE live_jobs ADD COLUMN stopped_at TEXT")
        if "expected_end_at" not in columns:
            conn.execute("ALTER TABLE live_jobs ADD COLUMN expected_end_at TEXT")
        if "exit_code" not in columns:
            conn.execute("ALTER TABLE live_jobs ADD COLUMN exit_code INTEGER")
        if "stop_reason" not in columns:
            conn.execute("ALTER TABLE live_jobs ADD COLUMN stop_reason TEXT")
        if "last_error" not in columns:
            conn.execute("ALTER TABLE live_jobs ADD COLUMN last_error TEXT")
        if "archived_at" not in columns:
            conn.execute("ALTER TABLE live_jobs ADD COLUMN archived_at TEXT")
        playlist_columns = {row["name"] for row in conn.execute("PRAGMA table_info(audio_playlists)").fetchall()}
        if "last_error" not in playlist_columns:
            conn.execute("ALTER TABLE audio_playlists ADD COLUMN last_error TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_live_jobs_channel_id ON live_jobs(channel_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_live_jobs_audio_playlist_id ON live_jobs(audio_playlist_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_live_jobs_archived_at ON live_jobs(archived_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_live_jobs_stopped_at ON live_jobs(stopped_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audio_playlist_items_playlist_id ON audio_playlist_items(playlist_id)")
        conn.commit()


def get_db():
    conn = connect_db()
    try:
        yield conn
    finally:
        conn.close()


def is_logged_in(request: Request) -> bool:
    return bool(request.session.get("authenticated"))


def require_admin(request: Request) -> None:
    if not is_logged_in(request):
        raise RedirectToLogin()


class RedirectToLogin(Exception):
    pass


def redirect(url: str, status_code: int = 303) -> RedirectResponse:
    return RedirectResponse(url, status_code=status_code)


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


def clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None

def parse_optional_int(value: str | int | None) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

def channel_name_exists(conn: sqlite3.Connection, name: str, exclude_id: int | None = None) -> bool:
    if exclude_id is None:
        row = conn.execute("SELECT id FROM channels WHERE lower(name) = lower(?) LIMIT 1", (name,)).fetchone()
    else:
        row = conn.execute(
            "SELECT id FROM channels WHERE lower(name) = lower(?) AND id != ? LIMIT 1",
            (name, exclude_id),
        ).fetchone()
    return row is not None

def configured_ffmpeg_path() -> str:
    configured = os.getenv("FFMPEG_PATH", "").strip().strip("\"'")
    if configured:
        return configured
    return shutil.which("ffmpeg") or "ffmpeg"

def ffmpeg_probe(timeout: int = 5) -> dict[str, Any]:
    executable = configured_ffmpeg_path()
    info: dict[str, Any] = {
        "detected": False,
        "path": executable,
        "version": None,
        "error": None,
        "from_env": bool(os.getenv("FFMPEG_PATH", "").strip()),
    }
    try:
        result = subprocess.run(
            [executable, "-version"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        info["error"] = f"FFmpeg executable was not found: {executable}"
        return info
    except Exception as exc:
        info["error"] = str(exc)
        return info

    output = (result.stdout or result.stderr or "").strip()
    first_line = output.splitlines()[0] if output else ""
    if result.returncode == 0:
        info["detected"] = True
        info["version"] = first_line or "FFmpeg detected"
    else:
        info["error"] = first_line or f"ffmpeg -version exited with code {result.returncode}"
    return info

def ffmpeg_path() -> str | None:
    info = ffmpeg_probe()
    return info["path"] if info["detected"] else None

def configured_ffprobe_path() -> str:
    configured = os.getenv("FFPROBE_PATH", "").strip().strip("\"'")
    if configured:
        return configured
    ffmpeg_executable = configured_ffmpeg_path()
    ffmpeg_name = Path(ffmpeg_executable).name.lower()
    if ffmpeg_name in {"ffmpeg.exe", "ffmpeg"}:
        sibling = Path(ffmpeg_executable).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
        if sibling.exists():
            return str(sibling)
    return shutil.which("ffprobe") or "ffprobe"

def ffprobe_probe(timeout: int = 5) -> dict[str, Any]:
    executable = configured_ffprobe_path()
    info: dict[str, Any] = {"detected": False, "path": executable, "version": None, "error": None}
    try:
        result = subprocess.run([executable, "-version"], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        info["error"] = f"FFprobe executable was not found: {executable}"
        return info
    except Exception as exc:
        info["error"] = str(exc)
        return info
    output = (result.stdout or result.stderr or "").strip()
    first_line = output.splitlines()[0] if output else ""
    if result.returncode == 0:
        info["detected"] = True
        info["version"] = first_line or "FFprobe detected"
    else:
        info["error"] = first_line or f"ffprobe -version exited with code {result.returncode}"
    return info

def probe_audio_duration(path: Path) -> float | None:
    ffprobe_info = ffprobe_probe()
    if not ffprobe_info["detected"]:
        return None
    try:
        result = subprocess.run(
            [
                ffprobe_info["path"],
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if result.returncode != 0:
            return None
        return float(result.stdout.strip())
    except Exception:
        return None

def format_seconds(seconds: float | int | None) -> str:
    if seconds in (None, ""):
        return "-"
    try:
        total = int(round(float(seconds)))
    except (TypeError, ValueError):
        return "-"
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"

def playlist_total_duration(conn: sqlite3.Connection, playlist_id: int) -> float | None:
    row = conn.execute(
        """
        SELECT SUM(COALESCE(audio_assets.duration_seconds, 0)) AS total
        FROM audio_playlist_items
        JOIN audio_assets ON audio_assets.id = audio_playlist_items.audio_asset_id
        WHERE audio_playlist_items.playlist_id = ?
        """,
        (playlist_id,),
    ).fetchone()
    total = row["total"] if row else None
    return float(total) if total is not None else None

def live_duration_seconds_for_start(job: dict[str, Any], now: datetime) -> int | None:
    end_at = parse_dt(job.get("end_at"))
    if end_at:
        remaining = int((end_at - now).total_seconds())
        return remaining if remaining > 0 else None
    duration = job.get("duration_minutes")
    if duration:
        try:
            return max(1, int(duration) * 60)
        except (TypeError, ValueError):
            return None
    return None


def expected_end_for_start(job: dict[str, Any], now: datetime) -> datetime | None:
    end_at = parse_dt(job.get("end_at"))
    if end_at:
        return end_at
    duration = job.get("duration_minutes")
    if duration:
        try:
            return now + timedelta(minutes=int(duration))
        except (TypeError, ValueError):
            return None
    return None

def process_exists(pid: int | None) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return str(pid) in result.stdout and "No tasks" not in result.stdout
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def process_exit_code(pid: int | None) -> int | None:
    return None

def stop_process(pid: int | None) -> tuple[bool, str]:
    if not pid:
        return True, "No PID was saved for this job."
    if not process_exists(pid):
        return True, "Process is already stopped."
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], check=True, capture_output=True, text=True)
        else:
            os.kill(pid, signal.SIGTERM)
        return True, "FFmpeg process stopped."
    except subprocess.CalledProcessError as exc:
        return False, exc.stderr.strip() or exc.stdout.strip() or str(exc)
    except Exception as exc:
        return False, str(exc)


def get_job(conn: sqlite3.Connection, job_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT live_jobs.*, videos.filename AS video_filename, videos.path AS video_path,
               videos.original_name AS video_original_name,
               channels.name AS channel_table_name,
               channels.handle AS channel_handle,
               channels.is_active AS channel_is_active,
               COALESCE(channels.name, live_jobs.channel_name) AS display_channel_name,
               audio_playlists.name AS audio_playlist_name,
               audio_playlists.status AS audio_playlist_status,
               audio_playlists.prepared_audio_path AS prepared_audio_path,
               audio_playlists.total_duration_seconds AS audio_playlist_duration_seconds
        FROM live_jobs
        JOIN videos ON videos.id = live_jobs.video_id
        LEFT JOIN channels ON channels.id = live_jobs.channel_id
        LEFT JOIN audio_playlists ON audio_playlists.id = live_jobs.audio_playlist_id
        WHERE live_jobs.id = ?
        """,
        (job_id,),
    ).fetchone()
    return row_to_dict(row)

def fetch_live_jobs(conn: sqlite3.Connection, filters: dict[str, str] | None = None) -> list[dict[str, Any]]:
    filters = filters or {}
    where = ["live_jobs.archived_at IS NULL"]
    params: list[Any] = []
    channel_id = filters.get("channel_id", "").strip()
    status = filters.get("status", "").strip()
    date_from = filters.get("date_from", "").strip()
    date_to = filters.get("date_to", "").strip()
    search = filters.get("search", "").strip()
    sort = filters.get("sort", "newest").strip() or "newest"

    if channel_id:
        where.append("live_jobs.channel_id = ?")
        params.append(channel_id)
    if status:
        where.append("live_jobs.status = ?")
        params.append(status)
    if date_from:
        where.append("date(COALESCE(live_jobs.start_at, live_jobs.created_at)) >= date(?)")
        params.append(date_from)
    if date_to:
        where.append("date(COALESCE(live_jobs.start_at, live_jobs.created_at)) <= date(?)")
        params.append(date_to)
    if search:
        like_value = f"%{search}%"
        where.append(
            """
            (
                live_jobs.live_name LIKE ?
                OR COALESCE(channels.name, live_jobs.channel_name) LIKE ?
                OR videos.original_name LIKE ?
                OR videos.filename LIKE ?
            )
            """
        )
        params.extend([like_value, like_value, like_value, like_value])

    order_by = {
        "newest": "live_jobs.created_at DESC, live_jobs.id DESC",
        "oldest": "live_jobs.created_at ASC, live_jobs.id ASC",
        "channel_az": "display_channel_name COLLATE NOCASE ASC, live_jobs.created_at DESC",
        "status": "live_jobs.status COLLATE NOCASE ASC, live_jobs.created_at DESC",
        "scheduled_start": "CASE WHEN live_jobs.start_at IS NULL THEN 1 ELSE 0 END, live_jobs.start_at ASC, live_jobs.created_at DESC",
    }.get(sort, "live_jobs.created_at DESC, live_jobs.id DESC")

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    return [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT live_jobs.*, videos.filename AS video_filename, videos.original_name AS video_original_name,
                   videos.path AS video_path,
                   channels.name AS channel_table_name,
                   channels.handle AS channel_handle,
                   channels.is_active AS channel_is_active,
                   COALESCE(channels.name, live_jobs.channel_name) AS display_channel_name,
                   audio_playlists.name AS audio_playlist_name,
                   audio_playlists.status AS audio_playlist_status,
                   audio_playlists.prepared_audio_path AS prepared_audio_path,
                   audio_playlists.total_duration_seconds AS audio_playlist_duration_seconds
            FROM live_jobs
            JOIN videos ON videos.id = live_jobs.video_id
            LEFT JOIN channels ON channels.id = live_jobs.channel_id
            LEFT JOIN audio_playlists ON audio_playlists.id = live_jobs.audio_playlist_id
            {where_sql}
            ORDER BY {order_by}
            """,
            params,
        ).fetchall()
    ]

def fetch_history_jobs(conn: sqlite3.Connection, filters: dict[str, str] | None = None) -> list[dict[str, Any]]:
    filters = filters or {}
    where = ["(live_jobs.status IN ('done', 'stopped', 'error') OR live_jobs.archived_at IS NOT NULL)"]
    params: list[Any] = []
    channel_id = filters.get("channel_id", "").strip()
    status = filters.get("status", "").strip()
    date_from = filters.get("date_from", "").strip()
    date_to = filters.get("date_to", "").strip()
    search = filters.get("search", "").strip()

    if channel_id:
        where.append("live_jobs.channel_id = ?")
        params.append(channel_id)
    if status:
        if status == "archived":
            where.append("live_jobs.archived_at IS NOT NULL")
        else:
            where.append("live_jobs.status = ?")
            params.append(status)
    if date_from:
        where.append("date(COALESCE(live_jobs.stopped_at, live_jobs.archived_at, live_jobs.updated_at)) >= date(?)")
        params.append(date_from)
    if date_to:
        where.append("date(COALESCE(live_jobs.stopped_at, live_jobs.archived_at, live_jobs.updated_at)) <= date(?)")
        params.append(date_to)
    if search:
        like_value = f"%{search}%"
        where.append(
            """
            (
                live_jobs.live_name LIKE ?
                OR videos.original_name LIKE ?
                OR videos.filename LIKE ?
                OR COALESCE(channels.name, live_jobs.channel_name) LIKE ?
            )
            """
        )
        params.extend([like_value, like_value, like_value, like_value])

    return [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT live_jobs.*, videos.filename AS video_filename, videos.original_name AS video_original_name,
                   videos.path AS video_path,
                   channels.name AS channel_table_name,
                   channels.handle AS channel_handle,
                   channels.is_active AS channel_is_active,
                   COALESCE(channels.name, live_jobs.channel_name) AS display_channel_name,
                   audio_playlists.name AS audio_playlist_name,
                   audio_playlists.status AS audio_playlist_status,
                   audio_playlists.prepared_audio_path AS prepared_audio_path,
                   audio_playlists.total_duration_seconds AS audio_playlist_duration_seconds
            FROM live_jobs
            JOIN videos ON videos.id = live_jobs.video_id
            LEFT JOIN channels ON channels.id = live_jobs.channel_id
            LEFT JOIN audio_playlists ON audio_playlists.id = live_jobs.audio_playlist_id
            WHERE {' AND '.join(where)}
            ORDER BY COALESCE(live_jobs.stopped_at, live_jobs.archived_at, live_jobs.updated_at) DESC, live_jobs.id DESC
            """,
            params,
        ).fetchall()
    ]

def history_summary(jobs: list[dict[str, Any]]) -> dict[str, Any]:
    total_runtime_seconds = sum(job_runtime_seconds(job) or 0 for job in jobs)
    return {
        "completed": sum(1 for job in jobs if job.get("status") == "done"),
        "stopped": sum(1 for job in jobs if job.get("status") == "stopped"),
        "error": sum(1 for job in jobs if job.get("status") == "error"),
        "total_duration": format_runtime_seconds(total_runtime_seconds),
    }

def dashboard_history_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    today = local_now().date().isoformat()
    rows = [
        dict(row)
        for row in conn.execute(
            """
            SELECT live_jobs.*, videos.filename AS video_filename, videos.original_name AS video_original_name,
                   COALESCE(channels.name, live_jobs.channel_name) AS display_channel_name
            FROM live_jobs
            JOIN videos ON videos.id = live_jobs.video_id
            LEFT JOIN channels ON channels.id = live_jobs.channel_id
            WHERE date(COALESCE(live_jobs.stopped_at, live_jobs.updated_at)) = date(?)
            """,
            (today,),
        ).fetchall()
    ]
    last_finished = row_to_dict(
        conn.execute(
            """
            SELECT live_jobs.*, videos.original_name AS video_original_name,
                   COALESCE(channels.name, live_jobs.channel_name) AS display_channel_name
            FROM live_jobs
            JOIN videos ON videos.id = live_jobs.video_id
            LEFT JOIN channels ON channels.id = live_jobs.channel_id
            WHERE live_jobs.status IN ('done', 'stopped') AND live_jobs.stopped_at IS NOT NULL
            ORDER BY live_jobs.stopped_at DESC, live_jobs.id DESC
            LIMIT 1
            """
        ).fetchone()
    )
    last_failed = row_to_dict(
        conn.execute(
            """
            SELECT live_jobs.*, videos.original_name AS video_original_name,
                   COALESCE(channels.name, live_jobs.channel_name) AS display_channel_name
            FROM live_jobs
            JOIN videos ON videos.id = live_jobs.video_id
            LEFT JOIN channels ON channels.id = live_jobs.channel_id
            WHERE live_jobs.status = 'error'
            ORDER BY COALESCE(live_jobs.stopped_at, live_jobs.updated_at) DESC, live_jobs.id DESC
            LIMIT 1
            """
        ).fetchone()
    )
    return {
        "today_completed": sum(1 for job in rows if job.get("status") == "done"),
        "today_error": sum(1 for job in rows if job.get("status") == "error"),
        "runtime_today": format_runtime_seconds(sum(job_runtime_seconds(job) or 0 for job in rows)),
        "last_finished": last_finished,
        "last_failed": last_failed,
    }


def get_stop_at(job: dict[str, Any]) -> datetime | None:
    expected_end_at = parse_dt(job.get("expected_end_at"))
    if expected_end_at:
        return expected_end_at
    end_at = parse_dt(job.get("end_at"))
    if end_at:
        return end_at
    duration = job.get("duration_minutes")
    if not duration:
        return None
    start_point = parse_dt(job.get("started_at")) or parse_dt(job.get("start_at"))
    if not start_point:
        return None
    return start_point + timedelta(minutes=int(duration))


def log_path(job_id: int) -> Path:
    return LOG_DIR / f"job_{job_id}.log"


def latest_log_text(job_id: int, stream_key: str | None = None, max_chars: int = 6000) -> str:
    path = log_path(job_id)
    if not path.exists():
        return "No FFmpeg log has been written for this job yet."
    text = path.read_text(encoding="utf-8", errors="replace")
    return mask_log(text[-max_chars:], stream_key).strip() or "The log file is empty."

def audio_playlist_log_text(playlist_id: int, max_chars: int = 4000) -> str:
    path = LOG_DIR / f"audio_playlist_{playlist_id}.log"
    if not path.exists():
        return "No prepare log has been written for this playlist yet."
    text = path.read_text(encoding="utf-8", errors="replace")
    return text[-max_chars:].strip() or "The log file is empty."


def latest_any_log(conn: sqlite3.Connection) -> dict[str, Any] | None:
    candidates = []
    for path in LOG_DIR.glob("job_*.log"):
        try:
            job_id = int(path.stem.split("_", 1)[1])
            candidates.append((path.stat().st_mtime, job_id))
        except (IndexError, ValueError, OSError):
            continue
    if not candidates:
        return None
    _, job_id = max(candidates)
    job = get_job(conn, job_id)
    if not job:
        return None
    return {"job": job, "text": latest_log_text(job_id, job.get("stream_key"), 2500)}


def start_job(conn: sqlite3.Connection, job_id: int) -> tuple[bool, str]:
    job = get_job(conn, job_id)
    now_value = local_now()
    now = dt_to_str(now_value)
    if not job:
        return False, "Live job was not found."
    if job.get("archived_at"):
        return False, "Archived jobs must be restored or duplicated before starting."
    if job["status"] == "running" and process_exists(job.get("pid")):
        return True, "Live job is already running."
    ffmpeg_info = ffmpeg_probe()
    if not ffmpeg_info["detected"]:
        message = f"FFmpeg is not detected using '{ffmpeg_info['path']}'. {ffmpeg_info.get('error') or ''}".strip()
        update_job_error(conn, job_id, message)
        return False, message
    video_path = Path(job["video_path"])
    if not video_path.exists():
        message = f"Selected video does not exist: {video_path}"
        update_job_error(conn, job_id, message)
        return False, message
    if not job.get("stream_key"):
        message = "Stream key is required."
        update_job_error(conn, job_id, message)
        return False, message

    target = f"{YOUTUBE_RTMP_BASE}/{job['stream_key']}"
    audio_path = Path(job["prepared_audio_path"]) if job.get("prepared_audio_path") else None
    if job.get("audio_playlist_id"):
        if job.get("audio_playlist_status") != "ready" or not audio_path:
            message = "Selected audio playlist is not ready."
            update_job_error(conn, job_id, message)
            return False, message
        if not audio_path.exists():
            message = f"Prepared audio playlist file does not exist: {audio_path}"
            update_job_error(conn, job_id, message)
            return False, message

    if audio_path:
        duration_seconds = live_duration_seconds_for_start(job, now_value)
        cmd = [
            ffmpeg_info["path"],
            "-hide_banner",
            "-loglevel",
            "info",
            "-re",
            "-stream_loop",
            "-1",
            "-i",
            str(video_path),
            "-stream_loop",
            "-1",
            "-i",
            str(audio_path),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-b:v",
            "1500k",
            "-pix_fmt",
            "yuv420p",
            "-g",
            "50",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
        ]
        if duration_seconds:
            cmd.extend(["-t", str(duration_seconds)])
        cmd.extend(["-f", "flv", target])
    else:
        cmd = [
            ffmpeg_info["path"],
            "-hide_banner",
            "-loglevel",
            "info",
            "-re",
            "-stream_loop",
            "-1",
            "-i",
            str(video_path),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-maxrate",
            "4500k",
            "-bufsize",
            "9000k",
            "-pix_fmt",
            "yuv420p",
            "-g",
            "50",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-ar",
            "44100",
            "-f",
            "flv",
            target,
        ]
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)

    try:
        with log_path(job_id).open("ab") as log_file:
            log_file.write(f"\n\n[{now}] Starting FFmpeg for job {job_id}\n".encode("utf-8"))
            process = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                creationflags=flags,
            )
        expected_end_at = expected_end_for_start(job, now_value)
        conn.execute(
            """
            UPDATE live_jobs
            SET status = 'running', pid = ?, started_at = ?, stopped_at = NULL,
                expected_end_at = ?, exit_code = NULL, stop_reason = NULL,
                last_error = NULL, updated_at = ?
            WHERE id = ?
            """,
            (process.pid, now, dt_to_str(expected_end_at), now, job_id),
        )
        conn.commit()
        return True, f"FFmpeg started with PID {process.pid}."
    except Exception as exc:
        message = f"Could not start FFmpeg: {exc}"
        update_job_error(conn, job_id, message)
        return False, message


def complete_stopped_job(
    conn: sqlite3.Connection,
    job_id: int,
    status: str,
    stop_reason: str,
    message: str,
    exit_code: int | None = None,
) -> tuple[bool, str]:
    now = dt_to_str(local_now())
    with log_path(job_id).open("ab") as log_file:
        log_file.write(f"\n[{now}] {message}\n".encode("utf-8"))
    conn.execute(
        """
        UPDATE live_jobs
        SET status = ?, pid = NULL, stopped_at = ?, exit_code = ?,
            stop_reason = ?, last_error = CASE WHEN ? = 'error' THEN COALESCE(last_error, ?) ELSE NULL END,
            updated_at = ?
        WHERE id = ?
        """,
        (status, now, exit_code, stop_reason, status, message, now, job_id),
    )
    conn.commit()
    return True, message

def stop_job(conn: sqlite3.Connection, job_id: int, stop_reason: str = "manual_stop") -> tuple[bool, str]:
    job = get_job(conn, job_id)
    if not job:
        return False, "Live job was not found."
    if job.get("archived_at"):
        return False, "Archived jobs cannot be stopped."
    ok, message = stop_process(job.get("pid"))
    if ok:
        final_status = "done" if stop_reason in {"completed_duration", "scheduler_end"} else "stopped"
        return complete_stopped_job(conn, job_id, final_status, stop_reason, message)
    update_job_error(conn, job_id, message)
    return False, message


def update_job_error(conn: sqlite3.Connection, job_id: int, message: str, exit_code: int | None = None) -> None:
    now = dt_to_str(local_now())
    conn.execute(
        """
        UPDATE live_jobs
        SET status = 'error', pid = NULL, stopped_at = COALESCE(stopped_at, ?),
            exit_code = ?, stop_reason = 'process_error', last_error = ?, updated_at = ?
        WHERE id = ?
        """,
        (now, exit_code, message, now, job_id),
    )
    conn.commit()

def archive_job(conn: sqlite3.Connection, job_id: int, reason: str = "archived") -> tuple[bool, str]:
    job = get_job(conn, job_id)
    if not job:
        return False, "Live job was not found."
    if job.get("status") == "running":
        return False, "Running live jobs cannot be archived. Stop the job first."
    now = dt_to_str(local_now())
    conn.execute(
        """
        UPDATE live_jobs
        SET archived_at = COALESCE(archived_at, ?),
            stop_reason = COALESCE(stop_reason, ?),
            stopped_at = CASE WHEN status IN ('done', 'stopped', 'error') THEN COALESCE(stopped_at, ?) ELSE stopped_at END,
            updated_at = ?
        WHERE id = ?
        """,
        (now, reason, now, now, job_id),
    )
    conn.commit()
    return True, "Live job archived to History."

def duplicate_job_as_new(conn: sqlite3.Connection, job_id: int) -> tuple[bool, str]:
    job = get_job(conn, job_id)
    if not job:
        return False, "History record was not found."
    now = dt_to_str(local_now())
    db_cursor = conn.execute(
        """
        INSERT INTO live_jobs (
            live_name, channel_name, channel_id, video_id, audio_playlist_id, stream_key,
            start_at, end_at, duration_minutes, status, pid, created_at, updated_at,
            started_at, stopped_at, expected_end_at, exit_code, stop_reason, last_error, archived_at
        )
        VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, 'stopped', NULL, ?, ?,
                NULL, NULL, NULL, NULL, NULL, NULL, NULL)
        """,
        (
            f"{job['live_name']} copy",
            job["channel_name"],
            job.get("channel_id"),
            job["video_id"],
            job.get("audio_playlist_id"),
            job["stream_key"],
            job.get("duration_minutes"),
            now,
            now,
        ),
    )
    conn.commit()
    return True, f"History job duplicated as Live Job #{db_cursor.lastrowid}."


async def scheduler_loop() -> None:
    while True:
        try:
            run_scheduler_once()
        except Exception:
            pass
        await asyncio.sleep(10)


def run_scheduler_once() -> None:
    now = local_now()
    with connect_db() as conn:
        jobs = [
            dict(row)
            for row in conn.execute(
                """
                SELECT live_jobs.*, videos.path AS video_path, videos.filename AS video_filename
                FROM live_jobs
                JOIN videos ON videos.id = live_jobs.video_id
                WHERE status IN ('queued', 'scheduled', 'running')
                  AND live_jobs.archived_at IS NULL
                """
            ).fetchall()
        ]
        for job in jobs:
            job_id = int(job["id"])
            status = job["status"]
            if status == "running":
                stop_at = get_stop_at(job)
                if stop_at and now >= stop_at:
                    reason = "scheduler_end" if job.get("end_at") else "completed_duration"
                    stop_job(conn, job_id, reason)
                    continue
                if job.get("pid") and not process_exists(job.get("pid")):
                    exit_code = process_exit_code(job.get("pid"))
                    if stop_at and now >= stop_at:
                        reason = "scheduler_end" if job.get("end_at") else "completed_duration"
                        complete_stopped_job(conn, job_id, "done", reason, "FFmpeg process completed.")
                    else:
                        update_job_error(conn, job_id, "FFmpeg process exited unexpectedly.", exit_code)
                continue

            start_at = parse_dt(job.get("start_at"))
            end_at = parse_dt(job.get("end_at"))
            if end_at and now >= end_at:
                conn.execute(
                    """
                    UPDATE live_jobs
                    SET status = 'stopped', stopped_at = COALESCE(stopped_at, ?),
                        expected_end_at = COALESCE(expected_end_at, ?),
                        stop_reason = 'scheduler_end', updated_at = ?, last_error = NULL
                    WHERE id = ?
                    """,
                    (dt_to_str(now), dt_to_str(end_at), dt_to_str(now), job_id),
                )
                conn.commit()
                continue
            if start_at and now >= start_at:
                start_job(conn, job_id)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    task = asyncio.create_task(scheduler_loop())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="YouTube Live Streaming Manager", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, same_site="lax", https_only=False)


@app.exception_handler(RedirectToLogin)
async def redirect_to_login_handler(request: Request, exc: RedirectToLogin) -> RedirectResponse:
    return redirect("/login")


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: str | None = None):
    if is_logged_in(request):
        return redirect("/")
    return templates.TemplateResponse("login.html", {"request": request, "error": error})


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    valid_user = secrets.compare_digest(username, ADMIN_USERNAME)
    valid_password = secrets.compare_digest(password, ADMIN_PASSWORD)
    if valid_user and valid_password:
        request.session["authenticated"] = True
        request.session["username"] = username
        return redirect("/")
    return redirect("/login?error=Invalid%20username%20or%20password")


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return redirect("/login")


@app.post("/ffmpeg/test")
def test_ffmpeg(_: None = Depends(require_admin)):
    info = ffmpeg_probe(timeout=10)
    if info["detected"]:
        return redirect(f"/settings?{urlencode({'message': 'FFmpeg detected: ' + (info.get('version') or info['path'])})}")
    return redirect(f"/settings?{urlencode({'error': 'FFmpeg test failed: ' + (info.get('error') or info['path'])})}")

@app.post("/channels")
def create_channel(
    name: str = Form(...),
    handle: str | None = Form(None),
    niche: str | None = Form(None),
    notes: str | None = Form(None),
    default_stream_key: str | None = Form(None),
    is_active: str | None = Form(None),
    db: sqlite3.Connection = Depends(get_db),
    _: None = Depends(require_admin),
):
    clean_name = name.strip()
    if not clean_name:
        return redirect(f"/channels?{urlencode({'error': 'Channel name is required.'})}")
    if channel_name_exists(db, clean_name):
        return redirect(f"/channels?{urlencode({'error': 'Channel name already exists.'})}")
    now = dt_to_str(local_now())
    db.execute(
        """
        INSERT INTO channels (
            name, handle, niche, notes, default_stream_key, is_active, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            clean_name,
            clean_optional(handle),
            clean_optional(niche),
            clean_optional(notes),
            clean_optional(default_stream_key),
            1 if is_active else 0,
            now,
            now,
        ),
    )
    db.commit()
    return redirect(f"/channels?{urlencode({'message': 'Channel added.'})}")

@app.post("/channels/{channel_id}/update")
def update_channel(
    channel_id: int,
    name: str = Form(...),
    handle: str | None = Form(None),
    niche: str | None = Form(None),
    notes: str | None = Form(None),
    default_stream_key: str | None = Form(None),
    is_active: str | None = Form(None),
    db: sqlite3.Connection = Depends(get_db),
    _: None = Depends(require_admin),
):
    clean_name = name.strip()
    if not clean_name:
        return redirect(f"/channels?{urlencode({'error': 'Channel name is required.'})}")
    channel = db.execute("SELECT id FROM channels WHERE id = ?", (channel_id,)).fetchone()
    if not channel:
        return redirect(f"/channels?{urlencode({'error': 'Channel was not found.'})}")
    if channel_name_exists(db, clean_name, exclude_id=channel_id):
        return redirect(f"/channels?{urlencode({'error': 'Channel name already exists.'})}")
    now = dt_to_str(local_now())
    db.execute(
        """
        UPDATE channels
        SET name = ?, handle = ?, niche = ?, notes = ?, default_stream_key = ?,
            is_active = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            clean_name,
            clean_optional(handle),
            clean_optional(niche),
            clean_optional(notes),
            clean_optional(default_stream_key),
            1 if is_active else 0,
            now,
            channel_id,
        ),
    )
    db.commit()
    return redirect(f"/channels?{urlencode({'message': 'Channel updated.'})}")

@app.post("/channels/{channel_id}/delete")
def delete_channel(
    channel_id: int,
    db: sqlite3.Connection = Depends(get_db),
    _: None = Depends(require_admin),
):
    channel = db.execute("SELECT id FROM channels WHERE id = ?", (channel_id,)).fetchone()
    if not channel:
        return redirect(f"/channels?{urlencode({'error': 'Channel was not found.'})}")
    usage_count = db.execute("SELECT COUNT(*) FROM live_jobs WHERE channel_id = ?", (channel_id,)).fetchone()[0]
    if usage_count:
        db.execute(
            "UPDATE channels SET is_active = 0, updated_at = ? WHERE id = ?",
            (dt_to_str(local_now()), channel_id),
        )
        db.commit()
        return redirect(f"/channels?{urlencode({'message': 'Channel is used by live jobs, so it was set inactive.'})}")
    db.execute("DELETE FROM channels WHERE id = ?", (channel_id,))
    db.commit()
    return redirect(f"/channels?{urlencode({'message': 'Channel deleted.'})}")

@app.get("/audio-assets/{asset_id}/preview")
def preview_audio_asset(
    asset_id: int,
    db: sqlite3.Connection = Depends(get_db),
    _: None = Depends(require_admin),
):
    asset = db.execute("SELECT * FROM audio_assets WHERE id = ?", (asset_id,)).fetchone()
    if not asset:
        return Response(status_code=404)
    path = Path(asset["path"])
    if not path.exists():
        return Response(status_code=404)
    return FileResponse(path)

@app.post("/audio-assets")
def upload_audio_asset(
    files: list[UploadFile] | None = File(None),
    file: UploadFile | None = File(None),
    db: sqlite3.Connection = Depends(get_db),
    _: None = Depends(require_admin),
):
    selected_files = [item for item in (files or []) if getattr(item, "filename", "")]
    if file and getattr(file, "filename", ""):
        selected_files.append(file)
    total_selected = len(selected_files)
    if not total_selected:
        return redirect(f"/audio?{urlencode({'error': 'Select at least one MP3, WAV, or M4A audio file.'})}")

    allowed_suffixes = {".mp3", ".wav", ".m4a"}
    uploaded_count = 0
    skipped_names: list[str] = []
    now = dt_to_str(local_now())
    for index, upload in enumerate(selected_files, start=1):
        original_name = upload.filename or ""
        if Path(original_name).suffix.lower() not in allowed_suffixes:
            skipped_names.append(f"{original_name or 'unnamed file'} (format tidak didukung)")
            continue
        try:
            filename = f"{datetime.now().strftime('%Y%m%d%H%M%S%f')}_{index}_{safe_audio_filename(original_name)}"
            target = AUDIO_DIR / filename
            with target.open("wb") as output:
                shutil.copyfileobj(upload.file, output)
            duration_seconds = probe_audio_duration(target)
            db.execute(
                """
                INSERT INTO audio_assets (filename, original_filename, path, file_size, duration_seconds, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (filename, original_name, str(target), target.stat().st_size, duration_seconds, now),
            )
            uploaded_count += 1
        except Exception:
            skipped_names.append(f"{original_name or 'unnamed file'} (gagal disimpan)")
    db.commit()

    skipped_count = total_selected - uploaded_count
    summary = (
        f"Ringkasan upload: total selected {total_selected}, "
        f"uploaded successfully {uploaded_count}, skipped/failed {skipped_count}."
    )
    if skipped_names:
        shown = ", ".join(skipped_names[:5])
        more = f" dan {len(skipped_names) - 5} file lain" if len(skipped_names) > 5 else ""
        summary = f"{summary} Dilewati/gagal: {shown}{more}."
    key = "message" if uploaded_count else "error"
    return redirect(f"/audio?{urlencode({key: summary})}")

@app.post("/audio-assets/{asset_id}/delete")
def delete_audio_asset(
    asset_id: int,
    db: sqlite3.Connection = Depends(get_db),
    _: None = Depends(require_admin),
):
    asset = db.execute("SELECT * FROM audio_assets WHERE id = ?", (asset_id,)).fetchone()
    if not asset:
        return redirect(f"/audio?{urlencode({'error': 'Audio file was not found.'})}")
    usage_count = db.execute(
        "SELECT COUNT(*) FROM audio_playlist_items WHERE audio_asset_id = ?",
        (asset_id,),
    ).fetchone()[0]
    if usage_count:
        return redirect(f"/audio?{urlencode({'error': 'Audio is used in a playlist and cannot be deleted.'})}")
    path = Path(asset["path"])
    if path.exists():
        path.unlink()
    db.execute("DELETE FROM audio_assets WHERE id = ?", (asset_id,))
    db.commit()
    return redirect(f"/audio?{urlencode({'message': 'Audio deleted.'})}")

@app.post("/audio-playlists")
def create_audio_playlist(
    channel_id: int = Form(...),
    name: str = Form(...),
    db: sqlite3.Connection = Depends(get_db),
    _: None = Depends(require_admin),
):
    clean_name = name.strip()
    channel = db.execute("SELECT id FROM channels WHERE id = ?", (channel_id,)).fetchone()
    if not channel:
        return redirect(f"/playlists?{urlencode({'error': 'Select a valid channel.'})}")
    if not clean_name:
        return redirect(f"/playlists?{urlencode({'error': 'Playlist name is required.'})}")
    now = dt_to_str(local_now())
    db.execute(
        """
        INSERT INTO audio_playlists (channel_id, name, status, created_at, updated_at)
        VALUES (?, ?, 'draft', ?, ?)
        """,
        (channel_id, clean_name, now, now),
    )
    db.commit()
    return redirect(f"/playlists?{urlencode({'message': 'Playlist created.'})}")

@app.post("/audio-playlists/{playlist_id}/add-item")
def add_playlist_item(
    playlist_id: int,
    audio_asset_id: int = Form(...),
    db: sqlite3.Connection = Depends(get_db),
    _: None = Depends(require_admin),
):
    playlist = db.execute("SELECT id FROM audio_playlists WHERE id = ?", (playlist_id,)).fetchone()
    asset = db.execute("SELECT id FROM audio_assets WHERE id = ?", (audio_asset_id,)).fetchone()
    if not playlist or not asset:
        return redirect(f"/playlists?{urlencode({'error': 'Playlist or audio file was not found.'})}")
    row = db.execute(
        "SELECT COALESCE(MAX(sort_order), 0) + 1 AS next_order FROM audio_playlist_items WHERE playlist_id = ?",
        (playlist_id,),
    ).fetchone()
    db.execute(
        "INSERT INTO audio_playlist_items (playlist_id, audio_asset_id, sort_order) VALUES (?, ?, ?)",
        (playlist_id, audio_asset_id, row["next_order"]),
    )
    db.execute(
        "UPDATE audio_playlists SET status = 'draft', updated_at = ?, prepared_audio_path = NULL, last_error = NULL WHERE id = ?",
        (dt_to_str(local_now()), playlist_id),
    )
    db.commit()
    return redirect(f"/playlists?{urlencode({'message': 'Audio added to playlist.'})}")

@app.post("/audio-playlist-items/{item_id}/remove")
def remove_playlist_item(
    item_id: int,
    db: sqlite3.Connection = Depends(get_db),
    _: None = Depends(require_admin),
):
    item = db.execute("SELECT * FROM audio_playlist_items WHERE id = ?", (item_id,)).fetchone()
    if not item:
        return redirect(f"/playlists?{urlencode({'error': 'Playlist item was not found.'})}")
    playlist_id = item["playlist_id"]
    db.execute("DELETE FROM audio_playlist_items WHERE id = ?", (item_id,))
    db.execute(
        "UPDATE audio_playlists SET status = 'draft', updated_at = ?, prepared_audio_path = NULL WHERE id = ?",
        (dt_to_str(local_now()), playlist_id),
    )
    db.commit()
    return redirect(f"/playlists?{urlencode({'message': 'Audio removed from playlist.'})}")

@app.post("/audio-playlist-items/{item_id}/move")
def move_playlist_item(
    item_id: int,
    direction: str = Form(...),
    db: sqlite3.Connection = Depends(get_db),
    _: None = Depends(require_admin),
):
    item = db.execute("SELECT * FROM audio_playlist_items WHERE id = ?", (item_id,)).fetchone()
    if not item:
        return redirect(f"/playlists?{urlencode({'error': 'Playlist item was not found.'})}")
    comparator = "<" if direction == "up" else ">"
    order = "DESC" if direction == "up" else "ASC"
    other = db.execute(
        f"""
        SELECT * FROM audio_playlist_items
        WHERE playlist_id = ? AND sort_order {comparator} ?
        ORDER BY sort_order {order}, id {order}
        LIMIT 1
        """,
        (item["playlist_id"], item["sort_order"]),
    ).fetchone()
    if other:
        db.execute("UPDATE audio_playlist_items SET sort_order = ? WHERE id = ?", (other["sort_order"], item_id))
        db.execute("UPDATE audio_playlist_items SET sort_order = ? WHERE id = ?", (item["sort_order"], other["id"]))
        db.execute(
            "UPDATE audio_playlists SET status = 'draft', updated_at = ?, prepared_audio_path = NULL WHERE id = ?",
            (dt_to_str(local_now()), item["playlist_id"]),
        )
        db.commit()
    return redirect("/playlists")

@app.post("/audio-playlists/{playlist_id}/shuffle")
def shuffle_playlist(
    playlist_id: int,
    db: sqlite3.Connection = Depends(get_db),
    _: None = Depends(require_admin),
):
    items = [dict(row) for row in db.execute("SELECT * FROM audio_playlist_items WHERE playlist_id = ?", (playlist_id,)).fetchall()]
    if not items:
        return redirect(f"/playlists?{urlencode({'error': 'Cannot shuffle an empty playlist.'})}")
    random.shuffle(items)
    for index, item in enumerate(items, start=1):
        db.execute("UPDATE audio_playlist_items SET sort_order = ? WHERE id = ?", (index, item["id"]))
    db.execute(
        "UPDATE audio_playlists SET status = 'draft', updated_at = ?, prepared_audio_path = NULL WHERE id = ?",
        (dt_to_str(local_now()), playlist_id),
    )
    db.commit()
    return redirect(f"/playlists?{urlencode({'message': 'Playlist shuffled.'})}")

@app.post("/audio-playlists/{playlist_id}/duplicate")
def duplicate_playlist(
    playlist_id: int,
    db: sqlite3.Connection = Depends(get_db),
    _: None = Depends(require_admin),
):
    playlist = db.execute("SELECT * FROM audio_playlists WHERE id = ?", (playlist_id,)).fetchone()
    if not playlist:
        return redirect(f"/playlists?{urlencode({'error': 'Playlist was not found.'})}")
    now = dt_to_str(local_now())
    cursor = db.execute(
        """
        INSERT INTO audio_playlists (channel_id, name, status, created_at, updated_at)
        VALUES (?, ?, 'draft', ?, ?)
        """,
        (playlist["channel_id"], f"{playlist['name']} Copy", now, now),
    )
    new_playlist_id = cursor.lastrowid
    for item in db.execute("SELECT * FROM audio_playlist_items WHERE playlist_id = ? ORDER BY sort_order, id", (playlist_id,)).fetchall():
        db.execute(
            "INSERT INTO audio_playlist_items (playlist_id, audio_asset_id, sort_order) VALUES (?, ?, ?)",
            (new_playlist_id, item["audio_asset_id"], item["sort_order"]),
        )
    db.commit()
    return redirect(f"/playlists?{urlencode({'message': 'Playlist duplicated.'})}")

def prepare_audio_playlist(conn: sqlite3.Connection, playlist_id: int) -> tuple[bool, str]:
    ffmpeg_info = ffmpeg_probe(timeout=10)
    ffprobe_info = ffprobe_probe(timeout=10)
    if not ffmpeg_info["detected"] or not ffprobe_info["detected"]:
        return False, "FFmpeg and FFprobe are required to prepare playlists."
    playlist = conn.execute("SELECT * FROM audio_playlists WHERE id = ?", (playlist_id,)).fetchone()
    if not playlist:
        return False, "Playlist was not found."
    items = [
        dict(row)
        for row in conn.execute(
            """
            SELECT audio_assets.*
            FROM audio_playlist_items
            JOIN audio_assets ON audio_assets.id = audio_playlist_items.audio_asset_id
            WHERE audio_playlist_items.playlist_id = ?
            ORDER BY audio_playlist_items.sort_order, audio_playlist_items.id
            """,
            (playlist_id,),
        ).fetchall()
    ]
    if not items:
        return False, "Cannot prepare playlist without audio items."
    missing = [item["original_filename"] for item in items if not Path(item["path"]).exists()]
    if missing:
        return False, f"Missing audio files: {', '.join(missing)}"

    now = dt_to_str(local_now())
    output_path = READY_DIR / f"audio_playlist_{playlist_id}.m4a"
    concat_path = READY_DIR / f"audio_playlist_{playlist_id}.txt"
    log_file_path = LOG_DIR / f"audio_playlist_{playlist_id}.log"
    conn.execute(
        "UPDATE audio_playlists SET status = 'processing', updated_at = ?, last_error = NULL WHERE id = ?",
        (now, playlist_id),
    )
    conn.commit()

    def concat_line(path_value: str) -> str:
        escaped = path_value.replace("\\", "/").replace("'", "'\\''")
        return f"file '{escaped}'"

    concat_path.write_text("\n".join(concat_line(item["path"]) for item in items), encoding="utf-8")
    cmd = [
        ffmpeg_info["path"],
        "-y",
        "-hide_banner",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_path),
        "-vn",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        str(output_path),
    ]
    with log_file_path.open("w", encoding="utf-8", errors="replace") as log_file:
        process = subprocess.run(cmd, stdout=log_file, stderr=subprocess.STDOUT, text=True)
    if process.returncode != 0 or not output_path.exists():
        error = f"FFmpeg prepare failed with exit code {process.returncode}."
        conn.execute(
            "UPDATE audio_playlists SET status = 'error', last_error = ?, updated_at = ? WHERE id = ?",
            (error, dt_to_str(local_now()), playlist_id),
        )
        conn.commit()
        return False, error
    duration_seconds = probe_audio_duration(output_path) or playlist_total_duration(conn, playlist_id)
    conn.execute(
        """
        UPDATE audio_playlists
        SET status = 'ready', prepared_audio_path = ?, total_duration_seconds = ?,
            last_error = NULL, updated_at = ?
        WHERE id = ?
        """,
        (str(output_path), duration_seconds, dt_to_str(local_now()), playlist_id),
    )
    conn.commit()
    return True, "Playlist prepared."

@app.post("/audio-playlists/{playlist_id}/prepare")
def prepare_playlist_route(
    playlist_id: int,
    db: sqlite3.Connection = Depends(get_db),
    _: None = Depends(require_admin),
):
    ok, message = prepare_audio_playlist(db, playlist_id)
    key = "message" if ok else "error"
    return redirect(f"/playlists?{urlencode({key: message})}")

NAV_ITEMS = [
    {"key": "dashboard", "label": "Dashboard", "href": "/dashboard"},
    {"key": "channels", "label": "Channels", "href": "/channels"},
    {"key": "videos", "label": "Video Library", "href": "/videos"},
    {"key": "audio", "label": "Audio Library", "href": "/audio"},
    {"key": "playlists", "label": "Playlists", "href": "/playlists"},
    {"key": "live_jobs", "label": "Live Jobs", "href": "/live-jobs"},
    {"key": "scheduler", "label": "Scheduler", "href": "/scheduler"},
    {"key": "history", "label": "History", "href": "/history"},
    {"key": "logs", "label": "Logs", "href": "/logs"},
    {"key": "settings", "label": "Settings", "href": "/settings"},
]

def admin_context(
    request: Request,
    db: sqlite3.Connection,
    active_tab: str,
    page_title: str,
    message: str | None = None,
    error: str | None = None,
    job_filters: dict[str, str] | None = None,
) -> dict[str, Any]:
    videos = [dict(row) for row in db.execute("SELECT * FROM videos ORDER BY uploaded_at DESC").fetchall()]
    jobs = fetch_live_jobs(db, job_filters if active_tab == "live_jobs" else None)
    history_jobs = fetch_history_jobs(db, job_filters if active_tab == "history" else None)
    channels = [
        dict(row)
        for row in db.execute(
            """
            SELECT channels.*,
                   COUNT(live_jobs.id) AS live_job_count
            FROM channels
            LEFT JOIN live_jobs ON live_jobs.channel_id = channels.id
            GROUP BY channels.id
            ORDER BY channels.is_active DESC, channels.name COLLATE NOCASE
            """
        ).fetchall()
    ]
    active_channels = [
        dict(row)
        for row in db.execute(
            "SELECT * FROM channels WHERE is_active = 1 ORDER BY name COLLATE NOCASE"
        ).fetchall()
    ]
    audio_assets = [
        dict(row)
        for row in db.execute(
            """
            SELECT audio_assets.*,
                   COUNT(audio_playlist_items.id) AS playlist_usage_count
            FROM audio_assets
            LEFT JOIN audio_playlist_items ON audio_playlist_items.audio_asset_id = audio_assets.id
            GROUP BY audio_assets.id
            ORDER BY audio_assets.created_at DESC
            """
        ).fetchall()
    ]
    audio_playlists = [
        dict(row)
        for row in db.execute(
            """
            SELECT audio_playlists.*, channels.name AS channel_name, channels.handle AS channel_handle,
                   COUNT(audio_playlist_items.id) AS item_count,
                   COALESCE(SUM(audio_assets.duration_seconds), 0) AS item_duration_seconds
            FROM audio_playlists
            JOIN channels ON channels.id = audio_playlists.channel_id
            LEFT JOIN audio_playlist_items ON audio_playlist_items.playlist_id = audio_playlists.id
            LEFT JOIN audio_assets ON audio_assets.id = audio_playlist_items.audio_asset_id
            GROUP BY audio_playlists.id
            ORDER BY channels.name COLLATE NOCASE, audio_playlists.created_at DESC
            """
        ).fetchall()
    ]
    playlist_items = [
        dict(row)
        for row in db.execute(
            """
            SELECT audio_playlist_items.*, audio_assets.original_filename, audio_assets.filename,
                   audio_assets.duration_seconds, audio_playlists.channel_id
            FROM audio_playlist_items
            JOIN audio_assets ON audio_assets.id = audio_playlist_items.audio_asset_id
            JOIN audio_playlists ON audio_playlists.id = audio_playlist_items.playlist_id
            ORDER BY audio_playlist_items.playlist_id, audio_playlist_items.sort_order, audio_playlist_items.id
            """
        ).fetchall()
    ]
    ready_playlists = [
        playlist for playlist in audio_playlists if playlist.get("status") == "ready" and playlist.get("prepared_audio_path")
    ]
    totals = {
        "videos": db.execute("SELECT COUNT(*) FROM videos").fetchone()[0],
        "jobs": db.execute("SELECT COUNT(*) FROM live_jobs").fetchone()[0],
        "running": db.execute("SELECT COUNT(*) FROM live_jobs WHERE status = 'running'").fetchone()[0],
        "stopped": db.execute("SELECT COUNT(*) FROM live_jobs WHERE status = 'stopped'").fetchone()[0],
        "error": db.execute("SELECT COUNT(*) FROM live_jobs WHERE status = 'error'").fetchone()[0],
    }
    ffmpeg_info = ffmpeg_probe()
    warnings = []
    if not ffmpeg_info["detected"]:
        warnings.append(f"FFmpeg is not detected using '{ffmpeg_info['path']}'. Start Live will not work until FFmpeg is available.")
    if ADMIN_USERNAME == "admin" and ADMIN_PASSWORD == "admin123":
        warnings.append("Default admin credentials are active. Set ADMIN_USERNAME and ADMIN_PASSWORD before using this beyond local testing.")
    for job in jobs:
        if not Path(job["video_path"]).exists():
            warnings.append(f"Video file missing for job '{job['live_name']}': {job['video_filename']}")
    scheduled_jobs = [
        job for job in jobs if job.get("status") == "scheduled" or job.get("start_at") or job.get("end_at")
    ]
    completed_jobs = [job for job in history_jobs if job.get("status") in FINAL_STATUSES]
    history_totals = history_summary(history_jobs)
    dashboard_history = dashboard_history_summary(db)
    return {
        "request": request,
        "nav_items": NAV_ITEMS,
        "active_tab": active_tab,
        "page_title": page_title,
        "videos": videos,
        "jobs": jobs,
        "history_jobs": history_jobs,
        "job_filters": job_filters or {},
        "job_statuses": ["queued", "scheduled", "running", "stopped", "done", "error"],
        "history_statuses": ["done", "stopped", "error", "archived"],
        "scheduled_jobs": scheduled_jobs,
        "completed_jobs": completed_jobs,
        "history_totals": history_totals,
        "dashboard_history": dashboard_history,
        "channels": channels,
        "active_channels": active_channels,
        "audio_assets": audio_assets,
        "audio_playlists": audio_playlists,
        "playlist_items": playlist_items,
        "ready_playlists": ready_playlists,
        "totals": totals,
        "warnings": warnings,
        "message": message,
        "error": error,
        "ffmpeg_info": ffmpeg_info,
        "latest_log": latest_any_log(db),
        "display_dt": display_dt,
        "display_dt_local": display_dt_local,
        "format_duration_minutes": format_duration_minutes,
        "format_seconds": format_seconds,
        "format_runtime_seconds": format_runtime_seconds,
        "job_runtime_seconds": job_runtime_seconds,
        "running_duration": running_duration,
        "status_badge_class": status_badge_class,
        "job_schedule_lines": job_schedule_lines,
        "audio_playlist_log_text": audio_playlist_log_text,
        "mask_secret": mask_secret,
        "admin_username": ADMIN_USERNAME,
        "database_path": str(DB_PATH),
        "video_dir": str(VIDEO_DIR),
        "audio_dir": str(AUDIO_DIR),
        "ready_dir": str(READY_DIR),
        "log_dir": str(LOG_DIR),
    }

def render_admin(
    template_name: str,
    request: Request,
    db: sqlite3.Connection,
    active_tab: str,
    page_title: str,
    message: str | None = None,
    error: str | None = None,
    job_filters: dict[str, str] | None = None,
) -> HTMLResponse:
    return templates.TemplateResponse(
        template_name,
        admin_context(request, db, active_tab, page_title, message, error, job_filters),
    )

@app.get("/", response_class=HTMLResponse)
def root(
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
    _: None = Depends(require_admin),
    message: str | None = None,
    error: str | None = None,
):
    return render_admin("dashboard.html", request, db, "dashboard", "Dashboard", message, error)

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_page(
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
    _: None = Depends(require_admin),
    message: str | None = None,
    error: str | None = None,
):
    return render_admin("dashboard.html", request, db, "dashboard", "Dashboard", message, error)

@app.get("/channels", response_class=HTMLResponse)
def channels_page(
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
    _: None = Depends(require_admin),
    message: str | None = None,
    error: str | None = None,
):
    return render_admin("channels.html", request, db, "channels", "Channels", message, error)

@app.get("/videos", response_class=HTMLResponse)
def videos_page(
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
    _: None = Depends(require_admin),
    message: str | None = None,
    error: str | None = None,
):
    return render_admin("videos.html", request, db, "videos", "Video Library", message, error)

@app.get("/audio", response_class=HTMLResponse)
def audio_page(
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
    _: None = Depends(require_admin),
    message: str | None = None,
    error: str | None = None,
):
    return render_admin("audio.html", request, db, "audio", "Audio Library", message, error)

@app.get("/playlists", response_class=HTMLResponse)
def playlists_page(
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
    _: None = Depends(require_admin),
    message: str | None = None,
    error: str | None = None,
):
    return render_admin("playlists.html", request, db, "playlists", "Playlists", message, error)

@app.get("/live-jobs", response_class=HTMLResponse)
def live_jobs_page(
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
    _: None = Depends(require_admin),
    message: str | None = None,
    error: str | None = None,
    channel_id: str = "",
    status: str = "",
    date_from: str = "",
    date_to: str = "",
    search: str = "",
    sort: str = "newest",
):
    job_filters = {
        "channel_id": channel_id,
        "status": status,
        "date_from": date_from,
        "date_to": date_to,
        "search": search,
        "sort": sort,
    }
    return render_admin("live_jobs.html", request, db, "live_jobs", "Live Jobs", message, error, job_filters)

@app.get("/scheduler", response_class=HTMLResponse)
def scheduler_page(
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
    _: None = Depends(require_admin),
    message: str | None = None,
    error: str | None = None,
):
    return render_admin("scheduler.html", request, db, "scheduler", "Scheduler", message, error)

@app.get("/history", response_class=HTMLResponse)
def history_page(
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
    _: None = Depends(require_admin),
    message: str | None = None,
    error: str | None = None,
    channel_id: str = "",
    status: str = "",
    date_from: str = "",
    date_to: str = "",
    search: str = "",
):
    job_filters = {
        "channel_id": channel_id,
        "status": status,
        "date_from": date_from,
        "date_to": date_to,
        "search": search,
    }
    return render_admin("history.html", request, db, "history", "History", message, error, job_filters)

@app.get("/history/export.csv")
def history_export_csv(
    db: sqlite3.Connection = Depends(get_db),
    _: None = Depends(require_admin),
    channel_id: str = "",
    status: str = "",
    date_from: str = "",
    date_to: str = "",
    search: str = "",
):
    jobs = fetch_history_jobs(
        db,
        {
            "channel_id": channel_id,
            "status": status,
            "date_from": date_from,
            "date_to": date_to,
            "search": search,
        },
    )
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Live name",
        "Channel",
        "Video",
        "Audio playlist",
        "Started at",
        "Stopped at",
        "Duration ran",
        "Final status",
        "Stop reason",
        "Exit code",
        "Error summary",
    ])
    for job in jobs:
        writer.writerow([
            job.get("live_name") or "",
            job.get("display_channel_name") or job.get("channel_name") or "",
            job.get("video_original_name") or "",
            job.get("audio_playlist_name") or "",
            job.get("started_at") or "",
            job.get("stopped_at") or "",
            format_runtime_seconds(job_runtime_seconds(job)),
            job.get("status") or "",
            job.get("stop_reason") or "",
            job.get("exit_code") if job.get("exit_code") is not None else "",
            job.get("last_error") or "",
        ])
    headers = {"Content-Disposition": "attachment; filename=live_history.csv"}
    return Response(output.getvalue(), media_type="text/csv", headers=headers)

@app.get("/logs", response_class=HTMLResponse)
def logs_page(
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
    _: None = Depends(require_admin),
    message: str | None = None,
    error: str | None = None,
):
    return render_admin("logs_admin.html", request, db, "logs", "Logs", message, error)

@app.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
    _: None = Depends(require_admin),
    message: str | None = None,
    error: str | None = None,
):
    return render_admin("settings.html", request, db, "settings", "Settings", message, error)


@app.post("/videos")
def upload_video(
    request: Request,
    file: UploadFile = File(...),
    db: sqlite3.Connection = Depends(get_db),
    _: None = Depends(require_admin),
):
    original_name = file.filename or ""
    if not original_name.lower().endswith(".mp4"):
        return redirect("/videos?error=Only%20MP4%20uploads%20are%20allowed")
    filename = f"{local_now().strftime('%Y%m%d%H%M%S')}_{safe_filename(original_name)}"
    target = VIDEO_DIR / filename
    with target.open("wb") as output:
        shutil.copyfileobj(file.file, output)
    now = dt_to_str(local_now())
    db.execute(
        "INSERT INTO videos (filename, original_name, path, uploaded_at) VALUES (?, ?, ?, ?)",
        (filename, original_name, str(target), now),
    )
    db.commit()
    return redirect("/videos?message=Video%20berhasil%20di-upload%20dan%20siap%20dipakai.")


@app.post("/jobs")
def create_job(
    request: Request,
    live_name: str = Form(...),
    channel_id: str = Form(""),
    video_id: str = Form(""),
    audio_playlist_id: str = Form(""),
    stream_key: str = Form(""),
    schedule_mode: str = Form("manual_now"),
    start_at: str | None = Form(None),
    end_at: str | None = Form(None),
    duration_minutes: str | None = Form(None),
    status: str = Form("stopped"),
    db: sqlite3.Connection = Depends(get_db),
    _: None = Depends(require_admin),
):
    clean_live_name = live_name.strip()
    selected_channel_id = parse_optional_int(channel_id)
    selected_video_id = parse_optional_int(video_id)
    selected_audio_playlist_value = parse_optional_int(audio_playlist_id)
    if selected_channel_id is None:
        return redirect(f"/live-jobs?{urlencode({'error': 'Select an active channel first.'})}")
    channel = db.execute("SELECT * FROM channels WHERE id = ? AND is_active = 1", (selected_channel_id,)).fetchone()
    if not channel:
        return redirect(f"/live-jobs?{urlencode({'error': 'Select an active channel first.'})}")
    clean_channel_name = channel["name"]
    clean_stream_key = stream_key.strip() or (channel["default_stream_key"] or "").strip()
    if not clean_live_name or not clean_channel_name or not clean_stream_key:
        return redirect("/live-jobs?error=Live%20name,%20channel,%20and%20stream%20key%20are%20required")
    if status not in STATUSES - {"running"}:
        status = "stopped"
    if selected_video_id is None:
        return redirect("/live-jobs?error=Select%20an%20uploaded%20video%20first")
    video = db.execute("SELECT * FROM videos WHERE id = ?", (selected_video_id,)).fetchone()
    if not video:
        return redirect("/live-jobs?error=Selected%20video%20was%20not%20found")
    if not Path(video["path"]).exists():
        return redirect("/live-jobs?error=Selected%20video%20file%20does%20not%20exist")
    selected_audio_playlist_id = None
    if selected_audio_playlist_value:
        playlist = db.execute(
            """
            SELECT * FROM audio_playlists
            WHERE id = ? AND channel_id = ? AND status = 'ready' AND prepared_audio_path IS NOT NULL
            """,
            (selected_audio_playlist_value, selected_channel_id),
        ).fetchone()
        if not playlist:
            return redirect(f"/live-jobs?{urlencode({'error': 'Selected audio playlist is not ready for this channel.'})}")
        if not Path(playlist["prepared_audio_path"]).exists():
            return redirect(f"/live-jobs?{urlencode({'error': 'Prepared audio playlist file is missing.'})}")
        selected_audio_playlist_id = selected_audio_playlist_value
    if schedule_mode not in {"manual_now", "start_end", "start_duration"}:
        schedule_mode = "manual_now"
    try:
        start_value = parse_dt(start_at) if start_at else None
        end_value = parse_dt(end_at) if end_at else None
    except ValueError:
        return redirect("/live-jobs?error=Invalid%20date%20or%20time")

    duration_value = None
    if duration_minutes:
        try:
            duration_value = int(duration_minutes)
            if duration_value <= 0:
                raise ValueError
        except ValueError:
            return redirect("/live-jobs?error=Duration%20must%20be%20a%20positive%20number")

    if schedule_mode == "manual_now":
        start_value = None
        end_value = None
    elif schedule_mode == "start_end":
        if not start_value or not end_value:
            return redirect(f"/live-jobs?{urlencode({'error': 'Start datetime dan end datetime wajib diisi.'})}")
        if end_value <= start_value:
            return redirect(f"/live-jobs?{urlencode({'error': 'End datetime harus setelah start datetime.'})}")
        duration_value = duration_between_minutes(start_value, end_value)
    elif schedule_mode == "start_duration":
        if not start_value:
            return redirect(f"/live-jobs?{urlencode({'error': 'Start datetime wajib diisi.'})}")
        if not duration_value or duration_value <= 0:
            return redirect(f"/live-jobs?{urlencode({'error': 'Duration minutes harus lebih dari 0.'})}")
        end_value = None

    if start_value and end_value and end_value <= start_value:
        return redirect(f"/live-jobs?{urlencode({'error': 'End datetime harus setelah start datetime.'})}")
    if start_value and end_value:
        duration_value = duration_between_minutes(start_value, end_value)
    if start_value and status == "stopped":
        status = "scheduled"

    now = dt_to_str(local_now())
    db.execute(
        """
        INSERT INTO live_jobs (
            live_name, channel_name, channel_id, video_id, audio_playlist_id, stream_key, start_at, end_at,
            duration_minutes, status, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            clean_live_name,
            clean_channel_name,
            selected_channel_id,
            selected_video_id,
            selected_audio_playlist_id,
            clean_stream_key,
            dt_to_str(start_value),
            dt_to_str(end_value),
            duration_value,
            status,
            now,
            now,
        ),
    )
    db.commit()
    return redirect("/live-jobs?message=Live%20job%20created")


@app.post("/jobs/{job_id}/start")
def start_live(job_id: int, db: sqlite3.Connection = Depends(get_db), _: None = Depends(require_admin)):
    ok, message = start_job(db, job_id)
    key = "message" if ok else "error"
    return redirect(f"/live-jobs?{urlencode({key: message})}")


@app.post("/jobs/{job_id}/stop")
def stop_live(job_id: int, db: sqlite3.Connection = Depends(get_db), _: None = Depends(require_admin)):
    ok, message = stop_job(db, job_id)
    key = "message" if ok else "error"
    return redirect(f"/live-jobs?{urlencode({key: message})}")


@app.post("/jobs/{job_id}/delete")
def delete_live_job(job_id: int, db: sqlite3.Connection = Depends(get_db), _: None = Depends(require_admin)):
    ok, message = archive_job(db, job_id, "deleted_from_live_jobs")
    key = "message" if ok else "error"
    return redirect(f"/live-jobs?{urlencode({key: message})}")

@app.post("/jobs/{job_id}/archive")
def archive_live_job(job_id: int, db: sqlite3.Connection = Depends(get_db), _: None = Depends(require_admin)):
    ok, message = archive_job(db, job_id)
    key = "message" if ok else "error"
    return redirect(f"/live-jobs?{urlencode({key: message})}")

@app.post("/jobs/archive-completed")
def archive_completed_jobs(db: sqlite3.Connection = Depends(get_db), _: None = Depends(require_admin)):
    now = dt_to_str(local_now())
    cursor = db.execute(
        """
        UPDATE live_jobs
        SET archived_at = COALESCE(archived_at, ?), updated_at = ?
        WHERE archived_at IS NULL AND status IN ('done', 'stopped', 'error')
        """,
        (now, now),
    )
    db.commit()
    return redirect(f"/live-jobs?{urlencode({'message': f'Archived {cursor.rowcount} completed jobs to History.'})}")

@app.post("/history/{job_id}/restore")
def restore_history_job(job_id: int, db: sqlite3.Connection = Depends(get_db), _: None = Depends(require_admin)):
    ok, message = duplicate_job_as_new(db, job_id)
    key = "message" if ok else "error"
    return redirect(f"/history?{urlencode({key: message})}")

@app.post("/history/{job_id}/delete")
def delete_history_job(job_id: int, db: sqlite3.Connection = Depends(get_db), _: None = Depends(require_admin)):
    job = get_job(db, job_id)
    if not job:
        return redirect(f"/history?{urlencode({'error': 'History record was not found.'})}")
    if job.get("status") == "running":
        return redirect(f"/history?{urlencode({'error': 'Running jobs cannot be deleted from History.'})}")
    db.execute("DELETE FROM live_jobs WHERE id = ?", (job_id,))
    db.commit()
    return redirect(f"/history?{urlencode({'message': 'History record deleted.'})}")


@app.post("/jobs/bulk")
def bulk_live_jobs(
    action: str = Form(...),
    job_ids: list[int] = Form(default=[]),
    db: sqlite3.Connection = Depends(get_db),
    _: None = Depends(require_admin),
):
    selected_ids = list(dict.fromkeys(job_ids))
    if not selected_ids:
        return redirect(f"/live-jobs?{urlencode({'error': 'Select at least one live job first.'})}")

    started = stopped = queued = deleted = archived = skipped = failed = 0
    for job_id in selected_ids:
        job = get_job(db, job_id)
        if not job:
            failed += 1
            continue

        if action == "start":
            if job.get("status") not in STARTABLE_STATUSES:
                skipped += 1
                continue
            ok, _ = start_job(db, job_id)
            if ok:
                started += 1
            else:
                failed += 1
        elif action == "stop":
            if job.get("status") != "running" or not job.get("pid"):
                skipped += 1
                continue
            ok, _ = stop_job(db, job_id)
            if ok:
                stopped += 1
            else:
                failed += 1
        elif action == "queue":
            if job.get("status") == "running":
                skipped += 1
                continue
            db.execute(
                """
                UPDATE live_jobs
                SET status = 'queued', pid = NULL, last_error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (dt_to_str(local_now()), job_id),
            )
            queued += 1
        elif action == "delete":
            if job.get("status") == "running":
                skipped += 1
                continue
            ok, _ = archive_job(db, job_id, "deleted_from_live_jobs")
            if not ok:
                failed += 1
                continue
            deleted += 1
        elif action == "archive":
            if job.get("status") not in FINAL_STATUSES or job.get("archived_at"):
                skipped += 1
                continue
            ok, _ = archive_job(db, job_id)
            if ok:
                archived += 1
            else:
                failed += 1
        else:
            return redirect(f"/live-jobs?{urlencode({'error': 'Unknown bulk action.'})}")

    db.commit()
    if action == "start":
        message = f"Bulk start summary: started {started}, skipped {skipped}, failed {failed}."
    elif action == "stop":
        message = f"Bulk stop summary: stopped {stopped}, skipped {skipped}, failed {failed}."
    elif action == "queue":
        message = f"Bulk queue summary: queued {queued}, skipped {skipped}, failed {failed}."
    elif action == "delete":
        message = f"Bulk delete summary: archived {deleted} to History, skipped {skipped}, failed {failed}."
    else:
        message = f"Bulk archive summary: archived {archived}, skipped {skipped}, failed {failed}."
    key = "error" if failed and not any([started, stopped, queued, deleted, archived]) else "message"
    return redirect(f"/live-jobs?{urlencode({key: message})}")


@app.get("/jobs/{job_id}/logs", response_class=HTMLResponse)
def job_logs(
    request: Request,
    job_id: int,
    db: sqlite3.Connection = Depends(get_db),
    _: None = Depends(require_admin),
):
    job = get_job(db, job_id)
    if not job:
        return redirect("/logs?error=Live%20job%20was%20not%20found")
    return templates.TemplateResponse(
        "logs.html",
        {
            "request": request,
            "job": job,
            "log_text": latest_log_text(job_id, job.get("stream_key")),
            "display_dt": display_dt,
            "mask_secret": mask_secret,
        },
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.head("/health")
def health_head() -> Response:
    return Response(status_code=200)
