"""
Text processing utilities for Sonorus.
Handles text splitting, name sanitization, and NPC filtering.
"""

import re
from constants import CONVERSATION_EARSHOT_DISTANCE, STEALTH_EARSHOT_DISTANCE


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


def filter_npcs_by_earshot(nearby_npcs, max_distance=None, player_in_stealth=False):
    """
    Filter nearby NPCs to only those within earshot distance for conversations.

    Args:
        nearby_npcs: List of NPC dicts with 'name' and 'distance' fields
        max_distance: Max distance in UE units (default: CONVERSATION_EARSHOT_DISTANCE)
        player_in_stealth: If True, uses reduced stealth distance (Disillusionment active)

    Returns:
        Filtered list of NPCs within earshot
    """
    if max_distance is None:
        if player_in_stealth:
            max_distance = STEALTH_EARSHOT_DISTANCE
        else:
            max_distance = CONVERSATION_EARSHOT_DISTANCE

    return [npc for npc in nearby_npcs if npc.get('distance', float('inf')) <= max_distance]


def validate_speaker_in_nearby(npc_id, nearby_npcs, load_localization_func=None):
    """
    Validate that an NPC is actually in the nearby NPC list.

    Args:
        npc_id: Internal NPC ID (e.g., "SebastianSallow") - also works with display names
        nearby_npcs: List of NPC dicts with 'name' field (ID format)
        load_localization_func: Optional function to load localization data for fallback

    Returns:
        True if NPC is in nearby list, False otherwise
    """
    if not npc_id or not nearby_npcs:
        return False

    # Normalize ID for comparison (remove spaces in case display name was passed)
    npc_id_lower = npc_id.lower().replace(' ', '')

    for npc in nearby_npcs:
        nearby_id = npc.get('name', '')
        # Compare with spaces removed
        nearby_id_lower = nearby_id.lower().replace(' ', '')

        # Exact match
        if npc_id_lower == nearby_id_lower:
            return True

        # Partial match (handles "Sebastian" matching "SebastianSallow")
        if npc_id_lower in nearby_id_lower or nearby_id_lower in npc_id_lower:
            return True

        # Check display name via localization if function provided
        if load_localization_func:
            loc = load_localization_func()
            display_name = loc.get(nearby_id, '')
            if display_name:
                display_lower = display_name.lower().replace(' ', '')
                if npc_id_lower == display_lower or npc_id_lower in display_lower:
                    return True

    return False


# ============================================
# Voice Spell Detection
# ============================================
# Spell index: normalized spoken name -> internal game spell name
SPELL_INDEX = {
    # Control spells (Yellow)
    "arresto momentum": "ArrestoMomentum",
    "glacius": "Glacius",
    "levioso": "Levioso",
    "transformation": "Transformation",

    # Force spells (Purple)
    "accio": "Accio",
    "depulso": "Depulso",
    "descendo": "Descendo",
    "flipendo": "Flipendo",

    # Damage spells (Red)
    "confringo": "Confringo",
    "diffindo": "Diffindo",
    "expelliarmus": "Expelliarmus",
    "incendio": "Incendio",
    "expulso": "Expulso",

    # Utility spells
    "disillusionment": "Disillusionment",
    "lumos": "Lumos",
    "reparo": "Reparo",
    "wingardium leviosa": "WingardiumLeviosa",
    "conjuration": "Conjuration",
    "evanesco": "Vanishment",
    "vanishment": "Vanishment",

    # Unforgivable Curses
    "avada kedavra": "AvadaKedavra",
    "crucio": "Crucio",
    "imperio": "Imperio",

    # Essential spells
    "revelio": "Revelio",
    "protego": "Protego",
    "stupefy": "Stupefy",
    "petrificus totalus": "PetrificusTotalus",
    "petrificus": "PetrificusTotalus",

    # Other spells
    "confundo": "Confundo",
    "oppugno": "Oppugno",
    "obliviate": "Obliviate",
    "episkey": "Episkey",

    # Common mispronunciations/alternatives
    "stupify": "Stupefy",
    "stupiphy": "Stupefy",
    "expeliarmus": "Expelliarmus",
    "avada cadavra": "AvadaKedavra",
    "wingardium": "WingardiumLeviosa",
    "leviosa": "Levioso",
    "aresto momentum": "ArrestoMomentum",
    "arresto": "ArrestoMomentum",
    "nox": "Lumos",  # Nox cancels Lumos (toggle)
    # Note: Bombarda appears to be a talent upgrade for Confringo, not a separate spell
}


def normalize_spell_text(text):
    """Normalize text for spell matching (lowercase, strip punctuation, trim whitespace)."""
    if not text:
        return ""
    # Lowercase, strip punctuation, normalize whitespace
    text = text.lower()
    text = re.sub(r'[^\w\s]', '', text)  # Remove punctuation
    text = ' '.join(text.split())  # Normalize whitespace
    return text


def detect_spell_in_text(text):
    """
    Detect if text contains a spell name.

    Returns:
        tuple: (internal_spell_name, matched_text) if found, (None, None) otherwise
    """
    if not text:
        return None, None

    normalized = normalize_spell_text(text)

    # Check exact match first
    if normalized in SPELL_INDEX:
        return SPELL_INDEX[normalized], normalized

    # Check if text contains a spell name (longer names first to avoid partial matches)
    sorted_spells = sorted(SPELL_INDEX.keys(), key=len, reverse=True)

    for spell_name in sorted_spells:
        if spell_name in normalized:
            return SPELL_INDEX[spell_name], spell_name

    return None, None
