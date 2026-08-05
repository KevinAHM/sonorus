"""
Commitment system business logic for Sonorus.
Handles validation, processing, prompt building, time-based activation, and event injection.
All commitment logic lives here — server.py and lua_socket.py just call into this module.
"""

import difflib
import json
import os
import re
import time as _time

import llm
from constants import (
    COMMITMENT_TRAVEL_TIME_MIN,
    COMMITMENT_WAIT_TIME_MIN,
    COMMITMENT_MAX_CONTEXT_HISTORY,
    LOCATION_ACTIVITIES,
    is_excluded_npc,
)
from . import commitments_db
from .dialogue import prettify_voice_name
from .dialogue_db import append_entry
from .llm_utils import parse_commitment_actions
from .localization import load_localization
from .settings import SONORUS_DIR, load_commitment_spots, load_settings


# ============================================
# Helpers
# ============================================

# Spot labels for pending commitments (commitment_id -> label)
# Ephemeral: lost on server restart, which is fine (falls back to random spot)
_commitment_spot_labels = {}


def _display_with_label(display, spot_label):
    """Return 'Display (label)' if spot_label is non-empty, else just 'Display'."""
    if spot_label:
        return f"{display} ({spot_label})"
    return display

# Build display name -> (location_id, entry) lookup and location_id -> entry lookup
_DISPLAY_NAME_MAP = {}
_LOCATION_BY_ID = {}
for _loc_id, _entry in LOCATION_ACTIVITIES.items():
    _DISPLAY_NAME_MAP[_entry["display"].lower()] = (_loc_id, _entry)
    _LOCATION_BY_ID[_loc_id] = _entry


def _invalidate_registry_display_cache():
    """Clear cached display map. Called when language changes."""
    if hasattr(_get_registry_display_map, '_cache'):
        del _get_registry_display_map._cache


def _get_registry_display_map():
    """Build mod_key -> display_name map from registry + localization.
    Cached after first call. Invalidated by _invalidate_registry_display_cache()."""
    if hasattr(_get_registry_display_map, '_cache'):
        return _get_registry_display_map._cache
    try:
        reg_path = os.path.join(SONORUS_DIR, "data", "location_registry.json")
        with open(reg_path, 'r', encoding='utf-8') as f:
            registry = json.load(f)
        loc = load_localization()
        result = {}
        for mod_key, entry in registry.items():
            loc_id = entry.get("localized_id")
            if loc_id and loc_id in loc:
                result[mod_key] = loc[loc_id]
        _get_registry_display_map._cache = result
        return result
    except Exception as e:
        print(f"[Commitments] Warning: could not load registry display map: {e}")
        return {}

# Expand with teleport-only locations from commitment_spots.json
# These locations have authored spots but no scheduler activity
def _expand_with_teleport_locations():
    try:
        spots = load_commitment_spots()
        # Get display names from registry + localization
        display_map = _get_registry_display_map()
        for loc_id in spots:
            if loc_id in LOCATION_ACTIVITIES:
                continue  # Already has a scheduler activity
            display = display_map.get(loc_id, loc_id)
            entry = {"activity": "", "display": display, "type": "Teleport"}
            _DISPLAY_NAME_MAP[display.lower()] = (loc_id, entry)
            _LOCATION_BY_ID[loc_id] = entry
    except Exception as e:
        print(f"[Commitments] Warning: could not load teleport locations: {e}")

_expand_with_teleport_locations()

_DISPLAY_NAMES = list(_DISPLAY_NAME_MAP.keys())


def reload_teleport_locations():
    """Reload teleport-only locations after spots file changes."""
    global _DISPLAY_NAMES
    _expand_with_teleport_locations()
    _DISPLAY_NAMES = list(_DISPLAY_NAME_MAP.keys())


def _fuzzy_match_location(text):
    """Match a location string to a LOCATION_ACTIVITIES entry via fuzzy matching.
    Returns (location_id, entry) or (None, None).
    """
    if not text:
        return None, None

    text_lower = text.strip().lower()

    # Exact match first
    if text_lower in _DISPLAY_NAME_MAP:
        return _DISPLAY_NAME_MAP[text_lower]

    # Fuzzy match – cutoff 0.8 to avoid wild mismatches (e.g. "Library" → "Owlery")
    matches = difflib.get_close_matches(text_lower, _DISPLAY_NAMES, n=1, cutoff=0.8)
    if matches:
        print(f"[Commitments] Fuzzy location match: '{text}' → '{matches[0]}'")
        return _DISPLAY_NAME_MAP[matches[0]]

    return None, None


def _build_location_list():
    """Build a flat numbered list of available locations with spot labels.
    Returns (list_text, index_map) where index_map maps number -> (location_id, label_or_none).
    Number 0 = no match."""
    spots = load_commitment_spots()
    display_map = _get_registry_display_map()

    # Also include LOCATION_ACTIVITIES locations (schedule-based)
    all_location_ids = set(spots.keys()) | set(LOCATION_ACTIVITIES.keys())

    # Group by area for readability
    hogsmeade = []
    hogwarts = []
    other = []

    for loc_id in sorted(all_location_ids):
        display = None
        if loc_id in LOCATION_ACTIVITIES:
            display = LOCATION_ACTIVITIES[loc_id]["display"]
        elif loc_id in display_map:
            display = display_map[loc_id]
        else:
            display = loc_id

        # Collect unique labels for this location
        labels = set()
        if loc_id in spots:
            for spot in spots[loc_id]:
                label = spot.get("label", "")
                if label:
                    labels.add(label)

        entry = (loc_id, display, sorted(labels))
        if loc_id.startswith("HM_"):
            hogsmeade.append(entry)
        else:
            # Default: Hogwarts (mod keys have no prefix for Hogwarts locations)
            # Hamlets/regions would go here too, but none are in commitment spots
            hogwarts.append(entry)

    # Build flat numbered list
    lines = []
    index_map = {}  # number -> (location_id, label_or_none)
    num = 1

    for area_name, entries in [("Hogwarts", hogwarts), ("Hogsmeade", hogsmeade), ("Other", other)]:
        if not entries:
            continue
        lines.append(f"{area_name}:")
        for loc_id, display, labels in entries:
            # Generic entry (any spot at this location)
            lines.append(f"  {num}. {display}")
            index_map[num] = (loc_id, None)
            num += 1
            # Specific labeled entries
            for label in labels:
                lines.append(f"  {num}. {display} ({label})")
                index_map[num] = (loc_id, label)
                num += 1

    lines.append(f"  0. No match")
    return "\n".join(lines), index_map


