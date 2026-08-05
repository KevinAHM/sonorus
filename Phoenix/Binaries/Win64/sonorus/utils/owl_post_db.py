"""
SQLite-backed storage for Sonorus Owl Post system (mail and bulletin boards).
Follows the same patterns as commitments_db.py: thread-local connections, WAL mode, schema versioning.
"""

import json
import os
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager

import time as _time

from .dialogue_db import (
    _game_datetime_to_minutes, _parse_game_time, _parse_game_date,
    _minutes_to_game_datetime, _format_game_time, _format_game_date,
    append_entry,
)
from .localization import get_display_name
from .settings import DATA_DIR


DB_PATH = os.path.join(DATA_DIR, "owl_post.db")

# Thread-local storage for connections
_local = threading.local()

# Track all connections for shutdown cleanup
_all_connections = []
_all_connections_lock = threading.Lock()

# Lock for initialization
_init_lock = threading.Lock()
_initialized = False

SCHEMA_VERSION = 6

# Seed data for boards
_BOARD_SEEDS = [
    ("Great Hall Notices", "great-hall-notices", "Official announcements and student notices posted in the Great Hall.", "public", None, None),
    ("Gryffindor Common Room", "gryffindor-common-room", "The Gryffindor common room notice board.", "house_locked", "Gryffindor", None),
    ("Hufflepuff Common Room", "hufflepuff-common-room", "The Hufflepuff common room notice board.", "house_locked", "Hufflepuff", None),
    ("Ravenclaw Common Room", "ravenclaw-common-room", "The Ravenclaw common room notice board.", "house_locked", "Ravenclaw", None),
    ("Slytherin Common Room", "slytherin-common-room", "The Slytherin common room notice board.", "house_locked", "Slytherin", None),
    ("Study Groups", "study-groups", "A board for organising study sessions and academic discussions.", "public", None, None),
    ("Faculty Board", "faculty-board", "Staff announcements and faculty correspondence.", "decorative", None, None),
    ("Mischief Corner", "mischief-corner", "A hidden board for those who know the password.", "password_locked", None, "mandrake"),
]


def _ensure_data_dir():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)


def _create_connection():
    _ensure_data_dir()
    conn = sqlite3.connect(DB_PATH, timeout=30.0, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    with _all_connections_lock:
        _all_connections.append(conn)
    return conn


def close_all_connections():
    """Close every tracked SQLite connection across all threads.

    Called during server shutdown to release file handles on Windows.
    """
    with _all_connections_lock:
        for conn in _all_connections:
            try:
                conn.close()
            except Exception:
                pass
        _all_connections.clear()
    if hasattr(_local, 'conn'):
        _local.conn = None


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
    global DB_PATH
    DB_PATH = os.path.join(data_dir, "owl_post.db")
    init_db()


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
                except Exception:
                    pass
                _local.conn = None

        if not hasattr(_local, 'conn') or _local.conn is None:
            _local.conn = _create_connection()

        yield _local.conn
    except sqlite3.Error:
        if hasattr(_local, 'conn') and _local.conn:
            try:
                _local.conn.close()
            except Exception:
                pass
            _local.conn = None
        raise
    except Exception:
        # Rollback uncommitted writes on any error
        if hasattr(_local, 'conn') and _local.conn is not None:
            try:
                _local.conn.rollback()
            except Exception:
                pass
        raise


def _get_schema_version(conn):
    try:
        result = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
        ).fetchone()
        if not result:
            return 0
        row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
        return row[0] if row else 0
    except Exception:
        return 0


def _set_schema_version(conn, version):
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
    conn.execute("DELETE FROM schema_version")
    conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))


