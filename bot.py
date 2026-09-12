import os
import sys
import asyncio
import logging
import subprocess
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.error import TelegramError, RetryAfter, Conflict

import database
from storage import storage_manager

# Setup structured production logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("Anime4uBot")

# ============================================================
# HTTP HEALTH CHECK SERVER FOR CLOUD RUN / RENDER
# ============================================================

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK - Anime4u Storage Bot Online")

    def log_message(self, format, *args):
        pass

def start_health_check_server():
    try:
        port = int(os.getenv("PORT", 3000))
        server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        logger.info(f"🌐 Health check HTTP server bound and listening on 0.0.0.0:{port}")
    except Exception as e:
        logger.warning(f"Could not start HTTP health check server: {e}")

# ============================================================
# CONFIGURATION
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "8769661029:AAED5_SSFoU-Q_xQ_-p-x5FqzU7J9MZcIaE").strip()
STORAGE_CHANNEL_ID_RAW = os.getenv("STORAGE_CHANNEL_ID", "-1004364304959").strip()
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "8525952693").strip()

try:
    STORAGE_CHANNEL_ID = int(STORAGE_CHANNEL_ID_RAW) if STORAGE_CHANNEL_ID_RAW else 0
except ValueError:
    logger.error("STORAGE_CHANNEL_ID must be a valid integer ID (e.g. -1001234567890)")
    STORAGE_CHANNEL_ID = 0

ADMIN_IDS = {
    int(x.strip())
    for x in ADMIN_IDS_RAW.split(",")
    if x.strip().replace("5192451273", "").isdigit()
}

MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE", 2147483648))  # 2 GB
PAGE_SIZE = 10

# Active state tracking
selected_folder = {}
pending_duplicates = {}

# ============================================================
# SECURITY FIRST
# ============================================================

def is_admin(update: Update) -> bool:
    """Strict security authorization check for all commands and callbacks."""
    user = update.effective_user
    return bool(user and user.id in ADMIN_IDS)

async def deny(update: Update):
    """Denies access without exposing system secrets."""
    if update.message:
        await update.message.reply_text("⛔ Admin authorization required.")
    elif update.callback_query:
        await update.callback_query.answer("⛔ Admin authorization required.", show_alert=True)

# ============================================================
# STARTUP VALIDATION
# ============================================================

async def startup_health_check(app: Application):
    logger.info("🚀 Running startup configuration and health checks...")

    missing = storage_manager.validate_startup_config()
    if missing:
        logger.critical(f"❌ Missing required environment variables for mode '{storage_manager.global_mode}': {', '.join(missing)}")
        # In production build, raise clear exception
        logger.warning("Please configure missing variables in .env before proceeding.")

    # Database initialization
    database.init_db()

    # Validate Telegram Connection & Storage Channel Access if configured
    try:
        bot_user = await app.bot.get_me()
        logger.info(f"✅ Telegram connected as @{bot_user.username}")

        if storage_manager.global_mode in ("telegram", "both") and STORAGE_CHANNEL_ID:
            test_msg = await app.bot.send_message(
                chat_id=STORAGE_CHANNEL_ID,
                text=f"🤖 Storage Engine Online | Mode: `{storage_manager.global_mode.upper()}`"
            )
            logger.info(f"✅ Storage channel connection validated! Message ID={test_msg.message_id}")
    except Exception as e:
        logger.warning(f"Storage channel validation warning: {e}")

    logger.info(f"🚀 Storage Engine ready! Active mode: {storage_manager.global_mode.upper()}")

# ============================================================
# KEYBOARD & COMMAND HANDLERS
# ============================================================

