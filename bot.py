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

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny(update)
        return

    user_id = update.effective_user.id
    curr_folder = selected_folder.get(user_id, "None")

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Storage Status", callback_data="status"),
            InlineKeyboardButton("📂 Folders", callback_data="folders_0")
        ],
        [
            InlineKeyboardButton("📤 Upload File", callback_data="upload_prompt"),
            InlineKeyboardButton("📁 New Folder", callback_data="newfolder_prompt")
        ],
        [
            InlineKeyboardButton("🔎 Search Storage", callback_data="search_prompt"),
            InlineKeyboardButton("⚙️ Settings", callback_data="settings")
        ]
    ])

    await update.message.reply_text(
        f"👑 *Anime4u Telegram Storage Manager*\n\n"
        f"Backend Storage Channel: `{STORAGE_CHANNEL_ID}`\n"
        f"Active Folder: `{curr_folder}/`\n\n"
        f"Send any video or document to upload directly into storage.",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

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

    if data.startswith("selfolder:"):
        folder_name = data.split(":", 1)[1]
        selected_folder[user_id] = folder_name
        await query.edit_message_text(
            f"✅ *Folder Selected!*\n\n📁 `{folder_name}/`\n\nSend any file or video to store.",
            parse_mode="Markdown"
        )

    elif data.startswith("fileinfo:"):
        msg_id = int(data.split(":", 1)[1])
        f_info = database.get_file_by_message_id(msg_id)
        if not f_info:
            await query.edit_message_text("❌ File record not found.")
            return

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📥 Retrieve", callback_data=f"getmsg:{msg_id}"),
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
    app.add_handler(CommandHandler("folders", folders_command))
    app.add_handler(CommandHandler("upload", start))
    app.add_handler(CommandHandler("newfolder", newfolder_command))
    app.add_handler(CommandHandler("selected", selected_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("search", search_command))

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
