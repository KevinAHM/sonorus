"""
Localization utilities for Sonorus.
Handles ID to display name mapping and reverse lookups.
"""

import os
import json
import re

from .settings import DATA_DIR

MAIN_LOCALIZATION_FILE = os.path.join(DATA_DIR, "main_localization.json")

# Module-level caches
_localization_cache = None
_reverse_localization_cache = None  # display_name.lower() -> id


def load_localization():
    """Load main_localization.json with caching"""
    global _localization_cache
    if _localization_cache is None:
        try:
            if os.path.exists(MAIN_LOCALIZATION_FILE):
                with open(MAIN_LOCALIZATION_FILE, 'r', encoding='utf-8') as f:
                    _localization_cache = json.load(f)
            else:
                _localization_cache = {}
        except Exception as e:
            print(f"[Localization] Error loading: {e}")
            _localization_cache = {}
    return _localization_cache


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

    # Fallback: add spaces at camelCase boundaries
    # "NellieOggspire" -> "Nellie Oggspire"
    return re.sub(r'([a-z])([A-Z])', r'\1 \2', npc_id)


def get_reverse_localization():
    """Build reverse lookup: display_name.lower() -> id"""
    global _reverse_localization_cache
    if _reverse_localization_cache is None:
        loc = load_localization()
        _reverse_localization_cache = {}
        for slug, display_name in loc.items():
            if display_name and isinstance(display_name, str):
                # Store lowercase for case-insensitive lookup
                _reverse_localization_cache[display_name.lower()] = slug
    return _reverse_localization_cache


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

    # 1. Check nearby NPCs first (exact match on slug)
    if nearby_npcs:
        for npc in nearby_npcs:
            npc_id = npc.get('name', '')
            if npc_id.lower() == name_lower:
                return npc_id

    # 2. Check localization for exact display name match
    reverse_loc = get_reverse_localization()
    if name_lower_spaces in reverse_loc:
        return reverse_loc[name_lower_spaces]

    # 3. Check if name is already a valid slug in localization
    loc = load_localization()
    if name in loc:
        return name
    # Try with spaces removed
    name_no_spaces = name.replace(" ", "")
    if name_no_spaces in loc:
        return name_no_spaces

    # 4. Partial match - check if any display name STARTS with or CONTAINS the input
    # (handles "Nellie" matching "Nellie Oggspire")
    for display_lower, slug in reverse_loc.items():
        # Check if input is the start of a display name
        if display_lower.startswith(name_lower_spaces):
            return slug
        # Check first name match (e.g., "Nellie" matches "Nellie Oggspire")
        first_name = display_lower.split()[0] if ' ' in display_lower else display_lower
        if first_name == name_lower_spaces:
            return slug

    # 5. Check nearby NPCs for partial match
    if nearby_npcs:
        for npc in nearby_npcs:
            npc_id = npc.get('name', '')
            npc_lower = npc_id.lower()
            # Partial match on slug
            if npc_lower.startswith(name_lower) or name_lower in npc_lower:
                return npc_id

    # 6. Fallback: return with spaces removed
    print(f"[Localization] No ID found for '{name}', using fallback")
    return name.replace(" ", "")


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
    return id_from_name(display_name, nearby_npcs)
