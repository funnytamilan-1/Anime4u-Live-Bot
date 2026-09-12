# Anime4u Telegram Storage Bot & Admin Dashboard

Production Telegram Admin Uploader Bot and Stream Management Dashboard using a Private Telegram Channel as the storage backend and SQLite / Supabase for metadata indexing. Zero external cloud storage dependencies (Backblaze B2 completely removed).

---

## 🏗️ Core Architecture

```
Telegram Admin 
   ↓
Admin Uploader Bot (python-telegram-bot v21+)
   ↓
Private Storage Channel (Native copy_message / Telegram file storage)
   ↓
Database Index (SQLite / Supabase)
   ↓
React Admin Dashboard & Web Streamer
```

---

## 🚀 Key Features

1. **Zero External Cloud Storage Fees**: Uses Telegram Private Channel as the file storage backend.
2. **Native Telegram-to-Telegram Copying**: Blazing fast `copy_message` without disk I/O bottlenecks.
3. **Database Metadata Indexing**: Instant queries for virtual folders, search, file sizes, and file records.
4. **Duplicate Detection Engine**: Detects existing files by `file_unique_id` before uploading.
5. **Storage Reconciliation**: `/reconcile` tool validates DB records against actual Telegram channel messages.
6. **Strict Security**: Admin authorization checked for every command, callback query, and media handler.
7. **Production DevOps Ready**: Preconfigured for Docker, Docker Compose, VPS, and Render deployment.

---

## 🛠️ Environment Variables (`.env`)

```env
BOT_TOKEN=1234567890:ABCdefGHIjklMNOpqrsTUVwxyz123456
STORAGE_CHANNEL_ID=-1001234567890
ADMIN_IDS=123456789,987654321
DATABASE_PATH=./storage.db
SUPABASE_URL=
SUPABASE_SERVICE_ROLE_KEY=
MAX_FILE_SIZE=2147483648
MAX_CONCURRENT_UPLOADS=2
MAX_CONCURRENT_FFMPEG=1
TEMP_DIR=./downloads
AUTO_HLS=false
```

---

## 🤖 Telegram Bot Commands

- `/start` - Main Storage Control Panel with inline keyboard menu
- `/help` - Command reference guide
- `/folders` - Browse virtual folders and counts
- `/newfolder` - Create and select a new virtual folder path
- `/selected` - View currently active folder path
- `/files` - Browse indexed files
- `/search <query>` - Search files by title, filename, or folder
- `/stats` - Live storage and database metrics
- `/storage` - Infrastructure health check (Bot, Channel, DB, FFmpeg)
- `/recent` - View 10 most recent file uploads
- `/reconcile` - Maintenance tool comparing DB vs Storage Channel
- `/cancel` - Cancel active input state

---

## 🐳 Running with Docker

```bash
# Build and run with Docker Compose
docker-compose up -d --build
```

