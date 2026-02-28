"""
Commitment system business logic for Sonorus.
Handles validation, processing, prompt building, time-based activation, and event injection.
All commitment logic lives here — server.py and lua_socket.py just call into this module.
"""

import re
import time as _time
import difflib

from constants import (
    COMMITMENT_TRAVEL_TIME_MIN,
    COMMITMENT_WAIT_TIME_MIN,
    COMMITMENT_MAX_CONTEXT_HISTORY,
    LOCATION_ACTIVITIES,
)
from . import commitments_db
from .llm_utils import parse_commitment_actions
from .dialogue import prettify_voice_name


# ============================================
# Helpers
# ============================================

# Build display name -> (location_id, entry) lookup
_DISPLAY_NAME_MAP = {}
for _loc_id, _entry in LOCATION_ACTIVITIES.items():
    _DISPLAY_NAME_MAP[_entry["display"].lower()] = (_loc_id, _entry)

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

    # Fuzzy match
    matches = difflib.get_close_matches(text_lower, _DISPLAY_NAMES, n=1, cutoff=0.6)
    if matches:
        return _DISPLAY_NAME_MAP[matches[0]]

    return None, None


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

def validate_meet_action(action, npc_id, game_context):
    """Validate a meet commitment action.

    Returns (success: bool, error_msg: str|None, parsed_data: dict|None)
    parsed_data includes: location_id, location_display, activity_id, game_time_start,
                         game_time_end, override_apply_time
    """
    # Fuzzy match location
    location_id, loc_entry = _fuzzy_match_location(action.get("location"))
    if not location_id:
        return False, f"Location '{action.get('location')}' not found in available locations", None

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
        "location_display": loc_entry["display"],
        "activity_id": loc_entry["activity"],
        "game_time_start": game_time_start,
        "game_time_end": game_time_end,
        "override_apply_time": override_apply_time,
    }


# ============================================
# Processing (called from server.py)
# ============================================

def process_commitment_actions(raw_response, speaker_id, player_name, game_context, lua_socket):
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
                _process_meet_action(action, speaker_id, player_name, game_context, lua_socket, results)
            elif action["type"] == "cancel":
                _process_cancel_action(action, speaker_id, player_name, game_context, lua_socket, results)
        except Exception as e:
            print(f"[Commitments] Error processing {action['type']} action: {e}")

    return results


def _process_meet_action(action, speaker_id, player_name, game_context, lua_socket, results):
    """Process a single meet action."""
    # Resolve target
    target = action.get("target", "")
    if target.lower() == player_name.lower() or target.lower() in ("player", player_name.lower()):
        target_id = "player"
    else:
        target_id = target  # Future: resolve NPC name to voice ID

    # Validate
    valid, error_msg, parsed = validate_meet_action(action, speaker_id, game_context)
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
    from .settings import load_settings
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
                    lua_socket.send_activate_commitment(c["npc_id"], c["activity_id"], c["location_id"])
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
        from .dialogue_db import append_entry

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

        # Use current game time if available, fall back to commitment start time
        time_source = current_game_time or commitment.get("game_time_start", "")
        game_date, game_time = _split_game_datetime_for_history(time_source)
        entry = {
            "timestamp": int(_time.time()),
            "gameTime": game_time,
            "gameDate": game_date,
            "speaker": "",
            "voiceName": npc_id,
            "target": "",
            "text": text,
            "isPlayer": False,
            "isAIResponse": False,
            "type": "commitment",
            "location": location,
        }
        append_entry(entry)
        print(f"[Commitments] Injected {event_type} event for {npc_id}: {text}")
    except Exception as e:
        print(f"[Commitments] Error injecting event: {e}")


# ============================================
# Prompt Building
# ============================================

