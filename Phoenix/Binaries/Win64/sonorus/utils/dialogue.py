"""
Dialogue history utilities for Sonorus.
Handles loading, saving, filtering, and formatting of dialogue history.
"""

import os
import re
import json

from .settings import DATA_DIR, load_settings
from constants import DIALOGUE_HISTORY_LIMIT


def load_dialogue_history(game_context_func=None):
    """
    Load dialogue history from file, collapsing consecutive duplicates.

    Args:
        game_context_func: Optional function that returns game context dict with 'playerName'
    """
    path = os.path.join(DATA_DIR, "dialogue_history.json")
    try:
        with open(path, 'r', encoding='utf-8') as f:
            raw_history = json.load(f)

        # Get player name to normalize player entries
        player_name = ''
        try:
            if game_context_func:
                game_context = game_context_func()
                player_name = game_context.get('playerName', '').lower()
        except:
            pass

        # Collapse consecutive identical NPC lines (cleans up rapid-fire repeats from Lua)
        cleaned = []
        for entry in raw_history:
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


def prettify_voice_name(voice_name):
    """Convert voice name slugs to readable names"""
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

    # Add spaces before capital letters: "ErnieLark" -> "Ernie Lark"
    pretty = re.sub(r'([a-z])([A-Z])', r'\1 \2', voice_name)
    # Also handle sequences like "McGonagall" -> keep as is, but "MC" -> "M C" fix
    pretty = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', pretty)

    return pretty


def format_dialogue_history(history, limit=None):
    """Format dialogue history for LLM context"""
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
        speaker = entry.get("speaker", "Unknown")
        voice_name = entry.get("voiceName", "")
        target = entry.get("target", "")
        text = entry.get("text", "")
        game_date = entry.get("gameDate", "")
        game_time = entry.get("gameTime", "")
        is_ai = entry.get("isAIResponse", False)
        is_player = entry.get("isPlayer", False)

        if not text:
            continue

        # Add day divider when date changes
        if game_date and prev_date and game_date != prev_date:
            lines.append(f"--- {game_date} ---")
        prev_date = game_date if game_date else prev_date

        # Time prefix (just time since date shown in divider)
        time_prefix = f"[{game_time}] " if game_time else ""

        # Handle location transition entries
        if entry.get('type') == 'location':
            location = entry.get('location', text.replace('Entered ', ''))
            lines.append(f"{time_prefix}[{speaker} entered {location}]")
            continue

        # Handle broom mount/dismount entries
        if entry.get('type') == 'broom':
            lines.append(f"{time_prefix}[{text}]")
            continue

        # Handle collapsed spell entries
        if entry.get('type') == 'spell':
            count = entry.get('count', 1)
            spell_text = text
            if count > 1:
                # Time range format for collapsed spells
                first_time = entry.get('firstGameTime', '')
                last_time = game_time
                if first_time and last_time and first_time != last_time:
                    time_str = f"[{first_time}-{last_time}]"
                else:
                    time_str = time_prefix.rstrip()
                lines.append(f"{time_str} {speaker}: {spell_text} ({count}x)")
            else:
                lines.append(f"{time_prefix}{speaker}: {spell_text}")
            continue

        if is_player:
            # Player message with target
            if target:
                lines.append(f"{time_prefix}{speaker} (to {target}): {text}")
            else:
                lines.append(f"{time_prefix}{speaker}: {text}")
        elif is_ai:
            # AI response with target
            if target:
                lines.append(f"{time_prefix}{speaker} (to {target}): {text}")
            else:
                lines.append(f"{time_prefix}{speaker}: {text}")
        else:
            # NPC ambient dialogue - prettify the name
            raw_name = speaker if speaker and speaker != "Unknown" else voice_name
            display_name = prettify_voice_name(raw_name)
            # Show target if known (from game's dialogue tracking)
            if target:
                lines.append(f"{time_prefix}{display_name} (to {target}): {text}")
            else:
                lines.append(f"{time_prefix}{display_name}: {text}")

    if not lines:
        return ""

    return "Recent events and conversations:\n" + "\n".join(lines)
