from storage.base import BaseStorage
from storage.b2_storage import B2Storage
from storage.telegram_storage import TelegramStorage
from storage.manager import storage_manager, StorageManager

__all__ = [
    "BaseStorage",
    "B2Storage",
    "TelegramStorage",
    "StorageManager",
    "storage_manager",
]
