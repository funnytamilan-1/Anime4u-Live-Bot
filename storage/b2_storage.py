import os
import logging
from pathlib import Path
from typing import Dict, Any, Optional, List
from storage.base import BaseStorage

logger = logging.getLogger("B2Storage")

class B2Storage(BaseStorage):
    def __init__(self):
        self.key_id = os.getenv("B2_APPLICATION_KEY_ID", "0054467fa469dc20000000002").strip()
        self.application_key = os.getenv("B2_APPLICATION_KEY", "K005ACvE5pP0RYQr4cplDYDSE96uMtA").strip()
        self.bucket_name = os.getenv("B2_BUCKET_NAME", "anime4u-videos").strip()
        self.public_base_url = os.getenv("B2_PUBLIC_BASE_URL", "").rstrip("/")
        self._bucket = None
        self._b2_api = None

    def is_configured(self) -> bool:
        return bool(self.key_id and self.application_key and self.bucket_name)

    def _init_b2_client(self):
        if self._bucket:
            return self._bucket
        if not self.is_configured():
            raise ValueError("Backblaze B2 credentials (B2_APPLICATION_KEY_ID, B2_APPLICATION_KEY, B2_BUCKET_NAME) are not fully configured.")

        try:
            from b2sdk.v2 import InMemoryAccountInfo, B2Api
            info = InMemoryAccountInfo()
            self._b2_api = B2Api(info)
            self._b2_api.authorize_account("production", self.key_id, self.application_key)
            self._bucket = self._b2_api.get_bucket_by_name(self.bucket_name)
            logger.info(f"Successfully connected to Backblaze B2 bucket: {self.bucket_name}")
            return self._bucket
        except Exception as e:
            logger.error(f"Failed to initialize B2 client: {e}")
            raise

    async def validate_connection(self) -> bool:
        """Verifies connection to Backblaze B2 bucket."""
        try:
            bucket = self._init_b2_client()
            return bucket is not None
        except Exception as e:
            logger.error(f"B2 Connection validation failed: {e}")
            return False

    def normalize_b2_path(self, folder: str, file_name: str) -> str:
        """Constructs safe normalized B2 object path: folder/file_name."""
        clean_folder = folder.strip("/").strip()
        clean_file = Path(file_name).name.replace("\0", "").replace("..", "_")
        if clean_folder:
            return f"{clean_folder}/{clean_file}"
        return clean_file

    async def store_file(self, file_path: Path, target_path: str, **kwargs) -> Dict[str, Any]:
        """Uploads a local file to Backblaze B2 bucket."""
        bucket = self._init_b2_client()
        local_file = str(file_path)

        if not file_path.exists():
            raise FileNotFoundError(f"Local file not found for B2 upload: {local_file}")

        logger.info(f"Uploading file {local_file} to B2 path: {target_path}")

        # Sync upload via B2 SDK
        file_version = bucket.upload_local_file(
            local_file=local_file,
            file_name=target_path
        )

        b2_file_id = file_version.id_
        if self.public_base_url:
            url = f"{self.public_base_url}/{target_path}"
        else:
            url = f"https://f000.backblazeb2.com/file/{self.bucket_name}/{target_path}"

        return {
            "b2_file_id": b2_file_id,
            "b2_path": target_path,
            "b2_url": url,
            "b2_status": "success",
            "file_size": file_version.size
        }

    async def delete_file(self, reference_id: str, b2_path: Optional[str] = None, **kwargs) -> bool:
        """Deletes a file version from Backblaze B2."""
        try:
            bucket = self._init_b2_client()
            if reference_id and b2_path:
                bucket.delete_file_version(reference_id, b2_path)
            else:
                # If only path is provided, hide or delete file
                file_info = bucket.get_file_info_by_name(b2_path or reference_id)
                bucket.delete_file_version(file_info.id_, file_info.file_name)
            logger.info(f"Deleted B2 file reference: {reference_id} ({b2_path})")
            return True
        except Exception as e:
            logger.error(f"Failed to delete B2 file {reference_id}: {e}")
            return False

    async def file_exists(self, reference_id: str) -> bool:
        """Checks if a file exists in B2 by path."""
        try:
            bucket = self._init_b2_client()
            file_info = bucket.get_file_info_by_name(reference_id)
            return file_info is not None
        except Exception:
            return False

    async def get_file(self, reference_id: str) -> Optional[Dict[str, Any]]:
        """Gets B2 file details by path."""
        try:
            bucket = self._init_b2_client()
            info = bucket.get_file_info_by_name(reference_id)
            if self.public_base_url:
                url = f"{self.public_base_url}/{reference_id}"
            else:
                url = f"https://f000.backblazeb2.com/file/{self.bucket_name}/{reference_id}"

            return {
                "b2_file_id": info.id_,
                "b2_path": info.file_name,
                "file_size": info.size,
                "b2_url": url
            }
        except Exception:
            return None

    async def list_files(self, prefix: str = "") -> List[Dict[str, Any]]:
        """Lists files in B2 under optional prefix."""
        try:
            bucket = self._init_b2_client()
            results = []
            for file_version, _ in bucket.list_file_names(prefix=prefix):
                results.append({
                    "b2_file_id": file_version.id_,
                    "b2_path": file_version.file_name,
                    "file_size": file_version.size,
                    "created_at": file_version.upload_timestamp
                })
            return results
        except Exception as e:
            logger.error(f"B2 list_files error: {e}")
            return []

    async def get_metadata(self, reference_id: str) -> Optional[Dict[str, Any]]:
        return await self.get_file(reference_id)
