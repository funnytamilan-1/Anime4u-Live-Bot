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
import logging
import asyncio
import sqlite3
import shutil
import uuid
from pathlib import Path
from datetime import datetime
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
    TELETHON_AVAILABLE = True
except ImportError:
    TELETHON_AVAILABLE = False
    logger.warning("Telethon library is not installed. MTProto large file downloading will be unavailable.")

# ============================================================
# 1. CONFIGURATION & ENVIRONMENT VARIABLES
# ============================================================

BOT_TOKEN = (os.getenv("BOT_TOKEN") or "8769661029:AAED5_SSFoU-Q_xQ_-p-x5FqzU7J9MZcIaE").strip()
API_ID_RAW = (os.getenv("API_ID") or "27806628").strip()
API_HASH = (os.getenv("API_HASH") or "25d88301e886b82826a525b7cf52e090").strip()
TELEGRAM_SESSION_STRING = (os.getenv("TELEGRAM_SESSION_STRING") or "").strip()

API_ID: Optional[int] = None
if API_ID_RAW and API_ID_RAW.isdigit():
    API_ID = int(API_ID_RAW)

SERVICE_MODE = (os.getenv("SERVICE_MODE") or "all").strip().lower()  # 'render', 'worker', or 'all'

ADMIN_IDS_RAW = (os.getenv("ADMIN_IDS") or "5192451273, 8525952693").strip()
ADMIN_IDS: Set[int] = set()
if ADMIN_IDS_RAW:
    for item in ADMIN_IDS_RAW.split(","):
        cleaned = item.strip().lstrip("-")
        if cleaned.isdigit():
            try:
                ADMIN_IDS.add(int(item.strip()))
            except ValueError:
                pass

B2_APPLICATION_KEY_ID = (os.getenv("B2_APPLICATION_KEY_ID") or "0054467fa469dc20000000002").strip()
B2_APPLICATION_KEY = (os.getenv("B2_APPLICATION_KEY") or "K005ACvE5pP0RYQr4cplDYDSE96uMtA").strip()
B2_BUCKET_NAME = (os.getenv("B2_BUCKET_NAME") or "anime4u-videos").strip()
B2_PUBLIC_BASE_URL = (os.getenv("B2_PUBLIC_BASE_URL") or "").rstrip("/")

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

DB_PATH = os.getenv("DATABASE_PATH", "./storage.db")
TEMP_DIR = Path(os.getenv("TEMP_DIR", "./downloads"))
TEMP_DIR.mkdir(parents=True, exist_ok=True)

MAX_FILE_SIZE_BYTES = int(os.getenv("MAX_FILE_SIZE_BYTES") or os.getenv("MAX_FILE_SIZE") or (4 * 1024 * 1024 * 1024))  # 4 GiB = 4,294,967,296 bytes
MAX_CONCURRENT_UPLOADS = int(os.getenv("MAX_CONCURRENT_UPLOADS", 2))
MAX_CONCURRENT_FFMPEG = int(os.getenv("MAX_CONCURRENT_FFMPEG", 1))
PORT = int(os.getenv("PORT", 3000))
PAGE_SIZE = int(os.getenv("PAGE_SIZE", 10))

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

# Global Telethon Client Reference
telethon_client: Optional[Any] = None

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
    if supabase_client:
        return

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
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)

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

def get_next_queued_job() -> Optional[Dict[str, Any]]:
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

    def _get_bucket(self):
        with self._lock:
            if self._bucket:
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
                logger.error(f"Failed to authenticate with Backblaze B2: {e}")
                raise

    def check_health(self) -> Tuple[bool, str]:
        if not self.is_configured():
            return False, "B2 credentials missing in environment variables."
        try:
            bucket = self._get_bucket()
            generator = bucket.ls(fetch_count=1)
            next(generator, None)
            return True, f"B2 Bucket '{self.bucket_name}' authenticated & verified."
        except Exception as e:
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
    media_info: Dict[str, Any]
) -> Tuple[bool, str, str]:
    ffmpeg_ok, _ = check_ffmpeg_installed()
    if not ffmpeg_ok:
        raise RuntimeError("FFmpeg binary is not installed on system.")

    v_codec = (media_info.get("video_codec") or "").lower()
    a_codec = (media_info.get("audio_codec") or "").lower()
    pix_fmt = (media_info.get("pix_fmt") or "").lower()

    is_h264_compatible = v_codec in ["h264", "avc1"] and pix_fmt in ["yuv420p", "yuvj420p", ""]
    is_aac_compatible = a_codec in ["aac"]

    async with FFMPEG_SEMAPHORE:
        if is_h264_compatible:
            mode = "remux_fastpath"
            cmd = [
                "ffmpeg", "-y",
                "-i", str(input_path),
                "-c:v", "copy",
                "-c:a", "copy" if is_aac_compatible else "aac",
            ]
            if not is_aac_compatible and a_codec:
                cmd.extend(["-b:a", "128k"])
            cmd.extend(["-movflags", "+faststart", str(output_path)])
        else:
            mode = "transcode_h264"
            cmd = [
                "ffmpeg", "-y",
                "-i", str(input_path),
                "-c:v", "libx264",
                "-preset", "medium",
                "-crf", "23",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac",
                "-b:a", "128k",
                "-movflags", "+faststart",
                str(output_path)
            ]

        logger.info(f"Executing FFmpeg [{mode}]: {' '.join(cmd)}")

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        _, stderr = await proc.communicate()

        if proc.returncode != 0:
            err_log = stderr.decode("utf-8", errors="ignore")[-500:]
            logger.error(f"FFmpeg process error: {err_log}")
            return False, mode, f"FFmpeg failed (code {proc.returncode}): {err_log}"

        if not output_path.exists() or output_path.stat().st_size == 0:
            return False, mode, "FFmpeg output file is missing or 0 bytes."

        return True, mode, "Success"

