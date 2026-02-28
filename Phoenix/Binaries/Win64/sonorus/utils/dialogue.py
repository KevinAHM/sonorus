"""
Dialogue history utilities for Sonorus.
Handles loading, saving, filtering, and formatting of dialogue history.
"""

import os
import re
import json

from .settings import DATA_DIR, load_settings
from .localization import get_display_name
from constants import DIALOGUE_HISTORY_LIMIT

# Import DB functions
from .dialogue_db import (
    load_all_entries as _db_load_all,
    replace_all_entries as _db_replace_all,
)


def load_dialogue_history(game_context=None):
    """
    Load dialogue history from database, collapsing consecutive duplicates.

    Args:
        game_context: Either a dict with 'playerName', or a callable that returns such a dict.
                     Accepts both for backwards compatibility.
    """
    try:
        raw_history = _db_load_all()

        # Get player name to normalize player entries
        # Support both dict and callable for backwards compatibility
        player_name = ''
        try:
            if game_context:
                if callable(game_context):
                    ctx = game_context()  # Call if it's a function
                else:
                    ctx = game_context  # Use directly if it's a dict
                player_name = (ctx.get('playerName') or '').lower()
        except:
            pass

        # Collapse consecutive identical NPC lines (cleans up rapid-fire repeats from Lua)
        cleaned = []
        for entry in raw_history:
            # Skip non-dict entries (corrupted data - shouldn't happen with DB)
            if not isinstance(entry, dict):
                print(f"[DialogueHistory] WARNING: Skipping non-dict entry: {type(entry).__name__} = {repr(entry)[:100]}")
                continue

            # Normalize player entries (Lua captures player voice lines without isPlayer flag)
            if player_name and not entry.get('isAIResponse'):
                speaker = (entry.get('speaker') or '').lower()
                voice_name = (entry.get('voiceName') or '').lower()
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
    except Exception as e:
        print(f"[DialogueHistory] Error loading history: {e}")
        return []


def replace_dialogue_history(history):
    """Replace entire dialogue history - BULK OPERATIONS ONLY.

    WARNING: This deletes ALL existing entries and reinserts everything.
    Use append_entry() from dialogue_db for normal single-entry writes.

    Valid use cases:
    - Import from JSON backup
    - Clear NPC from history (filter + replace)
    - Delete specific entries by timestamp
    """
    try:
        _db_replace_all(history)
    except Exception as e:
        print(f"[ERROR] Failed to replace dialogue history: {e}")


# Backwards compatibility alias - but prefer replace_dialogue_history for clarity
save_dialogue_history = replace_dialogue_history


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
        is_system_event = entry_type in ("location", "broom", "spell", "commitment")

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

    speaker = entry.get("speaker") or "Unknown"
    voice_name = entry.get("voiceName") or ""
    target = entry.get("target") or ""
    text = entry.get("text") or ""
    game_time = entry.get("gameTime") or ""
    is_ai = entry.get("isAIResponse", False)
    is_player = entry.get("isPlayer", False)
    entry_type = entry.get("type") or ""

    if not text:
        return None

    # Time prefix
    time_prefix = f"[{game_time}] " if (include_time and game_time) else ""

    # Player tag prefix
    player_prefix = "[PLAYER] " if (mark_player and is_player) else ""

    # Handle director prompt entries (NPC-to-NPC scene direction)
    if entry_type == 'prompt':
        # Director prompts are scene direction, not dialogue - skip in history
        # or format as a stage direction
        return f"{time_prefix}[Scene: {text}]"

    # Handle location transition entries
    if entry_type == 'location':
        location = entry.get('location', text.replace('Entered ', ''))
        companions = entry.get('companions')
        if companions:
            names = f"{speaker} and {', '.join(companions)}"
        else:
            names = speaker
        return f"{time_prefix}[{player_prefix}{names} entered {location}]"

    # Handle broom mount/dismount entries
    if entry_type == 'broom':
        return f"{time_prefix}[{text}]"

    # Handle spell entries
    if entry_type == 'spell':
        count = entry.get('count', 1)
        if count > 1:
            # Time range format for collapsed spells
            first_time = entry.get('firstGameTime') or ''
            last_time = game_time
            if include_time and first_time and last_time and first_time != last_time:
                time_str = f"[{first_time}-{last_time}] "
            else:
                time_str = time_prefix
            return f"{time_str}{player_prefix}{speaker}: {text} ({count}x)"
        else:
            return f"{time_prefix}{player_prefix}{speaker}: {text}"

    # Handle combat entries
    if entry_type == 'combat':
        # Time range format for combat (start to end)
        start_time = entry.get('firstGameTime') or ''
        end_time = game_time
        if include_time and start_time and end_time and start_time != end_time:
            time_str = f"[{start_time}-{end_time}] "
        else:
            time_str = time_prefix
        return f"{time_str}Combat: {text}"

    # Commitment events
    if entry_type == 'commitment':
        return f"{time_prefix}[{text}]"

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


