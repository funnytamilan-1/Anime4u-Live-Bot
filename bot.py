"""
Production-Ready Telegram File-Storage & Backblaze B2 Media Processing Bot
Architecture: Dual-Mode Engine (Render Web Service Bot + MTProto Worker)

Supports Telegram Files up to 4 GiB (4,294,967,296 bytes) using MTProto.
"""

import os
import sys
import re
import json
import time
import signal
import enum
import traceback
import logging
import asyncio
import sqlite3
import shutil
import uuid
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List, Set, Tuple
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

# Load environment variables from .env if python-dotenv is available
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("B2MediaStorageBot")

# Optional Telethon import for MTProto large file downloading
try:
    import telethon
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from telethon.errors import FloodWaitError, FloodError, RPCError
    TELETHON_AVAILABLE = True
except ImportError:
    TELETHON_AVAILABLE = False
    telethon = None
    TelegramClient = None
    StringSession = None
    FloodWaitError = Exception
    FloodError = Exception
    RPCError = Exception
    logger.warning("Telethon library is not installed. MTProto large file downloading will be unavailable.")

# ============================================================
# 1. CONFIGURATION & ENVIRONMENT VARIABLES
# ============================================================

def _get_env(key: str, default: str) -> str:
    val = (os.getenv(key) or "").strip()
    if not val or val.startswith("YOUR_") or val in ["YOUR_TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN", "YOUR_API_ID", "YOUR_API_HASH"]:
        return default
    return val

BOT_TOKEN = _get_env("BOT_TOKEN", "8769661029:AAED5_SSFoU-Q_xQ_-p-x5FqzU7J9MZcIaE")
API_ID_RAW = _get_env("API_ID", "27806628")
API_HASH = _get_env("API_HASH", "25d88301e886b82826a525b7cf52e090")
TELEGRAM_SESSION_STRING = _get_env("TELEGRAM_SESSION_STRING", "")

API_ID: Optional[int] = None
if API_ID_RAW and API_ID_RAW.isdigit():
    API_ID = int(API_ID_RAW)

SERVICE_MODE = (os.getenv("SERVICE_MODE") or "all").strip().lower()  # 'render', 'worker', or 'all'

ADMIN_IDS_RAW = _get_env("ADMIN_IDS", "5192451273, 8525952693")
ADMIN_IDS: Set[int] = set()
if ADMIN_IDS_RAW:
    for item in ADMIN_IDS_RAW.split(","):
        cleaned = item.strip().lstrip("-")
        if cleaned.isdigit():
            try:
                ADMIN_IDS.add(int(item.strip()))
            except ValueError:
                pass

B2_APPLICATION_KEY_ID = _get_env("B2_APPLICATION_KEY_ID", "0054467fa469dc20000000002")
B2_APPLICATION_KEY = _get_env("B2_APPLICATION_KEY", "K005ACvE5pP0RYQr4cplDYDSE96uMtA")
B2_BUCKET_NAME = _get_env("B2_BUCKET_NAME", "anime4u-videos")
B2_PUBLIC_BASE_URL = _get_env("B2_PUBLIC_BASE_URL", "").rstrip("/")

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

DB_PATH = os.getenv("DATABASE_PATH", "./storage.db")
TEMP_DIR = Path(os.getenv("TEMP_DIR", "./downloads"))
TEMP_DIR.mkdir(parents=True, exist_ok=True)

MAX_FILE_SIZE_BYTES = int(os.getenv("MAX_FILE_SIZE_BYTES") or os.getenv("MAX_FILE_SIZE") or (4 * 1024 * 1024 * 1024))  # 4 GiB = 4,294,967,296 bytes
MAX_CONCURRENT_UPLOADS = int(os.getenv("MAX_CONCURRENT_UPLOADS", 2))
MAX_CONCURRENT_FFMPEG = int(os.getenv("MAX_TRANSCODE_JOBS") or os.getenv("MAX_CONCURRENT_FFMPEG") or 1)
PORT = int(os.getenv("PORT", 3000))
PAGE_SIZE = int(os.getenv("PAGE_SIZE", 10))

# FFmpeg Encoding & Performance Configuration
VALID_FFMPEG_PRESETS = {"ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"}
_raw_preset = (os.getenv("FFMPEG_PRESET") or "veryfast").strip().lower()
FFMPEG_PRESET = _raw_preset if _raw_preset in VALID_FFMPEG_PRESETS else "veryfast"

try:
    FFMPEG_CRF = int(os.getenv("FFMPEG_CRF", "23"))
    if not (0 <= FFMPEG_CRF <= 51):
        FFMPEG_CRF = 23
except ValueError:
    FFMPEG_CRF = 23

try:
    FFMPEG_THREADS = int(os.getenv("FFMPEG_THREADS", "0"))
    if FFMPEG_THREADS < 0:
        FFMPEG_THREADS = 0
except ValueError:
    FFMPEG_THREADS = 0

try:
    FFMPEG_STALL_TIMEOUT = int(os.getenv("FFMPEG_STALL_TIMEOUT", "120"))
    if FFMPEG_STALL_TIMEOUT <= 0:
        FFMPEG_STALL_TIMEOUT = 120
except ValueError:
    FFMPEG_STALL_TIMEOUT = 120

# Semaphores for task rate limiting
UPLOAD_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_UPLOADS)
FFMPEG_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_FFMPEG)

# Import Telegram dependencies
import telegram
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# Global Telethon Client Reference & FloodWait Tracker
telethon_client: Optional[Any] = None
telethon_flood_wait_until: float = 0.0

# Optional Supabase Database integration
supabase_client = None
if SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY:
    try:
        from supabase import create_client
        supabase_client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
        logger.info("Connected to Supabase database backend")
    except Exception as e:
        logger.warning(f"Supabase connection failed: {e}. Falling back to SQLite.")

# ============================================================
# 2. DATABASE ENGINE & JOB QUEUE STATE MACHINE
# ============================================================

def get_sqlite_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_sqlite_conn()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS file_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_name TEXT NOT NULL,
            original_file_name TEXT NOT NULL,
            folder TEXT NOT NULL,
            b2_bucket TEXT NOT NULL,
            b2_path TEXT NOT NULL,
            b2_file_id TEXT,
            file_size INTEGER NOT NULL,
            mime_type TEXT,
            media_type TEXT,
            video_codec TEXT,
            audio_codec TEXT,
            width INTEGER,
            height INTEGER,
            duration REAL,
            processing_mode TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'complete',
            b2_url TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            status_message_id INTEGER,
            tg_file_id TEXT,
            tg_file_unique_id TEXT,
            original_filename TEXT NOT NULL,
            final_filename TEXT,
            folder TEXT NOT NULL,
            source_file_size INTEGER NOT NULL,
            final_file_size INTEGER,
            mime_type TEXT,
            media_type TEXT,
            video_codec TEXT,
            audio_codec TEXT,
            width INTEGER,
            height INTEGER,
            duration REAL,
            processing_mode TEXT,
            b2_bucket TEXT,
            b2_path TEXT,
            b2_file_id TEXT,
            b2_url TEXT,
            status TEXT NOT NULL DEFAULT 'QUEUED',
            progress_percent INTEGER DEFAULT 0,
            progress_stage TEXT DEFAULT 'QUEUED',
            error_stage TEXT,
            error_message TEXT,
            worker_node TEXT,
            claimed_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)

    # Dynamic column migrations if table already existed without new columns
    cursor.execute("PRAGMA table_info(jobs)")
    cols = {row[1] for row in cursor.fetchall()}
    if "worker_node" not in cols:
        try:
            cursor.execute("ALTER TABLE jobs ADD COLUMN worker_node TEXT")
        except Exception:
            pass
    if "claimed_at" not in cols:
        try:
            cursor.execute("ALTER TABLE jobs ADD COLUMN claimed_at TEXT")
        except Exception:
            pass

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            details TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_file_folder ON file_records(folder)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_file_b2_path ON file_records(b2_path)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_user_id ON jobs(user_id)")

    conn.commit()
    conn.close()
    logger.info("SQLite database & job queue schema initialized successfully.")

def recover_stale_jobs(stale_timeout_sec: int = 600) -> int:
    """Reset jobs that were claimed by a previous dead worker process back to QUEUED."""
    conn = get_sqlite_conn()
    cursor = conn.cursor()
    try:
        now_ts = time.time()
        cursor.execute("SELECT job_id, status, updated_at FROM jobs WHERE status IN ('CLAIMED', 'DOWNLOADING', 'INSPECTING', 'REMUXING', 'TRANSCODING', 'UPLOADING', 'VERIFYING')")
        rows = cursor.fetchall()
        recovered = 0
        now_iso = datetime.now(timezone.utc).isoformat()
        for r in rows:
            jid = r["job_id"]
            upd = r["updated_at"]
            try:
                dt = datetime.fromisoformat(upd.replace("Z", "+00:00"))
                if now_ts - dt.timestamp() > stale_timeout_sec:
                    cursor.execute("UPDATE jobs SET status = 'QUEUED', progress_stage = 'QUEUED', progress_percent = 0, updated_at = ? WHERE job_id = ?", (now_iso, jid))
                    recovered += 1
                    logger.info(f"[WORKER] Recovered stale job {jid} (previous status: {r['status']}) back to QUEUED.")
            except Exception:
                pass
        conn.commit()
        conn.close()
        return recovered
    except Exception as e:
        logger.warning(f"[WORKER] Stale job recovery note: {e}")
        try:
            conn.close()
        except Exception:
            pass
        return 0

def create_job(job_data: Dict[str, Any]) -> str:
    job_id = f"job_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    now_str = datetime.utcnow().isoformat()

    conn = get_sqlite_conn()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO jobs (
            job_id, user_id, chat_id, message_id, status_message_id,
            tg_file_id, tg_file_unique_id, original_filename, final_filename,
            folder, source_file_size, mime_type, media_type, b2_bucket,
            status, progress_percent, progress_stage, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        job_id,
        job_data["user_id"],
        job_data["chat_id"],
        job_data["message_id"],
        job_data.get("status_message_id"),
        job_data.get("tg_file_id", "N/A"),
        job_data.get("tg_file_unique_id", "N/A"),
        job_data["original_filename"],
        job_data.get("final_filename", job_data["original_filename"]),
        job_data["folder"],
        job_data["source_file_size"],
        job_data.get("mime_type", "application/octet-stream"),
        job_data.get("media_type", "document"),
        B2_BUCKET_NAME,
        job_data.get("status", "QUEUED"),
        0,
        "QUEUED",
        now_str,
        now_str
    ))
    conn.commit()
    conn.close()
    logger.info(f"Created job record: {job_id} for file {job_data['original_filename']}")
    return job_id

def update_job(job_id: str, updates: Dict[str, Any]):
    now_str = datetime.utcnow().isoformat()
    updates["updated_at"] = now_str

    conn = get_sqlite_conn()
    cursor = conn.cursor()

    set_clauses = []
    params = []
    for k, v in updates.items():
        set_clauses.append(f"{k} = ?")
        params.append(v)

    params.append(job_id)
    query = f"UPDATE jobs SET {', '.join(set_clauses)} WHERE job_id = ?"
    cursor.execute(query, tuple(params))
    conn.commit()
    conn.close()

def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    conn = get_sqlite_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