# ============================================================
# 5. MTPROTO TELETHON CLIENT & DOWNLOAD ENGINE
# ============================================================

async def get_telethon_client() -> Optional[Any]:
    global telethon_client
    if not TELETHON_AVAILABLE:
        return None

    if telethon_client and telethon_client.is_connected():
        return telethon_client

    if not API_ID or not API_HASH:
        logger.warning("MTProto API_ID or API_HASH is missing. Telethon MTProto client cannot start.")
        return None

    try:
        if TELEGRAM_SESSION_STRING:
            logger.info("Initializing Telethon client with TELEGRAM_SESSION_STRING...")
            session = StringSession(TELEGRAM_SESSION_STRING)
            telethon_client = TelegramClient(session, API_ID, API_HASH)
            await telethon_client.connect()
            if not await telethon_client.is_user_authorized():
                logger.error("TELEGRAM_SESSION_STRING is invalid or not authorized.")
                return None
        else:
            logger.info("Initializing Telethon client with Bot Token...")
            session = StringSession()
            telethon_client = TelegramClient(session, API_ID, API_HASH)
            await telethon_client.start(bot_token=BOT_TOKEN)

        logger.info("Telethon MTProto client initialized & connected successfully!")
        return telethon_client
    except Exception as e:
        logger.error(f"Failed to initialize Telethon MTProto client: {e}")
        return None

async def download_telegram_file_mtproto(
    chat_id: int,
    message_id: int,
    output_path: Path,
    progress_callback=None
) -> bool:
    client = await get_telethon_client()
    if not client:
        raise RuntimeError("MTProto client is not configured or unavailable. Set API_ID and API_HASH.")

    msg = await client.get_messages(chat_id, ids=message_id)
    if not msg or not msg.media:
        raise RuntimeError(f"Telegram message {message_id} in chat {chat_id} not found or contains no media via MTProto.")

    last_progress_time = [0.0]

    def raw_progress_cb(current: int, total: int):
        now = time.time()
        if now - last_progress_time[0] >= 3.0 or current == total:
            last_progress_time[0] = now
            if progress_callback:
                asyncio.run_coroutine_threadsafe(
                    progress_callback(current, total),
                    loop=asyncio.get_event_loop()
                )

    await client.download_media(
        msg,
        file=str(output_path),
        progress_callback=raw_progress_cb if progress_callback else None
    )

    return output_path.exists() and output_path.stat().st_size > 0

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

worker_running = False
worker_lock = asyncio.Lock()

async def run_worker_loop(ptb_app: Optional[Application] = None):
    global worker_running
    worker_running = True
    logger.info("MTProto Worker Loop started...")

    while worker_running:
        try:
            job = get_next_queued_job()
            if not job:
                await asyncio.sleep(2.0)
                continue

            async with worker_lock:
                await process_single_job(job, ptb_app)

        except asyncio.CancelledError:
            logger.info("Worker loop cancelled.")
            break
        except Exception as e:
            logger.error(f"Error in MTProto worker loop: {e}", exc_info=e)
            await asyncio.sleep(3.0)

