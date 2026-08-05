"""
World-scoped cache of the game scheduler DB, filled by chunked
schedule_dump socket messages from Lua.
"""

import os
import sqlite3
import threading
from contextlib import contextmanager

from .settings import DATA_DIR

DB_PATH = os.path.join(DATA_DIR, "schedule_cache.db")

SCHEMA_VERSION = 1

_local = threading.local()
_state_lock = threading.RLock()
_current_dump_id = None

_ACTIVITY_COLS = [
    "ActivityID", "ActivityTypeID", "StartTime", "EndTime", "LocationID",
    "ActivityRecurrenceTypeID", "Sunday", "Monday", "Tuesday", "Wednesday",
    "Thursday", "Friday", "Saturday",
]
_ACTIVITY_INT = {
    "StartTime", "EndTime", "Sunday", "Monday", "Tuesday", "Wednesday",
    "Thursday", "Friday", "Saturday",
}
_LOCATION_COLS = [
    "LocationID", "TypeID", "XPos", "YPos", "ZPos", "WorldID",
    "ParentLocationID", "VolumeOriginX", "VolumeOriginY", "VolumeOriginZ",
    "VolumeExtentX", "VolumeExtentY", "VolumeExtentZ",
]
_LOCATION_REAL = {
    "XPos", "YPos", "ZPos", "VolumeOriginX", "VolumeOriginY", "VolumeOriginZ",
    "VolumeExtentX", "VolumeExtentY", "VolumeExtentZ",
}
_ENTRY_COLS = [
    "source_table", "CharacterID", "ActivityID", "Priority", "OverrideLocationID",
    "OverrideStartTime", "OverrideEndTime", "EntryTypeID", "ScheduleKeys",
]
_ENTRY_INT = {"Priority", "OverrideStartTime", "OverrideEndTime"}


def _create_connection():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30.0, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def get_connection():
    if getattr(_local, "conn", None) is None or getattr(_local, "path", None) != DB_PATH:
        close_thread_local_connection()
        _local.conn = _create_connection()
        _local.path = DB_PATH
        _init_schema(_local.conn)
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


def reset_for_tests():
    global _current_dump_id
    with _state_lock:
        _current_dump_id = None


