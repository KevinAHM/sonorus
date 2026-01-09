"""
Dialogue history utilities for Sonorus.
Handles loading, saving, filtering, and formatting of dialogue history.
"""

import os
import json

from .settings import DATA_DIR, load_settings
from .localization import get_display_name
from constants import DIALOGUE_HISTORY_LIMIT


def load_dialogue_history(game_context=None):
    """
    Load dialogue history from file, collapsing consecutive duplicates.

    Args:
        game_context: Either a dict with 'playerName', or a callable that returns such a dict.
                     Accepts both for backwards compatibility.
    """
    path = os.path.join(DATA_DIR, "dialogue_history.json")
    try:
        with open(path, 'r', encoding='utf-8') as f:
            raw_history = json.load(f)

        # Debug: check raw JSON for non-dict entries
        raw_bad = [(i, type(e).__name__) for i, e in enumerate(raw_history) if not isinstance(e, dict)]
        if raw_bad:
            print(f"[DialogueHistory] RAW JSON has {len(raw_bad)} non-dict entries: {raw_bad[:5]}")

        # Get player name to normalize player entries
        # Support both dict and callable for backwards compatibility
        player_name = ''
        try:
            if game_context:
                if callable(game_context):
                    ctx = game_context()  # Call if it's a function
                else:
                    ctx = game_context  # Use directly if it's a dict
                player_name = ctx.get('playerName', '').lower()
        except:
            pass

        # Collapse consecutive identical NPC lines (cleans up rapid-fire repeats from Lua)
        cleaned = []
        for entry in raw_history:
            # Skip non-dict entries (corrupted data)
            if not isinstance(entry, dict):
                print(f"[DialogueHistory] WARNING: Skipping non-dict entry: {type(entry).__name__} = {repr(entry)[:100]}")
                continue

            # Normalize player entries (Lua captures player voice lines without isPlayer flag)
            if player_name and not entry.get('isAIResponse'):
                speaker = entry.get('speaker', '').lower()
                voice_name = entry.get('voiceName', '').lower()
                # Match player name (with or without spaces - "AdriValter" vs "Adri Valter")
                player_name_nospace = player_name.replace(' ', '')
                if (speaker == player_name or
                    voice_name == player_name_nospace or
                    speaker == player_name_nospace):
                    entry['isPlayer'] = True
                    entry['voiceName'] = 'Player'

            if not collapse_consecutive_duplicate(cleaned, entry):
                cleaned.append(entry)

        # Collapse consecutive spell casts (e.g., Stupefy spam -> "Cast Stupefy (5x)")
        cleaned = collapse_consecutive_spells(cleaned)

        # Debug: check after processing
        post_bad = [(i, type(e).__name__) for i, e in enumerate(cleaned) if not isinstance(e, dict)]
        if post_bad:
            print(f"[DialogueHistory] AFTER PROCESSING has {len(post_bad)} non-dict entries: {post_bad[:5]}")

        return cleaned
    except:
        return []


def save_dialogue_history(history):
    """Save dialogue history to file"""
    path = os.path.join(DATA_DIR, "dialogue_history.json")
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(history, f, indent=2)
    except Exception as e:
        print(f"[ERROR] Failed to save dialogue history: {e}")


def collapse_consecutive_duplicate(history, new_entry):
    """
    Collapse consecutive identical NPC lines, keeping latest timestamp.
    Returns True if collapsed (entry merged into last), False if not (caller should append).
    """
    if not history:
        return False

    # Only collapse ambient NPC dialogue, not player/AI messages
    if new_entry.get('isPlayer') or new_entry.get('isAIResponse'):
        return False

    last = history[-1]

    # Don't collapse if last entry was player/AI
    if last.get('isPlayer') or last.get('isAIResponse'):
        return False

    # Check if same speaker saying same thing
    if (last.get('voiceName') == new_entry.get('voiceName') and
        last.get('text') == new_entry.get('text')):
        # Update timestamp to latest, don't append new entry
        last['timestamp'] = new_entry.get('timestamp', last.get('timestamp'))
        last['gameTime'] = new_entry.get('gameTime', last.get('gameTime'))
        return True

    return False


