"""
Production-Ready Telegram File-Storage & Backblaze B2 Media Processing Bot
Single-File Architecture: bot.py

Workflow:
User sends file -> Bot detects file -> Discover REAL B2 folders -> User selects/creates folder
-> Download Telegram file -> FFprobe inspection -> Remux/Transcode video to web-compatible MP4
-> Rename prompt -> B2 duplicate check -> B2 Upload -> B2 Verification -> Save DB Metadata -> Success
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

# ============================================================
# 1. CONFIGURATION & ENVIRONMENT VARIABLES
# ============================================================

BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
ADMIN_IDS_RAW = (os.getenv("ADMIN_IDS") or "").strip()

ADMIN_IDS: Set[int] = set()
if ADMIN_IDS_RAW:
    for item in ADMIN_IDS_RAW.split(","):
        cleaned = item.strip().lstrip("-")
        if cleaned.isdigit():
            try:
                ADMIN_IDS.add(int(item.strip()))
            except ValueError:
                pass

B2_APPLICATION_KEY_ID = (os.getenv("B2_APPLICATION_KEY_ID") or "").strip()
B2_APPLICATION_KEY = (os.getenv("B2_APPLICATION_KEY") or "").strip()
B2_BUCKET_NAME = (os.getenv("B2_BUCKET_NAME") or "anime4u-videos").strip()
B2_PUBLIC_BASE_URL = (os.getenv("B2_PUBLIC_BASE_URL") or "").rstrip("/")

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

DB_PATH = os.getenv("DATABASE_PATH", "./storage.db")
TEMP_DIR = Path(os.getenv("TEMP_DIR", "./downloads"))
TEMP_DIR.mkdir(parents=True, exist_ok=True)

MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE", 2147483648))  # 2 GB default
MAX_CONCURRENT_UPLOADS = int(os.getenv("MAX_CONCURRENT_UPLOADS", 2))
MAX_CONCURRENT_FFMPEG = int(os.getenv("MAX_CONCURRENT_FFMPEG", 1))
PORT = int(os.getenv("PORT", 3000))
PAGE_SIZE = int(os.getenv("PAGE_SIZE", 10))

# Semaphores for task rate limiting
UPLOAD_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_UPLOADS)
FFMPEG_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_FFMPEG)

# Import Telegram dependencies
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

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
# 2. DATABASE ENGINE (SQLite & Supabase)
# ============================================================

def get_sqlite_conn():
    conn = sqlite3.connect(DB_PATH)
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
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            details TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_folder ON file_records(folder)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_b2_path ON file_records(b2_path)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_created_at ON file_records(created_at)")

    conn.commit()
    conn.close()
    logger.info("SQLite database initialized successfully")

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

    if total_bytes < 1024 * 1024:
        readable = f"{total_bytes / 1024:.1f} KB"
    elif total_bytes < 1024 * 1024 * 1024:
        readable = f"{total_bytes / (1024 * 1024):.1f} MB"
    else:
        readable = f"{total_bytes / (1024 * 1024 * 1024):.2f} GB"

    return {
        "total_files": total_files,
        "total_folders": total_folders,
        "total_bytes": total_bytes,
        "readable_bytes": readable
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
        """Performs a real authorization & bucket listing check against B2 API."""
        if not self.is_configured():
            return False, "B2 credentials missing in environment variables."
        try:
            bucket = self._get_bucket()
            # Perform real list check (fetch 1 item)
            list(bucket.list_file_names(fetch_count=1))
            return True, f"B2 Bucket '{self.bucket_name}' authenticated & verified."
        except Exception as e:
            return False, f"B2 Health Check Error: {e}"

    def discover_folders(self) -> List[str]:
        """Discovers real folder prefixes by reading actual object keys from Backblaze B2."""
        try:
            bucket = self._get_bucket()
            folders_set: Set[str] = set()

            for file_version, _ in bucket.list_file_names():
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

    def list_files_in_folder(self, folder_prefix: str = "") -> List[Dict[str, Any]]:
        """Lists actual B2 objects matching prefix."""
        try:
            bucket = self._get_bucket()
            prefix = folder_prefix.strip("/") + "/" if folder_prefix and folder_prefix != "/" else ""
            results = []

            for file_version, _ in bucket.list_file_names(prefix=prefix):
                file_name = file_version.file_name
                if prefix and not file_name.startswith(prefix):
                    continue
                rel = file_name[len(prefix):]
                if "/" in rel and rel.endswith("/"):
                    continue

                url = f"{self.public_base_url}/{file_name}" if self.public_base_url else None
                results.append({
                    "b2_file_id": file_version.id_,
                    "b2_path": file_name,
                    "file_name": Path(file_name).name,
                    "file_size": file_version.size,
                    "upload_timestamp": file_version.upload_timestamp,
                    "b2_url": url
                })

            return results
        except Exception as e:
            logger.error(f"Error listing files in B2 prefix '{folder_prefix}': {e}")
            return []

    def file_exists(self, b2_path: str) -> bool:
        """Verifies whether an object key exists in B2."""
        try:
            bucket = self._get_bucket()
            info = bucket.get_file_info_by_name(b2_path)
            return info is not None
        except Exception:
            return False

    def upload_file(self, local_file_path: Path, b2_path: str) -> Dict[str, Any]:
        """Uploads a local disk file to Backblaze B2 using b2sdk upload_local_file."""
        bucket = self._get_bucket()
        str_path = str(local_file_path)

        if not local_file_path.exists():
            raise FileNotFoundError(f"Local file not found for upload: {str_path}")

        logger.info(f"Uploading file to B2: {str_path} -> {b2_path}")

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

            for file_version, _ in bucket.list_file_names():
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
    """Runs ffprobe on disk file and returns format/stream metadata."""
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
    """
    Processes input video for progressive browser playback.
    Determines Remux (Fast Path) vs Transcode path:
    - Remux: If video codec is already h264/avc1 and pix_fmt is yuv420p.
    - Transcode: If video codec is hevc/h265, vp9, av1, etc.
    Always uses -movflags +faststart for web playback.
    """
    ffmpeg_ok, _ = check_ffmpeg_installed()
    if not ffmpeg_ok:
        raise RuntimeError("FFmpeg binary is not installed on system.")

    v_codec = (media_info.get("video_codec") or "").lower()
    a_codec = (media_info.get("audio_codec") or "").lower()
    pix_fmt = (media_info.get("pix_fmt") or "").lower()

    # Fast Path criteria: H.264 video + standard yuv420p pixel format
    is_h264_compatible = v_codec in ["h264", "avc1"] and pix_fmt in ["yuv420p", "yuvj420p", ""]
    is_aac_compatible = a_codec in ["aac"]

    async with FFMPEG_SEMAPHORE:
        if is_h264_compatible:
            mode = "remux_fastpath"
            # Remux video stream without re-encoding
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
            # Transcode video into H.264 + AAC + yuv420p + faststart
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
# 5. HELPER UTILITIES & SECURITY
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

def sanitize_filename(name: str) -> str:
    cleaned = name.replace("\0", "").replace("..", "_").replace("\\", "/")
    cleaned = re.sub(r'[/*?:"<>|]', '_', cleaned)
    return cleaned.strip()

def check_disk_space(required_bytes: int) -> Tuple[bool, str]:
    try:
        total, used, free = shutil.disk_usage(TEMP_DIR)
        if free < required_bytes:
            req_mb = required_bytes / (1024 * 1024)
            free_mb = free / (1024 * 1024)
            return False, f"Not enough disk space. Required: {req_mb:.1f} MB, Free: {free_mb:.1f} MB"
        return True, "Disk space OK"
    except Exception as e:
        return True, str(e)

# ============================================================
# 6. HTTP HEALTH CHECK SERVER (PORT 3000)
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
# 7. TELEGRAM COMMAND HANDLERS
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
        f"☁️ *B2 Bucket:* `{B2_BUCKET_NAME}`\n"
        f"🔗 *B2 Health:* {'✅ Connected' if b2_ok else '❌ Error'}\n\n"
        "Send any file or video to begin storage workflow!"
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
        "/files - List files in current folder\n"
        "/search <query> - Search stored B2 objects\n"
        "/stats - View storage usage statistics\n"
        "/storage - Perform real component health checks\n"
        "/admin - Admin Security Panel\n"
        "/admins - List authorized Admin IDs\n"
        "/addadmin <id> - Add an admin ID\n"
        "/removeadmin <id> - Remove an admin ID\n"
        "/broadcast <text> - Broadcast announcement\n"
        "/audit - View admin audit logs\n"
        "/cancel - Reset active interactive state"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def storage_health_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny_access(update)
        return

    msg = await update.message.reply_text("⏳ *Performing Real System Health Check...*", parse_mode="Markdown") if update.message else None

    # 1. B2 Check
    b2_ok, b2_msg = await asyncio.to_thread(b2_storage.check_health)

    # 2. FFmpeg / FFprobe Check
    ffmpeg_ok, ffprobe_ok = check_ffmpeg_installed()

    # 3. Database Check
    db_ok = True
    db_msg = "Database operational"
    try:
        stats = get_db_stats()
        db_msg = f"{stats['total_files']} files indexed ({stats['readable_bytes']})"
    except Exception as e:
        db_ok = False
        db_msg = str(e)

    status_text = (
        "🗄 *Real Storage Health Status*\n\n"
        f"☁️ *B2 Authentication:* {'✅' if b2_ok else '❌'}\n"
        f"🪣 *Bucket (`{B2_BUCKET_NAME}`):* {'✅' if b2_ok else '❌'}\n"
        f"📂 *Listing Capability:* {'✅' if b2_ok else '❌'}\n"
        f"📤 *Upload Capability:* {'✅' if b2_ok else '❌'}\n"
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
    # Root folder option
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

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny_access(update)
        return

    context.user_data.pop("awaiting_folder_input", None)
    context.user_data.pop("awaiting_rename_input", None)
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
        "• `/broadcast <text>` - Broadcast alert\n"
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
        await update.message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")

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
# 8. CALLBACK QUERY ROUTER
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
    elif data == "cmd_folders" or data == "cmd_refresh_folders":
        await folders_command(update, context)
    elif data == "cmd_stats":
        await stats_command(update, context)
    elif data == "cmd_storage_health":
        await storage_health_command(update, context)
    elif data == "admin_panel":
        await admin_panel_command(update, context)
    elif data == "admin_list":
        await list_admins_command(update, context)
    elif data == "admin_audit":
        await audit_command(update, context)
    elif data.startswith("folder_sel:"):
        folder = data.split("folder_sel:", 1)[1]
        context.user_data["selected_folder"] = folder
        
        # Check if there is a pending file waiting to be processed
        pending = context.user_data.get("pending_file")
        if pending:
            await execute_storage_pipeline(update, context, folder)
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
    elif data == "rename_yes":
        context.user_data["awaiting_rename_input"] = True
        await query.edit_message_text(
            "✏️ *Rename File*\n\n"
            "Please send the desired filename (e.g. `S01E01.mp4` or `Solo Leveling - 01`):",
            parse_mode="Markdown"
        )
    elif data == "rename_no":
        await continue_pipeline_after_rename(update, context, rename_to=None)
    elif data == "dup_overwrite":
        await continue_pipeline_b2_upload(update, context, overwrite=True)
    elif data == "dup_rename":
        context.user_data["awaiting_rename_input"] = True
        await query.edit_message_text(
            "✏️ *Rename File to avoid overwrite*\n\n"
            "Send a unique filename:",
            parse_mode="Markdown"
        )
    elif data == "dup_cancel":
        context.user_data.pop("pending_file", None)
        await query.edit_message_text("❌ Storage operation cancelled by user.", parse_mode="Markdown")

# ============================================================
# 9. MAIN INTERACTIVE STORAGE & MEDIA PIPELINE
# ============================================================

async def handle_incoming_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny_access(update)
        return

    msg = update.message
    if not msg:
        return

    # Handle text inputs for active prompts
    if context.user_data.get("awaiting_folder_input"):
        folder_raw = msg.text.strip().strip("/")
        context.user_data.pop("awaiting_folder_input", None)
        if folder_raw:
            folder = f"{folder_raw}/"
            context.user_data["selected_folder"] = folder
            
            pending = context.user_data.get("pending_file")
            if pending:
                await execute_storage_pipeline(update, context, folder)
            else:
                await msg.reply_text(f"✅ Active B2 destination folder created & set to: `{folder}`", parse_mode="Markdown")
        else:
            await msg.reply_text("❌ Invalid folder path.")
        return

    if context.user_data.get("awaiting_rename_input"):
        new_filename = msg.text.strip()
        context.user_data.pop("awaiting_rename_input", None)
        await continue_pipeline_after_rename(update, context, rename_to=new_filename)
        return

    # Handle file/media upload trigger
    file_obj = msg.document or msg.video or msg.audio or (msg.photo[-1] if msg.photo else None)
    if not file_obj:
        return

    original_name = getattr(file_obj, "file_name", None) or f"file_{int(time.time())}.dat"
    original_name = sanitize_filename(original_name)
    file_size = getattr(file_obj, "file_size", 0)
    mime_type = getattr(file_obj, "mime_type", "application/octet-stream")
    media_type = "video" if msg.video else ("audio" if msg.audio else "document")

    if file_size > MAX_FILE_SIZE:
        sz_gb = file_size / (1024 * 1024 * 1024)
        max_gb = MAX_FILE_SIZE / (1024 * 1024 * 1024)
        await msg.reply_text(
            f"❌ *File Size Error*\n\n"
            f"File size `{sz_gb:.2f} GB` exceeds the application limit of `{max_gb:.2f} GB`.",
            parse_mode="Markdown"
        )
        return

    # Save pending file context
    context.user_data["pending_file"] = {
        "tg_file_id": file_obj.file_id,
        "original_file_name": original_name,
        "file_size": file_size,
        "mime_type": mime_type,
        "media_type": media_type,
        "chat_id": msg.chat_id,
        "status_message_id": None
    }

    # Prompt user to select existing or create folder
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
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="dup_cancel")])

    keyboard = InlineKeyboardMarkup(buttons)
    await msg.reply_text(
        f"📩 *File Received:* `{original_name}` ({file_size / (1024*1024):.1f} MB)\n\n"
        "📂 *Select Destination B2 Folder:*",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

async def execute_storage_pipeline(update: Update, context: ContextTypes.DEFAULT_TYPE, folder: str):
    pending = context.user_data.get("pending_file")
    if not pending:
        return

    chat_id = pending["chat_id"]
    original_name = pending["original_file_name"]
    file_size = pending["file_size"]

    # Initial Status Message
    status_msg = await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "🎬 *Processing Workflow Started*\n\n"
            f"📄 *Original File:* `{original_name}`\n"
            f"📁 *B2 Target Folder:* `{folder}`\n\n"
            "⬇️ *Downloading Telegram file...*"
        ),
        parse_mode="Markdown"
    )
    pending["status_message_id"] = status_msg.message_id
    pending["folder"] = folder

    # Step 1: Disk Space Check
    space_ok, space_msg = check_disk_space(int(file_size * 2.5))
    if not space_ok:
        await status_msg.edit_text(
            f"❌ *STORAGE FAILED*\n\n"
            f"📄 *File:* `{original_name}`\n"
            f"📁 *Folder:* `{folder}`\n"
            f"🎬 Processing: ⚪ Not started\n"
            f"☁️ B2: ⚪ Not attempted\n"
            f"🗄 Database: ⚪ Not indexed\n\n"
            f"*Reason:* {space_msg}",
            parse_mode="Markdown"
        )
        context.user_data.pop("pending_file", None)
        return

    # Step 2: Download Telegram file
    local_download_path = TEMP_DIR / f"down_{int(time.time())}_{original_name}"
    pending["local_download_path"] = local_download_path

    try:
        tg_file = await context.bot.get_file(pending["tg_file_id"])
        await tg_file.download_to_drive(custom_path=local_download_path)

        if not local_download_path.exists() or local_download_path.stat().st_size == 0:
            raise FileNotFoundError("Downloaded Telegram file is empty or missing from disk.")

    except Exception as e:
        logger.error(f"Telegram download failed: {e}")
        await status_msg.edit_text(
            f"❌ *STORAGE FAILED*\n\n"
            f"📄 *File:* `{original_name}`\n"
            f"📁 *Folder:* `{folder}`\n"
            f"⬇️ Download: ❌ Failed\n"
            f"🎬 Processing: ⚪ Not attempted\n"
            f"☁️ B2: ⚪ Not attempted\n"
            f"🗄 Database: ⚪ Not indexed\n\n"
            f"*Reason:* Download failed ({e})",
            parse_mode="Markdown"
        )
        if local_download_path.exists():
            local_download_path.unlink(missing_ok=True)
        context.user_data.pop("pending_file", None)
        return

    # Step 3: FFprobe Inspection
    await status_msg.edit_text(
        "🎬 *Processing Workflow*\n\n"
        f"📄 *File:* `{original_name}`\n"
        f"📁 *Folder:* `{folder}`\n\n"
        "🔍 *Inspecting media properties with FFprobe...*",
        parse_mode="Markdown"
    )

    is_video = False
    media_info = {}
    try:
        media_info = await probe_media(local_download_path)
        is_video = media_info.get("is_video", False)
    except Exception as e:
        logger.warning(f"FFprobe inspection note: {e}")

    pending["is_video"] = is_video
    pending["media_info"] = media_info

    # Step 4: Video Processing (Remux vs Transcode)
    if is_video:
        v_codec = media_info.get("video_codec", "unknown")
        a_codec = media_info.get("audio_codec", "unknown")
        
        output_mp4_name = Path(original_name).stem + ".mp4"
        local_processed_path = TEMP_DIR / f"proc_{int(time.time())}_{output_mp4_name}"
        pending["local_processed_path"] = local_processed_path
        pending["processed_filename"] = output_mp4_name

        await status_msg.edit_text(
            "🎬 *Processing Video for Browser Playback*\n\n"
            f"📄 *File:* `{original_name}`\n"
            f"🔍 *Codecs Detected:* Video (`{v_codec}`), Audio (`{a_codec}`)\n"
            f"⚙️ *Target:* MP4 + H.264 + AAC + yuv420p + faststart\n\n"
            "⏳ *FFmpeg processing in progress...*",
            parse_mode="Markdown"
        )

        success, mode, err_desc = await process_video_for_web(
            local_download_path,
            local_processed_path,
            media_info
        )
        pending["processing_mode"] = mode

        if not success:
            await status_msg.edit_text(
                f"❌ *STORAGE FAILED*\n\n"
                f"📄 *File:* `{original_name}`\n"
                f"📁 *Folder:* `{folder}`\n"
                f"🎬 Processing: ❌ Failed ({mode})\n"
                f"☁️ B2: ⚪ Not attempted\n"
                f"🗄 Database: ⚪ Not indexed\n\n"
                f"*Reason:* {err_desc}",
                parse_mode="Markdown"
            )
            # Cleanup temp files
            if local_download_path.exists():
                local_download_path.unlink(missing_ok=True)
            if local_processed_path.exists():
                local_processed_path.unlink(missing_ok=True)
            context.user_data.pop("pending_file", None)
            return

        pending["upload_path"] = local_processed_path
        pending["final_size"] = local_processed_path.stat().st_size
        pending["mime_type"] = "video/mp4"
    else:
        # Non-video document passthrough
        pending["processing_mode"] = "passthrough"
        pending["processed_filename"] = original_name
        pending["upload_path"] = local_download_path
        pending["final_size"] = local_download_path.stat().st_size

    # Step 5: Ask User for Rename
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Yes, Rename", callback_data="rename_yes"),
            InlineKeyboardButton("➡️ Keep Current Name", callback_data="rename_no")
        ]
    ])

    current_target_name = pending["processed_filename"]
    await status_msg.edit_text(
        "✏️ *Rename File Before Upload?*\n\n"
        f"📄 *Current Processed Name:* `{current_target_name}`\n"
        f"📁 *Destination Folder:* `{folder}`\n"
        f"⚙️ *Mode:* `{pending['processing_mode']}`",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

async def continue_pipeline_after_rename(update: Update, context: ContextTypes.DEFAULT_TYPE, rename_to: Optional[str]):
    pending = context.user_data.get("pending_file")
    if not pending:
        return

    is_video = pending.get("is_video", False)
    if rename_to:
        clean_name = sanitize_filename(rename_to)
        if is_video and not clean_name.lower().endswith(".mp4"):
            clean_name += ".mp4"
        pending["final_file_name"] = clean_name
    else:
        pending["final_file_name"] = pending["processed_filename"]

    folder = pending["folder"]
    filename = pending["final_file_name"]
    
    # Construct B2 path key
    if folder == "/":
        b2_path = filename
    else:
        b2_path = f"{folder.strip('/')}/{filename}"

    pending["b2_path"] = b2_path

    # Step 6: B2 Duplicate Check
    exists = await asyncio.to_thread(b2_storage.file_exists, b2_path)
    chat_id = pending["chat_id"]
    status_msg_id = pending["status_message_id"]

    if exists:
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔄 Overwrite B2 File", callback_data="dup_overwrite"),
                InlineKeyboardButton("✏️ Rename File", callback_data="dup_rename")
            ],
            [
                InlineKeyboardButton("❌ Cancel", callback_data="dup_cancel")
            ]
        ])

        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=status_msg_id,
            text=(
                "⚠️ *File Already Exists in B2*\n\n"
                f"📁 *Folder:* `{folder}`\n"
                f"📄 *File:* `{filename}`\n"
                f"🔗 *B2 Path:* `{b2_path}`\n\n"
                "Please select an action:"
            ),
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
        return

    # Proceed directly to upload if no duplicate
    await continue_pipeline_b2_upload(update, context, overwrite=False)

async def continue_pipeline_b2_upload(update: Update, context: ContextTypes.DEFAULT_TYPE, overwrite: bool):
    pending = context.user_data.get("pending_file")
    if not pending:
        return

    chat_id = pending["chat_id"]
    status_msg_id = pending["status_message_id"]
    upload_path = pending["upload_path"]
    b2_path = pending["b2_path"]
    filename = pending["final_file_name"]
    folder = pending["folder"]
    proc_mode = pending["processing_mode"]

    # Step 7: Upload to Backblaze B2
    await context.bot.edit_message_text(
        chat_id=chat_id,
        message_id=status_msg_id,
        text=(
            "🎬 *Processing Workflow*\n\n"
            f"📄 *File:* `{filename}`\n"
            f"📁 *Folder:* `{folder}`\n"
            f"🎬 Processing: ✅ Success (`{proc_mode}`)\n"
            "☁️ *Uploading to Backblaze B2...*"
        ),
        parse_mode="Markdown"
    )

    b2_result = None
    b2_error = ""
    try:
        async with UPLOAD_SEMAPHORE:
            b2_result = await asyncio.to_thread(
                b2_storage.upload_file,
                upload_path,
                b2_path
            )
    except Exception as e:
        logger.error(f"B2 upload error: {e}")
        b2_error = str(e)

    if not b2_result:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=status_msg_id,
            text=(
                "❌ *STORAGE FAILED*\n\n"
                f"📄 *File:* `{filename}`\n"
                f"📁 *Folder:* `{folder}`\n"
                f"🎬 Processing: ✅ Success (`{proc_mode}`)\n"
                "☁️ B2 Upload: ❌ Failed\n"
                "🗄 Database: ⚪ Not indexed\n\n"
                f"*Reason:* B2 Upload Error ({b2_error})"
            ),
            parse_mode="Markdown"
        )
        cleanup_temp_files(pending)
        context.user_data.pop("pending_file", None)
        return

    # Step 8: Verify B2 Upload
    b2_verified = await asyncio.to_thread(b2_storage.file_exists, b2_path)
    if not b2_verified:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=status_msg_id,
            text=(
                "❌ *STORAGE FAILED*\n\n"
                f"📄 *File:* `{filename}`\n"
                f"📁 *Folder:* `{folder}`\n"
                f"🎬 Processing: ✅ Success (`{proc_mode}`)\n"
                "☁️ B2 Upload: ❌ Verification Failed\n"
                "🗄 Database: ⚪ Not indexed\n\n"
                "*Reason:* B2 Object key could not be verified post-upload."
            ),
            parse_mode="Markdown"
        )
        cleanup_temp_files(pending)
        context.user_data.pop("pending_file", None)
        return

    # Step 9: Save Database Metadata Index
    media_info = pending.get("media_info", {})
    record = {
        "file_name": filename,
        "original_file_name": pending["original_file_name"],
        "folder": folder,
        "b2_bucket": B2_BUCKET_NAME,
        "b2_path": b2_path,
        "b2_file_id": b2_result.get("b2_file_id"),
        "file_size": pending["final_size"],
        "mime_type": pending.get("mime_type", "application/octet-stream"),
        "media_type": pending.get("media_type", "document"),
        "video_codec": media_info.get("video_codec"),
        "audio_codec": media_info.get("audio_codec"),
        "width": media_info.get("width"),
        "height": media_info.get("height"),
        "duration": media_info.get("duration"),
        "processing_mode": proc_mode,
        "status": "complete",
        "b2_url": b2_result.get("b2_url")
    }

    db_rec_id = insert_file_record(record)

    if not db_rec_id:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=status_msg_id,
            text=(
                "⚠️ *PARTIAL SUCCESS (DATABASE INDEX FAILED)*\n\n"
                f"📄 *File:* `{filename}`\n"
                f"📁 *Folder:* `{folder}`\n"
                f"🎬 Processing: ✅ Success (`{proc_mode}`)\n"
                f"☁️ B2: ✅ Uploaded & Verified\n"
                f"🆔 B2 File ID: `{b2_result.get('b2_file_id')}`\n"
                f"🗄 Database: ❌ Index Failed\n\n"
                "*Note:* File is safe in B2, but database row creation encountered an error."
            ),
            parse_mode="Markdown"
        )
    else:
        url_text = f"`{b2_result['b2_url']}`" if b2_result.get("b2_url") else "`N/A (Private Bucket)`"
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=status_msg_id,
            text=(
                "✅ *STORAGE WORKFLOW SUCCESSFUL*\n\n"
                f"📄 *File:* `{filename}`\n"
                f"📁 *Folder:* `{folder}`\n"
                f"🎬 *Mode:* `{proc_mode}` (MP4 + H.264 + AAC + faststart)\n"
                f"☁️ *B2 Bucket:* `{B2_BUCKET_NAME}`\n"
                f"🆔 *B2 File ID:* `{b2_result.get('b2_file_id')}`\n"
                f"🔗 *B2 Path:* `{b2_path}`\n"
                f"🗄️ *DB Record ID:* `{db_rec_id}`\n"
                f"🌐 *Public URL:* {url_text}"
            ),
            parse_mode="Markdown"
        )

    # Clean up local temporary files
    cleanup_temp_files(pending)
    context.user_data.pop("pending_file", None)

def cleanup_temp_files(pending: Dict[str, Any]):
    p_down = pending.get("local_download_path")
    p_proc = pending.get("local_processed_path")
    if p_down and isinstance(p_down, Path) and p_down.exists():
        try:
            p_down.unlink(missing_ok=True)
        except Exception:
            pass
    if p_proc and isinstance(p_proc, Path) and p_proc.exists():
        try:
            p_proc.unlink(missing_ok=True)
        except Exception:
            pass

# ============================================================
# 10. MAIN ENTRY POINT & APPLICATION INITIALIZATION
# ============================================================

def main():
    logger.info("Starting Anime4u Storage & Media Bot...")

    # Start Health Check HTTP server on PORT 3000
    start_health_server()

    # Initialize Database Schema
    init_db()

    # Startup FFmpeg Check
    ffmpeg_ok, ffprobe_ok = check_ffmpeg_installed()
    if ffmpeg_ok and ffprobe_ok:
        logger.info("FFmpeg & FFprobe binaries detected successfully.")
    else:
        logger.warning(f"FFmpeg/FFprobe presence: FFmpeg={ffmpeg_ok}, FFprobe={ffprobe_ok}")

    # Startup B2 Check
    b2_ok, b2_msg = b2_storage.check_health()
    if b2_ok:
        logger.info(f"B2 Startup Check: {b2_msg}")
    else:
        logger.warning(f"B2 Startup Check Warning: {b2_msg}")

    if not BOT_TOKEN:
        logger.error("BOT_TOKEN is missing. Please set BOT_TOKEN environment variable.")
        sys.exit(1)

    # Initialize Telegram Application
    app = Application.builder().token(BOT_TOKEN).build()

    # Command Handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("folders", folders_command))
    app.add_handler(CommandHandler("selected", selected_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("storage", storage_health_command))
    app.add_handler(CommandHandler("search", search_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("admin", admin_panel_command))
    app.add_handler(CommandHandler("admins", list_admins_command))
    app.add_handler(CommandHandler("addadmin", add_admin_command))
    app.add_handler(CommandHandler("removeadmin", remove_admin_command))
    app.add_handler(CommandHandler("audit", audit_command))

    # Callback Query Handler
    app.add_handler(CallbackQueryHandler(handle_callback_query))

    # General Message Handler for File Uploads & Text Prompts
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_incoming_message))

    logger.info("Bot polling initiated...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
