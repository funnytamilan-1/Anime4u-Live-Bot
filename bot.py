"""
Anime4u Telegram Storage & Backblaze B2 Multi-Backend Bot
Single-File Production Implementation
"""

import os
import sys
import re
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

# Load environment variables if python-dotenv available
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Setup logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("Anime4uBot")

# ============================================================
# CONFIGURATION & ENVIRONMENT VARIABLES
# ============================================================

BOT_TOKEN = (os.getenv("BOT_TOKEN") or "8769661029:AAED5_SSFoU-Q_xQ_-p-x5FqzU7J9MZcIaE").strip()
STORAGE_CHANNEL_ID_RAW = (os.getenv("STORAGE_CHANNEL_ID") or "-1004364304959").strip()
ADMIN_IDS_RAW = (os.getenv("ADMIN_IDS") or "8525952693,5192451273").strip()

try:
    STORAGE_CHANNEL_ID = int(STORAGE_CHANNEL_ID_RAW) if STORAGE_CHANNEL_ID_RAW else 0
except ValueError:
    STORAGE_CHANNEL_ID = 0

ADMIN_IDS: Set[int] = set()
for x in ADMIN_IDS_RAW.split(","):
    cleaned = x.strip().lstrip("-")
    if cleaned.isdigit():
        try:
            ADMIN_IDS.add(int(x.strip()))
        except ValueError:
            pass

STORAGE_MODE = (os.getenv("STORAGE_MODE") or "telegram").lower().strip()
B2_APPLICATION_KEY_ID = (os.getenv("B2_APPLICATION_KEY_ID") or "0054467fa469dc20000000002").strip()
B2_APPLICATION_KEY = (os.getenv("B2_APPLICATION_KEY") or "K005ACvE5pP0RYQr4cplDYDSE96uMtA").strip()
B2_BUCKET_NAME = (os.getenv("B2_BUCKET_NAME") or "anime4u-videos").strip()
B2_PUBLIC_BASE_URL = (os.getenv("B2_PUBLIC_BASE_URL") or "").rstrip("/")

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

DB_PATH = os.getenv("DATABASE_PATH", "./storage.db")
TEMP_DIR = Path(os.getenv("TEMP_DIR", "./downloads"))
TEMP_DIR.mkdir(parents=True, exist_ok=True)

MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE", 2147483648))  # 2 GB default
MAX_CONCURRENT_UPLOADS = int(os.getenv("MAX_CONCURRENT_UPLOADS", 2))
PORT = int(os.getenv("PORT", 3000))
PAGE_SIZE = int(os.getenv("PAGE_SIZE", 10))

# Semaphore for upload concurrency
UPLOAD_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_UPLOADS)

# Global active mode variable
GLOBAL_STORAGE_MODE = STORAGE_MODE if STORAGE_MODE in ["telegram", "b2", "both"] else "telegram"

# Import Telegram libraries
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# Import Supabase if configured
supabase_client = None
if SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY:
    try:
        from supabase import create_client
        supabase_client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
        logger.info("Connected to Supabase database")
    except Exception as e:
        logger.warning(f"Supabase connection failed, falling back to SQLite: {e}")

# ============================================================
# DATABASE ENGINE (SQLite / Supabase)
# ============================================================

