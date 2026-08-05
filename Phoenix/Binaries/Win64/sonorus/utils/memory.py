"""
NPC Long-Term Memory System for Sonorus.

Uses a lightweight local Cognis fact store for NPC memories.
Each NPC has their own owner_id for memory isolation based on earshot.
"""

import os
import json
import asyncio
import re
import time
import string
import shutil
import sqlite3
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Optional, Dict, List, Any, ClassVar
from .settings import load_settings, DATA_DIR, SONORUS_DIR, is_dev_mode, is_llm_provider_feature_disabled
from .character_bios import (
    get_editor_guidance,
    get_player_static_bio,
    get_static_bio,
    is_npc_memory_effectively_enabled,
)
from .dialogue import format_combat_summary
from .profiler import Profiler
from constants import EPISODE_CONTEXT_WINDOW



# Get shared profiler instance
_profiler = Profiler.get("chat_flow")

_memory_data_dir = DATA_DIR
_MEMORY_SUPPORTED_PROVIDERS = ("openai", "openrouter", "gemini")


def _normalize_openai_compatible_memory_model(model: str, provider: str) -> str:
    model = (model or "gpt-4.1-nano").strip() or "gpt-4.1-nano"
    if provider == "openrouter":
        if "/" in model:
            return model
        if model.lower().startswith("gemini-"):
            return f"google/{model}"
        if model.startswith("gpt-") or model.startswith("o"):
            return f"openai/{model}"
        return model
    if provider == "openai":
        if "gemini" in model.lower():
            return "gpt-4.1-nano"
        if model.startswith("openai/"):
            return model.split("/", 1)[1]
    if provider == "gemini" and model.startswith("google/"):
        return model.split("/", 1)[1]
    return model


def _get_memory_llm_model(memory_settings: Dict[str, Any], key: str, fallback: str = "gpt-4.1-nano") -> str:
    settings = load_settings()
    provider = settings.get("llm", {}).get("provider", "gemini")
    return _normalize_openai_compatible_memory_model(memory_settings.get(key) or fallback, provider)


def _sync_memory_data_dir_from_player_context() -> str:
    """Keep memory storage aligned with the active player-scoped data directory."""
    global _memory_data_dir

    try:
        from . import player_context

        player_data_dir = player_context.get_player_data_dir()
        if player_data_dir:
            _memory_data_dir = player_data_dir
    except Exception:
        pass

    return _memory_data_dir


def get_memory_data_dir() -> str:
    """Return the active memory data directory."""
    return _sync_memory_data_dir_from_player_context()


def _get_memory_backup_root() -> str:
    """Return the directory that stores logical memory snapshots and legacy graph exports."""
    return os.path.join(_sync_memory_data_dir_from_player_context(), "graph_backups")


def _get_memory_snapshot_manifest_path(snapshot_dir: str) -> str:
    """Return the manifest path for a memory snapshot."""
    return os.path.join(snapshot_dir, "manifest.json")




def _get_memory_snapshot_cognis_dir(snapshot_dir: str) -> str:
    """Return the Cognis data directory inside a memory snapshot."""
    return os.path.join(snapshot_dir, "cognis")


def _get_memory_snapshot_sqlite_dir(snapshot_dir: str) -> str:
    """Return the SQLite snapshot directory inside a memory snapshot."""
    return os.path.join(snapshot_dir, "sqlite")


def _get_memory_snapshot_queue_db_path(snapshot_dir: str) -> str:
    """Return the snapshot path for the memory queue database."""
    return os.path.join(_get_memory_snapshot_sqlite_dir(snapshot_dir), "memory_queue.db")


def _get_chapter_content_dir_path() -> str:
    """Return the on-disk chapter content directory path."""
    return os.path.join(_sync_memory_data_dir_from_player_context(), "chapter_content")


def _safe_chapter_artifact_name(npc_id: str, chapter_title: str, timestamp: Optional[int] = None) -> str:
    raw = f"{npc_id}_{timestamp or int(time.time())}_{chapter_title}"
    safe = "".join(ch if ch.isalnum() else "_" for ch in raw).strip("_")
    return (safe[:180] or f"{npc_id}_chapter") + ".json"


