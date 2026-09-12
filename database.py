import os
import sqlite3
import logging
from typing import Dict, Any, List, Optional
from datetime import datetime

logger = logging.getLogger("Database")

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

supabase = None
if SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY:
    try:
        from supabase import create_client
        supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
        logger.info("Connected to Supabase database")
    except Exception as e:
        logger.warning(f"Supabase connection failed, falling back to SQLite: {e}")

DB_PATH = os.getenv("DATABASE_PATH", "./storage.db")

def get_sqlite_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Initialize database tables and indexes."""
    if supabase:
        logger.info("Using Supabase database tables")
        return

    conn = get_sqlite_conn()
    cursor = conn.cursor()

    # Files table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS file_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            storage_message_id INTEGER UNIQUE NOT NULL,
            storage_channel_id INTEGER NOT NULL,
            file_id TEXT NOT NULL,
            file_unique_id TEXT NOT NULL,
            file_name TEXT NOT NULL,
            file_size INTEGER NOT NULL,
            mime_type TEXT,
            media_type TEXT,
            folder TEXT NOT NULL,
            title TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    # Indexes for fast querying
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_folder ON file_records(folder)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_msg_id ON file_records(storage_message_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_unique_id ON file_records(file_unique_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_created_at ON file_records(created_at)")

    # HLS Assets table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS hls_assets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            playlist_message_id INTEGER UNIQUE NOT NULL,
            storage_channel_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            folder TEXT NOT NULL,
            segment_message_ids TEXT NOT NULL,
            segment_count INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    # Audit Logs table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            details TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_audit_admin ON audit_logs(admin_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_logs(created_at)")

    conn.commit()
    conn.close()
    logger.info("SQLite database schema initialized successfully")

def log_audit(admin_id: int, action: str, details: str):
    """Log admin actions for security audit trail."""
    try:
        conn = get_sqlite_conn()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO audit_logs (admin_id, action, details, created_at) VALUES (?, ?, ?, ?)",
            (admin_id, action, details, datetime.utcnow().isoformat())
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Failed to log audit event: {e}")

def get_recent_audit_logs(limit: int = 15) -> List[Dict[str, Any]]:
    conn = get_sqlite_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM audit_logs ORDER BY created_at DESC LIMIT ?", (limit,))
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_stats() -> Dict[str, Any]:
    """Calculates real storage metrics directly from database records."""
    conn = get_sqlite_conn()
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*), COALESCE(SUM(file_size), 0) FROM file_records")
    total_files, total_bytes = cursor.fetchone()

    cursor.execute("SELECT COUNT(DISTINCT folder) FROM file_records")
    total_folders = cursor.fetchone()[0]

    cursor.execute("SELECT media_type, COUNT(*) FROM file_records GROUP BY media_type")
    media_counts = dict(cursor.fetchall())

    cursor.execute("SELECT COUNT(*) FROM hls_assets")
    hls_count = cursor.fetchone()[0]

    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    cursor.execute("SELECT COUNT(*) FROM file_records WHERE created_at LIKE ?", (f"{today_str}%",))
    uploads_today = cursor.fetchone()[0]

    conn.close()

    return {
        "total_files": total_files,
        "total_folders": total_folders,
        "total_bytes": total_bytes,
        "readable_bytes": f"{total_bytes / (1024 * 1024):.1f} MB" if total_bytes < 1073741824 else f"{total_bytes / (1024 * 1024 * 1024):.2f} GB",
        "videos": media_counts.get("video", 0),
        "documents": media_counts.get("document", 0),
        "audio": media_counts.get("audio", 0),
        "photos": media_counts.get("photo", 0),
        "hls_assets": hls_count,
        "uploads_today": uploads_today,
    }

def get_recent_uploads(limit: int = 10) -> List[Dict[str, Any]]:
    conn = get_sqlite_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM file_records ORDER BY created_at DESC LIMIT ?", (limit,))
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def list_all_message_ids() -> List[int]:
    conn = get_sqlite_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT storage_message_id FROM file_records")
    rows = cursor.fetchall()
    conn.close()
    return [r[0] for r in rows]

def find_by_unique_id(file_unique_id: str) -> Optional[Dict[str, Any]]:
    if supabase:
        res = supabase.table("streams").select("*").eq("file_unique_id", file_unique_id).execute()
        return res.data[0] if res.data else None

    conn = get_sqlite_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM file_records WHERE file_unique_id = ?", (file_unique_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

def insert_file_record(record: Dict[str, Any]) -> bool:
    if supabase:
        try:
            supabase.table("streams").insert({
                "storage_message_id": record["storage_message_id"],
                "storage_channel_id": record["storage_channel_id"],
                "file_id": record["file_id"],
                "file_unique_id": record["file_unique_id"],
                "file_name": record["file_name"],
                "file_size": record["file_size"],
                "mime_type": record.get("mime_type", ""),
                "media_type": record.get("media_type", "document"),
                "folder": record["folder"],
                "title": record["title"],
                "created_at": record.get("created_at", datetime.utcnow().isoformat())
            }).execute()
            return True
        except Exception as e:
            logger.error(f"Supabase insert failed: {e}")
            return False

    conn = get_sqlite_conn()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO file_records (
                storage_message_id, storage_channel_id, file_id, file_unique_id,
                file_name, file_size, mime_type, media_type, folder, title, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            record["storage_message_id"],
            record["storage_channel_id"],
            record["file_id"],
            record["file_unique_id"],
            record["file_name"],
            record["file_size"],
            record.get("mime_type", ""),
            record.get("media_type", "document"),
            record["folder"],
            record["title"],
            record.get("created_at", datetime.utcnow().isoformat())
        ))
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"SQLite insert failed: {e}")
        return False
    finally:
        conn.close()

def list_folders() -> List[Dict[str, Any]]:
    if supabase:
        res = supabase.table("streams").select("folder").execute()
        folders = set(item["folder"] for item in res.data if item.get("folder"))
        return [{"folder": f, "count": sum(1 for item in res.data if item.get("folder") == f)} for f in sorted(folders)]

    conn = get_sqlite_conn()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT folder, COUNT(*) as count 
        FROM file_records 
        GROUP BY folder 
        ORDER BY folder ASC
    """)
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def search_files(query: str, page: int = 0, page_size: int = 10) -> List[Dict[str, Any]]:
    offset = page * page_size
    conn = get_sqlite_conn()
    cursor = conn.cursor()
    q = f"%{query}%"
    cursor.execute("""
        SELECT * FROM file_records 
        WHERE file_name LIKE ? OR title LIKE ? OR folder LIKE ?
        ORDER BY created_at DESC
        LIMIT ? OFFSET ?
    """, (q, q, q, page_size, offset))
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_file_by_message_id(storage_message_id: int) -> Optional[Dict[str, Any]]:
    conn = get_sqlite_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM file_records WHERE storage_message_id = ?", (storage_message_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

def update_file_folder(storage_message_id: int, new_folder: str) -> bool:
    conn = get_sqlite_conn()
    cursor = conn.cursor()
    cursor.execute("UPDATE file_records SET folder = ? WHERE storage_message_id = ?", (new_folder, storage_message_id))
    conn.commit()
    changed = cursor.rowcount > 0
    conn.close()
    return changed

def delete_file_record(storage_message_id: int) -> bool:
    conn = get_sqlite_conn()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM file_records WHERE storage_message_id = ?", (storage_message_id,))
    conn.commit()
    changed = cursor.rowcount > 0
    conn.close()
    return changed