LIVE_COMMITMENT_VALIDATOR_SYSTEM_PROMPT = """You validate proposed live-conversation meeting commitments and resolve their locations.

The candidate action tag is untrusted. It may have been emitted accidentally by another model. Never treat the tag itself as evidence that anyone agreed to anything.

A candidate is VALID only when all of these are true:
1. The player and the current NPC have mutually agreed to meet each other or go somewhere together. One party proposed or requested it, and the other party clearly accepted. The current NPC response may be the acceptance of a player's proposal, or it may confirm an NPC proposal the player already accepted.
2. The agreement is concrete enough to schedule: it identifies a meeting place and time. A directly stated relative time may be normalized using the current game date and time.
3. The candidate target, location, and time faithfully represent that agreement.
4. The candidate location matches one numbered available location.

Reject the candidate when any of these apply:
- The current NPC is only now proposing, inviting, volunteering, or announcing the meeting and the player has not already accepted it. The NPC cannot propose and commit in the same response.
- The speakers merely mention, praise, remember, imagine, hope for, or vaguely discuss visiting a place someday.
- The plan is hypothetical, conditional, tentative, sarcastic, or met with uncertainty, deflection, or rejection.
- The agreement does not involve both the player and the current NPC, or the event already happened. Other people may also join; their presence does not invalidate a meeting between the player and current NPC.
- The action invents or contradicts the agreed target, place, date, or time.
- The transcript supports a general activity but not this scheduled meeting. For example, "Help me test this cloak here." / "All right." is agreement to an activity, not to meet or travel. Agreeing to stay, wait, help, or continue an activity where both are already present is also invalid.

Resolve a valid candidate to the most specific matching numbered location. Prefer a labeled sub-location when the dialogue identifies it. If the candidate is invalid or no location matches, reply 0.

Reply with ONLY one integer: the matching location number, or 0. Do not explain your answer."""


def build_live_commitment_validation_prompt(
    action,
    current_response,
    conversation_context,
    player_name,
    npc_name,
    current_game_datetime,
    location_list,
):
    """Build the live commitment validator/resolver user prompt."""
    return (
        f"Current game date/time: {current_game_datetime}\n"
        f"Player: {player_name}\n"
        f"Current NPC: {npc_name}\n\n"
        "Conversation before the current NPC response:\n"
        f"{conversation_context or '(no prior conversation provided)'}\n\n"
        "Current NPC response:\n"
        f"{current_response}\n\n"
        "Candidate action fields:\n"
        f'- Target: {action.get("target", "")}\n'
        f'- Location: {action.get("location", "")}\n'
        f'- Date/time: {action.get("datetime", "")}\n\n'
        "Available locations:\n"
        f"{location_list}"
    )


