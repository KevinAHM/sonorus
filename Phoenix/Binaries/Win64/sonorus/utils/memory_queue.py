"""
Memory Indexing Queue System for Sonorus.

Provides robust, failure-tolerant processing of dialogue entries into
NPC long-term memory. Features:
- SQLite-backed queue with per-entry checkpointing
- Single worker thread prevents race conditions
- Idempotent chapter sync prevents duplicate memory entries
- Graceful failure recovery from any point
"""

import os
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Optional, List, Dict, Any, Callable
from datetime import datetime

from .settings import DATA_DIR, load_settings

# Database path
QUEUE_DB_PATH = os.path.join(DATA_DIR, "memory_queue.db")

# Thread-local storage for connections
_local = threading.local()

# Track all open connections across threads so we can close them all on restore.
_all_connections: list = []
_all_connections_lock = threading.Lock()

# Lock for initialization
_init_lock = threading.Lock()
_initialized = False

# Current schema version
SCHEMA_VERSION = 1

# Processing locks can be left behind if the server exits mid-job.
# Treat old locks as abandoned so queued NPCs can be picked up again.
PROCESSING_STALE_TIMEOUT_SECONDS = 15 * 60

# Global worker instance
_worker: Optional['MemoryIndexWorker'] = None

# Manual chapter recheck requests are routed through the normal worker, but we
# keep a small in-memory request/result registry so the API can wait for the
# specific NPC to finish.
_manual_recheck_lock = threading.Lock()
_manual_recheck_requests: Dict[str, Dict[str, Any]] = {}


# =============================================================================
# Database Connection Management
# =============================================================================

def _ensure_data_dir():
    """Ensure the data directory exists."""
    os.makedirs(os.path.dirname(QUEUE_DB_PATH), exist_ok=True)


def _create_connection():
    """Create a new database connection with proper settings."""
    _ensure_data_dir()
    conn = sqlite3.connect(QUEUE_DB_PATH, timeout=30.0, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row
    with _all_connections_lock:
        _all_connections.append(conn)
    return conn


def close_thread_local_connection():
    """Close the current thread's SQLite connection, if one is open."""
    if hasattr(_local, 'conn') and _local.conn is not None:
        try:
            _local.conn.close()
        except Exception:
            pass
        _local.conn = None


def close_all_connections():
    """Close every tracked SQLite connection across all threads.

    Must be called before moving/deleting the queue DB file on Windows,
    where open file handles prevent rename/delete.
    """
    with _all_connections_lock:
        for conn in _all_connections:
            try:
                conn.close()
            except Exception:
                pass
        _all_connections.clear()
    # Also clear the current thread's reference
    if hasattr(_local, 'conn'):
        _local.conn = None


def reset_connection_state():
    """Drop cached SQLite connection/init state so the next use reinitializes cleanly."""
    global _initialized
    close_all_connections()
    _initialized = False


def close_all():
    """Close all connections across all threads, checkpoint WAL, reset state."""
    global _initialized
    with _all_connections_lock:
        for conn in _all_connections:
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass
        _all_connections.clear()
    if hasattr(_local, 'conn'):
        _local.conn = None
    _initialized = False


def reinit(data_dir):
    """Re-initialize with a new data directory."""
    global QUEUE_DB_PATH
    QUEUE_DB_PATH = os.path.join(data_dir, "memory_queue.db")
    init_db()


@contextmanager
def get_connection():
    """Get thread-local SQLite connection."""
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
    except sqlite3.Error as e:
        if hasattr(_local, 'conn') and _local.conn:
            try:
                _local.conn.close()
            except:
                pass
            _local.conn = None
        raise
    finally:
        # Always rollback uncommitted transactions to release write locks
        # Committed transactions are unaffected by rollback
        if hasattr(_local, 'conn') and _local.conn is not None:
            try:
                _local.conn.rollback()
            except:
                pass


# =============================================================================
# Schema Management
# =============================================================================

def _get_schema_version(conn) -> int:
    """Get current schema version from database."""
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


def _set_schema_version(conn, version: int):
    """Set schema version in database."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY
        )
    """)
    conn.execute("DELETE FROM schema_version")
    conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))