def _parse_game_time(time_str):
    """Parse game time string like '2:09 AM' or '11:30 PM' into minutes since midnight."""
    if not time_str:
        return None
    try:
        time_str = time_str.strip()
        # Handle "2:09 AM" format
        parts = time_str.replace(':', ' ').replace('  ', ' ').split()
        if len(parts) < 2:
            return None
        hour = int(parts[0])
        minute = int(parts[1])
        period = parts[-1].upper() if len(parts) >= 3 else None
        if period == 'AM':
            if hour == 12:
                hour = 0
        elif period == 'PM':
            if hour != 12:
                hour += 12
        return hour * 60 + minute
    except (ValueError, IndexError):
        return None


def _game_datetime_to_minutes(game_date, game_time):
    """Convert game date + time to total minutes (for gap calculation)."""
    date = _parse_game_date(game_date)
    time_mins = _parse_game_time(game_time)
    if not date or time_mins is None:
        return None
    # Use approximate days (30-day months) * 1440 minutes/day + time
    total_days = date[0] * 365 + date[1] * 30 + date[2]
    return total_days * 1440 + time_mins


def _npc_witnessed(entry, npc_id):
    """Check if an NPC witnessed a dialogue entry (was speaker or in earshot)."""
    # NPC was the speaker
    if entry.get('voiceName') == npc_id:
        return True
    # NPC was in earshot
    if npc_id in entry.get('earshot', []):
        return True
    return False


def get_time_since_last_interaction(history, for_npc_id, current_game_date, current_game_time, player_name=None):
    """Find the time gap (in game minutes) since the NPC last interacted with the player.

    Only considers entries the NPC witnessed (same earshot filter as dialogue history),
    then looks for entries where the player was involved:
    - Player speaking (isPlayer=True) and NPC witnessed it
    - AI response from this NPC directed at the player
    - Cutscene dialogue the NPC witnessed (player assumed present)

    Ignores: NPC-to-NPC conversations, ambient chatter, combat, broom, spell, location.

    Returns (gap_minutes, date_formatted) or (None, None) if no prior interaction found.
    """
    if not history or not for_npc_id:
        return None, None

    current_mins = _game_datetime_to_minutes(current_game_date, current_game_time)
    if current_mins is None:
        return None, None

    # Apply same earshot filter as format_dialogue_history
    settings = load_settings()
    realistic_memory = settings.get('history', {}).get('realistic_memory', True)

    player_name_lower = (player_name or '').lower()

    # Scan backwards for the last meaningful interaction with the player
    for entry in reversed(history):
        if not isinstance(entry, dict):
            continue

        # Earshot filter: only entries the NPC witnessed
        if realistic_memory and not _npc_witnessed(entry, for_npc_id):
            continue

        entry_type = entry.get('type', 'dialogue')

        # Skip system events
        if entry_type in ('location', 'broom', 'spell', 'combat', 'prompt', 'commitment'):
            continue

        is_ai = entry.get('isAIResponse', False)
        is_player = entry.get('isPlayer', False)
        target = (entry.get('target') or '').lower()

        # Cutscene dialogue the NPC witnessed - player was present
        if entry_type == 'cutscene':
            pass  # Accept
        # Player speaking and NPC heard it
        elif is_player:
            pass  # Accept
        # AI response directed at the player (not NPC-to-NPC)
        elif is_ai and player_name_lower and target == player_name_lower:
            pass  # Accept
        else:
            # NPC-to-NPC conversation or ambient chatter - skip
            continue

        # Found a matching entry - compute gap
        entry_date = entry.get('gameDate', '')
        entry_time = entry.get('gameTime', '')
        entry_mins = _game_datetime_to_minutes(entry_date, entry_time)
        if entry_mins is None:
            return None, None

        gap = current_mins - entry_mins
        return gap, entry_date

    return None, None


