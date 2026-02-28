"""
SQLite-backed commitment storage for Sonorus NPC Commitment System.
Follows the same patterns as dialogue_db.py: thread-local connections, WAL mode, schema versioning.
"""

import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime

from .settings import DATA_DIR


DB_PATH = os.path.join(DATA_DIR, "commitments.db")

# Thread-local storage for connections
_local = threading.local()

# Lock for initialization
_init_lock = threading.Lock()
_initialized = False

SCHEMA_VERSION = 1


def _ensure_data_dir():
    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR, exist_ok=True)


def _create_connection():
    _ensure_data_dir()
    conn = sqlite3.connect(DB_PATH, timeout=30.0, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def get_connection():
    """Get thread-local SQLite connection with WAL mode."""
    try:
        if hasattr(_local, 'conn') and _local.conn is not None:
            try:
                _local.conn.execute("SELECT 1")
            except (sqlite3.ProgrammingError, sqlite3.OperationalError):
                try:
                    _local.conn.close()
                except:
                    pass
                _local.conn = None

        if not hasattr(_local, 'conn') or _local.conn is None:
            _local.conn = _create_connection()

        yield _local.conn
    except sqlite3.Error:
        if hasattr(_local, 'conn') and _local.conn:
            try:
                _local.conn.close()
            except:
                pass
            _local.conn = None
        raise
    finally:
        if hasattr(_local, 'conn') and _local.conn is not None:
            try:
                _local.conn.rollback()
            except:
                pass


def _get_schema_version(conn):
    try:
        result = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
        ).fetchone()
        if not result:
            return 0
        row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
        return row[0] if row else 0
    except:
        return 0


def _set_schema_version(conn, version):
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
    conn.execute("DELETE FROM schema_version")
    conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))


