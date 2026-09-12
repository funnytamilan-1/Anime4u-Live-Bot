import os
import logging
from pathlib import Path
from typing import Dict, Any, Optional, List
from datetime import datetime
from storage.base import BaseStorage

logger = logging.getLogger("TelegramStorage")

class TelegramStorage(BaseStorage):
    def __init__(self):
        raw_ch = os.getenv("STORAGE_CHANNEL_ID", "").strip()
        self.storage_channel_id = int(raw_ch) if raw_ch else None
        self.max_file_size = int(os.getenv("MAX_FILE_SIZE", 2147483648))

    def is_configured(self) -> bool:
        return self.storage_channel_id is not None and self.storage_channel_id != 0

    async def validate_connection(self, bot) -> bool:
        """Verifies bot is administrator in the Telegram storage channel."""
        if not self.is_configured():
            return False
        try:
            chat = await bot.get_chat(self.storage_channel_id)
            member = await bot.get_chat_member(self.storage_channel_id, bot.id)
            # Check if bot can post messages
            can_post = getattr(member, "can_post_messages", True) or member.status in ("administrator", "creator")
            logger.info(f"Telegram Storage Channel validated: {chat.title} ({self.storage_channel_id}), status={member.status}")
            return can_post
        except Exception as e:
            logger.error(f"Telegram Storage Channel validation failed: {e}")
            return False

    async def store_file_direct(self, bot, message, folder: str) -> Dict[str, Any]:
        """
        Direct Telegram-to-Telegram copy of user media without downloading to local disk.
        """
        if not self.is_configured():
            raise ValueError("STORAGE_CHANNEL_ID environment variable is not configured.")

        media = message.document or message.video or message.audio or message.photo or message.animation
        if not media:
            raise ValueError("No supported media found in message")

        if isinstance(media, list):
            media = media[-1]

        file_size = getattr(media, "file_size", 0) or 0
        if file_size > self.max_file_size:
            raise ValueError(f"File size ({file_size} bytes) exceeds MAX_FILE_SIZE ({self.max_file_size} bytes)")

        raw_name = getattr(media, "file_name", None) or f"file_{media.file_unique_id}.bin"
        file_name = Path(raw_name).name.replace("\0", "").replace("..", "_")
        mime_type = getattr(media, "mime_type", "application/octet-stream")
        file_unique_id = media.file_unique_id
        file_id = media.file_id

        readable_size = f"{file_size / (1024 * 1024):.1f} MB" if file_size else "Unknown"
        caption = (
            f"📦 TELEGRAM FILE STORAGE\n\n"
            f"📄 Name: {file_name}\n"
            f"📁 Folder: {folder}\n"
            f"📦 Size: {readable_size}\n"
            f"🆔 File ID: {file_unique_id}\n"
            f"📅 Stored: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"
        )

        # Native telegram copy_message
        copied_msg = await bot.copy_message(
            chat_id=self.storage_channel_id,
            from_chat_id=message.chat_id,
            message_id=message.message_id,
            caption=caption
        )

        return {
            "telegram_channel_id": self.storage_channel_id,
            "telegram_message_id": copied_msg.message_id,
            "telegram_file_id": file_id,
            "telegram_file_unique_id": file_unique_id,
            "telegram_status": "success",
            "file_name": file_name,
            "file_size": file_size,
            "mime_type": mime_type,
            "media_type": "video" if message.video else ("audio" if message.audio else ("photo" if message.photo else "document"))
        }

    async def store_file(self, file_path: Path, target_path: str, bot=None, folder: str = "root", **kwargs) -> Dict[str, Any]:
        """Uploads a local disk file to the Telegram Storage Channel."""
        if not self.is_configured():
            raise ValueError("STORAGE_CHANNEL_ID environment variable is not configured.")
        if not bot:
            raise ValueError("Bot instance required for TelegramStorage.store_file")

        file_name = file_path.name
        file_size = file_path.stat().st_size
        readable_size = f"{file_size / (1024 * 1024):.1f} MB" if file_size else "Unknown"
        caption = (
            f"📦 TELEGRAM FILE STORAGE\n\n"
            f"📄 Name: {file_name}\n"
            f"📁 Folder: {folder}\n"
            f"📦 Size: {readable_size}\n"
            f"📅 Stored: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"
        )

        with open(file_path, "rb") as f:
            sent_msg = await bot.send_document(
                chat_id=self.storage_channel_id,
                document=f,
                caption=caption,
                filename=file_name
            )

        doc = sent_msg.document or sent_msg.video or sent_msg.audio
        file_id = doc.file_id if doc else ""
        file_unique_id = doc.file_unique_id if doc else ""

        return {
            "telegram_channel_id": self.storage_channel_id,
            "telegram_message_id": sent_msg.message_id,
            "telegram_file_id": file_id,
            "telegram_file_unique_id": file_unique_id,
            "telegram_status": "success",
            "file_name": file_name,
            "file_size": file_size
        }

    async def delete_file(self, reference_id: str, bot=None, **kwargs) -> bool:
        """Deletes a message from the storage channel."""
        if not bot or not self.is_configured():
            return False
        try:
            msg_id = int(reference_id)
            await bot.delete_message(chat_id=self.storage_channel_id, message_id=msg_id)
            logger.info(f"Deleted Telegram storage message_id={msg_id}")
            return True
        except Exception as e:
            logger.warning(f"Failed to delete Telegram storage message {reference_id}: {e}")
            return False

    async def file_exists(self, reference_id: str, bot=None) -> bool:
        """Verifies existence of message in storage channel by temporary forward."""
        if not bot or not self.is_configured():
            return False
        try:
            msg_id = int(reference_id)
            fwd = await bot.forward_message(
                chat_id=self.storage_channel_id,
                from_chat_id=self.storage_channel_id,
                message_id=msg_id
            )
            await bot.delete_message(chat_id=self.storage_channel_id, message_id=fwd.message_id)
            return True
        except Exception:
            return False

    async def get_file(self, reference_id: str, bot=None) -> Optional[Dict[str, Any]]:
        exists = await self.file_exists(reference_id, bot=bot)
        if exists:
            return {"telegram_message_id": int(reference_id), "status": "exists"}
        return None

    async def list_files(self, prefix: str = "") -> List[Dict[str, Any]]:
        return []

    async def get_metadata(self, reference_id: str) -> Optional[Dict[str, Any]]:
        return await self.get_file(reference_id)