def _init_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS activities (
        ActivityID TEXT PRIMARY KEY, ActivityTypeID TEXT, StartTime INTEGER,
        EndTime INTEGER, LocationID TEXT, ActivityRecurrenceTypeID TEXT,
        Sunday INTEGER, Monday INTEGER, Tuesday INTEGER, Wednesday INTEGER,
        Thursday INTEGER, Friday INTEGER, Saturday INTEGER)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS locations (
        LocationID TEXT PRIMARY KEY, TypeID TEXT, XPos REAL, YPos REAL, ZPos REAL,
        WorldID TEXT, ParentLocationID TEXT,
        VolumeOriginX REAL, VolumeOriginY REAL, VolumeOriginZ REAL,
        VolumeExtentX REAL, VolumeExtentY REAL, VolumeExtentZ REAL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS schedule_entries (
        id INTEGER PRIMARY KEY AUTOINCREMENT, source_table TEXT, CharacterID TEXT,
        ActivityID TEXT, Priority INTEGER, OverrideLocationID TEXT,
        OverrideStartTime INTEGER, OverrideEndTime INTEGER,
        EntryTypeID TEXT, ScheduleKeys TEXT)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_entries_char ON schedule_entries(CharacterID)")
    conn.execute("CREATE TABLE IF NOT EXISTS dump_meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("""CREATE TABLE IF NOT EXISTS staging_activities (
        dump_id TEXT NOT NULL, ActivityID TEXT, ActivityTypeID TEXT, StartTime INTEGER,
        EndTime INTEGER, LocationID TEXT, ActivityRecurrenceTypeID TEXT,
        Sunday INTEGER, Monday INTEGER, Tuesday INTEGER, Wednesday INTEGER,
        Thursday INTEGER, Friday INTEGER, Saturday INTEGER)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS staging_locations (
        dump_id TEXT NOT NULL, LocationID TEXT, TypeID TEXT, XPos REAL, YPos REAL, ZPos REAL,
        WorldID TEXT, ParentLocationID TEXT,
        VolumeOriginX REAL, VolumeOriginY REAL, VolumeOriginZ REAL,
        VolumeExtentX REAL, VolumeExtentY REAL, VolumeExtentZ REAL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS staging_schedule_entries (
        dump_id TEXT NOT NULL, source_table TEXT, CharacterID TEXT,
        ActivityID TEXT, Priority INTEGER, OverrideLocationID TEXT,
        OverrideStartTime INTEGER, OverrideEndTime INTEGER,
        EntryTypeID TEXT, ScheduleKeys TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS dump_chunks (
        dump_id TEXT NOT NULL, table_name TEXT NOT NULL, kind TEXT NOT NULL,
        chunk INTEGER NOT NULL, total_chunks INTEGER NOT NULL,
        PRIMARY KEY (dump_id, table_name, kind, chunk))""")
    conn.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)
    conn.commit()


def _coerce(row, col, int_cols, real_cols=frozenset()):
    val = row.get(col)
    if val is None or val == "":
        return None
    if col in int_cols:
        try:
            return int(float(val))
        except (TypeError, ValueError):
            return None
    if col in real_cols:
        try:
            return float(val)
        except (TypeError, ValueError):
            return None
    return str(val)


def _begin_dump_if_new(conn, dump_id):
    global _current_dump_id
    with _state_lock:
        if dump_id == _current_dump_id:
            return
        _current_dump_id = dump_id
    conn.execute("DELETE FROM staging_activities")
    conn.execute("DELETE FROM staging_locations")
    conn.execute("DELETE FROM staging_schedule_entries")
    conn.execute("DELETE FROM dump_chunks")
    conn.execute("INSERT OR REPLACE INTO dump_meta(key, value) VALUES ('pending_dump_id', ?)",
                 (dump_id,))
    conn.commit()
    print(f"[ScheduleCache] New dump started: {dump_id}")


def _chunk_already_ingested(conn, dump_id, table, kind, chunk):
    row = conn.execute(
        """SELECT 1 FROM dump_chunks
           WHERE dump_id=? AND table_name=? AND kind=? AND chunk=?""",
        (dump_id, table, kind, chunk),
    ).fetchone()
    return row is not None


def _record_chunk(conn, dump_id, table, kind, chunk, total_chunks):
    conn.execute(
        """INSERT INTO dump_chunks
           (dump_id, table_name, kind, chunk, total_chunks) VALUES (?,?,?,?,?)""",
        (dump_id, table, kind, chunk, total_chunks),
    )


def _missing_chunks(conn, dump_id):
    rows = conn.execute(
        """SELECT table_name, kind, COUNT(*) AS received, MAX(total_chunks) AS expected
           FROM dump_chunks WHERE dump_id=? AND kind!='done'
           GROUP BY table_name, kind HAVING received != expected""",
        (dump_id,),
    ).fetchall()
    missing = [dict(row) for row in rows]
    received = conn.execute(
        "SELECT COUNT(*) FROM dump_chunks WHERE dump_id=? AND kind!='done'",
        (dump_id,),
    ).fetchone()[0]
    if received == 0:
        missing.append({"reason": "dump contained no data chunks"})
    return missing


def _discard_staging(conn, dump_id, reason):
    conn.execute("DELETE FROM staging_activities WHERE dump_id=?", (dump_id,))
    conn.execute("DELETE FROM staging_locations WHERE dump_id=?", (dump_id,))
    conn.execute("DELETE FROM staging_schedule_entries WHERE dump_id=?", (dump_id,))
    conn.execute("DELETE FROM dump_chunks WHERE dump_id=?", (dump_id,))
    conn.execute("INSERT OR REPLACE INTO dump_meta(key, value) VALUES ('failed_dump_id', ?)",
                 (dump_id,))
    conn.execute("INSERT OR REPLACE INTO dump_meta(key, value) VALUES ('failure_reason', ?)",
                 (reason,))
    conn.execute("DELETE FROM dump_meta WHERE key='pending_dump_id'")
    conn.commit()


def _publish_staging(conn, dump_id):
    activity_cols = ",".join(_ACTIVITY_COLS)
    location_cols = ",".join(_LOCATION_COLS)
    entry_cols = ",".join(_ENTRY_COLS)
    conn.execute("DELETE FROM activities")
    conn.execute("DELETE FROM locations")
    conn.execute("DELETE FROM schedule_entries")
    conn.execute(
        f"INSERT INTO activities ({activity_cols}) "
        f"SELECT {activity_cols} FROM staging_activities WHERE dump_id=?",
        (dump_id,),
    )
    conn.execute(
        f"INSERT INTO locations ({location_cols}) "
        f"SELECT {location_cols} FROM staging_locations WHERE dump_id=?",
        (dump_id,),
    )
    conn.execute(
        f"INSERT INTO schedule_entries ({entry_cols}) "
        f"SELECT {entry_cols} FROM staging_schedule_entries WHERE dump_id=?",
        (dump_id,),
    )
    conn.execute("DELETE FROM staging_activities WHERE dump_id=?", (dump_id,))
    conn.execute("DELETE FROM staging_locations WHERE dump_id=?", (dump_id,))
    conn.execute("DELETE FROM staging_schedule_entries WHERE dump_id=?", (dump_id,))
    conn.execute("DELETE FROM dump_chunks WHERE dump_id=?", (dump_id,))
    conn.execute("INSERT OR REPLACE INTO dump_meta(key, value) VALUES ('dump_id', ?)", (dump_id,))
    conn.execute("INSERT OR REPLACE INTO dump_meta(key, value) VALUES ('completed', ?)", (dump_id,))
    conn.execute("DELETE FROM dump_meta WHERE key IN ('pending_dump_id', 'failed_dump_id', 'failure_reason')")
    conn.commit()


def ingest_chunk(msg):
    """Handle one schedule_dump socket message. Returns rows stored."""
    kind = msg.get("kind")
    rows = msg.get("rows") or []
    dump_id = str(msg.get("dump_id") or "unknown")
    table = msg.get("table") or ""
    chunk = int(msg.get("chunk") or 1)
    total_chunks = max(1, int(msg.get("total_chunks") or 1))
    with _state_lock:
        with get_connection() as conn:
            _begin_dump_if_new(conn, dump_id)
            if _chunk_already_ingested(conn, dump_id, table, kind, chunk):
                return 0
            if kind == "activity":
                cols = ",".join(["dump_id"] + _ACTIVITY_COLS)
                marks = ",".join("?" * (len(_ACTIVITY_COLS) + 1))
                for row in rows:
                    vals = [dump_id] + [_coerce(row, col, _ACTIVITY_INT)
                                        for col in _ACTIVITY_COLS]
                    conn.execute(f"INSERT INTO staging_activities ({cols}) VALUES ({marks})", vals)
            elif kind == "location":
                cols = ",".join(["dump_id"] + _LOCATION_COLS)
                marks = ",".join("?" * (len(_LOCATION_COLS) + 1))
                for row in rows:
                    vals = [dump_id] + [_coerce(row, col, set(), _LOCATION_REAL)
                                        for col in _LOCATION_COLS]
                    conn.execute(f"INSERT INTO staging_locations ({cols}) VALUES ({marks})", vals)
            elif kind == "schedule_entries":
                cols = ",".join(["dump_id"] + _ENTRY_COLS)
                marks = ",".join("?" * (len(_ENTRY_COLS) + 1))
                for row in rows:
                    row = dict(row)
                    row["source_table"] = table
                    vals = [dump_id] + [_coerce(row, col, _ENTRY_INT) for col in _ENTRY_COLS]
                    conn.execute(
                        f"INSERT INTO staging_schedule_entries ({cols}) VALUES ({marks})", vals)
            elif kind == "done":
                missing = _missing_chunks(conn, dump_id)
                if msg.get("success") is False or missing:
                    reason = str(msg.get("errors") or missing or "dump reported failure")
                    _discard_staging(conn, dump_id, reason)
                    print(f"[ScheduleCache] Dump {dump_id} rejected: {reason}")
                    return 0
                _publish_staging(conn, dump_id)
                print(f"[ScheduleCache] Dump {dump_id} complete: {dump_summary()}")
                return 0
            else:
                print(f"[ScheduleCache] Unknown chunk kind: {kind}")
                return 0
            _record_chunk(conn, dump_id, table, kind, chunk, total_chunks)
            conn.commit()
    return len(rows)


def dump_summary():
    with get_connection() as conn:
        activities = conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0]
        locations = conn.execute("SELECT COUNT(*) FROM locations").fetchone()[0]
        entries = conn.execute("SELECT COUNT(*) FROM schedule_entries").fetchone()[0]
    return {"activities": activities, "locations": locations, "schedule_entries": entries}


def is_dump_complete():
    with get_connection() as conn:
        done = conn.execute("SELECT value FROM dump_meta WHERE key='completed'").fetchone()
        current = conn.execute("SELECT value FROM dump_meta WHERE key='dump_id'").fetchone()
    return bool(done and current and done[0] == current[0])


def get_completed_dump_id():
    with get_connection() as conn:
        row = conn.execute("SELECT value FROM dump_meta WHERE key='completed'").fetchone()
    return row[0] if row else None


def get_activity(activity_id):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM activities WHERE ActivityID = ?", (activity_id,)).fetchone()
    return dict(row) if row else None


def get_location(location_id):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM locations WHERE LocationID = ?", (location_id,)).fetchone()
    return dict(row) if row else None


def iter_locations():
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM locations").fetchall()
    for row in rows:
        yield dict(row)


def get_entries_for_character(character_id):
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM schedule_entries WHERE CharacterID = ?",
            (character_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def all_character_ids():
    with get_connection() as conn:
        rows = conn.execute("SELECT DISTINCT CharacterID FROM schedule_entries").fetchall()
    return {row[0] for row in rows if row[0]}