async def process_single_job(job: Dict[str, Any], ptb_app: Optional[Application]):
    job_id = job["job_id"]
    chat_id = job["chat_id"]
    message_id = job["message_id"]
    status_msg_id = job.get("status_message_id")
    original_filename = job["original_filename"]
    file_size = job["source_file_size"]
    folder = job["folder"]

    logger.info(f"Worker picking up job {job_id} for file '{original_filename}' ({format_size(file_size)})")
    update_job(job_id, {"status": "DOWNLOADING", "progress_stage": "DOWNLOADING", "progress_percent": 0})

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

    await notify_ui(
        "🎬 *Processing Workflow Started*\n\n"
        f"📄 *Original File:* `{original_filename}` ({format_size(file_size)})\n"
        f"📁 *B2 Target Folder:* `{folder}`\n\n"
        "⬇️ *Downloading Telegram file via MTProto...*"
    )

    # 1. Disk Space Check
    required_bytes = int(file_size * 2.2)
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
        if API_ID and API_HASH and TELETHON_AVAILABLE:
            async def download_cb(current: int, total: int):
                pct = int((current / total) * 100) if total else 0
                update_job(job_id, {"progress_percent": pct})
                await notify_ui(
                    "⬇️ *Downloading Telegram file via MTProto...*\n\n"
                    f"📄 *File:* `{original_filename}`\n"
                    f"📊 *Progress:* `{pct}%` (`{format_size(current)}` / `{format_size(total)}`)"
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

        # 3. FFprobe Inspection
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

        # 4. Processing (Remux / Transcode vs Passthrough)
        if is_video:
            v_codec = media_info.get("video_codec", "unknown")
            a_codec = media_info.get("audio_codec", "unknown")
            output_mp4_name = Path(original_filename).stem + ".mp4"
            local_processed_path = TEMP_DIR / f"proc_{job_id}_{output_mp4_name}"

            await notify_ui(
                "🎬 *Processing Video for Browser Playback*\n\n"
                f"📄 *File:* `{original_filename}`\n"
                f"🔍 *Codecs:* Video (`{v_codec}`), Audio (`{a_codec}`)\n"
                f"⚙️ *Target:* MP4 + H.264 + AAC + yuv420p + faststart\n\n"
                "⏳ *FFmpeg processing in progress...*"
            )

            success, mode, err_desc = await process_video_for_web(
                local_download_path,
                local_processed_path,
                media_info
            )

            if not success:
                raise RuntimeError(f"FFmpeg processing failed ({mode}): {err_desc}")

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

def check_disk_space(required_bytes: int) -> Tuple[bool, str]:
    try:
        total, used, free = shutil.disk_usage(TEMP_DIR)
        if free < required_bytes:
            return False, (
                f"Insufficient temporary disk space.\n"
                f"• Required: {required_bytes} bytes ({format_size(required_bytes)})\n"
                f"• Available: {free} bytes ({format_size(free)})"
            )
        return True, "Disk space OK"
    except Exception as e:
        return True, str(e)

# ============================================================
# 8. HTTP HEALTH CHECK SERVER (PORT 3000)
# ============================================================

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status": "ok", "service": "b2-telegram-storage-bot"}')

    def log_message(self, format, *args):
        pass

def start_health_server():
    try:
        server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        logger.info(f"Health check HTTP server listening on port {PORT}")
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

            await query.edit_message_text(
                "🎬 *Job Queued Successfully*\n\n"
                f"📄 *File:* `{pending['original_filename']}` ({format_size(pending['source_file_size'])})\n"
                f"📁 *B2 Target Folder:* `{folder}`\n"
                f"🆔 *Job ID:* `{job_id}`\n\n"
                "⏳ *Queued for MTProto worker processing...*",
                parse_mode="Markdown"
            )
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
                await msg.reply_text(
                    "🎬 *Job Queued Successfully*\n\n"
                    f"📄 *File:* `{pending['original_filename']}` ({format_size(pending['source_file_size'])})\n"
                    f"📁 *B2 Target Folder:* `{folder}`\n"
                    f"🆔 *Job ID:* `{job_id}`\n\n"
                    "⏳ *Queued for MTProto worker processing...*",
                    parse_mode="Markdown"
                )
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

async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    if isinstance(err, telegram.error.Conflict):
        logger.warning(
            "Telegram getUpdates Conflict: Another bot instance is currently active with this BOT_TOKEN. "
            "If deploying on Render / cloud, the previous container instance will terminate shortly and polling will resume automatically."
        )
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

    if not BOT_TOKEN:
        logger.error("BOT_TOKEN environment variable is missing.")
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

    # Mode-based Execution
    if SERVICE_MODE in ["worker", "all"]:
        # Launch worker loop as background task inside PTB event loop or standalone loop
        async def post_init(application: Application):
            asyncio.create_task(run_worker_loop(application))

        app.post_init = post_init

    logger.info("Bot polling initiated...")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
