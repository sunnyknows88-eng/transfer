import os
import json
import time
import datetime
import secrets
import mimetypes
import logging
import threading
from collections import defaultdict
from typing import BinaryIO, Optional

from flask import Flask, request, send_file, jsonify, abort
from werkzeug.utils import secure_filename
from dotenv import load_dotenv

from storage import LocalStorageProvider

# Load environment variables from .env
load_dotenv()

# Configure Logging
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s in %(module)s: %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Config Options
UPLOAD_DIR = os.getenv("UPLOAD_DIR", "uploads")
MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE", str(512 * 1024 * 1024)))  # Default: 512MB
FILE_EXPIRY_HOURS = int(os.getenv("FILE_EXPIRY_HOURS", "24"))          # Default: 24 hours
RATE_LIMIT_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_MINUTE", "60"))  # Default: 60 requests/min

# Initialize Flask App
app = Flask(__name__)
# Keep config in app.config as well
app.config["UPLOAD_DIR"] = UPLOAD_DIR
app.config["MAX_CONTENT_LENGTH"] = MAX_FILE_SIZE  # Also sets Flask's built-in body size check

# Initialize Storage Provider (Local Filesystem or S3-Compatible Storage)
S3_BUCKET_NAME = os.getenv("S3_BUCKET_NAME")
S3_ACCESS_KEY_ID = os.getenv("S3_ACCESS_KEY_ID")
S3_SECRET_ACCESS_KEY = os.getenv("S3_SECRET_ACCESS_KEY")
S3_ENDPOINT_URL = os.getenv("S3_ENDPOINT_URL")
S3_REGION_NAME = os.getenv("S3_REGION_NAME")

if S3_BUCKET_NAME and S3_ACCESS_KEY_ID and S3_SECRET_ACCESS_KEY:
    logger.info("Initializing S3StorageProvider as storage backend...")
    from storage import S3StorageProvider
    storage = S3StorageProvider(
        bucket_name=S3_BUCKET_NAME,
        access_key_id=S3_ACCESS_KEY_ID,
        secret_access_key=S3_SECRET_ACCESS_KEY,
        endpoint_url=S3_ENDPOINT_URL,
        region_name=S3_REGION_NAME
    )
else:
    logger.info("Initializing LocalStorageProvider as storage backend...")
    storage = LocalStorageProvider(UPLOAD_DIR)


# Rate Limiter implementation
class RateLimiter:
    """In-memory rate limiter using a sliding window per IP."""
    def __init__(self, limit: int, window_seconds: int = 60):
        self.limit = limit
        self.window_seconds = window_seconds
        self.requests = defaultdict(list)
        self.lock = threading.Lock()

    def check_rate_limit(self) -> bool:
        # Resolve real client IP, considering Render's reverse proxy headers
        if request.headers.get("X-Forwarded-For"):
            ip = request.headers.get("X-Forwarded-For").split(",")[0].strip()
        else:
            ip = request.remote_addr or "127.0.0.1"

        now = time.time()
        with self.lock:
            # Keep only requests within the active window
            self.requests[ip] = [t for t in self.requests[ip] if now - t < self.window_seconds]
            if len(self.requests[ip]) >= self.limit:
                logger.warning(f"Rate limit exceeded for IP: {ip} ({len(self.requests[ip])}/{self.limit} requests)")
                return False
            self.requests[ip].append(now)
            return True


# Instantiate the global rate limiter
rate_limiter = RateLimiter(limit=RATE_LIMIT_PER_MINUTE)


@app.before_request
def enforce_rate_limiting():
    """Applies IP rate limiting before requests are dispatched."""
    # Exclude health check from rate limiting
    if request.path == "/health":
        return None

    if not rate_limiter.check_rate_limit():
        return jsonify({
            "error": "Too Many Requests",
            "message": f"Rate limit exceeded. Maximum of {RATE_LIMIT_PER_MINUTE} requests per minute is allowed."
        }), 429


# Size limiting wrapper for stream uploads
class SizeLimitingStream:
    """Wraps an incoming request stream to enforce size constraints dynamically."""
    def __init__(self, stream: BinaryIO, limit: int):
        self.stream = stream
        self.limit = limit
        self.bytes_read = 0

    def read(self, limit: Optional[int] = None) -> bytes:
        read_chunk_size = limit if limit is not None else 8192
        chunk = self.stream.read(read_chunk_size)
        if chunk:
            self.bytes_read += len(chunk)
            if self.bytes_read > self.limit:
                raise ValueError("File size limit exceeded")
        return chunk


