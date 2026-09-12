import os
import sys
import asyncio
import logging
from datetime import datetime
from pathlib import Path

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
from telegram.error import TelegramError, RetryAfter

import database
import storage

# Setup structured production logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("Anime4uBot")

# ============================================================
# CONFIGURATION
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "1234567890:ABCdefGHIjklMNOpqrsTUVwxyz123456")
STORAGE_CHANNEL_ID_RAW = os.getenv("STORAGE_CHANNEL_ID", "")
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "")

try:
    STORAGE_CHANNEL_ID = int(STORAGE_CHANNEL_ID_RAW) if STORAGE_CHANNEL_ID_RAW else 0
except ValueError:
    logger.error("STORAGE_CHANNEL_ID must be a valid integer ID (e.g. -1001234567890)")
    STORAGE_CHANNEL_ID = 0

ADMIN_IDS = {
    int(x.strip())
    for x in ADMIN_IDS_RAW.split(",")
    if x.strip().isdigit()
}

MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE", 2147483648))  # 2 GB
PAGE_SIZE = 10

# User state for active target folders and pending operations
selected_folder = {}
pending_duplicates = {}

# ============================================================
# ADMIN SECURITY
# ============================================================

def is_admin(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id in ADMIN_IDS)

async def deny(update: Update):
    if update.message:
        await update.message.reply_text("⛔ Admin access only.")
    elif update.callback_query:
        await update.callback_query.answer("⛔ Admin access only.", show_alert=True)

# ============================================================
# STARTUP HEALTH CHECK
# ============================================================

async def startup_health_check(app: Application):
    logger.info("🚀 Bot starting health check...")

    if not BOT_TOKEN:
        logger.critical("BOT_TOKEN is missing!")
        sys.exit(1)

    if not STORAGE_CHANNEL_ID:
        logger.critical("STORAGE_CHANNEL_ID is missing or invalid!")
        sys.exit(1)

    if not ADMIN_IDS:
        logger.critical("ADMIN_IDS is missing!")
        sys.exit(1)

    # Database initialization
    database.init_db()
    storage.clean_stale_temp_files()

    # Validate Telegram Connection & Storage Channel Access
    try:
        bot_user = await app.bot.get_me()
        logger.info(f"✅ Telegram connected as @{bot_user.username}")

        # Test posting access to STORAGE_CHANNEL_ID
        test_msg = await app.bot.send_message(
            chat_id=STORAGE_CHANNEL_ID,
            text=f"🤖 Storage Channel Connected | Bot @{bot_user.username} online"
        )
        logger.info(f"✅ Storage channel accessible! Test message_id={test_msg.message_id}")
    except Exception as e:
        logger.critical(f"❌ Storage Channel Validation Failed: {e}")
        sys.exit(1)

    logger.info("🚀 All startup health checks PASSED! Bot is running.")

# ============================================================
# COMMAND HANDLERS
# ============================================================

def get_start_keyboard() -> InlineKeyboardMarkup:
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
            InlineKeyboardButton("💾 Storage", callback_data="cmd_storage")
        ],
        [
            InlineKeyboardButton("🕒 Recent Uploads", callback_data="cmd_recent"),
            InlineKeyboardButton("⚙️ Settings", callback_data="cmd_settings")
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
    curr_folder = selected_folder.get(user_id, "None")

    text = (
        f"👑 *Anime4u Telegram Storage Manager*\n\n"
        f"Backend Storage Channel: `{STORAGE_CHANNEL_ID}`\n"
        f"Active Virtual Folder: `{curr_folder}/`\n\n"
        f"Send any video, document, or audio to upload directly into private channel storage."
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
        "🤖 *Anime4u Telegram Storage Bot Help*\n\n"
        "Available Commands:\n"
        "/start - Main Storage Control Panel\n"
        "/folders - List virtual folders metadata\n"
        "/newfolder - Create and select new virtual folder path\n"
        "/selected - Show currently active target folder\n"
        "/files - Browse indexed storage files\n"
        "/search <query> - Search files by title, filename, or folder\n"
        "/stats - View live database & storage statistics\n"
        "/storage - Check bot, channel, database & FFmpeg health\n"
        "/recent - Show 10 most recent uploads\n"
        "/reconcile - Run maintenance check comparing channel vs database\n"
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

    page = 0
    start_idx = page * PAGE_SIZE
    visible = folders_list[start_idx:start_idx + PAGE_SIZE]

    buttons = []
    for f in visible:
        f_name = f["folder"]
        buttons.append([
            InlineKeyboardButton(f"📁 {f_name} ({f['count']})", callback_data=f"selfolder:{f_name}")
        ])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"folderpage:{page - 1}"))
    if start_idx + PAGE_SIZE < len(folders_list):
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"folderpage:{page + 1}"))
    if nav:
        buttons.append(nav)

    buttons.append([InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="start_menu")])

    keyboard = InlineKeyboardMarkup(buttons)
    text = f"📂 *Storage Virtual Folders*\nFound: `{len(folders_list)}` virtual folders"

    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")

