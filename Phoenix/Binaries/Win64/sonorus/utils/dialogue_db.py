"""
SQLite-backed dialogue history storage for Sonorus.
Provides atomic writes, proper locking, and efficient queries.
"""

import os
import json
import re
import shutil
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime

from .settings import DATA_DIR
from .llm_utils import LLM_ERROR_FALLBACK


# ============================================
# Game Time Parsing/Formatting Helpers
# ============================================

def _parse_game_time(time_str):
    """Parse gameTime like '1:41 AM' or '7:45 AM' into (hour_24, minute)."""
    if not time_str or str(time_str).strip() == "":
        return None

    match = re.match(r'(\d{1,2}):(\d{2})\s*(AM|PM)', str(time_str).strip(), re.IGNORECASE)
    if not match:
        return None

    hour = int(match.group(1))
    minute = int(match.group(2))
    period = match.group(3).upper()

    # Convert to 24-hour format
    if period == 'AM':
        if hour == 12:
            hour = 0
    else:  # PM
        if hour != 12:
            hour += 12

    return (hour, minute)


def _normalize_history_text(value):
    """Normalize whitespace so fallback checks survive tabs/newlines."""
    return " ".join(str(value or "").split())


def _should_skip_entry(entry):
    """Return True for dialogue entries that should never be persisted."""
    if not isinstance(entry, dict):
        return False

    fallback_text = _normalize_history_text(LLM_ERROR_FALLBACK)
    entry_text = _normalize_history_text(entry.get("text"))
    if not entry_text:
        return False

    if entry_text == fallback_text:
        return True

    for key in ("speaker", "voiceName"):
        prefix = _normalize_history_text(entry.get(key))
        if prefix and entry_text == f"{prefix} {fallback_text}":
            return True

    return False


_MONTH_NAMES_DB = {
    'january': 1, 'february': 2, 'march': 3, 'april': 4,
    'may': 5, 'june': 6, 'july': 7, 'august': 8,
    'september': 9, 'october': 10, 'november': 11, 'december': 12,
}


def _parse_game_date(date_str):
    """Parse gameDate into (year, month, day).

    Supports both formats:
    - Short: '1890/12/08'
    - Long:  'Wednesday, January 14th, 1891'
    """
    if not date_str or str(date_str).strip() == "":
        return None

    s = str(date_str).strip()

    # Try short format first: YYYY/MM/DD
    match = re.match(r'(\d{4})/(\d{1,2})/(\d{1,2})', s)
    if match:
        return (int(match.group(1)), int(match.group(2)), int(match.group(3)))

    # Try long format: "Wednesday, January 14th, 1891"
    match = re.search(r'(\w+)\s+(\d{1,2})\w*,?\s+(\d{4})', s)
    if match:
        month = _MONTH_NAMES_DB.get(match.group(1).lower())
        if month:
            return (int(match.group(3)), month, int(match.group(2)))

    return None


def _game_datetime_to_minutes(date, time):
    """Convert game date/time to total minutes since epoch for interpolation."""
    year, month, day = date
    hour, minute = time
    # Simple calculation: days since year start * 1440 + hour * 60 + minute
    # Using a reference point of 1890/01/01
    days = (year - 1890) * 365 + (month - 1) * 30 + (day - 1)  # Approximate
    return days * 1440 + hour * 60 + minute


def _minutes_to_game_datetime(total_minutes):
    """Convert total minutes back to game date/time."""
    days = total_minutes // 1440
    remaining_minutes = total_minutes % 1440

    hour = remaining_minutes // 60
    minute = remaining_minutes % 60

    # Convert days back to date (approximate)
    year = 1890 + days // 365
    remaining_days = days % 365
    month = 1 + remaining_days // 30
    day = 1 + remaining_days % 30

    # Clamp values
    month = min(max(month, 1), 12)
    day = min(max(day, 1), 31)

    return ((year, month, day), (hour, minute))


def _format_game_time(hour, minute):
    """Format 24-hour time as '7:45 AM' style."""
    period = 'AM' if hour < 12 else 'PM'
    display_hour = hour % 12
    if display_hour == 0:
        display_hour = 12
    return f"{display_hour}:{minute:02d} {period}"


def _format_game_date(year, month, day):
    """Format date as '1890/12/08' style."""
    return f"{year}/{month:02d}/{day:02d}"


def _normalize_commitment_game_times(entries):
    """Normalize legacy commitment gameTime values from 24h 'HH:MM' to 'H:MM AM/PM'.

    Returns a list of (entry_index, normalized_time) for rows that were changed.
    """
    updates = []

    for idx, entry in enumerate(entries):
        if entry.get('type') != 'commitment':
            continue

        game_time = (entry.get('gameTime') or '').strip()
        if not game_time:
            continue

        upper = game_time.upper()
        if 'AM' in upper or 'PM' in upper:
            continue  # Already normalized

        match = re.match(r'^(\d{1,2}):(\d{2})$', game_time)
        if not match:
            continue

        hour = int(match.group(1))
        minute = int(match.group(2))
        if hour < 0 or hour > 23 or minute < 0 or minute > 59:
            continue

        normalized = _format_game_time(hour, minute)
        if normalized != game_time:
            entry['gameTime'] = normalized
            updates.append((idx, normalized))

    return updates