def _run_migrations(conn, from_version: int) -> bool:
    """Run migrations from from_version to SCHEMA_VERSION."""
    current = from_version

    while current < SCHEMA_VERSION:
        if current == 0:
            # Version 0 -> 1: Initial schema

            # Per-NPC processing state and locking
            conn.execute("""
                CREATE TABLE IF NOT EXISTS npc_state (
                    npc_id TEXT PRIMARY KEY,
                    is_processing INTEGER DEFAULT 0,
                    last_processed_entry_id INTEGER DEFAULT 0,
                    last_completed_at INTEGER,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
            """)

            # Processing queue
            conn.execute("""
                CREATE TABLE IF NOT EXISTS processing_queue (
                    npc_id TEXT PRIMARY KEY,
                    priority INTEGER DEFAULT 0,
                    queued_at INTEGER NOT NULL,
                    attempts INTEGER DEFAULT 0,
                    last_error TEXT
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_queue_priority
                ON processing_queue(priority DESC, queued_at ASC)
            """)

            # Chapter sync state. The graphiti_synced column name is kept for
            # compatibility with existing queue DBs, but it now means memory sync.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS chapter_sync (
                    chapter_id TEXT PRIMARY KEY,
                    npc_id TEXT NOT NULL,
                    title TEXT,
                    start_timestamp INTEGER,
                    end_timestamp INTEGER,
                    graphiti_synced INTEGER DEFAULT 0,
                    synced_at INTEGER,
                    error_message TEXT,
                    created_at INTEGER NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_chapter_npc ON chapter_sync(npc_id)")
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_chapter_unsent
                ON chapter_sync(graphiti_synced) WHERE graphiti_synced = 0
            """)

            current = 1

        else:
            raise ValueError(f"Unknown schema version {current}")

    _set_schema_version(conn, SCHEMA_VERSION)
    return from_version != SCHEMA_VERSION


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
                    print(f"[MemoryQueue] Migrating schema from v{current_version} to v{SCHEMA_VERSION}")
                    _run_migrations(conn, current_version)
                    conn.commit()
                    print("[MemoryQueue] Schema migration complete")

            _initialized = True
            print(f"[MemoryQueue] Database initialized (schema v{SCHEMA_VERSION})")
        except Exception as e:
            print(f"[MemoryQueue] Error initializing database: {e}")
            raise


def _ensure_initialized():
    """Ensure database is initialized before operations."""
    if not _initialized:
        from . import player_context
        if not player_context.is_ready():
            return
        init_db()


def recover_stale_processing(max_age_seconds: int = PROCESSING_STALE_TIMEOUT_SECONDS) -> int:
    """Release abandoned processing locks left behind by interrupted workers."""
    _ensure_initialized()
    now = int(time.time())
    cutoff = now - max_age_seconds

    with get_connection() as conn:
        stale_rows = conn.execute("""
            SELECT npc_id FROM npc_state
            WHERE is_processing = 1
              AND COALESCE(updated_at, 0) < ?
        """, (cutoff,)).fetchall()

        if not stale_rows:
            return 0

        stale_ids = [row['npc_id'] for row in stale_rows]
        placeholders = ",".join("?" for _ in stale_ids)

        conn.execute(f"""
            UPDATE npc_state
            SET is_processing = 0,
                updated_at = ?
            WHERE npc_id IN ({placeholders})
        """, [now] + stale_ids)
        conn.commit()

    print(f"[MemoryQueue] Recovered {len(stale_ids)} stale processing lock(s): {stale_ids}")
    return len(stale_ids)


def _manual_recheck_active(npc_id: str) -> bool:
    """Check if an NPC has a manual recheck request pending."""
    with _manual_recheck_lock:
        request = _manual_recheck_requests.get(npc_id)
        return bool(request and request.get("status") == "pending")


def _complete_manual_recheck(npc_id: str, result: Dict[str, Any]):
    """Store the result for a manual recheck and wake any waiter."""
    with _manual_recheck_lock:
        request = _manual_recheck_requests.get(npc_id)
        if not request:
            return
        request["status"] = "completed"
        request["result"] = result
        request["event"].set()


def _build_force_detection_entries(npc_id: str, pending_entries: List[Dict], chapter_mgr) -> List[Dict]:
    """Build a richer context slice for manual chapter checks."""
    from .dialogue_db import get_entries_for_npc
    from .memory import should_include_entry_for_npc_chaptering
    from .settings import load_settings

    include_cutscene = load_settings().get('memory', {}).get('include_cutscene', True)

    full_entries = [
        e for e in get_entries_for_npc(npc_id)
        if should_include_entry_for_npc_chaptering(npc_id, e, include_cutscene=include_cutscene)
    ]
    if not full_entries:
        return []

    open_chapter = chapter_mgr.get_open_chapter(npc_id)
    start_ts = open_chapter.get('start_timestamp') if open_chapter else None

    if not start_ts and pending_entries:
        timestamps = [e.get('timestamp', 0) for e in pending_entries if e.get('timestamp', 0) > 0]
        if timestamps:
            start_ts = min(timestamps)

    if start_ts:
        before = [e for e in full_entries if e.get('timestamp', 0) < start_ts]
        after = [e for e in full_entries if e.get('timestamp', 0) >= start_ts]
        return before[-10:] + after

    return full_entries[-40:]


# =============================================================================
# Queue Management API
# =============================================================================

def queue_npcs_for_processing(npc_ids: List[str], priority: int = 0) -> List[str]:
    """
    Queue NPCs for memory indexing. Existing rows are retained and can be promoted.

    Args:
        npc_ids: List of NPC voice IDs to queue
        priority: Higher priority = processed sooner (default 0)

    Returns:
        The NPC IDs that were actually admitted to the queue.
    """
    if not npc_ids:
        return []

    from .memory import filter_memory_enabled_npc_ids

    requested_npc_ids = [npc_id for npc_id in npc_ids if npc_id]
    npc_ids = filter_memory_enabled_npc_ids(requested_npc_ids)
    if not npc_ids:
        print(f"[MemoryQueue] No eligible NPCs to queue from {len(requested_npc_ids)} request(s)")
        return []

    _ensure_initialized()
    now = int(time.time())

    with get_connection() as conn:
        for npc_id in npc_ids:
            try:
                conn.execute("""
                    INSERT INTO processing_queue (npc_id, priority, queued_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(npc_id) DO UPDATE SET
                        priority = MAX(processing_queue.priority, excluded.priority)
                """, (npc_id, priority, now))
            except sqlite3.Error as e:
                print(f"[MemoryQueue] Error queueing {npc_id}: {e}")
        conn.commit()

    # Ensure worker is running
    ensure_worker_running()

    queued_count = len(npc_ids)
    print(f"[MemoryQueue] Queued {queued_count} NPCs for processing")
    return npc_ids


def get_queue_status() -> Dict[str, Any]:
    """Get current queue status for diagnostics."""
    _ensure_initialized()

    with get_connection() as conn:
        queue_count = conn.execute("SELECT COUNT(*) FROM processing_queue").fetchone()[0]
        processing = conn.execute(
            "SELECT npc_id FROM npc_state WHERE is_processing = 1"
        ).fetchall()
        processing_ids = [row['npc_id'] for row in processing]

        pending_chapters = conn.execute(
            "SELECT COUNT(*) FROM chapter_sync WHERE graphiti_synced = 0"
        ).fetchone()[0]

    return {
        "queued": queue_count,
        "processing": processing_ids,
        "pending_chapters": pending_chapters,
        "worker_running": _worker is not None and _worker.is_running(),
    }


def get_npc_state(npc_id: str) -> Optional[Dict[str, Any]]:
    """Get processing state for a specific NPC."""
    _ensure_initialized()

    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM npc_state WHERE npc_id = ?", (npc_id,)
        ).fetchone()

        if row:
            return dict(row)
    return None


def is_chapter_synced(chapter_id: str) -> bool:
    """Check if a chapter has already been synced to long-term memory."""
    _ensure_initialized()

    with get_connection() as conn:
        row = conn.execute(
            "SELECT graphiti_synced FROM chapter_sync WHERE chapter_id = ?",
            (chapter_id,)
        ).fetchone()
        return row is not None and row['graphiti_synced'] == 1


def get_chapter_id(npc_id: str, start_ts: int, end_ts: Optional[int] = None) -> str:
    """Generate deterministic chapter ID for deduplication."""
    return f"{npc_id}:{start_ts}:{end_ts or 'open'}"


def mark_chapter_pending(npc_id: str, chapter_id: str, title: str,
                         start_ts: int, end_ts: Optional[int] = None):
    """Record a chapter that needs to be synced to long-term memory."""
    _ensure_initialized()
    now = int(time.time())

    with get_connection() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO chapter_sync
            (chapter_id, npc_id, title, start_timestamp, end_timestamp, graphiti_synced, created_at)
            VALUES (?, ?, ?, ?, ?, 0, ?)
        """, (chapter_id, npc_id, title, start_ts, end_ts, now))
        conn.commit()


