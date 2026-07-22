"""
Shared helpers for static bios, editor guidance, and effective memory state.
"""

from __future__ import annotations

from typing import Optional

from .settings import load_settings


def is_npc_memory_effectively_enabled(npc_id: str, settings=None) -> bool:
    """Return True when long-term memory is effectively enabled for an NPC."""
    if not npc_id or str(npc_id).strip().lower() == 'player':
        return False

    settings = settings or load_settings()
    memory_settings = settings.get('memory', {})
    if not memory_settings.get('enabled', False):
        return False

    if not memory_settings.get('whitelisted_npcs_only', False):
        return True

    allowlist = memory_settings.get('npc_long_term_memory', {})
    if not isinstance(allowlist, dict):
        return False

    npc_norm = str(npc_id).strip().lower()
    for raw_id, is_enabled in allowlist.items():
        if is_enabled is True and str(raw_id).strip().lower() == npc_norm:
            return True
    return False


def _lookup_character_text(mapping, npc_id=None, display_name=None) -> str:
    if not isinstance(mapping, dict):
        return ""
    if npc_id and mapping.get(npc_id):
        return mapping.get(npc_id, "")
    if display_name and mapping.get(display_name):
        return mapping.get(display_name, "")
    return ""


def get_static_bio(npc_id=None, display_name=None, settings=None) -> str:
    """Get static bio text for a character by id or display name."""
    settings = settings or load_settings()
    mapping = settings.get('prompts', {}).get('static_bios', {})
    return _lookup_character_text(mapping, npc_id=npc_id, display_name=display_name)


def get_editor_guidance(npc_id=None, display_name=None, settings=None) -> str:
    """Get editor guidance text for a character by id or display name."""
    settings = settings or load_settings()
    mapping = settings.get('prompts', {}).get('editor_guidance', {})
    return _lookup_character_text(mapping, npc_id=npc_id, display_name=display_name)


def get_player_static_bio(player_name: Optional[str] = None, settings=None) -> str:
    """Get the static player bio, with legacy editor-guidance fallback for compatibility."""
    settings = settings or load_settings()
    prompts = settings.get('prompts', {})
    static_bios = prompts.get('static_bios', {})
    editor_guidance = prompts.get('editor_guidance', {})

    if static_bios.get('Player'):
        return static_bios.get('Player', '')
    if player_name and static_bios.get(player_name):
        return static_bios.get(player_name, '')
    if editor_guidance.get('Player'):
        return editor_guidance.get('Player', '')
    if player_name and editor_guidance.get(player_name):
        return editor_guidance.get(player_name, '')
    return ''


def build_prompt_bio_sections(
    npc_id,
    display_name,
    *,
    player_name=None,
    prompt_mode=False,
    include_player_bio=True,
    settings=None,
):
    """Build static-bio grounding sections when long-term memory is effectively off."""
    settings = settings or load_settings()
    if is_npc_memory_effectively_enabled(npc_id, settings=settings):
        return []

    bio_sections = []
    npc_bio = get_static_bio(npc_id=npc_id, display_name=display_name, settings=settings)
    if npc_bio:
        bio_sections.append(f"About you ({display_name}): {npc_bio}")

    if not prompt_mode and include_player_bio and player_name:
        player_bio = get_player_static_bio(player_name=player_name, settings=settings)
        if player_bio:
            bio_sections.append(f"About {player_name}: {player_bio}")

    return bio_sections