def _run_migrations(conn, from_version):
    current = from_version

    while current < SCHEMA_VERSION:
        if current == 0:
            # --- mail table ---
            conn.execute("""
                CREATE TABLE IF NOT EXISTS mail (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    thread_id TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    recipient TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    body TEXT NOT NULL,
                    sent_at REAL NOT NULL,
                    arrives_at REAL NOT NULL,
                    read INTEGER NOT NULL DEFAULT 0
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_mail_thread ON mail(thread_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_mail_recipient ON mail(recipient)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_mail_sender ON mail(sender)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_mail_arrives ON mail(arrives_at)")

            # --- boards table ---
            conn.execute("""
                CREATE TABLE IF NOT EXISTS boards (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    slug TEXT NOT NULL UNIQUE,
                    description TEXT NOT NULL,
                    access_type TEXT NOT NULL,
                    house TEXT,
                    password TEXT,
                    first_visited_at REAL
                )
            """)

            # --- board_posts table ---
            conn.execute("""
                CREATE TABLE IF NOT EXISTS board_posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    board_id INTEGER NOT NULL REFERENCES boards(id),
                    author TEXT NOT NULL,
                    title TEXT,
                    body TEXT NOT NULL,
                    root_post_id INTEGER NOT NULL,
                    parent_id INTEGER,
                    created_at REAL NOT NULL,
                    visible_at REAL NOT NULL,
                    read INTEGER NOT NULL DEFAULT 0
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_board_posts_board ON board_posts(board_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_board_posts_root ON board_posts(root_post_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_board_posts_visible ON board_posts(visible_at)")

            # --- board_unlocks table ---
            conn.execute("""
                CREATE TABLE IF NOT EXISTS board_unlocks (
                    board_id INTEGER NOT NULL UNIQUE REFERENCES boards(id),
                    unlocked_at REAL NOT NULL
                )
            """)

            # --- Seed boards ---
            for name, slug, description, access_type, house, password in _BOARD_SEEDS:
                conn.execute("""
                    INSERT OR IGNORE INTO boards (name, slug, description, access_type, house, password)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (name, slug, description, access_type, house, password))

            _set_schema_version(conn, 1)
            current = 1

        if current < 2:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS mail_eval_state (
                    npc_id TEXT PRIMARY KEY,
                    last_eval_entry_id INTEGER NOT NULL DEFAULT 0,
                    last_eval_result TEXT NOT NULL DEFAULT 'no',
                    last_eval_at REAL NOT NULL DEFAULT 0
                )
            """)
            _set_schema_version(conn, 2)
            current = 2

        if current < 3:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS owl_post_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    event TEXT NOT NULL,
                    npc_id TEXT,
                    detail TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_owl_post_log_ts ON owl_post_log(timestamp DESC)")
            _set_schema_version(conn, 3)
            current = 3

        if current < 4:
            try:
                conn.execute("ALTER TABLE mail ADD COLUMN summary TEXT")
            except Exception:
                pass  # Column may already exist
            _set_schema_version(conn, 4)
            current = 4

        if current < 5:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS mail_proposals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mail_id INTEGER NOT NULL,
                    npc_id TEXT NOT NULL,
                    target TEXT NOT NULL,
                    location TEXT NOT NULL,
                    datetime TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    commitment_id TEXT,
                    FOREIGN KEY (mail_id) REFERENCES mail(id) ON DELETE CASCADE
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_mail_proposals_mail ON mail_proposals(mail_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_mail_proposals_status ON mail_proposals(status)")
            _set_schema_version(conn, 5)
            current = 5

        if current < 6:
            try:
                conn.execute("ALTER TABLE mail ADD COLUMN mail_type TEXT NOT NULL DEFAULT 'letter'")
            except Exception:
                pass  # Column may already exist
            _set_schema_version(conn, 6)
            current = 6

        if current > SCHEMA_VERSION:
            raise ValueError(f"Unknown schema version {current}")


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
                    print(f"[OwlPostDB] Migrating schema from v{current_version} to v{SCHEMA_VERSION}")
                    _run_migrations(conn, current_version)
                    conn.commit()

            _initialized = True
            print(f"[OwlPostDB] Database initialized (schema v{SCHEMA_VERSION})")
        except Exception as e:
            print(f"[OwlPostDB] Error initializing database: {e}")
            raise


def _ensure_initialized():
    if not _initialized:
        from . import player_context
        if not player_context.is_ready():
            return
        init_db()


# ============================================
# Time Helpers
# ============================================

def get_current_game_minutes(game_context):
    """Extract current game time from a game context dict and return absolute game minutes.

    Parses the timeFormatted field (e.g. '1:41 AM') and uses year/month/day fields.
    Uses _game_datetime_to_minutes from dialogue_db (not duplicated).
    """
    time_str = game_context.get('timeFormatted', '') or game_context.get('time', '')
    time_tuple = _parse_game_time(time_str)
    if time_tuple is None:
        return 0

    year = game_context.get('year')
    month = game_context.get('month')
    day = game_context.get('day')

    if year is not None and month is not None and day is not None:
        date_tuple = (int(year), int(month), int(day))
    else:
        date_tuple = None
        date_str = game_context.get('dateFormatted') or game_context.get('gameDate')
        if date_str:
            date_tuple = _parse_game_date(date_str)
        if date_tuple is None:
            # Fallback: use a reasonable default date
            date_tuple = (1890, 1, 1)

    return _game_datetime_to_minutes(date_tuple, time_tuple)


def get_latest_recorded_game_minutes():
    """Return the latest stored owl-post game minute across mail and board data, or 0."""
    _ensure_initialized()
    try:
        with get_connection() as conn:
            row = conn.execute("""
                SELECT MAX(value) AS latest_minutes
                FROM (
                    SELECT MAX(sent_at) AS value FROM mail
                    UNION ALL
                    SELECT MAX(arrives_at) AS value FROM mail
                    UNION ALL
                    SELECT MAX(created_at) AS value FROM board_posts
                    UNION ALL
                    SELECT MAX(visible_at) AS value FROM board_posts
                )
            """).fetchone()
        return int(row["latest_minutes"] or 0)
    except Exception as e:
        print(f"[OwlPostDB] Error getting latest recorded game minutes: {e}")
        return 0


def get_latest_recorded_game_time_candidates(limit=5, min_minutes=0):
    """Return top Owl Post rows contributing the latest recorded game times."""
    _ensure_initialized()
    candidates = []

    try:
        with get_connection() as conn:
            mail_rows = conn.execute("""
                SELECT id, thread_id, sender, recipient, subject, sent_at, arrives_at
                FROM mail
            """).fetchall()
            board_rows = conn.execute("""
                SELECT id, board_id, author, title, body, root_post_id, parent_id, created_at, visible_at
                FROM board_posts
            """).fetchall()

        for row in mail_rows:
            for source_label, minutes_key in (("mail_sent", "sent_at"), ("mail_arrives", "arrives_at")):
                total_minutes = int(row[minutes_key] or 0)
                if total_minutes < min_minutes or total_minutes <= 0:
                    continue
                subject = (row["subject"] or "").strip()
                if len(subject) > 120:
                    subject = subject[:117] + "..."
                candidates.append({
                    "minutes": total_minutes,
                    "kind": "mail",
                    "source": source_label,
                    "id": row["id"],
                    "threadId": row["thread_id"],
                    "sender": row["sender"] or "",
                    "recipient": row["recipient"] or "",
                    "subject": subject,
                })

        for row in board_rows:
            for source_label, minutes_key in (("board_created", "created_at"), ("board_visible", "visible_at")):
                total_minutes = int(row[minutes_key] or 0)
                if total_minutes < min_minutes or total_minutes <= 0:
                    continue
                title = (row["title"] or row["body"] or "").strip()
                if len(title) > 120:
                    title = title[:117] + "..."
                candidates.append({
                    "minutes": total_minutes,
                    "kind": "board_post",
                    "source": source_label,
                    "id": row["id"],
                    "boardId": row["board_id"],
                    "rootPostId": row["root_post_id"],
                    "parentId": row["parent_id"],
                    "author": row["author"] or "",
                    "title": title,
                })

        candidates.sort(key=lambda c: (c["minutes"], c["id"]), reverse=True)
        return candidates[:limit]
    except Exception as e:
        print(f"[OwlPostDB] Error getting latest recorded game time candidates: {e}")
        return []


# ============================================
# Mail CRUD
# ============================================

def send_mail(sender, recipient, subject, body, sent_at, arrives_at, thread_id=None, player_name=None, mail_type='letter'):
    """Send a mail message. Returns (mail_id, thread_id).

    Auto-generates a UUID thread_id if None.
    player_name: Optional display name for the player (e.g. 'Adri Valter') for history entries.
    mail_type: Type of letter — 'letter' (default) or 'howler'.
    """
    _ensure_initialized()
    if thread_id is None:
        thread_id = str(uuid.uuid4())

    try:
        with get_connection() as conn:
            cursor = conn.execute("""
                INSERT INTO mail (thread_id, sender, recipient, subject, body, sent_at, arrives_at, mail_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (thread_id, sender, recipient, subject, body, sent_at, arrives_at, mail_type))
            mail_id = cursor.lastrowid
            conn.commit()
        _inject_mail_history_entry(sender, recipient, subject, sent_at, player_name=player_name)
        return (mail_id, thread_id)
    except Exception as e:
        print(f"[OwlPostDB] Error sending mail: {e}")
        return (None, thread_id)


def _inject_mail_history_entry(sender, recipient, subject, sent_at, player_name=None):
    """Write a 'mail' entry into dialogue history so it appears in the timeline."""
    try:
        (date_tuple, time_tuple) = _minutes_to_game_datetime(sent_at)
        game_time = _format_game_time(*time_tuple)
        game_date = _format_game_date(*date_tuple)

        sender_name = (player_name or "Player") if sender == "player" else (get_display_name(sender) or sender)
        recipient_name = (player_name or "Player") if recipient == "player" else (get_display_name(recipient) or recipient)

        entry = {
            "timestamp": int(_time.time()),
            "gameTime": game_time,
            "gameDate": game_date,
            "speaker": sender_name,
            "voiceName": sender if sender != "player" else recipient,
            "target": recipient_name,
            "targetId": recipient if recipient != "player" else sender,
            "text": subject,
            "isPlayer": sender == "player",
            "isAIResponse": False,
            "type": "mail",
        }
        append_entry(entry)
    except Exception as e:
        print(f"[OwlPostDB] Error injecting mail history entry: {e}")


def get_player_mail(current_game_minutes):
    """Get player's inbox and sent mail.

    Returns delivered inbox mail (NPC->player where arrives_at <= current) plus
    all sent mail (player->anyone). Adds an 'in_flight' boolean flag.
    Hides undelivered NPC->player mail.
    """
    _ensure_initialized()
    try:
        with get_connection() as conn:
            # Delivered mail TO player + all mail FROM player
            rows = conn.execute("""
                SELECT * FROM mail
                WHERE (recipient = 'player' AND arrives_at <= ?)
                   OR sender = 'player'
                ORDER BY sent_at DESC
            """, (current_game_minutes,)).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            d['in_flight'] = d['arrives_at'] > current_game_minutes
            result.append(d)
        return result
    except Exception as e:
        print(f"[OwlPostDB] Error getting player mail: {e}")
        return []


def get_mail_thread(thread_id, current_game_minutes):
    """Get all messages in a thread, chronological.

    Hides undelivered NPC->player mail.
    """
    _ensure_initialized()
    try:
        with get_connection() as conn:
            rows = conn.execute("""
                SELECT * FROM mail
                WHERE thread_id = ?
                  AND (sender = 'player' OR (recipient = 'player' AND arrives_at <= ?))
                ORDER BY sent_at ASC
            """, (thread_id, current_game_minutes)).fetchall()
        result = []
        for row in rows:
            entry = dict(row)
            entry["in_flight"] = entry["arrives_at"] > current_game_minutes
            result.append(entry)
        return result
    except Exception as e:
        print(f"[OwlPostDB] Error getting mail thread: {e}")
        return []


def mark_mail_read(mail_id):
    """Mark a single mail message as read."""
    _ensure_initialized()
    try:
        with get_connection() as conn:
            conn.execute("UPDATE mail SET read = 1 WHERE id = ?", (mail_id,))
            conn.commit()
    except Exception as e:
        print(f"[OwlPostDB] Error marking mail read: {e}")


def delete_mail(mail_id):
    """Delete a single mail message. Returns the deleted row dict or None."""
    _ensure_initialized()
    try:
        with get_connection() as conn:
            row = conn.execute("SELECT * FROM mail WHERE id = ?", (mail_id,)).fetchone()
            if row is None:
                return None
            conn.execute("DELETE FROM mail WHERE id = ?", (mail_id,))
            conn.commit()
            return dict(row)
    except Exception as e:
        print(f"[OwlPostDB] Error deleting mail: {e}")
        return None


def delete_mail_thread(thread_id):
    """Delete all messages in a mail thread. Returns list of deleted row dicts."""
    _ensure_initialized()
    try:
        with get_connection() as conn:
            rows = conn.execute("SELECT * FROM mail WHERE thread_id = ?", (thread_id,)).fetchall()
            if not rows:
                return []
            conn.execute("DELETE FROM mail WHERE thread_id = ?", (thread_id,))
            conn.commit()
            return [dict(r) for r in rows]
    except Exception as e:
        print(f"[OwlPostDB] Error deleting mail thread: {e}")
        return []


def get_unread_mail_count(current_game_minutes):
    """Get count of unread, delivered mail for the player."""
    _ensure_initialized()
    try:
        with get_connection() as conn:
            row = conn.execute("""
                SELECT COUNT(*) FROM mail
                WHERE recipient = 'player'
                  AND arrives_at <= ?
                  AND read = 0
            """, (current_game_minutes,)).fetchone()
        return row[0] if row else 0
    except Exception as e:
        print(f"[OwlPostDB] Error getting unread mail count: {e}")
        return 0


def find_existing_thread(npc_id):
    """Find the most recent thread_id between player and a specific NPC.

    Returns thread_id or None.
    """
    _ensure_initialized()
    try:
        with get_connection() as conn:
            row = conn.execute("""
                SELECT thread_id FROM mail
                WHERE (sender = ? AND recipient = 'player')
                   OR (sender = 'player' AND recipient = ?)
                ORDER BY sent_at DESC
                LIMIT 1
            """, (npc_id, npc_id)).fetchone()
        return row['thread_id'] if row else None
    except Exception as e:
        print(f"[OwlPostDB] Error finding existing thread: {e}")
        return None


def thread_has_correspondent(thread_id, npc_id):
    """Return True if the thread already contains player mail with this correspondent."""
    _ensure_initialized()
    if not thread_id or not npc_id:
        return False

    try:
        with get_connection() as conn:
            row = conn.execute("""
                SELECT 1 FROM mail
                WHERE thread_id = ?
                  AND (
                    (sender = ? AND recipient = 'player')
                    OR (sender = 'player' AND recipient = ?)
                  )
                LIMIT 1
            """, (thread_id, npc_id, npc_id)).fetchone()
        return row is not None
    except Exception as e:
        print(f"[OwlPostDB] Error checking thread correspondent: {e}")
        return False


def get_unanswered_player_mail(current_game_minutes):
    """Get delivered player-sent mail that has no NPC response yet.

    Returns mail sent by the player where no reply exists in the same thread
    from the recipient after the player's message.
    """
    _ensure_initialized()
    try:
        with get_connection() as conn:
            rows = conn.execute("""
                SELECT m.* FROM mail m
                WHERE m.sender = 'player'
                  AND m.arrives_at <= ?
                  AND NOT EXISTS (
                      SELECT 1 FROM mail r
                      WHERE r.thread_id = m.thread_id
                        AND r.sender = m.recipient
                        AND r.sent_at > m.sent_at
                  )
                ORDER BY m.sent_at ASC
            """, (current_game_minutes,)).fetchall()
        return [dict(row) for row in rows]
    except Exception as e:
        print(f"[OwlPostDB] Error getting unanswered player mail: {e}")
        return []


def get_recent_mail_for_npc(npc_id, limit=3, thread_id=None):
    """Get recent mail between player and a specific NPC, chronological.

    If thread_id is provided, only returns messages from that thread.
    """
    _ensure_initialized()
    try:
        with get_connection() as conn:
            if thread_id:
                rows = conn.execute("""
                    SELECT * FROM mail
                    WHERE thread_id = ?
                      AND ((sender = ? AND recipient = 'player')
                        OR (sender = 'player' AND recipient = ?))
                    ORDER BY sent_at DESC
                    LIMIT ?
                """, (thread_id, npc_id, npc_id, limit)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT * FROM mail
                    WHERE (sender = ? AND recipient = 'player')
                       OR (sender = 'player' AND recipient = ?)
                    ORDER BY sent_at DESC
                    LIMIT ?
                """, (npc_id, npc_id, limit)).fetchall()
        # Reverse to chronological order
        return [dict(row) for row in reversed(rows)]
    except Exception as e:
        print(f"[OwlPostDB] Error getting recent mail for NPC: {e}")
        return []


def update_mail_summary(mail_id, summary):
    """Store a condensed summary for a mail message."""
    _ensure_initialized()
    try:
        with get_connection() as conn:
            conn.execute("UPDATE mail SET summary = ? WHERE id = ?", (summary, mail_id))
            conn.commit()
    except Exception as e:
        print(f"[OwlPostDB] Error updating mail summary: {e}")


def insert_mail_proposal(mail_id, npc_id, target, location, datetime_str):
    _ensure_initialized()
    try:
        with get_connection() as conn:
            cursor = conn.execute("""
                INSERT INTO mail_proposals (mail_id, npc_id, target, location, datetime, status)
                VALUES (?, ?, ?, ?, ?, 'pending')
            """, (mail_id, npc_id, target, location, datetime_str))
            proposal_id = cursor.lastrowid
            conn.commit()
        return proposal_id
    except Exception as e:
        print(f"[OwlPostDB] Error inserting proposal: {e}")
        return None


def get_proposals_for_mail(mail_id):
    _ensure_initialized()
    try:
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM mail_proposals WHERE mail_id = ?", (mail_id,)
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[OwlPostDB] Error getting proposals: {e}")
        return []


def get_proposals_for_thread(thread_id):
    _ensure_initialized()
    try:
        with get_connection() as conn:
            rows = conn.execute("""
                SELECT p.* FROM mail_proposals p
                JOIN mail m ON p.mail_id = m.id
                WHERE m.thread_id = ?
                ORDER BY p.id ASC
            """, (thread_id,)).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[OwlPostDB] Error getting thread proposals: {e}")
        return []


def get_proposal(proposal_id):
    _ensure_initialized()
    try:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM mail_proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
        return dict(row) if row else None
    except Exception as e:
        print(f"[OwlPostDB] Error getting proposal: {e}")
        return None


def update_proposal_status(proposal_id, status, commitment_id=None):
    _ensure_initialized()
    try:
        with get_connection() as conn:
            if commitment_id:
                conn.execute(
                    "UPDATE mail_proposals SET status = ?, commitment_id = ? WHERE id = ?",
                    (status, commitment_id, proposal_id),
                )
            else:
                conn.execute(
                    "UPDATE mail_proposals SET status = ? WHERE id = ?",
                    (status, proposal_id),
                )
            conn.commit()
    except Exception as e:
        print(f"[OwlPostDB] Error updating proposal status: {e}")


def get_pending_proposal_counts(mail_ids):
    """Get count of pending proposals per mail_id. Returns {mail_id: count}."""
    _ensure_initialized()
    if not mail_ids:
        return {}
    try:
        with get_connection() as conn:
            # Placeholders built from len(mail_ids), not user input — safe from injection
            placeholders = ",".join("?" for _ in mail_ids)
            rows = conn.execute(f"""
                SELECT mail_id, COUNT(*) as cnt FROM mail_proposals
                WHERE mail_id IN ({placeholders}) AND status = 'pending'
                GROUP BY mail_id
            """, mail_ids).fetchall()
        return {r["mail_id"]: r["cnt"] for r in rows}
    except Exception as e:
        print(f"[OwlPostDB] Error getting pending proposal counts: {e}")
        return {}


# ============================================
# Board CRUD
# ============================================

def get_all_boards():
    """Get all boards as a list of dicts."""
    _ensure_initialized()
    try:
        with get_connection() as conn:
            rows = conn.execute("SELECT * FROM boards ORDER BY id ASC").fetchall()
        return [dict(row) for row in rows]
    except Exception as e:
        print(f"[OwlPostDB] Error getting all boards: {e}")
        return []


def get_board_by_slug(slug):
    """Get a single board by slug. Returns dict or None."""
    _ensure_initialized()
    try:
        with get_connection() as conn:
            row = conn.execute("SELECT * FROM boards WHERE slug = ?", (slug,)).fetchone()
        return dict(row) if row else None
    except Exception as e:
        print(f"[OwlPostDB] Error getting board by slug: {e}")
        return None


def mark_board_visited(slug, game_minutes):
    """Set first_visited_at on a board if it hasn't been visited yet."""
    _ensure_initialized()
    try:
        with get_connection() as conn:
            conn.execute("""
                UPDATE boards SET first_visited_at = ?
                WHERE slug = ? AND first_visited_at IS NULL
            """, (game_minutes, slug))
            conn.commit()
    except Exception as e:
        print(f"[OwlPostDB] Error marking board visited: {e}")


def is_board_unlocked(board_id):
    """Check if a board has been unlocked. Returns bool."""
    _ensure_initialized()
    try:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM board_unlocks WHERE board_id = ?", (board_id,)
            ).fetchone()
        return row is not None
    except Exception as e:
        print(f"[OwlPostDB] Error checking board unlock: {e}")
        return False


def unlock_board(board_id, game_minutes):
    """Record that a board has been unlocked."""
    _ensure_initialized()
    try:
        with get_connection() as conn:
            conn.execute("""
                INSERT OR IGNORE INTO board_unlocks (board_id, unlocked_at)
                VALUES (?, ?)
            """, (board_id, game_minutes))
            conn.commit()
    except Exception as e:
        print(f"[OwlPostDB] Error unlocking board: {e}")


def get_board_threads(board_id, current_game_minutes, limit=20):
    """Get visible top-level posts for a board with reply_count and latest_activity.

    Returns list of dicts, ordered by latest activity descending.
    """
    _ensure_initialized()
    try:
        with get_connection() as conn:
            rows = conn.execute("""
                SELECT
                    p.*,
                    (SELECT COUNT(*) FROM board_posts r
                     WHERE r.root_post_id = p.id AND r.id != p.id
                       AND r.visible_at <= ?) AS reply_count,
                    (SELECT MAX(r.visible_at) FROM board_posts r
                     WHERE r.root_post_id = p.id
                       AND r.visible_at <= ?) AS latest_activity,
                    (SELECT COUNT(*) FROM board_posts bp4
                     WHERE bp4.root_post_id = p.id AND bp4.read = 0
                       AND bp4.visible_at <= ?) AS unread_count
                FROM board_posts p
                WHERE p.board_id = ?
                  AND p.parent_id IS NULL
                  AND p.visible_at <= ?
                ORDER BY latest_activity DESC
                LIMIT ?
            """, (current_game_minutes, current_game_minutes, current_game_minutes,
                  board_id, current_game_minutes, limit)).fetchall()
        return [dict(row) for row in rows]
    except Exception as e:
        print(f"[OwlPostDB] Error getting board threads: {e}")
        return []


def get_thread_posts(root_post_id, current_game_minutes):
    """Get all visible posts in a thread, chronological."""
    _ensure_initialized()
    try:
        with get_connection() as conn:
            rows = conn.execute("""
                SELECT * FROM board_posts
                WHERE root_post_id = ?
                  AND visible_at <= ?
                ORDER BY created_at ASC
            """, (root_post_id, current_game_minutes)).fetchall()
        return [dict(row) for row in rows]
    except Exception as e:
        print(f"[OwlPostDB] Error getting thread posts: {e}")
        return []


def create_board_post(board_id, author, title, body, created_at, visible_at,
                      root_post_id=None, parent_id=None):
    """Create a board post. Returns post_id.

    If root_post_id is None, this is a new thread (self-references after insert).
    """
    _ensure_initialized()
    try:
        with get_connection() as conn:
            if root_post_id is None:
                # New top-level thread: insert with a temporary root_post_id, then update
                cursor = conn.execute("""
                    INSERT INTO board_posts
                        (board_id, author, title, body, root_post_id, parent_id, created_at, visible_at)
                    VALUES (?, ?, ?, ?, 0, NULL, ?, ?)
                """, (board_id, author, title, body, created_at, visible_at))
                post_id = cursor.lastrowid
                # Self-reference
                conn.execute(
                    "UPDATE board_posts SET root_post_id = ? WHERE id = ?",
                    (post_id, post_id)
                )
            else:
                cursor = conn.execute("""
                    INSERT INTO board_posts
                        (board_id, author, title, body, root_post_id, parent_id, created_at, visible_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (board_id, author, title, body, root_post_id, parent_id,
                      created_at, visible_at))
                post_id = cursor.lastrowid
            conn.commit()
        return post_id
    except Exception as e:
        print(f"[OwlPostDB] Error creating board post: {e}")
        return None


def mark_post_read(post_id):
    """Mark a single board post as read."""
    _ensure_initialized()
    try:
        with get_connection() as conn:
            conn.execute("UPDATE board_posts SET read = 1 WHERE id = ?", (post_id,))
            conn.commit()
    except Exception as e:
        print(f"[OwlPostDB] Error marking post read: {e}")


def mark_thread_read(root_post_id, current_game_minutes):
    """Mark all visible posts in a thread as read."""
    _ensure_initialized()
    try:
        with get_connection() as conn:
            conn.execute("""
                UPDATE board_posts SET read = 1
                WHERE root_post_id = ?
                  AND visible_at <= ?
            """, (root_post_id, current_game_minutes))
            conn.commit()
    except Exception as e:
        print(f"[OwlPostDB] Error marking thread read: {e}")


def get_unread_count_per_board(current_game_minutes):
    """Get unread post counts per board. Returns {board_id: count}."""
    _ensure_initialized()
    try:
        with get_connection() as conn:
            rows = conn.execute("""
                SELECT board_id, COUNT(*) as cnt
                FROM board_posts
                WHERE read = 0
                  AND visible_at <= ?
                GROUP BY board_id
            """, (current_game_minutes,)).fetchall()
        return {row['board_id']: row['cnt'] for row in rows}
    except Exception as e:
        print(f"[OwlPostDB] Error getting unread counts: {e}")
        return {}


def delete_future_replies(root_post_id, current_game_minutes):
    """Delete not-yet-visible NPC replies in a thread.

    Removes posts where visible_at > current_game_minutes (excluding the root post itself).
    """
    _ensure_initialized()
    try:
        with get_connection() as conn:
            conn.execute("""
                DELETE FROM board_posts
                WHERE root_post_id = ?
                  AND id != root_post_id
                  AND visible_at > ?
                  AND author != 'player'
            """, (root_post_id, current_game_minutes))
            conn.commit()
    except Exception as e:
        print(f"[OwlPostDB] Error deleting future replies: {e}")


def get_recent_board_threads(board_id, limit=5):
    """Get recent thread titles for LLM context. Returns list of dicts with title and author."""
    _ensure_initialized()
    try:
        with get_connection() as conn:
            rows = conn.execute("""
                SELECT title, author, created_at FROM board_posts
                WHERE board_id = ?
                  AND parent_id IS NULL
                  AND title IS NOT NULL
                ORDER BY created_at DESC
                LIMIT ?
            """, (board_id, limit)).fetchall()
        return [dict(row) for row in rows]
    except Exception as e:
        print(f"[OwlPostDB] Error getting recent board threads: {e}")
        return []


def load_board_roster():
    """Load the NPC board roster from npc_board_roster.json.

    Returns dict mapping board slug to roster info.
    """
    roster_path = os.path.join(DATA_DIR, "npc_board_roster.json")
    try:
        with open(roster_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"[OwlPostDB] Error loading board roster: {e}")
        return {}


# ============================================
# Mail Evaluation State
# ============================================

def get_eval_state(npc_id):
    """Get the last evaluation state for an NPC. Returns dict or None."""
    _ensure_initialized()
    try:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM mail_eval_state WHERE npc_id = ?", (npc_id,)
            ).fetchone()
            return dict(row) if row else None
    except Exception as e:
        print(f"[OwlPostDB] Error getting eval state: {e}")
        return None


def save_eval_state(npc_id, last_entry_id, result):
    """Save evaluation result for an NPC. Upserts."""
    _ensure_initialized()
    import time
    try:
        with get_connection() as conn:
            conn.execute("""
                INSERT INTO mail_eval_state (npc_id, last_eval_entry_id, last_eval_result, last_eval_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(npc_id) DO UPDATE SET
                    last_eval_entry_id = excluded.last_eval_entry_id,
                    last_eval_result = excluded.last_eval_result,
                    last_eval_at = excluded.last_eval_at
            """, (npc_id, last_entry_id, result, time.time()))
            conn.commit()
    except Exception as e:
        print(f"[OwlPostDB] Error saving eval state: {e}")


def get_all_eval_states():
    """Get all NPC eval states. Returns dict of {npc_id: state_dict}."""
    _ensure_initialized()
    try:
        with get_connection() as conn:
            rows = conn.execute("SELECT * FROM mail_eval_state").fetchall()
            return {row["npc_id"]: dict(row) for row in rows}
    except Exception as e:
        print(f"[OwlPostDB] Error getting all eval states: {e}")
        return {}


# ============================================
# Activity Log
# ============================================

_OWL_LOG_MAX_ROWS = 200


def log_owl_event(event: str, npc_id: str = None, detail: str = None):
    """Append an entry to the owl post activity log (ring buffer, max rows)."""
    import time
    _ensure_initialized()
    try:
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO owl_post_log (timestamp, event, npc_id, detail) VALUES (?, ?, ?, ?)",
                (time.time(), event, npc_id, detail),
            )
            # Prune oldest rows beyond cap
            conn.execute("""
                DELETE FROM owl_post_log WHERE id NOT IN (
                    SELECT id FROM owl_post_log ORDER BY id DESC LIMIT ?
                )
            """, (_OWL_LOG_MAX_ROWS,))
            conn.commit()
    except Exception as e:
        print(f"[OwlPostDB] Error writing log: {e}")


def get_owl_log(limit: int = _OWL_LOG_MAX_ROWS) -> list:
    """Return the most recent owl post log entries, newest first."""
    _ensure_initialized()
    try:
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM owl_post_log ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[OwlPostDB] Error reading log: {e}")
        return []


def clear_owl_log():
    """Delete all owl post log entries."""
    _ensure_initialized()
    try:
        with get_connection() as conn:
            conn.execute("DELETE FROM owl_post_log")
            conn.commit()
    except Exception as e:
        print(f"[OwlPostDB] Error clearing log: {e}")


def clear_all_board_posts():
    """Delete all board posts. Board definitions and unlocks are preserved."""
    _ensure_initialized()
    try:
        with get_connection() as conn:
            conn.execute("DELETE FROM board_posts")
            conn.commit()
        print("[OwlPostDB] All board posts cleared")
    except Exception as e:
        print(f"[OwlPostDB] Error clearing board posts: {e}")


def clear_all_mail():
    """Delete all owl mail and mail generation state. Boards are preserved."""
    _ensure_initialized()
    try:
        with get_connection() as conn:
            rows = conn.execute("SELECT * FROM mail").fetchall()
            conn.execute("DELETE FROM mail")
            conn.execute("DELETE FROM mail_eval_state")
            conn.commit()
        print(f"[OwlPostDB] All owl mail cleared ({len(rows)} messages)")
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[OwlPostDB] Error clearing owl mail: {e}")
        return []


try:
    from . import player_context
    player_context.register("owl_post_db", close_all, reinit)
except ImportError:
    pass