def collapse_consecutive_spells(history):
    """
    Collapse consecutive identical spell casts into single entries with count and time range.
    Returns collapsed list with count, firstGameTime, firstGameDate, etc. added to collapsed entries.
    """
    if not history:
        return []

    collapsed = []
    for entry in history:
        # Only collapse spell entries
        if entry.get('type') != 'spell':
            collapsed.append(entry)
            continue

        # Check if can merge with last entry
        if collapsed and collapsed[-1].get('type') == 'spell':
            last = collapsed[-1]
            # Same caster + same spell = collapse
            if (last.get('voiceName') == entry.get('voiceName') and
                last.get('lineID') == entry.get('lineID')):
                # Update count
                last['count'] = last.get('count', 1) + 1
                # Track time range (keep first*, update last*)
                if 'firstGameTime' not in last:
                    last['firstGameTime'] = last.get('gameTime')
                    last['firstGameDate'] = last.get('gameDate')
                    last['firstTimestamp'] = last.get('timestamp')
                last['lastGameTime'] = entry.get('gameTime')
                last['lastGameDate'] = entry.get('gameDate')
                last['lastTimestamp'] = entry.get('timestamp')
                last['gameTime'] = entry.get('gameTime')  # Display shows latest
                last['gameDate'] = entry.get('gameDate')
                last['timestamp'] = entry.get('timestamp')
                continue

        # Start new entry (copy to avoid mutating original)
        collapsed.append(entry.copy())

    return collapsed


def filter_dialogue_history(history):
    """
    Filter dialogue history to remove duplicate NPC chatter.
    Keeps the LATEST occurrence of each NPC line within the dedup window.
    Player/AI lines are never filtered.

    Special case: "Sole speaker" detection - when one NPC dominates the history
    (>80% of NPC lines), dedupe their lines globally regardless of time window.
    This handles NPCs with looping ambient dialogue (e.g., same 4 lines repeated).
    """
    if not history:
        return []

    # Get dedup windows from settings
    settings = load_settings()
    ambient_dedup_minutes = settings.get('history', {}).get('ambient_dedup_window', 15)

    # First pass: detect sole speaker (one NPC dominating history)
    npc_line_counts = {}  # voice_name -> count
    total_npc_lines = 0

    for entry in history:
        is_player_or_ai = (
            entry.get("isPlayer", False) or
            entry.get("isAIResponse", False) or
            "player" in entry.get("speaker", "").lower() or
            "player" in entry.get("voiceName", "").lower()
        )
        if not is_player_or_ai and entry.get("type") != "spell":
            voice_name = entry.get("voiceName", "")
            if voice_name:
                npc_line_counts[voice_name] = npc_line_counts.get(voice_name, 0) + 1
                total_npc_lines += 1

    # Identify sole speakers (>80% of NPC lines)
    sole_speakers = set()
    if total_npc_lines >= 3:  # Need at least 3 lines to detect pattern
        for voice_name, count in npc_line_counts.items():
            if count / total_npc_lines >= 0.8:
                sole_speakers.add(voice_name)

    # Process in REVERSE to keep latest occurrence (first seen when reversed = latest)
    # Key: (voice_name, text), Value: timestamp of the kept entry
    seen_npc_lines = {}

    filtered = []
    for entry in reversed(history):
        speaker = entry.get("speaker", "")
        voice_name = entry.get("voiceName", "")
        text = entry.get("text", "")
        timestamp = entry.get("timestamp", 0)
        is_ai = entry.get("isAIResponse", False)

        # Check if this is a player line, AI response, or system event (never filter these)
        entry_type = entry.get("type", "")
        is_player_or_ai = (
            entry.get("isPlayer", False) or
            is_ai or
            "player" in speaker.lower() or
            "player" in voice_name.lower()
        )
        # Also preserve location, broom, and other system events
        is_system_event = entry_type in ("location", "broom", "spell")

        if is_player_or_ai or is_system_event:
            # Always keep player, AI, and system event lines
            filtered.append(entry)
        else:
            # NPC ambient line - check for duplicates
            # Use voice_name for dedup since speaker is often "Unknown"
            key = (voice_name, text)

            if key in seen_npc_lines:
                # Sole speakers: dedupe globally (ignore time window)
                if voice_name in sole_speakers:
                    continue

                # Normal speakers: dedupe within time window only
                kept_timestamp = seen_npc_lines[key]
                if abs(kept_timestamp - timestamp) < (ambient_dedup_minutes * 60):
                    continue

            # Keep this line (it's the latest we've seen so far)
            seen_npc_lines[key] = timestamp
            filtered.append(entry)

    # Restore chronological order
    return list(reversed(filtered))


