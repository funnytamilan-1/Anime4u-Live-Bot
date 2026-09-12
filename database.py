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
    """Initialize database tables, indexes, and migrations."""
    if supabase:
        logger.info("Using Supabase database backend")
        return

    conn = get_sqlite_conn()
    cursor = conn.cursor()

    # File Records table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS file_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_name TEXT NOT NULL,
            file_size INTEGER NOT NULL,
            mime_type TEXT,
            media_type TEXT,
            folder TEXT NOT NULL,
            storage_mode TEXT NOT NULL DEFAULT 'telegram',
            b2_file_id TEXT,
            b2_path TEXT,
            b2_url TEXT,
            b2_status TEXT DEFAULT 'none',
            telegram_channel_id INTEGER,
            telegram_message_id INTEGER,
            telegram_file_id TEXT,
            telegram_file_unique_id TEXT,
            telegram_status TEXT DEFAULT 'none',
            status TEXT NOT NULL DEFAULT 'complete',
            title TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)

    # Check and add any missing columns for backwards compatibility
    cursor.execute("PRAGMA table_info(file_records)")
    existing_cols = {row["name"] for row in cursor.fetchall()}
    
    col_definitions = [
        ("storage_mode", "TEXT NOT NULL DEFAULT 'telegram'"),
        ("b2_file_id", "TEXT"),
        ("b2_path", "TEXT"),
        ("b2_url", "TEXT"),
        ("b2_status", "TEXT DEFAULT 'none'"),
        ("telegram_channel_id", "INTEGER"),
        ("telegram_message_id", "INTEGER"),
        ("telegram_file_id", "TEXT"),
        ("telegram_file_unique_id", "TEXT"),
        ("telegram_status", "TEXT DEFAULT 'none'"),
        ("status", "TEXT NOT NULL DEFAULT 'complete'"),
        ("updated_at", "TEXT")
    ]

    for col_name, col_type in col_definitions:
        if col_name not in existing_cols:
            try:
                cursor.execute(f"ALTER TABLE file_records ADD COLUMN {col_name} {col_type}")
                logger.info(f"Added missing column '{col_name}' to file_records table")
            except Exception as e:
                logger.warning(f"Failed to add column {col_name}: {e}")

    # Indexes
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_folder ON file_records(folder)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_msg_id ON file_records(telegram_message_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_unique_id ON file_records(telegram_file_unique_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_b2_path ON file_records(b2_path)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_created_at ON file_records(created_at)")

    # Virtual Folders table with per-folder storage mode
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS folders (
            folder_name TEXT PRIMARY KEY NOT NULL,
            storage_mode TEXT,
            created_at TEXT NOT NULL
        )
    """)

    # HLS Assets table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS hls_assets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            parent_file_id INTEGER NOT NULL,
            asset_type TEXT NOT NULL,
            storage_mode TEXT NOT NULL,
            b2_path TEXT,
            telegram_message_id INTEGER,
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

    cursor.execute("SELECT storage_mode, COUNT(*) FROM file_records GROUP BY storage_mode")
    mode_counts = dict(cursor.fetchall())

    cursor.execute("SELECT media_type, COUNT(*) FROM file_records GROUP BY media_type")
    media_counts = dict(cursor.fetchall())

    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    cursor.execute("SELECT COUNT(*) FROM file_records WHERE created_at LIKE ?", (f"{today_str}%",))
    uploads_today = cursor.fetchone()[0]

    conn.close()

    return {
        "total_files": total_files,
        "total_folders": total_folders,
        "total_bytes": total_bytes,
        "readable_bytes": f"{total_bytes / (1024 * 1024):.1f} MB" if total_bytes < 1073741824 else f"{total_bytes / (1024 * 1024 * 1024):.2f} GB",
        "b2_files": mode_counts.get("b2", 0),
        "telegram_files": mode_counts.get("telegram", 0),
        "both_files": mode_counts.get("both", 0),
        "videos": media_counts.get("video", 0),
        "documents": media_counts.get("document", 0),
        "audio": media_counts.get("audio", 0),
        "photos": media_counts.get("photo", 0),
        "uploads_today": uploads_today,
    }

def get_recent_uploads(limit: int = 10) -> List[Dict[str, Any]]:
    conn = get_sqlite_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM file_records ORDER BY created_at DESC LIMIT ?", (limit,))
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def list_all_records() -> List[Dict[str, Any]]:
    conn = get_sqlite_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM file_records")
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def find_duplicate(telegram_file_unique_id: Optional[str] = None, b2_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Checks for existing duplicates by Telegram file_unique_id or B2 path."""
    conn = get_sqlite_conn()
    cursor = conn.cursor()
    row = None
    if telegram_file_unique_id:
        cursor.execute("SELECT * FROM file_records WHERE telegram_file_unique_id = ?", (telegram_file_unique_id,))
        row = cursor.fetchone()
    if not row and b2_path:
        cursor.execute("SELECT * FROM file_records WHERE b2_path = ?", (b2_path,))
        row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

def insert_file_record(record: Dict[str, Any]) -> Optional[int]:
    """Inserts a real metadata record into file_records table."""
    now_str = datetime.utcnow().isoformat()
    if supabase:
        try:
            res = supabase.table("streams").insert({
                "file_name": record["file_name"],
                "file_size": record["file_size"],
                "mime_type": record.get("mime_type", ""),
                "media_type": record.get("media_type", "document"),
                "folder": record["folder"],
                "storage_mode": record.get("storage_mode", "telegram"),
                "b2_file_id": record.get("b2_file_id"),
                "b2_path": record.get("b2_path"),
                "b2_url": record.get("b2_url"),
                "b2_status": record.get("b2_status", "none"),
                "telegram_channel_id": record.get("telegram_channel_id"),
                "telegram_message_id": record.get("telegram_message_id"),
                "telegram_file_id": record.get("telegram_file_id"),
                "telegram_file_unique_id": record.get("telegram_file_unique_id"),
                "telegram_status": record.get("telegram_status", "none"),
                "status": record.get("status", "complete"),
                "title": record.get("title", record["file_name"]),
                "created_at": record.get("created_at", now_str),
                "updated_at": now_str
            }).execute()
            return res.data[0]["id"] if res.data else None
        except Exception as e:
            logger.error(f"Supabase insert failed: {e}")
            return None

    conn = get_sqlite_conn()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO file_records (
                file_name, file_size, mime_type, media_type, folder, storage_mode,
                b2_file_id, b2_path, b2_url, b2_status,
                telegram_channel_id, telegram_message_id, telegram_file_id, telegram_file_unique_id, telegram_status,
                status, title, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            record["file_name"],
            record["file_size"],
            record.get("mime_type", ""),
            record.get("media_type", "document"),
            record["folder"],
            record.get("storage_mode", "telegram"),
            record.get("b2_file_id"),
            record.get("b2_path"),
            record.get("b2_url"),
            record.get("b2_status", "none"),
            record.get("telegram_channel_id"),
            record.get("telegram_message_id"),
            record.get("telegram_file_id"),
            record.get("telegram_file_unique_id"),
            record.get("telegram_status", "none"),
            record.get("status", "complete"),
            record.get("title", record["file_name"]),
            record.get("created_at", now_str),
            now_str
        ))
        conn.commit()
        last_id = cursor.lastrowid
        # Also ensure virtual folder exists
        ensure_folder_exists(record["folder"])
        return last_id
    except Exception as e:
        logger.error(f"SQLite insert failed: {e}")
        return None
    finally:
        conn.close()

def ensure_folder_exists(folder_name: str, storage_mode: Optional[str] = None):
    conn = get_sqlite_conn()
    cursor = conn.cursor()
    now_str = datetime.utcnow().isoformat()
    cursor.execute("""
        INSERT INTO folders (folder_name, storage_mode, created_at)
        VALUES (?, ?, ?)
        ON CONFLICT(folder_name) DO UPDATE SET
        storage_mode = COALESCE(excluded.storage_mode, folders.storage_mode)
    """, (folder_name, storage_mode, now_str))
    conn.commit()
    conn.close()

def set_folder_storage_mode(folder_name: str, mode: Optional[str]) -> bool:
    conn = get_sqlite_conn()
    cursor = conn.cursor()
    now_str = datetime.utcnow().isoformat()
    cursor.execute("""
        INSERT INTO folders (folder_name, storage_mode, created_at)
        VALUES (?, ?, ?)
        ON CONFLICT(folder_name) DO UPDATE SET storage_mode = excluded.storage_mode
    """, (folder_name, mode, now_str))
    conn.commit()
    conn.close()
    return True

def get_folder_storage_mode(folder_name: str) -> Optional[str]:
    conn = get_sqlite_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT storage_mode FROM folders WHERE folder_name = ?", (folder_name,))
    row = cursor.fetchone()
    conn.close()
    return row["storage_mode"] if row else None

def list_folders() -> List[Dict[str, Any]]:
    conn = get_sqlite_conn()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT f.folder_name as folder, f.storage_mode, COUNT(r.id) as count 
        FROM folders f
        LEFT JOIN file_records r ON f.folder_name = r.folder
        GROUP BY f.folder_name
        ORDER BY f.folder_name ASC
    """)
    rows = cursor.fetchall()
    conn.close()
    
    # Also grab distinct folders from file_records that might not be in folders table yet
    result = [dict(r) for r in rows]
    existing = {r["folder"] for r in result}
    
    conn = get_sqlite_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT folder FROM file_records")
    for row in cursor.fetchall():
        fn = row["folder"]
        if fn not in existing:
            result.append({"folder": fn, "storage_mode": None, "count": 0})
            existing.add(fn)
    conn.close()
    
    return sorted(result, key=lambda x: x["folder"])

def search_files(query: str, page: int = 0, page_size: int = 10) -> List[Dict[str, Any]]:
    offset = page * page_size
    conn = get_sqlite_conn()
    cursor = conn.cursor()
    q = f"%{query}%"
    cursor.execute("""
        SELECT * FROM file_records 
        WHERE file_name LIKE ? OR title LIKE ? OR folder LIKE ? OR CAST(id AS TEXT) = ? OR CAST(telegram_message_id AS TEXT) = ?
        ORDER BY created_at DESC
        LIMIT ? OFFSET ?
    """, (q, q, q, query, query, page_size, offset))
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_file_by_id(record_id: int) -> Optional[Dict[str, Any]]:
    conn = get_sqlite_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM file_records WHERE id = ?", (record_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

def get_file_by_message_id(telegram_message_id: int) -> Optional[Dict[str, Any]]:
    conn = get_sqlite_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM file_records WHERE telegram_message_id = ?", (telegram_message_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

def update_file_folder(record_id: int, new_folder: str) -> bool:
    conn = get_sqlite_conn()
    cursor = conn.cursor()
    cursor.execute("UPDATE file_records SET folder = ?, updated_at = ? WHERE id = ?", (new_folder, datetime.utcnow().isoformat(), record_id))
    conn.commit()
    changed = cursor.rowcount > 0
    conn.close()
    if changed:
        ensure_folder_exists(new_folder)
    return changed

def delete_file_record(record_id: int) -> bool:
    conn = get_sqlite_conn()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM file_records WHERE id = ?", (record_id,))
    conn.commit()
    changed = cursor.rowcount > 0
    conn.close()
    return changed