def format_time_gap(gap_minutes):
    """Format a gap in game minutes into a human-readable string.

    Returns string like '2 hours', '3 days', '1 week', or None if gap is small.
    Only returns a value if gap >= 60 minutes.
    """
    if gap_minutes is None or gap_minutes < 60:
        return None

    if gap_minutes < 120:
        return "about an hour"
    elif gap_minutes < 1440:
        hours = gap_minutes // 60
        return f"about {hours} hours"
    elif gap_minutes < 2880:
        return "about a day"
    elif gap_minutes < 10080:
        days = gap_minutes // 1440
        return f"about {days} days"
    elif gap_minutes < 20160:
        return "about a week"
    elif gap_minutes < 43200:
        weeks = gap_minutes // 10080
        return f"about {weeks} weeks"
    elif gap_minutes < 86400:
        return "about a month"
    else:
        months = gap_minutes // 43200
        return f"about {months} months"


_MONTH_NAMES = {
    'january': 1, 'february': 2, 'march': 3, 'april': 4,
    'may': 5, 'june': 6, 'july': 7, 'august': 8,
    'september': 9, 'october': 10, 'november': 11, 'december': 12,
}


def _parse_game_date(date_str):
    """Parse game date string into (year, month, day) tuple.

    Supports both formats:
    - Short: '1891/01/13'
    - Long:  'Wednesday, January 14th, 1891'
    """
    if not date_str:
        return None

    date_str = date_str.strip()

    # Try short format first: YYYY/MM/DD
    if re.match(r'^\d{4}/\d{1,2}/\d{1,2}$', date_str):
        try:
            parts = date_str.split('/')
            return (int(parts[0]), int(parts[1]), int(parts[2]))
        except (ValueError, IndexError):
            return None

    # Try long format: "Wednesday, January 14th, 1891" or "January 14th, 1891"
    # Extract month name, day number, and year
    match = re.search(r'(\w+)\s+(\d{1,2})\w*,?\s+(\d{4})', date_str)
    if match:
        month_name = match.group(1).lower()
        month = _MONTH_NAMES.get(month_name)
        if month:
            try:
                return (int(match.group(3)), month, int(match.group(2)))
            except ValueError:
                return None

    return None


def _format_relative_date(entry_date, current_date):
    """Format relative time between two game dates.

    Returns string like '3 days ago', '1 month ago', 'yesterday', 'today'.
    """
    if not entry_date or not current_date:
        return ""

    entry = _parse_game_date(entry_date)
    current = _parse_game_date(current_date)
    if not entry or not current:
        return ""

    # Simple day calculation (assumes 30-day months for simplicity)
    entry_days = entry[0] * 365 + entry[1] * 30 + entry[2]
    current_days = current[0] * 365 + current[1] * 30 + current[2]
    diff = current_days - entry_days

    if diff <= 0:
        return "today"
    elif diff == 1:
        return "yesterday"
    elif diff < 7:
        return f"{diff} days ago"
    elif diff < 14:
        return "1 week ago"
    elif diff < 30:
        weeks = diff // 7
        return f"{weeks} weeks ago"
    elif diff < 60:
        return "1 month ago"
    elif diff < 365:
        months = diff // 30
        return f"{months} months ago"
    else:
        years = diff // 365
        return f"{years} year{'s' if years > 1 else ''} ago"


def format_dialogue_history(history, limit=None, for_npc_id=None, current_game_date=None):
    """Format dialogue history for LLM context.

    Args:
        history: List of dialogue history entries
        limit: Max entries to include (default from settings)
        for_npc_id: If provided, filter to only entries this NPC witnessed (was speaker or in earshot)
        current_game_date: Current game date string (e.g., '1891/01/15') for relative time display
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
        filtered = [entry for entry in filtered if _npc_witnessed(entry, for_npc_id)]

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
    prev_date_tuple = None
    for entry in recent:
        game_date = entry.get("gameDate") or ""
        date_tuple = _parse_game_date(game_date) if game_date else None

        # Add day divider when date changes (compare parsed tuples to handle mixed formats)
        if date_tuple and prev_date_tuple and date_tuple != prev_date_tuple:
            # Always display in short format for consistency
            display_date = f"{date_tuple[0]}/{date_tuple[1]:02d}/{date_tuple[2]:02d}"
            relative = _format_relative_date(game_date, current_game_date)
            if relative:
                lines.append(f"--- {display_date} ({relative}) ---")
            else:
                lines.append(f"--- {display_date} ---")
        if date_tuple:
            prev_date_tuple = date_tuple

        # Format the entry using shared helper
        line = format_dialogue_entry(entry, include_time=True, mark_player=False)
        if line:
            lines.append(line)

    if not lines:
        return ""

    return "## Recent History\n" + "\n".join(lines)