def get_sqlite_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    if supabase_client:
        logger.info("Using Supabase database backend")
        return

    conn = get_sqlite_conn()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS file_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_name TEXT NOT NULL,
            file_size INTEGER NOT NULL,
            mime_type TEXT,
            media_type TEXT,
            folder TEXT NOT NULL,
            storage_mode TEXT NOT NULL DEFAULT 'telegram',
            b2_file_id TEXT,
            b2_path TEXT,
            b2_url TEXT,
            b2_status TEXT DEFAULT 'none',
            telegram_channel_id INTEGER,
            telegram_message_id INTEGER,
            telegram_file_id TEXT,
            telegram_file_unique_id TEXT,
            telegram_status TEXT DEFAULT 'none',
            status TEXT NOT NULL DEFAULT 'complete',
            title TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS folders (
            folder_name TEXT PRIMARY KEY NOT NULL,
            storage_mode TEXT,
            created_at TEXT NOT NULL
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
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_msg_id ON file_records(telegram_message_id)")
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
        logger.error(f"Failed to log audit event: {e}")

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

    cursor.execute("SELECT storage_mode, COUNT(*) FROM file_records GROUP BY storage_mode")
    mode_counts = dict(cursor.fetchall())

    conn.close()

    readable_bytes = f"{total_bytes / (1024 * 1024):.1f} MB" if total_bytes < 1073741824 else f"{total_bytes / (1024 * 1024 * 1024):.2f} GB"

    return {
        "total_files": total_files,
        "total_folders": total_folders,
        "total_bytes": total_bytes,
        "readable_bytes": readable_bytes,
        "b2_files": mode_counts.get("b2", 0),
        "telegram_files": mode_counts.get("telegram", 0),
        "both_files": mode_counts.get("both", 0)
    }

def insert_file_record(record: Dict[str, Any]) -> Optional[int]:
    now_str = datetime.utcnow().isoformat()
    if supabase_client:
        try:
            res = supabase_client.table("streams").insert({
                "file_name": record["file_name"],
                "file_size": record["file_size"],
                "mime_type": record.get("mime_type", ""),
                "media_type": record.get("media_type", "document"),
                "folder": record["folder"],
                "storage_mode": record.get("storage_mode", "telegram"),
                "b2_file_id": record.get("b2_file_id"),
                "b2_path": record.get("b2_path"),
                "b2_url": record.get("b2_url"),
                "b2_status": record.get("b2_status", "none"),
                "telegram_channel_id": record.get("telegram_channel_id"),
                "telegram_message_id": record.get("telegram_message_id"),
                "telegram_file_id": record.get("telegram_file_id"),
                "telegram_file_unique_id": record.get("telegram_file_unique_id"),
                "telegram_status": record.get("telegram_status", "none"),
                "status": record.get("status", "complete"),
                "title": record.get("title", record["file_name"]),
                "created_at": record.get("created_at", now_str),
                "updated_at": now_str
            }).execute()
            return res.data[0]["id"] if res.data else None
        except Exception as e:
            logger.error(f"Supabase insert failed: {e}")
            return None

    conn = get_sqlite_conn()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO file_records (
                file_name, file_size, mime_type, media_type, folder, storage_mode,
                b2_file_id, b2_path, b2_url, b2_status,
                telegram_channel_id, telegram_message_id, telegram_file_id, telegram_file_unique_id, telegram_status,
                status, title, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            record["file_name"],
            record["file_size"],
            record.get("mime_type", ""),
            record.get("media_type", "document"),
            record["folder"],
            record.get("storage_mode", "telegram"),
            record.get("b2_file_id"),
            record.get("b2_path"),
            record.get("b2_url"),
            record.get("b2_status", "none"),
            record.get("telegram_channel_id"),
            record.get("telegram_message_id"),
            record.get("telegram_file_id"),
            record.get("telegram_file_unique_id"),
            record.get("telegram_status", "none"),
            record.get("status", "complete"),
            record.get("title", record["file_name"]),
            record.get("created_at", now_str),
            now_str
        ))
        conn.commit()
        last_id = cursor.lastrowid
        ensure_folder_exists(record["folder"])
        return last_id
    except Exception as e:
        logger.error(f"SQLite insert failed: {e}")
        return None
    finally:
        conn.close()

def ensure_folder_exists(folder_name: str):
    conn = get_sqlite_conn()
    cursor = conn.cursor()
    now_str = datetime.utcnow().isoformat()
    cursor.execute("""
        INSERT INTO folders (folder_name, created_at)
        VALUES (?, ?)
        ON CONFLICT(folder_name) DO NOTHING
    """, (folder_name, now_str))
    conn.commit()
    conn.close()

def get_file_by_id(record_id: int) -> Optional[Dict[str, Any]]:
    conn = get_sqlite_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM file_records WHERE id = ?", (record_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