# Prefixes that indicate generic/ambient NPCs (not named characters)
GENERIC_NPC_PREFIXES = (
    "AdultMale", "AdultFemale", "ElderlyMale", "ElderlyFemale",
    "ChildMale", "ChildFemale", "TeenMale", "TeenFemale"
)


def is_named_npc(voice_name):
    """Return True if voice_name is a named NPC, not a generic townsperson."""
    if not voice_name:
        return False
    return not any(voice_name.startswith(prefix) for prefix in GENERIC_NPC_PREFIXES)


def prettify_voice_name(voice_name):
    """Convert voice name ID to readable display name.

    Args:
        voice_name: Internal voice ID (e.g., "SebastianSallow", "AdultMaleA")

    Returns:
        Display name (e.g., "Sebastian Sallow", "Male Townsperson")
    """
    if not voice_name:
        return "Unknown"

    # Generic NPC voices -> descriptive labels
    generic_map = {
        "AdultMale": "Male Townsperson",
        "AdultFemale": "Female Townsperson",
        "ElderlyMale": "Elderly Man",
        "ElderlyFemale": "Elderly Woman",
        "ChildMale": "Boy",
        "ChildFemale": "Girl",
        "TeenMale": "Teen Boy",
        "TeenFemale": "Teen Girl",
    }
    for prefix, label in generic_map.items():
        if voice_name.startswith(prefix):
            return label

    # Use localization for named NPCs
    return get_display_name(voice_name)


def format_dialogue_entry(entry, include_time=True, mark_player=False):
    """Format a single dialogue entry for LLM context.

    Args:
        entry: Dialogue entry dict with speaker, text, type, etc.
        include_time: Whether to include time prefix (default True)
        mark_player: Whether to prefix player entries with [PLAYER] (default False)

    Returns:
        Formatted string for this entry, or None if entry should be skipped
    """
    # Handle case where entry is already a string (shouldn't happen, but be defensive)
    if isinstance(entry, str):
        return entry if entry else None

    if not isinstance(entry, dict):
        return None

    speaker = entry.get("speaker", "Unknown")
    voice_name = entry.get("voiceName", "")
    target = entry.get("target", "")
    text = entry.get("text", "")
    game_time = entry.get("gameTime", "")
    is_ai = entry.get("isAIResponse", False)
    is_player = entry.get("isPlayer", False)
    entry_type = entry.get("type", "")

    if not text:
        return None

    # Time prefix
    time_prefix = f"[{game_time}] " if (include_time and game_time) else ""

    # Player tag prefix
    player_prefix = "[PLAYER] " if (mark_player and is_player) else ""

    # Handle location transition entries
    if entry_type == 'location':
        location = entry.get('location', text.replace('Entered ', ''))
        return f"{time_prefix}[{player_prefix}{speaker} entered {location}]"

    # Handle broom mount/dismount entries
    if entry_type == 'broom':
        return f"{time_prefix}[{text}]"

    # Handle spell entries
    if entry_type == 'spell':
        count = entry.get('count', 1)
        if count > 1:
            # Time range format for collapsed spells
            first_time = entry.get('firstGameTime', '')
            last_time = game_time
            if include_time and first_time and last_time and first_time != last_time:
                time_str = f"[{first_time}-{last_time}] "
            else:
                time_str = time_prefix
            return f"{time_str}{player_prefix}{speaker}: {text} ({count}x)"
        else:
            return f"{time_prefix}{player_prefix}{speaker}: {text}"

    # Regular dialogue
    if is_player or is_ai:
        # Player/AI message
        speaker_label = f"{player_prefix}{speaker}"
        if target:
            return f"{time_prefix}{speaker_label} (to {target}): {text}"
        else:
            return f"{time_prefix}{speaker_label}: {text}"
    else:
        # NPC ambient dialogue - prettify the name
        raw_name = speaker if speaker and speaker != "Unknown" else voice_name
        display_name = prettify_voice_name(raw_name)
        if target:
            return f"{time_prefix}{display_name} (to {target}): {text}"
        else:
            return f"{time_prefix}{display_name}: {text}"