def mark_chapter_synced(chapter_id: str):
    """Mark a chapter as successfully synced to long-term memory."""
    _ensure_initialized()
    now = int(time.time())

    with get_connection() as conn:
        conn.execute("""
            UPDATE chapter_sync
            SET graphiti_synced = 1, synced_at = ?, error_message = NULL
            WHERE chapter_id = ?
        """, (now, chapter_id))
        conn.commit()


def mark_chapter_failed(chapter_id: str, error: str):
    """Mark a chapter sync as failed."""
    _ensure_initialized()

    with get_connection() as conn:
        conn.execute("""
            UPDATE chapter_sync
            SET graphiti_synced = -1, error_message = ?
            WHERE chapter_id = ?
        """, (error[:500], chapter_id))
        conn.commit()


# =============================================================================
# Entry Retrieval (with IDs for checkpointing)
# =============================================================================

def get_entries_since_id(npc_id: str, last_id: int, limit: int = 500) -> List[Dict]:
    """
    Get dialogue entries for an NPC since the given entry ID.
    Returns entries with their database IDs for checkpointing.
    """
    from .dialogue_db import (
        get_connection as get_dialogue_conn,
        _ensure_initialized as ensure_dialogue_init,
    )
    from .localization import canonicalize_npc_id

    ensure_dialogue_init()

    with get_dialogue_conn() as conn:
        # Escape for LIKE pattern
        escaped_name = npc_id.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')

        rows = conn.execute("""
            SELECT id, timestamp, game_time, game_date, speaker, voice_name,
                   target, target_id, text, is_player, is_ai_response, entry_type, earshot,
                   line_id, location
            FROM dialogue_entries
            WHERE id > ?
              AND (voice_name = ? OR earshot LIKE ? ESCAPE '\\')
            ORDER BY id ASC
            LIMIT ?
        """, (last_id, npc_id, f'%"{escaped_name}"%', limit)).fetchall()

    entries = []
    for row in rows:
        earshot = row['earshot']
        if earshot:
            try:
                earshot = json.loads(earshot)
            except:
                earshot = []
        else:
            earshot = []

        entries.append({
            'id': row['id'],  # Include DB id for checkpointing
            'timestamp': row['timestamp'] or 0,
            'gameTime': row['game_time'],
            'gameDate': row['game_date'],
            'speaker': row['speaker'],
            'voiceName': row['voice_name'],
            'target': row['target'],
            'targetId': canonicalize_npc_id(row['target_id']),
            'text': row['text'],
            'isPlayer': bool(row['is_player']),
            'isAIResponse': bool(row['is_ai_response']),
            'type': row['entry_type'] or 'dialogue',
            'earshot': earshot,
            'lineID': row['line_id'],
            'location': row['location'],
        })

    return entries


def get_max_entry_id_for_timestamp(npc_id: str, timestamp: int) -> int:
    """
    Get the maximum entry ID for entries up to a given timestamp.
    Used for migrating from old timestamp-based tracking.
    """
    from .dialogue_db import get_connection as get_dialogue_conn, _ensure_initialized as ensure_dialogue_init

    ensure_dialogue_init()

    with get_dialogue_conn() as conn:
        escaped_name = npc_id.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')

        row = conn.execute("""
            SELECT MAX(id) as max_id FROM dialogue_entries
            WHERE timestamp <= ?
              AND (voice_name = ? OR earshot LIKE ? ESCAPE '\\')
        """, (timestamp, npc_id, f'%"{escaped_name}"%')).fetchone()

        return row['max_id'] if row and row['max_id'] else 0


# =============================================================================
# Worker Thread
# =============================================================================