def _deduplicate_timestamps(entries):
    """
    Advance timestamps for entries that share the same timestamp.
    Each duplicate gets +1 second from the previous.
    Returns count of adjusted entries.
    """
    adjusted = 0
    last_timestamp = None

    for entry in entries:
        ts = entry.get('timestamp', 0)
        if ts == 0:
            continue

        if last_timestamp is not None and ts <= last_timestamp:
            # Advance by 1 second past last timestamp
            entry['timestamp'] = last_timestamp + 1
            adjusted += 1
            last_timestamp = entry['timestamp']
        else:
            last_timestamp = ts

    return adjusted


def _backfill_game_times(entries):
    """
    Backfill missing gameTime and gameDate in entries.
    Returns count of backfilled entries.

    Strategy:
    1. Find "anchor" entries that have valid gameTime/gameDate
    2. For entries with empty gameTime, interpolate based on Unix timestamp
       position between the nearest anchors before and after
    """
    # First pass: identify anchors (entries with valid game time)
    anchors = []
    for i, entry in enumerate(entries):
        game_time = _parse_game_time(entry.get('gameTime', ''))
        game_date = _parse_game_date(entry.get('gameDate', ''))

        if game_time is not None:
            # If we have time but no date, try to infer date from nearby
            if game_date is None:
                # Look for nearest entry with a date
                for j in range(1, len(entries)):
                    if i - j >= 0:
                        prev_date = _parse_game_date(entries[i - j].get('gameDate', ''))
                        if prev_date:
                            game_date = prev_date
                            break
                    if i + j < len(entries):
                        next_date = _parse_game_date(entries[i + j].get('gameDate', ''))
                        if next_date:
                            game_date = next_date
                            break

            if game_date is not None:
                anchors.append({
                    'index': i,
                    'timestamp': entry.get('timestamp', 0),
                    'game_time': game_time,
                    'game_date': game_date,
                    'game_minutes': _game_datetime_to_minutes(game_date, game_time)
                })

    if len(anchors) < 1:
        return 0

    # Sort anchors by timestamp
    anchors.sort(key=lambda a: a['timestamp'])

    # Second pass: backfill entries missing time or date
    backfill_count = 0

    for i, entry in enumerate(entries):
        game_time = _parse_game_time(entry.get('gameTime', ''))
        game_date = _parse_game_date(entry.get('gameDate', ''))

        # Skip if already has both valid time and date
        if game_time is not None and game_date is not None:
            continue

        timestamp = entry.get('timestamp', 0)
        if timestamp == 0:
            continue

        # Find anchors before and after this entry
        anchor_before = None
        anchor_after = None

        for anchor in anchors:
            if anchor['timestamp'] <= timestamp:
                anchor_before = anchor
            elif anchor['timestamp'] > timestamp and anchor_after is None:
                anchor_after = anchor
                break

        # Interpolate or use nearest anchor
        if anchor_before and anchor_after:
            ts_range = anchor_after['timestamp'] - anchor_before['timestamp']
            if ts_range > 0:
                ratio = (timestamp - anchor_before['timestamp']) / ts_range
                game_minutes_range = anchor_after['game_minutes'] - anchor_before['game_minutes']
                interpolated_minutes = int(anchor_before['game_minutes'] + ratio * game_minutes_range)
                new_date, new_time = _minutes_to_game_datetime(interpolated_minutes)
            else:
                new_date = anchor_before['game_date']
                new_time = anchor_before['game_time']
        elif anchor_before:
            ts_delta = timestamp - anchor_before['timestamp']
            game_minutes_delta = int(ts_delta * 0.5)
            new_minutes = anchor_before['game_minutes'] + game_minutes_delta
            new_date, new_time = _minutes_to_game_datetime(new_minutes)
        elif anchor_after:
            ts_delta = anchor_after['timestamp'] - timestamp
            game_minutes_delta = int(ts_delta * 0.5)
            new_minutes = anchor_after['game_minutes'] - game_minutes_delta
            new_date, new_time = _minutes_to_game_datetime(new_minutes)
        else:
            continue

        # Only fill what's missing — don't overwrite existing valid data
        if game_time is None:
            entry['gameTime'] = _format_game_time(new_time[0], new_time[1])
        if game_date is None:
            entry['gameDate'] = _format_game_date(new_date[0], new_date[1], new_date[2])
        backfill_count += 1

    return backfill_count


def _merge_duplicate_npcs(entries):
    """
    Merge NPCs stored under variant names (e.g., "Nellie Oggspire" and "NellieOggspire")
    into the canonical no-space form.

    Strategy:
    - Group entries by normalized name (spaces removed)
    - For groups with multiple variants, prefer the no-space version as canonical
    - If no-space version doesn't exist, use the variant with most entries
    - Update voiceName and earshot arrays

    Returns count of entries updated.
    """
    from collections import defaultdict

    # Build mapping: normalized_name -> {variant_name: count}
    name_variants = defaultdict(lambda: defaultdict(int))

    for entry in entries:
        voice_name = entry.get('voiceName')
        if not voice_name:
            continue
        normalized = voice_name.replace(' ', '')
        name_variants[normalized][voice_name] += 1

    # Find groups with multiple variants and build merge map
    merge_map = {}  # old_name -> canonical_name
    for normalized, variants in name_variants.items():
        if len(variants) <= 1:
            continue

        # Pick canonical: prefer no-space version, else most entries
        if normalized in variants:
            canonical = normalized
        else:
            canonical = max(variants.keys(), key=lambda v: variants[v])

        for variant in variants:
            if variant != canonical:
                merge_map[variant] = canonical

    if not merge_map:
        return 0

    # Apply merges
    merged = 0
    for entry in entries:
        # Update voiceName
        voice_name = entry.get('voiceName')
        if voice_name in merge_map:
            entry['voiceName'] = merge_map[voice_name]
            merged += 1

        # Update earshot array
        earshot = entry.get('earshot', [])
        if earshot and isinstance(earshot, list):
            entry['earshot'] = [merge_map.get(npc, npc) for npc in earshot]

    return merged


