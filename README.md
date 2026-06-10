# Mini Transfer.sh Clone

A lightweight, terminal-first, REST-based file sharing web service built with Python and Flask. Designed to be deployed on Render, it allows users to upload, download, and query metadata of files using only `curl` commands directly from their terminal.

## Core Features

- 📤 **Terminal Uploads**: Stream file uploads using simple `PUT` requests (`curl --upload-file`).
- 📥 **Direct Downloads**: Efficient file streaming with automatic media-type guessing and original filename headers.
- ℹ️ **Metadata Endpoint**: Query file information (filename, size, uploaded time) in JSON.
- 🛡️ **Built-in Security**:
  - Path traversal defense.
  - Alphanumeric filename sanitization via Werkzeug.
  - Dynamically enforced file size limits (rejects oversized uploads mid-stream).
  - Process-local sliding window IP rate limiting.
  - Automatic random ID assignment on filename collisions.
- 🧹 **Auto Expiration**: Background scheduler process deletes files older than a configured period (default 24h).
- ☁️ **Render Ready**: Configured via a persistent disk Blueprint (`render.yaml`) and Gunicorn `Procfile`.

---

## Command Line Usage

Assuming your service is running at `https://app-name.onrender.com` (or `http://localhost:5000` locally):

### 1. Upload a File
Upload a file using the `PUT` HTTP method.

```bash
curl --upload-file report.pdf https://app-name.onrender.com/report.pdf
```
**Response (text/plain):**
```text
https://app-name.onrender.com/report.pdf
```
*(If a file with the same name already exists, a random 8-character ID prefix is automatically generated to prevent collision, e.g. `https://app-name.onrender.com/3f8a2c4e/report.pdf`)*

### 2. Download a File
Retrieve a file using simple GET. Use the `-O` or `-o` flags in curl to preserve or rename the file.

```bash
curl -O https://app-name.onrender.com/report.pdf
# Or if it has an ID prefix:
curl -O https://app-name.onrender.com/3f8a2c4e/report.pdf
```

### 3. Retrieve File Metadata
Query detailed metadata about a file in JSON format.

```bash
curl https://app-name.onrender.com/info/report.pdf
# Or if it has an ID prefix:
curl https://app-name.onrender.com/info/3f8a2c4e/report.pdf
```
**Response (application/json):**
```json
{
  "filename": "report.pdf",
  "size": 1048576,
  "uploaded_at": "2026-06-10T12:00:00.000000+00:00",
  "file_id": "3f8a2c4e"
}
```

### 4. Health Check
```bash
curl https://app-name.onrender.com/health
```
**Response (application/json):**
```json
{
  "status": "ok"
}
```

---

## Environment Variables

| Variable | Description | Default |
| :--- | :--- | :--- |
| `UPLOAD_DIR` | Directory on disk where files and sidecar JSON metadata are saved. | `uploads` |
| `MAX_FILE_SIZE` | Maximum file size allowed in bytes. | `536870912` (512 MB) |
| `FILE_EXPIRY_HOURS` | Number of hours before files automatically expire and get deleted. | `24` |
| `RATE_LIMIT_PER_MINUTE` | Number of requests allowed per client IP per minute. | `60` |

---

## Local Development Setup

1. **Clone and Install Dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure Environment Variables**:
   Create a `.env` file in the root directory (optional):
   ```env
   UPLOAD_DIR=uploads
   MAX_FILE_SIZE=10485760  # 10 MB limit
   FILE_EXPIRY_HOURS=1
   RATE_LIMIT_PER_MINUTE=30
   ```

3. **Run the Server**:
   ```bash
   python app.py
   ```
   The application will run on `http://localhost:5000`.

---

## Render Deployment

To deploy this project to Render:

1. Create a new **Blueprint** service on Render.
2. Link your Git repository containing these files.
3. Render will automatically parse the `render.yaml` file, which will:
   - Create a Web Service using python runtime.
   - Run `pip install -r requirements.txt` as build command.
   - Run Gunicorn as startup command.
   - Provision a **Persistent Disk** (default size: 1GB) and mount it to `/var/lib/uploads` so files are not lost when the service restarts.
   - Populate the environment variables.
