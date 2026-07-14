"""
Localization utilities for Sonorus.
Handles ID to display name mapping and reverse lookups.
Supports language-specific localization files.
"""

import os
import json
import re
import threading

from .settings import DATA_DIR, load_settings


class _LocalizationCache:
    """Singleton cache that persists across reloads."""
    _instance = None
    _lock = threading.RLock()  # Protects cache initialization across nested cache loads

    def __init__(self):
        self.localization = None
        self.reverse_localization = None
        self.lowercase_map = None  # lowercase ID -> canonical ID mapping
        self.cached_language = None

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance


# Global reference that survives auto-reload
_cache = _LocalizationCache.get_instance()


def get_localization_path(language=None):
    """
    Get path to main_localization.json for specified language.

    Args:
        language: Language code like "EN_US", "DE_DE". If None, reads from settings.

    Returns:
        Path to localization file. English uses base filename, others use suffix.
        e.g., "main_localization.json" (EN) or "main_localization_de_de.json" (DE)
    """
    if language is None:
        settings = load_settings()
        language = settings.get('setup', {}).get('language', 'EN_US')

    if language == 'EN_US':
        return os.path.join(DATA_DIR, "main_localization.json")
    else:
        suffix = f"_{language.lower()}"
        return os.path.join(DATA_DIR, f"main_localization{suffix}.json")


def get_subtitles_path(language=None):
    """
    Get path to subtitles.json for specified language.

    Args:
        language: Language code like "EN_US", "DE_DE". If None, reads from settings.

    Returns:
        Path to subtitles file. English uses base filename, others use suffix.
        e.g., "subtitles.json" (EN) or "subtitles_de_de.json" (DE)
    """
    if language is None:
        settings = load_settings()
        language = settings.get('setup', {}).get('language', 'EN_US')

    if language == 'EN_US':
        return os.path.join(DATA_DIR, "subtitles.json")
    else:
        suffix = f"_{language.lower()}"
        return os.path.join(DATA_DIR, f"subtitles{suffix}.json")


def invalidate_cache():
    """Clear localization caches and dependent registry caches. Call when language changes."""
    _cache.localization = None
    _cache.reverse_localization = None
    _cache.cached_language = None
    # Invalidate downstream caches that derive from localization
    try:
        from .commitments import _invalidate_registry_display_cache
        _invalidate_registry_display_cache()
    except ImportError:
        pass
    try:
        from .lua_socket import LuaSocketServer
        LuaSocketServer._location_reverse_map = None
    except (ImportError, AttributeError):
        pass


def load_localization():
    """Load main_localization.json with caching. Automatically reloads if language changed."""
    # Get current language setting
    settings = load_settings()
    current_language = settings.get('setup', {}).get('language', 'EN_US')

    # Invalidate cache if language changed
    if _cache.cached_language is not None and _cache.cached_language != current_language:
        print(f"[Localization] Language changed from {_cache.cached_language} to {current_language}, reloading")
        invalidate_cache()

    if _cache.localization is None:
        # Lock to prevent race condition on first load with threaded Flask
        with _LocalizationCache._lock:
            # Double-check after acquiring lock
            if _cache.localization is None:
                loc_path = get_localization_path(current_language)
                try:
                    if os.path.exists(loc_path):
                        with open(loc_path, 'r', encoding='utf-8') as f:
                            _cache.localization = json.load(f)
                        _cache.cached_language = current_language
                        print(f"[Localization] Loaded {len(_cache.localization)} entries from {os.path.basename(loc_path)}")
                    else:
                        print(f"[Localization] File not found: {loc_path}")
                        _cache.localization = {}
                except Exception as e:
                    print(f"[Localization] Error loading: {e}")
                    _cache.localization = {}
    return _cache.localization


