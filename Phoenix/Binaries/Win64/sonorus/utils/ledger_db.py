"""
Player-scoped ledger database.
Schema v1: presence_intervals.
"""

import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime

from . import player_context
from .settings import DATA_DIR

SCHEMA_VERSION = 1
VALID_SOURCES = ("flesh", "projected")

_local = threading.local()
_data_dir_override = None


def reset_for_tests(data_dir=None):
    global _data_dir_override
    close_thread_local_connection()
    _data_dir_override = data_dir


def _get_data_dir():
    if _data_dir_override:
        return _data_dir_override
    data_dir = player_context.get_player_data_dir()
    if data_dir:
        return data_dir
    return DATA_DIR


def _db_path():
    return os.path.join(_get_data_dir(), "ledger.db")


def _create_connection(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30.0, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row
    _init_schema(conn)
    return conn


def _init_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS presence_intervals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        npc_id TEXT NOT NULL,
        game_minutes_start INTEGER NOT NULL,
        game_minutes_end INTEGER,
        location_id TEXT,
        location_name TEXT,
        source TEXT NOT NULL CHECK (source IN ('flesh', 'projected')),
        near_player INTEGER NOT NULL DEFAULT 0,
        eyeshot INTEGER NOT NULL DEFAULT 0,
        last_x REAL, last_y REAL, last_z REAL,
        created_at TEXT NOT NULL)""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_presence_npc_start
                    ON presence_intervals(npc_id, game_minutes_start)""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_presence_open
                    ON presence_intervals(game_minutes_end) WHERE game_minutes_end IS NULL""")
    conn.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)
    conn.commit()


@contextmanager
def get_connection():
    path = _db_path()
    if getattr(_local, "conn", None) is None or getattr(_local, "path", None) != path:
        close_thread_local_connection()
        _local.conn = _create_connection(path)
        _local.path = path
    try:
        yield _local.conn
    except sqlite3.Error:
        close_thread_local_connection()
        raise


def close_thread_local_connection():
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
    _local.conn = None
    _local.path = None


def close_all():
    close_thread_local_connection()


def reinit(_new_data_dir=None):
    close_thread_local_connection()


def open_interval(npc_id, game_minutes_start, source, location_id=None, location_name=None,
                  near_player=False, eyeshot=False, x=None, y=None, z=None):
    if source not in VALID_SOURCES:
        raise ValueError(f"invalid source: {source}")
    start = int(game_minutes_start)
    with get_connection() as conn:
        # A reconnect or duplicate enter must not leave overlapping open rows.
        conn.execute(
            """UPDATE presence_intervals
               SET game_minutes_end = MAX(game_minutes_start, ?)
               WHERE npc_id=? AND source=? AND game_minutes_end IS NULL""",
            (start, npc_id, source),
        )
        cursor = conn.execute(
            """INSERT INTO presence_intervals
               (npc_id, game_minutes_start, location_id, location_name, source,
                near_player, eyeshot, last_x, last_y, last_z, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (npc_id, start, location_id, location_name, source,
             1 if near_player else 0, 1 if eyeshot else 0, x, y, z,
             datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()
        return cursor.lastrowid


def close_interval(interval_id, game_minutes_end):
    with get_connection() as conn:
        conn.execute(
            """UPDATE presence_intervals
               SET game_minutes_end = MAX(game_minutes_start, ?)
               WHERE id = ? AND game_minutes_end IS NULL""",
            (int(game_minutes_end), interval_id),
        )
        conn.commit()


def update_interval_position(interval_id, x, y, z):
    with get_connection() as conn:
        conn.execute(
            "UPDATE presence_intervals SET last_x=?, last_y=?, last_z=? WHERE id=?",
            (x, y, z, interval_id),
        )
        conn.commit()


def get_open_interval(npc_id, source="flesh"):
    with get_connection() as conn:
        row = conn.execute(
            """SELECT * FROM presence_intervals
               WHERE npc_id=? AND source=? AND game_minutes_end IS NULL
               ORDER BY id DESC LIMIT 1""",
            (npc_id, source),
        ).fetchone()
    return dict(row) if row else None


def close_all_open(game_minutes_end):
    with get_connection() as conn:
        cursor = conn.execute(
            """UPDATE presence_intervals
               SET game_minutes_end=MAX(game_minutes_start, ?)
               WHERE game_minutes_end IS NULL""",
            (int(game_minutes_end),),
        )
        conn.commit()
        return cursor.rowcount


def get_intervals(npc_id, t0_minutes, t1_minutes):
    """Intervals overlapping half-open range [t0, t1)."""
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT * FROM presence_intervals
               WHERE npc_id = ?
                 AND game_minutes_start < ?
                 AND (game_minutes_end IS NULL OR game_minutes_end > ?)
               ORDER BY game_minutes_start""",
            (npc_id, int(t1_minutes), int(t0_minutes)),
        ).fetchall()
    return [dict(row) for row in rows]


def replace_projected_intervals(npc_id, t0_minutes, t1_minutes, segments):
    """Replace the read-time projection cache overlapping [t0, t1)."""
    created_at = datetime.now().isoformat(timespec="seconds")
    with get_connection() as conn:
        conn.execute(
            """DELETE FROM presence_intervals
               WHERE npc_id=? AND source='projected'
                 AND game_minutes_start < ?
                 AND (game_minutes_end IS NULL OR game_minutes_end > ?)""",
            (npc_id, int(t1_minutes), int(t0_minutes)),
        )
        for segment in segments:
            start = max(int(segment["start_minutes"]), int(t0_minutes))
            end = min(int(segment["end_minutes"]), int(t1_minutes))
            if end <= start:
                continue
            conn.execute(
                """INSERT INTO presence_intervals
                   (npc_id, game_minutes_start, game_minutes_end,
                    location_id, location_name, source,
                    near_player, eyeshot, created_at)
                   VALUES (?,?,?,?,?,'projected',0,0,?)""",
                (npc_id, start, end, segment.get("location_id"),
                 segment.get("location_name"), created_at),
            )
        conn.commit()


player_context.register(name="ledger_db", close_fn=close_all, reinit_fn=reinit)