class MemoryIndexWorker:
    """
    Background worker that processes the memory indexing queue.

    Single-threaded to prevent race conditions. Uses checkpointing
    to handle failures gracefully.
    """

    def __init__(self):
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def start(self):
        """Start the worker thread."""
        if self._running:
            return

        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="MemoryIndexWorker")
        self._thread.start()
        print("[MemoryQueue] Worker started")

    def stop(self, timeout: float = 5.0):
        """Stop the worker thread."""
        if not self._running:
            return

        self._running = False
        self._stop_event.set()

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

        print("[MemoryQueue] Worker stopped")

    def is_running(self) -> bool:
        """Check if worker is running."""
        return self._running and self._thread is not None and self._thread.is_alive()

    def _run(self):
        """Main worker loop."""
        print("[MemoryQueue] Worker thread started")

        while self._running:
            try:
                # Pause processing when mod is disabled
                if not load_settings().get('server', {}).get('enabled', True):
                    self._stop_event.wait(timeout=5.0)
                    continue

                npc_id = self._dequeue_next()

                if npc_id:
                    remove_from_queue = False
                    success = False
                    result = None
                    try:
                        result = self._process_npc(npc_id) or {"status": "no_result"}
                        status = result.get("status")
                        remove_from_queue = status not in ("failed", "memory_unavailable", "memory_disabled")
                        success = status not in ("failed", "memory_unavailable", "memory_disabled", "memory_filtered")
                    except Exception as e:
                        print(f"[MemoryQueue] Error processing {npc_id}: {e}")
                        import traceback
                        traceback.print_exc()
                        self._mark_failed(npc_id, str(e))
                        result = {"status": "failed", "error": str(e)}
                    finally:
                        # Only remove from queue on success
                        # On failure, unlock but keep in queue for retry
                        self._finish_npc(npc_id, remove_from_queue=remove_from_queue)
                        if _manual_recheck_active(npc_id):
                            _complete_manual_recheck(npc_id, {"success": success, **(result or {})})
                else:
                    # No work available, wait
                    self._stop_event.wait(timeout=2.0)

            except Exception as e:
                print(f"[MemoryQueue] Worker error: {e}")
                import traceback
                traceback.print_exc()
                time.sleep(5)  # Back off on errors

        print("[MemoryQueue] Worker thread exiting")

    def _dequeue_next(self) -> Optional[str]:
        """Get next queued NPC that isn't already processing."""
        _ensure_initialized()
        recover_stale_processing()

        MAX_ATTEMPTS = 5  # Give up after this many failures

        with get_connection() as conn:
            # Select next NPC that isn't being processed and hasn't exceeded max attempts
            row = conn.execute("""
                SELECT q.npc_id FROM processing_queue q
                LEFT JOIN npc_state s ON q.npc_id = s.npc_id
                WHERE COALESCE(s.is_processing, 0) = 0
                  AND q.attempts < ?
                ORDER BY q.priority DESC, q.queued_at ASC
                LIMIT 1
            """, (MAX_ATTEMPTS,)).fetchone()

            # Clean up entries that exceeded max attempts (log them first)
            failed = conn.execute("""
                SELECT npc_id, attempts, last_error FROM processing_queue WHERE attempts >= ?
            """, (MAX_ATTEMPTS,)).fetchall()
            for fail_row in failed:
                print(f"[MemoryQueue] Giving up on {fail_row['npc_id']} after {fail_row['attempts']} attempts. "
                      f"Last error: {fail_row['last_error']}")

            conn.execute("""
                DELETE FROM processing_queue WHERE attempts >= ?
            """, (MAX_ATTEMPTS,))

            if not row:
                conn.commit()  # Must commit the DELETE before returning
                return None

            npc_id = row['npc_id']
            now = int(time.time())

            # Lock the NPC
            conn.execute("""
                INSERT INTO npc_state (npc_id, is_processing, created_at, updated_at)
                VALUES (?, 1, ?, ?)
                ON CONFLICT(npc_id) DO UPDATE SET
                    is_processing = 1,
                    updated_at = ?
            """, (npc_id, now, now, now))

            # Increment attempts
            conn.execute("""
                UPDATE processing_queue SET attempts = attempts + 1
                WHERE npc_id = ?
            """, (npc_id,))

            conn.commit()
            return npc_id

    def _process_npc(self, npc_id: str, force_detection: bool = False):
        """Process all pending entries for an NPC."""
        print(f"[MemoryQueue] Processing NPC: {npc_id}")
        force_detection = force_detection or _manual_recheck_active(npc_id)

        # Check if memory system is available
        try:
            from .memory import is_memory_available, MemoryManager, ChapterManager
            from .memory import detect_chapter_boundary, generate_episode_content
            from .memory import update_bio_incremental, load_settings
            from .localization import get_display_name
        except ImportError as e:
            print(f"[MemoryQueue] Memory system not available: {e}")
            return {"status": "memory_unavailable", "error": str(e), "force_detection": force_detection}

        if not is_memory_available():
            print(f"[MemoryQueue] Memory system not enabled, skipping {npc_id}")
            return {"status": "memory_unavailable", "force_detection": force_detection}

        settings = load_settings()
        memory_settings = settings.get('memory', {})

        if not memory_settings.get('enabled', True):
            print(f"[MemoryQueue] Memory disabled in settings, skipping {npc_id}")
            return {"status": "memory_disabled", "force_detection": force_detection}

        from .memory import get_npc_long_term_memory_status
        memory_status = get_npc_long_term_memory_status(npc_id, settings=settings)
        if not memory_status.get("enabled"):
            print(f"[MemoryQueue] Long-term memory disabled for {npc_id} ({memory_status.get('reason')}), skipping")
            return {
                "status": "memory_filtered",
                "force_detection": force_detection,
                "reason": memory_status.get("reason"),
            }

        # Get current state
        with get_connection() as conn:
            state = conn.execute(
                "SELECT last_processed_entry_id FROM npc_state WHERE npc_id = ?",
                (npc_id,)
            ).fetchone()
            last_id = (state['last_processed_entry_id'] or 0) if state else 0

        # Migrate from old timestamp-based tracking if needed
        if last_id == 0:
            last_id = self._migrate_from_timestamp(npc_id)

        # Get entries to process
        all_entries = get_entries_since_id(npc_id, last_id)

        # Track max ID from full fetch for checkpointing (so we don't re-fetch filtered entries)
        all_max_entry_id = max((e['id'] for e in all_entries), default=last_id)

        chapter_mgr = ChapterManager()
        open_chapter = chapter_mgr.get_open_chapter(npc_id)

        # Filter to the shared set of entries that actually count for chaptering.
        from .memory import should_include_entry_for_npc_chaptering, collect_chapter_context_characters
        include_cutscene = memory_settings.get('include_cutscene', True)
        entries = [e for e in all_entries
                   if should_include_entry_for_npc_chaptering(npc_id, e, include_cutscene=include_cutscene)]

        if force_detection:
            if not entries and not open_chapter:
                print(f"[MemoryQueue] No pending dialogue or open chapter to recheck for {npc_id}")
                return {"status": "no_new_entries", "processed_entries": 0, "force_detection": True}
            entries = _build_force_detection_entries(npc_id, entries, chapter_mgr)
            if not entries:
                print(f"[MemoryQueue] No dialogue context available for forced recheck of {npc_id}")
                return {"status": "no_new_entries", "processed_entries": 0, "force_detection": True}

        elif not all_entries:
            print(f"[MemoryQueue] No new entries for {npc_id}")
            return {"status": "no_new_entries", "processed_entries": 0, "force_detection": False}

        if not entries:
            # Only ambient lines - checkpoint past them so we don't re-fetch
            self._checkpoint_progress(npc_id, all_max_entry_id)
            print(f"[MemoryQueue] No significant entries for {npc_id}, checkpointed past ambient")
            return {
                "status": "ambient_only",
                "processed_entries": 0,
                "checkpointed_entry_id": all_max_entry_id,
                "force_detection": force_detection,
            }

        print(f"[MemoryQueue] Found {len(entries)} entries to process for {npc_id} (filtered from {len(all_entries)})")

        # Get NPC display name
        npc_name = get_display_name(npc_id)

        # Get current context from most recent entry
        recent_entry = entries[-1]
        current_location = recent_entry.get('location', 'Unknown')
        game_date = recent_entry.get('gameDate', '')
        game_time = recent_entry.get('gameTime', '')

        characters_in_earshot = collect_chapter_context_characters(npc_id, entries)

        # Check entry threshold before calling LLM
        # Always require minimum entries regardless of location change
        threshold = memory_settings.get('chapter_entry_threshold', 30)

        threshold_bypassed = force_detection and len(entries) < threshold

        if len(entries) < threshold and not force_detection:
            # Under threshold - do NOT checkpoint so entries accumulate
            # across multiple turns until we hit the threshold
            print(f"[MemoryQueue] Under threshold ({len(entries)} < {threshold}), waiting for more entries")
            return {"status": "under_threshold", "processed_entries": len(entries), "threshold": threshold}
        elif threshold_bypassed:
            print(f"[MemoryQueue] Force bypassing threshold for {npc_id} ({len(entries)} < {threshold})")

        # Run chapter detection
        result = detect_chapter_boundary(
            npc_id=npc_id,
            npc_name=npc_name,
            dialogue_entries=entries,
            current_location=current_location,
            characters_in_earshot=characters_in_earshot,
            force_detection=force_detection
        )

        if result.get('_failed'):
            # LLM call failed - don't checkpoint so entries are retried
            print(f"[MemoryQueue] Chapter detection LLM failed, will retry")
            return {"status": "failed", "processed_entries": len(entries), "force_detection": force_detection}

        if result.get('_skipped'):
            # Detection legitimately skipped (same location, under threshold, or meta initialized)
            # Safe to checkpoint since detect_chapter_boundary made an informed decision
            self._checkpoint_progress(npc_id, all_max_entry_id)
            print(f"[MemoryQueue] Chapter detection skipped, checkpointed to {all_max_entry_id}")
            return {
                "status": "skipped",
                "processed_entries": len(entries),
                "checkpointed_entry_id": all_max_entry_id,
                "threshold_bypassed": threshold_bypassed,
                "force_detection": force_detection,
            }

        # Process the result
        self._apply_chapter_result(
            npc_id=npc_id,
            npc_name=npc_name,
            result=result,
            entries=entries,
            current_location=current_location,
            game_date=game_date,
            game_time=game_time,
            chapter_mgr=chapter_mgr
        )

        # Sync any pending chapters to long-term memory.
        self._sync_pending_chapters(npc_id)

        # Checkpoint progress
        self._checkpoint_progress(npc_id, all_max_entry_id)

        print(f"[MemoryQueue] Completed processing {npc_id}, checkpointed to entry {all_max_entry_id}")
        return {
            "status": "processed",
            "processed_entries": len(entries),
            "checkpointed_entry_id": all_max_entry_id,
            "current_chapter_action": result.get('current_chapter_action', 'continue'),
            "new_chapters": len(result.get('new_chapters', [])),
            "threshold_bypassed": threshold_bypassed,
            "force_detection": force_detection,
        }

    def _migrate_from_timestamp(self, npc_id: str) -> int:
        """Migrate from old timestamp-based tracking to ID-based."""
        try:
            from .memory import ChapterManager

            chapter_mgr = ChapterManager()
            indexing_meta = chapter_mgr.get_indexing_meta(npc_id)
            last_ts = indexing_meta.get('last_index_timestamp', 0)
            print(f"[MemoryQueue] {npc_id}: indexing_meta={indexing_meta}, last_ts={last_ts}")

            # If no timestamp but there are existing closed chapters, use their end timestamp
            # This prevents reprocessing for NPCs with data but missing indexing_meta
            if last_ts == 0:
                closed_chapters = chapter_mgr.get_closed_chapters(npc_id)
                print(f"[MemoryQueue] {npc_id}: Found {len(closed_chapters)} closed chapters")
                if closed_chapters:
                    # Use the latest end_timestamp from closed chapters
                    max_chapter_ts = max(
                        c.get('end_timestamp', 0) or 0 for c in closed_chapters
                    )
                    if max_chapter_ts > 0:
                        last_ts = max_chapter_ts
                        print(f"[MemoryQueue] {npc_id}: Using closed chapter end_timestamp={last_ts}")

            if last_ts > 0:
                # Find the max entry ID for that timestamp
                max_id = get_max_entry_id_for_timestamp(npc_id, last_ts)
                print(f"[MemoryQueue] {npc_id}: get_max_entry_id_for_timestamp({last_ts}) = {max_id}")
                if max_id > 0:
                    # Update our state - use INSERT OR REPLACE to ensure row exists
                    with get_connection() as conn:
                        now = int(time.time())
                        conn.execute("""
                            INSERT INTO npc_state (npc_id, is_processing, last_processed_entry_id, created_at, updated_at)
                            VALUES (?, 1, ?, ?, ?)
                            ON CONFLICT(npc_id) DO UPDATE SET
                                last_processed_entry_id = ?,
                                updated_at = ?
                        """, (npc_id, max_id, now, now, max_id, now))
                        conn.commit()
                    print(f"[MemoryQueue] Migrated {npc_id} from timestamp {last_ts} to entry ID {max_id}")
                    return max_id
                else:
                    print(f"[MemoryQueue] {npc_id}: WARNING - no entry ID found for timestamp {last_ts}")
            else:
                print(f"[MemoryQueue] {npc_id}: No timestamp found, will process all entries")
        except Exception as e:
            import traceback
            print(f"[MemoryQueue] Migration error for {npc_id}: {e}")
            traceback.print_exc()

        return 0

    def _apply_chapter_result(self, npc_id: str, npc_name: str, result: Dict,
                               entries: List[Dict], current_location: str,
                               game_date: str, game_time: str, chapter_mgr):
        """Apply chapter detection result - close/open chapters as needed."""

        current_action = result.get('current_chapter_action', 'continue')
        additional_events = result.get('additional_events', [])

        # Handle closing current chapter
        if current_action == 'close':
            open_chapter = chapter_mgr.get_open_chapter(npc_id)
            if open_chapter:
                if additional_events:
                    existing_events = open_chapter.get('key_events', [])
                    existing_events.extend(additional_events)
                    open_chapter['key_events'] = existing_events

                close_at = result.get('close_at_timestamp')
                chapter_entries = [e for e in entries if not close_at or e.get('timestamp', 0) <= close_at]

                # Extract player name
                player_name = "the student"
                for entry in chapter_entries:
                    if entry.get('isPlayer') and entry.get('speaker'):
                        player_name = entry['speaker']
                        break

                # Record chapter for memory sync (idempotent)
                start_ts = open_chapter.get('start_timestamp', 0)
                end_ts = chapter_entries[-1].get('timestamp') if chapter_entries else None
                chapter_id = get_chapter_id(npc_id, start_ts, end_ts)

                if not is_chapter_synced(chapter_id):
                    mark_chapter_pending(
                        npc_id=npc_id,
                        chapter_id=chapter_id,
                        title=open_chapter.get('title', 'Untitled'),
                        start_ts=start_ts,
                        end_ts=end_ts
                    )

                    # Store content for sync
                    self._store_chapter_content(
                        chapter_id=chapter_id,
                        npc_name=npc_name,
                        chapter=open_chapter,
                        entries=chapter_entries,
                        player_name=player_name,
                        game_date=open_chapter.get('start_date', game_date),
                        game_time=open_chapter.get('start_time', game_time)
                    )

                # Close in chapter manager
                chapter_mgr.close_chapter(
                    npc_id,
                    open_chapter.get('summary', ''),
                    end_timestamp=end_ts,
                    key_events=open_chapter.get('key_events', [])
                )
                print(f"[MemoryQueue] Closed chapter '{open_chapter.get('title')}' for {npc_id}")

        elif current_action == 'continue' and additional_events:
            chapter_mgr.add_events_to_chapter(npc_id, additional_events)

        # Process new chapters
        new_chapters = result.get('new_chapters', [])
        for new_chapter in new_chapters:
            self._process_new_chapter(
                npc_id=npc_id,
                npc_name=npc_name,
                new_chapter=new_chapter,
                entries=entries,
                current_location=current_location,
                game_date=game_date,
                game_time=game_time,
                chapter_mgr=chapter_mgr
            )

        # If no chapters exist and none were created, start a default one
        if not chapter_mgr.get_open_chapter(npc_id) and not new_chapters:
            chapter_mgr.start_chapter(npc_id, "Beginning", current_location, game_date, game_time)

    def _process_new_chapter(self, npc_id: str, npc_name: str, new_chapter: Dict,
                              entries: List[Dict], current_location: str,
                              game_date: str, game_time: str, chapter_mgr):
        """Process a new chapter from detection result."""
        from .memory import is_valid_timestamp

        title = new_chapter.get('title', 'New Chapter')
        status = new_chapter.get('status', 'open')
        summary = new_chapter.get('summary', '')
        events = new_chapter.get('key_events', [])
        start_ts = new_chapter.get('start_timestamp')
        end_ts = new_chapter.get('end_timestamp')

        # Validate timestamps
        if start_ts and not is_valid_timestamp(start_ts):
            print(f"[MemoryQueue] Invalid start_timestamp {start_ts} for '{title}'")
            return
        if end_ts and not is_valid_timestamp(end_ts):
            print(f"[MemoryQueue] Invalid end_timestamp {end_ts} for '{title}'")
            return

        if status == 'closed' and start_ts and end_ts:
            # Closed chapter - queue for memory sync
            chapter_entries = [e for e in entries if start_ts <= e.get('timestamp', 0) <= end_ts]

            player_name = "the student"
            for entry in chapter_entries:
                if entry.get('isPlayer') and entry.get('speaker'):
                    player_name = entry['speaker']
                    break

            chapter_id = get_chapter_id(npc_id, start_ts, end_ts)

            if not is_chapter_synced(chapter_id):
                mark_chapter_pending(
                    npc_id=npc_id,
                    chapter_id=chapter_id,
                    title=title,
                    start_ts=start_ts,
                    end_ts=end_ts
                )

                self._store_chapter_content(
                    chapter_id=chapter_id,
                    npc_name=npc_name,
                    chapter={'title': title, 'summary': summary, 'key_events': events, 'location': current_location},
                    entries=chapter_entries,
                    player_name=player_name,
                    game_date=game_date,
                    game_time=game_time
                )

            print(f"[MemoryQueue] Queued closed chapter '{title}' for {npc_id}")

        else:
            # Open chapter - just track in chapter manager
            context_messages = []
            for entry in entries[-15:]:
                context_messages.append({
                    'time': f"{entry.get('gameDate', '')} {entry.get('gameTime', '')}".strip(),
                    'speaker': entry.get('speaker', 'Unknown'),
                    'text': (entry.get('text') or '')[:200]
                })

            chapter_mgr.start_chapter(
                npc_id, title, current_location, game_date, game_time,
                summary=summary,
                start_timestamp=start_ts,
                key_events=events,
                context_messages=context_messages
            )
            print(f"[MemoryQueue] Started new chapter '{title}' for {npc_id}")

    def _store_chapter_content(self, chapter_id: str, npc_name: str, chapter: Dict,
                                entries: List[Dict], player_name: str,
                                game_date: str, game_time: str):
        """Store chapter content for later memory sync."""
        # Store in a separate file to avoid bloating the SQLite DB
        content_dir = os.path.join(DATA_DIR, "chapter_content")
        os.makedirs(content_dir, exist_ok=True)

        # Sanitize chapter_id for filename
        safe_id = chapter_id.replace(':', '_').replace('/', '_')
        content_path = os.path.join(content_dir, f"{safe_id}.json")

        content_data = {
            'chapter_id': chapter_id,
            'npc_name': npc_name,
            'title': chapter.get('title', 'Untitled'),
            'summary': chapter.get('summary', ''),
            'key_events': chapter.get('key_events', []),
            'location': chapter.get('location', 'Unknown'),
            'entries': entries,
            'player_name': player_name,
            'game_date': game_date,
            'game_time': game_time,
        }

        with open(content_path, 'w', encoding='utf-8') as f:
            json.dump(content_data, f)

    def _load_chapter_content(self, chapter_id: str) -> Optional[Dict]:
        """Load stored chapter content for later memory sync."""
        content_dir = os.path.join(DATA_DIR, "chapter_content")
        safe_id = chapter_id.replace(':', '_').replace('/', '_')
        content_path = os.path.join(content_dir, f"{safe_id}.json")

        if not os.path.exists(content_path):
            return None

        try:
            with open(content_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"[MemoryQueue] Error loading chapter content: {e}")
            return None

    def _cleanup_chapter_content(self, chapter_id: str, content_data: Optional[Dict] = None):
        """Archive stored chapter content after successful sync for later debugging."""
        content_dir = os.path.join(DATA_DIR, "chapter_content")
        safe_id = chapter_id.replace(':', '_').replace('/', '_')
        content_path = os.path.join(content_dir, f"{safe_id}.json")
        synced_dir = os.path.join(content_dir, "synced")
        synced_path = os.path.join(synced_dir, f"{safe_id}.json")

        try:
            if os.path.exists(content_path):
                os.makedirs(synced_dir, exist_ok=True)
                if content_data is not None:
                    with open(content_path, 'w', encoding='utf-8') as f:
                        json.dump(content_data, f, ensure_ascii=False, indent=2)
                if os.path.exists(synced_path):
                    os.remove(synced_path)
                os.replace(content_path, synced_path)
        except Exception as e:
            print(f"[MemoryQueue] Error archiving chapter content: {e}")

    def _sync_pending_chapters(self, npc_id: str):
        """Sync all pending chapters to long-term memory."""
        from .memory import (
            MemoryManager,
            generate_episode_content,
            get_npc_long_term_memory_status,
            save_generated_episode_audit,
            update_bio_incremental,
        )

        memory_status = get_npc_long_term_memory_status(npc_id)
        if not memory_status.get("enabled"):
            print(f"[MemoryQueue] Skipping memory sync for {npc_id} ({memory_status.get('reason')})")
            return

        with get_connection() as conn:
            pending = conn.execute("""
                SELECT chapter_id, title, start_timestamp, end_timestamp
                FROM chapter_sync
                WHERE npc_id = ? AND (graphiti_synced = 0
                    OR (graphiti_synced = -1 AND error_message NOT LIKE '%not found%'))
                ORDER BY start_timestamp ASC
            """, (npc_id,)).fetchall()

        if not pending:
            return

        # Check for retries by querying failed count
        with get_connection() as conn:
            retry_count = conn.execute(
                "SELECT COUNT(*) FROM chapter_sync WHERE npc_id = ? AND graphiti_synced = -1 AND error_message NOT LIKE '%not found%'",
                (npc_id,)
            ).fetchone()[0]
        if retry_count:
            print(f"[MemoryQueue] Retrying {retry_count} previously failed chapter(s) for {npc_id}")

        memory_mgr = MemoryManager()
        if not memory_mgr.init_graphiti():
            print(f"[MemoryQueue] Memory backend not available for sync")
            return

        for chapter_row in pending:
            chapter_id = chapter_row['chapter_id']

            # Double-check not already synced
            if is_chapter_synced(chapter_id):
                continue

            # Load stored content
            content_data = self._load_chapter_content(chapter_id)
            if not content_data:
                print(f"[MemoryQueue] No content found for chapter {chapter_id}")
                mark_chapter_failed(chapter_id, "Content file not found")
                continue

            try:
                # Generate episode content
                episode_content = generate_episode_content(
                    npc_name=content_data['npc_name'],
                    chapter_title=content_data['title'],
                    chapter_summary=content_data['summary'],
                    key_events=content_data['key_events'],
                    location=content_data['location'],
                    dialogue_entries=content_data['entries'],
                    player_name=content_data['player_name']
                )
                content_data['generated_episode_content'] = episode_content
                save_generated_episode_audit(npc_id, content_data['title'], episode_content, content_data)

                # Send to long-term memory
                success = memory_mgr.add_episode(
                    npc_id=npc_id,
                    chapter_title=content_data['title'],
                    content=episode_content,
                    game_date=content_data['game_date'],
                    game_time=content_data['game_time']
                )

                if success:
                    mark_chapter_synced(chapter_id)
                    self._cleanup_chapter_content(chapter_id, content_data)
                    print(f"[MemoryQueue] Synced chapter '{content_data['title']}' to memory")

                    # Trigger bio update using timestamps from DB
                    try:
                        update_bio_incremental(
                            npc_id=npc_id,
                            npc_name=content_data['npc_name'],
                            chapter_start_ts=chapter_row['start_timestamp'] or 0,
                            chapter_end_ts=chapter_row['end_timestamp'] or int(time.time())
                        )
                    except Exception as e:
                        print(f"[MemoryQueue] Bio update failed (non-fatal): {e}")
                else:
                    mark_chapter_failed(chapter_id, "Memory add_episode returned False")

            except Exception as e:
                print(f"[MemoryQueue] Error syncing chapter {chapter_id}: {e}")
                import traceback
                traceback.print_exc()
                mark_chapter_failed(chapter_id, str(e)[:500])

    def _checkpoint_progress(self, npc_id: str, entry_id: int):
        """Update checkpoint after successful processing."""
        now = int(time.time())

        with get_connection() as conn:
            conn.execute("""
                UPDATE npc_state
                SET last_processed_entry_id = ?, updated_at = ?
                WHERE npc_id = ?
            """, (entry_id, now, npc_id))
            conn.commit()

    def _mark_failed(self, npc_id: str, error: str):
        """Mark NPC processing as failed."""
        with get_connection() as conn:
            conn.execute("""
                UPDATE processing_queue SET last_error = ?
                WHERE npc_id = ?
            """, (error[:500], npc_id))
            conn.commit()

    def _finish_npc(self, npc_id: str, remove_from_queue: bool = True):
        """Finish processing an NPC - unlock and optionally remove from queue.

        Args:
            npc_id: The NPC ID
            remove_from_queue: If True, remove from queue (success case).
                              If False, keep in queue for retry (failure case).
        """
        now = int(time.time())

        with get_connection() as conn:
            # Unlock
            conn.execute("""
                UPDATE npc_state
                SET is_processing = 0, last_completed_at = ?, updated_at = ?
                WHERE npc_id = ?
            """, (now, now, npc_id))

            # Only remove from queue on success
            if remove_from_queue:
                conn.execute("DELETE FROM processing_queue WHERE npc_id = ?", (npc_id,))

            conn.commit()


