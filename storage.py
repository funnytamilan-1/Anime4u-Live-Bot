import os
import asyncio
import logging
import subprocess
import shutil
from pathlib import Path
from typing import Dict, Any, Optional, List
from datetime import datetime

import database

logger = logging.getLogger("StorageEngine")

TEMP_DIR = Path(os.getenv("TEMP_DIR", "./downloads"))
TEMP_DIR.mkdir(parents=True, exist_ok=True)

MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE", 2147483648))  # 2 GB default
MAX_CONCURRENT_UPLOADS = int(os.getenv("MAX_CONCURRENT_UPLOADS", 2))
MAX_CONCURRENT_FFMPEG = int(os.getenv("MAX_CONCURRENT_FFMPEG", 1))
AUTO_HLS = os.getenv("AUTO_HLS", "false").lower() == "true"

upload_semaphore = asyncio.Semaphore(MAX_CONCURRENT_UPLOADS)
ffmpeg_semaphore = asyncio.Semaphore(MAX_CONCURRENT_FFMPEG)
transcode_lock = asyncio.Lock()

def safe_filename(filename: str) -> str:
    """Sanitizes filenames to prevent path traversal and shell execution risks."""
    safe_name = Path(filename).name.replace("\0", "").replace("..", "_")
    safe_name = "".join(c for c in safe_name if c.isalnum() or c in "._- ")
    return safe_name.strip() or "unnamed_file.bin"

def clean_stale_temp_files(max_age_hours: int = 24):
    """Clean temp download files older than max_age_hours."""
    now = datetime.now().timestamp()
    count = 0
    for file in TEMP_DIR.glob("*"):
        try:
            if file.is_file() and (now - file.stat().st_mtime) > (max_age_hours * 3600):
                file.unlink()
                count += 1
            elif file.is_dir() and (now - file.stat().st_mtime) > (max_age_hours * 3600):
                shutil.rmtree(file, ignore_errors=True)
                count += 1
        except Exception as e:
            logger.warning(f"Error cleaning temp file {file}: {e}")
    if count > 0:
        logger.info(f"Cleaned {count} stale temp files from {TEMP_DIR}")

async def store_file_direct(bot, message, folder: str, storage_channel_id: int) -> Dict[str, Any]:
    """
    Stores a Telegram media file directly into the storage channel without downloading to disk
    if the media can be copied/forwarded natively. Uses upload_semaphore for concurrency limits.
    """
    async with upload_semaphore:
        media = message.document or message.video or message.audio or message.photo or message.animation
        if not media:
            raise ValueError("No supported media found in message")

        if isinstance(media, list):  # Photos come as a list
            media = media[-1]

        file_size = getattr(media, "file_size", 0) or 0
        if file_size > MAX_FILE_SIZE:
            raise ValueError(f"File size ({file_size} bytes) exceeds MAX_FILE_SIZE ({MAX_FILE_SIZE} bytes)")

        raw_name = getattr(media, "file_name", None) or f"file_{media.file_unique_id}.bin"
        file_name = safe_filename(raw_name)
        mime_type = getattr(media, "mime_type", "application/octet-stream")
        file_unique_id = media.file_unique_id
        file_id = media.file_id

        # Format caption for Storage Channel
        readable_size = f"{file_size / (1024 * 1024):.1f} MB" if file_size else "Unknown"
        caption = (
            f"📦 FILE STORAGE\n\n"
            f"📄 Name: {file_name}\n"
            f"📁 Folder: {folder}\n"
            f"📦 Size: {readable_size}\n"
            f"🆔 File ID: {file_unique_id}\n"
            f"📅 Stored: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"
        )

        # Fast Telegram-to-Telegram Copy
        copied_msg = await bot.copy_message(
            chat_id=storage_channel_id,
            from_chat_id=message.chat_id,
            message_id=message.message_id,
            caption=caption
        )

        storage_message_id = copied_msg.message_id

        record = {
            "storage_message_id": storage_message_id,
            "storage_channel_id": storage_channel_id,
            "file_id": file_id,
            "file_unique_id": file_unique_id,
            "file_name": file_name,
            "file_size": file_size,
            "mime_type": mime_type,
            "media_type": "video" if message.video else "document",
            "folder": folder,
            "title": Path(file_name).stem,
            "created_at": datetime.utcnow().isoformat()
        }

        db_ok = database.insert_file_record(record)
        if not db_ok:
            logger.error(f"Failed to insert database record for msg_id={storage_message_id}")

        return {
            "storage_message_id": storage_message_id,
            "file_name": file_name,
            "file_size": readable_size,
            "folder": folder,
            "db_saved": db_ok
        }

async def reconcile_storage(bot, storage_channel_id: int) -> Dict[str, Any]:
    """
    Compares database metadata records against real Telegram Storage Channel messages.
    Identifies missing channel messages or inconsistent records without destructive auto-deletion.
    """
    db_msg_ids = database.list_all_message_ids()
    missing_in_channel = []
    verified_valid = []

    for msg_id in db_msg_ids:
        try:
            # Check if message exists by attempting a temporary message forward
            fwd = await bot.forward_message(
                chat_id=storage_channel_id,
                from_chat_id=storage_channel_id,
                message_id=msg_id
            )
            await bot.delete_message(chat_id=storage_channel_id, message_id=fwd.message_id)
            verified_valid.append(msg_id)
        except Exception as e:
            logger.warning(f"Reconciliation check failed for storage_message_id={msg_id}: {e}")
            missing_in_channel.append(msg_id)

    return {
        "total_db_records": len(db_msg_ids),
        "verified_valid": len(verified_valid),
        "missing_in_channel": len(missing_in_channel),
        "missing_ids": missing_in_channel,
    }

async def get_stored_file(bot, storage_channel_id: int, storage_message_id: int) -> Optional[Dict[str, Any]]:
    """Retrieves file record and verifies its existence in the storage channel."""
    rec = database.get_file_by_message_id(storage_message_id)
    if not rec:
        return None

    try:
        msg = await bot.forward_message(
            chat_id=storage_channel_id,
            from_chat_id=storage_channel_id,
            message_id=storage_message_id
        )
        await bot.delete_message(chat_id=storage_channel_id, message_id=msg.message_id)
        rec["verified"] = True
    except Exception as e:
        logger.warning(f"Could not verify storage message {storage_message_id}: {e}")
        rec["verified"] = False

    return rec

async def delete_stored_file(bot, storage_channel_id: int, storage_message_id: int) -> bool:
    """Deletes message from storage channel and removes DB record."""
    try:
        await bot.delete_message(chat_id=storage_channel_id, message_id=storage_message_id)
    except Exception as e:
        logger.warning(f"Could not delete message {storage_message_id} from channel: {e}")

    return database.delete_file_record(storage_message_id)

def find_stored_file(file_unique_id: str) -> Optional[Dict[str, Any]]:
    return database.find_by_unique_id(file_unique_id)

def list_folder_files(folder: str, page: int = 0, page_size: int = 10) -> List[Dict[str, Any]]:
    conn = database.get_sqlite_conn()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM file_records 
        WHERE folder = ?
        ORDER BY created_at DESC
        LIMIT ? OFFSET ?
    """, (folder, page_size, page * page_size))
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]