def get_start_keyboard() -> InlineKeyboardMarkup:
    mode_label = storage_manager.global_mode.upper()
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📤 Upload", callback_data="upload_prompt"),
            InlineKeyboardButton("📁 Folders", callback_data="cmd_folders")
        ],
        [
            InlineKeyboardButton("📄 Files", callback_data="files_0"),
            InlineKeyboardButton("🔎 Search", callback_data="search_prompt")
        ],
        [
            InlineKeyboardButton("📊 Statistics", callback_data="cmd_stats"),
            InlineKeyboardButton(f"⚙️ Storage ({mode_label})", callback_data="cmd_storage_mode")
        ],
        [
            InlineKeyboardButton("🕒 Recent", callback_data="cmd_recent"),
            InlineKeyboardButton("💾 Health Check", callback_data="cmd_storage")
        ],
        [
            InlineKeyboardButton("⚠️ Reconcile Engine", callback_data="cmd_reconcile")
        ]
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny(update)
        return

    user_id = update.effective_user.id
    curr_folder = selected_folder.get(user_id, "default")

    text = (
        f"👑 *Anime4u Multi-Backend Storage Bot*\n\n"
        f"⚙️ Active Storage Mode: `{storage_manager.global_mode.upper()}`\n"
        f"📁 Active Virtual Folder: `{curr_folder}/`\n\n"
        f"Send any document, video, or audio file to begin upload."
    )

    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=get_start_keyboard(), parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=get_start_keyboard(), parse_mode="Markdown")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny(update)
        return

    help_text = (
        "🤖 *Storage Bot Command Reference*\n\n"
        "/start - Main Control Panel & Storage Mode\n"
        "/folders - Browse folders & per-folder storage mode\n"
        "/newfolder - Create a new virtual folder path\n"
        "/selected - View active target folder\n"
        "/files - Browse indexed storage records\n"
        "/search <query> - Search records by filename, title, folder, or ID\n"
        "/stats - Real database & storage metrics\n"
        "/storage - Real health check (Telegram API, B2, DB, FFmpeg)\n"
        "/recent - View 10 most recent uploads\n"
        "/reconcile - Run reconciliation audit (B2 vs Telegram vs DB)\n"
        "/cancel - Cancel active input prompt"
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")

async def folders_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny(update)
        return

    folders_list = database.list_folders()
    if not folders_list:
        text = "📁 No folders found.\n\nUse /newfolder to create a virtual folder path."
        if update.callback_query:
            await update.callback_query.edit_message_text(text)
        else:
            await update.message.reply_text(text)
        return

    buttons = []
    for f in folders_list[:PAGE_SIZE]:
        f_name = f["folder"]
        f_mode = f.get("storage_mode") or "GLOBAL"
        buttons.append([
            InlineKeyboardButton(f"📁 {f_name} [{f_mode}] ({f['count']})", callback_data=f"selfolder:{f_name}")
        ])

    buttons.append([InlineKeyboardButton("➕ New Folder", callback_data="newfolder_prompt")])
    buttons.append([InlineKeyboardButton("⬅️ Main Menu", callback_data="start_menu")])

    keyboard = InlineKeyboardMarkup(buttons)
    text = f"📂 *Virtual Folders Index*\nTotal Folders: `{len(folders_list)}`"

    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")

async def newfolder_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny(update)
        return

    context.user_data["waiting_for_folder_name"] = True
    await update.message.reply_text(
        "📁 *Create Virtual Folder*\n\n"
        "Send the virtual folder path (e.g. `anime/naruto/season-1`).",
        parse_mode="Markdown"
    )

async def selected_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny(update)
        return

    folder = selected_folder.get(update.effective_user.id, "default")
    mode = storage_manager.get_effective_mode(folder)
    await update.message.reply_text(
        f"📁 *Active Target Folder:*\n`{folder}/` (Storage Mode: `{mode.upper()}`)",
        parse_mode="Markdown"
    )

async def files_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny(update)
        return

    user_id = update.effective_user.id
    folder = selected_folder.get(user_id)
    files = database.search_files(folder, page=0, page_size=10) if folder else database.get_recent_uploads(10)

    if not files:
        text = "📂 No files found."
        if update.callback_query:
            await update.callback_query.edit_message_text(text)
        else:
            await update.message.reply_text(text)
        return

    buttons = []
    for f in files:
        status_icon = "✅" if f["status"] == "complete" else ("⚠️" if f["status"] == "partial_success" else "❌")
        buttons.append([
            InlineKeyboardButton(f"{status_icon} {f['file_name']}", callback_data=f"fileinfo:{f['id']}")
        ])
    buttons.append([InlineKeyboardButton("⬅️ Main Menu", callback_data="start_menu")])

    keyboard = InlineKeyboardMarkup(buttons)
    title_str = f"In `{folder}/`" if folder else "Recent Storage Records"
    text = f"📄 *{title_str}* ({len(files)} items):"

    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny(update)
        return

    st = database.get_stats()
    text = (
        f"📊 *Real Database & Storage Statistics*\n\n"
        f"Total Files: `{st['total_files']}`\n"
        f"Virtual Folders: `{st['total_folders']}`\n"
        f"Indexed Storage Size: `{st['readable_bytes']}`\n"
        f"Uploads Today: `{st['uploads_today']}`\n\n"
        f"☁️ Backblaze B2 Files: `{st['b2_files']}`\n"
        f"📦 Telegram Storage Files: `{st['telegram_files']}`\n"
        f"🔄 Dual-Storage Files: `{st['both_files']}`\n\n"
        f"🎞️ Videos: `{st['videos']}` | 📄 Documents: `{st['documents']}`\n"
        f"🎵 Audio: `{st['audio']}` | 🖼️ Photos: `{st['photos']}`"
    )
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Main Menu", callback_data="start_menu")]])

    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")

async def storage_health_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny(update)
        return

    # Real health checks
    b2_ok = await storage_manager.b2_storage.validate_connection() if storage_manager.b2_storage.is_configured() else False
    tg_ok = await storage_manager.telegram_storage.validate_connection(context.bot) if storage_manager.telegram_storage.is_configured() else False

    b2_str = "✅ Online" if b2_ok else ("❌ Unavailable" if storage_manager.b2_storage.is_configured() else "⚪ Not Configured")
    tg_str = f"✅ Online (Channel `{STORAGE_CHANNEL_ID}`)" if tg_ok else ("❌ Unavailable" if STORAGE_CHANNEL_ID else "⚪ Not Configured")

    try:
        res = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True)
        ffmpeg_str = "✅ Installed" if res.returncode == 0 else "⚠️ Execution Error"
    except Exception:
        ffmpeg_str = "⚠️ Not Available"

    text = (
        f"💾 *Infrastructure & Backend Health Check*\n\n"
        f"🤖 Telegram Bot API: ✅ Online\n"
        f"☁️ Backblaze B2: {b2_str}\n"
        f"📦 Telegram Storage Channel: {tg_str}\n"
        f"🗄️ Database Metadata: ✅ Online\n"
        f"🎞️ FFmpeg Engine: {ffmpeg_str}\n\n"
        f"⚙️ Global Storage Mode: `{storage_manager.global_mode.upper()}`\n"
        f"📦 Max File Size Limit: `{MAX_FILE_SIZE / (1024*1024*1024):.1f} GB`"
    )
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Main Menu", callback_data="start_menu")]])

    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")

