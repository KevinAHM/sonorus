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
    load_all_entries_fast as _db_load_all_fast,
    replace_all_entries as _db_replace_all,
)


def _normalize_and_collapse_dialogue_history(raw_history, game_context=None):
    """Normalize player rows and collapse duplicate/spell entries for display."""
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
    return collapse_consecutive_spells(cleaned)


def load_dialogue_history(game_context=None):
    """
    Load dialogue history from database, collapsing consecutive duplicates.

    Args:
        game_context: Either a dict with 'playerName', or a callable that returns such a dict.
                     Accepts both for backwards compatibility.
    """
    try:
        raw_history = _db_load_all()
        return _normalize_and_collapse_dialogue_history(raw_history, game_context=game_context)
    except Exception as e:
        print(f"[DialogueHistory] Error loading history: {e}")
        return []


def load_dialogue_history_fast(game_context=None):
    """
    Load dialogue history using the fast read-only DB path.
    Intended for hot UI listing routes that should not mutate the DB on read.
    """
    try:
        raw_history = _db_load_all_fast()
        return _normalize_and_collapse_dialogue_history(raw_history, game_context=game_context)
    except Exception as e:
        print(f"[DialogueHistory] Error loading fast history: {e}")
        return []


def load_dialogue_history_fast_with_raw_count(game_context=None):
    """
    Load fast dialogue history plus the pre-collapse raw row count.
    Returns (history, raw_count).
    """
    try:
        raw_history = _db_load_all_fast()
        history = _normalize_and_collapse_dialogue_history(raw_history, game_context=game_context)
        return history, len(raw_history)
    except Exception as e:
        print(f"[DialogueHistory] Error loading fast history with raw count: {e}")
        return [], 0


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


def _merge_source_entry_ids(target_entry, source_entry):
    """Preserve raw DB row IDs when UI-visible history rows are merged."""
    merged = []
    for entry in (target_entry, source_entry):
        ids = entry.get('sourceEntryIds', [])
        if isinstance(ids, list):
            merged.extend(ids)

    if merged:
        target_entry['sourceEntryIds'] = sorted(set(merged))


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
        _merge_source_entry_ids(last, new_entry)
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
                _merge_source_entry_ids(last, entry)
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


def _event_collapse_key(entry):
    """Semantic identity for consecutive event de-duping in prompt context."""
    if not isinstance(entry, dict):
        return None

    entry_type = entry.get('type') or ''
    if not entry_type:
        return None

    if entry_type == 'spell':
        return None

    speaker = entry.get('speaker') or ''
    voice_name = entry.get('voiceName') or ''
    text = entry.get('text') or ''
    line_id = entry.get('lineID') or ''

    if entry_type == 'location':
        companions = entry.get('companions') or []
        if not isinstance(companions, list):
            companions = [companions]
        location = entry.get('location') or text.replace('Entered ', '')
        return (
            entry_type,
            speaker,
            tuple(str(companion) for companion in companions),
            location,
        )

    return (entry_type, speaker, voice_name, line_id, text)


def collapse_consecutive_events(history):
    """
    Collapse prompt event rows for compact display.

    Spell rows keep count metadata. Other event rows are de-duped only when the
    same semantic event appears consecutively, so location ping-pong remains.
    """
    if not history:
        return []

    collapsed = collapse_consecutive_spells(history)
    deduped = []
    for entry in collapsed:
        key = _event_collapse_key(entry)
        if deduped and key and key == _event_collapse_key(deduped[-1]):
            if isinstance(entry, dict):
                merged = entry.copy()
                _merge_source_entry_ids(merged, deduped[-1])
                deduped[-1] = merged
            else:
                deduped[-1] = entry
            continue
        deduped.append(entry.copy() if isinstance(entry, dict) else entry)

    return deduped


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
    # Key: (voice_name, text), Value: kept entry metadata
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
        # Also preserve location, mount, and other system events
        is_system_event = entry_type in ("location", "broom", "mount", "spell", "commitment", "mail")

        if is_player_or_ai or is_system_event:
            # Always keep player, AI, and system event lines
            filtered.append(entry)
        else:
            # NPC ambient line - check for duplicates
            # Use voice_name for dedup since speaker is often "Unknown"
            key = (voice_name, text)

            if key in seen_npc_lines:
                kept = seen_npc_lines[key]
                # Sole speakers: dedupe globally (ignore time window)
                if voice_name in sole_speakers:
                    _merge_source_entry_ids(kept['entry'], entry)
                    continue

                # Normal speakers: dedupe within time window only
                kept_timestamp = kept['timestamp']
                if abs(kept_timestamp - timestamp) < (ambient_dedup_minutes * 60):
                    _merge_source_entry_ids(kept['entry'], entry)
                    continue

            # Keep this line (it's the latest we've seen so far)
            seen_npc_lines[key] = {
                'timestamp': timestamp,
                'entry': entry,
            }
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


