"""
SQLite-backed event logging system for Sonorus.
Thread-safe logging of LLM calls, TTS operations, voice cloning, and vision captures.
"""
import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Any, List

from constants import resolve_llm_cost_context
from utils.settings import DATA_DIR


DB_PATH = Path(DATA_DIR) / "system_events.db"
LEGACY_JSON_PATH = Path(DATA_DIR) / "system_events.json"
SCHEMA_VERSION = 1

_local = threading.local()
_init_lock = threading.Lock()
_events_lock = threading.Lock()
_initialized = False


def _generate_event_id() -> str:
    """Generate unique event ID: evt_{timestamp_ms}_{uuid_short}"""
    timestamp_ms = int(time.time() * 1000)
    uuid_short = str(uuid.uuid4())[:8]
    return f"evt_{timestamp_ms}_{uuid_short}"


def _ensure_data_dir() -> None:
    """Ensure the data directory exists."""
    os.makedirs(DATA_DIR, exist_ok=True)


def _create_connection() -> sqlite3.Connection:
    """Create a new SQLite connection with WAL mode enabled."""
    _ensure_data_dir()
    conn = sqlite3.connect(DB_PATH, timeout=30.0, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def get_connection():
    """Get a thread-local SQLite connection."""
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
        if hasattr(_local, 'conn') and _local.conn is not None:
            try:
                _local.conn.close()
            except Exception:
                pass
            _local.conn = None
        raise
    finally:
        if hasattr(_local, 'conn') and _local.conn is not None:
            try:
                _local.conn.rollback()
            except Exception:
                pass


def close_connection() -> None:
    """Close the thread-local connection."""
    if hasattr(_local, 'conn') and _local.conn is not None:
        _local.conn.close()
        _local.conn = None


def _get_schema_version(conn: sqlite3.Connection) -> int:
    """Get the current schema version."""
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


def _set_schema_version(conn: sqlite3.Connection, version: int) -> None:
    """Persist the current schema version."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY
        )
    """)
    conn.execute("DELETE FROM schema_version")
    conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))