# =============================================================================
# Worker Management
# =============================================================================

def ensure_worker_running():
    """Ensure the memory index worker is running."""
    global _worker

    recover_stale_processing()

    if _worker is None:
        _worker = MemoryIndexWorker()

    if not _worker.is_running():
        _worker.start()


def stop_worker(timeout: float = 5.0):
    """Stop the memory index worker."""
    global _worker

    if _worker is not None:
        _worker.stop(timeout=timeout)
        _worker = None


def get_worker() -> Optional[MemoryIndexWorker]:
    """Get the current worker instance."""
    return _worker


def is_processing() -> bool:
    """Check if any memory processing is currently active."""
    if _worker is not None and _worker.is_running():
        # Check if worker is actively processing (not just waiting)
        with get_connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM npc_state WHERE is_processing = 1"
            ).fetchone()
            if row and row['cnt'] > 0:
                return True

    # Also check if the memory backend is busy.
    try:
        from .memory import MemoryManager
        memory_mgr = MemoryManager()
        if hasattr(memory_mgr, '_busy') and memory_mgr._busy:
            return True
    except:
        pass

    return False


def graceful_shutdown(max_wait: float = 30.0) -> bool:
    """
    Gracefully shutdown memory processing, waiting for current operations.

    Args:
        max_wait: Maximum seconds to wait for operations to complete

    Returns:
        True if shutdown was clean, False if timed out
    """
    global _worker

    def _close_memory_connection():
        try:
            from .memory import reset_memory_connection
            return reset_memory_connection()
        except Exception as e:
            print(f"[MemoryQueue] Error closing memory connection: {e}")
            return False

    if _worker is None:
        return _close_memory_connection()

    print("[MemoryQueue] Graceful shutdown requested...")

    # Signal worker to stop accepting new work
    _worker._running = False
    _worker._stop_event.set()

    # Wait for current processing to finish
    start_time = time.time()
    while time.time() - start_time < max_wait:
        if not is_processing():
            print("[MemoryQueue] All operations complete")
            stop_worker(timeout=2.0)
            close_ok = _close_memory_connection()
            close_all_connections()
            if not close_ok:
                print("[MemoryQueue] Memory connection did not close cleanly")
            return close_ok

        elapsed = int(time.time() - start_time)
        print(f"[MemoryQueue] Waiting for operations to complete... ({elapsed}s)")
        time.sleep(1.0)

    print(f"[MemoryQueue] Timeout after {max_wait}s, forcing shutdown")
    stop_worker(timeout=2.0)

    close_ok = _close_memory_connection()
    close_all_connections()
    if not close_ok:
        print("[MemoryQueue] Memory connection did not close cleanly during forced shutdown")

    return False