# Background Cleanup Task for Expired Files
def cleanup_expired_files(storage_provider, expiry_hours: int, upload_dir: str):
    """Identifies and deletes files older than the expiry period."""
    cleanup_lock_file = os.path.join(upload_dir, ".last_cleanup")
    now = time.time()
    
    # Try to coordinate among multiple Gunicorn workers
    try:
        if os.path.exists(cleanup_lock_file):
            mtime = os.path.getmtime(cleanup_lock_file)
            # Avoid running the cleanup more often than once every 5 minutes
            if now - mtime < 300:
                return
    except Exception as e:
        logger.debug(f"Failed checking cleanup lock file: {e}")

    # Acquire the lock by touching/writing to the marker file
    try:
        os.makedirs(upload_dir, exist_ok=True)
        with open(cleanup_lock_file, "w") as f:
            f.write(str(now))
    except Exception as e:
        logger.error(f"Could not write to cleanup lock file: {e}")
        return

    logger.info("Executing periodic cleanup task for expired files...")
    try:
        files = storage_provider.list_files()
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=expiry_hours)
        deleted_count = 0

        for file_info in files:
            uploaded_at_str = file_info.get("uploaded_at")
            if not uploaded_at_str:
                continue
            
            try:
                uploaded_at = datetime.datetime.fromisoformat(uploaded_at_str)
            except Exception:
                continue

            if uploaded_at < cutoff:
                filename = file_info["filename"]
                file_id = file_info.get("file_id")
                
                logger.info(f"Deleting expired file: {f'{file_id}/' if file_id else ''}{filename} (Uploaded: {uploaded_at_str})")
                if storage_provider.delete_file(filename, file_id):
                    deleted_count += 1

        if deleted_count > 0:
            logger.info(f"Successfully cleaned up {deleted_count} expired files.")
        else:
            logger.info("No expired files to clean up.")
            
    except Exception as e:
        logger.error(f"Error occurred during expired files cleanup: {e}", exc_info=True)


def start_cleanup_scheduler(app_instance, storage_provider, expiry_hours: int, upload_dir: str):
    """Launches the background daemon thread for cleanup scheduler."""
    def scheduler_loop():
        # Short startup delay to let the server initialize first
        time.sleep(15)
        while True:
            with app_instance.app_context():
                cleanup_expired_files(storage_provider, expiry_hours, upload_dir)
            time.sleep(300)  # Check every 5 minutes

    thread = threading.Thread(target=scheduler_loop, daemon=True)
    thread.start()


# Start background thread scheduler
start_cleanup_scheduler(app, storage, FILE_EXPIRY_HOURS, UPLOAD_DIR)


# --- API Routes ---

@app.route("/health", methods=["GET"])
def health_check():
    """Health status endpoint."""
    return jsonify({"status": "ok"}), 200


@app.route("/info/<filename>", methods=["GET"])
def info_direct(filename: str):
    """Retrieves metadata for a file in the root upload directory."""
    return retrieve_info(filename, None)


@app.route("/info/<file_id>/<filename>", methods=["GET"])
def info_with_id(file_id: str, filename: str):
    """Retrieves metadata for a file nested under a file ID."""
    return retrieve_info(filename, file_id)


def retrieve_info(filename: str, file_id: Optional[str]):
    """Common helper to resolve metadata for info endpoints."""
    try:
        metadata = storage.get_metadata(filename, file_id)
        response_payload = {
            "filename": metadata["filename"],
            "size": metadata["size"],
            "uploaded_at": metadata["uploaded_at"]
        }
        if metadata.get("file_id"):
            response_payload["file_id"] = metadata["file_id"]
        return jsonify(response_payload), 200
    except FileNotFoundError:
        return jsonify({"error": "Not Found", "message": "File metadata not found"}), 404
    except ValueError as e:
        return jsonify({"error": "Bad Request", "message": str(e)}), 400
    except Exception as e:
        logger.error(f"Error fetching metadata: {e}", exc_info=True)
        return jsonify({"error": "Internal Server Error", "message": "Could not fetch metadata"}), 500