def _clean_combat_enemy_name(name):
    """Normalize enemy names from old combat summaries."""
    name = (name or "").strip()
    name = re.sub(r"\s*\(\d+\)\s*$", "", name).strip()
    if not name or name.lower() in ("none", "nil", "null"):
        return "Unknown"
    return name


def _format_plain_list(items):
    cleaned = []
    seen = set()
    for item in items:
        item = (item or "").strip()
        key = item.lower()
        if item and key not in seen:
            cleaned.append(item)
            seen.add(key)

    if not cleaned:
        return ""
    if len(cleaned) == 1:
        return cleaned[0]
    if len(cleaned) == 2:
        return f"{cleaned[0]} and {cleaned[1]}"
    return f"{', '.join(cleaned[:-1])}, and {cleaned[-1]}"


def _parse_combat_damage_shares(text):
    """Parse old 'Damage: 123 (Name 80%, Other 20%)' text into shares."""
    match = re.search(r"Damage:\s*[\d.]+(?:\s*\(([^)]*)\))?", text or "")
    if not match or not match.group(1):
        return []

    shares = []
    for part in match.group(1).split(","):
        share_match = re.match(r"\s*(.*?)\s+(\d+(?:\.\d+)?)%\s*$", part)
        if share_match:
            shares.append((share_match.group(1).strip(), float(share_match.group(2))))
    return shares


def _combat_damage_phrase(entry, text):
    player_name = entry.get("speaker") or "Player"
    player_damage = entry.get("playerDamage")
    companion_damage = entry.get("companionDamage")
    shares = []

    if isinstance(player_damage, (int, float)) and isinstance(companion_damage, (int, float)):
        total_damage = player_damage + companion_damage
        if total_damage <= 0:
            return ""

        parsed_shares = _parse_combat_damage_shares(text)
        companion_name = parsed_shares[1][0] if len(parsed_shares) > 1 else "the companion"
        shares = [
            (player_name, float(player_damage) / total_damage),
            (companion_name, float(companion_damage) / total_damage),
        ]
    else:
        parsed_shares = _parse_combat_damage_shares(text)
        if not parsed_shares:
            return ""
        shares = [(name, pct / 100.0) for name, pct in parsed_shares]

    shares = [(name, share) for name, share in shares if share > 0]
    if not shares:
        return ""

    shares.sort(key=lambda item: item[1], reverse=True)
    leader_name, leader_share = shares[0]

    if len(shares) == 1 or leader_share >= 0.98:
        return f"{leader_name} did all of the damage."

    second_name = shares[1][0]
    if leader_share >= 0.85:
        return f"{leader_name} did the vast majority of the damage."
    if leader_share >= 0.65:
        return f"{leader_name} did most of the damage."
    if leader_share >= 0.62:
        return f"{leader_name} did slightly more damage than {second_name}."

    return f"{leader_name} and {second_name} did around equal damage."


