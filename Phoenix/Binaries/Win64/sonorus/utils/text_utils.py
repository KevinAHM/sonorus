"""
Text processing utilities for Sonorus.
Handles text splitting, name sanitization, and NPC filtering.
"""

import re
from constants import CONVERSATION_EARSHOT_DISTANCE


def split_into_sentences(text):
    """Split text into sentences for chunked TTS"""
    # Split on sentence-ending punctuation followed by space or end
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    # Filter empty strings and strip whitespace
    return [s.strip() for s in sentences if s.strip()]


def sanitize_name(name):
    """Strip LLM garbage from names (quotes, asterisks, markdown, etc.)"""
    if not name:
        return name
    # Strip quotes, asterisks, backticks, brackets, common markdown
    name = re.sub(r'^[\s\'"*`\[\]]+', '', name)  # Leading garbage
    name = re.sub(r'[\s\'"*`\[\]]+$', '', name)  # Trailing garbage
    return name.strip()


def parse_target_result(result):
    """Parse target selection result like 'Sebastian>player' into (speaker, target)"""
    if not result or result == "0":
        return None, None

    result = result.strip()
    # Strip leading "- " if LLM included it from the formatted list
    if result.startswith("- "):
        result = result[2:]

    if ">" not in result:
        return sanitize_name(result), "player"

    parts = result.split(">", 1)
    speaker = sanitize_name(parts[0].strip())
    target = sanitize_name(parts[1].strip()) if len(parts) > 1 else "player"

    # Strip leading "- " from each part
    if speaker.startswith("- "):
        speaker = speaker[2:]
    if target.startswith("- "):
        target = target[2:]

    return speaker, target


def filter_npcs_by_earshot(nearby_npcs, max_distance=None):
    """
    Filter nearby NPCs to only those within earshot distance for conversations.

    Args:
        nearby_npcs: List of NPC dicts with 'name' and 'distance' fields
        max_distance: Max distance in UE units (default: CONVERSATION_EARSHOT_DISTANCE)

    Returns:
        Filtered list of NPCs within earshot
    """
    if max_distance is None:
        max_distance = CONVERSATION_EARSHOT_DISTANCE

    return [npc for npc in nearby_npcs if npc.get('distance', float('inf')) <= max_distance]


def validate_speaker_in_nearby(speaker_name, nearby_npcs, load_localization_func=None):
    """
    Validate that a selected speaker is actually in the nearby NPC list.

    Args:
        speaker_name: Display name selected by LLM (e.g., "Sebastian Sallow")
        nearby_npcs: List of NPC dicts with 'name' field (slug format)
        load_localization_func: Optional function to load localization data

    Returns:
        True if speaker is in nearby list, False otherwise
    """
    if not speaker_name or not nearby_npcs:
        return False

    # Normalize speaker name for comparison
    speaker_lower = speaker_name.lower().replace(' ', '')

    for npc in nearby_npcs:
        npc_name = npc.get('name', '')
        # Compare with spaces removed (slug format)
        npc_lower = npc_name.lower().replace(' ', '')

        # Exact match
        if speaker_lower == npc_lower:
            return True

        # Partial match (handles "Sebastian" matching "SebastianSallow")
        if speaker_lower in npc_lower or npc_lower in speaker_lower:
            return True

        # Check display name via localization if function provided
        if load_localization_func:
            loc = load_localization_func()
            display_name = loc.get(npc_name, '')
            if display_name:
                display_lower = display_name.lower().replace(' ', '')
                if speaker_lower == display_lower or speaker_lower in display_lower:
                    return True

    return False