# =============================================================================
# Cleanup/Reset
# =============================================================================

def reset_npc_state(npc_id: str):
    """Reset processing state for an NPC (for debugging/recovery)."""
    _ensure_initialized()

    with get_connection() as conn:
        conn.execute("DELETE FROM npc_state WHERE npc_id = ?", (npc_id,))
        conn.execute("DELETE FROM processing_queue WHERE npc_id = ?", (npc_id,))
        conn.execute("DELETE FROM chapter_sync WHERE npc_id = ?", (npc_id,))
        conn.commit()

    print(f"[MemoryQueue] Reset state for {npc_id}")


def reset_all_processing_state():
    """Reset processing checkpoints/locks without dropping chapter sync history."""
    _ensure_initialized()

    with get_connection() as conn:
        conn.execute("DELETE FROM npc_state")
        conn.execute("DELETE FROM processing_queue")
        conn.commit()

    print("[MemoryQueue] Reset processing checkpoints for all NPCs")


def clear_unsynced_chapters_for_npc(npc_id: str) -> int:
    """Delete pending/failed unsynced chapter records and staged content for an NPC."""
    _ensure_initialized()

    with get_connection() as conn:
        rows = conn.execute("""
            SELECT chapter_id FROM chapter_sync
            WHERE npc_id = ? AND graphiti_synced != 1
        """, (npc_id,)).fetchall()

        chapter_ids = [row['chapter_id'] for row in rows]
        if chapter_ids:
            conn.execute("""
                DELETE FROM chapter_sync
                WHERE npc_id = ? AND graphiti_synced != 1
            """, (npc_id,))
            conn.commit()

    if not chapter_ids:
        return 0

    content_dir = os.path.join(DATA_DIR, "chapter_content")
    for chapter_id in chapter_ids:
        safe_id = chapter_id.replace(':', '_').replace('/', '_')
        content_path = os.path.join(content_dir, f"{safe_id}.json")
        try:
            if os.path.exists(content_path):
                os.remove(content_path)
        except Exception as e:
            print(f"[MemoryQueue] Error removing staged chapter content for {chapter_id}: {e}")

    print(f"[MemoryQueue] Cleared {len(chapter_ids)} unsynced chapter(s) for {npc_id}")
    return len(chapter_ids)