DB_PATH = os.path.join(DATA_DIR, "dialogue_history.db")
JSON_PATH = os.path.join(DATA_DIR, "dialogue_history.json")


def _backup_db(reason="backup"):
    """Create a timestamped backup of the DB before destructive operations.

    Returns backup path on success, None if DB doesn't exist or backup fails.
    """
    if not os.path.exists(DB_PATH):
        return None
    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = f"{DB_PATH}.{ts}.{reason}.bak"
        shutil.copy2(DB_PATH, backup_path)
        print(f"[DialogueDB] Backup created: {backup_path}")
        return backup_path
    except Exception as e:
        print(f"[DialogueDB] Backup failed: {e}")
        return None


# Thread-local storage for connections
_local = threading.local()

# Track all connections for shutdown cleanup
_all_connections = []
_all_connections_lock = threading.Lock()

# Lock for initialization to prevent race conditions during migration
_init_lock = threading.Lock()
_initialized = False

# Current schema version - increment when making schema changes
SCHEMA_VERSION = 3


def _ensure_data_dir():
    """Ensure the data directory exists."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)


def _create_connection():
    """Create a new database connection with proper settings."""
    _ensure_data_dir()
    conn = sqlite3.connect(DB_PATH, timeout=30.0, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")  # 30 second timeout for locks
    conn.row_factory = sqlite3.Row
    with _all_connections_lock:
        _all_connections.append(conn)
    return conn


@contextmanager
def get_connection():
    """
    Get thread-local SQLite connection with WAL mode.
    Each thread gets its own connection to avoid threading issues.
    Handles stale/closed connections gracefully.
    """
    try:
        # Check if we have a valid connection
        if hasattr(_local, 'conn') and _local.conn is not None:
            try:
                # Test if connection is still valid
                _local.conn.execute("SELECT 1")
            except (sqlite3.ProgrammingError, sqlite3.OperationalError):
                # Connection is stale, recreate it
                try:
                    _local.conn.close()
                except:
                    pass
                _local.conn = None

        if not hasattr(_local, 'conn') or _local.conn is None:
            _local.conn = _create_connection()

        yield _local.conn
    except sqlite3.Error as e:
        # On error, invalidate connection so next call creates fresh one
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


def close_connection():
    """Close thread-local connection (call on thread shutdown if needed)."""
    if hasattr(_local, 'conn') and _local.conn:
        with _all_connections_lock:
            try:
                _all_connections.remove(_local.conn)
            except ValueError:
                pass
        try:
            _local.conn.close()
        except Exception:
            pass
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
    DB_PATH = os.path.join(data_dir, "dialogue_history.db")
    init_db()


def _get_schema_version(conn):
    """Get current schema version from database."""
    try:
        # Check if schema_version table exists
        result = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
        ).fetchone()
        if not result:
            return 0  # No version table = version 0 (initial)

        row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
        return row[0] if row else 0
    except:
        return 0


def _set_schema_version(conn, version):
    """Set schema version in database."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY
        )
    """)
    conn.execute("DELETE FROM schema_version")
    conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))


def _run_migrations(conn, from_version):
    """
    Run all migrations from from_version to SCHEMA_VERSION.

    To add a new migration:
    1. Increment SCHEMA_VERSION at top of file
    2. Add a new elif block here for the new version
    3. The migration should be idempotent (safe to run multiple times)

    Example migration (adding a new column):
        elif current == 2:
            conn.execute("ALTER TABLE dialogue_entries ADD COLUMN new_field TEXT")
            current = 3
    """
    current = from_version

    while current < SCHEMA_VERSION:
        if current == 0:
            # Version 0 -> 1: Initial schema creation
            conn.execute("""
                CREATE TABLE IF NOT EXISTS dialogue_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp INTEGER NOT NULL,
                    game_time TEXT,
                    game_date TEXT,
                    speaker TEXT,
                    voice_name TEXT,
                    target TEXT,
                    target_id TEXT,
                    text TEXT,
                    is_player INTEGER DEFAULT 0,
                    is_ai_response INTEGER DEFAULT 0,
                    entry_type TEXT DEFAULT 'dialogue',
                    earshot TEXT,
                    line_id TEXT,
                    location TEXT,
                    duration REAL,
                    spell_category TEXT,
                    count INTEGER DEFAULT 1,
                    first_game_time TEXT,
                    first_game_date TEXT,
                    first_timestamp INTEGER,
                    last_game_time TEXT,
                    last_game_date TEXT,
                    last_timestamp INTEGER,
                    companions TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_voice_name ON dialogue_entries(voice_name)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_target_id ON dialogue_entries(target_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON dialogue_entries(timestamp)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_type ON dialogue_entries(entry_type)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_is_player ON dialogue_entries(is_player)")
            current = 1

        elif current == 1:
            try:
                conn.execute("ALTER TABLE dialogue_entries ADD COLUMN companions TEXT")
            except sqlite3.OperationalError as e:
                # SQLite raises an error if the column already exists
                if "duplicate column name" not in str(e).lower():
                    raise
            current = 2

        elif current == 2:
            try:
                conn.execute("ALTER TABLE dialogue_entries ADD COLUMN target_id TEXT")
            except sqlite3.OperationalError as e:
                if "duplicate column name" not in str(e).lower():
                    raise

            conn.execute("CREATE INDEX IF NOT EXISTS idx_target_id ON dialogue_entries(target_id)")
            _backfill_target_ids(conn)
            current = 3

        else:
            # Unknown version - can't migrate
            raise ValueError(f"Unknown schema version {current}, cannot migrate to {SCHEMA_VERSION}")

    _set_schema_version(conn, SCHEMA_VERSION)
    return from_version != SCHEMA_VERSION  # Return True if migrations ran


def init_db():
    """
    Initialize database schema if needed.
    Runs schema migrations for upgrades.
    Also runs migration from JSON if dialogue_history.json exists.
    Thread-safe - uses lock to prevent race conditions during migration.
    """
    global _initialized

    # Fast path: already initialized
    if _initialized:
        return

    with _init_lock:
        # Double-check after acquiring lock
        if _initialized:
            return

        try:
            with get_connection() as conn:
                current_version = _get_schema_version(conn)
                schema_changed = False

                if current_version < SCHEMA_VERSION:
                    _backup_db("schema_migration")
                    print(f"[DialogueDB] Migrating schema from v{current_version} to v{SCHEMA_VERSION}")
                    schema_changed = _run_migrations(conn, current_version)
                    print(f"[DialogueDB] Schema migration complete")

                target_repairs = _backfill_target_ids(conn)
                if schema_changed or target_repairs:
                    conn.commit()

            # Auto-migrate from JSON if it exists and DB is empty
            _maybe_migrate_from_json()

            _initialized = True
            print(f"[DialogueDB] Database initialized (schema v{SCHEMA_VERSION})")
        except Exception as e:
            print(f"[DialogueDB] Error initializing database: {e}")
            raise


def _maybe_migrate_from_json():
    """Migrate from JSON to SQLite if JSON exists and DB is empty.

    Data repairs performed during migration:
    1. Deduplicate timestamps (entries with same timestamp get +1 second each)
    2. Backfill missing gameTime/gameDate by interpolating from nearby anchors
    3. Merge duplicate NPC name variants (e.g., "Nellie Oggspire" -> "NellieOggspire")
    4. Filter out insignificant NPCs (generic townspeople, etc.)
    5. Clean earshot arrays to remove insignificant NPCs
    """
    if not os.path.exists(JSON_PATH):
        return

    with get_connection() as conn:
        count = conn.execute("SELECT COUNT(*) FROM dialogue_entries").fetchone()[0]
        if count > 0:
            return  # DB already has data, don't migrate

    # Load JSON
    try:
        with open(JSON_PATH, 'r', encoding='utf-8') as f:
            history = json.load(f)
    except Exception as e:
        print(f"[DialogueDB] Failed to load JSON for migration: {e}")
        return

    if not history:
        return

    print(f"[DialogueDB] Starting migration of {len(history)} entries from JSON...")

    # Step 1: Deduplicate timestamps (so interpolation works correctly)
    dedup_count = _deduplicate_timestamps(history)
    if dedup_count > 0:
        print(f"[DialogueDB] Deduplicated {dedup_count} timestamps")

    # Step 2: Backfill missing game times
    backfill_count = _backfill_game_times(history)
    if backfill_count > 0:
        print(f"[DialogueDB] Backfilled {backfill_count} missing gameTime/gameDate entries")

    # Step 3: Merge duplicate NPC name variants (e.g., "Nellie Oggspire" -> "NellieOggspire")
    merge_count = _merge_duplicate_npcs(history)
    if merge_count > 0:
        print(f"[DialogueDB] Merged {merge_count} entries with duplicate NPC name variants")

    # Import here to avoid circular imports
    from .text_utils import is_significant_npc

    # Step 4 & 5: Insert entries, filtering out insignificant NPCs
    migrated = 0
    skipped = 0
    fallback_skipped = 0
    with get_connection() as conn:
        for entry in history:
            if not isinstance(entry, dict):
                continue

            voice_name = entry.get('voiceName')
            is_player = entry.get('isPlayer', False)
            is_ai = entry.get('isAIResponse', False)

            # Skip entries from insignificant speakers (unless player/AI)
            if not is_player and not is_ai and voice_name:
                if not is_significant_npc(voice_name):
                    skipped += 1
                    continue

            # Clean earshot array - remove insignificant NPCs
            earshot = entry.get('earshot', [])
            if earshot and isinstance(earshot, list):
                entry['earshot'] = [npc for npc in earshot if is_significant_npc(npc)]

            if _insert_entry(conn, entry):
                migrated += 1
            else:
                fallback_skipped += 1
        conn.commit()

    # Rename JSON as backup
    backup_path = JSON_PATH + ".migrated"
    try:
        os.rename(JSON_PATH, backup_path)
        print(
            f"[DialogueDB] Migrated {migrated} entries from JSON "
            f"({skipped} insignificant skipped, {fallback_skipped} fallback skipped). "
            f"Backup: {backup_path}"
        )
    except Exception as e:
        print(f"[DialogueDB] Migration complete but failed to rename JSON: {e}")


def _insert_entry(conn, entry):
    """Insert a single entry dict into the database."""
    prepared = _prepare_entry_db_fields(entry)
    if prepared is None:
        return False

    cursor = conn.execute("""
        INSERT INTO dialogue_entries (
            timestamp, game_time, game_date, speaker, voice_name, target, target_id, text,
            is_player, is_ai_response, entry_type, earshot, line_id, location,
            duration, spell_category, count,
            first_game_time, first_game_date, first_timestamp,
            last_game_time, last_game_date, last_timestamp,
            companions
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        prepared['timestamp'],
        prepared['game_time'],
        prepared['game_date'],
        prepared['speaker'],
        prepared['voice_name'],
        prepared['target'],
        prepared['target_id'],
        prepared['text'],
        prepared['is_player'],
        prepared['is_ai_response'],
        prepared['entry_type'],
        prepared['earshot'],
        prepared['line_id'],
        prepared['location'],
        prepared['duration'],
        prepared['spell_category'],
        prepared['count'],
        prepared['first_game_time'],
        prepared['first_game_date'],
        prepared['first_timestamp'],
        prepared['last_game_time'],
        prepared['last_game_date'],
        prepared['last_timestamp'],
        prepared['companions'],
    ))
    row_id = cursor.lastrowid
    if isinstance(entry, dict):
        entry['targetId'] = prepared['target_id']
        if row_id:
            entry['sourceEntryIds'] = [row_id]
    return row_id or True


def _canonicalize_target_id(target_id):
    """Normalize stored target IDs to the canonical casing used by dialogue history."""
    if target_id is None:
        return None

    from .localization import canonicalize_npc_id
    return canonicalize_npc_id(target_id)


def _derive_target_id(entry):
    """Derive a canonical target_id for storage when an entry does not provide one."""
    if not isinstance(entry, dict):
        return None

    if 'targetId' in entry:
        return _canonicalize_target_id(entry.get('targetId'))

    target = entry.get('target')
    if target is None:
        return None

    target = str(target).strip()
    if not target:
        return None
    if target.lower() == "unknown":
        return "Unknown"

    from .localization import strict_id_from_name

    resolved = strict_id_from_name(target)
    if resolved:
        return _canonicalize_target_id(resolved)

    voice_name = str(entry.get('voiceName') or '').strip()
    if voice_name.lower() == "player":
        return None

    return "Player"


def _backfill_target_ids(conn):
    """Populate target_id for existing rows using strict reverse localization rules."""
    rows = conn.execute("""
        SELECT id, target, voice_name, target_id
        FROM dialogue_entries
        ORDER BY id ASC
    """).fetchall()

    updates = []
    for row in rows:
        current_target_id = row['target_id'] if 'target_id' in row.keys() else None
        entry = {
            'target': row['target'],
            'voiceName': row['voice_name'],
        }
        derived_target_id = _derive_target_id(entry)

        if _canonicalize_target_id(current_target_id) != derived_target_id:
            updates.append((derived_target_id, row['id']))

    if updates:
        conn.executemany(
            "UPDATE dialogue_entries SET target_id = ? WHERE id = ?",
            updates,
        )
        print(f"[DialogueDB] Backfilled target_id for {len(updates)} dialogue rows")
    return len(updates)


def _prepare_entry_db_fields(entry):
    """Normalize an entry dict for INSERT/UPDATE statements."""
    if _should_skip_entry(entry):
        print("[DialogueDB] Skipping LLM fallback entry in dialogue history")
        return None

    earshot = entry.get('earshot')
    if isinstance(earshot, list):
        earshot = json.dumps(earshot)
    elif earshot is None:
        earshot = None

    companions = entry.get('companions')
    if isinstance(companions, list):
        companions = json.dumps(companions)
    elif companions is None:
        companions = None

    return {
        'timestamp': entry.get('timestamp', 0),
        'game_time': entry.get('gameTime'),
        'game_date': entry.get('gameDate'),
        'speaker': entry.get('speaker'),
        'voice_name': entry.get('voiceName'),
        'target': entry.get('target'),
        'target_id': _derive_target_id(entry),
        'text': entry.get('text'),
        'is_player': 1 if entry.get('isPlayer') else 0,
        'is_ai_response': 1 if entry.get('isAIResponse') else 0,
        'entry_type': entry.get('type', 'dialogue'),
        'earshot': earshot,
        'line_id': entry.get('lineID'),
        'location': entry.get('location'),
        'duration': entry.get('duration'),
        'spell_category': entry.get('spellCategory'),
        'count': entry.get('count', 1),
        'first_game_time': entry.get('firstGameTime'),
        'first_game_date': entry.get('firstGameDate'),
        'first_timestamp': entry.get('firstTimestamp'),
        'last_game_time': entry.get('lastGameTime'),
        'last_game_date': entry.get('lastGameDate'),
        'last_timestamp': entry.get('lastTimestamp'),
        'companions': companions,
    }


def _row_to_dict(row):
    """Convert a sqlite3.Row to a dict matching the original JSON schema."""
    # Parse earshot back from JSON string
    earshot = row['earshot']
    if earshot:
        try:
            earshot = json.loads(earshot)
        except:
            earshot = []
    else:
        earshot = []

    entry = {
        'sourceEntryIds': [row['id']],
        'timestamp': row['timestamp'] or 0,  # Ensure never None
        'gameTime': row['game_time'],
        'gameDate': row['game_date'],
        'speaker': row['speaker'],
        'voiceName': row['voice_name'],
        'target': row['target'],
        'targetId': _canonicalize_target_id(row['target_id']) if 'target_id' in row.keys() else None,
        'text': row['text'],
        'isPlayer': bool(row['is_player']),
        'isAIResponse': bool(row['is_ai_response']),
        'type': row['entry_type'] or 'dialogue',  # Default type
        'earshot': earshot,
    }

    # Add optional fields if they have values
    if row['line_id']:
        entry['lineID'] = row['line_id']
    if row['location']:
        entry['location'] = row['location']
    if row['duration']:
        entry['duration'] = row['duration']
    if row['spell_category']:
        entry['spellCategory'] = row['spell_category']

    # Parse companions back from JSON string
    companions_raw = row['companions'] if 'companions' in row.keys() else None
    if companions_raw:
        try:
            entry['companions'] = json.loads(companions_raw)
        except Exception:
            pass

    # Collapsed spell fields
    if row['count'] and row['count'] > 1:
        entry['count'] = row['count']
        if row['first_game_time']:
            entry['firstGameTime'] = row['first_game_time']
        if row['first_game_date']:
            entry['firstGameDate'] = row['first_game_date']
        if row['first_timestamp']:
            entry['firstTimestamp'] = row['first_timestamp']
        if row['last_game_time']:
            entry['lastGameTime'] = row['last_game_time']
        if row['last_game_date']:
            entry['lastGameDate'] = row['last_game_date']
        if row['last_timestamp']:
            entry['lastTimestamp'] = row['last_timestamp']

    return entry


def _ensure_initialized():
    """Ensure database is initialized before operations."""
    if not _initialized:
        from . import player_context
        if not player_context.is_ready():
            return
        init_db()


def _load_all_entries_impl(repair_on_read=True):
    """
    Load all dialogue entries from database as list of dicts.
    Returns entries in chronological order (oldest first).

    When repair_on_read is enabled, legacy game date/time repairs are applied and
    persisted. Fast UI listing paths should disable this to avoid doing write
    work during normal reads.
    """
    _ensure_initialized()
    try:
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM dialogue_entries ORDER BY timestamp ASC, id ASC"
            ).fetchall()

        entries = []
        ids = []
        for row in rows:
            entries.append(_row_to_dict(row))
            ids.append(row['id'])

        if not repair_on_read:
            return entries

        # Check if any entries need backfilling
        missing = sum(1 for e in entries if not e.get('gameDate'))
        if missing > 0:
            print(f"[DialogueDB] Found {missing}/{len(entries)} entries missing gameDate, running backfill...")
            count = _backfill_game_times(entries)
            if count > 0:
                print(f"[DialogueDB] Backfilled {count} of {missing} missing entries")
                # Persist fixes back to DB
                with get_connection() as conn:
                    for i, entry in enumerate(entries):
                        if entry.get('gameDate') and entry.get('gameTime'):
                            conn.execute("""
                                UPDATE dialogue_entries
                                SET game_date = ?, game_time = ?
                                WHERE id = ? AND (game_date IS NULL OR game_date = '')
                            """, (entry['gameDate'], entry['gameTime'], ids[i]))
                    conn.commit()
                print(f"[DialogueDB] Persisted backfilled dates to DB")
            else:
                print(f"[DialogueDB] Backfill found no anchors to interpolate from")

        # Normalize long-format dates to short YYYY/MM/DD (one-time fix)
        normalize_count = 0
        for i, entry in enumerate(entries):
            game_date = entry.get('gameDate', '')
            if not game_date or '/' in game_date:
                continue  # Already short format or empty
            parsed = _parse_game_date(game_date)
            if parsed:
                short = f"{parsed[0]}/{parsed[1]:02d}/{parsed[2]:02d}"
                entries[i]['gameDate'] = short
                normalize_count += 1
        if normalize_count > 0:
            _backup_db("date_normalize")
            with get_connection() as conn:
                for i, entry in enumerate(entries):
                    conn.execute("""
                        UPDATE dialogue_entries
                        SET game_date = ?
                        WHERE id = ? AND game_date IS NOT NULL AND game_date NOT LIKE '____/%%'
                    """, (entry['gameDate'], ids[i]))
                conn.commit()
            print(f"[DialogueDB] Normalized {normalize_count} long-format dates to short format")

        # Normalize legacy commitment times stored as 24-hour HH:MM (one-time fix)
        commitment_time_updates = _normalize_commitment_game_times(entries)
        if commitment_time_updates:
            _backup_db("commitment_time_normalize")
            with get_connection() as conn:
                for idx, normalized_time in commitment_time_updates:
                    conn.execute("""
                        UPDATE dialogue_entries
                        SET game_time = ?
                        WHERE id = ? AND entry_type = 'commitment'
                    """, (normalized_time, ids[idx]))
                conn.commit()
            print(f"[DialogueDB] Normalized {len(commitment_time_updates)} commitment gameTime values to AM/PM format")

        return entries
    except Exception as e:
        print(f"[DialogueDB] Error loading entries: {e}")
        return []


def load_all_entries():
    """
    Load all dialogue entries from database as list of dicts.
    Returns entries in chronological order (oldest first).
    Backfills missing gameDate/gameTime from nearby entries and persists fixes.
    """
    return _load_all_entries_impl(repair_on_read=True)


def load_all_entries_fast():
    """
    Load all dialogue entries from database without mutation-on-read repairs.
    Intended for hot UI listing paths that should remain read-only.
    """
    return _load_all_entries_impl(repair_on_read=False)


def append_entry(entry):
    """
    Append a single entry to the database.
    This is the efficient path for normal dialogue recording.
    """
    _ensure_initialized()
    try:
        with get_connection() as conn:
            inserted = _insert_entry(conn, entry)
            conn.commit()
            return inserted
    except Exception as e:
        print(f"[DialogueDB] Error appending entry: {e}")
        return False


def replace_all_entries(history):
    """
    Replace entire history (for import operations).
    Uses a transaction for atomicity.
    """
    _ensure_initialized()
    try:
        with get_connection() as conn:
            conn.execute("DELETE FROM dialogue_entries")
            for entry in history:
                if isinstance(entry, dict):
                    _insert_entry(conn, entry)
            conn.commit()
    except Exception as e:
        print(f"[DialogueDB] Error replacing entries: {e}")
        raise  # Re-raise for import operations where caller needs to know


def clear_all_entries():
    """Clear all entries from the database."""
    _ensure_initialized()
    try:
        with get_connection() as conn:
            conn.execute("DELETE FROM dialogue_entries")
            conn.commit()
    except Exception as e:
        print(f"[DialogueDB] Error clearing entries: {e}")


def get_last_entry():
    """Get the most recent entry, or None if database is empty."""
    _ensure_initialized()
    try:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM dialogue_entries ORDER BY timestamp DESC, id DESC LIMIT 1"
            ).fetchone()
        if row:
            return _row_to_dict(row)
        return None
    except Exception as e:
        print(f"[DialogueDB] Error getting last entry: {e}")
        return None


def get_latest_recorded_game_time_candidates(limit=5, min_minutes=0):
    """Return top dialogue rows contributing the latest recorded game times."""
    _ensure_initialized()
    candidates = []

    try:
        with get_connection() as conn:
            rows = conn.execute("""
                SELECT
                    id, timestamp, speaker, voice_name, text, entry_type, line_id, location, count,
                    game_date, game_time,
                    first_game_date, first_game_time,
                    last_game_date, last_game_time
                FROM dialogue_entries
                WHERE (game_date IS NOT NULL AND game_date != '' AND game_time IS NOT NULL AND game_time != '')
                   OR (first_game_date IS NOT NULL AND first_game_date != '' AND first_game_time IS NOT NULL AND first_game_time != '')
                   OR (last_game_date IS NOT NULL AND last_game_date != '' AND last_game_time IS NOT NULL AND last_game_time != '')
            """).fetchall()

        for row in rows:
            # Legacy commitment-creation rows were incorrectly stamped with the
            # scheduled meeting time instead of the time the commitment was made.
            # Skip them for out-of-sync detection so old bad rows do not keep
            # generating false future-history warnings.
            text_lower = (row["text"] or "").strip().lower()
            if row["entry_type"] == "commitment" and " arranged to meet " in f" {text_lower} ":
                continue

            for source_label, date_key, time_key in (
                ("game", "game_date", "game_time"),
                ("first", "first_game_date", "first_game_time"),
                ("last", "last_game_date", "last_game_time"),
            ):
                game_date_raw = row[date_key] or ""
                game_time_raw = row[time_key] or ""
                game_date = _parse_game_date(game_date_raw)
                game_time = _parse_game_time(game_time_raw)
                if game_date is None or game_time is None:
                    continue

                total_minutes = _game_datetime_to_minutes(game_date, game_time)
                if total_minutes < min_minutes:
                    continue

                text = (row["text"] or "").strip()
                if len(text) > 120:
                    text = text[:117] + "..."

                candidates.append({
                    "minutes": total_minutes,
                    "id": row["id"],
                    "timestamp": row["timestamp"],
                    "source": source_label,
                    "gameDate": _format_game_date(*game_date),
                    "gameTime": _format_game_time(*game_time),
                    "type": row["entry_type"] or "dialogue",
                    "speaker": row["speaker"] or "",
                    "voiceName": row["voice_name"] or "",
                    "lineID": row["line_id"] or "",
                    "location": row["location"] or "",
                    "count": row["count"] or 1,
                    "text": text,
                })

        candidates.sort(key=lambda c: (c["minutes"], c["id"]), reverse=True)
        return candidates[:limit]
    except Exception as e:
        print(f"[DialogueDB] Error getting latest recorded game time candidates: {e}")
        return []


def get_latest_recorded_game_minutes():
    """Return the latest stored game minute across dialogue history, or 0."""
    candidates = get_latest_recorded_game_time_candidates(limit=1)
    if not candidates:
        return 0
    return int(candidates[0]["minutes"] or 0)


def delete_entries_by_voice(voice_name):
    """Delete all entries for a specific NPC voice."""
    _ensure_initialized()
    try:
        with get_connection() as conn:
            cursor = conn.execute(
                "DELETE FROM dialogue_entries WHERE voice_name = ?",
                (voice_name,)
            )
            conn.commit()
            return cursor.rowcount
    except Exception as e:
        print(f"[DialogueDB] Error deleting entries by voice: {e}")
        return 0


def delete_entries_by_ids(entry_ids):
    """
    Delete entries by their database IDs.
    Returns number of deleted entries.
    """
    if not entry_ids:
        return 0
    _ensure_initialized()
    try:
        with get_connection() as conn:
            placeholders = ','.join('?' * len(entry_ids))
            cursor = conn.execute(
                f"DELETE FROM dialogue_entries WHERE id IN ({placeholders})",
                tuple(entry_ids)  # Ensure it's a tuple for sqlite3
            )
            conn.commit()
            return cursor.rowcount
    except Exception as e:
        print(f"[DialogueDB] Error deleting entries by IDs: {e}")
        return 0


def get_entries_by_ids(entry_ids):
    """Load specific dialogue entries by their database IDs."""
    if not entry_ids:
        return []
    _ensure_initialized()
    try:
        with get_connection() as conn:
            placeholders = ','.join('?' * len(entry_ids))
            rows = conn.execute(
                f"SELECT * FROM dialogue_entries WHERE id IN ({placeholders}) ORDER BY timestamp ASC, id ASC",
                tuple(entry_ids)
            ).fetchall()
        return [_row_to_dict(row) for row in rows]
    except Exception as e:
        print(f"[DialogueDB] Error loading entries by IDs: {e}")
        return []


def update_entry_by_id(entry_id, entry):
    """Update a specific dialogue entry in place by its database ID."""
    if not entry_id:
        return False
    _ensure_initialized()
    try:
        prepared = _prepare_entry_db_fields(entry)
        if prepared is None:
            return False

        with get_connection() as conn:
            cursor = conn.execute("""
                UPDATE dialogue_entries
                SET timestamp = ?, game_time = ?, game_date = ?, speaker = ?, voice_name = ?,
                    target = ?, target_id = ?, text = ?, is_player = ?, is_ai_response = ?, entry_type = ?,
                    earshot = ?, line_id = ?, location = ?, duration = ?, spell_category = ?,
                    count = ?, first_game_time = ?, first_game_date = ?, first_timestamp = ?,
                    last_game_time = ?, last_game_date = ?, last_timestamp = ?, companions = ?
                WHERE id = ?
            """, (
                prepared['timestamp'],
                prepared['game_time'],
                prepared['game_date'],
                prepared['speaker'],
                prepared['voice_name'],
                prepared['target'],
                prepared['target_id'],
                prepared['text'],
                prepared['is_player'],
                prepared['is_ai_response'],
                prepared['entry_type'],
                prepared['earshot'],
                prepared['line_id'],
                prepared['location'],
                prepared['duration'],
                prepared['spell_category'],
                prepared['count'],
                prepared['first_game_time'],
                prepared['first_game_date'],
                prepared['first_timestamp'],
                prepared['last_game_time'],
                prepared['last_game_date'],
                prepared['last_timestamp'],
                prepared['companions'],
                entry_id,
            ))
            conn.commit()
            return cursor.rowcount > 0
    except Exception as e:
        print(f"[DialogueDB] Error updating entry {entry_id}: {e}")
        return False


def get_entries_for_npc(voice_name, limit=None):
    """Get entries where NPC was speaker or in earshot."""
    _ensure_initialized()
    try:
        with get_connection() as conn:
            # Escape LIKE special characters in voice_name for the earshot search
            escaped_name = voice_name.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
            query = """
                SELECT * FROM dialogue_entries
                WHERE voice_name = ?
                   OR earshot LIKE ? ESCAPE '\\'
                ORDER BY timestamp ASC, id ASC
            """
            params = [voice_name, f'%"{escaped_name}"%']
            if limit:
                query += " LIMIT ?"
                params.append(limit)
            rows = conn.execute(query, params).fetchall()
        return [_row_to_dict(row) for row in rows]
    except Exception as e:
        print(f"[DialogueDB] Error getting entries for NPC: {e}")
        return []


def get_recent_entries(limit=100):
    """Get the most recent N entries."""
    _ensure_initialized()
    try:
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM dialogue_entries ORDER BY timestamp DESC, id DESC LIMIT ?",
                (limit,)
            ).fetchall()
        # Reverse to get chronological order
        return [_row_to_dict(row) for row in reversed(rows)]
    except Exception as e:
        print(f"[DialogueDB] Error getting recent entries: {e}")
        return []


def get_recent_meaningful_npcs():
    """Get distinct NPC voice names from meaningful dialogue entries, ordered by most recent.

    Meaningful: AI dialogue, cutscene, combat, commitment.
    Excludes: ambient chatter, spells, location, broom, mount, prompt.
    Returns list of voice_name strings.
    """
    _ensure_initialized()
    try:
        with get_connection() as conn:
            rows = conn.execute("""
                SELECT voice_name, MAX(id) as last_id
                FROM dialogue_entries
                WHERE voice_name IS NOT NULL
                  AND voice_name != ''
                  AND is_player = 0
                  AND (
                    (entry_type = 'dialogue' AND is_ai_response = 1)
                    OR entry_type IN ('cutscene', 'combat', 'commitment')
                  )
                GROUP BY voice_name
                ORDER BY last_id DESC
            """).fetchall()
        return [row['voice_name'] for row in rows]
    except Exception as e:
        print(f"[DialogueDB] Error getting recent meaningful NPCs: {e}")
        return []


def get_entry_count():
    """Get total number of entries in database."""
    _ensure_initialized()
    try:
        with get_connection() as conn:
            return conn.execute("SELECT COUNT(*) FROM dialogue_entries").fetchone()[0]
    except Exception as e:
        print(f"[DialogueDB] Error getting entry count: {e}")
        return 0


def export_to_json():
    """Export all entries to JSON format (for backup/export API)."""
    return load_all_entries()


def import_from_json(history):
    """Import entries from JSON format, merging without rewriting row IDs."""
    if not isinstance(history, list):
        return

    existing = load_all_entries()
    existing_sigs = {
        (entry.get('timestamp', 0), entry.get('voiceName', ''), entry.get('text', ''))
        for entry in existing
    }

    for entry in history:
        if not isinstance(entry, dict):
            continue
        sig = (entry.get('timestamp', 0), entry.get('voiceName', ''), entry.get('text', ''))
        if sig in existing_sigs:
            continue
        if not append_entry(entry):
            continue
        existing_sigs.add(sig)


def get_db_info():
    """Get database information for diagnostics."""
    _ensure_initialized()
    try:
        with get_connection() as conn:
            version = _get_schema_version(conn)
            count = conn.execute("SELECT COUNT(*) FROM dialogue_entries").fetchone()[0]
            # Get file size
            db_size = os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0
        return {
            "schema_version": version,
            "entry_count": count,
            "db_path": DB_PATH,
            "db_size_bytes": db_size,
            "target_version": SCHEMA_VERSION,
        }
    except Exception as e:
        return {"error": str(e)}


try:
    from . import player_context
    player_context.register("dialogue_db", close_all, reinit)
except ImportError:
    pass