# ============================================================
# BACKBLAZE B2 STORAGE ENGINE
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
                raise ValueError("B2 credentials (B2_APPLICATION_KEY_ID, B2_APPLICATION_KEY, B2_BUCKET_NAME) are not configured.")

            try:
                from b2sdk.v2 import InMemoryAccountInfo, B2Api
                info = InMemoryAccountInfo()
                self._b2_api = B2Api(info)
                self._b2_api.authorize_account("production", self.key_id, self.application_key)
                self._bucket = self._b2_api.get_bucket_by_name(self.bucket_name)
                logger.info(f"Connected to Backblaze B2 bucket: {self.bucket_name}")
                return self._bucket
            except Exception as e:
                logger.error(f"B2 Connection failed: {e}")
                raise

    def check_health(self) -> Tuple[bool, str]:
        """Perform a real B2 health check."""
        try:
            bucket = self._get_bucket()
            # Try listing 1 object to verify list permission
            list(bucket.list_file_names(fetch_count=1))
            return True, f"Bucket '{self.bucket_name}' verified & accessible."
        except Exception as e:
            return False, str(e)

    def discover_folders(self) -> List[str]:
        """Scans B2 bucket objects using pagination and derives real prefix folders."""
        try:
            bucket = self._get_bucket()
            folders_set: Set[str] = set()

            # Paginate through all object file names in B2
            for file_version, _ in bucket.list_file_names():
                name = file_version.file_name
                if "/" in name:
                    parts = name.split("/")
                    accum = ""
                    for part in parts[:-1]:
                        if part:
                            accum = f"{accum}{part}/"
                            folders_set.add(accum.rstrip("/"))

            return sorted(list(folders_set))
        except Exception as e:
            logger.error(f"Error discovering B2 folders: {e}")
            return []

    def list_files_in_prefix(self, prefix: str = "") -> List[Dict[str, Any]]:
        """Lists files in B2 under prefix."""
        try:
            bucket = self._get_bucket()
            norm_prefix = prefix.strip("/") + "/" if prefix.strip("/") else ""
            results = []

            for file_version, _ in bucket.list_file_names(prefix=norm_prefix):
                file_name = file_version.file_name
                if norm_prefix and not file_name.startswith(norm_prefix):
                    continue

                rel_name = file_name[len(norm_prefix):]
                if "/" in rel_name and rel_name.endswith("/"):
                    continue

                url = f"{self.public_base_url}/{file_name}" if self.public_base_url else f"https://f000.backblazeb2.com/file/{self.bucket_name}/{file_name}"

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
            logger.error(f"B2 list files error: {e}")
            return []

    def file_exists(self, b2_path: str) -> bool:
        """Check if B2 object key exists."""
        try:
            bucket = self._get_bucket()
            file_info = bucket.get_file_info_by_name(b2_path)
            return file_info is not None
        except Exception:
            return False

    def upload_file(self, local_file_path: Path, b2_path: str) -> Dict[str, Any]:
        """Uploads a local file to B2 bucket using streaming/multipart API."""
        bucket = self._get_bucket()
        str_path = str(local_file_path)

        if not local_file_path.exists():
            raise FileNotFoundError(f"Local file does not exist: {str_path}")

        logger.info(f"Starting B2 upload for {str_path} -> {b2_path}")

        file_version = bucket.upload_local_file(
            local_file=str_path,
            file_name=b2_path
        )

        b2_file_id = file_version.id_
        url = f"{self.public_base_url}/{b2_path}" if self.public_base_url else f"https://f000.backblazeb2.com/file/{self.bucket_name}/{b2_path}"

        return {
            "b2_file_id": b2_file_id,
            "b2_path": b2_path,
            "b2_url": url,
            "b2_status": "success",
            "file_size": file_version.size
        }

    def search_files(self, query: str) -> List[Dict[str, Any]]:
        try:
            bucket = self._get_bucket()
            q_lower = query.lower()
            results = []

            for file_version, _ in bucket.list_file_names():
                if q_lower in file_version.file_name.lower():
                    url = f"{self.public_base_url}/{file_version.file_name}" if self.public_base_url else f"https://f000.backblazeb2.com/file/{self.bucket_name}/{file_version.file_name}"
                    results.append({
                        "b2_file_id": file_version.id_,
                        "b2_path": file_version.file_name,
                        "file_name": Path(file_version.file_name).name,
                        "file_size": file_version.size,
                        "b2_url": url
                    })

            return results
        except Exception as e:
            logger.error(f"B2 search error: {e}")
            return []

b2_storage = B2StorageEngine()

# ============================================================
# HELPER FUNCTIONS & SANITIZATION
# ============================================================

def is_admin(update: Update) -> bool:
    if not update.effective_user:
        return False
    return update.effective_user.id in ADMIN_IDS

async def deny_access(update: Update):
    msg = "🚫 *Access Denied*: You are not authorized to use this admin bot."
    if update.callback_query:
        await update.callback_query.answer("Access Denied", show_alert=True)
        await update.callback_query.edit_message_text(msg, parse_mode="Markdown")
    elif update.message:
        await update.message.reply_text(msg, parse_mode="Markdown")

def sanitize_filename(name: str) -> str:
    cleaned = name.replace("\0", "").replace("..", "_").replace("\\", "/")
    cleaned = re.sub(r'[/*?:"<>|]', '_', cleaned)
    return cleaned.strip()

def normalize_b2_path(folder: str, filename: str) -> str:
    clean_folder = folder.strip("/").strip()
    clean_file = sanitize_filename(filename)
    if clean_folder:
        return f"{clean_folder}/{clean_file}"
    return clean_file

# ============================================================
# HTTP HEALTH CHECK SERVER
# ============================================================

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status": "ok", "service": "anime4u-bot"}')

    def log_message(self, format, *args):
        pass

