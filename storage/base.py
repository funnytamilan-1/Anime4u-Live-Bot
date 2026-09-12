from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, List
from pathlib import Path

class BaseStorage(ABC):
    @abstractmethod
    async def store_file(self, file_path: Path, target_path: str, **kwargs) -> Dict[str, Any]:
        """Uploads or stores a file in the backend storage."""
        pass

    @abstractmethod
    async def get_file(self, reference_id: str) -> Optional[Dict[str, Any]]:
        """Retrieves file details or download link from backend storage."""
        pass

    @abstractmethod
    async def delete_file(self, reference_id: str, **kwargs) -> bool:
        """Deletes a file from backend storage."""
        pass

    @abstractmethod
    async def file_exists(self, reference_id: str) -> bool:
        """Checks if a file exists in backend storage."""
        pass

    @abstractmethod
    async def list_files(self, prefix: str = "") -> List[Dict[str, Any]]:
        """Lists files in backend storage under a given prefix."""
        pass

    @abstractmethod
    async def get_metadata(self, reference_id: str) -> Optional[Dict[str, Any]]:
        """Gets backend-specific file metadata."""
        pass
