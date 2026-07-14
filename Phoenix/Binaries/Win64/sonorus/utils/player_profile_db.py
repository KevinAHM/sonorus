"""
SQLite-backed per-player profile storage.

This owns player-specific editable facts that must follow the active
PlayerContext instead of living in global settings.json.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager

from .settings import DATA_DIR, SETTINGS_FILE, save_settings


DB_FILENAME = "player_profile.db"
DB_PATH = os.path.join(DATA_DIR, DB_FILENAME)
PLAYERS_DIR = os.path.join(DATA_DIR, "players")

SCHEMA_VERSION = 1

PROFILE_KEY_STATIC_BIO = "static_bio"
PROFILE_KEY_LEGACY_BIO_MIGRATED = "legacy_player_bio_migrated_v1"
GLOBAL_LEGACY_BIO_MIGRATION_KEY = "legacy_player_bio_migrated_to_profile_db_v1"

_UNSAFE_CHARS_RE = re.compile(r'[\\/:*?"<>|]')
_CONTROL_CHARS_RE = re.compile(r'[\x00-\x1f\x7f]')

_local = threading.local()
_all_connections = []
_all_connections_lock = threading.Lock()
_init_lock = threading.Lock()
_initialized = False


def _normalize_player_name(raw_name):
    if raw_name is None:
        return ""
    name = str(raw_name)
    name = _UNSAFE_CHARS_RE.sub("", name)
    name = _CONTROL_CHARS_RE.sub("", name)
    return name.casefold().strip().strip(".")


def _unique_values(*values):
    seen = set()
    result = []
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _clean_text(value):
    if not isinstance(value, str):
        return ""
    return value.strip()


def _ensure_data_dir(path=None):
    os.makedirs(os.path.dirname(path or DB_PATH), exist_ok=True)


def _create_connection(path=None, *, track=True):
    db_path = path or DB_PATH
    _ensure_data_dir(db_path)
    conn = sqlite3.connect(db_path, timeout=30.0, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row
    if track:
        with _all_connections_lock:
            _all_connections.append(conn)
    return conn


@contextmanager
def get_connection():
    """Get the current player profile connection."""
    try:
        if hasattr(_local, "conn") and _local.conn is not None:
            try:
                _local.conn.execute("SELECT 1")
            except (sqlite3.ProgrammingError, sqlite3.OperationalError):
                try:
                    _local.conn.close()
                except Exception:
                    pass
                _local.conn = None

        if not hasattr(_local, "conn") or _local.conn is None:
            _local.conn = _create_connection()

        yield _local.conn
    except sqlite3.Error:
        if hasattr(_local, "conn") and _local.conn:
            try:
                _local.conn.close()
            except Exception:
                pass
            _local.conn = None
        raise
    finally:
        if hasattr(_local, "conn") and _local.conn is not None:
            try:
                _local.conn.rollback()
            except Exception:
                pass


@contextmanager
def _one_shot_connection(path):
    conn = _create_connection(path, track=False)
    try:
        yield conn
    finally:
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass
        conn.close()


def close_all():
    """Close all tracked profile DB connections and reset initialization state."""
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
    if hasattr(_local, "conn"):
        _local.conn = None
    _initialized = False


def reinit(data_dir):
    """Re-initialize against a new player data directory."""
    global DB_PATH
    close_all()
    DB_PATH = os.path.join(data_dir, DB_FILENAME)
    init_db()


def _get_schema_version(conn):
    try:
        result = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
        ).fetchone()
        if not result:
            return 0
        row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
        return int(row[0]) if row else 0
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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS profile_values (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            current = 1
            _set_schema_version(conn, current)
        else:
            raise RuntimeError(f"Unknown player_profile DB schema version: {current}")
    conn.commit()


def init_db():
    """Initialize the current player profile database."""
    global _initialized
    with _init_lock:
        if _initialized:
            return
        with get_connection() as conn:
            version = _get_schema_version(conn)
            _run_migrations(conn, version)
        _initialized = True


def _ensure_initialized():
    if not _initialized:
        init_db()


def _init_db_at_path(path):
    with _one_shot_connection(path) as conn:
        version = _get_schema_version(conn)
        _run_migrations(conn, version)


def _get_value(conn, key):
    row = conn.execute("SELECT value FROM profile_values WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _set_value(conn, key, value):
    conn.execute(
        """
        INSERT INTO profile_values (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_at = excluded.updated_at
        """,
        (key, str(value or ""), int(time.time())),
    )


def _player_db_path(normalized_player_name):
    player_name = _normalize_player_name(normalized_player_name)
    if not player_name:
        return None
    return os.path.join(PLAYERS_DIR, player_name, DB_FILENAME)


def get_player_static_bio() -> str:
    """Return the current player's stored static bio."""
    _ensure_initialized()
    try:
        with get_connection() as conn:
            return _get_value(conn, PROFILE_KEY_STATIC_BIO) or ""
    except Exception as exc:
        print(f"[PlayerProfileDB] Error reading player bio: {exc}")
        return ""


def get_player_static_bio_for_player(normalized_player_name) -> str:
    """Read a specific player's bio without switching PlayerContext."""
    path = _player_db_path(normalized_player_name)
    if not path:
        return ""
    try:
        _init_db_at_path(path)
        with _one_shot_connection(path) as conn:
            return _get_value(conn, PROFILE_KEY_STATIC_BIO) or ""
    except Exception as exc:
        print(f"[PlayerProfileDB] Error reading player bio for {normalized_player_name}: {exc}")
        return ""


def set_player_static_bio(text) -> bool:
    """Set the current player's stored static bio."""
    _ensure_initialized()
    try:
        with get_connection() as conn:
            _set_value(conn, PROFILE_KEY_STATIC_BIO, _clean_text(text))
            _set_value(conn, PROFILE_KEY_LEGACY_BIO_MIGRATED, "1")
            conn.commit()
        return True
    except Exception as exc:
        print(f"[PlayerProfileDB] Error saving player bio: {exc}")
        return False


def set_player_static_bio_for_player(normalized_player_name, text) -> bool:
    """Set a specific player's bio without switching PlayerContext."""
    path = _player_db_path(normalized_player_name)
    if not path:
        return False
    try:
        _init_db_at_path(path)
        with _one_shot_connection(path) as conn:
            _set_value(conn, PROFILE_KEY_STATIC_BIO, _clean_text(text))
            _set_value(conn, PROFILE_KEY_LEGACY_BIO_MIGRATED, "1")
            conn.commit()
        return True
    except Exception as exc:
        print(f"[PlayerProfileDB] Error saving player bio for {normalized_player_name}: {exc}")
        return False


def _load_saved_settings_file():
    try:
        if not os.path.exists(SETTINGS_FILE):
            return {}
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        print(f"[PlayerProfileDB] Error reading settings for player bio migration: {exc}")
        return {}


def _mark_global_legacy_bio_migrated(settings):
    if not isinstance(settings, dict) or not settings:
        return False
    migrations = settings.setdefault("migrations", {})
    if not isinstance(migrations, dict):
        migrations = {}
        settings["migrations"] = migrations
    if migrations.get(GLOBAL_LEGACY_BIO_MIGRATION_KEY):
        return False
    migrations[GLOBAL_LEGACY_BIO_MIGRATION_KEY] = True
    return save_settings(settings)


def _settings_mapping(settings, key):
    prompts = settings.get("prompts", {}) if isinstance(settings, dict) else {}
    mapping = prompts.get(key, {}) if isinstance(prompts, dict) else {}
    return mapping if isinstance(mapping, dict) else {}


def _find_legacy_player_bio(settings, raw_player_name, normalized_player_name):
    prompts = settings.get("prompts", {}) if isinstance(settings, dict) else {}
    migrations = settings.get("migrations", {}) if isinstance(settings, dict) else {}
    if not isinstance(prompts, dict):
        return "", "", False
    if not isinstance(migrations, dict):
        migrations = {}

    normalized = _normalize_player_name(normalized_player_name or raw_player_name)
    player_static_bios = _settings_mapping(settings, "player_static_bios")
    static_bios = _settings_mapping(settings, "static_bios")
    editor_guidance = _settings_mapping(settings, "editor_guidance")

    for key in _unique_values(normalized, normalized_player_name):
        legacy_bio = _clean_text(player_static_bios.get(key))
        if legacy_bio:
            return legacy_bio, f"prompts.player_static_bios.{key}", False

    for key in _unique_values(raw_player_name, normalized_player_name, normalized):
        if key == "Player":
            continue
        legacy_bio = _clean_text(static_bios.get(key))
        if legacy_bio:
            return legacy_bio, f"prompts.static_bios.{key}", False
        legacy_bio = _clean_text(editor_guidance.get(key))
        if legacy_bio:
            return legacy_bio, f"prompts.editor_guidance.{key}", False

    if not migrations.get(GLOBAL_LEGACY_BIO_MIGRATION_KEY):
        legacy_bio = _clean_text(static_bios.get("Player"))
        if legacy_bio:
            return legacy_bio, "prompts.static_bios.Player", True
        legacy_bio = _clean_text(editor_guidance.get("Player"))
        if legacy_bio:
            return legacy_bio, "prompts.editor_guidance.Player", True

    return "", "", False


def _has_unconsumed_global_legacy_bio(settings):
    migrations = settings.get("migrations", {}) if isinstance(settings, dict) else {}
    if isinstance(migrations, dict) and migrations.get(GLOBAL_LEGACY_BIO_MIGRATION_KEY):
        return False
    static_bios = _settings_mapping(settings, "static_bios")
    editor_guidance = _settings_mapping(settings, "editor_guidance")
    return bool(
        _clean_text(static_bios.get("Player"))
        or _clean_text(editor_guidance.get("Player"))
    )


def migrate_legacy_player_bio(raw_player_name=None, normalized_player_name=None, data_dir=None):
    """One-time migration from legacy settings keys into the current player DB.

    The old settings keys are read as migration input only. They are not updated
    or removed, and runtime player bio reads ignore them after this point.
    """
    _ensure_initialized()
    try:
        settings = _load_saved_settings_file()
        with get_connection() as conn:
            if _get_value(conn, PROFILE_KEY_LEGACY_BIO_MIGRATED):
                if _has_unconsumed_global_legacy_bio(settings):
                    _mark_global_legacy_bio_migrated(settings)
                return False

        global_legacy_present = _has_unconsumed_global_legacy_bio(settings)
        legacy_bio, source, consumed_global = _find_legacy_player_bio(
            settings,
            raw_player_name,
            normalized_player_name,
        )

        with get_connection() as conn:
            if legacy_bio:
                _set_value(conn, PROFILE_KEY_STATIC_BIO, legacy_bio)
            _set_value(conn, PROFILE_KEY_LEGACY_BIO_MIGRATED, "1")
            conn.commit()

        if global_legacy_present or consumed_global:
            _mark_global_legacy_bio_migrated(settings)
        if legacy_bio:
            player_name = normalized_player_name or _normalize_player_name(raw_player_name)
            print(f"[PlayerProfileDB] Migrated legacy Player bio for {player_name} from {source}")
        return bool(legacy_bio)
    except Exception as exc:
        print(f"[PlayerProfileDB] Error migrating legacy player bio: {exc}")
        return False


try:
    from . import player_context
    player_context.register("player_profile_db", close_all, reinit, migrate_legacy_player_bio)
except ImportError:
    pass