def resolve_live_commitment_location(
    action,
    current_response,
    conversation_context,
    player_name,
    npc_name,
    current_game_datetime,
):
    """Validate a live-chat meeting agreement and resolve its location in one LLM call."""
    location_list, index_map = _build_location_list()
    if not index_map:
        print("[CommitmentValidator] No locations available")
        return None, None, None

    settings = load_settings()
    model = settings.get('commitment', {}).get(
        'location_resolver_model',
        'google/gemini-3.1-flash-lite',
    )
    prompt = build_live_commitment_validation_prompt(
        action=action,
        current_response=current_response,
        conversation_context=conversation_context,
        player_name=player_name,
        npc_name=npc_name,
        current_game_datetime=current_game_datetime,
        location_list=location_list,
    )

    try:
        result = llm.chat(
            [
                {"role": "system", "content": LIVE_COMMITMENT_VALIDATOR_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            model=model,
            temperature=0,
            max_tokens=8,
            context="location_resolver",
        )
        if not result:
            print("[CommitmentValidator] No response from LLM; rejecting action")
            return None, None, None

        match = re.fullmatch(r'\s*(\d+)\s*', result)
        if not match:
            print(f"[CommitmentValidator] Invalid response {result!r}; rejecting action")
            return None, None, None

        number = int(match.group(1))
        if number == 0:
            print(
                f"[CommitmentValidator] Rejected action for {npc_name}: "
                f"{action.get('location', '')!r}"
            )
            return None, None, None
        if number not in index_map:
            print(f"[CommitmentValidator] Location number {number} is out of range; rejecting action")
            return None, None, None

        location_id, label = index_map[number]
        location_entry = _LOCATION_BY_ID.get(location_id)
        display = location_entry["display"] if location_entry else location_id
        label_text = f" ({label})" if label else ""
        print(
            f"[CommitmentValidator] Accepted action for {npc_name}: "
            f"#{number} {display}{label_text} (id={location_id})"
        )
        return location_id, label, display
    except Exception as exc:
        print(f"[CommitmentValidator] Error; rejecting action: {exc}")
        return None, None, None


def resolve_commitment_location(raw_location, conversation_context="", source="conversation"):
    """Use an LLM to resolve a vague location mention to a specific spot.

    Args:
        raw_location: The location string from the commitment action (e.g. "upper North Hall")
        conversation_context: Recent dialogue or letter exchange for context
        source: "conversation" or "owl_mail"

    Returns:
        (location_id, label_or_none, display_name) or (None, None, None) on failure
    """
    location_list, index_map = _build_location_list()
    if not index_map:
        print("[LocationResolver] No locations available")
        return None, None, None

    settings = load_settings()
    model = settings.get('commitment', {}).get('location_resolver_model',
                'mistralai/mistral-small-3.2-24b-instruct:nitro')

    system = (
        "You resolve location mentions to numbered entries from a list. "
        "Reply with ONLY a number. Nothing else."
    )

    prompt = (
        f"The speaker mentioned: \"{raw_location}\"\n\n"
    )
    if conversation_context:
        prompt += f"Context:\n{conversation_context}\n\n"
    prompt += (
        f"Which location best matches? Reply with the number only.\n"
        f"If the mention is specific (e.g. 'upper part of the North Hall'), prefer the specific labeled entry.\n"
        f"If no location matches, reply 0.\n\n"
        f"{location_list}"
    )

    try:
        result = llm.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            model=model, temperature=0.1, max_tokens=8,
            context="location_resolver",
        )
        if not result:
            print("[LocationResolver] No response from LLM")
            return None, None, None

        # Parse number from response
        match = re.search(r'\d+', result.strip())
        if not match:
            print(f"[LocationResolver] Could not parse number from: {result}")
            return None, None, None

        num = int(match.group())
        if num == 0:
            print(f"[LocationResolver] LLM returned 0 (no match) for '{raw_location}'")
            return None, None, None

        if num not in index_map:
            print(f"[LocationResolver] Number {num} out of range for '{raw_location}'")
            return None, None, None

        loc_id, label = index_map[num]

        # Get display name and activity_id
        loc_entry = _LOCATION_BY_ID.get(loc_id)
        display = loc_entry["display"] if loc_entry else loc_id
        activity_id = loc_entry.get("activity", "") if loc_entry else ""

        label_str = f" ({label})" if label else ""
        print(f"[LocationResolver] '{raw_location}' -> #{num} {display}{label_str} (id={loc_id})")

        return loc_id, label, display

    except Exception as e:
        print(f"[LocationResolver] Error: {e}")
        return None, None, None


def _parse_commitment_datetime(text):
    """Parse datetime from LLM output to 'YYYY/MM/DD HH:MM' format.
    Handles: 'M/D/YYYY H:MM AM/PM', 'M/D/YYYY HH:MM', 'YYYY/MM/DD HH:MM'
    Returns string or None.
    """
    if not text:
        return None

    text = text.strip()

    # Try M/D/YYYY H:MM AM/PM
    m = re.match(r'(\d{1,2})/(\d{1,2})/(\d{4})\s+(\d{1,2}):(\d{2})\s*(AM|PM)', text, re.IGNORECASE)
    if m:
        month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hour, minute = int(m.group(4)), int(m.group(5))
        period = m.group(6).upper()
        if period == 'PM' and hour != 12:
            hour += 12
        elif period == 'AM' and hour == 12:
            hour = 0
        return f"{year}/{month:02d}/{day:02d} {hour:02d}:{minute:02d}"

    # Try M/D/YYYY HH:MM (24h)
    m = re.match(r'(\d{1,2})/(\d{1,2})/(\d{4})\s+(\d{1,2}):(\d{2})', text)
    if m:
        month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hour, minute = int(m.group(4)), int(m.group(5))
        return f"{year}/{month:02d}/{day:02d} {hour:02d}:{minute:02d}"

    # Try YYYY/MM/DD HH:MM (already correct format)
    m = re.match(r'(\d{4})/(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})', text)
    if m:
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hour, minute = int(m.group(4)), int(m.group(5))
        return f"{year}/{month:02d}/{day:02d} {hour:02d}:{minute:02d}"

    return None


def _add_game_minutes(time_str, minutes):
    """Add minutes to a 'YYYY/MM/DD HH:MM' time string."""
    m = re.match(r'(\d{4})/(\d{2})/(\d{2})\s+(\d{2}):(\d{2})', time_str)
    if not m:
        return time_str
    year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    hour, minute = int(m.group(4)), int(m.group(5))

    total_min = hour * 60 + minute + minutes
    hour = (total_min // 60) % 24
    minute = total_min % 60
    # Handle day overflow (simplified: 30-day months)
    extra_days = total_min // (24 * 60)
    if extra_days > 0:
        day += extra_days
        while day > 30:
            day -= 30
            month += 1
            if month > 12:
                month = 1
                year += 1
    return f"{year}/{month:02d}/{day:02d} {hour:02d}:{minute:02d}"


def _subtract_game_minutes(time_str, minutes):
    """Subtract minutes from a 'YYYY/MM/DD HH:MM' time string."""
    m = re.match(r'(\d{4})/(\d{2})/(\d{2})\s+(\d{2}):(\d{2})', time_str)
    if not m:
        return time_str
    year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    hour, minute = int(m.group(4)), int(m.group(5))

    total_min = hour * 60 + minute - minutes
    while total_min < 0:
        total_min += 24 * 60
        day -= 1
        if day < 1:
            month -= 1
            if month < 1:
                month = 12
                year -= 1
            day = 30
    hour = total_min // 60
    minute = total_min % 60
    return f"{year}/{month:02d}/{day:02d} {hour:02d}:{minute:02d}"


def _get_current_game_time_str(game_context):
    """Build 'YYYY/MM/DD HH:MM' from game_context fields.

    Tries explicit year/month/day fields first, then falls back to
    parsing dateFormatted (e.g. 'Wednesday, January 14th, 1891') since
    partial context refreshes may not include year/month/day.
    """
    y = game_context.get('year')
    m = game_context.get('month')
    d = game_context.get('day')
    h = game_context.get('hour', 12)
    mi = game_context.get('minute', 0)
    if y and m and d:
        return f"{y}/{int(m):02d}/{int(d):02d} {int(h):02d}:{int(mi):02d}"

    # Fallback: parse dateFormatted ("Wednesday, January 14th, 1891")
    date_str = game_context.get('dateFormatted', '')
    if date_str:
        # Try short format YYYY/MM/DD first
        date_match = re.match(r'(\d{4})/(\d{1,2})/(\d{1,2})', date_str)
        if date_match:
            y, m, d = int(date_match.group(1)), int(date_match.group(2)), int(date_match.group(3))
            return f"{y}/{int(m):02d}/{int(d):02d} {int(h):02d}:{int(mi):02d}"
        # Try long format "Day, Month DDth, YYYY"
        _MONTHS = {'january': 1, 'february': 2, 'march': 3, 'april': 4, 'may': 5, 'june': 6,
                    'july': 7, 'august': 8, 'september': 9, 'october': 10, 'november': 11, 'december': 12}
        date_match = re.search(r'(\w+)\s+(\d{1,2})\w*,?\s+(\d{4})', date_str)
        if date_match:
            month_name = date_match.group(1).lower()
            m = _MONTHS.get(month_name)
            if m:
                d = int(date_match.group(2))
                y = int(date_match.group(3))
                return f"{y}/{int(m):02d}/{int(d):02d} {int(h):02d}:{int(mi):02d}"

    return None


def _format_game_time_12h(hour, minute):
    """Format a 24-hour (hour, minute) as '7:45 AM'."""
    period = 'AM' if hour < 12 else 'PM'
    h12 = hour % 12
    if h12 == 0:
        h12 = 12
    return f"{h12}:{minute:02d} {period}"


def _split_game_datetime_for_history(time_str):
    """Split 'YYYY/MM/DD HH:MM' into (game_date, game_time_12h)."""
    if not time_str:
        return "", ""

    m = re.match(r'(\d{4})/(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})', str(time_str).strip())
    if not m:
        if " " in str(time_str):
            date_part, time_part = str(time_str).split(" ", 1)
            return date_part, time_part
        return "", ""

    year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    hour, minute = int(m.group(4)), int(m.group(5))
    return f"{year}/{month:02d}/{day:02d}", _format_game_time_12h(hour, minute)


def _format_time_display(time_str):
    """Convert 'YYYY/MM/DD HH:MM' to a display-friendly format like '9:30 PM on 2/4/1886'."""
    m = re.match(r'(\d{4})/(\d{2})/(\d{2})\s+(\d{2}):(\d{2})', time_str)
    if not m:
        return time_str
    year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    hour, minute = int(m.group(4)), int(m.group(5))
    return f"{_format_game_time_12h(hour, minute)} on {month}/{day}/{year}"


# ============================================
# Validation
# ============================================

def validate_meet_action(
    action,
    npc_id,
    game_context,
    *,
    require_mutual_agreement=False,
    current_response="",
    conversation_context="",
    player_name=None,
):
    """Validate a meet commitment action.

    Returns (success: bool, error_msg: str|None, parsed_data: dict|None)
    parsed_data includes: location_id, location_display, activity_id, game_time_start,
                         game_time_end, override_apply_time
    """
    raw_location = action.get("location", "")
    if require_mutual_agreement:
        player_name = player_name or game_context.get("playerName", "Player")
        target = str(action.get("target", "") or "").strip().lower()
        if target not in ("player", player_name.lower()):
            return False, f"Commitment target '{action.get('target', '')}' is not the player", None

        date_display = game_context.get("dateFormatted", "")
        time_display = game_context.get("timeFormatted", "") or game_context.get("time", "")
        if not date_display:
            year, month, day = (
                game_context.get("year"),
                game_context.get("month"),
                game_context.get("day"),
            )
            if year and month and day:
                date_display = f"{int(month)}/{int(day)}/{int(year)}"
        current_game_datetime = " ".join(
            part for part in (date_display, time_display) if part
        ) or "unknown"

        loc_id, spot_label, display = resolve_live_commitment_location(
            action=action,
            current_response=current_response,
            conversation_context=conversation_context,
            player_name=player_name,
            npc_name=prettify_voice_name(npc_id),
            current_game_datetime=current_game_datetime,
        )
        if not loc_id:
            return False, "Commitment was not grounded in a mutual live-chat agreement", None
        loc_entry = _LOCATION_BY_ID.get(
            loc_id,
            {"activity": "", "display": display, "type": "Teleport"},
        )
        location_id = loc_id
    else:
        # Owl-mail proposals still require explicit player acceptance in the UI,
        # so they retain location-only resolution and its fuzzy fallback.
        loc_id, spot_label, display = resolve_commitment_location(raw_location)
        if loc_id:
            loc_entry = _LOCATION_BY_ID.get(
                loc_id,
                {"activity": "", "display": display, "type": "Teleport"},
            )
            location_id = loc_id
        else:
            location_id, loc_entry = _fuzzy_match_location(raw_location)
            spot_label = None
    if not location_id:
        return False, f"Location '{raw_location}' not found in available locations", None

    # Parse datetime
    game_time_start = _parse_commitment_datetime(action.get("datetime"))
    if not game_time_start:
        return False, f"Could not parse date/time '{action.get('datetime')}'", None

    # Compute end and apply times
    game_time_end = _add_game_minutes(game_time_start, COMMITMENT_WAIT_TIME_MIN)
    override_apply_time = _subtract_game_minutes(game_time_start, COMMITMENT_TRAVEL_TIME_MIN)

    # Check conflicts
    conflicts = commitments_db.get_commitments_in_window(npc_id, override_apply_time, game_time_end)
    if conflicts:
        conflict_locs = ", ".join(c["location_display"] for c in conflicts)
        return False, f"Conflicts with existing commitment(s) at {conflict_locs}", None

    return True, None, {
        "location_id": location_id,
        "location_display": _display_with_label(loc_entry["display"], spot_label),
        "activity_id": loc_entry["activity"],
        "game_time_start": game_time_start,
        "game_time_end": game_time_end,
        "override_apply_time": override_apply_time,
        "spot_label": spot_label,
    }


# ============================================
# Processing (called from server.py)
# ============================================

def process_commitment_actions(
    raw_response,
    speaker_id,
    player_name,
    game_context,
    lua_socket,
    conversation_context="",
):
    """Parse, validate, and store commitment actions from an LLM response.

    Returns list of created/cancelled commitment IDs.
    """
    actions = parse_commitment_actions(raw_response)
    if not actions:
        return []

    results = []

    for action in actions:
        try:
            if action["type"] == "meet":
                _process_meet_action(
                    action,
                    raw_response,
                    conversation_context,
                    speaker_id,
                    player_name,
                    game_context,
                    lua_socket,
                    results,
                )
            elif action["type"] == "cancel":
                _process_cancel_action(action, speaker_id, player_name, game_context, lua_socket, results)
        except Exception as e:
            print(f"[Commitments] Error processing {action['type']} action: {e}")

    return results


def _process_meet_action(
    action,
    raw_response,
    conversation_context,
    speaker_id,
    player_name,
    game_context,
    lua_socket,
    results,
):
    """Process a single meet action."""
    # Resolve target
    target = action.get("target", "")
    if target.lower() == player_name.lower() or target.lower() in ("player", player_name.lower()):
        target_id = "player"
    else:
        target_id = target  # Future: resolve NPC name to voice ID

    # Validate
    valid, error_msg, parsed = validate_meet_action(
        action,
        speaker_id,
        game_context,
        require_mutual_agreement=True,
        current_response=raw_response,
        conversation_context=conversation_context,
        player_name=player_name,
    )
    if not valid:
        print(f"[Commitments] Validation failed for {speaker_id}: {error_msg}")
        return

    # Check if NPC is current companion
    companion_id = game_context.get('companionId')
    is_companion = bool(companion_id and companion_id.lower() == speaker_id.lower())

    # Create commitment
    commitment_id = commitments_db.create_commitment(
        npc_id=speaker_id,
        target_id=target_id,
        location_id=parsed["location_id"],
        location_display=parsed["location_display"],
        activity_id=parsed["activity_id"],
        game_time_start=parsed["game_time_start"],
        game_time_end=parsed["game_time_end"],
        override_apply_time=parsed["override_apply_time"],
        is_companion=is_companion,
    )

    if commitment_id and parsed.get("spot_label"):
        _commitment_spot_labels[commitment_id] = parsed["spot_label"]

    if commitment_id:
        results.append(commitment_id)

        # Notify player
        npc_display = prettify_voice_name(speaker_id)
        time_display = _format_time_display(parsed["game_time_start"])
        _notify(lua_socket, commitment_id, "created",
                f"Meeting {npc_display} at {parsed['location_display']} - {time_display}")

        # If meeting is within 15 min, pre-mark warning/due as notified
        # (player just arranged it, they don't need reminders for something imminent)
        current_time_str = _get_current_game_time_str(game_context)
        if current_time_str:
            fifteen_from_now = _add_game_minutes(current_time_str, COMMITMENT_TRAVEL_TIME_MIN)
            if parsed["game_time_start"] <= fifteen_from_now:
                _notified.setdefault(commitment_id, set()).update({"warning", "due"})

        print(f"[Commitments] Created: {commitment_id} - {speaker_id} meets {target_id} at {parsed['location_display']} ({parsed['game_time_start']})")


def _process_cancel_action(action, speaker_id, player_name, game_context, lua_socket, results):
    """Process a single cancel action."""
    commitment_id = action.get("commitment_id", "")
    if not commitment_id:
        return

    commitment = commitments_db.get_commitment(commitment_id)
    if not commitment:
        print(f"[Commitments] Cancel: commitment {commitment_id} not found")
        return

    # Only allow cancellation of own commitments
    if commitment["npc_id"] != speaker_id:
        print(f"[Commitments] Cancel: {speaker_id} cannot cancel {commitment['npc_id']}'s commitment")
        return

    was_active = commitment["status"] in ("active", "arrived")

    commitments_db.cancel_commitment(commitment_id)
    results.append(commitment_id)

    # Release Lua override if it was active
    if was_active and lua_socket:
        lua_socket.send_deactivate_commitment(commitment["npc_id"], commitment["activity_id"])

    # Notify player
    npc_display = prettify_voice_name(commitment["npc_id"])
    _notify(lua_socket, commitment_id, "cancelled",
            f"Meeting with {npc_display} at {commitment['location_display']} cancelled")

    # Inject cancellation event
    current_time = _get_current_game_time_str(game_context) if game_context else None
    _inject_commitment_event(commitment, "npc_cancelled", player_name, current_time)
    print(f"[Commitments] Cancelled: {commitment_id}")


# ============================================
# Notifications
# ============================================

# In-memory tracking — resets on server restart (OK: serves as reminder on restart)
_notified = {}  # {commitment_id: set of notification types already shown}


def _notify(lua_socket, commitment_id, notif_type, text):
    """Send a notification if not already shown for this commitment+type."""
    if notif_type in _notified.get(commitment_id, set()):
        return
    if commitment_id not in _notified:
        _notified[commitment_id] = set()
    _notified[commitment_id].add(notif_type)
    if lua_socket:
        try:
            lua_socket.send_notification(text)
            print(f"[Commitments] Notification ({notif_type}): {text}")
        except Exception as e:
            print(f"[Commitments] Notification error: {e}")


# ============================================
# Time-Based Processing
# ============================================

# Throttle timer checks
_last_timer_check = 0
_TIMER_CHECK_INTERVAL = 5.0  # seconds
_last_companion_id = None  # Track companion transitions


def check_commitment_timers(game_context, lua_socket):
    """Check pending→active and active→no_show transitions.
    Also detects companion dismissal to re-apply overrides.
    Called from lua_socket.py on each game_context update (throttled to 5s).
    """
    global _last_timer_check, _last_companion_id
    now = _time.time()
    if now - _last_timer_check < _TIMER_CHECK_INTERVAL:
        return
    _last_timer_check = now

    # Check if commitments are enabled (after throttle to avoid disk reads on every call)
    if not load_settings().get('commitments', {}).get('enabled', False):
        return

    current_time_str = _get_current_game_time_str(game_context)
    if not current_time_str:
        return

    # Detect companion dismissal — re-apply active commitments for the former companion
    current_companion = game_context.get('companionId')
    if _last_companion_id and _last_companion_id != current_companion:
        # Companion was dismissed — check for active commitments
        try:
            active = commitments_db.get_active_commitments()
            for c in active:
                if c["npc_id"] == _last_companion_id:
                    print(f"[Commitments] Companion dismissed ({_last_companion_id}) — re-applying override to {c['location_display']}")
                    if lua_socket:
                        lua_socket.send_activate_commitment(c["npc_id"], c["activity_id"], c["location_id"])
        except Exception as e:
            print(f"[Commitments] Error re-applying after companion dismiss: {e}")
    _last_companion_id = current_companion

    # Check pending commitments for activation
    try:
        pending = commitments_db.get_pending_commitments()
        for c in pending:
            if current_time_str >= c["override_apply_time"]:
                # Warning notification — only if we haven't passed start time yet (anti-spam on fast travel)
                if current_time_str < c["game_time_start"]:
                    npc_display = prettify_voice_name(c["npc_id"])
                    _notify(lua_socket, c["id"], "warning",
                            f"Meeting {npc_display} at {c['location_display']} in 15 minutes")

                print(f"[Commitments] Activating: {c['id']} ({c['npc_id']} -> {c['location_display']})")
                commitments_db.update_status(c["id"], "active")
                if lua_socket:
                    spot_label = _commitment_spot_labels.pop(c["id"], None)
                    lua_socket.send_activate_commitment(c["npc_id"], c["activity_id"], c["location_id"], spot_label)
    except Exception as e:
        print(f"[Commitments] Error checking pending: {e}")

    # Check active/arrived commitments for due notifications and expiry
    try:
        active = commitments_db.get_active_commitments()
        for c in active:
            # Due notification — meeting time has arrived, only if not already expired (anti-spam)
            if current_time_str >= c["game_time_start"] and current_time_str < c["game_time_end"]:
                npc_display = prettify_voice_name(c["npc_id"])
                _notify(lua_socket, c["id"], "due",
                        f"{npc_display} is waiting at {c['location_display']}")

            if current_time_str >= c["game_time_end"]:
                if c["status"] == "arrived":
                    # Player showed up and time window ended → completed
                    commitments_db.update_status(c["id"], "completed")
                    print(f"[Commitments] Completed: {c['id']} ({c['npc_id']})")
                else:
                    # Active but player never showed → no_show
                    commitments_db.update_status(c["id"], "no_show")
                    npc_display = prettify_voice_name(c["npc_id"])
                    _notify(lua_socket, c["id"], "no_show",
                            f"Missed meeting with {npc_display} at {c['location_display']}")
                    _inject_commitment_event(c, "no_show", game_context.get('playerName'), current_time_str)
                    print(f"[Commitments] No-show: {c['id']} ({c['npc_id']})")

                # Release Lua override
                if lua_socket:
                    lua_socket.send_deactivate_commitment(c["npc_id"], c["activity_id"])
    except Exception as e:
        print(f"[Commitments] Error checking active: {e}")


# ============================================
# Player Detection
# ============================================

def has_active_commitment(npc_id, game_context):
    """Check if this NPC has an active commitment and the meeting time has started.
    Only returns a commitment if current game time is within the meeting window
    (game_time_start to game_time_end), meaning the NPC should have arrived.
    Returns the commitment dict if found, None otherwise.
    """
    try:
        current_time_str = _get_current_game_time_str(game_context) if game_context else None
        if not current_time_str:
            return None
        active = commitments_db.get_active_commitments()
        for c in active:
            if c["npc_id"] == npc_id and c["status"] == "active":
                if c["game_time_start"] <= current_time_str <= c["game_time_end"]:
                    return c
    except Exception:
        pass
    return None


def check_player_arrival(speaker_id, game_context):
    """Check if talking to this NPC fulfills an active commitment.
    Called from server.py when player talks to an NPC.
    """
    try:
        active = commitments_db.get_active_commitments()
        current_time_str = _get_current_game_time_str(game_context)
        if not current_time_str:
            return

        for c in active:
            if c["npc_id"] == speaker_id and c["status"] == "active":
                if c["game_time_start"] <= current_time_str <= c["game_time_end"]:
                    commitments_db.update_status(c["id"], "arrived")
                    print(f"[Commitments] Player arrived: {c['id']} ({speaker_id} at {c['location_display']})")
    except Exception as e:
        print(f"[Commitments] Error checking player arrival: {e}")


# ============================================
# Event Injection
# ============================================

def _inject_commitment_event(commitment, event_type, player_name=None, current_game_time=None):
    """Inject a commitment outcome event into NPC dialog history."""
    try:
        npc_id = commitment["npc_id"]
        target_id = commitment["target_id"]
        location = commitment["location_display"]
        is_companion = commitment.get("is_companion", False)

        # Build display name for target
        if target_id == "player":
            target_display = player_name or "the player"
        else:
            target_display = prettify_voice_name(target_id)
        if event_type == "no_show":
            if is_companion:
                text = f"You and {target_display} decided not to go to {location}."
            else:
                text = f"{target_display} was a no-show at {location}. You waited but they never came."
        elif event_type == "npc_cancelled":
            text = f"You cancelled your meeting with {target_display} at {location}."
        elif event_type == "player_cancelled":
            text = f"{target_display} cancelled your meeting at {location}."
        else:
            text = f"Commitment event: {event_type} at {location}"

        # Cancellation events must never inherit the meeting's future start time.
        # They should be stamped with "now", and if a caller forgets to pass the
        # current game time we prefer to skip the history row rather than create
        # another misleading future-dated entry.
        if event_type in ("npc_cancelled", "player_cancelled") and not current_game_time:
            print(
                f"[Commitments] Skipping {event_type} history event for {npc_id}: "
                f"missing current game time (meeting starts {commitment.get('game_time_start', '')})"
            )
            return

        # Use current game time if available, fall back to commitment start time
        # for outcome events that naturally happen at/after the scheduled time.
        time_source = current_game_time or commitment.get("game_time_start", "")
        game_date, game_time = _split_game_datetime_for_history(time_source)
        entry = {
            "timestamp": int(_time.time()),
            "gameTime": game_time,
            "gameDate": game_date,
            "speaker": "",
            "voiceName": npc_id,
            "target": "",
            "targetId": None,
            "text": text,
            "isPlayer": False,
            "isAIResponse": False,
            "type": "commitment",
            "location": location,
        }
        append_entry(entry)
        print(
            f"[Commitments] Injected {event_type} event for {npc_id} at {time_source}: {text}"
        )
    except Exception as e:
        print(f"[Commitments] Error injecting event: {e}")


# ============================================
# Prompt Building
# ============================================

def create_commitment_from_ui(npc_id, location_id, game_time_start, game_context, lua_socket, player_name, spot_label=None):
    """Create a commitment from the UI (not from LLM action tags).

    Args:
        npc_id: NPC voice name (e.g. "SebastianSallow")
        location_id: Exact location key (e.g. "HM_ThreeBroomsticks", "HM_PostOffice")
        game_time_start: Time string in 'YYYY/MM/DD HH:MM' format
        game_context: Current game context dict
        lua_socket: Socket for sending Lua commands
        player_name: Player character name
        spot_label: Optional spot label for preferred placement (e.g. "Beasts Class")

    Returns (success, error_msg, commitment_dict)
    """
    # Look up location (includes both scheduler activities and teleport-only spots)
    loc_entry = _LOCATION_BY_ID.get(location_id)
    if not loc_entry:
        return False, f"Unknown location: {location_id}", None

    # Compute derived times
    game_time_end = _add_game_minutes(game_time_start, COMMITMENT_WAIT_TIME_MIN)
    override_apply_time = _subtract_game_minutes(game_time_start, COMMITMENT_TRAVEL_TIME_MIN)

    # Conflict check
    conflicts = commitments_db.get_commitments_in_window(npc_id, override_apply_time, game_time_end)
    if conflicts:
        conflict_locs = ", ".join(c["location_display"] for c in conflicts)
        return False, f"Conflicts with existing commitment(s) at {conflict_locs}", None

    # Check companion status
    companion_id = game_context.get('companionId') if game_context else None
    is_companion = bool(companion_id and companion_id.lower() == npc_id.lower())

    # Create in DB
    display = _display_with_label(loc_entry["display"], spot_label)
    commitment_id = commitments_db.create_commitment(
        npc_id=npc_id,
        target_id="player",
        location_id=location_id,
        location_display=display,
        activity_id=loc_entry["activity"],
        game_time_start=game_time_start,
        game_time_end=game_time_end,
        override_apply_time=override_apply_time,
        is_companion=is_companion,
    )
    if not commitment_id:
        return False, "Failed to create commitment in database", None

    # Store spot label for deferred activation
    if spot_label:
        _commitment_spot_labels[commitment_id] = spot_label

    # If override time has already passed, immediately activate
    current_time_str = _get_current_game_time_str(game_context) if game_context else None
    if current_time_str and current_time_str >= override_apply_time:
        commitments_db.update_status(commitment_id, "active")
        if lua_socket:
            lua_socket.send_activate_commitment(npc_id, loc_entry["activity"], location_id, spot_label)
        print(f"[Commitments] UI commitment immediately activated: {commitment_id}")

    # Inject dialogue history event
    npc_display = prettify_voice_name(npc_id)
    time_display = _format_time_display(game_time_start)
    event_text = f"{player_name or 'The player'} arranged to meet {npc_display} at {display} at {time_display}."
    try:
        history_time_source = current_time_str or game_time_start
        game_date, game_time = _split_game_datetime_for_history(history_time_source)
        entry = {
            "timestamp": int(_time.time()),
            "gameTime": game_time,
            "gameDate": game_date,
            "speaker": "",
            "voiceName": npc_id,
            "target": "",
            "targetId": None,
            "text": event_text,
            "isPlayer": False,
            "isAIResponse": False,
            "type": "commitment",
            "location": display,
        }
        append_entry(entry)
        print(
            f"[Commitments] Injected UI commitment creation event at current game time "
            f"{history_time_source} (meeting starts {game_time_start})"
        )
    except Exception as e:
        print(f"[Commitments] Error injecting UI commitment event: {e}")

    # Send notification
    _notify(lua_socket, commitment_id, "created",
            f"Meeting {npc_display} at {display} - {time_display}")

    # Suppress imminent reminders
    if current_time_str:
        fifteen_from_now = _add_game_minutes(current_time_str, COMMITMENT_TRAVEL_TIME_MIN)
        if game_time_start <= fifteen_from_now:
            _notified.setdefault(commitment_id, set()).update({"warning", "due"})

    # Return the full commitment dict
    commitment = commitments_db.get_commitment(commitment_id)
    return True, None, commitment


def build_commitment_action_instructions(player_name, is_current_companion=False, npc_id=None):
    """Build action instruction strings for the LLM prompt.
    Returns a list of strings to append to action_parts.
    When npc_id is provided, only includes cancel instruction if the NPC has cancellable commitments.
    Returns empty list for excluded NPCs (portraits, ghosts, dead, etc.).
    """
    if npc_id and is_excluded_npc(npc_id):
        return []

    parts = []

    parts.append(
        f'- `[Action: Meet "Name" at "Location" on "M/D/YYYY H:MM AM/PM"]` — '
        f'Go to a location to meet someone. This is the ONLY way you can physically travel somewhere. '
        f'ONLY use when {player_name} explicitly agrees to meet somewhere, or when you propose a meeting and {player_name} clearly accepts. '
        f'Casual mentions of places do NOT count — there must be a clear, mutual agreement to meet. '
        f'{"Since you are already traveling with " + player_name + ", do NOT use this for immediate trips — only for future plans. " if is_current_companion else "For immediate trips, use the current date/time. "}'
        f'Use "{player_name}" as the name when meeting them. '
        f'Do NOT commit and ask for agreement in the same response — wait for them to agree first. '
        f'Location can be any named place in Hogwarts or Hogsmeade.'
    )
    # Only show cancel action when this NPC actually has cancellable commitments
    has_cancellable = True  # default if no npc_id provided
    if npc_id:
        active = commitments_db.get_commitments_for_npc(npc_id, include_resolved=False)
        has_cancellable = any(c["status"] in ("pending", "active", "arrived") for c in active)
    if has_cancellable:
        parts.append(
            '- `[Action: CancelCommitment ID]` — Cancel a previously made commitment by its ID (shown in your commitment context). '
            'ONLY use to back out of a meeting that you no longer want to attend. Do NOT use when a meeting is happening or completed — completion is tracked automatically.'
        )

    return parts


def build_owl_mail_commitment_instructions(player_name, npc_id=None):
    """Build commitment action instructions for owl mail letter generation.

    Simpler than the live-chat version: no companion logic, no turn-taking
    rules. The NPC just proposes a meeting in its letter.
    Returns a fully formatted markdown block ready to inject into the prompt,
    or empty string for excluded NPCs.
    """
    if npc_id and is_excluded_npc(npc_id):
        return ""
    has_cancellable = False
    if npc_id:
        active = commitments_db.get_commitments_for_npc(npc_id, include_resolved=False)
        has_cancellable = any(c["status"] in ("pending", "active", "arrived") for c in active)

    cancel_line = (
        '\n- `[Action: CancelCommitment ID]` — Cancel a previously made commitment by its ID (shown in your commitment context). '
        'ONLY use to back out of a meeting that you no longer want to attend. Do NOT use when a meeting is happening or completed — completion is tracked automatically.'
        if has_cancellable else ''
    )

    return (
        f"**Actions:** You may OPTIONALLY include an action tag in your letter using `[Action: X]` format. "
        f"The vast majority of letters should have NO action tag.\n"
        f'- `[Action: Meet "{player_name}" at "Location" on "M/D/YYYY H:MM AM/PM"]` — '
        f'Propose meeting {player_name} at a location. '
        f'Use this when suggesting a meeting with {player_name} or accepting a meeting proposed by {player_name}. '
        f'Casual mentions of places do NOT count — there must be a clear intent to meet. '
        f'Location can be any named place in Hogwarts or Hogsmeade.'
        f'{cancel_line}'
    )


def build_commitment_context(npc_id, player_name=None):
    """Build commitment context string for the NPC's prompt.
    Returns formatted string or empty string if no commitments or excluded NPC.
    """
    if npc_id and is_excluded_npc(npc_id):
        return ""
    all_commitments = commitments_db.get_commitments_for_npc(npc_id, include_resolved=True)
    if not all_commitments:
        return ""

    current = []
    upcoming = []
    past = []

    for c in all_commitments:
        status = c["status"]
        target_display = (player_name or "the player") if c["target_id"] == "player" else prettify_voice_name(c["target_id"])
        location = c["location_display"]
        time_display = _format_time_display(c["game_time_start"])

        if status in ("active", "arrived"):
            if status == "arrived":
                current.append(f"- At {location} meeting {target_display} (arrived) [id: {c['id']}]")
            else:
                current.append(f"- At {location} to meet {target_display} (since {time_display}) [id: {c['id']}]")
        elif status == "pending":
            upcoming.append(f"- Meeting {target_display} at {location} on {time_display} [id: {c['id']}]")
        elif status == "completed":
            past.append(f"- Met {target_display} at {location} on {time_display} \u2713")
        elif status == "no_show":
            past.append(f"- {target_display} was a no-show at {location} on {time_display}")
        elif status == "cancelled":
            past.append(f"- Cancelled meeting with {target_display} at {location}")

    # Limit total to COMMITMENT_MAX_CONTEXT_HISTORY (drop oldest past first)
    total = len(current) + len(upcoming) + len(past)
    if total > COMMITMENT_MAX_CONTEXT_HISTORY:
        excess = total - COMMITMENT_MAX_CONTEXT_HISTORY
        past = past[excess:]  # Drop oldest past items

    if not current and not upcoming and not past:
        return ""

    sections = []
    if current:
        sections.append("Current:\n" + "\n".join(current))
    if upcoming:
        sections.append("Upcoming:\n" + "\n".join(upcoming))
    if past:
        sections.append("Past:\n" + "\n".join(past))

    return "**Commitments:**\n" + "\n\n".join(sections)
