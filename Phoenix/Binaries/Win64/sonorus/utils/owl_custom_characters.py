"""
Helpers for Owl Post-only custom characters.

These characters exist only for mail. They are not added to the normal NPC
systems, board rosters, or significant-NPC filters.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional

from constants import is_excluded_npc
from .settings import SONORUS_DIR, SETTINGS_FILE, load_settings


_RESERVED_CUSTOM_CHARACTER_IDS = {"player"}
_custom_characters_cache: Optional[List[Dict[str, str]]] = None
_custom_characters_cache_mtime: Optional[float] = None


def derive_custom_character_id(name: str) -> str:
    """Convert a display name into a stable Owl Post speaker ID."""
    return re.sub(r"[^A-Za-z0-9]+", "", str(name or ""))


def get_reserved_custom_character_ids() -> set[str]:
    return set(_RESERVED_CUSTOM_CHARACTER_IDS)


def load_builtin_owl_mail_recipient_ids() -> List[str]:
    """Load built-in Owl Mail recipient IDs from the voice manifest."""
    manifest_path = os.path.join(SONORUS_DIR, "data", "voice_manifest.json")
    if not os.path.exists(manifest_path):
        return []

    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except Exception as e:
        print(f"[OwlPost] Failed to load voice manifest: {e}")
        return []

    voices = manifest.get("voices", manifest)
    if not isinstance(voices, dict):
        return []

    return list(voices.keys())


def _sanitize_custom_owl_characters(raw_entries: Any) -> List[Dict[str, str]]:
    if not isinstance(raw_entries, list):
        return []

    characters: List[Dict[str, str]] = []
    for entry in raw_entries:
        if not isinstance(entry, dict):
            continue

        name = str(entry.get("name") or "").strip()
        character_id = str(entry.get("id") or "").strip()
        bio = entry.get("bio")
        if bio is None:
            bio = ""
        bio = str(bio)

        if not character_id and name:
            character_id = derive_custom_character_id(name)

        if not character_id:
            continue

        characters.append({
            "name": name or character_id,
            "id": character_id,
            "bio": bio,
        })

    return characters


def load_custom_owl_characters(settings: Optional[Dict[str, Any]] = None) -> List[Dict[str, str]]:
    """Return sanitized Owl Post custom characters from settings."""
    global _custom_characters_cache, _custom_characters_cache_mtime

    if settings is not None:
        owl_settings = settings.get("owl_post", {}) if isinstance(settings, dict) else {}
        return _sanitize_custom_owl_characters(owl_settings.get("custom_characters", []))

    settings_mtime = os.path.getmtime(SETTINGS_FILE) if os.path.exists(SETTINGS_FILE) else -1
    if _custom_characters_cache is not None and _custom_characters_cache_mtime == settings_mtime:
        return [dict(entry) for entry in _custom_characters_cache]

    loaded_settings = load_settings(raw=True)
    owl_settings = loaded_settings.get("owl_post", {}) if isinstance(loaded_settings, dict) else {}
    sanitized = _sanitize_custom_owl_characters(owl_settings.get("custom_characters", []))
    _custom_characters_cache = [dict(entry) for entry in sanitized]
    _custom_characters_cache_mtime = settings_mtime
    return sanitized


def get_custom_owl_character(character_id: str, settings: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, str]]:
    """Look up a custom Owl Post character by ID, case-insensitively."""
    key = str(character_id or "").strip().lower()
    if not key:
        return None

    for entry in load_custom_owl_characters(settings):
        if entry.get("id", "").strip().lower() == key:
            return entry
    return None


def is_custom_owl_character(character_id: str, settings: Optional[Dict[str, Any]] = None) -> bool:
    return get_custom_owl_character(character_id, settings) is not None


def get_custom_owl_character_display_name(character_id: str, settings: Optional[Dict[str, Any]] = None) -> Optional[str]:
    entry = get_custom_owl_character(character_id, settings)
    if not entry:
        return None
    return entry.get("name") or entry.get("id")


def get_custom_owl_character_bio(character_id: str, settings: Optional[Dict[str, Any]] = None) -> Optional[str]:
    entry = get_custom_owl_character(character_id, settings)
    if not entry:
        return None
    return entry.get("bio")


def get_allowed_owl_mail_recipient_ids(
    settings: Optional[Dict[str, Any]] = None,
    mission_statuses: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """Return all allowed Owl Mail recipient IDs after blacklist filtering."""
    allowed: List[str] = []
    seen = set()

    for recipient_id in load_builtin_owl_mail_recipient_ids():
        key = str(recipient_id or "").strip().lower()
        if (
            not key
            or key in seen
            or key in _RESERVED_CUSTOM_CHARACTER_IDS
            or is_excluded_npc(recipient_id, mission_statuses)
        ):
            continue
        seen.add(key)
        allowed.append(recipient_id)

    for entry in load_custom_owl_characters(settings):
        recipient_id = str(entry.get("id") or "").strip()
        key = recipient_id.lower()
        if (
            not key
            or key in seen
            or key in _RESERVED_CUSTOM_CHARACTER_IDS
            or is_excluded_npc(recipient_id, mission_statuses)
        ):
            continue
        seen.add(key)
        allowed.append(recipient_id)

    return allowed


def is_allowed_owl_mail_recipient(
    character_id: str,
    settings: Optional[Dict[str, Any]] = None,
    mission_statuses: Optional[Dict[str, Any]] = None,
) -> bool:
    """Return True if the ID is a non-blacklisted built-in or configured custom Owl Mail recipient."""
    key = str(character_id or "").strip().lower()
    if not key or key in _RESERVED_CUSTOM_CHARACTER_IDS:
        return False

    for allowed_id in get_allowed_owl_mail_recipient_ids(settings, mission_statuses):
        if str(allowed_id).strip().lower() == key:
            return True
    return False
