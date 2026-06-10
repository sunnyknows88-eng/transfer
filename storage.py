import os
import json
import datetime
import secrets
from typing import BinaryIO, Optional, Dict, Any, List, Tuple

# Optional imports for S3 support
try:
    import boto3
    from botocore.exceptions import ClientError
    from botocore.config import Config
except ImportError:
    boto3 = None
    ClientError = None
    Config = None


class StorageProvider:
    """Abstract base class for storage providers."""
    def save_file(self, filename: str, stream: BinaryIO, file_id: Optional[str] = None, content_type: Optional[str] = None) -> Tuple[Optional[str], str, Dict[str, Any]]:
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
        safe_filename = os.path.basename(filename)
        
        if file_id:
            safe_id = "".join([c for c in file_id if c.isalnum() or c in ("-", "_")])
            target_dir = os.path.join(self.upload_dir, safe_id)
        else:
            target_dir = self.upload_dir

        file_path = os.path.join(target_dir, safe_filename)
        
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

    def save_file(self, filename: str, stream: BinaryIO, file_id: Optional[str] = None, content_type: Optional[str] = None) -> Tuple[Optional[str], str, Dict[str, Any]]:
        safe_filename = os.path.basename(filename)
        
        if not file_id:
            if self.exists(safe_filename):
                file_id = secrets.token_hex(4)

        file_path, metadata_path = self._get_paths(safe_filename, file_id)
        os.makedirs(os.path.dirname(file_path), exist_ok=True)

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
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except Exception:
                    pass
            if file_id:
                parent_dir = os.path.dirname(file_path)
                try:
                    if os.path.exists(parent_dir) and not os.listdir(parent_dir):
                        os.rmdir(parent_dir)
                except Exception:
                    pass
            raise e

        metadata = {
            "filename": safe_filename,
            "file_id": file_id,
            "size": size,
            "uploaded_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        if content_type:
            metadata["content_type"] = content_type

        with open(metadata_path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2)

        return file_id, safe_filename, metadata

    def get_file_stream(self, filename: str, file_id: Optional[str] = None) -> Tuple[BinaryIO, int, Dict[str, Any]]:
        file_path, metadata_path = self._get_paths(filename, file_id)
        if not os.path.exists(file_path) or not os.path.exists(metadata_path):
            raise FileNotFoundError("File not found")

        with open(metadata_path, "r", encoding="utf-8") as f:
            metadata = json.load(f)

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
        for root, _, files in os.walk(self.upload_dir):
            for file in files:
                if file.endswith(".json") and not file.startswith("."):
                    metadata_path = os.path.join(root, file)
                    try:
                        actual_file_path = metadata_path[:-5]
                        if os.path.exists(actual_file_path):
                            with open(metadata_path, "r", encoding="utf-8") as f:
                                metadata = json.load(f)
                            files_metadata.append(metadata)
                        else:
                            os.remove(metadata_path)
                    except Exception:
                        pass
        return files_metadata


class S3StorageProvider(StorageProvider):
    """S3-compatible object storage implementation of StorageProvider (supports AWS S3, Cloudflare R2, Backblaze B2, MinIO)."""
    def __init__(self, bucket_name: str, access_key_id: str, secret_access_key: str, endpoint_url: Optional[str] = None, region_name: Optional[str] = None):
        if boto3 is None:
            raise ImportError("The 'boto3' package is required for S3StorageProvider. Add it to requirements.txt.")
        self.bucket_name = bucket_name
        self.s3_client = boto3.client(
            "s3",
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            endpoint_url=endpoint_url,
            region_name=region_name or "us-east-1",
            config=Config(signature_version="s3v4")
        )

    def _get_keys(self, filename: str, file_id: Optional[str] = None) -> Tuple[str, str]:
        safe_filename = os.path.basename(filename)
        if file_id:
            safe_id = "".join([c for c in file_id if c.isalnum() or c in ("-", "_")])
            file_key = f"{safe_id}/{safe_filename}"
        else:
            file_key = safe_filename
        
        metadata_key = file_key + ".json"
        return file_key, metadata_key

    def exists(self, filename: str, file_id: Optional[str] = None) -> bool:
        file_key, _ = self._get_keys(filename, file_id)
        try:
            self.s3_client.head_object(Bucket=self.bucket_name, Key=file_key)
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "404":
                return False
            raise e

    def save_file(self, filename: str, stream: BinaryIO, file_id: Optional[str] = None, content_type: Optional[str] = None) -> Tuple[Optional[str], str, Dict[str, Any]]:
        safe_filename = os.path.basename(filename)
        
        if not file_id:
            if self.exists(safe_filename):
                file_id = secrets.token_hex(4)

        file_key, metadata_key = self._get_keys(safe_filename, file_id)

        # We need to read/buffer the stream as S3 upload_fileobj reads chunks.
        # SizeLimitingStream will raise ValueError if size limit is exceeded during upload.
        try:
            # We upload directly from the stream. Boto3's upload_fileobj handles multi-threading/chunking.
            self.s3_client.upload_fileobj(stream, self.bucket_name, file_key)
        except Exception as e:
            # If upload fails mid-way, ensure any partial file is deleted in S3
            try:
                self.s3_client.delete_object(Bucket=self.bucket_name, Key=file_key)
            except Exception:
                pass
            raise e

        # Get actual object size from S3 to verify
        response = self.s3_client.head_object(Bucket=self.bucket_name, Key=file_key)
        size = response.get("ContentLength", 0)

        metadata = {
            "filename": safe_filename,
            "file_id": file_id,
            "size": size,
            "uploaded_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        if content_type:
            metadata["content_type"] = content_type

        # Save metadata file to S3
        metadata_json = json.dumps(metadata, indent=2)
        self.s3_client.put_object(
            Bucket=self.bucket_name,
            Key=metadata_key,
            Body=metadata_json.encode("utf-8"),
            ContentType="application/json"
        )

        return file_id, safe_filename, metadata

    def get_file_stream(self, filename: str, file_id: Optional[str] = None) -> Tuple[BinaryIO, int, Dict[str, Any]]:
        file_key, _ = self._get_keys(filename, file_id)
        metadata = self.get_metadata(filename, file_id)

        try:
            response = self.s3_client.get_object(Bucket=self.bucket_name, Key=file_key)
            # response['Body'] is a StreamingBody which acts as a binary stream
            return response["Body"], metadata["size"], metadata
        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchKey":
                raise FileNotFoundError("File not found")
            raise e

    def get_metadata(self, filename: str, file_id: Optional[str] = None) -> Dict[str, Any]:
        _, metadata_key = self._get_keys(filename, file_id)
        try:
            response = self.s3_client.get_object(Bucket=self.bucket_name, Key=metadata_key)
            metadata_content = response["Body"].read().decode("utf-8")
            return json.loads(metadata_content)
        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchKey":
                raise FileNotFoundError("Metadata not found")
            raise e

    def delete_file(self, filename: str, file_id: Optional[str] = None) -> bool:
        file_key, metadata_key = self._get_keys(filename, file_id)
        deleted = False

        try:
            self.s3_client.delete_object(Bucket=self.bucket_name, Key=file_key)
            deleted = True
        except ClientError:
            pass

        try:
            self.s3_client.delete_object(Bucket=self.bucket_name, Key=metadata_key)
            deleted = True
        except ClientError:
            pass

        return deleted

    def list_files(self) -> List[Dict[str, Any]]:
        files_metadata = []
        paginator = self.s3_client.get_paginator("list_objects_v2")
        
        try:
            for page in paginator.paginate(Bucket=self.bucket_name):
                for obj in page.get("Contents", []):
                    key = obj["Key"]
                    if key.endswith(".json") and key != ".last_cleanup.json":
                        try:
                            response = self.s3_client.get_object(Bucket=self.bucket_name, Key=key)
                            metadata_content = response["Body"].read().decode("utf-8")
                            metadata = json.loads(metadata_content)
                            
                            # Verify actual file exists in S3
                            actual_key = key[:-5]
                            try:
                                self.s3_client.head_object(Bucket=self.bucket_name, Key=actual_key)
                                files_metadata.append(metadata)
                            except ClientError as e:
                                if e.response["Error"]["Code"] == "404":
                                    # Clean up orphaned metadata file
                                    self.s3_client.delete_object(Bucket=self.bucket_name, Key=key)
                        except Exception:
                            pass
        except Exception as e:
            # Bucket might be empty or invalid
            pass

        return files_metadata