def create_commitment_from_ui(npc_id, location_id, game_time_start, game_context, lua_socket, player_name):
    """Create a commitment from the UI (not from LLM action tags).

    Args:
        npc_id: NPC voice name (e.g. "SebastianSallow")
        location_id: Exact location key from LOCATION_ACTIVITIES (e.g. "HM_ThreeBroomsticks")
        game_time_start: Time string in 'YYYY/MM/DD HH:MM' format
        game_context: Current game context dict
        lua_socket: Socket for sending Lua commands
        player_name: Player character name

    Returns (success, error_msg, commitment_dict)
    """
    # Look up location
    if location_id not in LOCATION_ACTIVITIES:
        return False, f"Unknown location: {location_id}", None
    loc_entry = LOCATION_ACTIVITIES[location_id]

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
    commitment_id = commitments_db.create_commitment(
        npc_id=npc_id,
        target_id="player",
        location_id=location_id,
        location_display=loc_entry["display"],
        activity_id=loc_entry["activity"],
        game_time_start=game_time_start,
        game_time_end=game_time_end,
        override_apply_time=override_apply_time,
        is_companion=is_companion,
    )
    if not commitment_id:
        return False, "Failed to create commitment in database", None

    # If override time has already passed, immediately activate
    current_time_str = _get_current_game_time_str(game_context) if game_context else None
    if current_time_str and current_time_str >= override_apply_time:
        commitments_db.update_status(commitment_id, "active")
        if lua_socket:
            lua_socket.send_activate_commitment(npc_id, loc_entry["activity"], location_id)
        print(f"[Commitments] UI commitment immediately activated: {commitment_id}")

    # Inject dialogue history event
    npc_display = prettify_voice_name(npc_id)
    time_display = _format_time_display(game_time_start)
    event_text = f"{player_name or 'The player'} arranged to meet {npc_display} at {loc_entry['display']} at {time_display}."
    try:
        from .dialogue_db import append_entry
        game_date, game_time = _split_game_datetime_for_history(game_time_start)
        entry = {
            "timestamp": int(_time.time()),
            "gameTime": game_time,
            "gameDate": game_date,
            "speaker": "",
            "voiceName": npc_id,
            "target": "",
            "text": event_text,
            "isPlayer": False,
            "isAIResponse": False,
            "type": "commitment",
            "location": loc_entry["display"],
        }
        append_entry(entry)
    except Exception as e:
        print(f"[Commitments] Error injecting UI commitment event: {e}")

    # Send notification
    _notify(lua_socket, commitment_id, "created",
            f"Meeting {npc_display} at {loc_entry['display']} - {time_display}")

    # Suppress imminent reminders
    if current_time_str:
        fifteen_from_now = _add_game_minutes(current_time_str, COMMITMENT_TRAVEL_TIME_MIN)
        if game_time_start <= fifteen_from_now:
            _notified.setdefault(commitment_id, set()).update({"warning", "due"})

    # Return the full commitment dict
    commitment = commitments_db.get_commitment(commitment_id)
    return True, None, commitment


def build_commitment_action_instructions(player_name):
    """Build action instruction strings for the LLM prompt.
    Returns a list of strings to append to action_parts.
    """
    parts = []

    # Location list grouped by area
    _COMMON_ROOM_IDS = {"HOG_GryffindorTower", "HOG_HufflepuffBasement", "HOG_Ravenclaw_CommonRoom", "HOG_Slytherin_CommonRoom"}
    hogsmeade_locs = [e["display"] for lid, e in LOCATION_ACTIVITIES.items() if lid.startswith("HM_")]
    common_room_locs = [e["display"] for lid, e in LOCATION_ACTIVITIES.items() if lid in _COMMON_ROOM_IDS]
    hogwarts_locs = [e["display"] for lid, e in LOCATION_ACTIVITIES.items()
                     if lid.startswith("HOG_") and lid not in _COMMON_ROOM_IDS]

    locations_text = (
        f"Hogsmeade: {', '.join(hogsmeade_locs)}\n"
        f"Hogwarts: {', '.join(hogwarts_locs)}\n"
        f"Common Rooms: {', '.join(common_room_locs)}"
    )

    parts.append(
        f'- `[Action: Meet "Name" at "Location" on "M/D/YYYY H:MM AM/PM"]` — '
        f'Go to a location to meet someone. This is the ONLY way you can physically travel somewhere — '
        f'use it whenever you agree to go somewhere, whether right now or later. '
        f'For immediate trips, use the current date/time. '
        f'The name should be "{player_name}" for the player. '
        f'Do NOT commit and ask for agreement in the same response — wait for them to agree first.\n'
        f'  Available locations:\n  {locations_text}'
    )
    parts.append(
        '- `[Action: CancelCommitment ID]` — Cancel a previously made commitment by its ID (shown in your commitment context).'
    )

    return parts


def build_commitment_context(npc_id, player_name=None):
    """Build commitment context string for the NPC's prompt.
    Returns formatted string or empty string if no commitments.
    """
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