def claim_next_queued_job(worker_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Atomically fetch and claim the next QUEUED job by updating its status to CLAIMED."""
    if not worker_id:
        worker_id = f"{os.uname().nodename}:{os.getpid()}"
    conn = get_sqlite_conn()
    cursor = conn.cursor()
    try:
        cursor.execute("BEGIN IMMEDIATE")
        cursor.execute("SELECT * FROM jobs WHERE status = 'QUEUED' ORDER BY created_at ASC LIMIT 1")
        row = cursor.fetchone()
        if not row:
            conn.commit()
            conn.close()
            return None
        job_data = dict(row)
        now_iso = datetime.now(timezone.utc).isoformat()
        cursor.execute(
            "UPDATE jobs SET status = 'CLAIMED', progress_stage = 'CLAIMED', progress_percent = 0, worker_node = ?, claimed_at = ?, updated_at = ? WHERE job_id = ? AND status = 'QUEUED'",
            (worker_id, now_iso, now_iso, job_data["job_id"])
        )
        if cursor.rowcount == 0:
            conn.commit()
            conn.close()
            return None
        conn.commit()
        conn.close()
        job_data["status"] = "CLAIMED"
        job_data["progress_stage"] = "CLAIMED"
        job_data["progress_percent"] = 0
        job_data["worker_node"] = worker_id
        job_data["claimed_at"] = now_iso
        job_data["updated_at"] = now_iso
        return job_data
    except Exception as e:
        logger.error(f"Error claiming next queued job: {e}")
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass
        return None

def get_next_queued_job() -> Optional[Dict[str, Any]]:
    """Retrieve the next QUEUED job without mutating its state."""
    conn = get_sqlite_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM jobs WHERE status = 'QUEUED' ORDER BY created_at ASC LIMIT 1")
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

def get_recent_jobs(limit: int = 10) -> List[Dict[str, Any]]:
    conn = get_sqlite_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,))
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def log_audit(admin_id: int, action: str, details: str):
    try:
        conn = get_sqlite_conn()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO audit_logs (admin_id, action, details, created_at) VALUES (?, ?, ?, ?)",
            (admin_id, action, details, datetime.utcnow().isoformat())
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Failed to record audit log: {e}")

def get_recent_audit_logs(limit: int = 15) -> List[Dict[str, Any]]:
    conn = get_sqlite_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM audit_logs ORDER BY created_at DESC LIMIT ?", (limit,))
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_db_stats() -> Dict[str, Any]:
    conn = get_sqlite_conn()
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*), COALESCE(SUM(file_size), 0) FROM file_records")
    total_files, total_bytes = cursor.fetchone()

    cursor.execute("SELECT COUNT(DISTINCT folder) FROM file_records")
    total_folders = cursor.fetchone()[0]

    conn.close()

    return {
        "total_files": total_files,
        "total_folders": total_folders,
        "total_bytes": total_bytes,
        "readable_bytes": format_size(total_bytes)
    }

def insert_file_record(record: Dict[str, Any]) -> Optional[int]:
    now_str = datetime.utcnow().isoformat()
    if supabase_client:
        try:
            res = supabase_client.table("streams").insert({
                "file_name": record["file_name"],
                "original_file_name": record.get("original_file_name", record["file_name"]),
                "folder": record["folder"],
                "b2_bucket": record["b2_bucket"],
                "b2_path": record["b2_path"],
                "b2_file_id": record.get("b2_file_id"),
                "file_size": record["file_size"],
                "mime_type": record.get("mime_type", ""),
                "media_type": record.get("media_type", "document"),
                "video_codec": record.get("video_codec"),
                "audio_codec": record.get("audio_codec"),
                "width": record.get("width"),
                "height": record.get("height"),
                "duration": record.get("duration"),
                "processing_mode": record.get("processing_mode", "passthrough"),
                "status": record.get("status", "complete"),
                "b2_url": record.get("b2_url"),
                "created_at": record.get("created_at", now_str),
                "updated_at": now_str
            }).execute()
            return res.data[0]["id"] if res.data else None
        except Exception as e:
            logger.error(f"Supabase insert error: {e}")
            return None

    conn = get_sqlite_conn()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO file_records (
                file_name, original_file_name, folder, b2_bucket, b2_path, b2_file_id,
                file_size, mime_type, media_type, video_codec, audio_codec,
                width, height, duration, processing_mode, status, b2_url,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            record["file_name"],
            record.get("original_file_name", record["file_name"]),
            record["folder"],
            record["b2_bucket"],
            record["b2_path"],
            record.get("b2_file_id"),
            record["file_size"],
            record.get("mime_type", ""),
            record.get("media_type", "document"),
            record.get("video_codec"),
            record.get("audio_codec"),
            record.get("width"),
            record.get("height"),
            record.get("duration"),
            record.get("processing_mode", "passthrough"),
            record.get("status", "complete"),
            record.get("b2_url"),
            record.get("created_at", now_str),
            now_str
        ))
        conn.commit()
        return cursor.lastrowid
    except Exception as e:
        logger.error(f"SQLite database insert error: {e}")
        return None
    finally:
        conn.close()

# ============================================================
# 3. REAL BACKBLAZE B2 STORAGE ENGINE
# ============================================================

class B2StorageEngine:
    def __init__(self):
        self.key_id = B2_APPLICATION_KEY_ID
        self.application_key = B2_APPLICATION_KEY
        self.bucket_name = B2_BUCKET_NAME
        self.public_base_url = B2_PUBLIC_BASE_URL
        self._bucket = None
        self._b2_api = None
        self._lock = threading.Lock()

    def is_configured(self) -> bool:
        return bool(self.key_id and self.application_key and self.bucket_name)

    def _get_bucket(self, force_reauth: bool = False):
        with self._lock:
            if self._bucket and not force_reauth:
                return self._bucket
            if not self.is_configured():
                raise ValueError("B2 credentials (B2_APPLICATION_KEY_ID, B2_APPLICATION_KEY, B2_BUCKET_NAME) are missing.")

            try:
                from b2sdk.v2 import InMemoryAccountInfo, B2Api
                info = InMemoryAccountInfo()
                self._b2_api = B2Api(info)
                self._b2_api.authorize_account("production", self.key_id, self.application_key)
                self._bucket = self._b2_api.get_bucket_by_name(self.bucket_name)
                logger.info(f"Successfully connected to Backblaze B2 bucket: {self.bucket_name}")
                return self._bucket
            except Exception as e:
                self._bucket = None
                self._b2_api = None
                logger.error(f"Failed to authenticate with Backblaze B2: {e}")
                raise

    def check_health(self) -> Tuple[bool, str]:
        if not self.is_configured():
            return False, "B2 credentials missing in environment variables."
        try:
            bucket = self._get_bucket(force_reauth=True)
            generator = bucket.ls(fetch_count=1)
            next(generator, None)
            return True, f"B2 Bucket '{self.bucket_name}' authenticated & verified."
        except Exception as e:
            self._bucket = None
            self._b2_api = None
            return False, f"B2 Health Check Error: {e}"

    def discover_folders(self) -> List[str]:
        try:
            bucket = self._get_bucket()
            folders_set: Set[str] = set()

            for file_version, _ in bucket.ls(recursive=True):
                name = file_version.file_name
                if "/" in name:
                    parts = name.split("/")
                    accum = ""
                    for part in parts[:-1]:
                        if part:
                            accum = f"{accum}{part}/"
                            folders_set.add(accum)

            return sorted(list(folders_set))
        except Exception as e:
            logger.error(f"Error discovering real B2 folders: {e}")
            return []

    def file_exists(self, b2_path: str) -> bool:
        try:
            bucket = self._get_bucket()
            if hasattr(bucket, "get_file_info_by_name"):
                info = bucket.get_file_info_by_name(b2_path)
            elif self._b2_api and hasattr(self._b2_api, "get_file_info_by_name"):
                info = self._b2_api.get_file_info_by_name(self.bucket_name, b2_path)
            else:
                for file_version, _ in bucket.ls(folder_to_list=b2_path, recursive=False):
                    if file_version.file_name == b2_path:
                        return True
                return False
            return info is not None
        except Exception:
            return False

    def upload_file(self, local_file_path: Path, b2_path: str) -> Dict[str, Any]:
        bucket = self._get_bucket()
        str_path = str(local_file_path)

        if not local_file_path.exists():
            raise FileNotFoundError(f"Local file not found for upload: {str_path}")

        logger.info(f"Uploading file to B2: {str_path} -> {b2_path} ({format_size(local_file_path.stat().st_size)})")

        file_version = bucket.upload_local_file(
            local_file=str_path,
            file_name=b2_path
        )

        b2_file_id = file_version.id_
        url = f"{self.public_base_url}/{b2_path}" if self.public_base_url else None

        return {
            "b2_file_id": b2_file_id,
            "b2_path": b2_path,
            "b2_url": url,
            "file_size": file_version.size,
            "b2_bucket": self.bucket_name
        }

    def search_files(self, query: str) -> List[Dict[str, Any]]:
        try:
            bucket = self._get_bucket()
            q_lower = query.lower()
            results = []

            for file_version, _ in bucket.ls(recursive=True):
                if q_lower in file_version.file_name.lower():
                    url = f"{self.public_base_url}/{file_version.file_name}" if self.public_base_url else None
                    results.append({
                        "b2_file_id": file_version.id_,
                        "b2_path": file_version.file_name,
                        "file_name": Path(file_version.file_name).name,
                        "file_size": file_version.size,
                        "b2_url": url
                    })

            return results
        except Exception as e:
            logger.error(f"Error searching B2 files: {e}")
            return []

b2_storage = B2StorageEngine()

def check_disk_space(required_bytes: int = 500 * 1024 * 1024) -> Tuple[bool, str]:
    """Check available disk space at TEMP_DIR location against required bytes using real OS disk statistics."""
    try:
        stat = shutil.disk_usage(TEMP_DIR)
        free_bytes = stat.free
        if free_bytes < required_bytes:
            return False, f"Insufficient disk space: {format_size(free_bytes)} free, but {format_size(required_bytes)} required."
        return True, f"Disk space OK: {format_size(free_bytes)} free."
    except Exception as e:
        logger.warning(f"Disk space check exception: {e}")
        return True, f"Disk space check bypassed: {e}"

# ============================================================
# 4. FFMPEG & MEDIA INSPECTION ENGINE
# ============================================================

def check_ffmpeg_installed() -> Tuple[bool, bool]:
    ffmpeg_bin = shutil.which("ffmpeg") is not None
    ffprobe_bin = shutil.which("ffprobe") is not None
    return ffmpeg_bin, ffprobe_bin

async def probe_media(file_path: Path) -> Dict[str, Any]:
    ffmpeg_ok, ffprobe_ok = check_ffmpeg_installed()
    if not ffprobe_ok:
        raise RuntimeError("FFprobe binary is not installed on system.")

    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(file_path)
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        err_msg = stderr.decode("utf-8", errors="ignore").strip()
        raise RuntimeError(f"FFprobe failed with returncode {proc.returncode}: {err_msg}")

    data = json.loads(stdout.decode("utf-8", errors="ignore"))
    format_info = data.get("format", {})
    streams = data.get("streams", [])

    video_stream = None
    audio_stream = None
    subtitle_streams = []

    for s in streams:
        codec_type = s.get("codec_type")
        if codec_type == "video" and not video_stream:
            video_stream = s
        elif codec_type == "audio" and not audio_stream:
            audio_stream = s
        elif codec_type == "subtitle":
            subtitle_streams.append(s)

    duration = None
    if "duration" in format_info:
        try:
            duration = float(format_info["duration"])
        except ValueError:
            pass

    return {
        "is_video": video_stream is not None,
        "format_name": format_info.get("format_name", ""),
        "duration": duration,
        "video_stream": video_stream,
        "audio_stream": audio_stream,
        "subtitle_count": len(subtitle_streams),
        "video_codec": video_stream.get("codec_name") if video_stream else None,
        "pix_fmt": video_stream.get("pix_fmt") if video_stream else None,
        "width": video_stream.get("width") if video_stream else None,
        "height": video_stream.get("height") if video_stream else None,
        "audio_codec": audio_stream.get("codec_name") if audio_stream else None,
    }

async def process_video_for_web(
    input_path: Path,
    output_path: Path,
    media_info: Dict[str, Any],
    progress_callback: Optional[Any] = None
) -> Tuple[bool, str, str]:
    ffmpeg_ok, _ = check_ffmpeg_installed()
    if not ffmpeg_ok:
        raise RuntimeError("FFmpeg binary is not installed on system.")

    v_codec = (media_info.get("video_codec") or "").lower()
    a_codec = (media_info.get("audio_codec") or "").lower()
    pix_fmt = (media_info.get("pix_fmt") or "").lower()
    total_duration = media_info.get("duration") or 0.0

    is_h264_compatible = v_codec in ["h264", "avc1"] and pix_fmt in ["yuv420p", "yuvj420p", ""]
    is_aac_compatible = a_codec in ["aac", "mp4a-40-2"]

    if is_h264_compatible:
        mode = "remux_fastpath"
        cmd = [
            "ffmpeg", "-hide_banner", "-y",
            "-progress", "pipe:1",
            "-nostats",
            "-i", str(input_path),
            "-map", "0:v:0",
            "-map", "0:a?",
            "-c:v", "copy",
            "-c:a", "copy" if is_aac_compatible else "aac",
        ]
        if not is_aac_compatible and a_codec:
            cmd.extend(["-b:a", "128k"])
        cmd.extend(["-movflags", "+faststart", str(output_path)])
    else:
        mode = "transcode_h264"
        cmd = [
            "ffmpeg", "-hide_banner", "-y",
            "-progress", "pipe:1",
            "-nostats",
            "-i", str(input_path),
            "-map", "0:v:0",
            "-map", "0:a?",
            "-c:v", "libx264",
            "-preset", FFMPEG_PRESET,
            "-crf", str(FFMPEG_CRF),
            "-pix_fmt", "yuv420p",
            "-threads", str(FFMPEG_THREADS),
            "-c:a", "copy" if is_aac_compatible else "aac",
        ]
        if not is_aac_compatible and a_codec:
            cmd.extend(["-b:a", "128k"])
        cmd.extend(["-movflags", "+faststart", str(output_path)])

    logger.info("[FFMPEG] Starting")
    logger.info(f"[FFMPEG] Input: {input_path.name}")
    logger.info(f"[FFMPEG] Input codec: {v_codec}")
    logger.info(f"[FFMPEG] Output codec: {'copy' if is_h264_compatible else 'h264'}")
    logger.info(f"[FFMPEG] Duration: {total_duration}s")
    logger.info(f"[FFMPEG] Preset: {FFMPEG_PRESET if mode == 'transcode_h264' else 'copy'}")
    logger.info(f"[FFMPEG] CRF: {FFMPEG_CRF if mode == 'transcode_h264' else 'N/A'}")
    logger.info(f"[FFMPEG] Threads: {'auto (0)' if FFMPEG_THREADS == 0 else FFMPEG_THREADS}")
    logger.info(f"[FFMPEG] Output: {output_path.resolve()}")
    logger.info(f"[FFMPEG] Command: {' '.join(cmd)}")

    async with FFMPEG_SEMAPHORE:
        t_enc_start = time.time()
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        stderr_chunks = []
        async def read_stderr():
            while True:
                chunk = await proc.stderr.read(4096)
                if not chunk:
                    break
                stderr_chunks.append(chunk)

        stderr_task = asyncio.create_task(read_stderr())

        out_time_sec = 0.0
        speed_str = "0x"
        fps_str = "0"
        last_log_time = time.time()
        last_progress_time = time.time()
        last_file_size = 0
        main_loop = asyncio.get_running_loop()

        def format_sec(sec: float) -> str:
            if not sec or sec <= 0:
                return "00:00:00"
            m, s = divmod(int(sec), 60)
            h, m = divmod(m, 60)
            return f"{h:02d}:{m:02d}:{s:02d}"

        while True:
            now = time.time()
            # Stall detection: timeout if no progress lines or file growth for FFMPEG_STALL_TIMEOUT
            if now - last_progress_time > FFMPEG_STALL_TIMEOUT:
                curr_size = output_path.stat().st_size if output_path.exists() else 0
                if curr_size > last_file_size:
                    last_file_size = curr_size
                    last_progress_time = now  # output file is actively being written
                else:
                    logger.error(f"[FFMPEG] Error: Process stalled (> {FFMPEG_STALL_TIMEOUT}s without progress). Terminating.")
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                    break

            try:
                line_bytes = await asyncio.wait_for(proc.stdout.readline(), timeout=5.0)
            except asyncio.TimeoutError:
                if proc.returncode is not None:
                    break
                continue

            if not line_bytes:
                break

            line = line_bytes.decode("utf-8", errors="ignore").strip()
            if "=" in line:
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()

                if key in ["out_time_us", "out_time_ms"]:
                    try:
                        out_time_sec = float(value) / 1000000.0
                    except ValueError:
                        pass
                elif key == "out_time":
                    try:
                        parts = value.split(":")
                        if len(parts) == 3:
                            h, m, s = float(parts[0]), float(parts[1]), float(parts[2])
                            out_time_sec = h * 3600 + m * 60 + s
                    except ValueError:
                        pass
                elif key == "speed":
                    speed_str = value
                elif key == "fps":
                    fps_str = value
                elif key == "progress":
                    last_progress_time = now
                    pct = 0
                    if total_duration > 0:
                        pct = int(min(100.0, max(0.0, (out_time_sec / total_duration) * 100.0)))

                    if now - last_log_time >= 2.0:
                        last_log_time = now
                        logger.info(f"[FFMPEG] progress={pct}% out_time={format_sec(out_time_sec)} speed={speed_str} fps={fps_str}")

                    if progress_callback:
                        asyncio.run_coroutine_threadsafe(
                            progress_callback(pct, out_time_sec, speed_str, fps_str, total_duration, mode),
                            loop=main_loop
                        )

        await proc.wait()
        await stderr_task

        stderr_output = b"".join(stderr_chunks).decode("utf-8", errors="ignore")
        total_enc_time = round(time.time() - t_enc_start, 2)

        if proc.returncode != 0:
            err_log = stderr_output[-1000:]
            logger.error(f"[FFMPEG] Exit code: {proc.returncode}")
            logger.error(f"[FFMPEG] Error: {err_log}")
            return False, mode, f"FFmpeg failed with exit code {proc.returncode}: {err_log}"

        # Performance Logging
        avg_speed = f"{(total_duration / total_enc_time):.2f}x" if total_duration > 0 and total_enc_time > 0 else speed_str
        in_size = input_path.stat().st_size if input_path.exists() else 0
        out_size = output_path.stat().st_size if output_path.exists() else 0

        logger.info(f"[FFMPEG] Input duration: {int(total_duration)}s")
        logger.info(f"[FFMPEG] Input size: {format_size(in_size)}")
        logger.info(f"[FFMPEG] Output size: {format_size(out_size)}")
        logger.info(f"[FFMPEG] Preset: {FFMPEG_PRESET if mode == 'transcode_h264' else 'copy'}")
        logger.info(f"[FFMPEG] CRF: {FFMPEG_CRF if mode == 'transcode_h264' else 'N/A'}")
        logger.info(f"[FFMPEG] Threads: {'auto (0)' if FFMPEG_THREADS == 0 else FFMPEG_THREADS}")
        logger.info(f"[FFMPEG] Speed: {avg_speed}")
        logger.info(f"[FFMPEG] FPS: {fps_str}")
        logger.info(f"[FFMPEG] Completed in: {total_enc_time}s")

        # Output Validation
        if not output_path.exists() or output_path.stat().st_size == 0:
            logger.error("[FFMPEG] Output validation failed: File missing or 0 bytes.")
            return False, mode, "FFmpeg output file missing or 0 bytes."

        try:
            out_probe = await probe_media(output_path)
            out_vcodec = (out_probe.get("video_codec") or "").lower()
            out_format = (out_probe.get("format_name") or "").lower()
            out_dur = out_probe.get("duration") or 0.0
            out_pix = (out_probe.get("pix_fmt") or "").lower()
            out_acodec = (out_probe.get("audio_codec") or "").lower()

            if out_vcodec not in ["h264", "avc1"]:
                logger.error(f"[FFMPEG] Validation failed: Video codec is '{out_vcodec}', expected 'h264'.")
                return False, mode, f"Validation failed: Output video codec is '{out_vcodec}'"

            if "mp4" not in out_format and "mov" not in out_format:
                logger.error(f"[FFMPEG] Validation failed: Container is '{out_format}', expected 'mp4'.")
                return False, mode, f"Validation failed: Container format is '{out_format}'"

            if out_pix not in ["yuv420p", "yuvj420p"]:
                logger.warning(f"[FFMPEG] Validation warning: Pixel format is '{out_pix}' (expected yuv420p).")

            if a_codec and not out_acodec:
                logger.error("[FFMPEG] Validation failed: Audio stream missing in output.")
                return False, mode, "Validation failed: Audio stream missing in output"

            logger.info(f"[FFMPEG] Output Validation PASS: Size={format_size(out_size)}, Codec={out_vcodec}, PixFmt={out_pix}, Format={out_format}, Duration={out_dur}s")

        except Exception as ve:
            logger.error(f"[FFMPEG] Output validation error: {ve}")
            return False, mode, f"Output validation probe failed: {ve}"

        return True, mode, "Success"

# ============================================================
# 5. MTPROTO TELETHON CLIENT & DOWNLOAD ENGINE
# ============================================================

async def get_telethon_client() -> Optional[Any]:
    global telethon_client, telethon_flood_wait_until
    if not TELETHON_AVAILABLE:
        return None

    if 'worker_lifecycle' in globals() and worker_lifecycle.is_shutting_down():
        logger.info("[MTPROTO] Shutdown in progress; skipping Telethon connection.")
        return None

    if telethon_client and telethon_client.is_connected():
        return telethon_client

    if not API_ID or not API_HASH:
        logger.warning("[MTPROTO] API_ID or API_HASH missing. Telethon MTProto client cannot start.")
        return None

    now = time.time()
    if now < telethon_flood_wait_until:
        remaining_sec = int(telethon_flood_wait_until - now)
        logger.warning(
            f"[MTPROTO] Telegram FloodWait cooldown active ({remaining_sec}s remaining). Skipping connection to respect rate limits."
        )
        return None

    try:
        if TELEGRAM_SESSION_STRING:
            logger.info("[MTPROTO] Initializing persistent Telethon client with TELEGRAM_SESSION_STRING...")
            session = StringSession(TELEGRAM_SESSION_STRING)
            telethon_client = TelegramClient(session, API_ID, API_HASH)
            await telethon_client.connect()
            if not await telethon_client.is_user_authorized():
                logger.error("[MTPROTO] TELEGRAM_SESSION_STRING is invalid or not authorized.")
                telethon_client = None
                return None
        else:
            session_file = str(Path(DB_PATH).parent / "telethon_worker")
            logger.info(f"[MTPROTO] Initializing persistent Telethon client with disk session ({session_file}.session)...")
            telethon_client = TelegramClient(session_file, API_ID, API_HASH)
            await telethon_client.connect()
            if not await telethon_client.is_user_authorized():
                logger.info("[MTPROTO] Authorizing Telethon client with Bot Token...")
                await telethon_client.start(bot_token=BOT_TOKEN)
            else:
                logger.info("[MTPROTO] Existing disk session loaded & authorized (no new authorization request needed).")

        if 'worker_lifecycle' in globals() and worker_lifecycle.is_shutting_down():
            logger.info("[MTPROTO] Shutdown requested during Telethon startup; disconnecting immediately.")
            try:
                if telethon_client.is_connected():
                    await telethon_client.disconnect()
            except Exception:
                pass
            telethon_client = None
            return None

        logger.info("[MTPROTO] Persistent Telethon MTProto client connected & authorized successfully!")
        return telethon_client
    except FloodWaitError as fwe:
        telethon_flood_wait_until = time.time() + fwe.seconds
        logger.warning(
            f"[MTPROTO] Telegram FloodWaitError: Telegram API rate limit requires waiting {fwe.seconds}s before authorizing a new bot MTProto session "
            f"(caused by {getattr(fwe, 'request', 'ImportBotAuthorizationRequest')}). "
            f"Worker will continue running in fallback mode until cooldown expires. "
            f"Tip: Set TELEGRAM_SESSION_STRING in environment variables for instant persistent authorization."
        )
        if telethon_client:
            try:
                if telethon_client.is_connected():
                    await telethon_client.disconnect()
            except Exception:
                pass
        telethon_client = None
        return None
    except Exception as e:
        err_msg = str(e)
        if "wait of" in err_msg.lower() or "flood" in err_msg.lower():
            import re
            match = re.search(r"(\d+)\s+seconds", err_msg)
            sec = int(match.group(1)) if match else 300
            telethon_flood_wait_until = time.time() + sec
            logger.warning(
                f"[MTPROTO] Telegram authorization rate limit: {err_msg}. "
                f"Worker will continue running in fallback mode for {sec}s. "
                f"Tip: Set TELEGRAM_SESSION_STRING in environment variables to bypass bot login rate limits."
            )
        else:
            logger.error(f"[MTPROTO] Failed to initialize persistent Telethon client: {e}")
        if telethon_client:
            try:
                if telethon_client.is_connected():
                    await telethon_client.disconnect()
            except Exception:
                pass
        telethon_client = None
        return None

async def download_telegram_file_mtproto(
    chat_id: int,
    message_id: int,
    output_path: Path,
    progress_callback=None
) -> bool:
    client = await get_telethon_client()
    if not client:
        now = time.time()
        if now < telethon_flood_wait_until:
            rem = int(telethon_flood_wait_until - now)
            raise RuntimeError(
                f"MTProto client rate limited by Telegram ({rem}s cooldown remaining). "
                f"Set TELEGRAM_SESSION_STRING in environment variables for instant persistent authentication."
            )
        raise RuntimeError("MTProto client is not configured or unavailable. Set API_ID and API_HASH.")

    logger.info(f"[WORKER] Download starting for chat_id={chat_id}, message_id={message_id}")
    msg = await client.get_messages(chat_id, ids=message_id)
    if not msg or not msg.media:
        raise RuntimeError(f"Telegram message {message_id} in chat {chat_id} not found or contains no media via MTProto.")

    dc_id = getattr(msg.media.document, "dc_id", None) if hasattr(msg.media, "document") else getattr(msg.media, "dc_id", "Unknown")
    expected_size = getattr(msg.media.document, "size", 0) if hasattr(msg.media, "document") else getattr(msg.media, "size", 0)

    logger.info(f"[WORKER] Telegram DC resolved: DC {dc_id}")
    logger.info(f"[WORKER] Download destination: {output_path.resolve()}")
    logger.info(f"[WORKER] Expected size: {expected_size} bytes ({format_size(expected_size)})")

    start_time = time.time()
    last_update_time = [0.0]
    main_loop = asyncio.get_running_loop()

    def raw_progress_cb(current: int, total: int):
        now = time.time()
        if now - last_update_time[0] >= 1.5 or current == total:
            last_update_time[0] = now
            elapsed = now - start_time
            speed = current / elapsed if elapsed > 0 else 0
            remaining = total - current
            eta_sec = remaining / speed if speed > 0 else 0
            if progress_callback:
                asyncio.run_coroutine_threadsafe(
                    progress_callback(current, total, speed, eta_sec),
                    loop=main_loop
                )

    max_attempts = 3
    attempt = 0
    success = False

    while attempt < max_attempts and not success:
        attempt += 1
        try:
            logger.info(f"[WORKER] Download attempt {attempt}/{max_attempts} starting...")
            await client.download_media(
                msg,
                file=str(output_path),
                progress_callback=raw_progress_cb if progress_callback else None
            )

            if output_path.exists() and output_path.stat().st_size > 0:
                actual_size = output_path.stat().st_size
                logger.info("[WORKER] Download completed")
                logger.info(f"[WORKER] Expected size: {expected_size} bytes")
                logger.info(f"[WORKER] Downloaded size: {actual_size} bytes")

                if expected_size > 0 and actual_size != expected_size:
                    logger.error("[WORKER] Verification: FAIL (Size mismatch)")
                    raise RuntimeError(f"Downloaded file size mismatch: Expected {expected_size} bytes, got {actual_size} bytes")

                logger.info("[WORKER] Verification: PASS")
                success = True
                break
        except Exception as e:
            logger.warning(f"[WORKER] Download attempt {attempt} failed: {e}")
            if attempt < max_attempts:
                await asyncio.sleep(2.0)
                client = await get_telethon_client()

    return success

async def setup_telethon_session_cli():
    """Interactive setup for generating TELEGRAM_SESSION_STRING."""
    if not TELETHON_AVAILABLE:
        print("Error: Telethon package is required. Run 'pip install telethon'.")
        return

    print("=== Telegram MTProto StringSession Setup ===")
    api_id_inp = input("Enter API_ID: ").strip()
    api_hash_inp = input("Enter API_HASH: ").strip()

    if not api_id_inp or not api_hash_inp:
        print("API_ID and API_HASH are required.")
        return

    client = TelegramClient(StringSession(), int(api_id_inp), api_hash_inp)
    await client.start()
    string_session = client.session.save()
    print("\n=======================================================")
    print("SUCCESS! Your TELEGRAM_SESSION_STRING is:")
    print(string_session)
    print("=======================================================")
    print("Save this in your .env or Render Environment Variables as TELEGRAM_SESSION_STRING.")
    await client.disconnect()

# ============================================================
# 6. VPS MTPROTO WORKER ENGINE (ASYNC JOB PROCESSOR)
# ============================================================

class WorkerState(enum.Enum):
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"

worker_running = False
worker_lock = asyncio.Lock()

class WorkerLifecycleManager:
    """Authoritative singleton lifecycle manager for the Railway/VPS worker service."""
    def __init__(self):
        self.state: WorkerState = WorkerState.STARTING
        self.shutdown_event = asyncio.Event()
        self._shutdown_lock = asyncio.Lock()
        self.worker_task: Optional[asyncio.Task] = None
        self.telethon_task: Optional[asyncio.Task] = None
        self.bot_instance: Optional[Any] = None
        self.start_time = time.time()

    def is_shutting_down(self) -> bool:
        return self.state in (WorkerState.STOPPING, WorkerState.STOPPED) or self.shutdown_event.is_set()

    def request_shutdown(self, signal_name: str = "SIGTERM"):
        """Synchronous signal handler callback - idempotent and immediate."""
        logger.info(f"[PROCESS] {signal_name} received")
        logger.info(f"[WORKER] Shutdown signal received ({signal_name})")
        if self.state in (WorkerState.STOPPING, WorkerState.STOPPED):
            logger.info("[PROCESS] Shutdown already in progress (idempotent signal ignored)")
            return

        self.state = WorkerState.STOPPING
        logger.info("[PROCESS] Lifecycle state: STOPPING")
        logger.info("[PROCESS] No further startup allowed")
        self.shutdown_event.set()

        global worker_running
        worker_running = False

        # If a telethon connection task is in progress, cancel it immediately
        if self.telethon_task and not self.telethon_task.done():
            self.telethon_task.cancel()

        # If worker queue task is active, cancel it immediately
        if self.worker_task and not self.worker_task.done():
            self.worker_task.cancel()

    async def run(self):
        """The single authoritative worker lifecycle runner."""
        start_iso = datetime.now(timezone.utc).isoformat()
        logger.info(f"[PROCESS] Main lifecycle entered (PID: {os.getpid()}, Start time: {start_iso}, Mode: {SERVICE_MODE})")
        logger.info("[WORKER] Starting worker lifecycle")

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, lambda s=sig.name: self.request_shutdown(s))
            except (NotImplementedError, AttributeError):
                signal.signal(sig, lambda s, f, s_name=sig.name: self.request_shutdown(s_name))

        if self.is_shutting_down():
            await self._cleanup()
            return

        # 1. Stale Job Recovery
        try:
            rec = recover_stale_jobs(stale_timeout_sec=600)
            if rec > 0:
                logger.info(f"[WORKER] Stale job recovery initialized: {rec} job(s) reset to QUEUED.")
        except Exception as e:
            logger.warning(f"[WORKER] Stale job recovery note: {e}")

        if self.is_shutting_down():
            await self._cleanup()
            return

        # 2. Run Startup Diagnostics
        try:
            await run_worker_diagnostics()
        except Exception as err:
            logger.warning(f"[DIAGNOSTICS] Diagnostic check warning: {err}")

        if self.is_shutting_down():
            await self._cleanup()
            return

        # 3. Connect Persistent Telethon (guarded against SIGTERM during connect)
        logger.info("[WORKER] Connecting Telethon")
        try:
            self.telethon_task = asyncio.create_task(get_telethon_client())
            client = await self.telethon_task
            if client:
                logger.info("[WORKER] Telethon connected")
            else:
                logger.warning("[WORKER] Persistent Telethon client not available. Worker running in fallback mode.")
        except asyncio.CancelledError:
            logger.info("[WORKER] Telethon connection cancelled by shutdown signal.")
        except Exception as e:
            logger.warning(f"[WORKER] Error connecting Telethon: {e}")
        finally:
            self.telethon_task = None

        if self.is_shutting_down():
            await self._cleanup()
            return

        # 4. Initialize Standalone Bot Client for notifications only (no polling)
        if BOT_TOKEN:
            try:
                import telegram
                self.bot_instance = telegram.Bot(token=BOT_TOKEN)
                await self.bot_instance.initialize()
            except Exception as e:
                logger.warning(f"[WORKER] Standalone bot instance initialization note: {e}")

        if self.is_shutting_down():
            await self._cleanup()
            return

        # 5. Transition to RUNNING and start Queue Consumer
        self.state = WorkerState.RUNNING
        global worker_running
        worker_running = True
        logger.info("[WORKER] Starting queue consumer")
        logger.info("[WORKER] Worker loop ACTIVE")
        logger.info("[WORKER] Waiting for jobs...")
        self.worker_task = asyncio.create_task(self._worker_loop_core())
        logger.info("[PROCESS] Main lifecycle waiting")

        # 6. Wait for Shutdown Event
        try:
            await self.shutdown_event.wait()
        except asyncio.CancelledError:
            logger.info("[PROCESS] Main lifecycle received CancelledError.")

        # 7. Execute Cleanup
        await self._cleanup()

    async def _worker_loop_core(self):
        last_waiting_log = 0.0
        consecutive_errors = 0

        while self.state == WorkerState.RUNNING and not self.shutdown_event.is_set():
            try:
                job = claim_next_queued_job()
                if not job:
                    now = time.time()
                    if now - last_waiting_log > 300.0:
                        last_waiting_log = now
                        logger.info("[WORKER] Waiting for jobs... (worker active and queue healthy)")

                    try:
                        await asyncio.sleep(2.0)
                    except asyncio.CancelledError:
                        break
                    consecutive_errors = 0
                    continue

                if self.is_shutting_down():
                    job_id = job.get("job_id")
                    if job_id:
                        update_job(job_id, {"status": "QUEUED", "progress_stage": "QUEUED"})
                    break

                consecutive_errors = 0
                job_id = job["job_id"]
                orig_name = job.get("original_filename", "unknown")
                logger.info(f"[WORKER] Processing job {job_id} ({orig_name})...")
                async with worker_lock:
                    await process_single_job(job, ptb_app=None, direct_bot=self.bot_instance)

            except asyncio.CancelledError:
                break
            except Exception as e:
                consecutive_errors += 1
                logger.error(f"[WORKER] Unhandled exception in worker loop: {e}\n{traceback.format_exc()}")
                backoff_sec = min(30.0, 2.0 * consecutive_errors)
                logger.info(f"[WORKER] Backing off worker loop for {backoff_sec:.1f}s before retrying...")
                try:
                    await asyncio.sleep(backoff_sec)
                except asyncio.CancelledError:
                    break

        logger.info("[WORKER] Stopping queue consumer")
        logger.info("[WORKER] Worker loop stopped")
        logger.info("[PROCESS] Queue consumer stopped")

    async def _cleanup(self):
        """Authoritative teardown routine - executed once."""
        async with self._shutdown_lock:
            if self.state == WorkerState.STOPPED:
                return
            self.state = WorkerState.STOPPING

            global worker_running
            worker_running = False

            # Stop and await worker task
            if self.worker_task and not self.worker_task.done():
                self.worker_task.cancel()
                try:
                    await self.worker_task
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    logger.error(f"[WORKER] Error awaiting worker task: {e}")
                self.worker_task = None

            # Disconnect Persistent Telethon
            global telethon_client
            if telethon_client:
                logger.info("[WORKER] Disconnecting Telethon")
                try:
                    if telethon_client.is_connected():
                        await telethon_client.disconnect()
                    logger.info("[WORKER] Telethon disconnected")
                    logger.info("[PROCESS] Telethon disconnected")
                except Exception as e:
                    logger.warning(f"[WORKER] Error disconnecting Telethon: {e}")
                finally:
                    telethon_client = None

            # Close Standalone Bot Client
            if self.bot_instance:
                try:
                    await self.bot_instance.shutdown()
                except Exception:
                    pass
                self.bot_instance = None

            logger.info("[WORKER] Shutdown complete")
            self.state = WorkerState.STOPPED
            logger.info("[PROCESS] Lifecycle state: STOPPED")
            logger.info("[PROCESS] Main lifecycle exiting (Exit code: 0)")

worker_lifecycle = WorkerLifecycleManager()

async def run_worker_diagnostics():
    """Perform real startup diagnostics for worker dependencies."""
    logger.info("[DIAGNOSTICS] Running MTProto worker startup diagnostics...")
    try:
        mtproto_ok = bool(API_ID and API_HASH and TELETHON_AVAILABLE)
        logger.info(f"[DIAGNOSTICS] MTProto Client Config: {'✅ Configured' if mtproto_ok else '⚠️ Missing credentials or Telethon'}")

        try:
            conn = get_sqlite_conn()
            conn.execute("SELECT 1")
            conn.close()
            db_ok = True
        except Exception as e:
            db_ok = False
            logger.error(f"[DIAGNOSTICS] Database check error: {e}")
        logger.info(f"[DIAGNOSTICS] Database Connection: {'✅ OK' if db_ok else '❌ Error'}")

        b2_ok, b2_msg = b2_storage.check_health()
        logger.info(f"[DIAGNOSTICS] B2 Storage Health: {'✅ OK' if b2_ok else f'❌ {b2_msg}'}")

        ffmpeg_ok, ffprobe_ok = check_ffmpeg_installed()
        logger.info(f"[DIAGNOSTICS] FFmpeg Binary: {'✅ Present' if ffmpeg_ok else '❌ Missing'}")
        logger.info(f"[DIAGNOSTICS] FFprobe Binary: {'✅ Present' if ffprobe_ok else '❌ Missing'}")
        logger.info(f"[DIAGNOSTICS] CPU Cores: {os.cpu_count() or 'Unknown'}")
        logger.info(f"[DIAGNOSTICS] FFmpeg Preset: {FFMPEG_PRESET} | CRF: {FFMPEG_CRF} | Threads: {FFMPEG_THREADS} | Stall Timeout: {FFMPEG_STALL_TIMEOUT}s")

        space_ok, space_msg = check_disk_space()
        logger.info(f"[DIAGNOSTICS] Disk Space: {'✅ OK' if space_ok else f'⚠️ {space_msg}'}")

        try:
            conn = get_sqlite_conn()
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM jobs WHERE status = 'QUEUED'")
            q_cnt = cursor.fetchone()[0]
            conn.close()
            logger.info(f"[DIAGNOSTICS] Job Queue Status: ✅ {q_cnt} job(s) queued")
        except Exception as e:
            logger.error(f"[DIAGNOSTICS] Queue status check error: {e}")

    except Exception as exc:
        logger.warning(f"[DIAGNOSTICS] Diagnostics warning: {exc}")

async def run_worker_lifecycle():
    """Entry point for worker lifecycle delegated to the authoritative manager."""
    await worker_lifecycle.run()

async def run_worker_loop(ptb_app: Optional[Application] = None):
    """Legacy helper if called within unified mode."""
    bot_client = ptb_app.bot if ptb_app else None
    await worker_lifecycle._worker_loop_core()

async def process_single_job(job: Dict[str, Any], ptb_app: Optional[Application] = None, direct_bot: Optional[Any] = None):
    t_job_claimed = time.time()
    job_id = job["job_id"]
    chat_id = job["chat_id"]
    message_id = job["message_id"]
    status_msg_id = job.get("status_message_id")
    original_filename = job["original_filename"]
    file_size = job["source_file_size"]
    folder = job["folder"]

    # Calculate queue wait time
    created_at_str = job.get("created_at") or ""
    try:
        from datetime import datetime
        created_dt = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
        queue_wait_sec = round(t_job_claimed - created_dt.timestamp(), 2)
    except Exception:
        queue_wait_sec = 0.0

    logger.info(f"Worker picking up job {job_id} for file '{original_filename}' ({format_size(file_size)}) [PERF Queue Wait: {queue_wait_sec}s]")
    update_job(job_id, {"status": "DOWNLOADING", "progress_stage": "DOWNLOADING", "progress_percent": 0})

    bot_client = direct_bot if direct_bot else (ptb_app.bot if ptb_app else None)

    async def notify_ui(text: str, reply_markup=None):
        nonlocal status_msg_id
        if not bot_client:
            return
        if not status_msg_id:
            curr_job = get_job(job_id)
            if curr_job and curr_job.get("status_message_id"):
                status_msg_id = curr_job["status_message_id"]

        if status_msg_id:
            try:
                await bot_client.edit_message_text(
                    chat_id=chat_id,
                    message_id=status_msg_id,
                    text=text,
                    reply_markup=reply_markup,
                    parse_mode="Markdown"
                )
                return
            except Exception as e:
                logger.debug(f"UI notification edit exception for msg {status_msg_id}: {e}")

        try:
            sent = await bot_client.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode="Markdown"
            )
            if sent and hasattr(sent, "message_id"):
                status_msg_id = sent.message_id
                update_job(job_id, {"status_message_id": status_msg_id})
        except Exception as e:
            logger.error(f"Fallback send_message failed in notify_ui: {e}")

    await notify_ui(
        "🎬 *Processing Workflow Started*\n\n"
        f"📄 *Original File:* `{original_filename}` ({format_size(file_size)})\n"
        f"📁 *B2 Target Folder:* `{folder}`\n\n"
        "⬇️ *Downloading Telegram file via MTProto...*"
    )

    # 1. Disk Space Check
    required_bytes = int(file_size * 2.5)
    space_ok, space_msg = check_disk_space(required_bytes)
    if not space_ok:
        err_text = (
            f"❌ *STORAGE FAILED*\n\n"
            f"📄 *File:* `{original_filename}`\n"
            f"📁 *Folder:* `{folder}`\n"
            f"⬇️ Download: ❌ Insufficient Disk Space\n\n"
            f"*Reason:* {space_msg}"
        )
        update_job(job_id, {"status": "FAILED", "error_stage": "DOWNLOADING", "error_message": space_msg})
        await notify_ui(err_text)
        return

    local_download_path = TEMP_DIR / f"down_{job_id}_{original_filename}"
    local_processed_path = None

    try:
        # 2. MTProto Download
        t_dl_start = time.time()
        if API_ID and API_HASH and TELETHON_AVAILABLE:
            def format_time(seconds: float) -> str:
                if seconds <= 0:
                    return "00:00"
                m, s = divmod(int(seconds), 60)
                h, m = divmod(m, 60)
                if h > 0:
                    return f"{h:02d}:{m:02d}:{s:02d}"
                return f"{m:02d}:{s:02d}"

            async def download_cb(current: int, total: int, speed: float = 0.0, eta_sec: float = 0.0):
                pct = int((current / total) * 100) if total else 0
                filled = int(round(10 * pct / 100))
                bar = "█" * filled + "░" * (10 - filled)
                speed_text = f"{format_size(int(speed))}/s" if speed > 0 else "0 B/s"
                eta_text = format_time(eta_sec)

                update_job(job_id, {"progress_percent": pct, "progress_stage": "DOWNLOADING"})
                await notify_ui(
                    "⬇️ *Downloading from Telegram via MTProto*\n\n"
                    f"📄 *File:* `{original_filename}`\n"
                    f"📁 *Folder:* `{folder}`\n\n"
                    f"`[{bar}] {pct}%`\n\n"
                    f"📊 `{format_size(current)} / {format_size(total)}`\n"
                    f"🚀 *Speed:* `{speed_text}` | ⏱ *ETA:* `{eta_text}`"
                )

            download_ok = await download_telegram_file_mtproto(
                chat_id=chat_id,
                message_id=message_id,
                output_path=local_download_path,
                progress_callback=download_cb
            )

            if not download_ok:
                raise RuntimeError("MTProto download returned 0 bytes or file missing on disk.")

        else:
            # Fallback for small files <= 20 MB via Bot API if MTProto is unconfigured
            if file_size > 20 * 1024 * 1024:
                raise RuntimeError(
                    f"Telegram Bot API Download Limit Exceeded.\n"
                    f"File size: {format_size(file_size)} (Exceeds 20 MiB Bot API limit).\n"
                    f"To process files up to 4 GiB, configure API_ID and API_HASH for MTProto."
                )

            if not bot_client:
                raise RuntimeError("Bot client unavailable for fallback download.")

            tg_file = await bot_client.get_file(job["tg_file_id"])
            await tg_file.download_to_drive(custom_path=local_download_path)

        dl_dur = round(time.time() - t_dl_start, 2)
        dl_mbps = round((file_size / (1024 * 1024)) / dl_dur, 2) if dl_dur > 0 else 0.0
        logger.info(f"[PERF] Job {job_id} | Queue wait: {queue_wait_sec} s | Download: {dl_mbps} MB/s ({dl_dur} s)")

        # 3. FFprobe Inspection
        t_probe_start = time.time()
        update_job(job_id, {"status": "INSPECTING", "progress_stage": "INSPECTING"})
        await notify_ui(
            "🎬 *Inspecting Media Properties*\n\n"
            f"📄 *File:* `{original_filename}`\n\n"
            "🔍 *Running FFprobe analysis...*"
        )

        media_info = {}
        is_video = False
        try:
            media_info = await probe_media(local_download_path)
            is_video = media_info.get("is_video", False)
        except Exception as e:
            logger.warning(f"FFprobe note for job {job_id}: {e}")

        probe_ms = int((time.time() - t_probe_start) * 1000)
        logger.info(f"[PERF] Job {job_id} | FFprobe: {probe_ms} ms")

        # 4. Processing (Remux / Transcode vs Passthrough)
        t_proc_start = time.time()
        if is_video:
            v_codec = media_info.get("video_codec", "unknown")
            a_codec = media_info.get("audio_codec", "unknown")
            output_mp4_name = Path(original_filename).stem + ".mp4"
            local_processed_path = TEMP_DIR / f"proc_{job_id}_{output_mp4_name}"

            last_ffmpeg_ui = 0.0
            async def ffmpeg_progress_cb(pct: int, out_time_sec: float, speed_str: str, fps_str: str, total_duration: float, mode: str):
                nonlocal last_ffmpeg_ui
                now = time.time()
                if (now - last_ffmpeg_ui < 3.5) and (pct != 0 and pct != 100):
                    return
                last_ffmpeg_ui = now

                stage_name = "Transcoding" if mode == "transcode_h264" else "Remuxing"
                status_key = "TRANSCODING" if mode == "transcode_h264" else "REMUXING"
                filled = int(round(10 * pct / 100))
                bar = "█" * filled + "░" * (10 - filled)
                def fmt_sec(s: float) -> str:
                    if not s or s <= 0:
                        return "00:00:00"
                    m, sec = divmod(int(s), 60)
                    h, m = divmod(m, 60)
                    return f"{h:02d}:{m:02d}:{sec:02d}"

                time_str = f"{fmt_sec(out_time_sec)} / {fmt_sec(total_duration)}" if total_duration > 0 else fmt_sec(out_time_sec)
                update_job(job_id, {"status": status_key, "progress_stage": status_key, "progress_percent": pct})

                await notify_ui(
                    "🎬 *Processing Video*\n\n"
                    f"📄 *File:* `{original_filename}`\n"
                    f"⚙️ *{v_codec.upper()} → H.264*\n\n"
                    f"`[{bar}] {pct}%`\n\n"
                    f"⏱ *Time:* `{time_str}`\n"
                    f"🚀 *Speed:* `{speed_str}` | 🎞 *FPS:* `{fps_str}`\n\n"
                    f"Stage: `{stage_name}`"
                )

            await notify_ui(
                "🎬 *Processing Video for Browser Playback*\n\n"
                f"📄 *File:* `{original_filename}`\n"
                f"🔍 *Codecs:* Video (`{v_codec}`), Audio (`{a_codec}`)\n"
                f"⚙️ *Target:* MP4 + H.264 + AAC + yuv420p + faststart\n\n"
                "⏳ *FFmpeg processing starting...*"
            )

            success, mode, err_desc = await process_video_for_web(
                local_download_path,
                local_processed_path,
                media_info,
                progress_callback=ffmpeg_progress_cb
            )

            if not success:
                update_job(job_id, {"status": "FAILED", "progress_stage": "FAILED"})
                raise RuntimeError(f"FFmpeg processing failed ({mode}): {err_desc}")

            update_job(job_id, {"status": "OUTPUT_VALIDATING", "progress_stage": "OUTPUT_VALIDATING"})
            await notify_ui(
                "🎬 *Validating Processed Video*\n\n"
                f"📄 *File:* `{output_mp4_name}`\n\n"
                "🔍 *Verifying H.264 MP4 output integrity...*"
            )

            upload_path = local_processed_path
            final_size = local_processed_path.stat().st_size
            proc_mode = mode
            processed_filename = output_mp4_name
            mime_type = "video/mp4"
        else:
            upload_path = local_download_path
            final_size = local_download_path.stat().st_size
            proc_mode = "passthrough"
            processed_filename = original_filename
            mime_type = job.get("mime_type", "application/octet-stream")

        proc_dur = round(time.time() - t_proc_start, 2)
        logger.info(f"[PERF] Job {job_id} | Remux/Transcode ({proc_mode}): {proc_dur} s")

        # 5. Save intermediate state and prompt rename
        update_job(job_id, {
            "status": "WAITING_FOR_RENAME",
            "progress_stage": "WAITING_FOR_RENAME",
            "processing_mode": proc_mode,
            "final_filename": processed_filename,
            "final_file_size": final_size,
            "mime_type": mime_type,
            "video_codec": media_info.get("video_codec"),
            "audio_codec": media_info.get("audio_codec"),
            "width": media_info.get("width"),
            "height": media_info.get("height"),
            "duration": media_info.get("duration"),
        })

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Keep Current Name", callback_data=f"job_rename_no:{job_id}"),
                InlineKeyboardButton("✏️ Rename File", callback_data=f"job_rename_yes:{job_id}")
            ],
            [
                InlineKeyboardButton("❌ Cancel Job", callback_data=f"job_cancel:{job_id}")
            ]
        ])

        await notify_ui(
            "✏️ *Rename File Before Upload?*\n\n"
            f"📄 *Processed Name:* `{processed_filename}`\n"
            f"📁 *B2 Target Folder:* `{folder}`\n"
            f"⚙️ *Processing Mode:* `{proc_mode}`\n"
            f"📦 *Processed Size:* `{format_size(final_size)}`",
            reply_markup=keyboard
        )

    except Exception as e:
        logger.error(f"Job {job_id} processing exception: {e}", exc_info=e)
        update_job(job_id, {"status": "FAILED", "error_stage": "PROCESSING", "error_message": str(e)})
        await notify_ui(
            f"❌ *STORAGE FAILED*\n\n"
            f"📄 *File:* `{original_filename}`\n"
            f"📁 *Folder:* `{folder}`\n"
            f"⬇️ Download: ❌ Exception\n\n"
            f"*Reason:* {e}"
        )
        if local_download_path.exists():
            local_download_path.unlink(missing_ok=True)
        if local_processed_path and local_processed_path.exists():
            local_processed_path.unlink(missing_ok=True)

async def continue_job_upload(job_id: str, ptb_app: Optional[Application]):
    job = get_job(job_id)
    if not job or job["status"] in ["CANCELLED", "FAILED", "SUCCESS"]:
        return

    chat_id = job["chat_id"]
    status_msg_id = job.get("status_message_id")
    folder = job["folder"]
    filename = job["final_filename"]
    proc_mode = job.get("processing_mode", "passthrough")

    bot_client = ptb_app.bot if ptb_app else None

    async def notify_ui(text: str, reply_markup=None):
        if not bot_client or not status_msg_id:
            return
        try:
            await bot_client.edit_message_text(
                chat_id=chat_id,
                message_id=status_msg_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.debug(f"UI notification edit exception: {e}")

    # Construct B2 path
    clean_filename = sanitize_filename(filename)
    if folder == "/":
        b2_path = clean_filename
    else:
        b2_path = f"{folder.strip('/')}/{clean_filename}"

    update_job(job_id, {"b2_path": b2_path, "status": "UPLOADING", "progress_stage": "UPLOADING"})

    # Check B2 duplicate
    exists = await asyncio.to_thread(b2_storage.file_exists, b2_path)
    if exists:
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔄 Overwrite B2 File", callback_data=f"job_dup_overwrite:{job_id}"),
                InlineKeyboardButton("✏️ Auto-Rename", callback_data=f"job_dup_autorename:{job_id}")
            ],
            [
                InlineKeyboardButton("❌ Cancel Job", callback_data=f"job_cancel:{job_id}")
            ]
        ])
        await notify_ui(
            "⚠️ *File Already Exists in B2*\n\n"
            f"📁 *Folder:* `{folder}`\n"
            f"📄 *File:* `{clean_filename}`\n"
            f"🔗 *B2 Path:* `{b2_path}`\n\n"
            "Please select duplicate resolution action:",
            reply_markup=keyboard
        )
        return

    await execute_b2_upload_and_indexing(job_id, ptb_app)

async def execute_b2_upload_and_indexing(job_id: str, ptb_app: Optional[Application]):
    job = get_job(job_id)
    if not job:
        return

    chat_id = job["chat_id"]
    status_msg_id = job.get("status_message_id")
    folder = job["folder"]
    b2_path = job["b2_path"]
    filename = job["final_filename"]
    proc_mode = job.get("processing_mode", "passthrough")

    bot_client = ptb_app.bot if ptb_app else None

    async def notify_ui(text: str, reply_markup=None):
        if not bot_client or not status_msg_id:
            return
        try:
            await bot_client.edit_message_text(
                chat_id=chat_id,
                message_id=status_msg_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode="Markdown"
            )
        except Exception:
            pass

    # Find upload file path on disk
    local_download_path = TEMP_DIR / f"down_{job_id}_{job['original_filename']}"
    output_mp4_name = Path(job['original_filename']).stem + ".mp4"
    local_processed_path = TEMP_DIR / f"proc_{job_id}_{output_mp4_name}"

    upload_path = local_processed_path if local_processed_path.exists() else local_download_path
    if not upload_path.exists():
        await notify_ui(f"❌ *STORAGE FAILED*: Local temp file missing on disk for upload.")
        update_job(job_id, {"status": "FAILED", "error_message": "Local disk file missing"})
        return

    await notify_ui(
        "☁️ *Uploading File to Backblaze B2...*\n\n"
        f"📄 *File:* `{filename}`\n"
        f"📁 *Folder:* `{folder}`\n"
        f"🔗 *B2 Path:* `{b2_path}`\n"
        f"📦 *Size:* `{format_size(upload_path.stat().st_size)}`"
    )

    b2_result = None
    b2_err = ""
    t_up_start = time.time()
    try:
        async with UPLOAD_SEMAPHORE:
            b2_result = await asyncio.to_thread(
                b2_storage.upload_file,
                upload_path,
                b2_path
            )
    except Exception as e:
        logger.error(f"B2 upload exception for job {job_id}: {e}")
        b2_err = str(e)

    if not b2_result:
        update_job(job_id, {"status": "FAILED", "error_stage": "UPLOADING", "error_message": b2_err})
        await notify_ui(
            f"❌ *STORAGE FAILED*\n\n"
            f"📄 *File:* `{filename}`\n"
            f"📁 *Folder:* `{folder}`\n"
            f"☁️ B2 Upload: ❌ Failed\n\n"
            f"*Reason:* {b2_err}"
        )
        cleanup_job_temp_files(job_id, job['original_filename'])
        return

    up_dur = round(time.time() - t_up_start, 2)
    up_size = upload_path.stat().st_size
    up_mbps = round((up_size / (1024 * 1024)) / up_dur, 2) if up_dur > 0 else 0.0
    logger.info(f"[PERF] Job {job_id} | B2 upload: {up_mbps} MB/s ({up_dur} s)")

    # Verify B2
    update_job(job_id, {"status": "VERIFYING", "progress_stage": "VERIFYING"})
    verified = await asyncio.to_thread(b2_storage.file_exists, b2_path)
    if not verified:
        update_job(job_id, {"status": "FAILED", "error_stage": "VERIFYING", "error_message": "Post-upload verification failed"})
        await notify_ui(
            f"❌ *STORAGE FAILED*\n\n"
            f"📄 *File:* `{filename}`\n"
            f"📁 *Folder:* `{folder}`\n"
            f"☁️ B2 Verification: ❌ Object key not found in bucket after upload."
        )
        cleanup_job_temp_files(job_id, job['original_filename'])
        return

    # Database Indexing
    t_db_start = time.time()
    update_job(job_id, {"status": "DATABASE_INDEXING", "progress_stage": "DATABASE_INDEXING"})
    rec_data = {
        "file_name": filename,
        "original_file_name": job["original_filename"],
        "folder": folder,
        "b2_bucket": B2_BUCKET_NAME,
        "b2_path": b2_path,
        "b2_file_id": b2_result.get("b2_file_id"),
        "file_size": b2_result.get("file_size", upload_path.stat().st_size),
        "mime_type": job.get("mime_type", "application/octet-stream"),
        "media_type": job.get("media_type", "document"),
        "video_codec": job.get("video_codec"),
        "audio_codec": job.get("audio_codec"),
        "width": job.get("width"),
        "height": job.get("height"),
        "duration": job.get("duration"),
        "processing_mode": proc_mode,
        "status": "complete",
        "b2_url": b2_result.get("b2_url")
    }

    db_rec_id = insert_file_record(rec_data)
    db_ms = int((time.time() - t_db_start) * 1000)
    logger.info(f"[PERF] Job {job_id} | DB write: {db_ms} ms")
    update_job(job_id, {
        "status": "SUCCESS",
        "progress_stage": "SUCCESS",
        "b2_file_id": b2_result.get("b2_file_id"),
        "b2_url": b2_result.get("b2_url")
    })

    if not db_rec_id:
        await notify_ui(
            "⚠️ *PARTIAL SUCCESS (DATABASE INDEX FAILED)*\n\n"
            f"📄 *File:* `{filename}`\n"
            f"📁 *Folder:* `{folder}`\n"
            f"☁️ B2: ✅ Uploaded & Verified\n"
            f"🆔 B2 File ID: `{b2_result.get('b2_file_id')}`\n"
            f"🗄 Database: ❌ Index row insertion failed"
        )
    else:
        url_text = f"`{b2_result['b2_url']}`" if b2_result.get("b2_url") else "`N/A (Private Bucket)`"
        await notify_ui(
            "✅ *STORAGE WORKFLOW SUCCESSFUL*\n\n"
            f"📄 *File:* `{filename}`\n"
            f"📁 *Folder:* `{folder}`\n"
            f"🎬 *Mode:* `{proc_mode}`\n"
            f"☁️ *B2 Bucket:* `{B2_BUCKET_NAME}`\n"
            f"🆔 *B2 File ID:* `{b2_result.get('b2_file_id')}`\n"
            f"🔗 *B2 Path:* `{b2_path}`\n"
            f"🗄️ *DB Record ID:* `{db_rec_id}`\n"
            f"🌐 *Public URL:* {url_text}"
        )

    cleanup_job_temp_files(job_id, job['original_filename'])

def cleanup_job_temp_files(job_id: str, original_filename: str):
    p_down = TEMP_DIR / f"down_{job_id}_{original_filename}"
    output_mp4_name = Path(original_filename).stem + ".mp4"
    p_proc = TEMP_DIR / f"proc_{job_id}_{output_mp4_name}"

    if p_down.exists():
        try:
            p_down.unlink(missing_ok=True)
        except Exception:
            pass
    if p_proc.exists():
        try:
            p_proc.unlink(missing_ok=True)
        except Exception:
            pass

# ============================================================
# 7. HELPER UTILITIES & SECURITY
# ============================================================

def is_admin(update: Update) -> bool:
    if not update.effective_user:
        return False
    return update.effective_user.id in ADMIN_IDS

async def deny_access(update: Update):
    msg = "🚫 *Access Denied*: You are not an authorized admin user for this storage bot."
    if update.callback_query:
        await update.callback_query.answer("Access Denied", show_alert=True)
        try:
            await update.callback_query.edit_message_text(msg, parse_mode="Markdown")
        except Exception:
            pass
    elif update.message:
        await update.message.reply_text(msg, parse_mode="Markdown")

def format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.2f} KiB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.2f} MiB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GiB"

def sanitize_filename(name: str) -> str:
    cleaned = name.replace("\0", "").replace("..", "_").replace("\\", "/")
    cleaned = re.sub(r'[/*?:"<>|]', '_', cleaned)
    return cleaned.strip()

# ============================================================
# 8. HTTP HEALTH CHECK SERVER (PORT 3000)
# ============================================================

_http_server_instance: Optional[HTTPServer] = None
_process_start_time = time.time()

class HealthHandler(BaseHTTPRequestHandler):
    def get_health_payload(self) -> bytes:
        telethon_status = "unconfigured"
        if API_ID and API_HASH and TELETHON_AVAILABLE:
            if telethon_client and telethon_client.is_connected():
                telethon_status = "connected"
            elif telethon_flood_wait_until > time.time():
                telethon_status = f"rate_limited ({int(telethon_flood_wait_until - time.time())}s cooldown)"
            else:
                telethon_status = "configured_idle"

        queue_status = "healthy"
        try:
            conn = get_sqlite_conn()
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM jobs WHERE status = 'QUEUED'")
            q_cnt = cursor.fetchone()[0]
            conn.close()
            queue_status = f"healthy ({q_cnt} queued)"
        except Exception:
            queue_status = "accessible"

        worker_state_str = worker_lifecycle.state.value if 'worker_lifecycle' in globals() else ("RUNNING" if worker_running else "IDLE")

        payload = {
            "status": "ok",
            "service": "b2-telegram-storage-bot",
            "service_mode": SERVICE_MODE,
            "worker": "active" if worker_state_str == "RUNNING" else worker_state_str.lower(),
            "worker_state": worker_state_str,
            "telethon": telethon_status,
            "queue": queue_status,
            "pid": os.getpid(),
            "uptime_sec": int(time.time() - _process_start_time)
        }
        return (json.dumps(payload, indent=2) + "\n").encode("utf-8")

    def do_GET(self):
        body = self.get_health_payload()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        body = self.get_health_payload()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()

    def log_message(self, format, *args):
        pass

def start_health_server():
    global _http_server_instance
    try:
        _http_server_instance = HTTPServer(("0.0.0.0", PORT), HealthHandler)
        thread = threading.Thread(target=_http_server_instance.serve_forever, daemon=True)
        thread.start()
        logger.info(f"Health check HTTP server listening on port {PORT} (mode={SERVICE_MODE})")
    except Exception as e:
        logger.warning(f"Could not bind HTTP server on port {PORT}: {e}")

# ============================================================
# 9. TELEGRAM COMMAND HANDLERS
# ============================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny_access(update)
        return

    admin_id = update.effective_user.id
    b2_ok, b2_msg = await asyncio.to_thread(b2_storage.check_health)

    text = (
        "🚀 *Production B2 & Telegram Storage Bot*\n\n"
        f"👑 *Admin User:* `{admin_id}`\n"
        f"⚙️ *Service Mode:* `{SERVICE_MODE}`\n"
        f"⚡ *MTProto Client:* {'✅ Configured' if (API_ID and API_HASH) else '⚠️ Unconfigured'}\n"
        f"☁️ *B2 Bucket:* `{B2_BUCKET_NAME}`\n"
        f"🔗 *B2 Health:* {'✅ Connected' if b2_ok else '❌ Error'}\n\n"
        "Send any file or video (up to 4 GiB) to begin storage workflow!"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📂 Browse B2 Folders", callback_data="cmd_folders"),
            InlineKeyboardButton("📊 Storage Stats", callback_data="cmd_stats")
        ],
        [
            InlineKeyboardButton("🗄 Storage Health Check", callback_data="cmd_storage_health"),
            InlineKeyboardButton("🔄 Refresh Folders", callback_data="cmd_refresh_folders")
        ],
        [
            InlineKeyboardButton("📋 Active Jobs Queue", callback_data="cmd_jobs_list"),
            InlineKeyboardButton("👑 Admin Panel", callback_data="admin_panel")
        ]
    ])

    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny_access(update)
        return

    text = (
        "📋 *Bot Commands Reference*\n\n"
        "/start - Main Menu\n"
        "/help - Commands Guide\n"
        "/folders - Browse Real B2 Folders\n"
        "/selected - View Active Upload Folder\n"
        "/search <query> - Search stored B2 objects\n"
        "/stats - View storage usage statistics\n"
        "/storage - Perform real component health checks\n"
        "/jobs - List recent processing jobs\n"
        "/admin - Admin Security Panel\n"
        "/admins - List authorized Admin IDs\n"
        "/addadmin <id> - Add an admin ID\n"
        "/removeadmin <id> - Remove an admin ID\n"
        "/audit - View admin audit logs\n"
        "/cancel - Reset active interactive state"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def storage_health_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny_access(update)
        return

    msg = await update.message.reply_text("⏳ *Performing Real System Health Check...*", parse_mode="Markdown") if update.message else None

    b2_ok, b2_msg = await asyncio.to_thread(b2_storage.check_health)
    ffmpeg_ok, ffprobe_ok = check_ffmpeg_installed()

    db_ok = True
    db_msg = "Database operational"
    try:
        stats = get_db_stats()
        db_msg = f"{stats['total_files']} files indexed ({stats['readable_bytes']})"
    except Exception as e:
        db_ok = False
        db_msg = str(e)

    mtproto_ok = bool(API_ID and API_HASH and TELETHON_AVAILABLE)

    status_text = (
        "🗄 *Real System Health Status*\n\n"
        f"⚙️ *Service Mode:* `{SERVICE_MODE}`\n"
        f"⚡ *MTProto Client:* {'✅ Available (4 GiB)' if mtproto_ok else '⚠️ Unconfigured'}\n"
        f"☁️ *B2 Authentication:* {'✅' if b2_ok else '❌'}\n"
        f"🪣 *Bucket (`{B2_BUCKET_NAME}`):* {'✅' if b2_ok else '❌'}\n"
        f"📄 *B2 Status:* {b2_msg}\n\n"
        f"🎬 *FFmpeg Binary:* {'✅' if ffmpeg_ok else '❌'}\n"
        f"🔍 *FFprobe Binary:* {'✅' if ffprobe_ok else '❌'}\n\n"
        f"🗄️ *Database:* {'✅' if db_ok else '❌'}\n"
        f"📄 *DB Details:* {db_msg}"
    )

    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Main Menu", callback_data="start_menu")]])

    if update.callback_query:
        await update.callback_query.edit_message_text(status_text, reply_markup=keyboard, parse_mode="Markdown")
    elif msg:
        await msg.edit_text(status_text, reply_markup=keyboard, parse_mode="Markdown")

async def folders_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny_access(update)
        return

    b2_folders = await asyncio.to_thread(b2_storage.discover_folders)
    cur_folder = context.user_data.get("selected_folder", "/")

    buttons = []
    root_icon = "🎯 " if cur_folder == "/" else "📁 "
    buttons.append([InlineKeyboardButton(f"{root_icon}[Root /]", callback_data="folder_sel:/")])

    if b2_folders:
        for f in b2_folders[:15]:
            icon = "🎯 " if cur_folder == f else "📁 "
            buttons.append([InlineKeyboardButton(f"{icon}{f}", callback_data=f"folder_sel:{f}")])
    else:
        buttons.append([InlineKeyboardButton("📂 No existing folders found.", callback_data="noop")])

    buttons.append([
        InlineKeyboardButton("➕ Create Folder", callback_data="folder_create"),
        InlineKeyboardButton("🔄 Refresh", callback_data="cmd_refresh_folders")
    ])
    buttons.append([InlineKeyboardButton("⬅️ Main Menu", callback_data="start_menu")])

    keyboard = InlineKeyboardMarkup(buttons)
    text = (
        "📂 *Select Destination Folder*\n\n"
        f"🎯 *Active Folder:* `{cur_folder}`\n"
        f"☁️ *B2 Bucket:* `{B2_BUCKET_NAME}`\n\n"
        "Folders discovered directly from Backblaze B2 object keys:"
    )

    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")

async def selected_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny_access(update)
        return

    folder = context.user_data.get("selected_folder", "/")
    await update.message.reply_text(
        f"🎯 *Currently Selected B2 Target Folder:* `{folder}`",
        parse_mode="Markdown"
    )

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny_access(update)
        return

    stats = get_db_stats()
    text = (
        "📊 *Storage Usage Statistics*\n\n"
        f"📦 *Total Stored Files:* `{stats['total_files']}`\n"
        f"💾 *Total Disk Storage:* `{stats['readable_bytes']}`\n"
        f"📁 *Discovered B2 Folders:* `{stats['total_folders']}`\n"
        f"☁️ *B2 Bucket:* `{B2_BUCKET_NAME}`"
    )

    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Main Menu", callback_data="start_menu")]])

    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")

async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny_access(update)
        return

    if not context.args:
        await update.message.reply_text("🔎 Usage: `/search <filename>`", parse_mode="Markdown")
        return

    query = " ".join(context.args).strip()
    results = await asyncio.to_thread(b2_storage.search_files, query)

    if not results:
        await update.message.reply_text(f"🔎 *No B2 objects found matching:* `{query}`", parse_mode="Markdown")
        return

    lines = [f"🔎 *Search Results for* `{query}` ({len(results)} found):\n"]
    for r in results[:10]:
        sz_mb = r["file_size"] / (1024 * 1024)
        lines.append(f"• `{r['file_name']}` ({sz_mb:.1f} MB)\n  Path: `{r['b2_path']}`")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def jobs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny_access(update)
        return

    jobs = get_recent_jobs(10)
    if not jobs:
        text = "📋 No processing jobs in queue history."
    else:
        lines = ["📋 *Recent Processing Jobs Queue*:\n"]
        for j in jobs:
            st = j["status"]
            icon = "✅" if st == "SUCCESS" else ("❌" if st == "FAILED" else "⏳")
            lines.append(
                f"• {icon} `{j['original_filename']}` ({format_size(j['source_file_size'])})\n"
                f"  Status: *{st}* | Folder: `{j['folder']}` | ID: `{j['job_id']}`"
            )
        text = "\n".join(lines)

    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Main Menu", callback_data="start_menu")]])

    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, parse_mode="Markdown")

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny_access(update)
        return

    context.user_data.pop("awaiting_folder_input", None)
    context.user_data.pop("awaiting_rename_job_id", None)
    context.user_data.pop("pending_file", None)
    await update.message.reply_text("❌ Active prompts and operations cancelled.", parse_mode="Markdown")

async def admin_panel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny_access(update)
        return

    stats = get_db_stats()
    text = (
        "👑 *Admin Security Panel*\n\n"
        f"🛡️ *Authorized Admins:* `{len(ADMIN_IDS)}`\n"
        f"📦 *Total Database Records:* `{stats['total_files']}`\n"
        f"💾 *Total B2 Data Indexed:* `{stats['readable_bytes']}`\n\n"
        "Admin Commands:\n"
        "• `/admins` - View authorized IDs\n"
        "• `/addadmin <id>` - Grant access\n"
        "• `/removeadmin <id>` - Revoke access\n"
        "• `/audit` - View action logs"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🛡️ Admins List", callback_data="admin_list"),
            InlineKeyboardButton("📋 Audit Logs", callback_data="admin_audit")
        ],
        [
            InlineKeyboardButton("⬅️ Main Menu", callback_data="start_menu")
        ]
    ])

    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, parse_mode="Markdown")

async def list_admins_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny_access(update)
        return

    admins_str = "\n".join([f"• `{aid}`" for aid in sorted(ADMIN_IDS)])
    text = f"🛡️ *Authorized Admin Users* ({len(ADMIN_IDS)}):\n\n{admins_str}"
    await update.message.reply_text(text, parse_mode="Markdown")

async def add_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny_access(update)
        return

    if not context.args:
        await update.message.reply_text("⚠️ Usage: `/addadmin <user_id>`", parse_mode="Markdown")
        return

    try:
        new_id = int(context.args[0].strip())
        ADMIN_IDS.add(new_id)
        log_audit(update.effective_user.id, "add_admin", f"Added admin {new_id}")
        await update.message.reply_text(f"✅ User `{new_id}` authorized as Admin!", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")

async def remove_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny_access(update)
        return

    if not context.args:
        await update.message.reply_text("⚠️ Usage: `/removeadmin <user_id>`", parse_mode="Markdown")
        return

    try:
        target_id = int(context.args[0].strip())
        if target_id in ADMIN_IDS:
            if len(ADMIN_IDS) <= 1:
                await update.message.reply_text("❌ Cannot remove the sole remaining admin.")
                return
            ADMIN_IDS.remove(target_id)
            log_audit(update.effective_user.id, "remove_admin", f"Removed admin {target_id}")
            await update.message.reply_text(f"✅ Revoked admin access for `{target_id}`.", parse_mode="Markdown")
        else:
            await update.message.reply_text("⚠️ User ID not found in admin list.")
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")

async def audit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny_access(update)
        return

    logs = get_recent_audit_logs(15)
    if not logs:
        text = "📋 No audit logs recorded yet."
    else:
        lines = []
        for l in logs:
            dt = l.get("created_at", "")[:19].replace("T", " ")
            lines.append(f"• `[{dt}]` Admin `{l['admin_id']}`: *{l['action']}* — {l['details']}")
        text = "📋 *Admin Action Audit Logs*:\n\n" + "\n".join(lines)

    await update.message.reply_text(text, parse_mode="Markdown")

# ============================================================
# 10. CALLBACK QUERY ROUTER
# ============================================================

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny_access(update)
        return

    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "start_menu":
        await start_command(update, context)
    elif data in ["cmd_folders", "cmd_refresh_folders"]:
        await folders_command(update, context)
    elif data == "cmd_stats":
        await stats_command(update, context)
    elif data == "cmd_storage_health":
        await storage_health_command(update, context)
    elif data == "cmd_jobs_list":
        await jobs_command(update, context)
    elif data == "admin_panel":
        await admin_panel_command(update, context)
    elif data == "admin_list":
        await list_admins_command(update, context)
    elif data == "admin_audit":
        await audit_command(update, context)
    elif data.startswith("folder_sel:"):
        folder = data.split("folder_sel:", 1)[1]
        context.user_data["selected_folder"] = folder
        
        pending = context.user_data.get("pending_file")
        if pending:
            pending["folder"] = folder
            job_id = create_job(pending)
            context.user_data.pop("pending_file", None)

            sent_msg = await query.edit_message_text(
                "🎬 *Job Queued Successfully*\n\n"
                f"📄 *File:* `{pending['original_filename']}` ({format_size(pending['source_file_size'])})\n"
                f"📁 *B2 Target Folder:* `{folder}`\n"
                f"🆔 *Job ID:* `{job_id}`\n\n"
                "⏳ *Queued for MTProto worker processing...*",
                parse_mode="Markdown"
            )
            if sent_msg and hasattr(sent_msg, "message_id"):
                update_job(job_id, {"status_message_id": sent_msg.message_id})
        else:
            await query.edit_message_text(
                f"✅ Active destination folder set to: `{folder}`\n\nNow send any file or video to process and upload to this folder.",
                parse_mode="Markdown"
            )

    elif data == "folder_create":
        context.user_data["awaiting_folder_input"] = True
        await query.edit_message_text(
            "📁 *Create New B2 Folder*\n\n"
            "Please send the new folder path (e.g. `Solo Leveling` or `Movies/Action`):",
            parse_mode="Markdown"
        )

    elif data.startswith("job_rename_no:"):
        job_id = data.split("job_rename_no:", 1)[1]
        app_ref = context.application
        asyncio.create_task(continue_job_upload(job_id, app_ref))

    elif data.startswith("job_rename_yes:"):
        job_id = data.split("job_rename_yes:", 1)[1]
        context.user_data["awaiting_rename_job_id"] = job_id
        await query.edit_message_text(
            "✏️ *Rename File*\n\n"
            "Please send the desired filename (e.g. `S01E01.mp4` or `Solo Leveling - 01`):",
            parse_mode="Markdown"
        )

    elif data.startswith("job_dup_overwrite:"):
        job_id = data.split("job_dup_overwrite:", 1)[1]
        app_ref = context.application
        asyncio.create_task(execute_b2_upload_and_indexing(job_id, app_ref))

    elif data.startswith("job_dup_autorename:"):
        job_id = data.split("job_dup_autorename:", 1)[1]
        job = get_job(job_id)
        if job:
            stem = Path(job["final_filename"]).stem
            ext = Path(job["final_filename"]).suffix
            new_fn = f"{stem}_{int(time.time())}{ext}"
            folder = job["folder"]
            new_b2_path = new_fn if folder == "/" else f"{folder.strip('/')}/{new_fn}"
            update_job(job_id, {"final_filename": new_fn, "b2_path": new_b2_path})
            app_ref = context.application
            asyncio.create_task(execute_b2_upload_and_indexing(job_id, app_ref))

    elif data.startswith("job_cancel:"):
        job_id = data.split("job_cancel:", 1)[1]
        update_job(job_id, {"status": "CANCELLED"})
        await query.edit_message_text(f"❌ Job `{job_id}` cancelled by user.", parse_mode="Markdown")

# ============================================================
# 11. INCOMING MESSAGE HANDLER (FILES & TEXT INPUTS)
# ============================================================

async def handle_incoming_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny_access(update)
        return

    msg = update.message
    if not msg:
        return

    # Handle text inputs for folder creation or file renaming
    if context.user_data.get("awaiting_folder_input"):
        folder_raw = msg.text.strip().strip("/")
        context.user_data.pop("awaiting_folder_input", None)
        if folder_raw:
            folder = f"{folder_raw}/"
            context.user_data["selected_folder"] = folder
            
            pending = context.user_data.get("pending_file")
            if pending:
                pending["folder"] = folder
                job_id = create_job(pending)
                context.user_data.pop("pending_file", None)
                sent_msg = await msg.reply_text(
                    "🎬 *Job Queued Successfully*\n\n"
                    f"📄 *File:* `{pending['original_filename']}` ({format_size(pending['source_file_size'])})\n"
                    f"📁 *B2 Target Folder:* `{folder}`\n"
                    f"🆔 *Job ID:* `{job_id}`\n\n"
                    "⏳ *Queued for MTProto worker processing...*",
                    parse_mode="Markdown"
                )
                if sent_msg and hasattr(sent_msg, "message_id"):
                    update_job(job_id, {"status_message_id": sent_msg.message_id})
            else:
                await msg.reply_text(f"✅ Active B2 destination folder created & set to: `{folder}`", parse_mode="Markdown")
        else:
            await msg.reply_text("❌ Invalid folder path.")
        return

    if context.user_data.get("awaiting_rename_job_id"):
        job_id = context.user_data.pop("awaiting_rename_job_id")
        new_filename = msg.text.strip()
        clean_fn = sanitize_filename(new_filename)
        job = get_job(job_id)
        if job:
            if job.get("media_type") == "video" and not clean_fn.lower().endswith(".mp4"):
                clean_fn += ".mp4"
            folder = job["folder"]
            b2_path = clean_fn if folder == "/" else f"{folder.strip('/')}/{clean_fn}"
            update_job(job_id, {"final_filename": clean_fn, "b2_path": b2_path})
            app_ref = context.application
            asyncio.create_task(continue_job_upload(job_id, app_ref))
        return

    # Handle incoming document/video file uploads
    file_obj = msg.document or msg.video or msg.audio or (msg.photo[-1] if msg.photo else None)
    if not file_obj:
        return

    original_name = getattr(file_obj, "file_name", None) or f"file_{int(time.time())}.dat"
    original_name = sanitize_filename(original_name)
    file_size = getattr(file_obj, "file_size", 0)
    mime_type = getattr(file_obj, "mime_type", "application/octet-stream")
    media_type = "video" if msg.video else ("audio" if msg.audio else "document")
    file_id = getattr(file_obj, "file_id", "N/A")
    file_unique_id = getattr(file_obj, "file_unique_id", "N/A")

    logger.info(
        f"Incoming Telegram File Metadata:\n"
        f"  file_id: {file_id}\n"
        f"  file_unique_id: {file_unique_id}\n"
        f"  file_size: {file_size} bytes ({format_size(file_size)})\n"
        f"  filename: {original_name}\n"
        f"  MIME type: {mime_type}\n"
        f"  Application limit: {MAX_FILE_SIZE_BYTES} bytes ({format_size(MAX_FILE_SIZE_BYTES)})"
    )

    if file_size and file_size > MAX_FILE_SIZE_BYTES:
        await msg.reply_text(
            f"❌ *Application Size Limit Exceeded*\n\n"
            f"• *File:* `{original_name}`\n"
            f"• *File Size:* `{file_size}` bytes ({format_size(file_size)})\n"
            f"• *Application Limit:* `{MAX_FILE_SIZE_BYTES}` bytes ({format_size(MAX_FILE_SIZE_BYTES)})\n\n"
            f"The application maximum is 4 GiB (4,294,967,296 bytes).",
            parse_mode="Markdown"
        )
        return

    cur_folder = context.user_data.get("selected_folder", "/")
    pending_data = {
        "user_id": msg.from_user.id if msg.from_user else 0,
        "chat_id": msg.chat_id,
        "message_id": msg.message_id,
        "status_message_id": None,
        "tg_file_id": file_id,
        "tg_file_unique_id": file_unique_id,
        "original_filename": original_name,
        "final_filename": original_name,
        "folder": cur_folder,
        "source_file_size": file_size,
        "mime_type": mime_type,
        "media_type": media_type,
        "status": "QUEUED"
    }

    context.user_data["pending_file"] = pending_data

    # Display B2 folder selection interface
    b2_folders = await asyncio.to_thread(b2_storage.discover_folders)

    buttons = []
    root_icon = "🎯 " if cur_folder == "/" else "📁 "
    buttons.append([InlineKeyboardButton(f"{root_icon}[Root /]", callback_data="folder_sel:/")])

    if b2_folders:
        for f in b2_folders[:15]:
            icon = "🎯 " if cur_folder == f else "📁 "
            buttons.append([InlineKeyboardButton(f"{icon}{f}", callback_data=f"folder_sel:{f}")])
    else:
        buttons.append([InlineKeyboardButton("📂 No existing folders found.", callback_data="noop")])

    buttons.append([
        InlineKeyboardButton("➕ Create Folder", callback_data="folder_create"),
        InlineKeyboardButton("🔄 Refresh", callback_data="cmd_refresh_folders")
    ])
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="job_cancel:none")])

    keyboard = InlineKeyboardMarkup(buttons)
    await msg.reply_text(
        f"📩 *File Received:* `{original_name}` ({format_size(file_size)})\n\n"
        f"🎯 *Default Folder:* `{cur_folder}`\n"
        "📂 *Select Destination B2 Folder:*",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

# ============================================================
# 12. GLOBAL ERROR HANDLER
# ============================================================

_last_conflict_log_time = 0.0

async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    global _last_conflict_log_time
    err = context.error
    if isinstance(err, telegram.error.Conflict):
        now = time.time()
        if now - _last_conflict_log_time > 60.0:
            _last_conflict_log_time = now
            logger.warning(
                "[409 CONFLICT] Telegram getUpdates Conflict: Another process or service instance is actively polling Telegram Bot API with this BOT_TOKEN. "
                "Backing off getUpdates retry loop. Set SERVICE_MODE=worker on worker nodes to disable Bot API polling."
            )
        await asyncio.sleep(15)
    elif isinstance(err, telegram.error.NetworkError):
        logger.warning(f"Telegram Network Error encountered: {err}. Retrying automatically...")
    elif isinstance(err, telegram.error.TimedOut):
        logger.warning(f"Telegram Request Timed Out: {err}. Retrying automatically...")
    else:
        logger.error(f"Unhandled exception in Telegram bot update loop: {err}", exc_info=err)

# ============================================================
# 13. MAIN ENTRY POINT & APPLICATION INITIALIZATION
# ============================================================

def main():
    if "--setup-session" in sys.argv:
        asyncio.run(setup_telethon_session_cli())
        return

    logger.info(f"Starting Anime4u Storage Bot & MTProto Worker Engine [SERVICE_MODE={SERVICE_MODE}]...")

    start_health_server()
    init_db()

    ffmpeg_ok, ffprobe_ok = check_ffmpeg_installed()
    logger.info(f"FFmpeg presence: FFmpeg={ffmpeg_ok}, FFprobe={ffprobe_ok}")

    b2_ok, b2_msg = b2_storage.check_health()
    logger.info(f"B2 Startup Check: {b2_msg}")

    # Mode-based Execution
    if SERVICE_MODE == "worker":
        logger.info("[SERVICE_MODE] Running in WORKER mode. Telegram Bot API polling is DISABLED.")
        try:
            asyncio.run(run_worker_lifecycle())
        except (KeyboardInterrupt, SystemExit):
            logger.info("[WORKER] Worker process terminated gracefully.")
        except Exception as e:
            logger.error(f"[WORKER] Fatal error in worker lifecycle: {e}\n{traceback.format_exc()}")
            sys.exit(1)
        return

    if not BOT_TOKEN:
        logger.error("BOT_TOKEN environment variable is missing for bot polling mode.")
        sys.exit(1)

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .read_timeout(300)
        .write_timeout(300)
        .connect_timeout(60)
        .get_updates_read_timeout(60)
        .build()
    )

    app.add_error_handler(global_error_handler)

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("folders", folders_command))
    app.add_handler(CommandHandler("selected", selected_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("storage", storage_health_command))
    app.add_handler(CommandHandler("search", search_command))
    app.add_handler(CommandHandler("jobs", jobs_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("admin", admin_panel_command))
    app.add_handler(CommandHandler("admins", list_admins_command))
    app.add_handler(CommandHandler("addadmin", add_admin_command))
    app.add_handler(CommandHandler("removeadmin", remove_admin_command))
    app.add_handler(CommandHandler("audit", audit_command))

    app.add_handler(CallbackQueryHandler(handle_callback_query))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_incoming_message))

    if SERVICE_MODE == "all":
        logger.info("[SERVICE_MODE] Running in ALL mode (Bot Polling + MTProto Worker Engine).")
        worker_task_all: Optional[asyncio.Task] = None

        async def post_init(application: Application):
            nonlocal worker_task_all
            await run_worker_diagnostics()
            await get_telethon_client()
            worker_lifecycle.state = WorkerState.RUNNING
            worker_lifecycle.bot_instance = application.bot
            global worker_running
            worker_running = True
            worker_task_all = asyncio.create_task(worker_lifecycle._worker_loop_core())

        async def post_shutdown(application: Application):
            nonlocal worker_task_all
            logger.info("[WORKER] Stopping worker task on application shutdown...")
            global worker_running
            worker_running = False
            if worker_task_all and not worker_task_all.done():
                worker_task_all.cancel()
                try:
                    await worker_task_all
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    logger.warning(f"Error awaiting worker task: {e}")
            if telethon_client:
                try:
                    if telethon_client.is_connected():
                        await telethon_client.disconnect()
                    logger.info("[WORKER] Telethon disconnected")
                except Exception as e:
                    logger.warning(f"Error disconnecting telethon: {e}")

        app.post_init = post_init
        app.post_shutdown = post_shutdown

    logger.info("[SERVICE_MODE] Initiating Telegram Bot API polling...")
    try:
        app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
    except telegram.error.InvalidToken as exc:
        logger.exception("Telegram polling startup failed due to InvalidToken. Web HTTP server remains active on port 3000.")
        while True:
            time.sleep(3600)
    except Exception as exc:
        logger.exception("Telegram polling startup failed. Web HTTP server remains active on port 3000.")
        while True:
            time.sleep(3600)

if __name__ == "__main__":
    main()