def _run_migrations(conn, from_version):
    current = from_version

    while current < SCHEMA_VERSION:
        if current == 0:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS commitments (
                    id TEXT PRIMARY KEY,
                    npc_id TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    location_id TEXT NOT NULL,
                    location_display TEXT NOT NULL,
                    activity_id TEXT NOT NULL,
                    game_time_start TEXT NOT NULL,
                    game_time_end TEXT NOT NULL,
                    override_apply_time TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    is_companion INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT,
                    notes TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_commitments_npc ON commitments(npc_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_commitments_status ON commitments(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_commitments_start ON commitments(game_time_start)")
            current = 1
        else:
            raise ValueError(f"Unknown schema version {current}")

    _set_schema_version(conn, SCHEMA_VERSION)


def init_db():
    """Initialize database schema if needed."""
    global _initialized

    if _initialized:
        return

    with _init_lock:
        if _initialized:
            return

        try:
            with get_connection() as conn:
                current_version = _get_schema_version(conn)
                if current_version < SCHEMA_VERSION:
                    print(f"[CommitmentsDB] Migrating schema from v{current_version} to v{SCHEMA_VERSION}")
                    _run_migrations(conn, current_version)
                    conn.commit()

            _initialized = True
            print(f"[CommitmentsDB] Database initialized (schema v{SCHEMA_VERSION})")
        except Exception as e:
            print(f"[CommitmentsDB] Error initializing database: {e}")
            raise


def _ensure_initialized():
    if not _initialized:
        init_db()


def _row_to_dict(row):
    """Convert a sqlite3.Row to a dict."""
    return {
        "id": row["id"],
        "npc_id": row["npc_id"],
        "target_id": row["target_id"],
        "location_id": row["location_id"],
        "location_display": row["location_display"],
        "activity_id": row["activity_id"],
        "game_time_start": row["game_time_start"],
        "game_time_end": row["game_time_end"],
        "override_apply_time": row["override_apply_time"],
        "status": row["status"],
        "is_companion": bool(row["is_companion"]),
        "created_at": row["created_at"],
        "resolved_at": row["resolved_at"],
        "notes": row["notes"],
    }


def create_commitment(npc_id, target_id, location_id, location_display, activity_id,
                      game_time_start, game_time_end, override_apply_time,
                      is_companion=False, notes=None):
    """Create a new commitment. Returns generated ID."""
    _ensure_initialized()
    commitment_id = str(uuid.uuid4())[:8]
    created_at = datetime.now().isoformat()

    try:
        with get_connection() as conn:
            conn.execute("""
                INSERT INTO commitments (
                    id, npc_id, target_id, location_id, location_display, activity_id,
                    game_time_start, game_time_end, override_apply_time,
                    status, is_companion, created_at, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
            """, (
                commitment_id, npc_id, target_id, location_id, location_display,
                activity_id, game_time_start, game_time_end, override_apply_time,
                1 if is_companion else 0, created_at, notes,
            ))
            conn.commit()
        print(f"[CommitmentsDB] Created commitment {commitment_id}: {npc_id} -> {location_display} at {game_time_start}")
        return commitment_id
    except Exception as e:
        print(f"[CommitmentsDB] Error creating commitment: {e}")
        return None


def get_commitment(commitment_id):
    """Get a single commitment by ID."""
    _ensure_initialized()
    try:
        with get_connection() as conn:
            row = conn.execute("SELECT * FROM commitments WHERE id = ?", (commitment_id,)).fetchone()
        return _row_to_dict(row) if row else None
    except Exception as e:
        print(f"[CommitmentsDB] Error getting commitment: {e}")
        return None


def get_commitments_for_npc(npc_id, include_resolved=False):
    """Get all commitments for a specific NPC."""
    _ensure_initialized()
    try:
        with get_connection() as conn:
            if include_resolved:
                rows = conn.execute(
                    "SELECT * FROM commitments WHERE npc_id = ? ORDER BY game_time_start ASC",
                    (npc_id,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM commitments WHERE npc_id = ? AND status NOT IN ('completed', 'no_show', 'cancelled') ORDER BY game_time_start ASC",
                    (npc_id,)
                ).fetchall()
        return [_row_to_dict(row) for row in rows]
    except Exception as e:
        print(f"[CommitmentsDB] Error getting commitments for NPC: {e}")
        return []


def get_pending_commitments():
    """Get all pending commitments (not yet activated)."""
    _ensure_initialized()
    try:
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM commitments WHERE status = 'pending' ORDER BY override_apply_time ASC"
            ).fetchall()
        return [_row_to_dict(row) for row in rows]
    except Exception as e:
        print(f"[CommitmentsDB] Error getting pending commitments: {e}")
        return []


def get_active_commitments():
    """Get all active commitments (override applied, NPC at location)."""
    _ensure_initialized()
    try:
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM commitments WHERE status IN ('active', 'arrived') ORDER BY game_time_start ASC"
            ).fetchall()
        return [_row_to_dict(row) for row in rows]
    except Exception as e:
        print(f"[CommitmentsDB] Error getting active commitments: {e}")
        return []


def update_status(commitment_id, new_status, resolved_at=None):
    """Update a commitment's status."""
    _ensure_initialized()
    terminal_statuses = ('completed', 'no_show', 'cancelled')
    try:
        with get_connection() as conn:
            if new_status in terminal_statuses and not resolved_at:
                resolved_at = datetime.now().isoformat()
            conn.execute(
                "UPDATE commitments SET status = ?, resolved_at = ? WHERE id = ?",
                (new_status, resolved_at, commitment_id)
            )
            conn.commit()
        print(f"[CommitmentsDB] Updated {commitment_id} -> {new_status}")
    except Exception as e:
        print(f"[CommitmentsDB] Error updating status: {e}")


def cancel_commitment(commitment_id):
    """Cancel a commitment."""
    update_status(commitment_id, 'cancelled')


def get_all_commitments(npc_id=None):
    """Get all commitments (including resolved), ordered by game_time_start DESC.
    If npc_id provided, filters to that NPC.
    """
    _ensure_initialized()
    try:
        with get_connection() as conn:
            if npc_id:
                rows = conn.execute(
                    "SELECT * FROM commitments WHERE npc_id = ? ORDER BY game_time_start DESC",
                    (npc_id,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM commitments ORDER BY game_time_start DESC"
                ).fetchall()
        return [_row_to_dict(row) for row in rows]
    except Exception as e:
        print(f"[CommitmentsDB] Error getting all commitments: {e}")
        return []


def update_commitment(commitment_id, **kwargs):
    """Update allowed fields on a non-terminal commitment.
    Allowed fields: location_id, location_display, activity_id,
    game_time_start, game_time_end, override_apply_time.
    Returns True on success, False on failure.
    """
    _ensure_initialized()
    allowed = {'location_id', 'location_display', 'activity_id',
               'game_time_start', 'game_time_end', 'override_apply_time'}
    updates = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
    if not updates:
        return False

    try:
        with get_connection() as conn:
            # Verify commitment exists and is non-terminal
            row = conn.execute("SELECT status FROM commitments WHERE id = ?", (commitment_id,)).fetchone()
            if not row:
                print(f"[CommitmentsDB] Update: commitment {commitment_id} not found")
                return False
            if row["status"] in ('completed', 'no_show', 'cancelled'):
                print(f"[CommitmentsDB] Update: commitment {commitment_id} is terminal ({row['status']})")
                return False

            set_clause = ", ".join(f"{k} = ?" for k in updates)
            values = list(updates.values()) + [commitment_id]
            conn.execute(f"UPDATE commitments SET {set_clause} WHERE id = ?", values)
            conn.commit()
        print(f"[CommitmentsDB] Updated {commitment_id}: {updates}")
        return True
    except Exception as e:
        print(f"[CommitmentsDB] Error updating commitment: {e}")
        return False


def get_commitments_in_window(npc_id, window_start, window_end):
    """Get non-terminal commitments for an NPC that overlap a time window.
    Used for conflict detection. Times are 'YYYY/MM/DD HH:MM' strings.
    """
    _ensure_initialized()
    try:
        with get_connection() as conn:
            rows = conn.execute("""
                SELECT * FROM commitments
                WHERE npc_id = ?
                  AND status NOT IN ('completed', 'no_show', 'cancelled')
                  AND override_apply_time < ?
                  AND game_time_end > ?
                ORDER BY game_time_start ASC
            """, (npc_id, window_end, window_start)).fetchall()
        return [_row_to_dict(row) for row in rows]
    except Exception as e:
        print(f"[CommitmentsDB] Error checking window conflicts: {e}")
        return []
