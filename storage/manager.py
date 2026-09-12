import os
import asyncio
import logging
from pathlib import Path
from typing import Dict, Any, Optional, List
from datetime import datetime

import database
from storage.b2_storage import B2Storage
from storage.telegram_storage import TelegramStorage

logger = logging.getLogger("StorageManager")

class StorageManager:
    def __init__(self):
        self.global_mode = os.getenv("STORAGE_MODE", "telegram").lower().strip()
        if self.global_mode not in ("b2", "telegram", "both"):
            logger.warning(f"Invalid STORAGE_MODE '{self.global_mode}', defaulting to 'telegram'")
            self.global_mode = "telegram"

        self.b2_storage = B2Storage()
        self.telegram_storage = TelegramStorage()

    def set_global_mode(self, new_mode: str):
        if new_mode in ("b2", "telegram", "both"):
            self.global_mode = new_mode
            logger.info(f"Updated runtime global storage mode to: {new_mode}")
            return True
        return False

    def validate_startup_config(self) -> List[str]:
        """
        Validates environment variables based on active STORAGE_MODE.
        Returns a list of missing required variable names.
        """
        missing = []

        bot_token = os.getenv("BOT_TOKEN", "").strip()
        admin_ids = os.getenv("ADMIN_IDS", "").strip()

        if not bot_token:
            missing.append("BOT_TOKEN")
        if not admin_ids:
            missing.append("ADMIN_IDS")

        if self.global_mode in ("b2", "both"):
            if not os.getenv("B2_APPLICATION_KEY_ID", "").strip():
                missing.append("B2_APPLICATION_KEY_ID")
            if not os.getenv("B2_APPLICATION_KEY", "").strip():
                missing.append("B2_APPLICATION_KEY")
            if not os.getenv("B2_BUCKET_NAME", "").strip():
                missing.append("B2_BUCKET_NAME")

        if self.global_mode in ("telegram", "both"):
            if not os.getenv("STORAGE_CHANNEL_ID", "").strip():
                missing.append("STORAGE_CHANNEL_ID")

        return missing

    def get_effective_mode(self, folder: str) -> str:
        """Determines storage mode for a folder, falling back to global_mode."""
        folder_mode = database.get_folder_storage_mode(folder)
        if folder_mode in ("b2", "telegram", "both"):
            return folder_mode
        return self.global_mode

    async def store_file(
        self,
        bot,
        message,
        folder: str,
        local_file_path: Optional[Path] = None,
        max_retries: int = 3
    ) -> Dict[str, Any]:
        """
        Stores file using effective storage mode (b2, telegram, or both).
        Handles retries, FloodWait, and partial status tracking.
        """
        mode = self.get_effective_mode(folder)
        logger.info(f"Storing file in folder '{folder}' using mode: {mode}")

        media = message.document or message.video or message.audio or message.photo or message.animation
        if isinstance(media, list):
            media = media[-1]

        raw_name = getattr(media, "file_name", None) or f"file_{getattr(media, 'file_unique_id', 'unknown')}.bin"
        file_name = Path(raw_name).name.replace("\0", "").replace("..", "_")
        file_size = getattr(media, "file_size", 0) or 0
        mime_type = getattr(media, "mime_type", "application/octet-stream")
        file_unique_id = getattr(media, "file_unique_id", None)
        file_id = getattr(media, "file_id", None)
        media_type = "video" if message.video else ("audio" if message.audio else ("photo" if message.photo else "document"))

        b2_res: Optional[Dict[str, Any]] = None
        tg_res: Optional[Dict[str, Any]] = None
        b2_err: Optional[str] = None
        tg_err: Optional[str] = None

        # 1. Backblaze B2 Upload
        if mode in ("b2", "both"):
            temp_path = local_file_path
            created_temp = False

            try:
                if not temp_path or not temp_path.exists():
                    # Download file locally to temp for B2 upload
                    temp_dir = Path(os.getenv("TEMP_DIR", "./downloads"))
                    temp_dir.mkdir(parents=True, exist_ok=True)
                    temp_path = temp_dir / f"temp_{file_unique_id}_{file_name}"

                    logger.info(f"Downloading file from Telegram for B2 upload: {temp_path}")
                    file_obj = await bot.get_file(file_id)
                    await file_obj.download_to_drive(custom_path=temp_path)
                    created_temp = True

                b2_target_path = self.b2_storage.normalize_b2_path(folder, file_name)
                
                # Retry logic for B2
                for attempt in range(1, max_retries + 1):
                    try:
                        b2_res = await self.b2_storage.store_file(temp_path, b2_target_path)
                        break
                    except Exception as e:
                        logger.warning(f"B2 upload attempt {attempt} failed: {e}")
                        if attempt == max_retries:
                            b2_err = str(e)
                        else:
                            await asyncio.sleep(2 ** attempt)

            except Exception as e:
                logger.error(f"Failed to prepare B2 upload: {e}")
                b2_err = str(e)
            finally:
                if created_temp and temp_path and temp_path.exists():
                    try:
                        temp_path.unlink()
                    except Exception:
                        pass

        # 2. Telegram Storage Channel Upload
        if mode in ("telegram", "both"):
            # Retry logic for Telegram
            for attempt in range(1, max_retries + 1):
                try:
                    tg_res = await self.telegram_storage.store_file_direct(bot, message, folder)
                    break
                except Exception as e:
                    # Check FloodWait
                    if hasattr(e, "retry_after"):
                        wait_sec = getattr(e, "retry_after", 5)
                        logger.warning(f"Telegram FloodWait encountered, sleeping for {wait_sec}s")
                        await asyncio.sleep(wait_sec)
                    else:
                        logger.warning(f"Telegram upload attempt {attempt} failed: {e}")
                        if attempt == max_retries:
                            tg_err = str(e)
                        else:
                            await asyncio.sleep(2 ** attempt)

        # Evaluate final status
        b2_ok = bool(b2_res and b2_res.get("b2_status") == "success")
        tg_ok = bool(tg_res and tg_res.get("telegram_status") == "success")

        if mode == "b2":
            status = "complete" if b2_ok else "failed"
        elif mode == "telegram":
            status = "complete" if tg_ok else "failed"
        else:  # both
            if b2_ok and tg_ok:
                status = "complete"
            elif b2_ok or tg_ok:
                status = "partial_success"
            else:
                status = "failed"

        # Record payload for database
        record = {
            "file_name": file_name,
            "file_size": file_size,
            "mime_type": mime_type,
            "media_type": media_type,
            "folder": folder,
            "storage_mode": mode,
            "b2_file_id": b2_res.get("b2_file_id") if b2_res else None,
            "b2_path": b2_res.get("b2_path") if b2_res else None,
            "b2_url": b2_res.get("b2_url") if b2_res else None,
            "b2_status": "success" if b2_ok else ("failed" if mode in ("b2", "both") else "none"),
            "telegram_channel_id": tg_res.get("telegram_channel_id") if tg_res else None,
            "telegram_message_id": tg_res.get("telegram_message_id") if tg_res else None,
            "telegram_file_id": file_id,
            "telegram_file_unique_id": file_unique_id,
            "telegram_status": "success" if tg_ok else ("failed" if mode in ("telegram", "both") else "none"),
            "status": status,
            "title": Path(file_name).stem,
            "created_at": datetime.utcnow().isoformat()
        }

        # Index in database
        record_id = database.insert_file_record(record)
        record["id"] = record_id

        return {
            "record": record,
            "record_id": record_id,
            "status": status,
            "b2_ok": b2_ok,
            "b2_err": b2_err,
            "tg_ok": tg_ok,
            "tg_err": tg_err,
            "mode": mode
        }

    async def delete_file(self, record: Dict[str, Any], bot=None) -> Dict[str, Any]:
        """Deletes file from B2, Telegram Storage Channel, and Database."""
        b2_deleted = True
        tg_deleted = True

        b2_file_id = record.get("b2_file_id")
        b2_path = record.get("b2_path")
        if b2_file_id or b2_path:
            b2_deleted = await self.b2_storage.delete_file(b2_file_id or b2_path, b2_path=b2_path)

        tg_msg_id = record.get("telegram_message_id")
        if tg_msg_id and bot:
            tg_deleted = await self.telegram_storage.delete_file(str(tg_msg_id), bot=bot)

        # Delete database record if either deletion was attempted
        db_deleted = database.delete_file_record(record["id"])

        success = b2_deleted and tg_deleted and db_deleted

        return {
            "success": success,
            "b2_deleted": b2_deleted,
            "telegram_deleted": tg_deleted,
            "db_deleted": db_deleted
        }

    async def reconcile(self, bot=None) -> Dict[str, Any]:
        """
        Reconciles database records against B2 bucket and Telegram Storage Channel.
        Does NOT perform destructive auto-deletion of production data.
        """
        records = database.list_all_records()

        total = len(records)
        b2_missing = []
        tg_missing = []
        verified_b2 = 0
        verified_tg = 0

        for r in records:
            # Check B2
            if r.get("b2_path"):
                exists = await self.b2_storage.file_exists(r["b2_path"])
                if exists:
                    verified_b2 += 1
                else:
                    b2_missing.append(r)

            # Check Telegram
            if r.get("telegram_message_id") and bot:
                exists = await self.telegram_storage.file_exists(str(r["telegram_message_id"]), bot=bot)
                if exists:
                    verified_tg += 1
                else:
                    tg_missing.append(r)

        return {
            "total_records": total,
            "verified_b2": verified_b2,
            "b2_missing_count": len(b2_missing),
            "b2_missing_ids": [r["id"] for r in b2_missing],
            "verified_tg": verified_tg,
            "tg_missing_count": len(tg_missing),
            "tg_missing_ids": [r["id"] for r in tg_missing]
        }

# Global singleton storage manager
storage_manager = StorageManager()