async def recent_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny(update)
        return

    recent = database.get_recent_uploads(10)
    if not recent:
        text = "📂 No files found."
        if update.callback_query:
            await update.callback_query.edit_message_text(text)
        else:
            await update.message.reply_text(text)
        return

    buttons = []
    for r in recent:
        status_icon = "✅" if r["status"] == "complete" else ("⚠️" if r["status"] == "partial_success" else "❌")
        buttons.append([
            InlineKeyboardButton(f"{status_icon} {r['file_name']} ({r['folder']})", callback_data=f"fileinfo:{r['id']}")
        ])
    buttons.append([InlineKeyboardButton("⬅️ Main Menu", callback_data="start_menu")])

    keyboard = InlineKeyboardMarkup(buttons)
    text = f"🕒 *10 Most Recent Uploads*:"

    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")

async def reconcile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny(update)
        return

    msg = await update.message.reply_text("⏳ Running deep reconciliation check across B2, Telegram Channel, and Database...") if update.message else None

    res = await storage_manager.reconcile(context.bot)
    database.log_audit(update.effective_user.id, "reconcile", f"Reconciled {res['total_records']} records")

    report = (
        f"⚠️ *Storage Reconciliation Audit Report*\n\n"
        f"Total Database Indexed Records: `{res['total_records']}`\n\n"
        f"☁️ Backblaze B2 Verified: `{res['verified_b2']}`\n"
        f"☁️ B2 Missing Objects: `{res['b2_missing_count']}`\n\n"
        f"📦 Telegram Channel Verified: `{res['verified_tg']}`\n"
        f"📦 Telegram Missing Messages: `{res['tg_missing_count']}`"
    )

    if msg:
        await msg.edit_text(report, parse_mode="Markdown")
    elif update.callback_query:
        await update.callback_query.edit_message_text(report, parse_mode="Markdown")

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny(update)
        return

    context.user_data.clear()
    await update.message.reply_text("❌ Input prompt cancelled.")