def force_chapter_recheck(npc_id: str, wait_timeout: float = 60.0) -> Dict[str, Any]:
    """Queue a manual chapter recheck and wait for the normal worker to finish it."""
    from .memory import get_npc_long_term_memory_status

    memory_status = get_npc_long_term_memory_status(npc_id)
    if not memory_status.get("enabled"):
        return {"success": False, "error": f"Long-term memory is disabled for {npc_id}"}

    _ensure_initialized()
    recover_stale_processing()

    with get_connection() as conn:
        state = conn.execute(
            "SELECT is_processing FROM npc_state WHERE npc_id = ?",
            (npc_id,)
        ).fetchone()
        if state and state['is_processing'] == 1:
            return {"success": False, "error": f"{npc_id} is already being processed"}

    with _manual_recheck_lock:
        existing = _manual_recheck_requests.get(npc_id)
        if existing and existing.get("status") == "pending":
            return {"success": False, "error": f"{npc_id} already has a pending manual chapter recheck"}

        event = threading.Event()
        _manual_recheck_requests[npc_id] = {
            "status": "pending",
            "requested_at": time.time(),
            "event": event,
            "result": None,
        }

    queued_npc_ids = queue_npcs_for_processing([npc_id], priority=100)
    if npc_id not in queued_npc_ids:
        with _manual_recheck_lock:
            _manual_recheck_requests.pop(npc_id, None)
        memory_status = get_npc_long_term_memory_status(npc_id)
        if not memory_status.get("enabled"):
            return {"success": False, "error": f"Long-term memory is disabled for {npc_id}"}
        return {"success": False, "error": f"Could not queue chapter recheck for {npc_id}"}

    completed = event.wait(timeout=wait_timeout)
    if not completed:
        with _manual_recheck_lock:
            _manual_recheck_requests.pop(npc_id, None)
        return {"success": False, "error": f"Timed out waiting for chapter recheck for {npc_id}"}

    with _manual_recheck_lock:
        request = _manual_recheck_requests.pop(npc_id, None)

    if not request:
        return {"success": False, "error": f"Manual chapter recheck completed without a stored result for {npc_id}"}

    return request.get("result") or {"success": False, "error": "Manual chapter recheck returned no result"}