def format_dialogue_history(history, limit=None, for_npc_id=None):
    """Format dialogue history for LLM context.

    Args:
        history: List of dialogue history entries
        limit: Max entries to include (default from settings)
        for_npc_id: If provided, filter to only entries this NPC witnessed (was speaker or in earshot)
    """
    if not history:
        return ""

    # Get settings
    settings = load_settings()
    if limit is None:
        limit = settings.get('history', {}).get('max_entries', DIALOGUE_HISTORY_LIMIT)

    # Get max location entries setting (default 2)
    max_location_entries = settings.get('history', {}).get('max_location_entries', 2)
    # Get max spell entries setting (default 3)
    max_spell_entries = settings.get('history', {}).get('max_spell_entries', 3)

    # Filter duplicates first
    filtered = filter_dialogue_history(history)

    # Filter by earshot if realistic memory is enabled and NPC specified
    realistic_memory = settings.get('history', {}).get('realistic_memory', True)
    if for_npc_id and realistic_memory:
        def npc_witnessed(entry):
            # NPC was the speaker
            if entry.get('voiceName') == for_npc_id:
                return True
            # NPC was in earshot
            if for_npc_id in entry.get('earshot', []):
                return True
            # Legacy handling: entries without earshot field
            if 'earshot' not in entry:
                # Player events (broom, location, spell) without earshot were never tracked
                # Exclude them since NPC couldn't have witnessed
                entry_type = entry.get('type', '')
                if entry_type in ('broom', 'location', 'spell'):
                    return False
                # Dialogue entries (chatter, cutscene, ai_response) - include for backwards compat
                return True
            return False
        filtered = [entry for entry in filtered if npc_witnessed(entry)]

    # Take last N entries
    recent = filtered[-limit:] if len(filtered) > limit else filtered

    # Limit location and spell entries to only the most recent N of each
    # Process in reverse to keep the most recent ones
    location_count = 0
    spell_count = 0
    limited = []
    for entry in reversed(recent):
        entry_type = entry.get('type')
        if entry_type == 'location':
            if max_location_entries > 0 and location_count < max_location_entries:
                limited.append(entry)
                location_count += 1
            # Skip if max is 0 or limit reached
        elif entry_type == 'spell':
            if max_spell_entries > 0 and spell_count < max_spell_entries:
                limited.append(entry)
                spell_count += 1
            # Skip if max is 0 or limit reached
        else:
            limited.append(entry)
    recent = list(reversed(limited))

    if not recent:
        return ""

    lines = []
    prev_date = None
    for entry in recent:
        game_date = entry.get("gameDate", "")

        # Add day divider when date changes
        if game_date and prev_date and game_date != prev_date:
            lines.append(f"--- {game_date} ---")
        prev_date = game_date if game_date else prev_date

        # Format the entry using shared helper
        line = format_dialogue_entry(entry, include_time=True, mark_player=False)
        if line:
            lines.append(line)

    if not lines:
        return ""

    return "**Recent events and conversations:**\n" + "\n".join(lines)