def format_combat_summary(entry, include_damage=True):
    """Render combat as plain prompt context without raw damage numbers."""
    if not isinstance(entry, dict):
        return ""

    text = entry.get("text") or ""
    defeated_names = []
    defeated_match = re.search(r"Defeated:\s*(.*?)(?:\s*\|\s*Damage:|$)", text)
    if defeated_match:
        for raw_name in defeated_match.group(1).split(","):
            defeated_names.append(_clean_combat_enemy_name(raw_name))

    parts = []
    defeated = _format_plain_list(defeated_names)
    if defeated:
        parts.append(f"Defeated {defeated}.")

    if include_damage:
        damage_phrase = _combat_damage_phrase(entry, text)
        if damage_phrase:
            parts.append(damage_phrase)

    if not parts:
        if not include_damage:
            return "Combat encounter occurred."
        stripped = text.strip()
        stripped_lower = stripped.lower()
        if stripped and stripped_lower != "combat encounter" and not stripped_lower.startswith("damage:"):
            parts.append(stripped)
        else:
            parts.append("Damage was exchanged.")

    return " ".join(parts)


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

    # Handle mount/dismount entries
    if entry_type in ('broom', 'mount'):
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
        return f"{time_str}Combat: {format_combat_summary(entry)}"

    # Commitment events
    if entry_type == 'commitment':
        return f"{time_prefix}[{text}]"

    # Mail events
    if entry_type == 'mail':
        return f"{time_prefix}[{speaker} sent \"{text}\" to {target}]"

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

    Ignores: NPC-to-NPC conversations, ambient chatter, combat, mount, spell, location.

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
        if entry_type in ('location', 'broom', 'mount', 'spell', 'combat', 'prompt', 'commitment'):
            continue

        is_ai = entry.get('isAIResponse', False)
        is_player = entry.get('isPlayer', False)
        target_id = str(entry.get('targetId') or '').strip()

        # Cutscene dialogue the NPC witnessed - player was present
        if entry_type == 'cutscene':
            pass  # Accept
        # Player speaking and NPC heard it
        elif is_player:
            pass  # Accept
        # AI response directed at the player (not NPC-to-NPC)
        elif is_ai and target_id == 'Player':
            pass  # Accept
        elif is_ai and not target_id and player_name_lower and (entry.get('target') or '').lower() == player_name_lower:
            pass  # Legacy fallback for pre-targetId rows
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


def _clean_history_line_for_prompt(line: str) -> str:
    """Normalize old narration-formatted history only for prompt rendering."""
    if not line:
        return ""

    def _replace_italic(match):
        content = match.group(1).strip()
        word_count = len(re.findall(r"\S+", content))
        if word_count >= 3 or re.search(r'[.!?,]', content):
            return ""
        return match.group(0)

    def _replace_quotes(match):
        content = match.group(1)
        if re.search(r'[.!?,]', content):
            return content
        return match.group(0)

    # Order matters: strip narration-like italics first, then dialogue-style
    # double quotes, then collapse whitespace introduced by those removals.
    line = re.sub(r'\*([^*\n]+)\*', _replace_italic, line)
    line = re.sub(r'"([^"\n]*)"', _replace_quotes, line)
    line = re.sub(r'[ \t]{2,}', ' ', line)
    return line.strip()


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
    narration_enabled = settings.get('conversation', {}).get('narration_enabled', False)
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
        if line and not narration_enabled:
            line = _clean_history_line_for_prompt(line)
        if line:
            lines.append(line)

    if not lines:
        return ""

    return "## Recent History\n" + "\n".join(lines)