async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny(update)
        return

    query = " ".join(context.args) if context.args else ""
    if not query:
        context.user_data["waiting_for_search"] = True
        await update.message.reply_text("🔎 Send search query (filename, title, folder, or record ID):")
        return

    results = database.search_files(query, page=0, page_size=10)
    if not results:
        await update.message.reply_text(f"🔎 No results found for `{query}`.", parse_mode="Markdown")
        return

    buttons = []
    for r in results:
        status_icon = "✅" if r["status"] == "complete" else ("⚠️" if r["status"] == "partial_success" else "❌")
        buttons.append([
            InlineKeyboardButton(f"{status_icon} {r['file_name']}", callback_data=f"fileinfo:{r['id']}")
        ])

    keyboard = InlineKeyboardMarkup(buttons)
    await update.message.reply_text(
        f"🔎 *Search Results for '{query}'* ({len(results)} items):",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

# ============================================================
# TEXT INPUT & PATH VALIDATION
# ============================================================

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny(update)
        return

    user_data = context.user_data

    # New folder prompt input
    if user_data.get("waiting_for_folder_name"):
        folder_raw = update.message.text.strip().strip("/")
        if not folder_raw or ".." in folder_raw or "\\" in folder_raw or len(folder_raw) > 120:
            await update.message.reply_text("❌ Invalid folder path. Avoid '..', '\\', or empty names.")
            return

        selected_folder[update.effective_user.id] = folder_raw
        user_data.pop("waiting_for_folder_name", None)
        database.ensure_folder_exists(folder_raw)

        await update.message.reply_text(
            f"✅ *Folder Selected!*\n\n📁 `{folder_raw}/`\n\nNow send any file or video to upload.",
            parse_mode="Markdown"
        )
        return

    # Search input
    if user_data.get("waiting_for_search"):
        query = update.message.text.strip()
        user_data.pop("waiting_for_search", None)
        context.args = query.split()
        await search_command(update, context)
        return

    # Move target folder input
    if user_data.get("waiting_for_move_target"):
        record_id = user_data.pop("waiting_for_move_target")
        new_folder = update.message.text.strip().strip("/")
        if not new_folder or ".." in new_folder or "\\" in new_folder or len(new_folder) > 120:
            await update.message.reply_text("❌ Invalid folder path.")
            return

        ok = database.update_file_folder(record_id, new_folder)
        database.log_audit(update.effective_user.id, "move_file", f"record_id={record_id}, new_folder={new_folder}")
        if ok:
            await update.message.reply_text(f"✅ Record ID `{record_id}` moved to `{new_folder}/`.", parse_mode="Markdown")
        else:
            await update.message.reply_text("❌ Failed to update folder in database.")
        return

# ============================================================
# MEDIA UPLOAD & DUPLICATE DETECTION PIPELINE
# ============================================================

async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny(update)
        return

    user_id = update.effective_user.id
    folder = selected_folder.get(user_id, "default")

    media = (
        update.message.document
        or update.message.video
        or update.message.audio
        or (update.message.photo[-1] if update.message.photo else None)
    )

    if not media:
        return

    file_unique_id = getattr(media, "file_unique_id", None)
    raw_name = getattr(media, "file_name", f"file_{file_unique_id}.bin")
    b2_path = storage_manager.b2_storage.normalize_b2_path(folder, raw_name)

    # Duplicate check by Telegram file_unique_id or B2 path
    existing = database.find_duplicate(telegram_file_unique_id=file_unique_id, b2_path=b2_path)
    if existing:
        pending_duplicates[user_id] = update.message
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📂 Use Existing", callback_data=f"dup_use:{existing['id']}"),
                InlineKeyboardButton("📤 Store Again", callback_data="dup_store_again")
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data="dup_cancel")]
        ])

        await update.message.reply_text(
            f"⚠️ *Duplicate File Detected*\n\n"
            f"📄 Filename: `{existing['file_name']}`\n"
            f"📁 Folder: `{existing['folder']}`\n"
            f"⚙️ Mode: `{existing['storage_mode']}`\n"
            f"🆔 Record ID: `{existing['id']}`\n\n"
            f"Choose action:",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
        return

    await process_file_upload(update.message, folder, context)

async def process_file_upload(message, folder: str, context: ContextTypes.DEFAULT_TYPE):
    status_msg = await message.reply_text("⬇️ Receiving file & preparing pipeline...")

    try:
        await status_msg.edit_text("🔍 Checking storage backends...")
        mode = storage_manager.get_effective_mode(folder)

        if mode == "b2":
            await status_msg.edit_text("☁️ Uploading to Backblaze B2...")
        elif mode == "telegram":
            await status_msg.edit_text("📦 Storing in Telegram Private Channel...")
        else:
            await status_msg.edit_text("🔄 Storing in BOTH Backblaze B2 & Telegram Channel...")

        # Execute upload through unified StorageManager
        upload_res = await storage_manager.store_file(
            bot=context.bot,
            message=message,
            folder=folder
        )

        status = upload_res["status"]
        rec = upload_res["record"]

        if status == "complete":
            title_text = "✅ *STORAGE COMPLETE*"
        elif status == "partial_success":
            title_text = "⚠️ *PARTIAL STORAGE COMPLETE*"
        else:
            title_text = "❌ *STORAGE FAILED*"

        b2_str = "✅ Success" if upload_res["b2_ok"] else (f"❌ {upload_res.get('b2_err', 'Failed')}" if mode in ("b2", "both") else "⚪ N/A")
        tg_str = "✅ Success" if upload_res["tg_ok"] else (f"❌ {upload_res.get('tg_err', 'Failed')}" if mode in ("telegram", "both") else "⚪ N/A")

        summary = (
            f"{title_text}\n\n"
            f"📄 *File:* `{rec['file_name']}`\n"
            f"📁 *Folder:* `{rec['folder']}/`\n"
            f"⚙️ *Storage Mode:* `{mode.upper()}`\n\n"
            f"☁️ *B2 Status:* {b2_str}\n"
            f"📦 *Telegram Status:* {tg_str}\n\n"
            f"🆔 *Record ID:* `{rec.get('id', 'N/A')}`"
        )

        await status_msg.edit_text(summary, parse_mode="Markdown")

        database.log_audit(
            message.from_user.id,
            "upload",
            f"file={rec['file_name']}, mode={mode}, status={status}"
        )

    except RetryAfter as e:
        await status_msg.edit_text(f"⏳ Telegram rate limit. Auto-retrying in {e.retry_after} seconds...")
        await asyncio.sleep(e.retry_after)
        await process_file_upload(message, folder, context)

    except Exception as e:
        logger.error(f"Upload pipeline failure: {e}", exc_info=True)
        await status_msg.edit_text(f"❌ Storage pipeline error: `{str(e)[:200]}`", parse_mode="Markdown")

# ============================================================
# CALLBACK QUERY HANDLER
# ============================================================

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(update):
        await deny(update)
        return

    await query.answer()
    data = query.data
    user_id = query.from_user.id

    if data == "start_menu":
        await start(update, context)

    elif data == "cmd_folders":
        await folders_command(update, context)

    elif data == "files_0":
        await files_command(update, context)

    elif data == "cmd_stats":
        await stats_command(update, context)

    elif data == "cmd_storage":
        await storage_health_command(update, context)

    elif data == "cmd_recent":
        await recent_command(update, context)

    elif data == "cmd_reconcile":
        await reconcile_command(update, context)

    elif data == "cmd_storage_mode":
        curr_mode = storage_manager.global_mode.upper()
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"{'✅ ' if curr_mode == 'B2' else ''}☁️ Backblaze B2", callback_data="set_mode:b2")],
            [InlineKeyboardButton(f"{'✅ ' if curr_mode == 'TELEGRAM' else ''}📦 Telegram Channel", callback_data="set_mode:telegram")],
            [InlineKeyboardButton(f"{'✅ ' if curr_mode == 'BOTH' else ''}🔄 Both (Dual Storage)", callback_data="set_mode:both")],
            [InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="start_menu")]
        ])
        await query.edit_message_text(
            f"⚙️ *Global Storage Mode Selector*\n\n"
            f"Currently Configured Mode: `{curr_mode}`\n\n"
            f"Select active default storage backend:",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )

    elif data.startswith("set_mode:"):
        new_mode = data.split(":", 1)[1]
        storage_manager.set_global_mode(new_mode)
        database.log_audit(user_id, "set_storage_mode", new_mode)
        await query.edit_message_text(
            f"✅ Storage Mode updated to `{new_mode.upper()}`!",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="start_menu")]]),
            parse_mode="Markdown"
        )

    elif data == "upload_prompt":
        folder = selected_folder.get(user_id, "default")
        mode = storage_manager.get_effective_mode(folder)
        await query.edit_message_text(
            f"📤 *Upload Pipeline*\n\n"
            f"📁 Target Folder: `{folder}/`\n"
            f"⚙️ Active Mode: `{mode.upper()}`\n\n"
            f"Send any document, video, or audio file directly to this chat.",
            parse_mode="Markdown"
        )

    elif data == "newfolder_prompt":
        await newfolder_command(update, context)

    elif data == "search_prompt":
        context.user_data["waiting_for_search"] = True
        await query.edit_message_text("🔎 Send search query (filename, title, folder, or record ID):")

    elif data.startswith("selfolder:"):
        folder_name = data.split(":", 1)[1]
        selected_folder[user_id] = folder_name
        mode = storage_manager.get_effective_mode(folder_name)
        database.log_audit(user_id, "select_folder", folder_name)

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📤 Upload File Here", callback_data="upload_prompt")],
            [InlineKeyboardButton(f"⚙️ Set Folder Mode (Curr: {mode.upper()})", callback_data=f"foldermode_prompt:{folder_name}")],
            [InlineKeyboardButton("⬅️ Back to Folders", callback_data="cmd_folders")]
        ])

        await query.edit_message_text(
            f"✅ *Folder Selected!*\n\n📁 `{folder_name}/`\n⚙️ Storage Mode: `{mode.upper()}`\n\nSend any file or video to store.",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )

    elif data.startswith("foldermode_prompt:"):
        fn = data.split(":", 1)[1]
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("☁️ Backblaze B2 Only", callback_data=f"setfoldermode:{fn}:b2")],
            [InlineKeyboardButton("📦 Telegram Channel Only", callback_data=f"setfoldermode:{fn}:telegram")],
            [InlineKeyboardButton("🔄 Both (Dual Storage)", callback_data=f"setfoldermode:{fn}:both")],
            [InlineKeyboardButton("🌐 Use Global Default", callback_data=f"setfoldermode:{fn}:none")],
            [InlineKeyboardButton("⬅️ Back", callback_data=f"selfolder:{fn}")]
        ])
        await query.edit_message_text(f"⚙️ Set per-folder storage mode for `{fn}/`:", reply_markup=keyboard, parse_mode="Markdown")

    elif data.startswith("setfoldermode:"):
        _, fn, mode_val = data.split(":", 2)
        set_val = mode_val if mode_val != "none" else None
        database.set_folder_storage_mode(fn, set_val)
        database.log_audit(user_id, "set_folder_mode", f"folder={fn}, mode={set_val}")
        await query.edit_message_text(f"✅ Folder `{fn}/` storage mode updated to `{mode_val.upper()}`!", parse_mode="Markdown")

    elif data.startswith("fileinfo:"):
        rec_id = int(data.split(":", 1)[1])
        rec = database.get_file_by_id(rec_id)
        if not rec:
            await query.edit_message_text("❌ File record not found.")
            return

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📥 Retrieve File", callback_data=f"getrec:{rec_id}"),
                InlineKeyboardButton("📁 Move Folder", callback_data=f"moveprompt:{rec_id}")
            ],
            [InlineKeyboardButton("🗑️ Delete File", callback_data=f"delconfirm:{rec_id}")],
            [InlineKeyboardButton("⬅️ Back to Start", callback_data="start_menu")]
        ])

        await query.edit_message_text(
            f"📄 *File Record Details*\n\n"
            f"Title: `{rec['title']}`\n"
            f"Filename: `{rec['file_name']}`\n"
            f"Folder: `{rec['folder']}`\n"
            f"Size: `{rec['file_size'] / (1024*1024):.1f} MB`\n"
            f"⚙️ Mode: `{rec['storage_mode'].upper()}`\n"
            f"☁️ B2 Status: `{rec['b2_status']}` (Path: `{rec.get('b2_path', 'N/A')}`)\n"
            f"📦 Telegram Status: `{rec['telegram_status']}` (Msg ID: `{rec.get('telegram_message_id', 'N/A')}`)\n"
            f"🆔 Record ID: `{rec['id']}`\n"
            f"📌 Status: `{rec['status']}`\n"
            f"📅 Created: `{rec['created_at']}`",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )

    elif data.startswith("getrec:"):
        rec_id = int(data.split(":", 1)[1])
        rec = database.get_file_by_id(rec_id)
        if not rec:
            await query.edit_message_text("❌ Record not found.")
            return

        if rec.get("telegram_message_id") and STORAGE_CHANNEL_ID:
            try:
                await context.bot.copy_message(
                    chat_id=update.effective_chat.id,
                    from_chat_id=STORAGE_CHANNEL_ID,
                    message_id=rec["telegram_message_id"]
                )
                database.log_audit(user_id, "retrieve_file", f"id={rec_id}")
                return
            except Exception as e:
                logger.warning(f"Failed Telegram message retrieval: {e}")

        if rec.get("b2_url"):
            await query.edit_message_text(f"🔗 *B2 Direct Access Link:*\n{rec['b2_url']}", parse_mode="Markdown")
        else:
            await query.edit_message_text("❌ File could not be retrieved directly.")

    elif data.startswith("moveprompt:"):
        rec_id = int(data.split(":", 1)[1])
        context.user_data["waiting_for_move_target"] = rec_id
        await query.edit_message_text(f"📁 Send new virtual folder path for Record ID `{rec_id}`:", parse_mode="Markdown")

    elif data.startswith("delconfirm:"):
        rec_id = int(data.split(":", 1)[1])
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🗑️ Yes, Delete", callback_data=f"delfile:{rec_id}"),
                InlineKeyboardButton("❌ Cancel", callback_data=f"fileinfo:{rec_id}")
            ]
        ])
        await query.edit_message_text(f"⚠️ *Delete file from B2, Telegram Storage, and DB?*\nRecord ID: `{rec_id}`", reply_markup=keyboard, parse_mode="Markdown")

    elif data.startswith("delfile:"):
        rec_id = int(data.split(":", 1)[1])
        rec = database.get_file_by_id(rec_id)
        if not rec:
            await query.edit_message_text("❌ Record not found.")
            return

        del_res = await storage_manager.delete_file(rec, bot=context.bot)
        database.log_audit(user_id, "delete_file", f"id={rec_id}, success={del_res['success']}")

        if del_res["success"]:
            await query.edit_message_text("✅ File successfully deleted from storage backends and database.")
        else:
            b2_str = "✅" if del_res["b2_deleted"] else "❌"
            tg_str = "✅" if del_res["telegram_deleted"] else "❌"
            await query.edit_message_text(f"⚠️ Partial Deletion Result:\nB2: {b2_str}\nTelegram: {tg_str}\nDB: ✅")

    elif data.startswith("dup_use:"):
        rec_id = int(data.split(":", 1)[1])
        pending_duplicates.pop(user_id, None)
        rec = database.get_file_by_id(rec_id)
        if rec:
            await query.edit_message_text(f"📂 *Using Existing File Record (ID: `{rec_id}`)*\nName: `{rec['file_name']}`", parse_mode="Markdown")
        else:
            await query.edit_message_text("❌ Record missing.")

    elif data == "dup_store_again":
        msg = pending_duplicates.pop(user_id, None)
        if msg:
            folder = selected_folder.get(user_id, "default")
            await process_file_upload(msg, folder, context)

    elif data == "dup_cancel":
        pending_duplicates.pop(user_id, None)
        await query.edit_message_text("❌ Upload cancelled.")