def get_display_name(npc_id):
    """
    Convert NPC ID to display name using localization.

    Args:
        npc_id: Internal ID like "NellieOggspire", "SebastianSallow"

    Returns:
        Display name like "Nellie Oggspire", "Sebastian Sallow"
    """
    if not npc_id:
        return "Unknown"

    # Check localization for proper display name
    loc = load_localization()
    if npc_id in loc:
        return loc[npc_id]

    # Owl Post-only custom characters can provide their own display names.
    try:
        from .owl_custom_characters import get_custom_owl_character_display_name
        custom_name = get_custom_owl_character_display_name(npc_id)
        if custom_name:
            return custom_name
    except Exception:
        pass

    # Fallback: add spaces at camelCase boundaries
    # "NellieOggspire" -> "Nellie Oggspire"
    return re.sub(r'([a-z])([A-Z])', r'\1 \2', npc_id)


def get_reverse_localization():
    """Build reverse lookup: display_name.lower() -> id"""
    if _cache.reverse_localization is None:
        with _LocalizationCache._lock:
            if _cache.reverse_localization is None:
                loc = load_localization()
                reverse = {}
                for slug, display_name in loc.items():
                    if display_name and isinstance(display_name, str):
                        # Skip menu/UI keys — these are not NPC IDs and can
                        # shadow real NPC slugs (e.g. Menu_Opponent5 -> "Professor Ronen")
                        if slug.startswith("Menu_"):
                            continue
                        # Store lowercase for case-insensitive lookup
                        reverse[display_name.lower()] = slug
                _cache.reverse_localization = reverse
    return _cache.reverse_localization


def get_lowercase_map():
    """Load lowercase_map.json for IDs that the game provides in lowercase."""
    if _cache.lowercase_map is None:
        with _LocalizationCache._lock:
            if _cache.lowercase_map is None:
                map_path = os.path.join(DATA_DIR, "lowercase_map.json")
                try:
                    if os.path.exists(map_path):
                        with open(map_path, 'r', encoding='utf-8') as f:
                            _cache.lowercase_map = json.load(f)
                    else:
                        print(f"[Localization] File not found: {map_path}")
                        _cache.lowercase_map = {}
                except Exception as e:
                    print(f"[Localization] Error loading: {e}")
                    _cache.lowercase_map = {}
    return _cache.lowercase_map


def canonicalize_npc_id(npc_id):
    """
    Normalize an NPC ID to the canonical slug used by localization/history.

    Handles:
    - special values like Player / Unknown
    - lowercase game-emitted IDs using lowercase_map.json
    - already-canonical localization slugs
    """
    if npc_id is None:
        return None

    npc_id = str(npc_id).strip()
    if not npc_id:
        return None

    lowered = npc_id.lower()
    if lowered == 'player':
        return 'Player'
    if lowered == 'unknown':
        return 'Unknown'

    loc = load_localization()
    if npc_id in loc:
        return npc_id

    lowercase_map = get_lowercase_map()
    if npc_id in lowercase_map:
        return npc_id

    for canonical_id, lower_id in lowercase_map.items():
        if isinstance(lower_id, str) and lower_id.lower() == lowered:
            return canonical_id

    return npc_id