def reset_all_state():
    """Reset processing state for all NPCs. Used when clearing all memories."""
    _ensure_initialized()

    with get_connection() as conn:
        conn.execute("DELETE FROM npc_state")
        conn.execute("DELETE FROM processing_queue")
        conn.execute("DELETE FROM chapter_sync")
        conn.commit()

    print("[MemoryQueue] Reset state for all NPCs")


def retry_failed_chapters(queue_npcs: bool = True) -> int:
    """Retry all failed chapter syncs.

    Args:
        queue_npcs: If True, also queue the affected NPCs for processing

    Returns:
        Number of chapters marked for retry
    """
    _ensure_initialized()

    with get_connection() as conn:
        # Get affected NPCs before updating
        if queue_npcs:
            affected_npcs = conn.execute("""
                SELECT DISTINCT npc_id FROM chapter_sync WHERE graphiti_synced = -1
            """).fetchall()
            npc_ids = [row['npc_id'] for row in affected_npcs]

        conn.execute("""
            UPDATE chapter_sync SET graphiti_synced = 0, error_message = NULL
            WHERE graphiti_synced = -1
        """)
        count = conn.total_changes
        conn.commit()

    if count > 0:
        print(f"[MemoryQueue] Marked {count} failed chapters for retry")
        if queue_npcs and npc_ids:
            queue_npcs_for_processing(npc_ids, priority=1)  # Higher priority for retries

    return count


try:
    from . import player_context
    player_context.register("memory_queue", close_all, reinit)
except ImportError:
    pass