def start_health_server():
    try:
        server = HTTPServer(("0.0.0.0", PORT), HealthCheckHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        logger.info(f"Health check HTTP server bound on port {PORT}")
    except Exception as e:
        logger.warning(f"Could not start health check HTTP server on port {PORT}: {e}")

# ============================================================
# TELEGRAM BOT COMMAND HANDLERS
# ============================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny_access(update)
        return

    admin_id = update.effective_user.id
    mode = GLOBAL_STORAGE_MODE.upper()

    text = (
        "🚀 *Anime4u Multi-Backend Storage Bot*\n\n"
        f"👑 *Admin User:* `{admin_id}`\n"
        f"⚙️ *Active Storage Mode:* `{mode}`\n"
        f"📦 *Storage Channel ID:* `{STORAGE_CHANNEL_ID}`\n"
        f"☁️ *B2 Bucket:* `{B2_BUCKET_NAME}`\n\n"
        "Send any video or document file directly to store it!"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📂 Browse Folders", callback_data="cmd_folders"),
            InlineKeyboardButton("📊 Storage Stats", callback_data="cmd_stats")
        ],
        [
            InlineKeyboardButton("👑 Admin Panel", callback_data="admin_panel"),
            InlineKeyboardButton("🗄 Real Health Check", callback_data="cmd_health")
        ],
        [
            InlineKeyboardButton("📁 Selected Folder", callback_data="cmd_selected"),
            InlineKeyboardButton("🔄 Refresh Cache", callback_data="cmd_refresh")
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

    help_text = (
        "📋 *Anime4u Storage Bot Commands*\n\n"
        "/start - Main menu\n"
        "/help - Commands reference\n"
        "/folders - Browse B2/Database folders\n"
        "/newfolder - Create a new virtual folder path\n"
        "/files - List files in current folder\n"
        "/selected - View active folder for uploads\n"
        "/search <query> - Search stored files\n"
        "/stats - View storage metrics\n"
        "/storage - Run real B2 & Telegram health check\n"
        "/admin - Admin Control Panel\n"
        "/admins - List authorized admin user IDs\n"
        "/addadmin <id> - Authorize new admin\n"
        "/removeadmin <id> - Revoke admin ID\n"
        "/broadcast <text> - Send announcement to admins\n"
        "/audit - View security audit logs\n"
        "/mode <telegram|b2|both> - Switch storage engine\n"
        "/cancel - Cancel active input prompt"
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")

async def admin_panel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny_access(update)
        return

    admin_count = len(ADMIN_IDS)
    curr_mode = GLOBAL_STORAGE_MODE.upper()
    stats = get_db_stats()

    text = (
        "👑 *Anime4u Admin Control Panel*\n\n"
        f"⚙️ *Active Storage Mode:* `{curr_mode}`\n"
        f"🛡️ *Authorized Admins:* `{admin_count}`\n"
        f"📦 *Total Stored Files:* `{stats['total_files']}` ({stats['readable_bytes']})\n"
        f"📁 *Virtual Folders:* `{stats['total_folders']}`\n"
        f"🌐 *Health Check Port:* `{PORT}`\n\n"
        "Commands:\n"
        "• `/mode <telegram|b2|both>` - Switch mode\n"
        "• `/admins` - List admin IDs\n"
        "• `/addadmin <id>` - Add new admin\n"
        "• `/removeadmin <id>` - Remove admin\n"
        "• `/broadcast <text>` - Broadcast msg\n"
        "• `/audit` - View audit logs"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⚙️ Mode: Telegram", callback_data="admin_set_mode_telegram"),
            InlineKeyboardButton("⚙️ Mode: B2", callback_data="admin_set_mode_b2"),
        ],
        [
            InlineKeyboardButton("⚙️ Mode: Dual Both", callback_data="admin_set_mode_both"),
            InlineKeyboardButton("📊 Stats", callback_data="cmd_stats"),
        ],
        [
            InlineKeyboardButton("🛡️ Admins List", callback_data="admin_list"),
            InlineKeyboardButton("📋 Audit Logs", callback_data="admin_audit"),
        ],
        [
            InlineKeyboardButton("📢 Broadcast Prompt", callback_data="admin_broadcast_prompt"),
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

    admin_list_str = "\n".join([f"• `{aid}`" for aid in sorted(ADMIN_IDS)])
    text = f"🛡️ *Authorized Admin Users* ({len(ADMIN_IDS)}):\n\n{admin_list_str}\n\nUse `/addadmin <user_id>` or `/removeadmin <user_id>` to modify access."

    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, parse_mode="Markdown")

async def add_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny_access(update)
        return

    if not context.args:
        await update.message.reply_text("⚠️ Usage: `/addadmin <user_id>`", parse_mode="Markdown")
        return

    try:
        new_admin = int(context.args[0].strip())
        ADMIN_IDS.add(new_admin)
        log_audit(update.effective_user.id, "add_admin", f"Added user {new_admin}")
        await update.message.reply_text(f"✅ Added user `{new_admin}` to authorized admins!", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ User ID must be a valid integer.")

async def remove_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny_access(update)
        return

    if not context.args:
        await update.message.reply_text("⚠️ Usage: `/removeadmin <user_id>`", parse_mode="Markdown")
        return

    try:
        target_admin = int(context.args[0].strip())
        if target_admin in ADMIN_IDS:
            if len(ADMIN_IDS) <= 1:
                await update.message.reply_text("❌ Cannot remove the last remaining admin.")
                return
            ADMIN_IDS.remove(target_admin)
            log_audit(update.effective_user.id, "remove_admin", f"Removed user {target_admin}")
            await update.message.reply_text(f"✅ Removed user `{target_admin}` from authorized admins.", parse_mode="Markdown")
        else:
            await update.message.reply_text(f"⚠️ User `{target_admin}` is not an admin.", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ User ID must be a valid integer.")

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny_access(update)
        return

    if not context.args:
        context.user_data["waiting_for_broadcast"] = True
        await update.message.reply_text("📢 Send the announcement message to broadcast to all admins.")
        return

    broadcast_text = " ".join(context.args)
    sender_id = update.effective_user.id
    success_count = 0

    for aid in list(ADMIN_IDS):
        try:
            await context.bot.send_message(
                chat_id=aid,
                text=f"📢 *ADMIN ANNOUNCEMENT*\nFrom: `{sender_id}`\n\n{broadcast_text}",
                parse_mode="Markdown"
            )
            success_count += 1
        except Exception as e:
            logger.warning(f"Could not deliver broadcast to {aid}: {e}")

    log_audit(sender_id, "broadcast", broadcast_text[:100])
    await update.message.reply_text(f"✅ Broadcast sent to `{success_count}` admin(s).", parse_mode="Markdown")

async def audit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny_access(update)
        return

    logs = get_recent_audit_logs(15)
    if not logs:
        text = "📋 No audit log events recorded yet."
    else:
        lines = []
        for l in logs:
            dt = l.get("created_at", "")[:19].replace("T", " ")
            lines.append(f"• `[{dt}]` Admin `{l['admin_id']}`: *{l['action']}* — {l['details']}")
        text = "📋 *Security & Admin Audit Logs* (Recent 15):\n\n" + "\n".join(lines)

    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Admin Panel", callback_data="admin_panel")]])

    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")

async def mode_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global GLOBAL_STORAGE_MODE
    if not is_admin(update):
        await deny_access(update)
        return

    if not context.args:
        await update.message.reply_text(
            f"⚙️ Active Storage Mode: `{GLOBAL_STORAGE_MODE.upper()}`\n\nUsage: `/mode <telegram|b2|both>`",
            parse_mode="Markdown"
        )
        return

    new_m = context.args[0].lower().strip()
    if new_m in ["telegram", "b2", "both"]:
        GLOBAL_STORAGE_MODE = new_m
        log_audit(update.effective_user.id, "change_storage_mode", new_m)
        await update.message.reply_text(f"✅ Storage Mode updated to `{new_m.upper()}`!", parse_mode="Markdown")
    else:
        await update.message.reply_text("❌ Invalid mode. Valid choices: `telegram`, `b2`, `both`", parse_mode="Markdown")

async def storage_health_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny_access(update)
        return

    msg = await update.message.reply_text("⏳ *Running Real Storage Health Checks...*", parse_mode="Markdown") if update.message else None

    telegram_ok = False
    telegram_err = ""
    if STORAGE_CHANNEL_ID:
        try:
            m = await context.bot.send_message(
                chat_id=STORAGE_CHANNEL_ID,
                text="🤖 *Anime4u Health Check Ping*"
            )
            telegram_ok = True
            await context.bot.delete_message(chat_id=STORAGE_CHANNEL_ID, message_id=m.message_id)
        except Exception as e:
            telegram_err = str(e)
    else:
        telegram_err = "STORAGE_CHANNEL_ID is not configured."

    b2_ok, b2_msg = await asyncio.to_thread(b2_storage.check_health)
    ffmpeg_ok = shutil.which("ffmpeg") is not None

    status_text = (
        "🗄 *Real Storage Health Status*\n\n"
        f"⚙️ *Global Mode:* `{GLOBAL_STORAGE_MODE.upper()}`\n\n"
        f"☁️ *Backblaze B2:* {'✅ Connected' if b2_ok else '❌ Failed'}\n"
        f"🪣 *Bucket:* `{B2_BUCKET_NAME}`\n"
        f"📄 *B2 Details:* {b2_msg}\n\n"
        f"📦 *Telegram Storage:* {'✅ Connected' if telegram_ok else '❌ Failed'}\n"
        f"📢 *Channel ID:* `{STORAGE_CHANNEL_ID}`\n"
        f"📄 *Telegram Details:* {'Channel read/write validated' if telegram_ok else telegram_err}\n\n"
        f"🎬 *FFmpeg Installed:* {'✅ Yes' if ffmpeg_ok else '❌ Missing'}"
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
    current_folder = context.user_data.get("selected_folder", "default")

    buttons = []
    buttons.append([InlineKeyboardButton("🏠 Browse Root", callback_data="select_folder:default")])

    if b2_folders:
        for f in b2_folders[:15]:
            icon = "🎯 " if f == current_folder else "📁 "
            buttons.append([InlineKeyboardButton(f"{icon}{f}/", callback_data=f"select_folder:{f}")])
    else:
        buttons.append([InlineKeyboardButton("📂 No B2 Folders Found", callback_data="noop")])

    buttons.append([
        InlineKeyboardButton("➕ New Folder", callback_data="cmd_newfolder"),
        InlineKeyboardButton("🔄 Refresh B2", callback_data="cmd_refresh")
    ])
    buttons.append([InlineKeyboardButton("⬅️ Main Menu", callback_data="start_menu")])

    keyboard = InlineKeyboardMarkup(buttons)
    text = (
        "📂 *B2 Real Folder Browser*\n\n"
        f"🎯 *Active Upload Folder:* `{current_folder}/`\n"
        f"☁️ *B2 Bucket:* `{B2_BUCKET_NAME}`\n\n"
        "Select a folder below to set it as active target:"
    )

    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")

async def new_folder_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny_access(update)
        return

    context.user_data["waiting_for_folder_name"] = True
    await update.message.reply_text(
        "📁 *Create Virtual Folder Path*\n\n"
        "Send the new folder path (e.g., `Solo Leveling` or `Anime/Naruto/S01`):",
        parse_mode="Markdown"
    )

async def selected_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny_access(update)
        return

    folder = context.user_data.get("selected_folder", "default")
    await update.message.reply_text(
        f"🎯 *Currently Selected Folder:* `{folder}/`\n\nAll incoming file uploads will be stored in this directory.",
        parse_mode="Markdown"
    )

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny_access(update)
        return

    stats = get_db_stats()
    text = (
        "📊 *Storage Metrics Summary*\n\n"
        f"📦 *Total Stored Files:* `{stats['total_files']}`\n"
        f"💾 *Total Capacity Used:* `{stats['readable_bytes']}`\n"
        f"📁 *Virtual Folders:* `{stats['total_folders']}`\n"
        f"☁️ *B2 Mode Records:* `{stats['b2_files']}`\n"
        f"📦 *Telegram Mode Records:* `{stats['telegram_files']}`\n"
        f"🔄 *Dual Mode Records:* `{stats['both_files']}`"
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
        await update.message.reply_text("🔎 Usage: `/search <filename or query>`", parse_mode="Markdown")
        return

    query = " ".join(context.args).strip()
    b2_results = await asyncio.to_thread(b2_storage.search_files, query)

    if not b2_results:
        await update.message.reply_text(f"🔎 *No files found for:* `{query}`", parse_mode="Markdown")
        return

    lines = [f"🔎 *Search Results for* `{query}` ({len(b2_results)} found):\n"]
    for r in b2_results[:10]:
        sz_mb = r["file_size"] / (1024 * 1024)
        lines.append(f"• `{r['file_name']}` ({sz_mb:.1f} MB)\n  Path: `{r['b2_path']}`")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny_access(update)
        return

    context.user_data.pop("waiting_for_folder_name", None)
    context.user_data.pop("waiting_for_broadcast", None)
    await update.message.reply_text("❌ Input prompts cancelled.", parse_mode="Markdown")

# ============================================================
# FILE UPLOAD ORCHESTRATOR & MESSAGE HANDLER
# ============================================================

async def handle_incoming_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny_access(update)
        return

    msg = update.message
    file_obj = msg.document or msg.video or msg.audio or (msg.photo[-1] if msg.photo else None)

    if not file_obj:
        return

    if context.user_data.get("waiting_for_folder_name"):
        folder_raw = msg.text.strip().strip("/")
        context.user_data.pop("waiting_for_folder_name", None)
        if folder_raw and ".." not in folder_raw:
            context.user_data["selected_folder"] = folder_raw
            ensure_folder_exists(folder_raw)
            await msg.reply_text(f"✅ Active folder set to: `{folder_raw}/`", parse_mode="Markdown")
        else:
            await msg.reply_text("❌ Invalid folder path.")
        return

    if context.user_data.get("waiting_for_broadcast"):
        b_text = msg.text.strip()
        context.user_data.pop("waiting_for_broadcast", None)
        context.args = b_text.split()
        await broadcast_command(update, context)
        return

    file_name = getattr(file_obj, "file_name", None) or f"file_{int(time.time())}.dat"
    file_name = sanitize_filename(file_name)
    file_size = getattr(file_obj, "file_size", 0)
    mime_type = getattr(file_obj, "mime_type", "application/octet-stream")
    media_type = "video" if msg.video else ("audio" if msg.audio else "document")

    selected_folder = context.user_data.get("selected_folder", "default")
    b2_target_path = normalize_b2_path(selected_folder, file_name)

    if file_size > MAX_FILE_SIZE:
        sz_gb = file_size / (1024 * 1024 * 1024)
        max_gb = MAX_FILE_SIZE / (1024 * 1024 * 1024)
        await msg.reply_text(
            f"❌ *STORAGE FAILED*\n\n"
            f"📄 *File:* `{file_name}`\n"
            f"📁 *Folder:* `{selected_folder}/`\n"
            f"⚙️ *Storage Mode:* `{GLOBAL_STORAGE_MODE.upper()}`\n\n"
            f"❌ *Reason:* File size ({sz_gb:.2f} GB) exceeds configured application limit ({max_gb:.2f} GB).",
            parse_mode="Markdown"
        )
        return

    status_msg = await msg.reply_text(
        f"⏳ *Processing Upload Request...*\n\n"
        f"📄 *File:* `{file_name}` ({file_size / (1024 * 1024):.1f} MB)\n"
        f"📁 *Target Folder:* `{selected_folder}/`\n"
        f"⚙️ *Mode:* `{GLOBAL_STORAGE_MODE.upper()}`\n"
        f"📦 *Telegram Download:* ⏳ In Progress...",
        parse_mode="Markdown"
    )

    async with UPLOAD_SEMAPHORE:
        local_file_path = TEMP_DIR / f"{int(time.time())}_{file_name}"
        telegram_status = "none"
        b2_status = "none"
        b2_res = {}
        tg_res = {}
        error_detail = ""

        try:
            logger.info(f"Downloading Telegram file {file_name} to {local_file_path}")
            tg_file = await context.bot.get_file(file_obj.file_id)
            await tg_file.download_to_drive(custom_path=local_file_path)

            if not local_file_path.exists():
                raise FileNotFoundError("Downloaded temporary file not found on disk.")

            telegram_status = "downloaded"

            if GLOBAL_STORAGE_MODE in ["b2", "both"]:
                await status_msg.edit_text(
                    f"⏳ *Uploading to Backblaze B2...*\n\n"
                    f"📄 *File:* `{file_name}`\n"
                    f"📁 *B2 Path:* `{b2_target_path}`\n"
                    f"☁️ *B2 Upload:* ⏳ Streaming...",
                    parse_mode="Markdown"
                )

                exists = await asyncio.to_thread(b2_storage.file_exists, b2_target_path)
                if exists:
                    logger.info(f"File {b2_target_path} already exists in B2.")

                b2_res = await asyncio.to_thread(
                    b2_storage.upload_file,
                    local_file_path,
                    b2_target_path
                )
                b2_status = "success"

            if GLOBAL_STORAGE_MODE in ["telegram", "both"]:
                if STORAGE_CHANNEL_ID:
                    await status_msg.edit_text(
                        f"⏳ *Forwarding to Telegram Storage Channel...*\n\n"
                        f"📄 *File:* `{file_name}`\n"
                        f"📢 *Channel ID:* `{STORAGE_CHANNEL_ID}`",
                        parse_mode="Markdown"
                    )
                    with open(local_file_path, "rb") as f:
                        sent_m = await context.bot.send_document(
                            chat_id=STORAGE_CHANNEL_ID,
                            document=f,
                            filename=file_name,
                            caption=f"📁 Folder: {selected_folder}\n📄 File: {file_name}"
                        )
                    tg_res = {
                        "channel_id": STORAGE_CHANNEL_ID,
                        "message_id": sent_m.message_id,
                        "file_id": sent_m.document.file_id,
                        "file_unique_id": sent_m.document.file_unique_id
                    }
                    telegram_status = "success"
                else:
                    telegram_status = "skipped"

            rec_id = insert_file_record({
                "file_name": file_name,
                "file_size": file_size,
                "mime_type": mime_type,
                "media_type": media_type,
                "folder": selected_folder,
                "storage_mode": GLOBAL_STORAGE_MODE,
                "b2_file_id": b2_res.get("b2_file_id"),
                "b2_path": b2_res.get("b2_path"),
                "b2_url": b2_res.get("b2_url"),
                "b2_status": b2_status,
                "telegram_channel_id": tg_res.get("channel_id"),
                "telegram_message_id": tg_res.get("message_id"),
                "telegram_file_id": tg_res.get("file_id"),
                "telegram_file_unique_id": tg_res.get("file_unique_id"),
                "telegram_status": telegram_status,
                "status": "complete",
                "title": file_name
            })

            success_text = (
                "✅ *STORAGE SUCCESSFUL*\n\n"
                f"📄 *File:* `{file_name}`\n"
                f"📁 *Folder:* `{selected_folder}/`\n"
                f"⚙️ *Mode:* `{GLOBAL_STORAGE_MODE.upper()}`\n\n"
                f"☁️ *B2 Status:* {'✅ Success' if b2_status == 'success' else '⚪ N/A'}\n"
                f"🆔 *B2 File ID:* `{b2_res.get('b2_file_id', 'N/A')}`\n"
                f"🔗 *B2 Path:* `{b2_res.get('b2_path', 'N/A')}`\n\n"
                f"📦 *Telegram Status:* {'✅ Success' if telegram_status == 'success' else '⚪ N/A'}\n"
                f"🆔 *Record ID:* `{rec_id}`"
            )
            await status_msg.edit_text(success_text, parse_mode="Markdown")

        except Exception as e:
            logger.error(f"Upload failed for {file_name}: {e}", exc_info=True)
            error_detail = str(e)
            fail_text = (
                "❌ *STORAGE FAILED*\n\n"
                f"📄 *File:* `{file_name}`\n"
                f"📁 *Folder:* `{selected_folder}/`\n"
                f"⚙️ *Mode:* `{GLOBAL_STORAGE_MODE.upper()}`\n\n"
                f"☁️ *B2 Status:* {'❌ Failed' if GLOBAL_STORAGE_MODE in ['b2', 'both'] else '⚪ N/A'}\n"
                f"📦 *Telegram Status:* `{telegram_status}`\n\n"
                f"❌ *Reason:* {error_detail}"
            )
            await status_msg.edit_text(fail_text, parse_mode="Markdown")

        finally:
            if local_file_path.exists():
                try:
                    local_file_path.unlink()
                    logger.info(f"Cleaned up temp file {local_file_path}")
                except Exception as ce:
                    logger.warning(f"Could not delete temp file: {ce}")

# ============================================================
# CALLBACK QUERY HANDLER
# ============================================================

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny_access(update)
        return

    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "start_menu":
        await start_command(update, context)
    elif data == "admin_panel":
        await admin_panel_command(update, context)
    elif data == "admin_list":
        await list_admins_command(update, context)
    elif data == "admin_audit":
        await audit_command(update, context)
    elif data == "admin_broadcast_prompt":
        context.user_data["waiting_for_broadcast"] = True
        await query.edit_message_text("📢 Send the announcement text to broadcast to all admins.")
    elif data == "admin_set_mode_telegram":
        global GLOBAL_STORAGE_MODE
        GLOBAL_STORAGE_MODE = "telegram"
        log_audit(update.effective_user.id, "change_storage_mode", "telegram")
        await admin_panel_command(update, context)
    elif data == "admin_set_mode_b2":
        GLOBAL_STORAGE_MODE = "b2"
        log_audit(update.effective_user.id, "change_storage_mode", "b2")
        await admin_panel_command(update, context)
    elif data == "admin_set_mode_both":
        GLOBAL_STORAGE_MODE = "both"
        log_audit(update.effective_user.id, "change_storage_mode", "both")
        await admin_panel_command(update, context)
    elif data == "cmd_folders":
        await folders_command(update, context)
    elif data == "cmd_newfolder":
        await new_folder_command(update, context)
    elif data == "cmd_selected":
        await selected_command(update, context)
    elif data == "cmd_stats":
        await stats_command(update, context)
    elif data == "cmd_health":
        await storage_health_command(update, context)
    elif data == "cmd_refresh":
        await folders_command(update, context)
    elif data.startswith("select_folder:"):
        folder = data.split(":", 1)[1]
        context.user_data["selected_folder"] = folder
        ensure_folder_exists(folder)
        await query.edit_message_text(f"✅ Active folder set to: `{folder}/`", parse_mode="Markdown")

# ============================================================
# APPLICATION MAIN ENTRYPOINT
# ============================================================

def main():
    logger.info("Initializing Anime4u Multi-Backend Storage Bot...")

    init_db()
    start_health_server()

    if not BOT_TOKEN:
        logger.critical("BOT_TOKEN environment variable is missing!")
        sys.exit(1)

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("admin", admin_panel_command))
    app.add_handler(CommandHandler("adminpanel", admin_panel_command))
    app.add_handler(CommandHandler("admins", list_admins_command))
    app.add_handler(CommandHandler("addadmin", add_admin_command))
    app.add_handler(CommandHandler("removeadmin", remove_admin_command))
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    app.add_handler(CommandHandler("audit", audit_command))
    app.add_handler(CommandHandler("mode", mode_command))
    app.add_handler(CommandHandler("storage", storage_health_command))
    app.add_handler(CommandHandler("folders", folders_command))
    app.add_handler(CommandHandler("newfolder", new_folder_command))
    app.add_handler(CommandHandler("selected", selected_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("search", search_command))
    app.add_handler(CommandHandler("cancel", cancel_command))

    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.DOCUMENT | filters.VIDEO | filters.AUDIO | filters.PHOTO | filters.TEXT, handle_incoming_file))

    logger.info("🚀 Storage Engine ready! Polling started.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