def format_dialogue_as_messages(history, for_npc_id=None, current_game_date=None,
                                 max_entries=None, bulk_drop_ratio=None):
    """Convert dialogue history into user/assistant message pairs for multi-message LLM calls.

    Role mapping:
      - isPlayer=True -> user
      - isAIResponse=True -> assistant
      - type='cutscene' with isPlayer -> user, otherwise -> assistant
      - Everything else (location, spell, ambient, etc.) -> excluded, returned as events

    Consecutive same-role entries are merged with '\\n\\n' separators.
    Day dividers are prepended to the next message's content.

    Args:
        history: List of dialogue history entries
        for_npc_id: If provided, filter to entries this NPC witnessed
        current_game_date: Current game date string for relative day display
        max_entries: Override max entries (default from settings)
        bulk_drop_ratio: Override bulk drop ratio (default from settings)

    Returns:
        Tuple of (messages, event_entries) where:
          messages: list of {"role": "user"|"assistant", "content": str}
          event_entries: list of raw entry dicts excluded from messages (for recent events block)
    """
    if not history:
        return [], []

    settings = load_settings()
    narration_enabled = settings.get('conversation', {}).get('narration_enabled', False)
    history_settings = settings.get('history', {})

    if max_entries is None:
        max_entries = history_settings.get('max_entries', DIALOGUE_HISTORY_LIMIT)
    if bulk_drop_ratio is None:
        bulk_drop_ratio = history_settings.get('bulk_drop_ratio', 0.25)

    # Filter duplicates
    filtered = filter_dialogue_history(history)

    # Filter by earshot if realistic memory enabled
    realistic_memory = history_settings.get('realistic_memory', True)
    if for_npc_id and realistic_memory:
        filtered = [entry for entry in filtered if _npc_witnessed(entry, for_npc_id)]

    # Separate dialogue entries from event entries BEFORE trimming so events
    # don't eat into the dialogue budget and cause unpredictable cache-breaking drops.
    EVENT_TYPES = {'location', 'broom', 'mount', 'spell', 'combat', 'commitment', 'mail', 'prompt'}
    dialogue_entries = []
    event_entries = []

    for entry in filtered:
        entry_type = entry.get('type') or ''
        is_player = entry.get('isPlayer', False)
        is_ai = entry.get('isAIResponse', False)
        is_cutscene = entry_type == 'cutscene'

        if entry_type in EVENT_TYPES:
            event_entries.append(entry)
        elif is_player or is_ai or is_cutscene:
            dialogue_entries.append(entry)
        else:
            # Ambient NPC chatter — treat as event
            event_entries.append(entry)

    # Bulk-drop trimming on dialogue entries only (events go to dynamic context untrimmed).
    # Quantize the drop to multiples of drop_size so the start position stays fixed for
    # drop_size consecutive turns — giving stable prefix cache hits between jumps.
    drop_size = int(max_entries * bulk_drop_ratio) if bulk_drop_ratio else 0
    total = len(dialogue_entries)
    if total > max_entries and drop_size > 0:
        overshoot = total - max_entries
        drops_needed = ((overshoot + drop_size - 1) // drop_size) * drop_size
        dialogue_entries = dialogue_entries[drops_needed:]

    if not dialogue_entries:
        return [], event_entries

    # Map entries to roles
    def _entry_role(entry):
        if entry.get('isPlayer', False):
            return 'user'
        # Cutscene non-player or AI response -> assistant
        return 'assistant'

    # Build message list with merging
    messages = []
    prev_date_tuple = None

    for entry in dialogue_entries:
        role = _entry_role(entry)
        game_date = entry.get('gameDate') or ''
        date_tuple = _parse_game_date(game_date) if game_date else None

        # Build day divider if date changed
        divider = ""
        if date_tuple and prev_date_tuple and date_tuple != prev_date_tuple:
            display_date = f"{date_tuple[0]}/{date_tuple[1]:02d}/{date_tuple[2]:02d}"
            relative = _format_relative_date(game_date, current_game_date)
            if relative:
                divider = f"--- {display_date} ({relative}) ---"
            else:
                divider = f"--- {display_date} ---"
        if date_tuple:
            prev_date_tuple = date_tuple

        # Format the entry text
        line = format_dialogue_entry(entry, include_time=True, mark_player=False)
        if line and not narration_enabled:
            line = _clean_history_line_for_prompt(line)
        if not line:
            continue

        # Prepend day divider if present
        if divider:
            line = f"{divider}\n{line}"

        # Merge with previous message if same role, otherwise start new message
        if messages and messages[-1]['role'] == role:
            messages[-1]['content'] += f"\n\n{line}"
        else:
            messages.append({'role': role, 'content': line})

    return messages, event_entries