def save_generated_episode_audit(npc_id: str, chapter_title: str, episode_content: str,
                                 prompt_inputs: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Persist generated episode prose so bad extracted facts can be audited later."""
    try:
        audit_dir = os.path.join(_get_chapter_content_dir_path(), "generated")
        os.makedirs(audit_dir, exist_ok=True)
        path = os.path.join(
            audit_dir,
            _safe_chapter_artifact_name(npc_id, chapter_title, int(time.time())),
        )
        payload = {
            "npc_id": npc_id,
            "chapter_title": chapter_title,
            "created_at": int(time.time()),
            "episode_content": episode_content,
            "prompt_inputs": prompt_inputs or {},
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return path
    except Exception as e:
        print(f"[Memory] Warning: could not save generated episode audit for {npc_id}: {e}")
        return None


def _get_current_server_session() -> int:
    """Best-effort current server session number for today's backup folder."""
    logs_dir = os.path.join(SONORUS_DIR, "logs")
    today = datetime.now().strftime("%Y-%m-%d")
    prefix = f"server_{today}_"
    latest_session = 1

    if not os.path.isdir(logs_dir):
        return latest_session

    for name in os.listdir(logs_dir):
        if not (name.startswith(prefix) and name.endswith(".log")):
            continue
        session_text = name[len(prefix):-4]
        try:
            latest_session = max(latest_session, int(session_text))
        except ValueError:
            continue

    return latest_session


def _get_session_backup_root(root_dir: str) -> str:
    """Return the dated session directory for graph export backups."""
    date_dir = os.path.join(root_dir, datetime.now().strftime("%Y-%m-%d"))
    session_dir = os.path.join(date_dir, f"session_{_get_current_server_session()}")
    os.makedirs(session_dir, exist_ok=True)
    return session_dir


def _sanitize_backup_reason(reason: str) -> str:
    """Make backup reason filesystem-safe and readable."""
    safe = ''.join(ch.lower() if ch.isalnum() else '_' for ch in (reason or "backup"))
    while "__" in safe:
        safe = safe.replace("__", "_")
    return safe.strip("_") or "backup"


def _make_unique_backup_dir(root_dir: str, reason: str) -> str:
    """Create a unique export directory path for a logical graph backup."""
    session_root = _get_session_backup_root(root_dir)
    ts = datetime.now().strftime("%H%M%S")
    base_name = f"{ts}_{_sanitize_backup_reason(reason)}"
    candidate = os.path.join(session_root, base_name)
    suffix = 1
    while os.path.exists(candidate):
        suffix += 1
        candidate = os.path.join(session_root, f"{base_name}.{suffix}")
    return candidate


def _iter_graph_backup_dirs(root_dir: str) -> List[str]:
    """Return all concrete export backup directories."""
    backup_dirs: List[str] = []
    if not os.path.isdir(root_dir):
        return backup_dirs

    for date_name in os.listdir(root_dir):
        date_path = os.path.join(root_dir, date_name)
        if not os.path.isdir(date_path):
            continue
        for session_name in os.listdir(date_path):
            session_path = os.path.join(date_path, session_name)
            if not os.path.isdir(session_path):
                continue
            for backup_name in os.listdir(session_path):
                backup_path = os.path.join(session_path, backup_name)
                if os.path.isdir(backup_path):
                    backup_dirs.append(backup_path)

    return backup_dirs


def _prune_old_graph_backups(root_dir: str, keep: int = 10) -> None:
    """Keep only the newest logical graph backups."""
    if keep <= 0 or not os.path.isdir(root_dir):
        return

    backup_dirs = _iter_graph_backup_dirs(root_dir)
    backup_dirs.sort(key=lambda path: os.path.getmtime(path), reverse=True)
    for stale_path in backup_dirs[keep:]:
        try:
            shutil.rmtree(stale_path)
            print(f"[Memory] Pruned old memory backup: {stale_path}")
        except Exception as e:
            print(f"[Memory] Failed to prune memory backup '{stale_path}': {e}")

    # Clean up empty session/date folders after pruning.
    for date_name in os.listdir(root_dir):
        date_path = os.path.join(root_dir, date_name)
        if not os.path.isdir(date_path):
            continue
        for session_name in os.listdir(date_path):
            session_path = os.path.join(date_path, session_name)
            if os.path.isdir(session_path) and not os.listdir(session_path):
                try:
                    os.rmdir(session_path)
                except Exception:
                    pass
        if not os.listdir(date_path):
            try:
                os.rmdir(date_path)
            except Exception:
                pass


def _find_latest_graph_backup() -> Optional[str]:
    """Return the newest logical export backup directory, if any."""
    root_dir = _get_memory_backup_root()
    backup_dirs = _iter_graph_backup_dirs(root_dir)
    if not backup_dirs:
        return None

    backup_dirs.sort(key=lambda path: os.path.getmtime(path), reverse=True)
    return backup_dirs[0]


def _relative_graph_backup_id(backup_path: str) -> str:
    """Return a stable backup ID relative to the graph backup root."""
    return os.path.relpath(backup_path, _get_memory_backup_root()).replace("\\", "/")


def _resolve_memory_backup_path(backup_id: str) -> Optional[str]:
    """Resolve a client-provided memory backup ID to an on-disk directory."""
    if not backup_id:
        return None

    root_dir = os.path.abspath(_get_memory_backup_root())
    candidate = os.path.abspath(os.path.join(root_dir, backup_id.replace("/", os.sep)))

    try:
        if os.path.commonpath([root_dir, candidate]) != root_dir:
            return None
    except ValueError:
        return None

    if not os.path.isdir(candidate):
        return None

    manifest_path = _get_memory_snapshot_manifest_path(candidate)
    if os.path.isfile(manifest_path):
        return candidate

    legacy_required = ("schema.cypher", "copy.cypher")
    if all(os.path.exists(os.path.join(candidate, name)) for name in legacy_required):
        return candidate

    return None


def _load_backup_manifest(backup_path: str) -> Dict[str, Any]:
    """Load a snapshot manifest, or synthesize metadata for legacy graph-only backups."""
    manifest_path = _get_memory_snapshot_manifest_path(backup_path)
    if os.path.isfile(manifest_path):
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
            if isinstance(manifest, dict):
                return manifest
        except Exception as e:
            print(f"[Memory] Failed to read backup manifest '{manifest_path}': {e}")

    return {
        "backup_type": "legacy_graph_export",
        "version": 0,
        "includes": {
            "cognis": False,
            "graph_export": True,
            "memory_queue_db": False,
            "chapters": False,
            "chapter_content": False,
            "npc_bios": False,
        }
    }


def _is_restorable_cognis_backup(manifest: Dict[str, Any]) -> bool:
    """Return True when this snapshot can be restored into the Cognis backend."""
    backup_type = manifest.get("backup_type") or "legacy_graph_export"
    includes = manifest.get("includes", {})
    if backup_type == "cognis_memory_snapshot":
        return bool(includes.get("cognis"))
    if backup_type == "memory_snapshot":
        return bool(includes.get("cognis"))
    return False


def list_memory_backups() -> List[Dict[str, Any]]:
    """List restorable Cognis memory snapshots."""
    backups: List[Dict[str, Any]] = []
    root_dir = _get_memory_backup_root()

    for backup_path in _iter_graph_backup_dirs(root_dir):
        rel_id = _relative_graph_backup_id(backup_path)
        parts = rel_id.split("/")
        date_part = parts[0] if len(parts) > 0 else ""
        session_part = parts[1] if len(parts) > 1 else ""
        backup_name = parts[2] if len(parts) > 2 else os.path.basename(backup_path)
        time_part, _, reason_part = backup_name.partition("_")
        reason_label = reason_part.replace("_", " ").strip() if reason_part else backup_name
        manifest = _load_backup_manifest(backup_path)
        if not _is_restorable_cognis_backup(manifest):
            continue

        try:
            created_at = int(os.path.getmtime(backup_path))
        except Exception:
            created_at = 0

        file_names: List[str] = []
        try:
            file_names = sorted(os.listdir(backup_path))
        except Exception:
            pass

        backup_type = manifest.get("backup_type") or "legacy_graph_export"
        kind_label = "Cognis Memory Snapshot"
        backups.append({
            "backup_id": rel_id,
            "date": date_part,
            "session": session_part,
            "time": time_part,
            "reason": reason_part,
            "reason_label": reason_label or "backup",
            "created_at": created_at,
            "files": file_names,
            "backup_type": backup_type,
            "kind_label": kind_label,
            "includes": manifest.get("includes", {}),
        })

    backups.sort(key=lambda item: item.get("created_at", 0), reverse=True)
    return backups



# Lightweight memory backend imports.
_cognis_available = False


try:
    from cognis import Cognis, CognisConfig
    _cognis_available = True
except ImportError as e:
    print(f"[Memory] cognis not installed - memory system unavailable: {e}")


# =============================================================================
# Custom Entity Types for Hogwarts Domain
# =============================================================================


# =============================================================================
# Utility Functions
# =============================================================================

def game_time_to_datetime(game_date: str, game_time: str = "") -> datetime:
    """
    Convert game time (1890/12/08, 14:30) to a datetime for memory timestamps.

    Args:
        game_date: Date string like "1890/12/08" or "Monday, December 8th, 1890"
        game_time: Time string like "14:30" or "2:30 PM"

    Returns:
        datetime object for memory reference time
    """
    try:
        # Try parsing YYYY/MM/DD format first
        if '/' in game_date and len(game_date.split('/')[0]) == 4:
            date_parts = game_date.split('/')
            year = int(date_parts[0])
            month = int(date_parts[1])
            day = int(date_parts[2])
        else:
            # Fallback - use current date components
            now = datetime.now()
            year, month, day = now.year, now.month, now.day

        # Parse time
        hour, minute = 12, 0  # Default to noon
        if game_time:
            time_clean = game_time.strip().upper()
            is_pm = 'PM' in time_clean
            is_am = 'AM' in time_clean
            time_clean = time_clean.replace('AM', '').replace('PM', '').strip()

            if ':' in time_clean:
                time_parts = time_clean.split(':')
                hour = int(time_parts[0])
                minute = int(time_parts[1]) if len(time_parts) > 1 else 0

                # Convert 12-hour to 24-hour
                if is_pm and hour < 12:
                    hour += 12
                elif is_am and hour == 12:
                    hour = 0

        return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
    except Exception as e:
        print(f"[Memory] Error parsing game time '{game_date}' '{game_time}': {e}")
        return datetime.now(timezone.utc)


def is_valid_timestamp(ts: any) -> bool:
    """Check if a value is a valid Unix timestamp (reasonable range)."""
    if ts is None:
        return False
    try:
        ts_int = int(ts)
        # Valid range: ~2017 to ~2286 in Unix time
        # This covers our use case where we record real timestamps
        return 1500000000 <= ts_int <= 10000000000
    except (ValueError, TypeError):
        return False


def _get_enabled_long_term_memory_npc_ids(memory_settings: Dict[str, Any]) -> set[str]:
    """Return the canonical per-NPC allowlist for whitelist-only memory mode."""
    from .localization import canonicalize_npc_id

    enabled_npcs = set()
    raw_allowlist = memory_settings.get('npc_long_term_memory', {})
    if not isinstance(raw_allowlist, dict):
        return enabled_npcs

    for raw_npc_id, enabled in raw_allowlist.items():
        if enabled is not True:
            continue
        canonical_npc_id = canonicalize_npc_id(raw_npc_id)
        if canonical_npc_id:
            enabled_npcs.add(canonical_npc_id)

    return enabled_npcs


def get_npc_long_term_memory_status(npc_id: str, settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return whether long-term memory is effectively enabled for one NPC."""
    from .localization import canonicalize_npc_id

    canonical_npc_id = canonicalize_npc_id(npc_id)
    if not canonical_npc_id:
        return {
            "npc_id": npc_id,
            "enabled": False,
            "reason": "missing_npc_id",
        }

    if settings is None:
        settings = load_settings()

    memory_settings = settings.get('memory', {})
    if not memory_settings.get('enabled', True):
        return {
            "npc_id": canonical_npc_id,
            "enabled": False,
            "reason": "memory_disabled",
        }

    provider = settings.get('llm', {}).get('provider', 'gemini')
    if is_llm_provider_feature_disabled('memory', settings):
        return {
            "npc_id": canonical_npc_id,
            "enabled": False,
            "reason": f"memory_disabled_for_provider_{provider}",
        }
    if provider not in _MEMORY_SUPPORTED_PROVIDERS:
        return {
            "npc_id": canonical_npc_id,
            "enabled": False,
            "reason": f"unsupported_memory_provider_{provider}",
        }

    if not memory_settings.get('whitelisted_npcs_only', False):
        return {
            "npc_id": canonical_npc_id,
            "enabled": True,
            "reason": "global_memory_enabled",
        }

    enabled_npcs = _get_enabled_long_term_memory_npc_ids(memory_settings)
    if canonical_npc_id in enabled_npcs:
        return {
            "npc_id": canonical_npc_id,
            "enabled": True,
            "reason": "npc_whitelisted",
        }

    return {
        "npc_id": canonical_npc_id,
        "enabled": False,
        "reason": "npc_not_whitelisted",
    }


def is_npc_long_term_memory_enabled(npc_id: str, settings: Optional[Dict[str, Any]] = None) -> bool:
    """Return True when long-term memory is effectively enabled for this NPC."""
    return get_npc_long_term_memory_status(npc_id, settings=settings).get("enabled", False)


def filter_memory_enabled_npc_ids(npc_ids: List[str], settings: Optional[Dict[str, Any]] = None) -> List[str]:
    """Filter NPC IDs to those currently eligible for long-term memory, preserving order."""
    filtered: List[str] = []
    seen = set()

    for npc_id in npc_ids or []:
        status = get_npc_long_term_memory_status(npc_id, settings=settings)
        dedupe_key = status.get("npc_id") or str(npc_id).strip()
        if not status.get("enabled") or not dedupe_key or dedupe_key in seen:
            continue
        filtered.append(npc_id)
        seen.add(dedupe_key)

    return filtered


def should_include_entry_for_npc_chaptering(npc_id: str, entry: Dict, include_cutscene: Optional[bool] = None) -> bool:
    """
    Return True if an entry should contribute to chapter detection/summarization for an NPC.

    Rules:
    - Keep anything spoken by the NPC, except spell/mount utility events.
    - Keep witnessed real conversation flow, including player turns, AI turns, and
      targeted third-party NPC dialogue.
    - Keep witnessed structured events that matter for chapter continuity.
    - Drop witnessed ambient third-party chatter.
    """
    if not npc_id or not isinstance(entry, dict):
        return False

    if include_cutscene is None:
        include_cutscene = load_settings().get('memory', {}).get('include_cutscene', True)

    voice_name = entry.get('voiceName', '')
    entry_type = entry.get('type') or 'dialogue'
    earshot = entry.get('earshot', [])
    witnessed = isinstance(earshot, list) and npc_id in earshot
    target_id = str(entry.get('targetId') or '').strip()
    target_id_norm = target_id.lower()

    if entry_type == 'cutscene':
        return include_cutscene and (voice_name == npc_id or witnessed)

    if entry_type in ('spell', 'broom', 'mount', 'mail', 'location'):
        return False

    if voice_name == npc_id:
        return True

    if not witnessed:
        return False

    if entry_type in ('cutscene', 'location', 'combat', 'commitment', 'prompt'):
        return True

    if entry.get('isPlayer'):
        return True

    if entry.get('isAIResponse'):
        return True

    # Non-AI third-party dialogue only counts when it is actually directed at someone.
    # Rows with empty/Unknown targets are ambient chatter and should not drive chapters.
    if target_id:
        return target_id_norm != 'unknown'

    legacy_target = str(entry.get('target') or '').strip().lower()
    return bool(legacy_target and legacy_target != 'unknown')


def count_chapter_candidate_entries_by_npc(history: List[Dict]) -> Dict[str, int]:
    """Count how many chapter-eligible entries each significant NPC has in shared history."""
    counts = {}
    settings = load_settings()
    include_cutscene = settings.get('memory', {}).get('include_cutscene', True)

    try:
        from .text_utils import is_significant_npc
    except Exception:
        is_significant_npc = None

    for entry in history or []:
        if not isinstance(entry, dict):
            continue

        candidate_npcs = set()

        voice_name = entry.get('voiceName')
        if voice_name and str(voice_name).lower() != 'player':
            if is_significant_npc is None or is_significant_npc(voice_name):
                candidate_npcs.add(voice_name)

        earshot = entry.get('earshot', [])
        if isinstance(earshot, list):
            for npc_id in earshot:
                if not npc_id or str(npc_id).lower() == 'player':
                    continue
                if is_significant_npc is None or is_significant_npc(npc_id):
                    candidate_npcs.add(npc_id)

        for npc_id in candidate_npcs:
            if not is_npc_long_term_memory_enabled(npc_id, settings=settings):
                continue
            if should_include_entry_for_npc_chaptering(npc_id, entry, include_cutscene=include_cutscene):
                counts[npc_id] = counts.get(npc_id, 0) + 1

    return counts


def collect_chapter_context_characters(npc_id: str, entries: List[Dict]) -> List[str]:
    """Collect non-player conversation participants from kept chapter entries only."""
    try:
        from .localization import get_display_name
    except Exception:
        get_display_name = lambda value: value

    characters = set()
    npc_id_norm = str(npc_id or '').strip().lower()
    player_ids = {'player'}

    for entry in entries or []:
        if not isinstance(entry, dict) or not entry.get('isPlayer'):
            continue

        voice_name = str(entry.get('voiceName') or '').strip()
        if voice_name:
            player_ids.add(voice_name.lower())

    for entry in entries or []:
        if not isinstance(entry, dict):
            continue

        voice_name = str(entry.get('voiceName') or '').strip()
        voice_norm = voice_name.lower()
        if voice_name and voice_norm not in player_ids and voice_norm != npc_id_norm:
            characters.add(get_display_name(voice_name) or voice_name)

        target_id = str(entry.get('targetId') or '').strip()
        target_norm = target_id.lower()
        if (
            not target_id or
            target_norm in ('unknown', 'player') or
            target_norm in player_ids
        ):
            continue

        if target_norm == npc_id_norm:
            continue

        characters.add(get_display_name(target_id) or target_id)

    return sorted(characters)


def get_chapters_dir() -> str:
    """Get the chapters directory path, creating it if needed."""
    chapters_dir = os.path.join(_memory_data_dir, "chapters")
    os.makedirs(chapters_dir, exist_ok=True)
    return chapters_dir


def _format_episode_chapter_name(episode_name: str) -> str | None:
    """Convert chapter episode ids like chapter_a_midnight_trek into display text."""
    if not episode_name.startswith('chapter_'):
        return None

    # Use capwords to avoid uppercasing after apostrophes (e.g., "Ferdinand'S")
    return string.capwords(episode_name[8:].replace('_', ' '))


async def _load_episode_chapter_map(driver: Any, episode_uuids: set[str] | list[str]) -> Dict[str, str]:
    """Load episode names without touching large/corrupt episodic text columns."""
    if not episode_uuids:
        return {}

    uuids = list(dict.fromkeys(episode_uuids))
    query = """
        MATCH (e:Episodic)
        WHERE e.uuid IN $uuids
        RETURN DISTINCT
            e.uuid AS uuid,
            e.name AS name
    """

    try:
        records, _, _ = await driver.execute_query(query, uuids=uuids, routing_='r')
    except UnicodeDecodeError:
        records = []
        for episode_uuid in uuids:
            try:
                row_records, _, _ = await driver.execute_query(
                    """
                    MATCH (e:Episodic {uuid: $uuid})
                    RETURN e.uuid AS uuid, e.name AS name
                    """,
                    uuid=episode_uuid,
                    routing_='r',
                )
                records.extend(row_records)
            except UnicodeDecodeError as e:
                print(f"[Memory] Warning: Skipping corrupt episode row {episode_uuid}: {e}")

    episode_to_chapter: Dict[str, str] = {}
    for record in records:
        episode_uuid = record.get('uuid')
        episode_name = record.get('name')
        if not episode_uuid or not episode_name:
            continue
        chapter_name = _format_episode_chapter_name(episode_name)
        if chapter_name:
            episode_to_chapter[episode_uuid] = chapter_name

    return episode_to_chapter


class CognisMemoryManager:
    """Legacy manager-compatible facade backed by Cognis flat fact memory."""

    _instance = None
    _lock = threading.Lock()
    _indices_built: ClassVar[bool] = True
    _startup_backup_done: ClassVar[bool] = False

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._cognis = None
        self._last_added_edges = {}
        self._busy = False
        self._initialized = True

    def _get_data_dir(self) -> str:
        return os.path.join(_sync_memory_data_dir_from_player_context(), "cognis")

    def _get_config(self):
        settings = load_settings()
        memory_settings = settings.get("memory", {})
        provider = settings.get("llm", {}).get("provider", "gemini")
        config = CognisConfig()
        if provider == "openrouter":
            config.embedding_model = memory_settings.get("embedding_model") or "openai/text-embedding-3-small"
        elif provider == "openai":
            config.embedding_model = memory_settings.get("embedding_model") or "text-embedding-3-small"
        elif provider == "gemini":
            config.embedding_model = memory_settings.get("embedding_model") or "gemini-embedding-2"
        config.extraction_llm_model = _normalize_openai_compatible_memory_model(
            memory_settings.get("graphiti_model")
            or memory_settings.get("graphiti_small_model")
            or config.llm_model,
            provider,
        )
        config.operation_llm_model = _normalize_openai_compatible_memory_model(
            memory_settings.get("graphiti_small_model")
            or config.extraction_llm_model,
            provider,
        )
        config.llm_model = config.extraction_llm_model
        config.enable_immediate_recall = False
        config.recency_boost_weight = 0.05
        return config

    def init_graphiti(self) -> bool:
        """Initialize the lightweight local memory store."""
        if not _cognis_available:
            print("[Memory] Cognis not available")
            return False

        settings = load_settings()
        provider = settings.get("llm", {}).get("provider", "gemini")
        if is_llm_provider_feature_disabled('memory', settings):
            print(f"[Memory] Long-term memory disabled for provider '{provider}' by LLM Provider settings")
            return False
        if provider not in _MEMORY_SUPPORTED_PROVIDERS:
            print(f"[Memory] Long-term memory disabled for provider '{provider}' (unsupported memory provider)")
            return False

        try:
            from . import player_context
            if not player_context.is_ready():
                print("[Memory] Player context not ready - deferring memory init")
                return False
        except Exception:
            pass

        with self._lock:
            if self._cognis is not None:
                return True
            try:
                self._cognis = Cognis(
                    data_dir=self._get_data_dir(),
                    owner_id="sonorus",
                    agent_id="npc_memory",
                    session_id="sonorus",
                    config=self._get_config(),
                )
                print(f"[Memory/Cognis] Initialized local fact memory: {self._get_data_dir()}")
                return True
            except Exception as e:
                print(f"[Memory/Cognis] Failed to initialize: {e}")
                import traceback
                traceback.print_exc()
                self._cognis = None
                return False

    def close_graphiti(self, timeout: float = 30.0) -> bool:
        with self._lock:
            if self._cognis is not None:
                try:
                    self._cognis.close()
                except Exception as e:
                    print(f"[Memory/Cognis] Error closing: {e}")
                    return False
                finally:
                    self._cognis = None
            self._busy = False
        return True

    def export_graph_backup_to_dir(self, export_dir: str, timeout: float = 120.0) -> Optional[str]:
        """Snapshot the Cognis directory into the legacy graph-export slot."""
        src = self._get_data_dir()
        if not os.path.isdir(src):
            return None
        try:
            if os.path.isdir(export_dir):
                shutil.rmtree(export_dir)
            shutil.copytree(src, export_dir)
            return export_dir
        except Exception as e:
            print(f"[Memory/Cognis] Snapshot copy failed: {e}")
            return None

    def export_graph_backup(self, reason: str, timeout: float = 120.0, keep: int = 5) -> Optional[str]:
        backup_root = _get_memory_backup_root()
        export_dir = _make_unique_backup_dir(backup_root, reason)
        result = self.export_graph_backup_to_dir(export_dir, timeout=timeout)
        if result:
            _prune_old_graph_backups(backup_root, keep=keep)
        return result

    def _session_id_for_chapter(self, npc_id: str, chapter_title: str, game_date: str, game_time: str) -> str:
        raw = f"{npc_id}:{chapter_title}:{game_date}:{game_time}"
        safe = "".join(ch if ch.isalnum() else "_" for ch in raw).strip("_")
        return safe[:180] or f"{npc_id}_chapter"

    def add_episode(self, npc_id: str, chapter_title: str,
                    content: str, game_date: str, game_time: str = "") -> bool:
        """Extract and store deduped facts from a closed chapter episode."""
        if not self.init_graphiti():
            return False

        session_id = self._session_id_for_chapter(npc_id, chapter_title, game_date, game_time)
        event_time = game_time_to_datetime(game_date, game_time) if game_date else None
        episode = (
            f"NPC memory episode for {npc_id}\n"
            f"Chapter: {chapter_title}\n"
            f"Game date: {game_date} {game_time}\n\n"
            f"{content}"
        ).strip()

        self._busy = True
        try:
            result = self._cognis.add(
                [{"role": "user", "content": episode}],
                owner_id=npc_id,
                agent_id="npc_memory",
                session_id=session_id,
                event_time=event_time,
            )
            memories = result.get("memories", []) if isinstance(result, dict) else []
            self._last_added_edges[npc_id] = [
                self._memory_to_edge(memory)
                for memory in memories
                if memory.get("content")
            ]
            count = len(memories)
            print(f"[Memory/Cognis] Added episode '{chapter_title}' for {npc_id}: {count} new/updated facts")
            return bool(result and result.get("success"))
        except Exception as e:
            print(f"[Memory/Cognis] Failed to add episode for {npc_id}: {e}")
            import traceback
            traceback.print_exc()
            return False
        finally:
            self._busy = False

    def _memory_to_edge(self, mem: Dict[str, Any]) -> Dict[str, Any]:
        metadata = mem.get("metadata") or {}
        session_id = mem.get("session_id") or ""
        chapter = None
        if session_id:
            parts = [p for p in session_id.split("_") if p]
            if len(parts) > 1:
                chapter = string.capwords(" ".join(parts[1:6]))
        return {
            "memory_id": mem.get("memory_id") or mem.get("id") or "",
            "fact": mem.get("content", ""),
            "source": "You",
            "target": "",
            "source_type": "NPC",
            "target_type": metadata.get("category") or "Memory",
            "name": metadata.get("category") or "event",
            "chapters": [chapter] if chapter else [],
            "valid_at": mem.get("event_time"),
            "created_at": mem.get("created_at"),
        }

    def get_graph_data(self, npc_id: str) -> Dict[str, Any]:
        if not self.init_graphiti():
            return {"edges": [], "entity_count": 0}
        try:
            result = self._cognis.get_all(
                owner_id=npc_id,
                agent_id="npc_memory",
                limit=2000,
                include_historical=False,
            )
            memories = result.get("memories", []) if isinstance(result, dict) else []
            edges = [self._memory_to_edge(m) for m in memories if m.get("content")]
            edges.sort(key=lambda e: e.get("created_at") or "")
            return {"edges": edges, "entity_count": len(edges)}
        except Exception as e:
            print(f"[Memory/Cognis] Failed to load memory data for {npc_id}: {e}")
            return {"edges": [], "entity_count": 0}

    def get_node_uuid(self, npc_id: str, node_name: str) -> Optional[str]:
        return None

    def get_last_added_edges(self, npc_id: str) -> List[Dict[str, Any]]:
        return list(self._last_added_edges.get(npc_id, []))

    def search_facts(self, npc_id: str, query: str,
                     center_node_uuid: Optional[str] = None,
                     max_results: int = 5) -> List[Dict[str, str]]:
        if not self.init_graphiti():
            return []
        try:
            result = self._cognis.search(
                query=query,
                owner_id=npc_id,
                agent_id="npc_memory",
                session_id="sonorus",
                limit=max_results,
            )
            memories = result.get("results", []) if isinstance(result, dict) else []
            facts = []
            seen = set()
            for mem in memories:
                fact = (mem.get("content") or "").strip()
                key = fact.lower()
                if not fact or key in seen:
                    continue
                seen.add(key)
                metadata = mem.get("metadata") or {}
                session_id = mem.get("session_id") or ""
                chapter = None
                if session_id:
                    parts = [p for p in session_id.split("_") if p]
                    if len(parts) > 1:
                        chapter = string.capwords(" ".join(parts[1:6]))
                facts.append({
                    "memory_id": mem.get("memory_id") or mem.get("id") or "",
                    "fact": fact,
                    "source": "You",
                    "target": "",
                    "category": metadata.get("category") or "",
                    "chapter": chapter or "",
                    "valid_at": mem.get("event_time"),
                    "created_at": mem.get("created_at"),
                })
            print(f"[Memory/Cognis] Search '{query}': {len(facts)} facts")
            return facts
        except Exception as e:
            print(f"[Memory/Cognis] Search failed for {npc_id}: {e}")
            return []

    def delete_node(self, npc_id: str, node_name: str) -> Dict[str, Any]:
        return {
            "success": False,
            "error": "Node deletion is not supported by the lightweight fact memory backend.",
        }

    def delete_edge(
        self,
        npc_id: str,
        source_name: str,
        target_name: str,
        fact: Optional[str] = None,
        memory_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not fact and not memory_id:
            return {"success": False, "error": "Fact text or memory_id is required for lightweight memory deletion."}
        if not self.init_graphiti():
            return {"success": False, "error": "Memory not initialized"}
        try:
            if memory_id:
                result = self._cognis.delete(memory_id, owner_id=npc_id)
                if result.get("success"):
                    ChapterManager().invalidate_memory_cache(npc_id)
                    invalidate_bio(npc_id)
                elif not result.get("error") and result.get("message"):
                    result["error"] = result["message"]
                return result

            result = self._cognis.get_all(
                owner_id=npc_id,
                agent_id="npc_memory",
                limit=5000,
                include_historical=False,
            )
            target = fact.strip().lower()
            for mem in result.get("memories", []):
                if (mem.get("content") or "").strip().lower() == target:
                    result = self._cognis.delete(mem.get("memory_id"), owner_id=npc_id)
                    if result.get("success"):
                        ChapterManager().invalidate_memory_cache(npc_id)
                        invalidate_bio(npc_id)
                    elif not result.get("error") and result.get("message"):
                        result["error"] = result["message"]
                    return result
            return {"success": False, "error": "Fact not found"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def update_fact(self, npc_id: str, memory_id: str, fact: str, category: Optional[str] = None) -> Dict[str, Any]:
        if not memory_id:
            return {"success": False, "error": "memory_id is required for fact edits."}
        fact = (fact or "").strip()
        if not fact:
            return {"success": False, "error": "Fact text is required."}
        if not self.init_graphiti():
            return {"success": False, "error": "Memory not initialized"}
        try:
            result = self._cognis.update(
                memory_id=memory_id,
                owner_id=npc_id,
                content=fact,
                category=category,
            )
            if result.get("success"):
                ChapterManager().invalidate_memory_cache(npc_id)
                invalidate_bio(npc_id)
            return result
        except Exception as e:
            return {"success": False, "error": str(e)}

    def clear_graph(self, npc_id: str) -> Dict[str, Any]:
        if not self.init_graphiti():
            return {"success": False, "error": "Memory not initialized"}
        try:
            return self._cognis.clear(owner_id=npc_id)
        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_vector_migration_status(self) -> Dict[str, Any]:
        if not self.init_graphiti():
            return {"success": False, "error": "Memory not initialized"}
        try:
            status = self._cognis.vector_migration_status()
            status["success"] = True
            return status
        except Exception as e:
            return {"success": False, "error": str(e)}

    def migrate_vectors(self, progress_callback=None) -> Dict[str, Any]:
        if not self.init_graphiti():
            return {"success": False, "error": "Memory not initialized"}
        self._busy = True
        try:
            return self._cognis.rebuild_mismatched_vectors(progress_callback=progress_callback)
        except Exception as e:
            return {"success": False, "error": str(e)}
        finally:
            self._busy = False


# Keep existing call sites stable while exposing a backend-neutral name.
MemoryManager = CognisMemoryManager
GraphitiManager = CognisMemoryManager


# =============================================================================
# ChapterManager - Handles chapter detection and state
# =============================================================================

class ChapterManager:
    """
    Manages chapter detection and state per NPC.

    Storage format (chapters/{NpcId}.json):
    {
        "open_chapter": {
            "title": "...",
            "summary": "...",
            "location": "...",
            "start_timestamp": ..., # Unix timestamp (stable identifier)
            "start_date": "...",    # Game date for display
            "start_time": "...",    # Game time for display
            "key_events": [...],
            "context_messages": [...]
        },
        "closed_chapters": [
            {
                "title": "...",
                "summary": "...",
                "location": "...",
                "start_timestamp": ...,
                "end_timestamp": ...,
                "start_date": "...",
                "start_time": "...",
                "key_events": [...]
            },
            ...
        ]
    }

    NOTE: We use timestamps (not array indices) to identify chapter boundaries.
    This is stable regardless of filtering or pagination in the UI.
    """

    def __init__(self):
        self._chapters_dir = get_chapters_dir()

    def _get_chapter_file(self, npc_id: str) -> str:
        """Get path to NPC's chapter state file."""
        return os.path.join(self._chapters_dir, f"{npc_id}.json")

    def _get_memory_cache_file(self, npc_id: str) -> str:
        """Get path to NPC's cached memory prose file."""
        return os.path.join(self._chapters_dir, f"{npc_id}_memory.txt")

    def _load_chapter_data(self, npc_id: str) -> Dict:
        """Load full chapter data for an NPC."""
        try:
            path = self._get_chapter_file(npc_id)
            if os.path.exists(path):
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    # Migrate old format if needed
                    if 'status' in data:
                        # Old format - single chapter object
                        return self._migrate_old_format(data)
                    return data
        except Exception as e:
            print(f"[Memory] Error loading chapters for {npc_id}: {e}")
        return {"open_chapter": None, "closed_chapters": []}

    def _migrate_old_format(self, old_data: Dict) -> Dict:
        """Migrate old single-chapter format to new format (timestamps only, no indices)."""
        if old_data.get('status') == 'open':
            return {
                "open_chapter": {
                    "title": old_data.get('title', 'Untitled'),
                    "summary": old_data.get('summary', ''),
                    "location": old_data.get('location', 'Unknown'),
                    "start_timestamp": old_data.get('start_timestamp'),
                    "start_date": old_data.get('start_date', ''),
                    "start_time": old_data.get('start_time', ''),
                    "key_events": old_data.get('key_events', []),
                    "context_messages": old_data.get('context_messages', [])
                },
                "closed_chapters": []
            }
        else:
            # It was closed - add to closed_chapters
            return {
                "open_chapter": None,
                "closed_chapters": [{
                    "title": old_data.get('title', 'Untitled'),
                    "summary": old_data.get('summary', ''),
                    "location": old_data.get('location', 'Unknown'),
                    "start_timestamp": old_data.get('start_timestamp'),
                    "end_timestamp": old_data.get('end_timestamp'),
                    "start_date": old_data.get('start_date', ''),
                    "start_time": old_data.get('start_time', ''),
                    "key_events": old_data.get('key_events', [])
                }]
            }

    def _save_chapter_data(self, npc_id: str, data: Dict):
        """Save full chapter data for an NPC."""
        try:
            path = self._get_chapter_file(npc_id)
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"[Memory] Error saving chapters for {npc_id}: {e}")

    def get_open_chapter(self, npc_id: str) -> Optional[Dict]:
        """Get the currently open chapter for an NPC, if any."""
        data = self._load_chapter_data(npc_id)
        return data.get('open_chapter')

    def get_closed_chapters(self, npc_id: str) -> List[Dict]:
        """Get all closed chapters for an NPC."""
        data = self._load_chapter_data(npc_id)
        return data.get('closed_chapters', [])

    def get_all_chapters(self, npc_id: str) -> Dict:
        """Get both open and closed chapters for UI display."""
        return self._load_chapter_data(npc_id)

    def save_chapter_state(self, npc_id: str, chapter_data: Dict):
        """Save chapter state for an NPC (legacy compatibility)."""
        # Convert to new format (timestamps only, no indices)
        if chapter_data.get('status') == 'open':
            data = self._load_chapter_data(npc_id)
            data['open_chapter'] = {
                "title": chapter_data.get('title', 'Untitled'),
                "summary": chapter_data.get('summary', ''),
                "location": chapter_data.get('location', 'Unknown'),
                "start_timestamp": chapter_data.get('start_timestamp'),
                "start_date": chapter_data.get('start_date', ''),
                "start_time": chapter_data.get('start_time', ''),
                "key_events": chapter_data.get('key_events', []),
                "context_messages": chapter_data.get('context_messages', [])
            }
            self._save_chapter_data(npc_id, data)
        else:
            # Closed - this shouldn't happen through legacy path
            self._save_chapter_data(npc_id, self._migrate_old_format(chapter_data))

    def start_chapter(self, npc_id: str, title: str, location: str,
                      game_date: str, game_time: str, summary: str = "",
                      start_timestamp: int = None,
                      key_events: List[Dict] = None, context_messages: List[Dict] = None) -> Dict:
        """Start a new chapter for an NPC."""
        data = self._load_chapter_data(npc_id)

        chapter = {
            "title": title,
            "summary": summary,
            "location": location,
            "start_timestamp": start_timestamp or int(datetime.now().timestamp()),
            "start_date": game_date,
            "start_time": game_time,
            "key_events": key_events or [],
            "context_messages": context_messages or []
        }

        data['open_chapter'] = chapter
        self._save_chapter_data(npc_id, data)
        print(f"[Memory] Started chapter '{title}' for {npc_id} at ts {chapter['start_timestamp']}")
        return chapter

    def close_chapter(self, npc_id: str, summary: str,
                      end_timestamp: int = None, key_events: List[Dict] = None) -> Optional[Dict]:
        """Close the current chapter and add to closed_chapters list."""
        data = self._load_chapter_data(npc_id)
        open_chapter = data.get('open_chapter')

        if not open_chapter:
            return None

        # Build closed chapter record (timestamps only, no indices)
        closed_chapter = {
            "title": open_chapter.get('title', 'Untitled'),
            "summary": summary or open_chapter.get('summary', ''),
            "location": open_chapter.get('location', 'Unknown'),
            "start_timestamp": open_chapter.get('start_timestamp'),
            "end_timestamp": end_timestamp or int(datetime.now().timestamp()),
            "start_date": open_chapter.get('start_date', ''),
            "start_time": open_chapter.get('start_time', ''),
            "key_events": key_events or open_chapter.get('key_events', [])
        }

        # Add to closed chapters list
        closed_chapters = data.get('closed_chapters', [])
        closed_chapters.append(closed_chapter)

        # Clear open chapter
        data['open_chapter'] = None
        data['closed_chapters'] = closed_chapters

        self._save_chapter_data(npc_id, data)

        # Invalidate memory cache
        self.invalidate_memory_cache(npc_id)

        print(f"[Memory] Closed chapter '{closed_chapter['title']}' for {npc_id} "
              f"(ts {closed_chapter['start_timestamp']}-{closed_chapter['end_timestamp']})")
        return closed_chapter

    def add_events_to_chapter(self, npc_id: str, events: List[Dict]) -> bool:
        """Add events to the current open chapter without closing it."""
        data = self._load_chapter_data(npc_id)
        open_chapter = data.get('open_chapter')

        if not open_chapter:
            return False

        existing_events = open_chapter.get('key_events', [])
        existing_events.extend(events)
        open_chapter['key_events'] = existing_events

        data['open_chapter'] = open_chapter
        self._save_chapter_data(npc_id, data)
        print(f"[Memory] Added {len(events)} events to chapter '{open_chapter['title']}' for {npc_id}")
        return True

    def update_chapter_context(self, npc_id: str, new_messages: List[Dict]) -> bool:
        """Update context messages for the current open chapter."""
        data = self._load_chapter_data(npc_id)
        open_chapter = data.get('open_chapter')

        if not open_chapter:
            return False

        context = open_chapter.get('context_messages', [])
        context.extend(new_messages)
        # Keep only last 20 context messages
        open_chapter['context_messages'] = context[-20:]

        data['open_chapter'] = open_chapter
        self._save_chapter_data(npc_id, data)
        return True

    def get_cached_memory(self, npc_id: str) -> Optional[str]:
        """Get cached memory prose for an NPC, if available."""
        try:
            path = self._get_memory_cache_file(npc_id)
            if os.path.exists(path):
                with open(path, 'r', encoding='utf-8') as f:
                    return f.read()
        except Exception as e:
            print(f"[Memory] Error loading cached memory for {npc_id}: {e}")
        return None

    def cache_memory(self, npc_id: str, prose: str):
        """Cache compiled memory prose for an NPC."""
        try:
            path = self._get_memory_cache_file(npc_id)
            with open(path, 'w', encoding='utf-8') as f:
                f.write(prose)
        except Exception as e:
            print(f"[Memory] Error caching memory for {npc_id}: {e}")

    def invalidate_memory_cache(self, npc_id: str):
        """Invalidate cached memory for an NPC."""
        try:
            path = self._get_memory_cache_file(npc_id)
            if os.path.exists(path):
                os.remove(path)
                print(f"[Memory] Invalidated memory cache for {npc_id}")
        except Exception as e:
            print(f"[Memory] Error invalidating cache for {npc_id}: {e}")

    def get_indexing_meta(self, npc_id: str) -> Dict:
        """Get indexing metadata for an NPC (last index timestamp)."""
        data = self._load_chapter_data(npc_id)
        return data.get('indexing_meta', {})

    def update_indexing_meta(self, npc_id: str, timestamp: int):
        """Update indexing metadata after successful chapter detection."""
        data = self._load_chapter_data(npc_id)
        data['indexing_meta'] = {
            'last_index_timestamp': timestamp
        }
        self._save_chapter_data(npc_id, data)
        print(f"[Memory] Updated indexing meta for {npc_id}: last_index_timestamp={timestamp}")

    def _clear_chapter_tracking_files(self, npc_id: str):
        """Delete chapter-state files without touching the NPC's bio."""
        try:
            chapter_path = self._get_chapter_file(npc_id)
            if os.path.exists(chapter_path):
                os.remove(chapter_path)
                print(f"[Memory] Deleted chapter tracking for {npc_id}")

            cache_path = self._get_memory_cache_file(npc_id)
            if os.path.exists(cache_path):
                os.remove(cache_path)
                print(f"[Memory] Deleted memory cache for {npc_id}")

            content_dir = os.path.join(_memory_data_dir, "chapter_content")
            if os.path.exists(content_dir):
                prefix = f"{npc_id}_"
                for filename in os.listdir(content_dir):
                    if filename.startswith(prefix) and filename.endswith('.json'):
                        filepath = os.path.join(content_dir, filename)
                        os.remove(filepath)
                        print(f"[Memory] Deleted chapter content: {filename}")
        except Exception as e:
            print(f"[Memory] Error clearing chapter tracking for {npc_id}: {e}")

    def reset_chapter_tracking(self, npc_id: str):
        """Clear chapter/indexing state for an NPC without deleting the bio."""
        self._clear_chapter_tracking_files(npc_id)
        self.invalidate_memory_cache(npc_id)

    def repair_after_in_place_history_edit(self, npc_id: str, last_timestamp: int):
        """
        Repair chapter state after an in-place history edit where row IDs remain stable.

        We preserve queue/indexing progress and only clean up chapter artifacts that can
        no longer be trusted after manual edits.
        """
        data = self._load_chapter_data(npc_id)
        has_real_chapters = bool(data.get('open_chapter') or data.get('closed_chapters'))

        if not has_real_chapters:
            # Preserve indexing progress for unchaptered NPCs so the next real batch is not
            # misclassified as "old untracked data" and skipped.
            self.invalidate_memory_cache(npc_id)
            print(f"[Memory] Preserved indexing state for {npc_id} after in-place history edit")
            return

        if last_timestamp <= 0:
            # No history remains for this NPC; clear chapter artifacts but keep the bio.
            self.reset_chapter_tracking(npc_id)
            print(f"[Memory] Cleared chapter tracking for {npc_id} after in-place history edit")
            return

        # Manual edits can invalidate the in-progress chapter structure.
        if data.get('open_chapter'):
            data['open_chapter'] = None

        closed_chapters = []
        for chapter in data.get('closed_chapters', []):
            start_ts = chapter.get('start_timestamp') or 0
            end_ts = chapter.get('end_timestamp') or 0

            # Drop chapters that now lie wholly beyond the remaining history tail.
            if start_ts > last_timestamp or end_ts > last_timestamp:
                continue

            closed_chapters.append(chapter)

        data['closed_chapters'] = closed_chapters
        self._save_chapter_data(npc_id, data)
        self.invalidate_memory_cache(npc_id)
        print(f"[Memory] Repaired chapter state for {npc_id} after in-place history edit")

    def repair_after_history_rewrite(self, npc_id: str, last_timestamp: int):
        """
        Repair chapter tracking after a bulk dialogue-history rewrite.

        This is intentionally conservative:
        - Drop the open chapter, since arbitrary rewrites can invalidate it
        - Drop any closed chapters that now lie entirely beyond remaining history
        - Advance indexing_meta to the end of remaining history so old content is
          not auto-reprocessed into duplicate memories
        """
        data = self._load_chapter_data(npc_id)
        has_real_chapters = bool(data.get('open_chapter') or data.get('closed_chapters'))

        if not has_real_chapters:
            self.reset_chapter_tracking(npc_id)
            print(f"[Memory] Cleared stale chapter tracking for {npc_id} after history rewrite")
            return

        # Manual edits can invalidate the in-progress chapter structure.
        if data.get('open_chapter'):
            data['open_chapter'] = None

        closed_chapters = []
        for chapter in data.get('closed_chapters', []):
            start_ts = chapter.get('start_timestamp') or 0
            end_ts = chapter.get('end_timestamp') or 0

            # Drop chapters that now lie wholly beyond the edited history tail.
            if start_ts > last_timestamp or end_ts > last_timestamp:
                continue

            closed_chapters.append(chapter)

        data['closed_chapters'] = closed_chapters
        data['indexing_meta'] = {
            'last_index_timestamp': last_timestamp
        }

        self._save_chapter_data(npc_id, data)
        self.invalidate_memory_cache(npc_id)
        print(f"[Memory] Repaired chapter state for {npc_id} after history rewrite; last_index_timestamp={last_timestamp}")

    def repair_after_history_edit(self, npc_id: str, last_timestamp: int):
        """Backward-compatible alias for the older, rewrite-style repair path."""
        self.repair_after_history_rewrite(npc_id, last_timestamp)

    def clear_npc_data(self, npc_id: str) -> bool:
        """Clear all chapter data, memory cache, bio, and chapter content for an NPC."""
        try:
            self._clear_chapter_tracking_files(npc_id)

            # Delete bio file
            bio_path = get_bio_path(npc_id)
            if os.path.exists(bio_path):
                os.remove(bio_path)
                print(f"[Memory] Deleted bio for {npc_id}")

            return True
        except Exception as e:
            print(f"[Memory] Error clearing NPC data for {npc_id}: {e}")
            return False


# =============================================================================
# NPC Bio System
# =============================================================================

def get_bios_dir() -> str:
    """Get the bios directory path, creating it if needed."""
    bios_dir = os.path.join(_memory_data_dir, "npc_bios")
    os.makedirs(bios_dir, exist_ok=True)
    return bios_dir


def get_bio_path(npc_id: str) -> str:
    """Get path to an NPC's bio file."""
    return os.path.join(get_bios_dir(), f"{npc_id}.json")


def load_bio(npc_id: str) -> Optional[Dict]:
    """Load an NPC's bio, returning None if not found."""
    path = get_bio_path(npc_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"[Bio] Error loading bio for {npc_id}: {e}")
        return None


def save_bio(npc_id: str, bio: Dict) -> bool:
    """Save an NPC's bio atomically (write to temp, then rename)."""
    path = get_bio_path(npc_id)
    temp_path = path + ".tmp"
    try:
        bio['last_updated'] = int(datetime.now().timestamp())
        with open(temp_path, 'w', encoding='utf-8') as f:
            json.dump(bio, f, indent=2, ensure_ascii=False)
        os.replace(temp_path, path)  # Atomic rename
        return True
    except Exception as e:
        print(f"[Bio] Error saving bio for {npc_id}: {e}")
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except:
                pass
        return False


def invalidate_bio(npc_id: str) -> bool:
    """Delete a generated bio when its source facts have changed."""
    path = get_bio_path(npc_id)
    if not os.path.exists(path):
        return False
    try:
        os.remove(path)
        print(f"[Bio] Invalidated generated bio for {npc_id}")
        return True
    except Exception as e:
        print(f"[Bio] Error invalidating bio for {npc_id}: {e}")
        return False


def format_bio_for_context(bio: Dict) -> str:
    """Format a bio dict as compact context for NPC prompts."""
    if not bio:
        return ""

    sections = []

    # Personality + Preferences combined
    traits = []
    if bio.get("personality"):
        traits.extend(bio["personality"][:3])  # Max 3
    if bio.get("preferences"):
        traits.extend(bio["preferences"][:3])  # Max 3
    if traits:
        sections.append("### Who You Are\n" + "\n".join(f"- {t}" for t in traits))

    # Ongoing tasks
    if bio.get("ongoing_tasks"):
        tasks = []
        for t in bio["ongoing_tasks"][:3]:  # Max 3
            task_str = f"- {t.get('task', 'Unknown task')}"
            if t.get('context'):
                task_str += f" ({t['context']})"
            tasks.append(task_str)
        sections.append("### Current Tasks\n" + "\n".join(tasks))

    # Recent completed tasks (last 2)
    if bio.get("completed_tasks"):
        recent = bio["completed_tasks"][-2:]  # Last 2
        tasks = [f"- {t.get('task', 'Unknown task')} ({t.get('outcome', 'completed')})" for t in recent]
        sections.append("### Recently Completed\n" + "\n".join(tasks))

    # Key relationships
    if bio.get("relationships"):
        rels = []
        for name, data in list(bio["relationships"].items())[:4]:  # Max 4
            if isinstance(data, dict):
                rels.append(f"- {name}: {data.get('notes', data.get('type', 'known'))}")
            else:
                rels.append(f"- {name}: {data}")
        sections.append("### Key Relationships\n" + "\n".join(rels))

    # Motivations
    if bio.get("motivations"):
        motivs = bio["motivations"][:2]  # Max 2
        sections.append("### What Drives You\n" + "\n".join(f"- {m}" for m in motivs))

    # User notes (from editor's guidance)
    if bio.get("user_notes"):
        notes = bio["user_notes"][:3]  # Max 3
        sections.append("### Additional Notes\n" + "\n".join(f"- {n}" for n in notes))

    return "\n\n".join(sections)


def _build_static_background_sections(npc_id: str, npc_name: str = None,
                                      player_name: str = "Player",
                                      settings=None,
                                      include_player: bool = True) -> List[str]:
    """Build additive static background sections for NPC and player."""
    settings = settings or load_settings()
    sections = []

    npc_static_bio = get_static_bio(npc_id=npc_id, display_name=npc_name, settings=settings)
    if npc_static_bio:
        sections.append(f"### About You\n**Background:** {npc_static_bio}")

    player_static_bio = get_player_static_bio(player_name=player_name, settings=settings)
    if include_player and player_static_bio:
        sections.append(f"### About {player_name}\n**Background:** {player_static_bio}")

    return sections


def get_character_background_context(npc_id: str, npc_name: str = None,
                                     player_name: str = "Player") -> Optional[str]:
    """Get shared NPC background context for direct chat/mail flows."""
    settings = load_settings()

    if is_npc_memory_effectively_enabled(npc_id, settings=settings):
        memory_block = get_contextual_memory(
            npc_id=npc_id,
            npc_name=npc_name,
            player_name=player_name,
            current_location=None,
            nearby_npcs=None,
            mentioned_entities=None,
        )
        if memory_block:
            return memory_block

    sections = _build_static_background_sections(
        npc_id=npc_id,
        npc_name=npc_name,
        player_name=player_name,
        settings=settings,
        include_player=True,
    )
    if sections:
        return "\n\n".join(sections)

    guidance = get_editor_guidance(npc_id=npc_id, display_name=npc_name, settings=settings)
    if guidance:
        return f"### About You\n{guidance}"
    return None


def _build_static_bio_generation_section(npc_id: str, npc_name: str = None, settings=None) -> str:
    """Build static-bio grounding for generated bio prompts."""
    settings = settings or load_settings()
    static_bio = get_static_bio(npc_id=npc_id, display_name=npc_name, settings=settings)
    if not static_bio:
        return ""

    return f"""
STATIC BIO:
{static_bio}

When generating or updating the memory bio:
- Treat the STATIC BIO section as immutable background
- Do NOT repeat facts, details, or traits that are already stated in the STATIC BIO section
- Prefer dynamic developments, relationships, tasks, recent changes, and memory-derived details that add to the STATIC BIO section
- Do not copy text from the STATIC BIO section into user_notes unless a guidance-specific note truly belongs there

"""


def generate_full_bio(npc_id: str, npc_name: str, editor_guidance: str = None) -> Optional[Dict]:
    """
    Generate a complete bio from all graph data (for migration/rebuild).

    Args:
        npc_id: NPC's internal ID
        npc_name: NPC's display name
        editor_guidance: Optional character essence/guidance from user to incorporate

    Returns the bio dict, or None if generation fails.
    """
    import llm
    settings = load_settings()
    if not is_npc_long_term_memory_enabled(npc_id, settings=settings):
        print(f"[Bio] Long-term memory disabled for {npc_id}, skipping full bio generation")
        return None

    memory_mgr = MemoryManager()
    if not memory_mgr.init_graphiti():
        print(f"[Bio] Could not initialize memory backend for {npc_id}")
        return None

    # Get all facts
    graph_data = memory_mgr.get_graph_data(npc_id)
    edges = graph_data.get('edges', [])
    if not edges:
        print(f"[Bio] No edges found for {npc_id}")
        return None

    # Format facts by chapter
    formatted = format_graph_for_context(edges, npc_name)

    static_bio_section = _build_static_bio_generation_section(
        npc_id=npc_id,
        npc_name=npc_name,
        settings=settings,
    )
    static_bio_rule = ""
    if static_bio_section:
        static_bio_rule = '\n9. Keep the generated bio lean by avoiding facts, details, or traits already covered in the "STATIC BIO" section'

    # Build guidance section if provided
    guidance_section = ""
    if editor_guidance:
        guidance_section = f"""
EDITOR'S GUIDANCE (character essence to preserve):
{editor_guidance}

When generating the bio:
- Incorporate guidance that is STILL CONSISTENT with the facts
- If facts contradict guidance, facts take precedence
- Put guidance elements that don't fit other categories in "user_notes"

"""

    # Call LLM to generate bio
    prompt = f"""Analyze these facts about {npc_name} and create a structured bio.
{static_bio_section}
{guidance_section}
FACTS:
{formatted}

Generate a JSON bio with these exact fields:
- "preferences": List of significant likes/dislikes/opinions (strings)
- "personality": List of observed personality traits (strings)
- "ongoing_tasks": List of {{"task": str, "start_date": str, "context": str}} for incomplete tasks
- "completed_tasks": List of {{"task": str, "start_date": str, "end_date": str, "outcome": str}}
- "relationships": Dict of name -> {{"type": str, "notes": str}} for SIGNIFICANT relationships only
- "motivations": List of observed goals/drives (strings)
- "user_notes": List of additional context from editor's guidance that doesn't fit above categories (strings)

RULES:
1. Only include SIGNIFICANT items - skip one-off neutral interactions
2. Use dates from chapter titles when available (e.g., "Dec 3, 1890")
3. For relationships, require meaningful interaction (helped, argued, traveled together - not just "saw them")
4. Keep each item concise (1-2 sentences max)
5. If unsure about task completion status, mark as ongoing
6. Return ONLY valid JSON, no extra text
7. FACTUAL ACCURACY - CRITICAL:
   - Task names must describe the LITERAL ACTION, not an interpretation of its effect
   - "Retrieved X for Y" is correct. "Helped Y overcome their fear" is WRONG (unless explicitly stated)
   - Do not infer character growth, emotional change, or narrative meaning
   - If the facts say "A did X for B because B was scared", the task is "Did X for B", NOT "Helped B overcome fear"
8. For user_notes: Include guidance items that are still valid but don't fit structured categories{static_bio_rule}

```json
"""

    model = _get_memory_llm_model(settings.get('memory', {}), 'prose_model')

    parsed = call_llm_with_retry(prompt, model, max_retries=3, context="bio_generation")
    if not parsed:
        print(f"[Bio] LLM failed to generate bio for {npc_id}")
        return None

    # Build complete bio structure
    bio = {
        "npc_id": npc_id,
        "npc_name": npc_name,
        "preferences": parsed.get("preferences", []),
        "personality": parsed.get("personality", []),
        "ongoing_tasks": parsed.get("ongoing_tasks", []),
        "completed_tasks": parsed.get("completed_tasks", []),
        "relationships": parsed.get("relationships", {}),
        "motivations": parsed.get("motivations", []),
        "user_notes": parsed.get("user_notes", [])
    }

    # Save and return
    if save_bio(npc_id, bio):
        print(f"[Bio] Generated full bio for {npc_name}")
        return bio
    return None


def update_bio_incremental(npc_id: str, npc_name: str,
                           chapter_start_ts: int, chapter_end_ts: int) -> Optional[Dict]:
    """
    Update an NPC's bio incrementally after a chapter closes.

    Uses facts extracted from the recently closed chapter,
    then uses LLM to update only affected bio sections.
    Incorporates editor's guidance from settings if available.
    """
    import llm
    settings = load_settings()
    if not is_npc_long_term_memory_enabled(npc_id, settings=settings):
        print(f"[Bio] Long-term memory disabled for {npc_id}, skipping incremental bio update")
        return None

    # Load existing bio
    bio = load_bio(npc_id)
    if not bio:
        print(f"[Bio] No existing bio for {npc_id}, skipping incremental update")
        return None

    # Get facts from long-term memory.
    memory_mgr = MemoryManager()
    if not memory_mgr.init_graphiti():
        return bio

    new_edges = memory_mgr.get_last_added_edges(npc_id)

    # Fallback for rebuild paths where the update is not called immediately
    # after add_episode().
    graph_data = memory_mgr.get_graph_data(npc_id)
    edges = graph_data.get('edges', [])
    if not new_edges:
        for edge in edges:
            created = edge.get('created_at')
            if created:
                try:
                    from dateutil.parser import parse as parse_date
                    edge_ts = int(parse_date(created).timestamp())
                    if chapter_start_ts <= edge_ts <= chapter_end_ts + 300:
                        new_edges.append(edge)
                except Exception:
                    pass

    if not new_edges:
        print(f"[Bio] No new memory facts for {npc_id} in chapter timeframe ({chapter_start_ts}-{chapter_end_ts})")
        return bio

    print(f"[Bio] Found {len(new_edges)} new memory facts for {npc_id}")

    # Format new facts
    new_facts = "\n".join(f"- {e.get('fact', '')}" for e in new_edges)

    # Load editor's guidance from settings
    editor_guidance = get_editor_guidance(npc_id=npc_id, display_name=npc_name, settings=settings)
    static_bio_section = _build_static_bio_generation_section(
        npc_id=npc_id,
        npc_name=npc_name,
        settings=settings,
    )
    static_bio_rule = ""
    if static_bio_section:
        static_bio_rule = '\n8. Remove or avoid adding entries that repeat facts, details, or traits already covered in the "STATIC BIO" section'

    guidance_section = ""
    if editor_guidance:
        guidance_section = f"""
EDITOR'S GUIDANCE (character essence to preserve):
{editor_guidance}

When updating, ensure user_notes section preserves guidance items that are still valid.

"""

    # Call LLM to update bio
    prompt = f"""Update this NPC bio based on new facts from a recent conversation.
{static_bio_section}
{guidance_section}
CURRENT BIO:
```json
{json.dumps(bio, indent=2)}
```

NEW FACTS:
{new_facts}

For EACH bio section, determine if it needs updating:
- "append": Add new items (new preference, new relationship, etc.)
- "replace": Rewrite section (task completed, opinion changed, contradiction)
- "no_change": Section unaffected by new facts

Return JSON with this structure:
{{
  "preferences": {{"action": "append|replace|no_change", "items": [...]}},
  "personality": {{"action": "append|replace|no_change", "items": [...]}},
  "ongoing_tasks": {{"action": "append|replace|no_change", "items": [...]}},
  "completed_tasks": {{"action": "append|replace|no_change", "items": [...]}},
  "relationships": {{"action": "append|replace|no_change", "items": {{}}}},
  "motivations": {{"action": "append|replace|no_change", "items": [...]}},
  "user_notes": {{"action": "append|replace|no_change", "items": [...]}}
}}

RULES:
1. Only update sections with relevant new information
2. When a task completes, MOVE it from ongoing_tasks to completed_tasks (replace both)
3. Keep items concise (1-2 sentences)
4. For relationships, "items" should be a dict like {{"Name": {{"type": "...", "notes": "..."}}}}
5. Return ONLY valid JSON
6. FACTUAL ACCURACY: Describe only what literally happened. Do not embellish or infer meaning beyond stated facts.
7. Preserve user_notes that are still valid; remove any contradicted by new facts{static_bio_rule}

```json
"""

    model = _get_memory_llm_model(settings.get('memory', {}), 'prose_model')

    updates = call_llm_with_retry(prompt, model, max_retries=2, context="bio_update")
    if not updates:
        print(f"[Bio] LLM failed to update bio for {npc_id}")
        return bio

    # Apply updates to bio
    changes_made = False
    for section in ["preferences", "personality", "ongoing_tasks", "completed_tasks", "motivations", "user_notes"]:
        update = updates.get(section, {})
        action = update.get("action", "no_change")
        items = update.get("items", [])

        if action == "append" and items:
            bio[section] = bio.get(section, []) + items
            changes_made = True
        elif action == "replace":
            if bio.get(section, []) != items:
                bio[section] = items
                changes_made = True

    # Handle relationships separately (dict merge)
    rel_update = updates.get("relationships", {})
    if rel_update.get("action") == "append" and rel_update.get("items"):
        bio["relationships"] = {**bio.get("relationships", {}), **rel_update["items"]}
        changes_made = True
    elif rel_update.get("action") == "replace":
        new_relationships = rel_update.get("items", {})
        if bio.get("relationships", {}) != new_relationships:
            bio["relationships"] = new_relationships
            changes_made = True

    # Save updated bio
    if changes_made:
        if save_bio(npc_id, bio):
            print(f"[Bio] Updated bio for {npc_name}")
    else:
        print(f"[Bio] No changes needed for {npc_name}")

    return bio


# =============================================================================
# Chapter Detection
# =============================================================================

def build_chapter_prompt(npc_name: str, npc_id: str, dialogue_entries: List[Dict],
                         open_chapter: Optional[Dict], characters_in_earshot: List[str] = None) -> str:
    """Build the prompt for chapter boundary detection."""
    include_cutscene = load_settings().get('memory', {}).get('include_cutscene', True)

    dialogue_entries = [
        entry for entry in dialogue_entries
        if should_include_entry_for_npc_chaptering(npc_id, entry, include_cutscene=include_cutscene)
    ]

    # Get player name from entries if available
    player_name = "the student"
    for entry in dialogue_entries:
        if entry.get('isPlayer') and entry.get('speaker'):
            player_name = entry['speaker']
            break

    # Build character list
    characters_section = ""
    if characters_in_earshot:
        char_list = "\n".join([f"- {c}" for c in characters_in_earshot])
        characters_section = f"""## Characters {npc_name} knows about:
{char_list}
"""

    # Format new messages with timestamps
    # IMPORTANT: Include Unix timestamp so LLM can reference exact values in its response
    formatted_entries = []
    for i, entry in enumerate(dialogue_entries[-50:]):  # Last 50 entries
        timestamp = entry.get('timestamp') or 0
        game_time = entry.get('gameTime') or ''
        game_date = entry.get('gameDate') or ''
        speaker = entry.get('speaker') or 'Unknown'
        text = entry.get('text') or ''
        entry_type = entry.get('type') or 'dialogue'

        # Show both Unix timestamp AND game time so LLM knows the exact timestamp to use
        game_time_str = f"{game_date} {game_time}".strip() if game_date or game_time else ""
        time_prefix = f"[ts:{timestamp}]" if timestamp else ""
        if game_time_str:
            time_prefix = f"{time_prefix} [{game_time_str}]" if time_prefix else f"[{game_time_str}]"

        if entry_type == 'location':
            continue
        elif entry_type == 'combat':
            combat_summary = format_combat_summary(entry, include_damage=False)
            formatted_entries.append(f"{time_prefix} Combat: {combat_summary}")
        else:
            role = "Player" if entry.get('isPlayer') else speaker
            formatted_entries.append(f"{time_prefix} {role}: {text}")

    entries_str = "\n".join(formatted_entries) if formatted_entries else "No recent events."

    # Build open chapter context with context messages
    if open_chapter:
        context_messages = open_chapter.get('context_messages', [])
        context_str = ""
        if context_messages:
            ctx_lines = []
            for msg in context_messages[-10:]:  # Last 10 from open chapter
                ctx_lines.append(f"[{msg.get('time', '')}] {msg.get('speaker', '')}: {msg.get('text', '')}")
            context_str = f"""
### Context Messages from Current Chapter
{chr(10).join(ctx_lines)}
"""

        open_chapter_section = f"""## Current Open Chapter
Title: "{open_chapter.get('title', 'Untitled')}"
Started: {open_chapter.get('start_date', '')} {open_chapter.get('start_time', '')}
Location: {open_chapter.get('location', 'Unknown')}
Summary: {open_chapter.get('summary', 'Chapter in progress...')}
{context_str}"""

        task_section = f"""## Task

Analyze the new messages to determine chapter boundaries.

For the currently open chapter, decide whether to:
1. Close it (if there's a natural break - location change, time skip, plot resolution)
2. Continue it (keep it open)

Then identify any new chapters in the remaining messages.

Each dialogue entry includes a Unix timestamp prefix like `[ts:1768035550]`. Use these exact values for any timestamp fields in your response.

Return your analysis as JSON:

```json
{{
  "current_chapter_action": "close" or "continue",
  "close_at_timestamp": number (use the ts: value from the dialogue entry),
  "additional_events": [
    {{
      "title": "Event Title (max 32 chars)",
      "summary": "{npc_name} and [character] do action, while event occurs, leading to outcome.",
      "characters": ["Character Full Name"]
    }}
  ],
  "new_chapters": [
    {{
      "title": "Chapter title (2-6 words)",
      "start_timestamp": number,
      "end_timestamp": number or null (null if should remain open),
      "status": "closed" or "open",
      "summary": "Brief 1-2 sentence chapter summary from {npc_name}'s perspective",
      "key_events": [
        {{
          "title": "Event Title",
          "summary": "{npc_name} and [character] do action, while event occurs, leading to outcome.",
          "characters": ["Character Full Name"]
        }}
      ]
    }}
  ]
}}
```"""

        example_section = f"""## Example Outputs

### Example 1: Closing current chapter and starting new one
```json
{{
  "current_chapter_action": "close",
  "close_at_timestamp": 1768035500,
  "additional_events": [
    {{
      "title": "Lock Blocks Rescue",
      "summary": "{npc_name} and {player_name} discover the portrait is behind a level 2 lock, while Ashwinders patrol nearby, leading to abandoning the rescue attempt.",
      "characters": ["{player_name}", "Ferdinand Pratt"]
    }}
  ],
  "new_chapters": [
    {{
      "title": "Regrouping After Failure",
      "start_timestamp": 1768035550,
      "end_timestamp": null,
      "status": "open",
      "summary": "After the failed rescue, {npc_name} and {player_name} discuss what blocked Ferdinand Pratt's rescue.",
      "key_events": []
    }}
  ]
}}
```

### Example 2: Continuing current chapter
```json
{{
  "current_chapter_action": "continue",
  "additional_events": [
    {{
      "title": "Ashwinders Defeated",
      "summary": "{npc_name} watches {player_name} defeat the Ashwinder guards, while dark magic crackles through the ruins, leading to access to the inner chamber.",
      "characters": ["{player_name}"]
    }}
  ],
  "new_chapters": []
}}
```"""

    else:
        open_chapter_section = "## No Open Chapter\nThis analysis will start a new chapter."

        task_section = f"""## Task

Identify chapter boundaries in the messages and create chapters.

Each dialogue entry includes a Unix timestamp prefix like `[ts:1768035550]`. Use these exact values for any timestamp fields in your response.

Return your analysis as JSON:

```json
{{
  "new_chapters": [
    {{
      "title": "Chapter title (2-6 words)",
      "start_timestamp": number (use the ts: value from the first dialogue entry in this chapter),
      "end_timestamp": number or null (use the ts: value from the last entry, or null if chapter should remain open),
      "status": "closed" or "open",
      "summary": "Brief 1-2 sentence chapter summary from {npc_name}'s perspective",
      "key_events": [
        {{
          "title": "Event Title (max 32 chars)",
          "summary": "{npc_name} and [character] do action, while event occurs, leading to outcome.",
          "characters": ["Character Full Name"]
        }}
      ]
    }}
  ]
}}
```"""

        example_section = f"""## Example Output

```json
{{
  "new_chapters": [
    {{
      "title": "The Rescue Mission",
      "start_timestamp": 1768030000,
      "end_timestamp": 1768035000,
      "status": "closed",
      "summary": "{npc_name} accompanies {player_name} to Marunweem ruins to rescue Ferdinand Pratt's portrait from Ashwinders.",
      "key_events": [
        {{
          "title": "Ashwinder Ambush",
          "summary": "{npc_name} and {player_name} fight through Ashwinder guards, while dark wizards attempt to stop them, leading to reaching the inner ruins.",
          "characters": ["{player_name}"]
        }}
      ]
    }},
    {{
      "title": "Blocked Rescue",
      "start_timestamp": 1768035100,
      "end_timestamp": null,
      "status": "open",
      "summary": "Unable to open the locked door, {npc_name} and {player_name} leave Ferdinand Pratt's portrait rescue unresolved.",
      "key_events": []
    }}
  ]
}}
```"""

    guidelines = f"""## Guidelines

1. **Chapter Length**: Chapters typically span 20-50 dialogue exchanges, but follow narrative flow over strict counts

2. **Natural Breaks**: Look for:
   - Scene transitions (location changes, time skips)
   - Major plot developments or revelations
   - Shifts in conversation focus or tone
   - Resolution of conflicts or story arcs
   - Introduction of new characters or elements

3. **Open Chapters**: Leave the last chapter open if the story is ongoing

4. **Title Quality**:
   - Engaging and hint at content without spoilers
   - 2-6 words typically
   - Capture the essence or mood of what happened

5. **Key Events**: 0-3 plot-critical moments that directly and significantly alter the story state

   **Only save events that represent**:
   - **Story Initiation**: The core event starting a major plot thread or quest
   - **Critical Revelations**: Discovery of information ESSENTIAL to understanding the story (e.g., true identity, hidden purpose, crucial weakness)
   - **Major State Changes**: Actions that fundamentally alter the situation (e.g., alliance formed, enemy defeated, location discovered)
   - **Decisive Turning Points**: Moments where the story trajectory significantly shifts
   - **Resolution Events**: Definitive conclusions to story arcs or major obstacles

   **Do NOT save**:
   - Routine conversations or minor disagreements
   - Small character interactions or basic dialogue
   - Incremental progress or minor discoveries
   - Emotional reactions without plot consequences
   - Procedural actions (walking, looking, basic questioning)
   - Routine location changes, travel transitions, entering places, exiting places, or route descriptions
   - Generic greetings or farewells

   - Each event title: max 32 characters
   - Summary format: "{npc_name} and [character] do action, while event occurs, leading to outcome."
   - Keep summaries factual and action-focused (max 264 characters)
   - Always use specific character names when known
   - Only include named characters in the characters array

6. **Summary**: 1-2 sentences capturing the main thread from {npc_name}'s perspective"""

    return f"""You are analyzing {npc_name}'s experiences to identify natural chapter breaks and create meaningful titles.

Important: This is from {npc_name}'s perspective. When writing summaries and describing events, write from their point of view using their name.
Do not give {npc_name} false agency. If {npc_name} only overheard or witnessed {player_name} talking to another character, say {npc_name} witnessed/overheard that exchange or omit it. Do not say {npc_name} planned, arranged, accepted, purchased, rescued, or conferred unless {npc_name} personally did so.

The player character is named "{player_name}".

{characters_section}
{open_chapter_section}

## New Events to Analyze
{entries_str}

{task_section}

{guidelines}

{example_section}

Return only the JSON response with no additional explanation."""


# =============================================================================
# Episode Content Generation
# =============================================================================

def build_episode_prompt(npc_name: str, chapter_title: str, chapter_summary: str,
                         key_events: List[Dict], location: str,
                         dialogue_entries: List[Dict], player_name: str = "the student") -> str:
    """
    Build prompt for generating episode content optimized for graph storage/retrieval.

    This generates rich prose that Cognis will extract into deduped facts.
    """

    # Format dialogue entries
    dialogue_lines = []
    for entry in dialogue_entries:
        speaker = entry.get('speaker', 'Unknown')
        text = entry.get('text', '')
        entry_type = entry.get('type', 'dialogue')

        if entry_type == 'location':
            continue
        elif entry_type in ('spell', 'broom', 'mount', 'mail'):
            continue  # Skip spell casts, mount events, mail markers - not meaningful for long-term memory
        elif entry_type == 'combat':
            combat_summary = format_combat_summary(entry, include_damage=False)
            dialogue_lines.append(f"[Combat: {combat_summary}]")
        else:
            dialogue_lines.append(f"{speaker}: {text}")

    dialogue_str = "\n".join(dialogue_lines) if dialogue_lines else "No dialogue recorded."

    # Format key events
    events_str = ""
    if key_events:
        event_lines = [f"- {e.get('title', '')}: {e.get('summary', '')}" for e in key_events]
        events_str = "\n".join(event_lines)

    return f"""You are generating a memory episode for {npc_name} to be stored as searchable long-term facts.

## Chapter Information
Title: {chapter_title}
Location: {location}
Summary: {chapter_summary}

## Key Events
{events_str if events_str else "None identified"}

## Raw Dialogue
{dialogue_str}

## Task

Generate a rich prose narrative of this chapter from {npc_name}'s perspective. This will be extracted into independent searchable facts, so a search for a name won't match a pronoun reference.

CRITICAL PERSPECTIVE RULE:
- Treat the Chapter Information and Key Events as authoritative context for the chapter. Do not contradict them just because the raw dialogue is long, noisy, or missing supporting lines.
- If the dialogue is unclear, omit uncertain details rather than claiming a summarized key event did not happen.
- Only say {npc_name} did, planned, arranged, promised, asked, rescued, bought, or accepted something if {npc_name} personally did it in the raw dialogue or key events.
- If {npc_name} merely overheard or witnessed {player_name} speaking with another character, preserve that witness role exactly.
- Do NOT convert another character's quest, plan, purchase, or rescue task into {npc_name}'s quest or plan.
- If {player_name} talks to another character about that character's task while {npc_name} is nearby, write that {npc_name} overheard the specific discussion only if it is meaningful for {npc_name}'s memory. Do NOT turn the other character's task into {npc_name}'s task.

Optimize for:

1. **EXPLICIT NAMES**: Always use full character names, NEVER pronouns like "he", "she", "they", "him", "her", "them". Every single reference to a person must use their name.
   - BAD: "He told her about the mission"
   - GOOD: "{player_name} told {npc_name} about the mission"
   - GOOD WITNESSING: "{npc_name} overheard {player_name} and another named character discuss the named task"
   - BAD ATTRIBUTION: "{npc_name} planned the named task" unless {npc_name} personally planned it

2. **MAXIMIZE RELATIONSHIP CLARITY**: State relationships and connections explicitly. Don't assume context.
   - BAD: "They went to the ruins together"
   - GOOD: "{npc_name} traveled with {player_name} to the ruins near Marunweem Lake"
   - BAD: "The shopkeeper helped"
   - GOOD: "Sirona Ryan, the innkeeper at the Three Broomsticks, helped {npc_name} and {player_name}"

3. **DO NOT PAD WITH MOVEMENT, PLANS, OR REACTIONS**: Do not turn routine location changes, walking routes, proposed destinations, where-to-go questions, hiding suggestions, overheard filler, or momentary reactions into memory material.
   - BAD: "{npc_name} and {player_name} entered the Grand Staircase"
   - BAD: "{npc_name} traveled with {player_name} into the Faculty Tower"
   - BAD: "{npc_name} asked whether to go to a school common room or a hidden corner"
   - BAD: "Cedric Marsh expressed disgust at {player_name}'s suggestion" if the suggestion is not explicitly named
   - GOOD: "{player_name} made a specific memorable insult toward Cedric Marsh, and Cedric Marsh reacted with a specific emotion because of that insult"
   - If a reaction only makes sense with its cause, include the cause and reaction together in one sentence or omit the reaction.

4. **EXTRAPOLATE TEMPORAL RELATIONSHIPS CAREFULLY**: State when important things happened and derive only direct, useful implications.
   - BAD: "Later, they found the portrait"
   - GOOD: "After defeating the Ashwinders, {npc_name} and {player_name} discovered Ferdinand Pratt's portrait"
   - EXTRAPOLATE: If someone was rescued, state they were previously captured. If a lock blocked progress, state the mission remains incomplete. If this is a second meeting, reference the first.

5. **SELECTIVE COMPLETENESS, NOT FACT SPAM**: Pack in only material useful for long-term retrieval: full names, durable backstory, explicit lore, stable preferences, lasting relationship changes, completed memorable outcomes, named heirlooms, unusual revelations, and meaningful discoveries. Prefer one longer complete memory over several vague fragments. Omit ordinary navigation, filler, routine chatter, and momentary reactions.

6. **THIRD PERSON WITHOUT FALSE AGENCY**: Write about {npc_name} in third person, but do not make {npc_name} the actor for witnessed events. Use "{npc_name} witnessed...", "{npc_name} overheard...", or "{npc_name} was present when..." when {npc_name} was only nearby.

7. **DURABLE MEMORY, NOT TASK LOG**: Do not preserve open quest/task state as memory material. Requests, plans, blockers, attempts, errands, and next steps usually become stale and contradictory. Mention task-related material only when it became a completed memorable outcome, revealed durable lore, or changed a relationship in a lasting way.
   - BAD: "Rowan Bell is on a quest for bravery" (temporary task state)
   - BAD: "Rowan Bell asked {npc_name} to retrieve a leaf" (open request)
   - BAD: "Rowan Bell expressed relief after a fight" (momentary reaction)
   - GOOD: "{npc_name} delivered the leaf to Rowan Bell, and Rowan Bell's request ended with Rowan Bell feeling less ashamed of Rowan Bell's fear" (completed outcome with lasting character relevance)
   - GOOD: "Rowan Bell revealed that Rowan Bell's family expects courage because Rowan Bell's older siblings were celebrated duelists" (backstory/lore)

## Output Format

Write 2-4 paragraphs of flowing prose. No headers, no bullet points, just narrative text optimized for fact retrieval.

Begin your response with the prose directly, no preamble."""


def generate_episode_content(npc_name: str, chapter_title: str, chapter_summary: str,
                             key_events: List[Dict], location: str,
                             dialogue_entries: List[Dict], player_name: str = "the student",
                             max_retries: int = 3) -> str:
    """
    Generate episode content using LLM for long-term memory extraction.

    Returns rich prose optimized for fact retrieval with explicit names and relationships.
    """
    import llm

    settings = load_settings()
    memory_settings = settings.get('memory', {})
    include_cutscene = memory_settings.get('include_cutscene', True)
    model = _get_memory_llm_model(
        memory_settings,
        'prose_model',
        memory_settings.get('chapter_model') or 'gpt-4.1-nano',
    )

    prompt = build_episode_prompt(
        npc_name=npc_name,
        chapter_title=chapter_title,
        chapter_summary=chapter_summary,
        key_events=key_events,
        location=location,
        dialogue_entries=dialogue_entries,
        player_name=player_name
    )

    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            result = llm.chat(
                messages=[{"role": "user", "content": prompt}],
                model=model,
                temperature=0.7,
                max_tokens=4096,
                context="episode_generation"
            )

            if result and isinstance(result, str) and len(result.strip()) > 50:
                print(f"[Memory] Generated episode content: {len(result)} chars")
                return result.strip()
            else:
                print(f"[Memory] Attempt {attempt}/{max_retries}: insufficient content, retrying...")
        except Exception as e:
            last_error = e
            print(f"[Memory] Attempt {attempt}/{max_retries} failed: {e}")

        if attempt < max_retries:
            time.sleep(1)

    print(f"[Memory] Episode generation failed after {max_retries} attempts: {last_error}")

    # Fallback to simple format if LLM fails
    events_str = ""
    if key_events:
        event_lines = [f"- {e.get('title', '')}: {e.get('summary', '')}" for e in key_events]
        events_str = f"\n\nKey Events:\n{chr(10).join(event_lines)}"

    return f"""Chapter: {chapter_title}
Location: {location}
Summary: {chapter_summary}
{events_str}"""


def detect_chapter_boundary(npc_id: str, npc_name: str, dialogue_entries: List[Dict],
                            current_location: str, characters_in_earshot: List[str] = None,
                            force_detection: bool = False) -> Dict[str, Any]:
    """
    Detect if a chapter boundary should occur for an NPC.

    Returns:
        Dict with structure matching the prompt output:
        - current_chapter_action: "close" or "continue" (if open chapter exists)
        - close_at_timestamp: number (if closing)
        - additional_events: list of events to add to current chapter
        - new_chapters: list of new chapters to create
    """
    import llm

    settings = load_settings()
    memory_settings = settings.get('memory', {})
    include_cutscene = memory_settings.get('include_cutscene', True)

    if not memory_settings.get('enabled', True):
        return {"current_chapter_action": "continue", "new_chapters": []}

    dialogue_entries = [
        entry for entry in dialogue_entries
        if should_include_entry_for_npc_chaptering(npc_id, entry, include_cutscene=include_cutscene)
    ]

    chapter_mgr = ChapterManager()
    open_chapter = chapter_mgr.get_open_chapter(npc_id)

    # Check if we need to initialize indexing meta for old data
    # This MUST happen before any other checks to prevent treating all entries as "new"
    indexing_meta = chapter_mgr.get_indexing_meta(npc_id)
    last_index_ts = indexing_meta.get('last_index_timestamp', 0)

    if last_index_ts == 0 and dialogue_entries:
        # No last_index_timestamp = old data without tracking
        # Initialize to current time and skip - only genuinely new entries after this will trigger
        import time
        current_ts = int(time.time())
        chapter_mgr.update_indexing_meta(npc_id, current_ts)
        print(f"[Memory] Initialized indexing meta for {npc_id} (existing data, {len(dialogue_entries)} entries)")
        return {"current_chapter_action": "continue", "new_chapters": [], "_skipped": True}

    # Check if we should skip chapter detection
    # Skip if: same location AND entries since last index below threshold
    location_changed = not open_chapter or open_chapter.get('location') != current_location

    if not force_detection and not location_changed:
        # Same location - check entry threshold
        entries_since_last = sum(1 for e in dialogue_entries if e.get('timestamp', 0) > last_index_ts)
        threshold = memory_settings.get('chapter_entry_threshold', 30)

        if entries_since_last < threshold:
            # Same location and under threshold - skip expensive LLM call
            # Mark as skipped so we don't update indexing meta
            return {"current_chapter_action": "continue", "new_chapters": [], "_skipped": True}
        else:
            print(f"[Memory] Entry threshold reached for {npc_id}: {entries_since_last} >= {threshold}")
    elif force_detection:
        print(f"[Memory] Force-running chapter detection for {npc_id} (threshold bypass only)")

    # Build and send prompt
    prompt = build_chapter_prompt(npc_name, npc_id, dialogue_entries, open_chapter, characters_in_earshot)
    model = _get_memory_llm_model(memory_settings, 'chapter_model')

    # Use retry helper
    parsed = call_llm_with_retry(prompt, model, max_retries=2, context="chapter_detection")

    if not parsed:
        # LLM call failed - don't update indexing meta and don't checkpoint
        # so entries are retried on next processing cycle
        return {"current_chapter_action": "continue", "new_chapters": [], "_failed": True}

    print(f"[Memory] Chapter detection result: action={parsed.get('current_chapter_action', 'N/A')}, "
          f"new_chapters={len(parsed.get('new_chapters', []))}")
    return parsed


# =============================================================================
# Memory Compilation
# =============================================================================


def get_contextual_memory(npc_id: str, npc_name: str = None, player_name: str = "Player",
                          current_location: str = None,
                          nearby_npcs: List[str] = None,
                          mentioned_entities: List[str] = None) -> Optional[str]:
    """
    Get compact, contextual memory for an NPC's prompt.

    Always includes:
    - NPC's own node summary (self-knowledge)
    - Player node summary (what NPC knows about the player)
    - Current location summary (what NPC knows about where they are)

    Optionally includes:
    - Nearby NPC summaries (who's in the scene)
    - Mentioned entity summaries (what came up in conversation)

    Args:
        npc_id: NPC's internal ID
        npc_name: NPC's display name (e.g., "Nellie Oggspire")
        player_name: Player's character name
        current_location: Current location name (e.g., "Three Broomsticks")
        nearby_npcs: List of NPC names currently in earshot
        mentioned_entities: List of entity names mentioned recently

    Returns:
        Compact context string (~50-200 tokens) or None
    """
    settings = load_settings()
    if not settings.get('memory', {}).get('enabled', True):
        return None
    if not is_npc_long_term_memory_enabled(npc_id, settings=settings):
        return None

    memory_mgr = MemoryManager()
    if not memory_mgr.init_graphiti():
        return None

    # Skip if memory backend is busy adding episodes.
    if memory_mgr._busy:
        print("[Memory] Contextual memory skipped - memory backend busy")
        return None

    # Lightweight memory has no graph node summaries. Use structured bio when
    # available, then cached compiled fact prose and static background.
    sections = []

    bio = load_bio(npc_id)
    if bio:
        bio_context = format_bio_for_context(bio)
        if bio_context:
            sections.append(bio_context)

    if not sections:
        compiled = compile_memory_prose(npc_id, npc_name or npc_id)
        if compiled:
            sections.append(compiled)

    static_sections = _build_static_background_sections(
        npc_id=npc_id,
        npc_name=npc_name,
        player_name=player_name,
        settings=settings,
        include_player=False,
    )
    sections.extend(static_sections)

    player_bio = get_player_static_bio(player_name=player_name, settings=settings)
    if player_bio:
        sections.append(f"### About {player_name}\n**Background:** {player_bio}")

    if sections:
        return "\n\n".join(sections)

    editor_guidance = get_editor_guidance(npc_id=npc_id, display_name=npc_name, settings=settings)
    if editor_guidance:
        return f"### About You\n{editor_guidance}"

    return None



def _parse_memory_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        if isinstance(value, datetime):
            return value
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _format_memory_game_time(value: Any) -> str:
    """Format stored game-time timestamps for prompt context."""
    dt = _parse_memory_datetime(value)
    if not dt:
        return ""
    label = f"{dt.strftime('%b')} {dt.day}, {dt.year}"
    if dt.hour or dt.minute:
        label += f" {dt.hour:02d}:{dt.minute:02d}"
    return label


def _format_time_ago(event_time: Any, reference_time: Any) -> str:
    event_dt = _parse_memory_datetime(event_time)
    reference_dt = _parse_memory_datetime(reference_time)
    if not event_dt or not reference_dt:
        return ""

    delta_seconds = int((reference_dt - event_dt).total_seconds())
    if delta_seconds < 0:
        delta_seconds = abs(delta_seconds)
        suffix = "from now"
    else:
        suffix = "ago"

    if delta_seconds < 60:
        return "moments ago" if suffix == "ago" else "in moments"
    minutes = delta_seconds // 60
    if minutes < 60:
        unit = "minute" if minutes == 1 else "minutes"
        return f"{minutes} {unit} {suffix}"
    hours = minutes // 60
    if hours < 24:
        unit = "hour" if hours == 1 else "hours"
        return f"{hours} {unit} {suffix}"
    days = hours // 24
    if days < 14:
        unit = "day" if days == 1 else "days"
        return f"{days} {unit} {suffix}"
    weeks = days // 7
    if weeks < 8:
        unit = "week" if weeks == 1 else "weeks"
        return f"{weeks} {unit} {suffix}"
    months = max(1, days // 30)
    if months < 18:
        unit = "month" if months == 1 else "months"
        return f"{months} {unit} {suffix}"
    years = max(1, days // 365)
    unit = "year" if years == 1 else "years"
    return f"{years} {unit} {suffix}"


def _format_memory_fact_line(fact: str, valid_at: Any = None, chapter: str = "", reference_time: Any = None) -> str:
    parts = []
    time_label = _format_time_ago(valid_at, reference_time) if reference_time else _format_memory_game_time(valid_at)
    if time_label:
        parts.append(f"happened {time_label}")
    if chapter:
        parts.append(f"chapter: {chapter}")
    prefix = f"[{'; '.join(parts)}] " if parts else ""
    return f"{prefix}{fact}"


def format_graph_for_context(edges: List[Dict], npc_name: str = "You") -> str:
    """
    Format graph edges into organized, deterministic context for NPC prompts.

    Groups facts by chapter (temporal), then by type within each chapter:
    1. Your experiences & feelings (edges where source = "You")
    2. What you observed others do (edges between other characters)
    3. World knowledge (locations, objects, general facts)

    Args:
        edges: List of edge dicts with 'source', 'target', 'fact', 'name', 'chapters'
        npc_name: The NPC's name (for display)

    Returns:
        Formatted string for prompt context
    """
    if not edges:
        return ""

    from collections import defaultdict

    # Group edges by chapter
    chapter_facts = defaultdict(lambda: {"yours": [], "witnessed": [], "world": []})
    no_chapter = {"yours": [], "witnessed": [], "world": []}

    # Known location/object indicators for categorization
    location_words = {'ruins', 'lake', 'broomsticks', 'castle', 'village', 'shop', 'room', 'area', 'dungeon', 'forest', 'cave', 'hogwarts', 'hogsmeade'}
    object_words = {'portrait', 'door', 'key', 'wand', 'potion', 'book', 'letter', 'butterbeer'}

    for edge in edges:
        source = edge.get('source', 'Unknown')
        target = edge.get('target', 'Unknown')
        fact = _format_memory_fact_line(
            edge.get('fact', ''),
            valid_at=edge.get('valid_at'),
        )
        chapters = edge.get('chapters', [])

        source_lower = source.lower()
        target_lower = target.lower()

        # Determine category
        if source == "You":
            category = "yours"
        elif any(word in source_lower for word in location_words | object_words) or \
             any(word in target_lower for word in location_words | object_words):
            if source != "You" and target != "You":
                category = "world"
            else:
                category = "yours"
        else:
            category = "witnessed"

        # Add to chapter groups (may appear in multiple chapters)
        if chapters:
            for chapter in chapters:
                chapter_facts[chapter][category].append(fact)
        else:
            no_chapter[category].append(fact)

    # Build formatted output - chapters in order (oldest first based on edge order)
    sections = []

    # Get chapter order from edges (first occurrence = oldest)
    chapter_order = []
    seen_chapters = set()
    for edge in edges:
        for ch in edge.get('chapters', []):
            if ch not in seen_chapters:
                chapter_order.append(ch)
                seen_chapters.add(ch)

    # Output each chapter
    for chapter in chapter_order:
        facts = chapter_facts[chapter]
        if not any(facts.values()):
            continue

        sections.append(f"\n**{chapter}:**")

        if facts["yours"]:
            for fact in facts["yours"]:
                sections.append(f"- {fact}")

        if facts["witnessed"]:
            for fact in facts["witnessed"]:
                sections.append(f"- {fact}")

        if facts["world"]:
            for fact in facts["world"]:
                sections.append(f"- {fact}")

    # Any facts without chapter association
    if any(no_chapter.values()):
        sections.append("\n**General Knowledge:**")
        for fact in no_chapter["yours"] + no_chapter["witnessed"] + no_chapter["world"]:
            sections.append(f"- {fact}")

    result = "\n".join(sections)
    return result.strip()


def compile_memory_prose(npc_id: str, npc_name: str) -> Optional[str]:
    """
    Compile an NPC's graph data into natural prose for their prompt.

    Returns:
        Prose string or None if no memories/error
    """
    import llm

    settings = load_settings()
    memory_settings = settings.get('memory', {})

    if not memory_settings.get('enabled', True):
        return None
    if not is_npc_long_term_memory_enabled(npc_id, settings=settings):
        return None

    # Check cache first
    chapter_mgr = ChapterManager()
    cached = chapter_mgr.get_cached_memory(npc_id)
    if cached:
        return cached

    # Get graph data
    memory_mgr = MemoryManager()
    graph_data = memory_mgr.get_graph_data(npc_id)

    if not graph_data.get('edges'):
        return None  # No memories yet

    # Format facts deterministically into organized sections
    formatted_facts = format_graph_for_context(graph_data['edges'][:30], npc_name)

    prompt = f"""You are compiling {npc_name}'s memories into a natural prose summary for their character context.

## Facts from Memory
{formatted_facts}

## Task
Convert these facts into a flowing, natural prose summary written in second person from {npc_name}'s perspective.

**Structure your response as follows:**
1. Start with the most important ongoing relationships (who they've spent time with, how they feel about them)
2. Then cover significant recent events or adventures
3. End with any unresolved situations or things still in progress

**Guidelines:**
- Write in second person: "You remember...", "You've been...", "You and [character]..."
- Keep the tone natural, as if {npc_name} is recalling their experiences
- Focus on plot-relevant information, not mundane details
- Maximum 200 words
- Do not include information not present in the facts
- If facts mention emotions or opinions, include those

**Example output style:**
"You've grown quite fond of the new fifth-year student who arrived under mysterious circumstances. Together you ventured to the ruins near Marunweem Lake, where you witnessed their impressive dueling skills against the Ashwinders. The rescue mission was cut short by a lock neither of you could open - Ferdinand Pratt's portrait remains stuck there, much to your private amusement. Back at the Three Broomsticks, you shared a laugh with Astoria about Ferdinand's predicament."

Write your summary now:"""

    model = _get_memory_llm_model(memory_settings, 'prose_model')

    try:
        result = llm.chat(
            messages=[{"role": "user", "content": prompt}],
            model=model,
            temperature=0.7,
            max_tokens=4096,
            context="memory_prose"
        )

        if result:
            prose = f"## What You Remember\n\n{result.strip()}"
            # Cache the result
            chapter_mgr.cache_memory(npc_id, prose)
            return prose

    except Exception as e:
        print(f"[Memory] Prose compilation error: {e}")

    return None


# =============================================================================
# Public API for server.py
# =============================================================================

def get_npc_memory(npc_id: str, npc_name: str = None) -> Optional[str]:
    """
    Get the compiled memory block for an NPC to inject into their prompt.

    Args:
        npc_id: NPC's internal ID (e.g., "NellieOggspire")
        npc_name: NPC's display name for prose generation (optional)

    Returns:
        Memory prose string or None if no memories
    """
    settings = load_settings()
    if not settings.get('memory', {}).get('enabled', True):
        return None

    if not _cognis_available:
        return None

    # Use npc_id as name if not provided
    if not npc_name:
        from .localization import get_display_name
        npc_name = get_display_name(npc_id)

    return compile_memory_prose(npc_id, npc_name)


def _build_search_query_messages(player_message: str, npc_name: str,
                                 player_name: str = "the player") -> List[Dict[str, str]]:
    """Build the exact messages used by the production search-intent extractor."""
    system_prompt = f"""Extract a search query from a player message.

Extract key entities/topics for searching an NPC's memory facts (2-6 words).
- EXCLUDE the NPC's name ({npc_name}) - we're already searching their graph
- Replace "I/me/my/we/us/our" with player name ({player_name}) if relevant
- Extract other names, places, events, objects mentioned
- If greeting/thanks/small talk, return NONE
- Narration/action can be searchable if it mentions a concrete durable entity, object, place, or event
- Return NONE for conversational repair/continuation with no memory topic

Return ONLY the search terms or NONE. No explanation.

Examples:
- "do you remember what happened with Thomas?" → Thomas
- "what about that cave we explored?" → cave {player_name}
- "tell me about Professor Williams" → Professor Williams
- "{npc_name}, what do you think about the headmaster?" → headmaster
- "do you remember when {npc_name} helped with the potion?" → potion
- "what do you think of me?" → {player_name}
- "remember when we fought those bandits?" → bandits {player_name}
- "have you seen my wand?" → wand {player_name}
- "what about our adventure in the forest?" → adventure forest {player_name}
- "I met someone strange at Hogsmeade" → Hogsmeade {player_name}
- "do you know my friend Sebastian?" → Sebastian {player_name}
- "what did Poppy say about the beasts?" → Poppy beasts
- "have you spoken to Professor Elm lately?" → Professor Elm
- "did Sebastian mention the scriptorium?" → Sebastian scriptorium
- "what do you think of Rowan?" → Rowan
- "what happened at the tavern?" → tavern
- "*He grabs the shining blue diamond and puts it in his pocket*" → shining blue diamond {player_name}
- "*she suddenly stopped him* he was just passing.. sorry.. what were you saying?" → NONE
- "sorry, what were you saying?" → NONE
- "*nods* go on" → NONE
- "hey how are you?" → NONE
- "thanks for your help" → NONE
- "that's interesting" → NONE
- "let's go" → NONE
- "tell me about yourself" → NONE"""

    prompt = f"""Player: {player_name}
Speaking to: {npc_name}
Message: "{player_message}"

Search terms:"""

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt},
    ]


def extract_search_query(player_message: str, npc_name: str, player_name: str = "the player") -> Optional[str]:
    """
    Extract a clean search query from player's conversational message.

    Uses a fast LLM to identify what the player is actually asking about,
    stripping conversational fluff and extracting key entities/topics.
    Resolves pronouns like "we", "us", "I" to actual entity names.

    Returns:
        Clean search query string, or None if no search needed (greetings, etc.)
    """
    import llm

    # Skip very short messages
    if len(player_message.strip()) < 5:
        return None

    settings = load_settings()
    # Use reranker model (small/fast) for intent extraction
    model = _get_memory_llm_model(settings.get('memory', {}), 'reranker_model', 'gpt-4.1-nano')

    try:
        _profiler.mark("search_intent start")
        result = llm.chat(
            messages=_build_search_query_messages(player_message, npc_name, player_name),
            model=model,
            temperature=0.2,
            max_tokens=128,
            context="search_intent"
        )
        _profiler.mark("search_intent done")

        if result:
            result = result.strip()
            # Normalize: remove quotes, asterisks, and other LLM formatting artifacts
            result = result.strip('"\'`*_')
            # Remove any remaining paired quotes inside
            if result.startswith('"') and result.endswith('"'):
                result = result[1:-1]
            if result.startswith("'") and result.endswith("'"):
                result = result[1:-1]
            result = result.strip()

            if result.upper() == "NONE" or len(result) < 3:
                return None
            print(f"[Memory] Search query extracted: '{result}' from '{player_message[:50]}...'")
            return result
    except Exception as e:
        print(f"[Memory] Search intent extraction failed: {e}")

    # Fallback: return original message
    return player_message


_SEARCH_STOPWORDS = {
    'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for',
    'of', 'with', 'by', 'from', 'is', 'was', 'are', 'were', 'been', 'be',
    'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could',
    'should', 'may', 'might', 'must', 'shall', 'can', 'need', 'dare',
    'that', 'this', 'these', 'those', 'it', 'its', 'they', 'them', 'their',
    'we', 'us', 'our', 'you', 'your', 'i', 'me', 'my', 'he', 'him', 'his',
    'she', 'her', 'what', 'which', 'who', 'whom', 'when', 'where', 'why',
    'how', 'about', 'into', 'through', 'during', 'before', 'after', 'above',
    'below', 'between', 'under', 'again', 'further', 'then', 'once', 'here',
    'there', 'all', 'each', 'few', 'more', 'most', 'other', 'some', 'such',
    'no', 'not', 'only', 'own', 'same', 'so', 'than', 'too', 'very', 'just',
    'also', 'now', 'any', 'both', 'being', 'over', 'ever',
}


def _normalize_search_text(text: str) -> str:
    text = str(text or "").lower()
    return re.sub(r"['\u2019](s|t|d|ll|ve|re|m)\b", "", text)


def _rank_search_candidates(facts, search_query, reference_time):
    """Format and rank one Cognis result stream, preserving raw dedup keys."""
    query_words = [
        word for word in _normalize_search_text(search_query).split()
        if word not in _SEARCH_STOPWORDS and len(word) > 2
    ]
    keyword_matched = []
    semantic_only = []
    seen = set()

    for fact_item in facts or []:
        raw_fact = fact_item.get('fact', '') if isinstance(fact_item, dict) else str(fact_item)
        raw_fact = raw_fact.strip()
        key = raw_fact.lower()
        if not raw_fact or not key or key in seen:
            continue
        seen.add(key)
        formatted = raw_fact
        if isinstance(fact_item, dict):
            formatted = _format_memory_fact_line(
                raw_fact,
                valid_at=fact_item.get('valid_at'),
                chapter=fact_item.get('chapter') or "",
                reference_time=reference_time,
            )
        candidate = {"key": key, "line": formatted}
        normalized_fact = _normalize_search_text(raw_fact)
        if any(keyword in normalized_fact for keyword in query_words):
            keyword_matched.append(candidate)
        else:
            semantic_only.append(candidate)

    return keyword_matched + semantic_only, len(keyword_matched), len(semantic_only)


def _safe_search_facts(memory_mgr, npc_id, search_query, max_results):
    try:
        return memory_mgr.search_facts(
            npc_id=npc_id,
            query=search_query,
            center_node_uuid=None,
            max_results=max_results,
        )
    except Exception as exc:
        print(f"[Memory] Search failed for '{search_query}': {exc}")
        return []


def _merge_search_candidate_streams(active_candidates, topic_candidates, max_results):
    """Select balanced unique results, reallocating unused capacity."""
    if max_results <= 0:
        return [], 0, 0
    streams = {
        "active": list(active_candidates or []),
        "topic": list(topic_candidates or []),
    }
    if not streams["active"]:
        selected = streams["topic"][:max_results]
        return selected, 0, len(selected)
    if not streams["topic"]:
        selected = streams["active"][:max_results]
        return selected, len(selected), 0

    quotas = {
        "active": (max_results + 1) // 2,
        "topic": max_results // 2,
    }
    indices = {"active": 0, "topic": 0}
    contributions = {"active": 0, "topic": 0}
    selected = []
    seen = set()

    def take_one(name, enforce_quota):
        if enforce_quota and contributions[name] >= quotas[name]:
            return False
        stream = streams[name]
        while indices[name] < len(stream):
            candidate = stream[indices[name]]
            indices[name] += 1
            if candidate["key"] in seen:
                continue
            seen.add(candidate["key"])
            selected.append(candidate)
            contributions[name] += 1
            return True
        return False

    while len(selected) < max_results:
        progress = False
        for name in ("active", "topic"):
            progress = take_one(name, enforce_quota=True) or progress
            if len(selected) >= max_results:
                break
        if not progress:
            break

    while len(selected) < max_results:
        progress = False
        for name in ("active", "topic"):
            progress = take_one(name, enforce_quota=False) or progress
            if len(selected) >= max_results:
                break
        if not progress:
            break

    return selected, contributions["active"], contributions["topic"]


def search_relevant_facts(npc_id: str, query: str, npc_name: str = None,
                          player_name: str = None, center_node_name: str = None,
                          max_results: int = 50,
                          current_game_date: str = "",
                          current_game_time: str = "",
                          topic_query: str = None) -> Optional[List[str]]:
    """
    Search for facts relevant to a query, centered on a specific node.

    Uses a small LLM to extract search intent from conversational messages,
    then Cognis semantic/BM25 search to find relevant facts.

    Args:
        npc_id: NPC's memory owner id
        query: The player's message (will be processed to extract search query)
        npc_name: NPC's display name (for query extraction context)
        player_name: Player's character name (for resolving "we", "I", etc.)
        center_node_name: Node to center search on (default: NPC's name or "You")
        max_results: Maximum unique facts to return (after deduplication)
        topic_query: Optional pre-extracted scene topic to search alongside the active query

    Returns:
        List of unique fact strings, or None if search fails/no memory data
    """
    settings = load_settings()
    if not settings.get('memory', {}).get('enabled', True):
        return None
    if not is_npc_long_term_memory_enabled(npc_id, settings=settings):
        return None

    if not _cognis_available:
        return None

    reference_time = None
    if current_game_date and "/" in current_game_date and len(current_game_date.split("/", 1)[0]) == 4:
        reference_time = game_time_to_datetime(current_game_date, current_game_time)

    clean_topic = str(topic_query or "").strip()
    if clean_topic.upper() == "NONE":
        clean_topic = ""

    memory_mgr = MemoryManager()
    active_query = None
    topic_candidates = []

    if clean_topic:
        print(f"[Memory] Dual search init with topic '{clean_topic}'")
        if not memory_mgr.init_graphiti():
            return None
        _profiler.mark("graph_lookup start")
        with ThreadPoolExecutor(max_workers=2) as executor:
            topic_future = executor.submit(
                _safe_search_facts,
                memory_mgr,
                npc_id,
                clean_topic,
                max_results * 4,
            )
            active_query = extract_search_query(query, npc_name or "the NPC", player_name or "the player")
            duplicate_query = (
                active_query
                and _normalize_search_text(active_query).strip() == _normalize_search_text(clean_topic).strip()
            )
            if active_query and not duplicate_query:
                active_facts = _safe_search_facts(
                    memory_mgr, npc_id, active_query, max_results * 4
                )
            else:
                active_facts = []
            topic_facts = topic_future.result()
        _profiler.mark("graph_lookup done")

        topic_candidates, _, _ = _rank_search_candidates(
            topic_facts, clean_topic, reference_time
        )
        if duplicate_query:
            active_candidates = topic_candidates
            topic_candidates = []
            print("[Memory] Active and topic queries are identical; reused one search")
        else:
            active_candidates, _, _ = _rank_search_candidates(
                active_facts, active_query or "", reference_time
            )
    else:
        active_query = extract_search_query(query, npc_name or "the NPC", player_name or "the player")
        if not active_query:
            print(f"[Memory] No search needed for: '{query[:50]}...'")
            return None
        print(f"[Memory] Search init for '{active_query}'...")
        if not memory_mgr.init_graphiti():
            return None
        _profiler.mark("graph_lookup start")
        active_facts = _safe_search_facts(
            memory_mgr, npc_id, active_query, max_results * 4
        )
        _profiler.mark("graph_lookup done")
        active_candidates, _, _ = _rank_search_candidates(
            active_facts, active_query, reference_time
        )

    selected, active_contribution, topic_contribution = _merge_search_candidate_streams(
        active_candidates, topic_candidates, max_results
    )
    if not selected:
        return None

    active_keys = {candidate["key"] for candidate in active_candidates}
    topic_keys = {candidate["key"] for candidate in topic_candidates}
    selected_keys = {candidate["key"] for candidate in selected}
    print(
        f"[Memory] Search allocation: active={len(active_candidates)} candidates/"
        f"{len(selected_keys & active_keys)} represented, topic={len(topic_candidates)} candidates/"
        f"{len(selected_keys & topic_keys)} represented, contributions="
        f"{active_contribution}+{topic_contribution}, final={len(selected)}"
    )
    return [candidate["line"] for candidate in selected]


def evaluate_chapter_boundary(npc_id: str, npc_name: str, dialogue_history: List[Dict],
                               current_location: str, game_date: str = "",
                               game_time: str = "", characters_in_earshot: List[str] = None) -> bool:
    """
    Evaluate if a chapter should close and handle the transition.
    Called when a conversation ends.

    Args:
        npc_id: NPC's internal ID
        npc_name: NPC's display name
        dialogue_history: Recent dialogue entries (already earshot-filtered)
        current_location: Current game location
        game_date: Current game date
        game_time: Current game time
        characters_in_earshot: List of character names this NPC knows about

    Returns:
        True if any chapters were closed and episodes added
    """
    settings = load_settings()
    if not settings.get('memory', {}).get('enabled', True):
        return False

    if not _cognis_available:
        return False

    chapter_mgr = ChapterManager()
    memory_mgr = MemoryManager()

    # Detect chapter boundary
    result = detect_chapter_boundary(npc_id, npc_name, dialogue_history, current_location, characters_in_earshot)

    chapters_closed = False

    def close_runtime_open_chapter(close_at: Optional[int],
                                   additional_events: Optional[List[Dict]] = None,
                                   reason: str = "closed") -> bool:
        nonlocal chapters_closed

        open_chapter = chapter_mgr.get_open_chapter(npc_id)
        if not open_chapter:
            return False

        if additional_events:
            existing_events = open_chapter.get('key_events') or []
            existing_events.extend(additional_events)
            open_chapter['key_events'] = existing_events

        start_ts = open_chapter.get('start_timestamp', 0)
        if close_at and not is_valid_timestamp(close_at):
            close_at = None

        chapter_entries = []
        for entry in dialogue_history:
            timestamp = entry.get('timestamp', 0)
            if start_ts and timestamp < start_ts:
                continue
            if close_at and timestamp > close_at:
                break
            chapter_entries.append(entry)

        if not chapter_entries:
            print(
                f"[Memory] WARNING: No dialogue entries found while closing chapter "
                f"'{open_chapter.get('title')}' for {npc_id} at ts {start_ts}-{close_at}; "
                "leaving it open"
            )
            return False

        player_name = "the student"
        for entry in chapter_entries:
            if entry.get('isPlayer') and entry.get('speaker'):
                player_name = entry['speaker']
                break

        episode_content = generate_episode_content(
            npc_name=npc_name,
            chapter_title=open_chapter['title'],
            chapter_summary=open_chapter.get('summary', 'No summary'),
            key_events=open_chapter.get('key_events', []),
            location=open_chapter.get('location', 'Unknown'),
            dialogue_entries=chapter_entries,
            player_name=player_name
        )
        save_generated_episode_audit(npc_id, open_chapter['title'], episode_content, {
            "summary": open_chapter.get('summary', 'No summary'),
            "key_events": open_chapter.get('key_events', []),
            "location": open_chapter.get('location', 'Unknown'),
            "entries": chapter_entries,
            "close_reason": reason,
        })

        memory_mgr.add_episode(
            npc_id=npc_id,
            chapter_title=open_chapter['title'],
            content=episode_content,
            game_date=open_chapter.get('start_date', game_date),
            game_time=open_chapter.get('start_time', game_time)
        )

        last_entry_ts = close_at or (chapter_entries[-1].get('timestamp') if chapter_entries else None)
        chapter_mgr.close_chapter(
            npc_id,
            open_chapter.get('summary', ''),
            end_timestamp=last_entry_ts,
            key_events=open_chapter.get('key_events', [])
        )
        chapters_closed = True
        print(f"[Memory] Closed chapter '{open_chapter['title']}' for {npc_id} ({reason})")

        try:
            update_bio_incremental(
                npc_id=npc_id,
                npc_name=npc_name,
                chapter_start_ts=open_chapter.get('start_timestamp', 0),
                chapter_end_ts=last_entry_ts or int(datetime.now().timestamp())
            )
        except Exception as e:
            print(f"[Bio] Update failed (non-fatal): {e}")

        return True

    # Handle current chapter action
    current_action = result.get('current_chapter_action', 'continue')
    additional_events = result.get('additional_events', [])

    if current_action == 'close':
        open_chapter = chapter_mgr.get_open_chapter(npc_id)
        if open_chapter:
            # Get close timestamp
            close_at = result.get('close_at_timestamp')
            close_runtime_open_chapter(close_at, additional_events=additional_events, reason="LLM close action")

    elif current_action == 'continue' and additional_events:
        # Just add events to current chapter without closing
        chapter_mgr.add_events_to_chapter(npc_id, additional_events)

    # Process any new chapters from the result
    new_chapters = result.get('new_chapters', [])
    if chapter_mgr.get_open_chapter(npc_id) and new_chapters:
        valid_starts = [
            chapter.get('start_timestamp')
            for chapter in new_chapters
            if is_valid_timestamp(chapter.get('start_timestamp'))
        ]
        if valid_starts:
            first_new_start = min(valid_starts)
            open_chapter = chapter_mgr.get_open_chapter(npc_id)
            open_start = open_chapter.get('start_timestamp', 0) if open_chapter else 0
            previous_timestamps = [
                entry.get('timestamp', 0)
                for entry in dialogue_history
                if open_start <= entry.get('timestamp', 0) < first_new_start
            ]
            inferred_close_ts = max(previous_timestamps) if previous_timestamps else first_new_start
            print(
                f"[Memory] WARNING: Chapter detection returned {len(new_chapters)} new chapter(s) "
                f"while '{open_chapter.get('title')}' was still open; closing it at ts "
                f"{inferred_close_ts} before starting the next chapter"
            )
            if not close_runtime_open_chapter(inferred_close_ts, reason="inferred boundary before new chapter"):
                print("[Memory] WARNING: Skipping returned new chapters because the existing open chapter could not be closed")
                new_chapters = []
        else:
            open_chapter = chapter_mgr.get_open_chapter(npc_id)
            print(
                f"[Memory] WARNING: Chapter detection returned new chapters while "
                f"'{open_chapter.get('title') if open_chapter else 'Unknown'}' was open, "
                "but none had valid start timestamps"
            )
            new_chapters = []

    for new_chapter in new_chapters:
        chapter_title = new_chapter.get('title', 'New Chapter')
        chapter_status = new_chapter.get('status', 'open')
        chapter_summary = new_chapter.get('summary', '')
        chapter_events = new_chapter.get('key_events', [])
        start_ts = new_chapter.get('start_timestamp')
        end_ts = new_chapter.get('end_timestamp')

        # Validate timestamps - LLM sometimes returns garbage
        if start_ts and not is_valid_timestamp(start_ts):
            print(f"[Memory] Invalid start_timestamp {start_ts} for chapter '{chapter_title}' - skipping")
            continue
        if end_ts and not is_valid_timestamp(end_ts):
            print(f"[Memory] Invalid end_timestamp {end_ts} for chapter '{chapter_title}' - skipping")
            continue

        if chapter_status == 'closed' and start_ts and end_ts:
            # This is a fully closed chapter - add directly to long-term memory.
            chapter_entries = [e for e in dialogue_history
                              if start_ts <= e.get('timestamp', 0) <= end_ts]

            # Extract player name
            player_name = "the student"
            for entry in chapter_entries:
                if entry.get('isPlayer') and entry.get('speaker'):
                    player_name = entry['speaker']
                    break

            # Generate episode content with LLM
            episode_content = generate_episode_content(
                npc_name=npc_name,
                chapter_title=chapter_title,
                chapter_summary=chapter_summary,
                key_events=chapter_events,
                location=current_location,
                dialogue_entries=chapter_entries,
                player_name=player_name
            )
            save_generated_episode_audit(npc_id, chapter_title, episode_content, {
                "summary": chapter_summary,
                "key_events": chapter_events,
                "location": current_location,
                "entries": chapter_entries,
            })

            memory_mgr.add_episode(
                npc_id=npc_id,
                chapter_title=chapter_title,
                content=episode_content,
                game_date=game_date,
                game_time=game_time
            )
            chapters_closed = True
            print(f"[Memory] Added closed chapter '{chapter_title}' for {npc_id}")

            # Trigger bio update (non-blocking, best-effort)
            try:
                update_bio_incremental(
                    npc_id=npc_id,
                    npc_name=npc_name,
                    chapter_start_ts=start_ts,
                    chapter_end_ts=end_ts
                )
            except Exception as e:
                print(f"[Bio] Update failed (non-fatal): {e}")

        else:
            # Open chapter - start tracking it
            # Build context messages from recent dialogue
            context_messages = []
            for entry in dialogue_history[-15:]:
                context_messages.append({
                    'time': f"{entry.get('gameDate', '')} {entry.get('gameTime', '')}".strip(),
                    'speaker': entry.get('speaker', 'Unknown'),
                    'text': entry.get('text', '')[:200]  # Truncate long messages
                })

            chapter_mgr.start_chapter(
                npc_id, chapter_title, current_location, game_date, game_time,
                summary=chapter_summary,
                key_events=chapter_events,
                context_messages=context_messages
            )
            print(f"[Memory] Started new chapter '{chapter_title}' for {npc_id}")

    # If no chapters exist and none were created, start a default one
    if not chapter_mgr.get_open_chapter(npc_id) and not new_chapters:
        chapter_mgr.start_chapter(npc_id, "Beginning", current_location, game_date, game_time)

    # Update indexing meta with the latest entry timestamp
    # Only if we actually ran chapter detection (didn't skip)
    if not result.get('_skipped') and dialogue_history:
        max_ts = max(e.get('timestamp', 0) for e in dialogue_history)
        if max_ts > 0:
            chapter_mgr.update_indexing_meta(npc_id, max_ts)

    return chapters_closed


# =============================================================================
# LLM Helpers with Retry Logic
# =============================================================================

def call_llm_with_retry(prompt: str, model: str, max_retries: int = 3,
                        context: str = "memory") -> Optional[Dict]:
    """
    Call LLM and parse JSON response with retry logic.

    Args:
        prompt: The prompt to send
        model: Model name
        max_retries: Number of retries on failure
        context: Context label for logging

    Returns:
        Parsed JSON dict or None if all retries failed
    """
    import llm
    import time
    import re

    last_error = None

    for attempt in range(max_retries):
        try:
            result = llm.chat(
                messages=[{"role": "user", "content": prompt}],
                model=model,
                temperature=0.3,
                max_tokens=4096,
                context=context
            )

            if not result:
                last_error = "No LLM response"
                print(f"[Memory] Attempt {attempt + 1}/{max_retries}: No response")
                time.sleep(1)
                continue

            # Parse JSON response
            json_block_match = re.search(r'```json\s*([\s\S]*?)\s*```', result)
            if json_block_match:
                json_str = json_block_match.group(1)
            else:
                first_brace = result.find('{')
                last_brace = result.rfind('}')
                if first_brace != -1 and last_brace != -1:
                    json_str = result[first_brace:last_brace + 1]
                else:
                    json_str = result

            parsed = json.loads(json_str)
            return parsed

        except json.JSONDecodeError as e:
            last_error = f"JSON parse error: {e}"
            print(f"[Memory] Attempt {attempt + 1}/{max_retries}: {last_error}")

            # On retry, add hint to prompt about JSON formatting
            if attempt < max_retries - 1:
                prompt = prompt + "\n\nIMPORTANT: Your previous response had invalid JSON. Please return ONLY valid JSON with no extra text."
                time.sleep(1)

        except Exception as e:
            last_error = str(e)
            print(f"[Memory] Attempt {attempt + 1}/{max_retries}: {last_error}")
            time.sleep(1)

    print(f"[Memory] All {max_retries} attempts failed: {last_error}")
    return None


# =============================================================================
# Migration / Batch Processing
# =============================================================================

def migrate_npc_history(npc_id: str, npc_name: str, full_history: List[Dict],
                        batch_size: int = 50, progress_callback=None,
                        force_rebuild: bool = False) -> Dict[str, Any]:
    """
    Migrate existing dialogue history into chapters/episodes for an NPC.
    Processes history in batches to handle large histories (1000+ entries).

    Args:
        npc_id: NPC's internal ID
        npc_name: NPC's display name
        full_history: Complete dialogue history (will be filtered to NPC's earshot)
        batch_size: Number of entries to process per batch
        progress_callback: Optional callback(current, total, message) for progress updates

    Returns:
        Dict with: chapters_created, episodes_added, entries_processed, errors
    """
    import llm

    settings = load_settings()
    if not settings.get('memory', {}).get('enabled', True):
        return {"error": "Memory system not enabled"}
    if not is_npc_long_term_memory_enabled(npc_id, settings=settings):
        return {"error": f"Long-term memory is disabled for {npc_name or npc_id}"}

    if not _cognis_available:
        return {"error": "Cognis memory backend not available"}

    # Filter to entries that should actually contribute to this NPC's chapters.
    include_cutscene = settings.get('memory', {}).get('include_cutscene', True)
    npc_history = [
        e for e in full_history
        if should_include_entry_for_npc_chaptering(npc_id, e, include_cutscene=include_cutscene)
    ]

    if not npc_history:
        return {"error": f"No history found for {npc_id}", "entries_processed": 0}

    # Sort by timestamp
    npc_history.sort(key=lambda x: x.get('timestamp', 0))

    total_entries = len(npc_history)
    print(f"[Migration] Processing {total_entries} entries for {npc_name}")

    if progress_callback:
        progress_callback(0, total_entries, f"Starting migration for {npc_name}")

    chapter_mgr = ChapterManager()
    memory_mgr = MemoryManager()

    # Initialize memory backend.
    if not memory_mgr.init_graphiti():
        return {"error": "Failed to initialize memory backend"}

    if has_npc_memory_facts(npc_id) and not force_rebuild:
        print(f"[Migration] {npc_name} already has Cognis facts - skipping migration")
        return {
            "skipped": True,
            "reason": "Already migrated to Cognis",
            "chapters_created": 0,
            "episodes_added": 0,
            "entries_processed": 0,
            "errors": []
        }

    results = {
        "chapters_created": 0,
        "episodes_added": 0,
        "entries_processed": 0,
        "errors": []
    }

    # Old Graphiti chapters are not proof of Cognis migration. Clear per-NPC
    # derived state before rebuilding so chapter files and queue rows do not
    # make duplicate or stale memory state.
    try:
        memory_mgr.clear_graph(npc_id)
    except Exception as e:
        print(f"[Migration] Warning: could not clear existing facts for {npc_id}: {e}")
    try:
        chapter_mgr.clear_npc_data(npc_id)
    except Exception as e:
        print(f"[Migration] Warning: could not clear old chapter state for {npc_id}: {e}")
    try:
        from .memory_queue import reset_npc_state
        reset_npc_state(npc_id)
    except Exception as e:
        print(f"[Migration] Warning: could not reset queue state for {npc_id}: {e}")

    # Keep the auxiliary character list aligned with the same filtered chapter inputs.
    characters_list = collect_chapter_context_characters(npc_id, npc_history)

    # Process in batches with sliding window
    current_pos = 0
    open_chapter = None

    def close_migration_open_chapter(close_ts: int, game_date: str, game_time: str,
                                     additional_events: Optional[List[Dict]] = None,
                                     reason: str = "closed") -> bool:
        """Close the in-memory migration chapter and index it before moving on."""
        nonlocal open_chapter
        if not open_chapter:
            return False

        start_ts = open_chapter.get('start_timestamp', 0)
        if not is_valid_timestamp(close_ts):
            close_ts = start_ts
        if close_ts < start_ts:
            close_ts = start_ts

        key_events = list(open_chapter.get('key_events') or [])
        if additional_events:
            key_events.extend(additional_events)

        chapter_entries = [
            e for e in npc_history
            if start_ts <= e.get('timestamp', 0) <= close_ts
        ]
        if not chapter_entries:
            error = (
                f"No entries found while closing chapter '{open_chapter.get('title')}' "
                f"for {npc_id} at ts {start_ts}-{close_ts}"
            )
            print(f"[Migration] WARNING: {error}")
            results["errors"].append(error)
            return False

        player_name = "the student"
        for entry in chapter_entries:
            if entry.get('isPlayer') and entry.get('speaker'):
                player_name = entry['speaker']
                break

        if progress_callback:
            progress_callback(
                current_pos,
                total_entries,
                f"Generating episode: {open_chapter['title'][:30]}..."
            )

        episode_content = generate_episode_content(
            npc_name=npc_name,
            chapter_title=open_chapter['title'],
            chapter_summary=open_chapter.get('summary', ''),
            key_events=key_events,
            location=open_chapter.get('location', 'Unknown'),
            dialogue_entries=chapter_entries,
            player_name=player_name
        )
        save_generated_episode_audit(npc_id, open_chapter['title'], episode_content, {
            "summary": open_chapter.get('summary', ''),
            "key_events": key_events,
            "location": open_chapter.get('location', 'Unknown'),
            "entries": chapter_entries,
            "migration": True,
            "close_reason": reason,
        })

        if progress_callback:
            progress_callback(
                current_pos,
                total_entries,
                f"Indexing to graph: {open_chapter['title'][:30]}..."
            )

        if memory_mgr.add_episode(
            npc_id=npc_id,
            chapter_title=open_chapter['title'],
            content=episode_content,
            game_date=open_chapter.get('start_date', game_date),
            game_time=open_chapter.get('start_time', game_time)
        ):
            results["episodes_added"] += 1

        closed = chapter_mgr.close_chapter(
            npc_id=npc_id,
            summary=open_chapter.get('summary', ''),
            end_timestamp=close_ts,
            key_events=key_events
        )
        if not closed:
            error = f"ChapterManager had no open chapter while closing '{open_chapter.get('title')}' for {npc_id}"
            print(f"[Migration] WARNING: {error}")
            results["errors"].append(error)
            return False

        results["chapters_created"] += 1
        print(f"[Migration] Closed chapter '{open_chapter['title']}' ({reason}, ts {start_ts}-{close_ts})")
        open_chapter = None
        return True

    while current_pos < total_entries:
        # Get batch with context overlap
        context_start = max(0, current_pos - 10)  # 10 entries overlap for context
        batch_end = min(total_entries, current_pos + batch_size)
        batch = npc_history[context_start:batch_end]

        # Determine location from batch
        current_location = "Unknown"
        for entry in reversed(batch):
            if entry.get('type') == 'location':
                current_location = entry.get('text', 'Unknown')
                break

        # Get game date/time from last entry
        last_entry = batch[-1] if batch else {}
        game_date = last_entry.get('gameDate', '')
        game_time = last_entry.get('gameTime', '')

        if progress_callback:
            progress_callback(current_pos, total_entries,
                            f"Processing entries {current_pos}-{batch_end}")

        try:
            # Build prompt for this batch
            prompt = build_chapter_prompt(
                npc_name, npc_id, batch, open_chapter, characters_list
            )

            model = _get_memory_llm_model(settings.get('memory', {}), 'chapter_model')

            if progress_callback:
                progress_callback(current_pos, total_entries, "Detecting chapter boundaries...")

            # Use retry helper for robustness
            parsed = call_llm_with_retry(prompt, model, max_retries=3, context="migration_chapter")

            if not parsed:
                results["errors"].append(f"Failed after 3 retries at position {current_pos}")
                current_pos = batch_end
                continue

            # Handle current chapter action
            action = parsed.get('current_chapter_action', 'continue')
            additional_events = parsed.get('additional_events', [])
            new_chapters = parsed.get('new_chapters', [])

            if action == 'close' and open_chapter:
                close_ts = parsed.get('close_at_timestamp')
                if not is_valid_timestamp(close_ts):
                    close_ts = batch[-1].get('timestamp')
                close_migration_open_chapter(
                    close_ts,
                    game_date,
                    game_time,
                    additional_events=additional_events,
                    reason="LLM close action"
                )
            elif action == 'continue' and open_chapter and additional_events and not new_chapters:
                existing_events = open_chapter.get('key_events') or []
                existing_events.extend(additional_events)
                open_chapter['key_events'] = existing_events
                try:
                    chapter_mgr.add_events_to_chapter(npc_id, additional_events)
                except Exception as e:
                    print(f"[Migration] Warning: could not persist events for open chapter '{open_chapter.get('title')}': {e}")

            # Process new chapters
            if open_chapter and new_chapters:
                valid_starts = [
                    chapter.get('start_timestamp')
                    for chapter in new_chapters
                    if is_valid_timestamp(chapter.get('start_timestamp'))
                ]
                if valid_starts:
                    first_new_start = min(valid_starts)
                    open_start = open_chapter.get('start_timestamp', 0)
                    previous_timestamps = [
                        e.get('timestamp', 0)
                        for e in npc_history
                        if open_start <= e.get('timestamp', 0) < first_new_start
                    ]
                    inferred_close_ts = max(previous_timestamps) if previous_timestamps else first_new_start
                    print(
                        f"[Migration] WARNING: LLM returned {len(new_chapters)} new chapter(s) "
                        f"while '{open_chapter.get('title')}' was still open; closing it at "
                        f"ts {inferred_close_ts} before starting the next chapter"
                    )
                    if not close_migration_open_chapter(
                        inferred_close_ts,
                        game_date,
                        game_time,
                        additional_events=parsed.get('additional_events', []),
                        reason="inferred boundary before new chapter"
                    ):
                        print("[Migration] WARNING: Skipping returned new chapters because the existing open chapter could not be closed")
                        new_chapters = []
                else:
                    error = (
                        f"LLM returned new chapters while '{open_chapter.get('title')}' was open, "
                        "but none had valid start timestamps"
                    )
                    print(f"[Migration] WARNING: {error}")
                    results["errors"].append(error)
                    new_chapters = []

            for new_chapter in new_chapters:
                chapter_status = new_chapter.get('status', 'open')

                if chapter_status == 'closed':
                    # Fully closed chapter - add directly
                    start_ts = new_chapter.get('start_timestamp', 0)
                    end_ts = new_chapter.get('end_timestamp', 0)

                    # Validate timestamps - LLM sometimes returns garbage
                    if not is_valid_timestamp(start_ts) or not is_valid_timestamp(end_ts):
                        print(f"[Migration] Invalid timestamps ({start_ts}, {end_ts}) for chapter '{new_chapter.get('title')}' - skipping")
                        continue

                    # Get chapter entries by timestamp range
                    chapter_entries = [e for e in npc_history
                                       if start_ts <= e.get('timestamp', 0) <= end_ts]

                    # Extract player name
                    player_name = "the student"
                    for entry in chapter_entries:
                        if entry.get('isPlayer') and entry.get('speaker'):
                            player_name = entry['speaker']
                            break

                    # Generate episode content with LLM
                    if progress_callback:
                        progress_callback(current_pos, total_entries,
                                        f"Generating episode: {new_chapter['title'][:30]}...")

                    episode_content = generate_episode_content(
                        npc_name=npc_name,
                        chapter_title=new_chapter['title'],
                        chapter_summary=new_chapter.get('summary', ''),
                        key_events=new_chapter.get('key_events', []),
                        location=current_location,
                        dialogue_entries=chapter_entries,
                        player_name=player_name
                    )
                    save_generated_episode_audit(npc_id, new_chapter['title'], episode_content, {
                        "summary": new_chapter.get('summary', ''),
                        "key_events": new_chapter.get('key_events', []),
                        "location": current_location,
                        "entries": chapter_entries,
                        "migration": True,
                    })

                    if progress_callback:
                        progress_callback(current_pos, total_entries,
                                        f"Indexing to graph: {new_chapter['title'][:30]}...")

                    if memory_mgr.add_episode(
                        npc_id=npc_id,
                        chapter_title=new_chapter['title'],
                        content=episode_content,
                        game_date=game_date,
                        game_time=game_time
                    ):
                        results["episodes_added"] += 1

                    # Save the closed chapter (start then immediately close)
                    chapter_mgr.start_chapter(
                        npc_id=npc_id,
                        title=new_chapter['title'],
                        location=current_location,
                        game_date=game_date,
                        game_time=game_time,
                        summary=new_chapter.get('summary', ''),
                        start_timestamp=start_ts,
                        key_events=new_chapter.get('key_events', [])
                    )
                    chapter_mgr.close_chapter(
                        npc_id=npc_id,
                        summary=new_chapter.get('summary', ''),
                        end_timestamp=end_ts,
                        key_events=new_chapter.get('key_events', [])
                    )

                    results["chapters_created"] += 1
                    print(f"[Migration] Added closed chapter '{new_chapter['title']}' (ts {start_ts}-{end_ts})")

                else:
                    # Open chapter - track it and save to ChapterManager
                    start_ts = new_chapter.get('start_timestamp', batch[0].get('timestamp', 0))

                    # Validate timestamp - use batch timestamp as fallback
                    if not is_valid_timestamp(start_ts):
                        fallback_ts = batch[0].get('timestamp', 0) if batch else 0
                        if is_valid_timestamp(fallback_ts):
                            print(f"[Migration] Invalid start_timestamp {start_ts}, using fallback {fallback_ts}")
                            start_ts = fallback_ts
                        else:
                            print(f"[Migration] Invalid timestamps for open chapter '{new_chapter.get('title')}' - skipping")
                            continue

                    open_chapter = {
                        "title": new_chapter.get('title', 'Chapter'),
                        "summary": new_chapter.get('summary', ''),
                        "location": current_location,
                        "start_date": game_date,
                        "start_time": game_time,
                        "start_timestamp": start_ts,
                        "key_events": new_chapter.get('key_events', [])
                    }

                    # Save to ChapterManager so it persists
                    chapter_mgr.start_chapter(
                        npc_id=npc_id,
                        title=open_chapter['title'],
                        location=current_location,
                        game_date=game_date,
                        game_time=game_time,
                        summary=new_chapter.get('summary', ''),
                        start_timestamp=start_ts,
                        key_events=new_chapter.get('key_events', [])
                    )

                    print(f"[Migration] Started chapter '{open_chapter['title']}' at ts {start_ts}")

        except json.JSONDecodeError as e:
            results["errors"].append(f"JSON parse error at {current_pos}: {e}")
        except Exception as e:
            results["errors"].append(f"Error at {current_pos}: {e}")

        results["entries_processed"] = batch_end
        current_pos = batch_end

    # Note: Open chapter is already saved to ChapterManager during processing
    if open_chapter:
        print(f"[Migration] Left open chapter '{open_chapter['title']}' at ts {open_chapter.get('start_timestamp', 0)}")

    # Mark this NPC as fully indexed so the memory queue doesn't reprocess them
    # Update BOTH the JSON indexing_meta AND the SQLite npc_state
    if npc_history:
        max_ts = max(e.get('timestamp', 0) for e in npc_history)
        if max_ts > 0:
            # Update JSON indexing_meta (legacy)
            chapter_mgr.update_indexing_meta(npc_id, max_ts)
            print(f"[Migration] Set indexing_meta for {npc_id} to timestamp {max_ts}")

            # CRITICAL: Also update SQLite npc_state directly
            # This is what the memory queue actually reads to determine what's processed
            try:
                from .memory_queue import get_max_entry_id_for_timestamp, get_connection, _ensure_initialized
                _ensure_initialized()

                max_entry_id = get_max_entry_id_for_timestamp(npc_id, max_ts)
                if max_entry_id > 0:
                    import time
                    now = int(time.time())
                    with get_connection() as conn:
                        # Use INSERT OR REPLACE to ensure the row exists
                        conn.execute("""
                            INSERT INTO npc_state (npc_id, is_processing, last_processed_entry_id, created_at, updated_at)
                            VALUES (?, 0, ?, ?, ?)
                            ON CONFLICT(npc_id) DO UPDATE SET
                                last_processed_entry_id = ?,
                                updated_at = ?
                        """, (npc_id, max_entry_id, now, now, max_entry_id, now))
                        conn.commit()
                    print(f"[Migration] Set SQLite last_processed_entry_id for {npc_id} to {max_entry_id}")
                else:
                    print(f"[Migration] WARNING: Could not find entry ID for timestamp {max_ts}")
            except Exception as e:
                print(f"[Migration] WARNING: Failed to update SQLite state: {e}")

    if progress_callback:
        progress_callback(total_entries, total_entries, "Migration complete")

    print(f"[Migration] Complete: {results['chapters_created']} chapters, "
          f"{results['episodes_added']} episodes, {len(results['errors'])} errors")

    return results


def migrate_all_npcs(full_history: List[Dict], progress_callback=None) -> Dict[str, Any]:
    """
    Migrate dialogue history for all NPCs that have history.

    Args:
        full_history: Complete dialogue history
        progress_callback: Optional callback(npc_id, npc_progress, message)

    Returns:
        Dict mapping npc_id -> migration results
    """
    from .localization import get_display_name

    npc_entry_counts = count_chapter_candidate_entries_by_npc(full_history)

    # Filter to NPCs meeting minimum entry threshold (same as migration-status endpoint)
    settings = load_settings()
    min_entries = settings.get('memory', {}).get('chapter_entry_threshold', 30)
    npc_ids = [
        npc_id for npc_id, count in npc_entry_counts.items()
        if count >= min_entries
    ]
    npc_ids = filter_memory_enabled_npc_ids(npc_ids, settings=settings)

    results = {}
    total_npcs = len(npc_ids)

    for i, npc_id in enumerate(npc_ids):
        npc_name = get_display_name(npc_id)

        if progress_callback:
            progress_callback(npc_id, f"{i+1}/{total_npcs}", f"Migrating {npc_name}")

        results[npc_id] = migrate_npc_history(npc_id, npc_name, full_history)

    return results


def is_memory_available() -> bool:
    """Check if the lightweight memory system is available."""
    return _cognis_available


def has_npc_memory_facts(npc_id: str) -> bool:
    """Return True only when the active Cognis backend has facts for this NPC."""
    if not _cognis_available:
        return False
    settings = load_settings()
    if not settings.get('memory', {}).get('enabled', False):
        return False
    provider = settings.get('llm', {}).get('provider', 'gemini')
    if is_llm_provider_feature_disabled('memory', settings) or provider not in _MEMORY_SUPPORTED_PROVIDERS:
        return False
    memory_mgr = MemoryManager()
    graph_data = memory_mgr.get_graph_data(npc_id)
    return bool(graph_data.get("edges"))


def init_memory() -> bool:
    """
    Initialize the lightweight memory system.
    Call on server startup and after memory settings change.

    Returns:
        True if initialization succeeded, False otherwise.
    """
    _sync_memory_data_dir_from_player_context()

    try:
        from . import player_context

        if not player_context.is_ready():
            print("[Memory] Player context not ready - deferring memory init until player scope is available")
            return False
    except Exception:
        pass

    if not _cognis_available:
        print("[Memory] Cognis not available - memory system disabled")
        return False

    settings = load_settings()
    memory_settings = settings.get('memory', {})

    if not memory_settings.get('enabled', True):
        print("[Memory] Memory system disabled in settings")
        return False

    provider = settings.get('llm', {}).get('provider', 'gemini')
    if is_llm_provider_feature_disabled('memory', settings):
        print(f"[Memory] Memory system disabled for provider '{provider}' by LLM Provider settings")
        return False
    if provider not in _MEMORY_SUPPORTED_PROVIDERS:
        print(f"[Memory] Memory system disabled for provider '{provider}' (unsupported memory provider)")
        return False

    memory_mgr = MemoryManager()
    success = memory_mgr.init_graphiti()

    if success:
        if not MemoryManager._startup_backup_done:
            if create_memory_snapshot("startup_validated", timeout=180.0, keep=10):
                MemoryManager._startup_backup_done = True
        print("[Memory] Memory system initialized successfully")
    else:
        print("[Memory] Memory system initialization failed")

    return success


def reset_memory_connection() -> bool:
    """
    Reset the memory connection. Call when memory settings change.
    Next init_memory() or graph operation will reconnect with new settings.
    """
    memory_mgr = MemoryManager()
    close_ok = memory_mgr.close_graphiti(timeout=30.0)
    success = close_ok
    MemoryManager._indices_built = False
    MemoryManager._startup_backup_done = False

    if success:
        print("[Memory] Memory connection reset - will reconnect on next use")
    else:
        print(f"[Memory] Memory connection reset incomplete (close_ok={close_ok}, shutdown_ok={shutdown_ok})")

    return success


def close_all():
    """Shut down memory resources and reset all state."""
    reset_memory_connection()


def reinit(data_dir):
    """Re-initialize memory system at a new data directory."""
    global _memory_data_dir
    _memory_data_dir = data_dir
    init_memory()


def _copy_tree_if_exists(src: str, dst: str) -> bool:
    """Copy a directory if it exists."""
    if not os.path.isdir(src):
        return False
    shutil.copytree(src, dst)
    return True


def _export_memory_queue_snapshot(dest_db_path: str) -> bool:
    """Create a consistent SQLite backup of the memory queue database."""
    from .memory_queue import QUEUE_DB_PATH

    if not os.path.exists(QUEUE_DB_PATH):
        return False

    os.makedirs(os.path.dirname(dest_db_path), exist_ok=True)
    src_conn = sqlite3.connect(QUEUE_DB_PATH, timeout=30.0, check_same_thread=False)
    dest_conn = sqlite3.connect(dest_db_path, timeout=30.0, check_same_thread=False)
    try:
        src_conn.backup(dest_conn)
        dest_conn.commit()
        return True
    finally:
        try:
            dest_conn.close()
        except Exception:
            pass
        try:
            src_conn.close()
        except Exception:
            pass


def create_memory_snapshot(reason: str, timeout: float = 180.0, keep: int = 10) -> Optional[str]:
    """Create a restorable memory snapshot including Cognis data, queue DB, and chapter state."""
    backup_root = _get_memory_backup_root()
    snapshot_dir = _make_unique_backup_dir(backup_root, reason)
    includes = {
        "cognis": False,
        "memory_queue_db": False,
        "chapters": False,
        "chapter_content": False,
        "npc_bios": False,
    }

    try:
        os.makedirs(snapshot_dir, exist_ok=True)

        cognis_dir = os.path.join(_sync_memory_data_dir_from_player_context(), "cognis")
        includes["cognis"] = _copy_tree_if_exists(
            cognis_dir,
            _get_memory_snapshot_cognis_dir(snapshot_dir),
        )

        chapters_dir = os.path.join(_memory_data_dir, "chapters")
        includes["chapters"] = _copy_tree_if_exists(chapters_dir, os.path.join(snapshot_dir, "chapters"))
        includes["chapter_content"] = _copy_tree_if_exists(
            _get_chapter_content_dir_path(),
            os.path.join(snapshot_dir, "chapter_content"),
        )
        includes["npc_bios"] = _copy_tree_if_exists(
            os.path.join(_memory_data_dir, "npc_bios"),
            os.path.join(snapshot_dir, "npc_bios"),
        )
        includes["memory_queue_db"] = _export_memory_queue_snapshot(
            _get_memory_snapshot_queue_db_path(snapshot_dir)
        )

        manifest = {
            "backup_type": "cognis_memory_snapshot",
            "version": 2,
            "created_at": int(time.time()),
            "reason": reason,
            "includes": includes,
        }
        with open(_get_memory_snapshot_manifest_path(snapshot_dir), "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)

        _prune_old_graph_backups(backup_root, keep=keep)
        print(f"[Memory] Created memory snapshot: {snapshot_dir}")
        return snapshot_dir
    except Exception as e:
        if os.path.isdir(snapshot_dir):
            try:
                shutil.rmtree(snapshot_dir)
            except Exception:
                pass
        print(f"[Memory] Failed to create memory snapshot '{reason}': {e}")
        return None



def _move_existing_path(src: str, dst: str) -> None:
    """Move an existing file or directory into rollback storage."""
    if not os.path.exists(src):
        return
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.move(src, dst)


def _remove_path_if_exists(path: str) -> None:
    """Delete a file or directory if it exists."""
    if not os.path.exists(path):
        return
    if os.path.isdir(path):
        shutil.rmtree(path)
    else:
        os.remove(path)


def restore_memory_backup(backup_id: str) -> Dict[str, Any]:
    """Restore a Cognis memory snapshot."""
    backup_path = _resolve_memory_backup_path(backup_id)
    if not backup_path:
        return {"success": False, "error": "Backup not found or invalid"}

    latest_rel_id = _relative_graph_backup_id(backup_path)
    manifest = _load_backup_manifest(backup_path)
    backup_type = manifest.get("backup_type") or "legacy_graph_export"
    includes = manifest.get("includes", {})

    if backup_type == "legacy_graph_export":
        return {
            "success": False,
            "error": "Legacy Kuzu/Graphiti exports cannot be restored into the Cognis memory backend. Re-index from chapters instead.",
        }

    if backup_type == "memory_snapshot" and not includes.get("cognis"):
        return {
            "success": False,
            "error": "This pre-Cognis memory snapshot does not contain Cognis data. Re-index from chapters instead.",
        }

    if backup_type not in ("cognis_memory_snapshot", "memory_snapshot"):
        return {"success": False, "error": f"Unsupported backup type: {backup_type}"}

    create_memory_snapshot("before_manual_restore", timeout=180.0, keep=10)

    from .memory_queue import graceful_shutdown, close_all_connections, reset_connection_state

    if not graceful_shutdown(max_wait=30.0):
        return {"success": False, "error": "Memory shutdown did not complete cleanly"}

    close_all_connections()
    reset_connection_state()
    reset_memory_connection()

    temp_root = tempfile.mkdtemp(prefix="memory_restore_", dir=_memory_data_dir)
    rollback_root = os.path.join(temp_root, "rollback")
    staged_root = os.path.join(temp_root, "staged")
    os.makedirs(rollback_root, exist_ok=True)
    os.makedirs(staged_root, exist_ok=True)

    stage_queue_path = os.path.join(staged_root, "memory_queue.db")
    staged_dirs: Dict[str, str] = {}

    try:
        for dir_name in ("cognis", "chapters", "chapter_content", "npc_bios"):
            src_dir = os.path.join(backup_path, dir_name)
            if os.path.isdir(src_dir):
                staged_dir = os.path.join(staged_root, dir_name)
                shutil.copytree(src_dir, staged_dir)
                staged_dirs[dir_name] = staged_dir

        queue_snapshot_path = _get_memory_snapshot_queue_db_path(backup_path)
        if includes.get("memory_queue_db") and os.path.exists(queue_snapshot_path):
            shutil.copy2(queue_snapshot_path, stage_queue_path)
            conn = sqlite3.connect(stage_queue_path, timeout=30.0, check_same_thread=False)
            try:
                conn.execute("SELECT 1")
            finally:
                conn.close()

        from .memory_queue import QUEUE_DB_PATH

        restore_targets = {
            "cognis": os.path.join(_memory_data_dir, "cognis"),
            "memory_queue.db": QUEUE_DB_PATH,
            "memory_queue.db-wal": QUEUE_DB_PATH + "-wal",
            "memory_queue.db-shm": QUEUE_DB_PATH + "-shm",
            "chapters": os.path.join(_memory_data_dir, "chapters"),
            "chapter_content": _get_chapter_content_dir_path(),
            "npc_bios": os.path.join(_memory_data_dir, "npc_bios"),
        }

        for name, final_path in restore_targets.items():
            _move_existing_path(final_path, os.path.join(rollback_root, name))

        for dir_name in ("cognis", "chapters", "chapter_content", "npc_bios"):
            final_dir = restore_targets[dir_name]
            if dir_name in staged_dirs:
                shutil.move(staged_dirs[dir_name], final_dir)
            else:
                _remove_path_if_exists(final_dir)

        if os.path.exists(stage_queue_path):
            shutil.move(stage_queue_path, QUEUE_DB_PATH)
        else:
            _remove_path_if_exists(QUEUE_DB_PATH)
        _remove_path_if_exists(QUEUE_DB_PATH + "-wal")
        _remove_path_if_exists(QUEUE_DB_PATH + "-shm")

        shutil.rmtree(rollback_root, ignore_errors=True)
        shutil.rmtree(temp_root, ignore_errors=True)
    except Exception as e:
        for name in ("cognis", "memory_queue.db", "memory_queue.db-wal", "memory_queue.db-shm", "chapters", "chapter_content", "npc_bios"):
            _remove_path_if_exists(os.path.join(_memory_data_dir, name))

        if os.path.isdir(rollback_root):
            for name in os.listdir(rollback_root):
                shutil.move(os.path.join(rollback_root, name), os.path.join(_memory_data_dir, name))

        shutil.rmtree(temp_root, ignore_errors=True)
        return {"success": False, "error": f"Restore failed: {e}"}

    init_ok = init_memory()
    if not init_ok:
        print("[Memory] Restored memory snapshot; memory backend did not initialize under current settings")

    try:
        from .memory_queue import ensure_worker_running
        ensure_worker_running()
    except Exception as e:
        print(f"[Memory] Warning: restored memory, but failed to restart memory queue worker: {e}")

    return {
        "success": True,
        "backup_id": latest_rel_id,
        "path": backup_path,
        "backup_type": backup_type,
        "memory_initialized": bool(init_ok),
    }

def list_graph_backups() -> List[Dict[str, Any]]:
    """Backward-compatible alias for memory snapshot listing."""
    return list_memory_backups()


def restore_graph_backup(backup_id: str) -> Dict[str, Any]:
    """Backward-compatible alias for memory snapshot restore."""
    return restore_memory_backup(backup_id)


try:
    from . import player_context
    player_context.register("memory", close_all, reinit)
except ImportError:
    pass