def id_from_name(name, nearby_npcs=None):
    """
    Find character ID (slug) from a display name or partial name.

    Args:
        name: Display name like "Nellie Oggspire", "Nellie", or slug "NellieOggspire"
        nearby_npcs: Optional list of nearby NPCs to check first (fastest path)

    Returns:
        Character ID (slug) like "NellieOggspire", or input with spaces removed as fallback
    """
    if not name:
        return name

    name_lower = name.lower().replace(" ", "")
    name_lower_spaces = name.lower()
    name_no_spaces = name.replace(" ", "")

    # 1. Check nearby NPCs first (exact match on slug)
    if nearby_npcs:
        for npc in nearby_npcs:
            npc_id = npc.get('name', '')
            if npc_id.lower() == name_lower:
                return npc_id

        # If the LLM returns a display name like "Slytherin Student", prefer
        # the live nearby actor over the global localization reverse map. Many
        # generic and cut/scrapped characters share display names.
        loc = load_localization()
        for npc in nearby_npcs:
            npc_id = npc.get('name', '')
            if not npc_id:
                continue
            display_name = loc.get(npc_id) or get_display_name(npc_id)
            display_lower = display_name.lower()
            display_no_spaces = display_lower.replace(' ', '')
            if name_lower_spaces == display_lower or name_lower == display_no_spaces:
                return npc_id

    # 2. Check lowercase_map.json for IDs that are given by game in lowercase
    lowercase_map = get_lowercase_map()
    # e.g. NeridaRoberts or Nerida Roberts -> neridaroberts
    if name in lowercase_map:
        return canonicalize_npc_id(name)
    if name_no_spaces in lowercase_map:
        return canonicalize_npc_id(name_no_spaces)

    # 2. Check localization for exact display name match
    reverse_loc = get_reverse_localization()
    if name_lower_spaces in reverse_loc:
        return reverse_loc[name_lower_spaces]

    # 3. Check if name is already a valid slug in localization
    loc = load_localization()
    if name in loc:
        return canonicalize_npc_id(name)
    # Try with spaces removed
    name_no_spaces = name.replace(" ", "")
    if name_no_spaces in loc:
        return canonicalize_npc_id(name_no_spaces)

    # 4. Partial match nearby display names before global localization, for
    # ambiguous labels like "Slytherin Student".
    if nearby_npcs:
        for npc in nearby_npcs:
            npc_id = npc.get('name', '')
            if not npc_id:
                continue
            display_name = loc.get(npc_id) or get_display_name(npc_id)
            display_lower = display_name.lower()
            first_name = display_lower.split()[0] if ' ' in display_lower else display_lower
            if display_lower.startswith(name_lower_spaces) or first_name == name_lower_spaces:
                return npc_id

    # 5. Partial match - check if any display name STARTS with or CONTAINS the input
    # (handles "Nellie" matching "Nellie Oggspire")
    for display_lower, slug in reverse_loc.items():
        # Check if input is the start of a display name
        if display_lower.startswith(name_lower_spaces):
            return slug
        # Check first name match (e.g., "Nellie" matches "Nellie Oggspire")
        first_name = display_lower.split()[0] if ' ' in display_lower else display_lower
        if first_name == name_lower_spaces:
            return slug

    # 6. Check nearby NPCs for partial match
    if nearby_npcs:
        for npc in nearby_npcs:
            npc_id = npc.get('name', '')
            npc_lower = npc_id.lower()
            # Partial match on slug
            if npc_lower.startswith(name_lower) or name_lower in npc_lower:
                return npc_id

    # 7. Fallback: return with spaces removed
    print(f"[Localization] No ID found for '{name}', using fallback")
    return name.replace(" ", "")


def strict_id_from_name(name):
    """
    Resolve a display name to an internal slug without any fuzzy fallbacks.

    Allowed resolution paths only:
    - lowercase_map.json exact match
    - reverse localization exact display-name match
    - already-a-slug check

    Returns:
        Canonical slug string when resolved, otherwise None.
    """
    if not name:
        return None

    name = str(name).strip()
    if not name:
        return None

    name_no_spaces = name.replace(" ", "")
    name_lower_spaces = name.lower()

    lowercase_map = get_lowercase_map()
    if name in lowercase_map:
        return canonicalize_npc_id(name)
    if name_no_spaces in lowercase_map:
        return canonicalize_npc_id(name_no_spaces)

    reverse_loc = get_reverse_localization()
    if name_lower_spaces in reverse_loc:
        return reverse_loc[name_lower_spaces]

    loc = load_localization()
    if name in loc:
        return canonicalize_npc_id(name)
    if name_no_spaces in loc:
        return canonicalize_npc_id(name_no_spaces)

    return None


def find_npc_id_by_name(display_name, nearby_npcs):
    """
    Find the NPC ID (slug) from a display name by matching against nearby NPCs.
    Uses localization reverse lookup for proper ID resolution.

    Args:
        display_name: Display name like "Nellie Oggspire", "Nellie", or "NellieOggspire"
        nearby_npcs: List of NPC dicts with 'name' field (slug format)

    Returns:
        NPC ID (slug) if found, otherwise the input with spaces removed
    """
    npc_id = id_from_name(display_name, nearby_npcs)
    return npc_id