def _run_migrations(conn: sqlite3.Connection, from_version: int) -> None:
    """Run all schema migrations up to SCHEMA_VERSION."""
    current = from_version

    while current < SCHEMA_VERSION:
        if current == 0:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS system_events (
                    id TEXT PRIMARY KEY,
                    timestamp REAL NOT NULL,
                    type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT,
                    data_json TEXT NOT NULL,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    total_tokens INTEGER,
                    duration_ms REAL,
                    cost_total REAL,
                    cost_upstream_inference REAL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_system_events_timestamp ON system_events(timestamp DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_system_events_type ON system_events(type)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_system_events_status ON system_events(status)")
            current = 1
        else:
            raise ValueError(f"Unknown schema version {current}")

    _set_schema_version(conn, SCHEMA_VERSION)


def _extract_metrics(data: Dict[str, Any]) -> Dict[str, Any]:
    """Extract query-friendly metrics from the event payload."""
    tokens = data.get("tokens")
    if not isinstance(tokens, dict):
        tokens = {}

    cost = data.get("cost")
    if not isinstance(cost, dict):
        cost = {}

    return {
        "input_tokens": tokens.get("input"),
        "output_tokens": tokens.get("output"),
        "total_tokens": tokens.get("total"),
        "duration_ms": data.get("duration_ms"),
        "cost_total": cost.get("total"),
        "cost_upstream_inference": cost.get("upstream_inference_cost"),
    }


def _insert_event_row(
    conn: sqlite3.Connection,
    event_id: str,
    timestamp: float,
    event_type: str,
    status: str,
    data: Dict[str, Any],
    error: Optional[str],
    or_ignore: bool = False,
) -> None:
    """Insert one event row into the database."""
    metrics = _extract_metrics(data)
    insert_mode = "INSERT OR IGNORE" if or_ignore else "INSERT"
    conn.execute(f"""
        {insert_mode} INTO system_events (
            id, timestamp, type, status, error, data_json,
            input_tokens, output_tokens, total_tokens,
            duration_ms, cost_total, cost_upstream_inference
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        event_id,
        timestamp,
        event_type,
        status,
        error,
        json.dumps(data, ensure_ascii=False),
        metrics["input_tokens"],
        metrics["output_tokens"],
        metrics["total_tokens"],
        metrics["duration_ms"],
        metrics["cost_total"],
        metrics["cost_upstream_inference"],
    ))


def _count_existing_ids(conn: sqlite3.Connection, event_ids: List[str], chunk_size: int = 500) -> int:
    """Count how many of the given IDs exist in the database."""
    total = 0
    for start in range(0, len(event_ids), chunk_size):
        chunk = event_ids[start:start + chunk_size]
        placeholders = ",".join("?" for _ in chunk)
        row = conn.execute(
            f"SELECT COUNT(*) FROM system_events WHERE id IN ({placeholders})",
            chunk,
        ).fetchone()
        total += row[0] if row else 0
    return total


def _maybe_migrate_from_json() -> None:
    """Import legacy JSON events into SQLite, then delete the JSON file."""
    if not LEGACY_JSON_PATH.exists():
        return

    try:
        content = LEGACY_JSON_PATH.read_text(encoding='utf-8').strip()
    except Exception as e:
        print(f"[EventLogger] Failed to read legacy JSON: {e}")
        return

    if not content:
        try:
            LEGACY_JSON_PATH.unlink()
            print("[EventLogger] Removed empty legacy system_events.json")
        except Exception as e:
            print(f"[EventLogger] Failed to remove empty legacy JSON: {e}")
        return

    try:
        raw_events = json.loads(content)
    except Exception as e:
        print(f"[EventLogger] Failed to parse legacy JSON: {e}")
        return

    if not isinstance(raw_events, list):
        print("[EventLogger] Legacy system_events.json is not a list; skipping migration")
        return

    prepared_events = []
    for event in raw_events:
        if not isinstance(event, dict):
            continue

        event_id = str(event.get("id") or _generate_event_id())
        timestamp = float(event.get("timestamp") or time.time())
        event_type = str(event.get("type") or "unknown")
        status = str(event.get("status") or "success")
        data = event.get("data")
        if not isinstance(data, dict):
            data = {}

        prepared_events.append({
            "id": event_id,
            "timestamp": timestamp,
            "type": event_type,
            "status": status,
            "data": data,
            "error": event.get("error"),
        })

    if not prepared_events:
        try:
            LEGACY_JSON_PATH.unlink()
            print("[EventLogger] Removed legacy system_events.json with no importable events")
        except Exception as e:
            print(f"[EventLogger] Failed to remove legacy JSON with no events: {e}")
        return

    unique_ids = list(dict.fromkeys(event["id"] for event in prepared_events))
    print(f"[EventLogger] Importing {len(prepared_events)} legacy events into SQLite...")

    try:
        with _events_lock:
            with get_connection() as conn:
                for event in prepared_events:
                    _insert_event_row(
                        conn,
                        event_id=event["id"],
                        timestamp=event["timestamp"],
                        event_type=event["type"],
                        status=event["status"],
                        data=event["data"],
                        error=event["error"],
                        or_ignore=True,
                    )
                conn.commit()

                imported_count = _count_existing_ids(conn, unique_ids)
                if imported_count != len(unique_ids):
                    raise RuntimeError(
                        f"Legacy import verification failed ({imported_count}/{len(unique_ids)} IDs present)"
                    )
    except Exception as e:
        print(f"[EventLogger] Legacy JSON migration failed: {e}")
        return

    try:
        LEGACY_JSON_PATH.unlink()
        print(f"[EventLogger] Imported legacy events to {DB_PATH.name} and deleted system_events.json")
    except Exception as e:
        print(f"[EventLogger] Imported legacy events but failed to delete JSON: {e}")


def init_db() -> None:
    """Initialize the event log database and import legacy JSON if present."""
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
                    print(f"[EventLogger] Migrating schema from v{current_version} to v{SCHEMA_VERSION}")
                    _run_migrations(conn, current_version)
                    conn.commit()

            _maybe_migrate_from_json()
            _initialized = True
            print(f"[EventLogger] Database initialized (schema v{SCHEMA_VERSION})")
        except Exception as e:
            print(f"[EventLogger] Error initializing database: {e}")
            raise


def _ensure_initialized() -> None:
    """Ensure the event database is initialized before use."""
    if not _initialized:
        init_db()


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    """Convert a sqlite row back to the legacy event JSON shape."""
    try:
        data = json.loads(row["data_json"]) if row["data_json"] else {}
    except Exception:
        data = {}

    return {
        "id": row["id"],
        "timestamp": row["timestamp"],
        "type": row["type"],
        "status": row["status"],
        "data": data,
        "error": row["error"],
    }


def log_event(event_type: str, status: str = "success", data: Optional[Dict[str, Any]] = None, error: Optional[str] = None) -> str:
    """
    Log a system event (LLM, TTS, voice clone, or vision).

    Args:
        event_type: "llm" | "tts" | "voice_clone" | "vision"
        status: "success" | "error" | "warning"
        data: Type-specific event data
        error: Error message if status="error"

    Returns:
        Event ID
    """
    _ensure_initialized()

    event_id = _generate_event_id()
    timestamp = time.time()
    payload_data = data if isinstance(data, dict) else {}

    try:
        with _events_lock:
            with get_connection() as conn:
                _insert_event_row(
                    conn,
                    event_id=event_id,
                    timestamp=timestamp,
                    event_type=event_type,
                    status=status,
                    data=payload_data,
                    error=error,
                )
                conn.commit()
    except Exception as e:
        print(f"[EventLogger] Error logging event: {e}")

    return event_id


def get_recent_events(limit: int = 100) -> List[Dict[str, Any]]:
    """Get most recent events (reverse chronological order)."""
    _ensure_initialized()
    if limit <= 0:
        return []

    try:
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM system_events ORDER BY timestamp DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_dict(row) for row in rows]
    except Exception as e:
        print(f"[EventLogger] Error loading recent events: {e}")
        return []


def clear_events() -> None:
    """Clear all events."""
    _ensure_initialized()
    try:
        with _events_lock:
            with get_connection() as conn:
                conn.execute("DELETE FROM system_events")
                conn.commit()
    except Exception as e:
        print(f"[EventLogger] Error clearing events: {e}")


def _resolve_timeframe_bounds(timeframe: str) -> Dict[str, Any]:
    """Resolve a named timeframe into local timestamp bounds."""
    normalized = str(timeframe or "today").strip().lower()
    now = datetime.now().astimezone()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    if normalized == "yesterday":
        yesterday_start = today_start - timedelta(days=1)
        return {
            "timeframe": "yesterday",
            "label": "Yesterday",
            "start_ts": yesterday_start.timestamp(),
            "end_ts": today_start.timestamp(),
        }

    if normalized == "all":
        return {
            "timeframe": "all",
            "label": "All Time",
            "start_ts": None,
            "end_ts": None,
        }

    return {
        "timeframe": "today",
        "label": "Today",
        "start_ts": today_start.timestamp(),
        "end_ts": None,
    }


def get_cost_breakdown(timeframe: str = "today") -> Dict[str, Any]:
    """Get cost breakdown grouped by feature and module for billable LLM events."""
    _ensure_initialized()

    bounds = _resolve_timeframe_bounds(timeframe)
    query = [
        "SELECT id, timestamp, data_json, cost_total",
        "FROM system_events",
        "WHERE type = ?",
        "AND status IN (?, ?)",
        "AND COALESCE(cost_total, 0) > 0",
    ]
    params: List[Any] = ["llm", "success", "warning"]

    if bounds["start_ts"] is not None:
        query.append("AND timestamp >= ?")
        params.append(bounds["start_ts"])
    if bounds["end_ts"] is not None:
        query.append("AND timestamp < ?")
        params.append(bounds["end_ts"])

    query.append("ORDER BY cost_total DESC, timestamp DESC, id DESC")

    try:
        with get_connection() as conn:
            rows = conn.execute("\n".join(query), params).fetchall()
    except Exception as e:
        print(f"[EventLogger] Error loading cost breakdown: {e}")
        return {
            "timeframe": bounds["timeframe"],
            "timeframe_label": bounds["label"],
            "total_cost": 0.0,
            "call_count": 0,
            "feature_count": 0,
            "features": [],
        }

    feature_groups: Dict[str, Dict[str, Any]] = {}
    total_cost = 0.0
    call_count = 0

    for row in rows:
        cost_total = float(row["cost_total"] or 0.0)
        if cost_total <= 0:
            continue

        try:
            data = json.loads(row["data_json"]) if row["data_json"] else {}
        except Exception:
            data = {}

        context = ""
        if isinstance(data, dict):
            context = str(data.get("context") or "").strip()

        resolved = resolve_llm_cost_context(context)
        feature_slug = resolved["feature_slug"]
        module_slug = resolved["module_slug"]

        feature_entry = feature_groups.setdefault(feature_slug, {
            "feature_slug": feature_slug,
            "feature_label": resolved["feature_label"],
            "total_cost": 0.0,
            "call_count": 0,
            "modules": {},
        })
        feature_entry["total_cost"] += cost_total
        feature_entry["call_count"] += 1

        module_entry = feature_entry["modules"].setdefault(module_slug, {
            "context": module_slug,
            "label": resolved["module_label"],
            "total_cost": 0.0,
            "call_count": 0,
        })
        module_entry["total_cost"] += cost_total
        module_entry["call_count"] += 1

        total_cost += cost_total
        call_count += 1

    features = []
    for feature_entry in feature_groups.values():
        modules = sorted(
            feature_entry["modules"].values(),
            key=lambda item: (-item["total_cost"], item["label"].lower(), item["context"]),
        )
        features.append({
            "feature_slug": feature_entry["feature_slug"],
            "feature_label": feature_entry["feature_label"],
            "total_cost": feature_entry["total_cost"],
            "call_count": feature_entry["call_count"],
            "modules": modules,
        })

    features.sort(key=lambda item: (-item["total_cost"], item["feature_label"].lower(), item["feature_slug"]))

    return {
        "timeframe": bounds["timeframe"],
        "timeframe_label": bounds["label"],
        "total_cost": total_cost,
        "call_count": call_count,
        "feature_count": len(features),
        "features": features,
    }


# ============================================
# Event logging helpers for specific types
# ============================================

def log_llm_event(
    model: str,
    context: str,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    total_tokens: Optional[int] = None,
    reasoning_tokens: Optional[int] = None,
    duration_ms: Optional[float] = None,
    cost_total: Optional[float] = None,
    cost_upstream_inference: Optional[float] = None,
    provider_used: Optional[str] = None,
    response_model: Optional[str] = None,
    status: str = "success",
    error: Optional[str] = None,
    warning: Optional[str] = None,
) -> str:
    """
    Log an LLM call event.

    Args:
        model: Model name (e.g., "google/gemini-3-flash-preview:nitro")
        context: Context of the call ("chat", "target_selection", "interjection", "vision", "sentiment")
        input_tokens: Number of input tokens used
        output_tokens: Number of output tokens generated
        total_tokens: Total tokens used
        duration_ms: Request latency in milliseconds
        cost_total: Total billed cost reported by the provider
        cost_upstream_inference: Upstream provider cost, when reported separately
        status: "success" | "warning" | "error"
        error: Error message if failed

    Returns:
        Event ID
    """
    return log_event(
        event_type="llm",
        status=status,
        data={
            "model": model,
            "response_model": response_model,
            "provider_used": provider_used,
            "context": context,
            "tokens": {
                "input": input_tokens,
                "output": output_tokens,
                "total": total_tokens,
                "reasoning": reasoning_tokens
            },
            "cost": {
                "total": cost_total,
                "upstream_inference_cost": cost_upstream_inference
            },
            "duration_ms": duration_ms,
            "warning": warning
        },
        error=error
    )


def log_tts_event(
    voice_id: str,
    text_excerpt: str,
    audio_bytes: int,
    text_length: Optional[int] = None,
    duration_ms: Optional[float] = None,
    status: str = "success",
    error: Optional[str] = None
) -> str:
    """
    Log a TTS synthesis event.

    Args:
        voice_id: Voice ID or character name
        text_excerpt: First 50-100 chars of text being synthesized
        audio_bytes: Number of audio bytes generated
        text_length: Total character count of text being synthesized
        duration_ms: Request latency in milliseconds
        status: "success" | "error"
        error: Error message if failed

    Returns:
        Event ID
    """
    return log_event(
        event_type="tts",
        status=status,
        data={
            "voice_id": voice_id,
            "text_excerpt": text_excerpt[:100],
            "text_length": text_length,
            "audio_bytes": audio_bytes,
            "duration_ms": duration_ms
        },
        error=error
    )


def log_voice_clone_event(
    character_name: str,
    language: str,
    reference_filename: str,
    voice_id: Optional[str] = None,
    status: str = "success",
    error: Optional[str] = None
) -> str:
    """
    Log a voice cloning event (PRIORITY).

    Args:
        character_name: Character whose voice is being cloned
        language: Language code (e.g., "EN_US")
        reference_filename: Name of the reference audio file used
        voice_id: ID of the created voice (if successful)
        status: "success" | "error"
        error: Error message if failed

    Returns:
        Event ID
    """
    return log_event(
        event_type="voice_clone",
        status=status,
        data={
            "character_name": character_name,
            "language": language,
            "reference_filename": reference_filename,
            "voice_id": voice_id
        },
        error=error
    )


def log_vision_event(
    trigger_reason: str,
    location_name: str,
    scene_description_excerpt: str,
    model: str,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    total_tokens: Optional[int] = None,
    status: str = "success",
    error: Optional[str] = None
) -> str:
    """
    Log a vision capture event (PRIORITY).

    Args:
        trigger_reason: Why capture happened ("distance" | "time_interval")
        location_name: Name of the location captured
        scene_description_excerpt: First 100 chars of vision description
        model: Vision model used
        input_tokens: Vision model input tokens
        output_tokens: Vision model output tokens
        total_tokens: Total tokens used
        status: "success" | "error"
        error: Error message if failed

    Returns:
        Event ID
    """
    return log_event(
        event_type="vision",
        status=status,
        data={
            "trigger_reason": trigger_reason,
            "location_name": location_name,
            "description_excerpt": scene_description_excerpt[:100],
            "model": model,
            "tokens": {
                "input": input_tokens,
                "output": output_tokens,
                "total": total_tokens
            }
        },
        error=error
    )


if __name__ == "__main__":
    print("Testing event_logger...")
    init_db()

    eid1 = log_llm_event(
        model="google/gemini-3-flash-preview:nitro",
        context="chat",
        input_tokens=150,
        output_tokens=42,
        total_tokens=192
    )
    print(f"Logged LLM event: {eid1}")

    eid2 = log_tts_event(
        voice_id="sebastian-voice-123",
        text_excerpt="The Dark Arts are indeed intriguing to most wizards...",
        audio_bytes=8192,
        duration_ms=2500
    )
    print(f"Logged TTS event: {eid2}")

    eid3 = log_voice_clone_event(
        character_name="Sebastian Sallow",
        language="EN_US",
        reference_filename="sebastian_neutral_001.wav",
        voice_id="voice-clone-789"
    )
    print(f"Logged voice clone event: {eid3}")

    recent = get_recent_events(limit=10)
    print(f"\nRecent events: {len(recent)}")
    for evt in recent:
        print(f"  {evt['id']}: {evt['type']} ({evt['status']})")