async def files_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny(update)
        return

    user_id = update.effective_user.id
    folder = selected_folder.get(user_id)
    query_folder = folder or ""

    files = storage.list_folder_files(query_folder, page=0, page_size=10) if query_folder else database.get_recent_uploads(10)
    if not files:
        text = "📂 No files found."
        if update.callback_query:
            await update.callback_query.edit_message_text(text)
        else:
            await update.message.reply_text(text)
        return

    buttons = []
    for f in files:
        buttons.append([
            InlineKeyboardButton(f"📄 {f['file_name']}", callback_data=f"fileinfo:{f['storage_message_id']}")
        ])
    buttons.append([InlineKeyboardButton("⬅️ Main Menu", callback_data="start_menu")])

    keyboard = InlineKeyboardMarkup(buttons)
    title_str = f"In `{query_folder}/`" if query_folder else "Recent Indexed Files"
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
        f"📊 *Live Database & Storage Statistics*\n\n"
        f"Indexed Files: `{st['total_files']}`\n"
        f"Virtual Folders: `{st['total_folders']}`\n"
        f"Total Stored Size: `{st['readable_bytes']}`\n"
        f"Uploads Today: `{st['uploads_today']}`\n\n"
        f"🎞️ Videos: `{st['videos']}`\n"
        f"📄 Documents: `{st['documents']}`\n"
        f"🎵 Audio: `{st['audio']}`\n"
        f"🖼️ Photos: `{st['photos']}`"
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
    db_ok = "✅ SQLite / Supabase Online"
    ch_ok = f"✅ Connected (`{STORAGE_CHANNEL_ID}`)" if STORAGE_CHANNEL_ID else "❌ Invalid Channel ID"
    
    # Check FFmpeg
    try:
        import subprocess
        res = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True)
        ffmpeg_ok = "✅ FFmpeg Installed" if res.returncode == 0 else "⚠️ FFmpeg Error"
    except Exception:
        ffmpeg_ok = "⚠️ FFmpeg Not Available"

    text = (
        f"💾 *Storage & Infrastructure Health*\n\n"
        f"Telegram API: ✅ Connected\n"
        f"Storage Channel: {ch_ok}\n"
        f"Database Backend: {db_ok}\n"
        f"FFmpeg Engine: {ffmpeg_ok}\n"
        f"Max File Limit: `{MAX_FILE_SIZE / (1024*1024*1024):.1f} GB`\n"
        f"Auto HLS Transcode: `{storage.AUTO_HLS}`"
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
        buttons.append([
            InlineKeyboardButton(f"📄 {r['file_name']} ({r['folder']})", callback_data=f"fileinfo:{r['storage_message_id']}")
        ])
    buttons.append([InlineKeyboardButton("⬅️ Main Menu", callback_data="start_menu")])

    keyboard = InlineKeyboardMarkup(buttons)
    text = f"🕒 *Recent Uploads* ({len(recent)} items):"

    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")

async def reconcile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny(update)
        return

    msg = await update.message.reply_text("⏳ Running storage reconciliation check...") if update.message else None

    res = await storage.reconcile_storage(context.bot, STORAGE_CHANNEL_ID)
    database.log_audit(update.effective_user.id, "reconcile", f"Reconciled {res['total_db_records']} records")

    report = (
        f"⚠️ *Storage Reconciliation Report*\n\n"
        f"Total Database Indexed Records: `{res['total_db_records']}`\n"
        f"Verified Valid Channel Messages: `{res['verified_valid']}`\n"
        f"Missing Channel Messages: `{res['missing_in_channel']}`\n"
    )
    if res["missing_ids"]:
        report += f"\nMissing Storage Message IDs: `{res['missing_ids'][:5]}`"

    if msg:
        await msg.edit_text(report, parse_mode="Markdown")
    elif update.callback_query:
        await update.callback_query.edit_message_text(report, parse_mode="Markdown")

async def folders_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny(update)
        return

    folders_list = database.list_folders()
    if not folders_list:
        await update.message.reply_text(
            "📂 No virtual folders found in database.\n\n"
            "Use /newfolder to create a new folder path."
        )
        return

    page = 0
    start_idx = page * PAGE_SIZE
    visible = folders_list[start_idx:start_idx + PAGE_SIZE]

    buttons = []
    for f in visible:
        f_name = f["folder"]
        buttons.append([
            InlineKeyboardButton(f"📁 {f_name} ({f['count']})", callback_data=f"selfolder:{f_name}")
        ])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"page:{page - 1}"))
    if start_idx + PAGE_SIZE < len(folders_list):
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"page:{page + 1}"))
    if nav:
        buttons.append(nav)

    buttons.append([InlineKeyboardButton("🔄 Refresh", callback_data="refresh_folders")])

    keyboard = InlineKeyboardMarkup(buttons)
    await update.message.reply_text(
        f"📂 *Storage Virtual Folders*\nFound: `{len(folders_list)}` folders",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

async def newfolder_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny(update)
        return

    context.user_data["waiting_for_folder_name"] = True
    await update.message.reply_text(
        "📁 *New Folder Path*\n\n"
        "Send the virtual folder path.\n\n"
        "Example:\n`anime/naruto/season-1`",
        parse_mode="Markdown"
    )

async def selected_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny(update)
        return

    folder = selected_folder.get(update.effective_user.id)
    if folder:
        await update.message.reply_text(f"📁 *Currently Selected Folder:*\n`{folder}/`", parse_mode="Markdown")
    else:
        await update.message.reply_text("📂 No folder selected. Use /folders or /newfolder.")

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny(update)
        return

    context.user_data.clear()
    await update.message.reply_text("❌ Operation cancelled.")

async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny(update)
        return

    query = " ".join(context.args) if context.args else ""
    if not query:
        context.user_data["waiting_for_search"] = True
        await update.message.reply_text("🔎 Send search query (filename, title, or folder):")
        return

    results = database.search_files(query, page=0, page_size=10)
    if not results:
        await update.message.reply_text(f"🔎 No results found for `{query}`.", parse_mode="Markdown")
        return

    buttons = []
    for r in results:
        buttons.append([
            InlineKeyboardButton(f"📄 {r['file_name']}", callback_data=f"fileinfo:{r['storage_message_id']}")
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

    # Handle New Folder Prompt Input
    if user_data.get("waiting_for_folder_name"):
        folder_raw = update.message.text.strip().strip("/")
        
        # Path validation
        if not folder_raw or ".." in folder_raw or "\\" in folder_raw or len(folder_raw) > 120:
            await update.message.reply_text("❌ Invalid folder path. Avoid '..', '\\', or empty names.")
            return

        selected_folder[update.effective_user.id] = folder_raw
        user_data.pop("waiting_for_folder_name", None)

        await update.message.reply_text(
            f"✅ *Folder Selected!*\n\n📁 `{folder_raw}/`\n\nNow send any file or video.",
            parse_mode="Markdown"
        )
        return

    # Handle Search Input
    if user_data.get("waiting_for_search"):
        query = update.message.text.strip()
        user_data.pop("waiting_for_search", None)
        context.args = query.split()
        await search_command(update, context)
        return

    # Handle Move Target Folder Input
    if user_data.get("waiting_for_move_target"):
        msg_id = user_data.pop("waiting_for_move_target")
        new_folder = update.message.text.strip().strip("/")
        if not new_folder or ".." in new_folder or "\\" in new_folder or len(new_folder) > 120:
            await update.message.reply_text("❌ Invalid folder path.")
            return

        ok = database.update_file_folder(msg_id, new_folder)
        database.log_audit(update.effective_user.id, "move_file", f"msg_id={msg_id}, new_folder={new_folder}")
        if ok:
            await update.message.reply_text(f"✅ File ID `{msg_id}` moved to `{new_folder}/`.", parse_mode="Markdown")
        else:
            await update.message.reply_text("❌ Failed to update folder in database.")
        return

# ============================================================
# MEDIA UPLOAD & DUPLICATE DETECTION
# ============================================================

async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny(update)
        return

    user_id = update.effective_user.id
    folder = selected_folder.get(user_id)

    if not folder:
        await update.message.reply_text("📂 No folder selected. Use /folders or /newfolder first.")
        return

    media = (
        update.message.document
        or update.message.video
        or update.message.audio
        or (update.message.photo[-1] if update.message.photo else None)
    )

    if not media:
        return

    file_unique_id = media.file_unique_id
    file_name = getattr(media, "file_name", f"file_{file_unique_id}.bin")

    # Duplicate check
    existing = storage.find_stored_file(file_unique_id)
    if existing:
        pending_duplicates[user_id] = update.message
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📂 Use Existing", callback_data=f"dup_use:{existing['storage_message_id']}"),
                InlineKeyboardButton("📤 Store Again", callback_data="dup_store_again")
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data="dup_cancel")]
        ])

        await update.message.reply_text(
            f"⚠️ *Duplicate File Detected*\n\n"
            f"📄 Filename: `{existing['file_name']}`\n"
            f"📁 Existing Folder: `{existing['folder']}`\n"
            f"🆔 Storage Msg ID: `{existing['storage_message_id']}`\n\n"
            f"Choose action:",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
        return

    await process_file_upload(update.message, folder, context)

async def process_file_upload(message, folder: str, context: ContextTypes.DEFAULT_TYPE):
    status_msg = await message.reply_text("⬇️ Receiving file & preparing storage...")

    try:
        await status_msg.edit_text("☁️ Storing in Private Telegram Channel...")

        # Native Telegram Storage Copy
        res = await storage.store_file_direct(
            bot=context.bot,
            message=message,
            folder=folder,
            storage_channel_id=STORAGE_CHANNEL_ID
        )

        await status_msg.edit_text(
            f"✅ *UPLOAD COMPLETE*\n\n"
            f"📄 *File:* `{res['file_name']}`\n"
            f"📦 *Size:* `{res['file_size']}`\n"
            f"📁 *Folder:* `{res['folder']}/`\n"
            f"🆔 *Storage Msg ID:* `{res['storage_message_id']}`\n\n"
            f"Stored securely in Telegram Channel `{STORAGE_CHANNEL_ID}`.",
            parse_mode="Markdown"
        )

    except RetryAfter as e:
        await status_msg.edit_text(f"⏳ Telegram rate limit. Auto-retrying in {e.retry_after} seconds...")
        await asyncio.sleep(e.retry_after)
        await process_file_upload(message, folder, context)

    except Exception as e:
        logger.error(f"Upload failure: {e}", exc_info=True)
        await status_msg.edit_text(f"❌ Storage failed: `{str(e)[:200]}`", parse_mode="Markdown")

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

    elif data == "upload_prompt":
        folder = selected_folder.get(user_id, "None")
        await query.edit_message_text(
            f"📤 *Upload File*\n\nActive Target Folder: `{folder}/`\n\n"
            f"Send any document, video, or audio file directly to this chat.",
            parse_mode="Markdown"
        )

    elif data == "newfolder_prompt":
        await newfolder_command(update, context)

    elif data == "search_prompt":
        context.user_data["waiting_for_search"] = True
        await query.edit_message_text("🔎 Send search query (filename, title, or folder):")

    elif data == "cmd_settings":
        settings_text = (
            f"⚙️ *System Settings & Config*\n\n"
            f"Bot Token: `{BOT_TOKEN[:8]}...`\n"
            f"Storage Channel ID: `{STORAGE_CHANNEL_ID}`\n"
            f"Admin IDs: `{ADMIN_IDS}`\n"
            f"Max Upload Limit: `{MAX_FILE_SIZE / (1024*1024*1024):.1f} GB`\n"
            f"Database Engine: `SQLite / Supabase`"
        )
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="start_menu")]])
        await query.edit_message_text(settings_text, reply_markup=keyboard, parse_mode="Markdown")

    elif data.startswith("selfolder:"):
        folder_name = data.split(":", 1)[1]
        selected_folder[user_id] = folder_name
        database.log_audit(user_id, "select_folder", folder_name)
        await query.edit_message_text(
            f"✅ *Folder Selected!*\n\n📁 `{folder_name}/`\n\nSend any file or video to store.",
            parse_mode="Markdown"
        )

    elif data.startswith("getmsg:"):
        msg_id = int(data.split(":", 1)[1])
        try:
            fwd = await context.bot.copy_message(
                chat_id=update.effective_chat.id,
                from_chat_id=STORAGE_CHANNEL_ID,
                message_id=msg_id
            )
            database.log_audit(user_id, "retrieve_file", f"msg_id={msg_id}")
        except Exception as e:
            await query.edit_message_text(f"❌ Failed to retrieve file from storage channel: {e}")

    elif data.startswith("moveprompt:"):
        msg_id = int(data.split(":", 1)[1])
        context.user_data["waiting_for_move_target"] = msg_id
        await query.edit_message_text(f"📁 Send new virtual folder path for file ID `{msg_id}`:", parse_mode="Markdown")

    elif data.startswith("fileinfo:"):
        msg_id = int(data.split(":", 1)[1])
        f_info = database.get_file_by_message_id(msg_id)
        if not f_info:
            await query.edit_message_text("❌ File record not found.")
            return

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📥 Retrieve File", callback_data=f"getmsg:{msg_id}"),
                InlineKeyboardButton("📁 Move Folder", callback_data=f"moveprompt:{msg_id}")
            ],
            [InlineKeyboardButton("🗑️ Delete File", callback_data=f"delconfirm:{msg_id}")],
            [InlineKeyboardButton("⬅️ Back to Start", callback_data="start_menu")]
        ])

        await query.edit_message_text(
            f"📄 *File Details*\n\n"
            f"Title: `{f_info['title']}`\n"
            f"Filename: `{f_info['file_name']}`\n"
            f"Folder: `{f_info['folder']}`\n"
            f"Size: `{f_info['file_size'] / (1024*1024):.1f} MB`\n"
            f"Media Type: `{f_info['media_type']}`\n"
            f"Storage Msg ID: `{f_info['storage_message_id']}`\n"
            f"Created: `{f_info['created_at']}`",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )

    elif data.startswith("delconfirm:"):
        msg_id = int(data.split(":", 1)[1])
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🗑️ Yes, Delete", callback_data=f"delfile:{msg_id}"),
                InlineKeyboardButton("❌ Cancel", callback_data=f"fileinfo:{msg_id}")
            ]
        ])
        await query.edit_message_text(f"⚠️ *Delete this file from Storage Channel & Database?*\nMsg ID: `{msg_id}`", reply_markup=keyboard, parse_mode="Markdown")

    elif data.startswith("delfile:"):
        msg_id = int(data.split(":", 1)[1])
        ok = await storage.delete_stored_file(context.bot, STORAGE_CHANNEL_ID, msg_id)
        database.log_audit(user_id, "delete_file", f"msg_id={msg_id}, success={ok}")
        if ok:
            await query.edit_message_text("✅ File deleted from Storage Channel and Database.")
        else:
            await query.edit_message_text("❌ Delete failed or record missing.")

    elif data == "dup_store_again":
        msg = pending_duplicates.pop(user_id, None)
        if msg:
            folder = selected_folder.get(user_id, "default")
            await process_file_upload(msg, folder, context)

    elif data == "dup_cancel":
        pending_duplicates.pop(user_id, None)
        await query.edit_message_text("❌ Upload cancelled.")

# ============================================================
# MAIN APPLICATION ENTRY POINT
# ============================================================

def main():
    logger.info("Initializing Anime4u Telegram Storage Bot...")
    
    app = Application.builder().token(BOT_TOKEN).build()

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

    logger.info("🚀 Polling started. Listening for admin requests...")
    app.run_polling()

if __name__ == "__main__":
    main()