@app.route("/<filename>", methods=["GET"])
def download_direct(filename: str):
    """Downloads a file directly from the root upload directory."""
    return perform_download(filename, None)


@app.route("/<file_id>/<filename>", methods=["GET"])
def download_with_id(file_id: str, filename: str):
    """Downloads a file stored under a file ID."""
    return perform_download(filename, file_id)


def perform_download(filename: str, file_id: Optional[str]):
    """Common helper to retrieve and stream files."""
    try:
        stream, size, metadata = storage.get_file_stream(filename, file_id)
    except FileNotFoundError:
        return jsonify({"error": "Not Found", "message": "File not found"}), 404
    except ValueError as e:
        return jsonify({"error": "Bad Request", "message": str(e)}), 400
    except Exception as e:
        logger.error(f"Error reading file stream: {e}", exc_info=True)
        return jsonify({"error": "Internal Server Error", "message": "Could not read file"}), 500

    # Clean display name from metadata or path
    display_name = metadata.get("filename", filename)

    # Determine media mime type
    mimetype = metadata.get("content_type")
    if not mimetype:
        mimetype, _ = mimetypes.guess_type(display_name)
        mimetype = mimetype or "application/octet-stream"

    # Stream the file content back to the client using send_file.
    # We specify as_attachment=True to trigger download headers.
    return send_file(
        stream,
        mimetype=mimetype,
        as_attachment=True,
        download_name=display_name
    )


@app.route("/<filename>", methods=["PUT"])
def upload_file(filename: str):
    """Handles PUT file uploads from the terminal."""
    # Ensure safe filenames to avoid path traversal in parameter itself
    safe_filename = secure_filename(filename)
    if not safe_filename:
        # Fallback to avoid empty names
        safe_filename = f"upload_{secrets.token_hex(4)}"

    # Static check using Content-Length header
    content_length = request.content_length
    if content_length is not None and content_length > MAX_FILE_SIZE:
        return jsonify({
            "error": "Payload Too Large",
            "message": f"File size exceeds maximum allowed size of {MAX_FILE_SIZE} bytes"
        }), 413

    # Dynamic check via custom SizeLimitingStream wrapper (handles chunked uploads correctly)
    limiting_stream = SizeLimitingStream(request.stream, MAX_FILE_SIZE)

    # Calculate MIME type
    mimetype, _ = mimetypes.guess_type(safe_filename)
    if not mimetype:
        mimetype = request.content_type or "application/octet-stream"

    try:
        file_id, saved_filename, metadata = storage.save_file(
            safe_filename, limiting_stream, content_type=mimetype
        )

        # Generate download URL mapping
        url_subpath = f"{file_id}/{saved_filename}" if file_id else saved_filename
        download_url = f"{request.host_url}{url_subpath}\n"
        
        logger.info(f"File uploaded successfully: {saved_filename} (Size: {metadata['size']} bytes, ID: {file_id})")
        return download_url, 201, {"Content-Type": "text/plain"}

    except ValueError as e:
        if str(e) == "File size limit exceeded":
            logger.warning("Upload aborted: File size limit exceeded.")
            return jsonify({
                "error": "Payload Too Large",
                "message": f"File size exceeds maximum allowed size of {MAX_FILE_SIZE} bytes"
            }), 413
        return jsonify({"error": "Bad Request", "message": str(e)}), 400
    except Exception as e:
        logger.error(f"Error occurred during file upload: {e}", exc_info=True)
        return jsonify({"error": "Internal Server Error", "message": "Failed to upload file"}), 500


# Error Handlers for generic server exceptions
@app.errorhandler(404)
def handle_not_found(e):
    return jsonify({"error": "Not Found", "message": "The requested resource could not be found."}), 404

@app.errorhandler(405)
def handle_method_not_allowed(e):
    return jsonify({"error": "Method Not Allowed", "message": "The method is not allowed for the requested URL."}), 405

@app.errorhandler(500)
def handle_internal_error(e):
    return jsonify({"error": "Internal Server Error", "message": "An unexpected error occurred."}), 500


if __name__ == "__main__":
    # Ensure upload directory exists before starting
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    # Run the development server
    app.run(host="0.0.0.0", port=5000, debug=False)
