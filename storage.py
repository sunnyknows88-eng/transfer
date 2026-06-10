import os
import json
import datetime
import secrets
from typing import BinaryIO, Optional, Dict, Any, List, Tuple

class StorageProvider:
    """Abstract base class for storage providers."""
    def save_file(self, filename: str, stream: BinaryIO, file_id: Optional[str] = None) -> Tuple[Optional[str], str, Dict[str, Any]]:
        """Saves a file from a stream and returns (file_id, filename, metadata)."""
        raise NotImplementedError

    def get_file_stream(self, filename: str, file_id: Optional[str] = None) -> Tuple[BinaryIO, int, Dict[str, Any]]:
        """Returns a tuple of (stream, size, metadata) for downloading."""
        raise NotImplementedError

    def get_metadata(self, filename: str, file_id: Optional[str] = None) -> Dict[str, Any]:
        """Returns the metadata dictionary for the file."""
        raise NotImplementedError

    def delete_file(self, filename: str, file_id: Optional[str] = None) -> bool:
        """Deletes a file and its metadata, returns True if deleted."""
        raise NotImplementedError

    def list_files(self) -> List[Dict[str, Any]]:
        """Lists metadata for all stored files."""
        raise NotImplementedError

    def exists(self, filename: str, file_id: Optional[str] = None) -> bool:
        """Checks if a file exists in storage."""
        raise NotImplementedError


class LocalStorageProvider(StorageProvider):
    """Local filesystem implementation of StorageProvider."""
    def __init__(self, upload_dir: str):
        self.upload_dir = os.path.abspath(upload_dir)
        os.makedirs(self.upload_dir, exist_ok=True)

    def _get_paths(self, filename: str, file_id: Optional[str] = None) -> Tuple[str, str]:
        """Resolves file and metadata paths, validating against path traversal."""
        # Sanitize components
        safe_filename = os.path.basename(filename)
        
        if file_id:
            # Simple alphanumeric/hyphen/underscore sanitization for file_id
            safe_id = "".join([c for c in file_id if c.isalnum() or c in ("-", "_")])
            target_dir = os.path.join(self.upload_dir, safe_id)
        else:
            target_dir = self.upload_dir

        file_path = os.path.join(target_dir, safe_filename)
        
        # Ensure path is strictly within upload_dir (path traversal check)
        resolved_file_path = os.path.abspath(file_path)
        if not resolved_file_path.startswith(self.upload_dir):
            raise ValueError("Path traversal detected")
            
        metadata_path = resolved_file_path + ".json"
        return resolved_file_path, metadata_path

    def exists(self, filename: str, file_id: Optional[str] = None) -> bool:
        try:
            file_path, _ = self._get_paths(filename, file_id)
            return os.path.exists(file_path)
        except (ValueError, Exception):
            return False

    def save_file(self, filename: str, stream: BinaryIO, file_id: Optional[str] = None) -> Tuple[Optional[str], str, Dict[str, Any]]:
        safe_filename = os.path.basename(filename)
        
        # Handle filename collision if no ID is specified
        if not file_id:
            if self.exists(safe_filename):
                # Generate unique ID to prevent overwriting
                file_id = secrets.token_hex(4)

        file_path, metadata_path = self._get_paths(safe_filename, file_id)
        
        # Create parent directories if needed (e.g. for uploads/file_id/)
        os.makedirs(os.path.dirname(file_path), exist_ok=True)

        # Stream chunk-by-chunk to avoid loading large files in memory
        size = 0
        try:
            with open(file_path, "wb") as f:
                while True:
                    chunk = stream.read(8192)
                    if not chunk:
                        break
                    f.write(chunk)
                    size += len(chunk)
        except Exception as e:
            # Clean up partial file on failure
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except Exception:
                    pass
            # Clean up parent directory if empty
            if file_id:
                parent_dir = os.path.dirname(file_path)
                try:
                    if os.path.exists(parent_dir) and not os.listdir(parent_dir):
                        os.rmdir(parent_dir)
                except Exception:
                    pass
            raise e

        # Build metadata dictionary
        metadata = {
            "filename": safe_filename,
            "file_id": file_id,
            "size": size,
            "uploaded_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }

        # Save metadata sidecar file
        with open(metadata_path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2)

        return file_id, safe_filename, metadata

    def get_file_stream(self, filename: str, file_id: Optional[str] = None) -> Tuple[BinaryIO, int, Dict[str, Any]]:
        file_path, metadata_path = self._get_paths(filename, file_id)
        if not os.path.exists(file_path) or not os.path.exists(metadata_path):
            raise FileNotFoundError("File not found")

        # Load metadata first
        with open(metadata_path, "r", encoding="utf-8") as f:
            metadata = json.load(f)

        # Open file binary stream
        stream = open(file_path, "rb")
        return stream, metadata["size"], metadata

    def get_metadata(self, filename: str, file_id: Optional[str] = None) -> Dict[str, Any]:
        _, metadata_path = self._get_paths(filename, file_id)
        if not os.path.exists(metadata_path):
            raise FileNotFoundError("File not found")

        with open(metadata_path, "r", encoding="utf-8") as f:
            metadata = json.load(f)
        return metadata

    def delete_file(self, filename: str, file_id: Optional[str] = None) -> bool:
        try:
            file_path, metadata_path = self._get_paths(filename, file_id)
        except ValueError:
            return False

        deleted = False

        if os.path.exists(file_path):
            os.remove(file_path)
            deleted = True

        if os.path.exists(metadata_path):
            os.remove(metadata_path)
            deleted = True

        # Clean up empty subdirectory if a file_id was used
        if file_id:
            parent_dir = os.path.dirname(file_path)
            try:
                if os.path.exists(parent_dir) and not os.listdir(parent_dir):
                    os.rmdir(parent_dir)
            except Exception:
                pass

        return deleted

    def list_files(self) -> List[Dict[str, Any]]:
        files_metadata = []
        # Walk upload_dir to find all .json metadata files
        for root, _, files in os.walk(self.upload_dir):
            for file in files:
                if file.endswith(".json") and not file.startswith("."):
                    metadata_path = os.path.join(root, file)
                    try:
                        # Ensure corresponding actual file exists
                        actual_file_path = metadata_path[:-5]
                        if os.path.exists(actual_file_path):
                            with open(metadata_path, "r", encoding="utf-8") as f:
                                metadata = json.load(f)
                            files_metadata.append(metadata)
                        else:
                            # Clean up orphan metadata files
                            os.remove(metadata_path)
                    except Exception:
                        pass
        return files_metadata