async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(context.error, Conflict):
        logger.warning(
            "⚠️ Telegram Conflict (409): Another instance is currently polling with this bot token, "
            "or a Render zero-downtime container transition is in progress. Polling will auto-resume."
        )
    else:
        logger.error(f"Unhandled bot exception: {context.error}", exc_info=context.error)

# ============================================================
# MAIN APPLICATION ENTRY POINT
# ============================================================

def main():
    logger.info("Initializing Anime4u Multi-Backend Storage Bot...")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_error_handler(global_error_handler)

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("folders", folders_command))
    app.add_handler(CommandHandler("upload", start))
    app.add_handler(CommandHandler("newfolder", newfolder_command))
    app.add_handler(CommandHandler("selected", selected_command))
    app.add_handler(CommandHandler("files", files_command))
    app.add_handler(CommandHandler("search", search_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("storage", storage_health_command))
    app.add_handler(CommandHandler("recent", recent_command))
    app.add_handler(CommandHandler("reconcile", reconcile_command))
    app.add_handler(CommandHandler("cancel", cancel_command))

    # Callback Query
    app.add_handler(CallbackQueryHandler(callback_handler))

    # Media & Text Handlers
    app.add_handler(MessageHandler(filters.Document.ALL | filters.VIDEO | filters.AUDIO | filters.PHOTO, handle_media))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Run Startup Health Check before polling
    loop = asyncio.get_event_loop()
    loop.run_until_complete(startup_health_check(app))

    # Start HTTP Health Check Server for Render Web Services & Cloud Run port binding
    start_health_check_server()

    logger.info("🚀 Polling started. Listening for admin requests...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
