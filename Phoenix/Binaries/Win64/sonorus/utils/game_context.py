"""
Game context utilities for Sonorus.
Handles formatting of game context for LLM prompts.
"""

import json
import os
import time

from .settings import load_settings
from .character_bios import get_editor_guidance
from .dialogue import collapse_consecutive_events, format_dialogue_entry, _clean_history_line_for_prompt
from .landmarks import get_landmark_beacons, format_beacons_for_llm
from .localization import find_npc_id_by_name, get_display_name


# ── Schedule context ────────────────────────────────────────────────

_schedule_cache = None
_roster_cache = None

_DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

_TEACHER_DISPLAY_SUBJECTS = {
    "Beasts": "Care of Magical Creatures",
}


def _normalize_participant_names(participants):
    """Return participant names in order, dropping duplicate display names."""
    normalized = []
    seen = set()
    for participant in participants or []:
        if participant is None:
            continue
        name = str(participant).strip()
        if not name:
            continue
        key = "".join(name.split()).lower()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(name)
    return normalized


def _format_participant_list(participants):
    names = _normalize_participant_names(participants)
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return ", ".join(names[:-1]) + f", and {names[-1]}"


def _get_current_speaker_name(current_speaker):
    """Return the prompted character's display name for explicit identity cues."""
    if not current_speaker:
        return ""
    speaker_name = get_display_name(current_speaker)
    return speaker_name if speaker_name and speaker_name != "Unknown" else str(current_speaker)


def _format_vision_section(vision_section):
    """Format vision context with grounding for commonly over-inferred wording."""
    output = "**What you can see:**\n" + vision_section
    if "cavernous" in vision_section.casefold():
        output += (
            '\n\n**Vision grounding:** "Cavernous" only means spacious or cave-like; '
            "do not infer that the location is a cave. In the Hogwarts setting, it may simply "
            "describe a large doorway or interior."
        )
    return output


def _load_teacher_schedules():
    global _schedule_cache
    if _schedule_cache is not None:
        return _schedule_cache
    path = os.path.join(os.path.dirname(__file__), "..", "data", "teacher_schedules.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            _schedule_cache = json.load(f)
    except Exception:
        _schedule_cache = {}
    return _schedule_cache


def _load_roster_houses():
    """Build speaker_id -> house mapping from npc_board_roster.json."""
    global _roster_cache
    if _roster_cache is not None:
        return _roster_cache
    path = os.path.join(os.path.dirname(__file__), "..", "data", "npc_board_roster.json")
    _roster_cache = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            roster = json.load(f)
        for _board_id, data in roster.items():
            house = data.get("house")
            if house and "members" in data:
                for member in data["members"]:
                    _roster_cache[member] = house
    except Exception:
        pass
    return _roster_cache


def _fmt_schedule_time(t_str):
    """'12:00' -> '12:00 PM', '08:00' -> '8:00 AM'."""
    try:
        parts = t_str.split(":")
        h, m = int(parts[0]), int(parts[1])
        ampm = "AM" if h < 12 else "PM"
        h12 = h % 12 or 12
        return f"{h12}:{m:02d} {ampm}"
    except Exception:
        return t_str


def _fmt_mil(military_int):
    """Format military int (e.g. 1400) to '2:00 PM'."""
    h = military_int // 100
    m = military_int % 100
    ampm = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12}:{m:02d} {ampm}"


def _time_to_military(h, m):
    """Convert hour/minute to military int like 1430."""
    return h * 100 + m



def _parse_time(t_str):
    """'12:00' -> 1200 military int."""
    try:
        parts = t_str.split(":")
        return int(parts[0]) * 100 + int(parts[1])
    except Exception:
        return 0


def _build_day_timeline(sched_data, day_name, is_teacher, speaker_id=None):
    """Build an ordered list of periods for a given day.

    Each entry: {"label": str, "start": int, "end": int, "type": str, "houses": list|None, "subject": str|None, "teacher": str|None}
    Types: "class", "meal", "free", "after_hours"
    """
    data = sched_data
    teachers = data.get("teachers", {})

    is_weekend = day_name in ("Saturday", "Sunday")

    if is_weekend:
        return [
            {"label": "Weekend free time", "start": 600, "end": 2159, "type": "free", "houses": None, "subject": None, "teacher": None},
            {"label": "After hours", "start": 2200, "end": 559, "type": "after_hours", "houses": None, "subject": None, "teacher": None},
        ]

    # Build the fixed weekday timeline
    periods = []

    # Free morning
    periods.append({"label": "Free time", "start": 600, "end": 759, "type": "free"})
    # Breakfast
    periods.append({"label": "Breakfast", "start": 800, "end": 959, "type": "meal"})
    # Free mid-morning
    periods.append({"label": "Free time", "start": 1000, "end": 1159, "type": "free"})
    # Class block 1
    periods.append({"label": "Class block 1", "start": 1200, "end": 1359, "type": "class_block"})
    # Free afternoon
    periods.append({"label": "Free time", "start": 1400, "end": 1559, "type": "free"})
    # Class block 2
    periods.append({"label": "Class block 2", "start": 1600, "end": 1759, "type": "class_block"})
    # Free evening
    periods.append({"label": "Free time", "start": 1800, "end": 1859, "type": "free"})
    # Dinner
    periods.append({"label": "Dinner", "start": 1900, "end": 2029, "type": "meal"})
    # Free night
    periods.append({"label": "Free time", "start": 2030, "end": 2159, "type": "free"})
    # After hours
    periods.append({"label": "After hours", "start": 2200, "end": 559, "type": "after_hours"})

    # Now resolve class blocks for this specific NPC
    if is_teacher and speaker_id and speaker_id in teachers:
        teacher_info = teachers[speaker_id]
        subject = teacher_info.get("subject", "")
        display_subject = _TEACHER_DISPLAY_SUBJECTS.get(subject, subject)
        for cls in teacher_info.get("classes", []):
            if cls["day"] == day_name:
                cls_start = _parse_time(cls["start"])
                cls_end = _parse_time(cls["end"])
                # Replace the matching class_block
                for p in periods:
                    if p["type"] == "class_block" and p["start"] == cls_start:
                        p["label"] = f"Teaching {display_subject}"
                        p["type"] = "class"
                        p["houses"] = cls.get("houses", [])
                        p["subject"] = display_subject
                        p["teacher"] = speaker_id
        # Any remaining class_blocks become free time for this teacher
        for p in periods:
            if p["type"] == "class_block":
                p["label"] = "Free time"
                p["type"] = "free"
    elif not is_teacher:
        # Student: look up their house and find which classes they attend
        roster = _load_roster_houses()
        student_house = roster.get(speaker_id)
        if student_house:
            for tid, tinfo in teachers.items():
                subject = tinfo.get("subject", "")
                display_subject = _TEACHER_DISPLAY_SUBJECTS.get(subject, subject)
                # Format teacher name from speaker_id
                teacher_name = "Professor " + ''.join(
                    ' ' + c if c.isupper() and i > 0 else c
                    for i, c in enumerate(tid)
                ).split()[-1]  # Last name only
                for cls in tinfo.get("classes", []):
                    if cls["day"] == day_name and student_house in cls.get("houses", []):
                        cls_start = _parse_time(cls["start"])
                        for p in periods:
                            if p["type"] == "class_block" and p["start"] == cls_start:
                                p["label"] = f"{display_subject} with {teacher_name}"
                                p["type"] = "class"
                                p["houses"] = cls.get("houses", [])
                                p["subject"] = display_subject
                                p["teacher"] = tid
        # Astronomy special: check if student has astronomy tonight (21:00–22:00)
        if student_house:
            for cls in teachers.get("SatyavatiShah", {}).get("classes", []):
                if cls["day"] == day_name and student_house in cls.get("houses", []):
                    # Insert astronomy into the free_night slot, split it
                    for i, p in enumerate(periods):
                        if p["type"] == "free" and p["start"] == 2030:
                            teacher_name = "Professor Shah"
                            display_subject = "Astronomy"
                            # Split: 20:30-20:59 free, 21:00-22:00 astronomy
                            periods[i] = {"label": "Free time", "start": 2030, "end": 2059, "type": "free",
                                          "houses": None, "subject": None, "teacher": None}
                            periods.insert(i + 1, {
                                "label": f"Astronomy with {teacher_name}",
                                "start": 2100, "end": 2159, "type": "class",
                                "houses": cls.get("houses", []),
                                "subject": display_subject, "teacher": "SatyavatiShah"
                            })
                            break
        # Any remaining class_blocks become free time
        for p in periods:
            if p["type"] == "class_block":
                p["label"] = "Free time"
                p["type"] = "free"

    # Also handle astronomy for teachers (SatyavatiShah)
    if is_teacher and speaker_id == "SatyavatiShah":
        teacher_info = teachers.get("SatyavatiShah", {})
        for cls in teacher_info.get("classes", []):
            if cls["day"] == day_name:
                cls_start = _parse_time(cls["start"])
                if cls_start == 2100:
                    display_subject = "Astronomy"
                    for i, p in enumerate(periods):
                        if p["type"] == "free" and p["start"] == 2030:
                            periods[i] = {"label": "Free time", "start": 2030, "end": 2059, "type": "free",
                                          "houses": None, "subject": None, "teacher": None}
                            periods.insert(i + 1, {
                                "label": f"Teaching {display_subject}",
                                "start": 2100, "end": 2159, "type": "class",
                                "houses": cls.get("houses", []),
                                "subject": display_subject, "teacher": speaker_id
                            })
                            break

    # Ensure all entries have full keys
    for p in periods:
        p.setdefault("houses", None)
        p.setdefault("subject", None)
        p.setdefault("teacher", None)

    return periods


def _in_period(military_time, start, end):
    """Check if military_time falls within [start, end], handling midnight wrap."""
    if start <= end:
        return start <= military_time <= end
    else:
        return military_time >= start or military_time <= end


def build_schedule_context(speaker_id, context):
    """Build a schedule context line for a teacher or student NPC.

    Returns a string like:
      'You are currently teaching Defence Against the Dark Arts to Gryffindor and Slytherin students. Adri Valter (Slytherin) is in attendance. Next: Free time at 2:00 PM.'
    Or empty string if speaker has no schedule data.
    """
    sched_data = _load_teacher_schedules()
    if not sched_data:
        return ""

    teachers = sched_data.get("teachers", {})
    is_teacher = speaker_id in teachers

    # For students, check roster
    if not is_teacher:
        roster = _load_roster_houses()
        if speaker_id not in roster:
            return ""

    # Get current game time
    hour = context.get('hour')
    minute = context.get('minute')
    day_of_week = context.get('dayOfWeek')
    if hour is None or day_of_week is None:
        return ""
    if minute is None:
        minute = 0

    try:
        hour = int(hour)
        minute = int(minute)
        day_of_week = int(day_of_week)
    except (ValueError, TypeError):
        return ""

    day_name = _DAY_NAMES[day_of_week % 7]
    now_mil = _time_to_military(hour, minute)

    timeline = _build_day_timeline(sched_data, day_name, is_teacher, speaker_id)
    if not timeline:
        return ""

    # Find current and next period
    current = None
    next_period = None
    for i, p in enumerate(timeline):
        if _in_period(now_mil, p["start"], p["end"]):
            current = p
            # Find next non-same-type period (skip if after_hours, just show it)
            if i + 1 < len(timeline):
                next_period = timeline[i + 1]
            break

    if not current:
        return ""

    # Player info
    player_name = context.get('playerName', 'the student')
    player_house = context.get('playerHouse', '')

    parts = []

    # Current period description
    if current["type"] == "class":
        if is_teacher:
            houses_str = " and ".join(current["houses"]) if current["houses"] else "students"
            parts.append(f"You are currently teaching {current['subject']} to {houses_str} students.")
            if player_house and current["houses"] and player_house in current["houses"]:
                parts.append(f"{player_name} is in attendance at your class.")
            elif player_house:
                parts.append(f"{player_name} ({player_house}) is not scheduled for this session.")
        else:
            start_str = _fmt_mil(current["start"])
            end_str = _fmt_mil(current["end"])
            label = current["label"]
            houses = current.get("houses") or []
            houses_note = f" (shared with {' and '.join(houses)} students)" if houses else ""
            parts.append(f"You should be in {label} right now ({start_str} \u2013 {end_str}){houses_note}.")
            if player_house and houses and player_house in houses:
                parts.append(f"{player_name} is also in this class.")
            elif player_house and houses:
                parts.append(f"{player_name} ({player_house}) is not in this class.")
    elif current["type"] == "meal":
        meal = current["label"].lower()
        parts.append(f"It is {meal} time.")
    elif current["type"] == "free":
        parts.append("You have free time right now.")
    elif current["type"] == "after_hours":
        parts.append("It is after hours.")

    # Weekend
    is_weekend = day_name in ("Saturday", "Sunday")
    if is_weekend and current["type"] == "free":
        parts = ["It is the weekend."]

    # Next period
    if next_period:
        next_time = _fmt_mil(next_period["start"])
        if next_period["type"] == "class":
            if is_teacher:
                houses_str = " and ".join(next_period["houses"]) if next_period["houses"] else "students"
                subj = next_period["subject"]
                parts.append(f"Your next class is at {next_time} \u2014 {subj} with {houses_str}.")
            else:
                next_houses = next_period.get("houses") or []
                next_label = next_period["label"]
                if player_house and next_houses and player_house in next_houses:
                    parts.append(f"Next: {next_label} at {next_time}. {player_name} will also be there.")
                elif player_house and next_houses:
                    parts.append(f"Next: {next_label} at {next_time} ({' and '.join(next_houses)} only).")
                else:
                    parts.append(f"Next: {next_label} at {next_time}.")
        elif next_period["type"] == "meal":
            parts.append(f"Next: {next_period['label']} at {next_time}.")
        elif next_period["type"] == "free":
            parts.append(f"Next: Free time at {next_time}.")
        elif next_period["type"] == "after_hours":
            parts.append(f"After hours begins at {next_time}.")

    # For teachers during non-class time, show when their next class is (may be today or not)
    if is_teacher and current["type"] != "class" and (not next_period or next_period["type"] != "class"):
        teacher_info = teachers.get(speaker_id, {})
        display_subject = _TEACHER_DISPLAY_SUBJECTS.get(teacher_info.get("subject", ""), teacher_info.get("subject", ""))
        # Find next class today
        found_next_class = False
        for p in timeline:
            if p["type"] == "class" and p["start"] > now_mil:
                nt = _fmt_mil(p["start"])
                houses_str = " and ".join(p["houses"]) if p["houses"] else "students"
                parts.append(f"Your next class is at {nt} \u2014 {p['subject']} with {houses_str}.")
                found_next_class = True
                break
        if not found_next_class and not is_weekend:
            # Find chronologically nearest class day
            today_idx = _DAY_NAMES.index(day_name) if day_name in _DAY_NAMES else 0
            best_day = None
            best_dist = 8
            for cls in teacher_info.get("classes", []):
                if cls["day"] != day_name and cls["day"] in _DAY_NAMES:
                    cls_idx = _DAY_NAMES.index(cls["day"])
                    dist = (cls_idx - today_idx) % 7
                    if dist < best_dist:
                        best_dist = dist
                        best_day = cls["day"]
            if best_day:
                parts.append(f"Your next class is on {best_day}.")

    return " ".join(parts)


# ── Schedule transition notifications ───────────────────────────────

_last_schedule_period = None   # key string like "Monday_1200_class"
_last_schedule_type = None     # "class", "meal", "free", "after_hours"


def check_schedule_transition(context):
    """Check if the player's schedule period has changed. Returns notification text or None.

    Called on each game_context update (~every 5s). Only fires once per transition.
    """
    global _last_schedule_period, _last_schedule_type

    sched_data = _load_teacher_schedules()
    if not sched_data:
        return None

    hour = context.get('hour')
    minute = context.get('minute')
    day_of_week = context.get('dayOfWeek')
    player_house = context.get('playerHouse', '')

    if hour is None or day_of_week is None or not player_house:
        return None

    try:
        hour = int(hour)
        minute = int(minute or 0)
        day_of_week = int(day_of_week)
    except (ValueError, TypeError):
        return None

    day_name = _DAY_NAMES[day_of_week % 7]
    now_mil = _time_to_military(hour, minute)

    # Build player's timeline: temporarily add a roster entry for the player's house
    # so _build_day_timeline treats them as a student. Use a synthetic speaker_id.
    _PLAYER_SCHED_KEY = "__player__"
    roster = _load_roster_houses()
    roster[_PLAYER_SCHED_KEY] = player_house
    timeline = _build_day_timeline(sched_data, day_name, False, _PLAYER_SCHED_KEY)
    roster.pop(_PLAYER_SCHED_KEY, None)

    # Find current period (single scan)
    current_period = None
    for p in timeline:
        if _in_period(now_mil, p["start"], p["end"]):
            current_period = p
            break

    if current_period is None:
        return None

    current_key = f"{day_name}_{current_period['start']}_{current_period['type']}"
    current_type = current_period.get("type", "")

    if _last_schedule_period is None:
        # First check — initialize without notifying
        _last_schedule_period = current_key
        _last_schedule_type = current_type
        return None

    if current_key == _last_schedule_period:
        return None

    # Period changed!
    prev_type = _last_schedule_type
    _last_schedule_period = current_key
    _last_schedule_type = current_type

    is_weekend = day_name in ("Saturday", "Sunday")

    # Build notification text
    ptype = current_period.get("type", "")
    if ptype == "class":
        return f"{current_period['label']} starts now"
    elif ptype == "meal":
        return current_period["label"]
    elif ptype == "free":
        if is_weekend:
            return None
        # Only notify free time if coming out of a class or meal
        if prev_type in ("class", "meal"):
            return "Free time"
        return None
    elif ptype == "after_hours":
        return "After hours \u2014 time for bed"

    return None


def _format_mail_time_ago(sent_at, current_minutes):
    """Format relative time between a mail's sent_at and current game minutes.

    Returns string like '2 hours ago', 'yesterday', '3 days ago'.
    """
    diff = current_minutes - sent_at
    if diff < 0:
        return ""
    minutes = int(diff)
    if minutes < 60:
        return "just now" if minutes < 5 else f"{minutes} minutes ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = hours // 24
    if days == 1:
        return "yesterday"
    if days < 7:
        return f"{days} days ago"
    weeks = days // 7
    if weeks == 1:
        return "1 week ago"
    if days < 30:
        return f"{weeks} weeks ago"
    months = days // 30
    return f"{months} month{'s' if months != 1 else ''} ago"


def _get_mail_context(speaker_id, game_context=None, player_name=None):
    """Get recent mail context for an NPC, for injection into conversation prompt.

    Shows the most recent thread (up to 10 replies), using summaries where available.
    Includes timestamps and relative time for each letter.
    """
    try:
        from utils.owl_post_db import get_recent_mail_for_npc, get_current_game_minutes
        from utils.dialogue_db import _minutes_to_game_datetime, _format_game_time
        mail = get_recent_mail_for_npc(speaker_id, limit=10)
        if not mail:
            return ""
        # Find the most recent thread
        latest_thread = mail[-1].get("thread_id")
        thread_mail = [m for m in mail if m.get("thread_id") == latest_thread]
        if not thread_mail:
            return ""

        current_minutes = get_current_game_minutes(game_context) if game_context else 0

        lines = []
        for m in thread_mail:
            sender_label = "You" if m["sender"] == speaker_id else "The player"
            content = m.get("summary") or m["body"]

            # Build timestamp info
            time_info = ""
            sent_at = m.get("sent_at")
            if sent_at and current_minutes:
                sent_at_minutes = int(sent_at)
                (_, (h, mi)) = _minutes_to_game_datetime(sent_at_minutes)
                time_str = _format_game_time(h, mi)
                ago_str = _format_mail_time_ago(sent_at_minutes, current_minutes)
                time_info = f" [{time_str}, {ago_str}]" if ago_str else f" [{time_str}]"

            lines.append(f"- {sender_label} wrote{time_info}: \"{m['subject']}\" — {content}")
        header = f"## Recent Letters with {player_name}" if player_name else "## Recent Letters"
        return "\n\n" + header + "\n" + "\n".join(lines)
    except Exception:
        return ""


def _get_board_context(speaker_id):
    """Get notice board context for an NPC — boards they post on, with passwords if applicable."""
    try:
        from utils.owl_post_db import load_board_roster, get_board_by_slug
        roster = load_board_roster()
        lines = []
        for slug, info in roster.items():
            members = info.get("members", [])
            if speaker_id not in members:
                continue
            board = get_board_by_slug(slug)
            if not board:
                continue
            name = board["name"]
            desc = board.get("description", "")
            entry = f"- {name}"
            if board.get("access_type") == "password_locked" and board.get("password"):
                entry += f' (password: "{board["password"]}")'
            if desc:
                entry += f" — {desc}"
            lines.append(entry)
        if not lines:
            return ""
        return "\n\n## Notice Boards You Post On\n" + "\n".join(lines)
    except Exception:
        return ""


def _get_mail_recency_references(speaker_id, game_context=None, player_name=None):
    """Get short letter recency references for dynamic context.

    Returns lines like: Recent letters: "A Dash to the Forest?" (you wrote, 2 days ago)
    """
    try:
        from utils.owl_post_db import get_recent_mail_for_npc, get_current_game_minutes
        mail = get_recent_mail_for_npc(speaker_id, limit=10)
        if not mail:
            return ""
        latest_thread = mail[-1].get("thread_id")
        thread_mail = [m for m in mail if m.get("thread_id") == latest_thread]
        if not thread_mail:
            return ""

        current_minutes = get_current_game_minutes(game_context) if game_context else 0

        refs = []
        for m in thread_mail:
            sender_label = "you" if m["sender"] == speaker_id else "the player"
            ago_str = ""
            sent_at = m.get("sent_at")
            if sent_at and current_minutes:
                ago_str = _format_mail_time_ago(int(sent_at), current_minutes)
            ref = f'"{m["subject"]}" ({sender_label} wrote'
            if ago_str:
                ref += f", {ago_str}"
            ref += ")"
            refs.append(ref)

        if not refs:
            return ""
        return "Recent letters: " + "; ".join(refs)
    except Exception:
        return ""


def format_game_context(context, current_speaker=None, participants=None, observer_mode=False):
    """Format game context for LLM prompt

    Args:
        context: Game context dict from Lua
        current_speaker: NPC ID of the character being prompted (to exclude from nearby list)
        participants: List of participant names in the conversation (for interjections).
                      If None, defaults to just the player.
        observer_mode: If True, the player is not involved in the conversation (director mode).
                       Skips player-centric info like visibility, attire, and player header.
    """
    if not context:
        return ""

    player_name = context.get('playerName', 'Unknown')
    player_house = context.get('playerHouse', 'Unknown')
    in_stealth = context.get('inStealth', False)
    current_speaker_name = _get_current_speaker_name(current_speaker)
    speaker_subject = f"You, {current_speaker_name}," if current_speaker_name else "You"

    settings = load_settings()
    conv_settings = settings.get('conversation', {})

    # === RESOLVE LOCATION (needed for header + scene) ===
    zone = context.get('zoneLocation', '')
    region = context.get('location', '')
    location_name = zone if zone else (region.replace('_', ' ') if region and region not in ("Hogwarts", "Unknown", "") else '')
    location_clause = f" in {location_name}" if location_name else ""

    # === HEADER: Speaking with ===
    header_parts = []
    if observer_mode:
        # In observer mode (director prompt), NPCs are speaking to each other
        # Participants list contains the other conversation participants
        speaking_with = _format_participant_list(participants)
        if speaking_with:
            header_parts.append(f"{speaker_subject} are currently{location_clause}, in a conversation with {speaking_with}.")
        # No player visibility/status info in observer mode
    else:
        speaking_with = _format_participant_list(participants)
        if speaking_with:
            header_parts.append(f"{speaker_subject} are currently{location_clause}, speaking with {speaking_with}.")
        elif player_name and player_name != "Unknown":
            player_desc = f"{speaker_subject} are currently{location_clause}, speaking with {player_name}, a {player_house} student"
            status_parts = []
            if context.get('inCombat'):
                status_parts.append("currently in combat")
            if context.get('isOnMount'):
                mount_type = context.get('mountType', 'broom')
                if mount_type == 'broom':
                    status_parts.append("flying on a broom")
                elif mount_type == 'hippogriff':
                    status_parts.append("riding a hippogriff")
                elif mount_type == 'graphorn':
                    status_parts.append("riding a graphorn")
                else:
                    status_parts.append(f"riding a {mount_type}")
            if context.get('isSwimming'):
                status_parts.append("swimming")
            if context.get('hoodUp'):
                status_parts.append("with their hood up")
            if status_parts:
                player_desc += f" who is {' and '.join(status_parts)}"
            header_parts.append(player_desc + ".")

    # Visibility status (skip in observer mode)
    if not observer_mode:
        if in_stealth:
            header_parts.append(f"{player_name} has the Disillusionment charm active (invisible/hard to see).")
        else:
            header_parts.append(f"{player_name} is visible (no Disillusionment charm).")

    # Companion status (skip in observer mode unless companion is a participant)
    if context.get('hasCompanion') and not observer_mode:
        companion_id = context.get('companionId', '')
        companion_name = get_display_name(companion_id) if companion_id else 'companion'
        companion_status = "invisible (Disillusionment charm)" if in_stealth else "visible"
        if context.get('companionIsSwimming'):
            companion_status += " and swimming"
        if context.get('companionIsOnBroom'):
            companion_status += " and flying on a broom"
        # Rephrase to second person if prompting the companion themselves
        if current_speaker and companion_id and current_speaker == companion_id:
            header_parts.append(f"You are {companion_status}, accompanying {player_name}.")
        else:
            header_parts.append(f"{companion_name} is accompanying {player_name} and is {companion_status}.")

    # Follower status - tell the current speaker if they are following the player
    if not observer_mode and current_speaker:
        followers = context.get('followers', [])
        if current_speaker.lower() in [f.lower() for f in followers]:
            header_parts.append(f"You are currently following {player_name} on their adventures.")

    # === PLAYER ATTIRE (if enabled, skip in observer mode) ===
    attire_section = ""
    gear_context_enabled = conv_settings.get('gear_context', True)
    player_gear = context.get('playerGear', '')
    if player_gear and gear_context_enabled and not observer_mode:
        attire_section = f"\n\n**{player_name}'s attire:**\n{player_gear}"
        attire_section += f"\n**Note:** Don't comment on {player_name}'s attire unless directly relevant to the conversation."

    # === PLAYER FOCUS (for companions, skip in observer mode) ===
    focus_section = ""
    mission_context_enabled = conv_settings.get('mission_context', True)
    companion_id = context.get('companionId', '')
    if mission_context_enabled and current_speaker and companion_id and not observer_mode:
        if current_speaker == companion_id:
            current_quest = context.get('currentQuest', '')
            quest_objective = context.get('questObjective', '')
            if current_quest or quest_objective:
                focus_parts = []
                if current_quest:
                    focus_parts.append(f"Quest: {current_quest}")
                if quest_objective:
                    focus_parts.append(f"Their goal: {quest_objective}")
                focus_section = f"\n\n**{player_name}'s current focus:**\n" + "\n".join(focus_parts)
                focus_section += f"\n(This is just for your awareness as {player_name}'s companion. Don't push them to pursue it - they'll get to it when they're ready. You may reference it naturally if it comes up.)"

    # === DATE/TIME/LOCATION ===
    scene_parts = []
    date_formatted = context.get('dateFormatted', '')
    time_formatted = context.get('timeFormatted', '')
    time_period = context.get('timePeriod', 'Day')

    if date_formatted:
        scene_parts.append(f"**Date:** {date_formatted}")

    if time_formatted:
        time_desc = {
            'Night': 'nighttime', 'Dawn': 'early morning', 'Morning': 'morning',
            'Noon': 'midday', 'Afternoon': 'afternoon', 'Evening': 'evening'
        }.get(time_period, '')
        scene_parts.append(f"**Time:** {time_formatted}" + (f" ({time_desc})" if time_desc else ""))

    # === SCHEDULE CONTEXT (if NPC Schedule Enhanced mod enabled) ===
    try:
        from . import mods
        npc_sched_settings = settings.get('game_mods', {}).get('npc_schedule', {})
        sched_context_enabled = npc_sched_settings.get('context_enabled', True)
        sched_mod_installed = mods.is_mod_installed('npc_schedule')
        if current_speaker and sched_context_enabled and sched_mod_installed:
            schedule_line = build_schedule_context(current_speaker, context)
            if schedule_line:
                scene_parts.append(f"**Schedule:** {schedule_line}")
    except Exception as e:
        print(f"[Context] Schedule context error: {e}")

    if location_name:
        scene_parts.append(f"**Your current location:** {location_name}")

    # === NEARBY CHARACTERS ===
    nearby = context.get('nearbyNpcs', [])
    nearby_parts = []

    if current_speaker_name:
        nearby_parts.append(f"- {current_speaker_name} (You)")

    # In observer mode, the player is not part of this exchange.
    if player_name and player_name != "Unknown" and not observer_mode:
        nearby_parts.append(f"- {player_name} (you are responding to them)")

    companion_id = context.get('companionId', '')
    followers = [f.lower() for f in context.get('followers', [])]
    for char in nearby:
        npc_id = char.get('name', 'Unknown')
        if current_speaker and npc_id.lower() == current_speaker.lower():
            continue
        distance_m = round(char.get('distance', 0) / 100)
        npc_name = get_display_name(npc_id)
        guidance = get_editor_guidance(npc_id=npc_id, display_name=npc_name, settings=settings)
        is_companion = companion_id and npc_id.lower() == companion_id.lower() and player_name
        is_follower = npc_id.lower() in followers
        if is_companion:
            tag = f"{player_name}'s companion"
        elif is_follower:
            tag = f"following {player_name}"
        else:
            tag = f"~{distance_m}m away"
        if guidance:
            nearby_parts.append(f"- {npc_name} ({tag}): {guidance}")
        else:
            nearby_parts.append(f"- {npc_name} ({tag})")

    # === VISION CONTEXT ===
    vision_section = ""
    try:
        from vision_agent import get_agent
        agent = get_agent()
        vision_ctx = agent.get_current_context() if agent else None
        if vision_ctx:
            age = time.time() - vision_ctx.get('timestamp', 0)
            # Distance check: use cached vision if player is within 5m (500 UE units)
            ctx_pos = vision_ctx.get('position', {})
            px, py, pz = context.get('x'), context.get('y'), context.get('z')
            if px is not None and ctx_pos:
                dx = px - ctx_pos.get('x', 0)
                dy = py - ctx_pos.get('y', 0)
                dz = pz - ctx_pos.get('z', 0)
                dist = (dx*dx + dy*dy + dz*dz) ** 0.5
                nearby = dist < 500  # 5m in UE units
            else:
                nearby = False
                dist = -1
            if age > 300 and not nearby:
                print(f"[Context] Vision cache expired (age={age:.0f}s, dist={dist:.0f})")
                vision_ctx = None
            elif not nearby:
                # Far from capture point — fall back to zone/region match
                if zone or region:
                    ctx_zone = vision_ctx.get('zoneLocation', '')
                    ctx_region = vision_ctx.get('location', '')
                    if zone and ctx_zone and zone.lower() != ctx_zone.lower():
                        print(f"[Context] Vision cache zone mismatch ({ctx_zone} vs {zone}, dist={dist:.0f})")
                        vision_ctx = None
                    elif not zone and region and ctx_region and region.lower() != ctx_region.lower():
                        print(f"[Context] Vision cache region mismatch ({ctx_region} vs {region})")
                        vision_ctx = None
        else:
            if agent:
                print(f"[Context] No vision cache available")
        if vision_ctx:
            vision_parts = []
            if vision_ctx.get('scene'):
                vision_parts.append(f"**Scene:** {vision_ctx['scene']}")
            if vision_ctx.get('notable'):
                vision_parts.append(f"**Notable details:** {vision_ctx['notable']}")
            if vision_ctx.get('player'):
                vision_parts.append(f"**{player_name}:** {vision_ctx['player']}")
            if vision_ctx.get('atmosphere'):
                vision_parts.append(f"**Atmosphere:** {vision_ctx['atmosphere']}")
            if vision_ctx.get('characters'):
                vision_parts.append(f"**Visible:** {vision_ctx['characters']}")
            if vision_parts:
                vision_section = "\n".join(vision_parts)
            elif vision_ctx.get('description'):
                vision_section = vision_ctx['description']
            print(f"[Context] Using cached vision ({len(vision_section)} chars, age={time.time() - vision_ctx.get('timestamp', 0):.0f}s)")

        # If capture is still in progress, append partial streaming description
        if agent and agent._capture_in_progress:
            partial = agent.get_partial_description()
            if partial:
                partial_trimmed = partial.rstrip()
                if partial_trimmed:
                    if vision_section:
                        vision_section = f"{vision_section}\n\n(Updating...)\n{partial_trimmed}..."
                    else:
                        vision_section = f"{partial_trimmed}..."
                    print(f"[Context] Using partial vision ({len(partial_trimmed)} chars)")
            else:
                print(f"[Context] Vision capture in progress but no chunks yet")
    except Exception as e:
        print(f"[Context] Vision context error: {e}")

    # === LANDMARKS ===
    landmark_section = ""
    try:
        beacons = get_landmark_beacons()
        if zone and beacons:
            zone_lower = zone.lower()
            beacons = [b for b in beacons if zone_lower not in b['name'].lower()
                      and b['name'].lower() not in zone_lower]
        beacon_str = format_beacons_for_llm(beacons)
        if beacon_str:
            landmark_section = beacon_str
    except Exception as e:
        print(f"[Context] Error getting beacons: {e}")

    # === HOUSE POINTS (if mod enabled) ===
    house_points_section = ""
    try:
        from . import mods
        hp_settings = settings.get('game_mods', {}).get('house_points', {})
        context_enabled = hp_settings.get('context_enabled', True)
        mod_installed = mods.is_mod_installed('house_points')
        print(f"[Context] House points check: context_enabled={context_enabled}, mod_installed={mod_installed}")
        if context_enabled and mod_installed:
            hp_live = mods.get_live_data('house_points')
            print(f"[Context] House points raw: {hp_live}")
            hp_points = hp_live.get('points', {})
            print(f"[Context] House points data: {bool(hp_points)} keys={list(hp_points.keys()) if hp_points else []}")
            if hp_points:
                # Determine current season from game date
                season_name = ""
                month = int(context.get('month', 0) or 0)
                if month:
                    # Spring: Feb-Apr (2-4), Summer: May-Jul (5-7)
                    # Autumn: Aug-Oct (8-10), Winter: Nov-Jan (11,12,1)
                    if month in (2, 3, 4):
                        season_name = "Spring"
                    elif month in (5, 6, 7):
                        season_name = "Summer"
                    elif month in (8, 9, 10):
                        season_name = "Autumn"
                    elif month in (11, 12, 1):
                        season_name = "Winter"

                # Build markdown table with all time periods
                table_lines = ["**House Point Standings:**"]
                if season_name:
                    table_lines.append(f"Current season: {season_name}")
                table_lines.extend([
                    "| House      | Season | Month | Week | Day |",
                    "|------------|--------|-------|------|-----|"
                ])
                for house in ["Gryffindor", "Slytherin", "Hufflepuff", "Ravenclaw"]:
                    if house in hp_points:
                        p = hp_points[house]
                        table_lines.append(
                            f"| {house:10} | {p.get('season', 0):6} | {p.get('month', 0):5} | {p.get('week', 0):4} | {p.get('day', 0):3} |"
                        )
                house_points_section = "\n".join(table_lines)
    except Exception as e:
        print(f"[Context] Error getting house points: {e}")

    # === BUILD FINAL OUTPUT ===
    output = []

    # Header (speaking with, visibility)
    output.append(" ".join(header_parts))

    # Attire and focus (inline with header area)
    if attire_section:
        output.append(attire_section)
    if focus_section:
        output.append(focus_section)

    # Scene info
    if scene_parts:
        output.append("\n\n" + "\n".join(scene_parts))

    # Nearby characters
    if nearby_parts:
        output.append("\n\n**Nearby characters:**\n" + "\n".join(nearby_parts))

    # Vision
    if vision_section:
        output.append("\n\n" + _format_vision_section(vision_section))

    # Landmarks
    if landmark_section:
        output.append("\n\n" + landmark_section)

    # House Points (game mod)
    if house_points_section:
        output.append("\n\n" + house_points_section)

    # NPC-specific story milestones (from mission completion)
    if current_speaker:
        try:
            from .world_facts import get_npc_facts
            npc_story = get_npc_facts(
                context.get('missionStatuses'),
                current_speaker,
                player_name=context.get('playerName'),
            )
            if npc_story:
                output.append("\n\n" + npc_story)
        except Exception as e:
            print(f"[WorldFacts] Error getting NPC facts: {e}")

    # Commitments (only when enabled)
    if current_speaker:
        try:
            if load_settings().get('commitments', {}).get('enabled', False):
                from .commitments import build_commitment_context
                commitment_section = build_commitment_context(current_speaker, player_name=context.get('playerName'))
                if commitment_section:
                    output.append("\n\n" + commitment_section)
        except Exception as e:
            print(f"[GameContext] Error building commitment context: {e}")

    # Mail and board context (if speaker specified)
    if current_speaker:
        mail_ctx = _get_mail_context(current_speaker, game_context=context, player_name=player_name)
        if mail_ctx:
            output.append(mail_ctx)
        board_ctx = _get_board_context(current_speaker)
        if board_ctx:
            output.append(board_ctx)

    if not output:
        return ""

    return "## Current Situation\n" + "".join(output)


def format_static_context(context, current_speaker=None, observer_mode=False):
    """Format the stable (cacheable) portions of game context.

    Returns content suitable for a system message that doesn't change every turn:
    player attire, quest focus, house points, NPC story milestones, commitments,
    full mail content, and notice boards.
    """
    if not context:
        return ""

    player_name = context.get('playerName', 'Unknown')

    settings = load_settings()
    conv_settings = settings.get('conversation', {})

    output = []

    # === PLAYER ATTIRE (if enabled, skip in observer mode) ===
    gear_context_enabled = conv_settings.get('gear_context', True)
    player_gear = context.get('playerGear', '')
    if player_gear and gear_context_enabled and not observer_mode:
        attire_section = f"**{player_name}'s attire:**\n{player_gear}"
        attire_section += f"\n**Note:** Don't comment on {player_name}'s attire unless directly relevant to the conversation."
        output.append(attire_section)

    # === PLAYER FOCUS (for companions, skip in observer mode) ===
    mission_context_enabled = conv_settings.get('mission_context', True)
    companion_id = context.get('companionId', '')
    if mission_context_enabled and current_speaker and companion_id and not observer_mode:
        if current_speaker == companion_id:
            current_quest = context.get('currentQuest', '')
            quest_objective = context.get('questObjective', '')
            if current_quest or quest_objective:
                focus_parts = []
                if current_quest:
                    focus_parts.append(f"Quest: {current_quest}")
                if quest_objective:
                    focus_parts.append(f"Their goal: {quest_objective}")
                focus_section = f"**{player_name}'s current focus:**\n" + "\n".join(focus_parts)
                focus_section += f"\n(This is just for your awareness as {player_name}'s companion. Don't push them to pursue it - they'll get to it when they're ready. You may reference it naturally if it comes up.)"
                output.append(focus_section)

    # === HOUSE POINTS (if mod enabled) ===
    try:
        from . import mods
        hp_settings = settings.get('game_mods', {}).get('house_points', {})
        context_enabled = hp_settings.get('context_enabled', True)
        mod_installed = mods.is_mod_installed('house_points')
        if context_enabled and mod_installed:
            hp_live = mods.get_live_data('house_points')
            hp_points = hp_live.get('points', {})
            if hp_points:
                # Determine current season from game date
                season_name = ""
                month = int(context.get('month', 0) or 0)
                if month:
                    if month in (2, 3, 4):
                        season_name = "Spring"
                    elif month in (5, 6, 7):
                        season_name = "Summer"
                    elif month in (8, 9, 10):
                        season_name = "Autumn"
                    elif month in (11, 12, 1):
                        season_name = "Winter"

                table_lines = ["**House Point Standings:**"]
                if season_name:
                    table_lines.append(f"Current season: {season_name}")
                table_lines.extend([
                    "| House      | Season | Month | Week | Day |",
                    "|------------|--------|-------|------|-----|"
                ])
                for house in ["Gryffindor", "Slytherin", "Hufflepuff", "Ravenclaw"]:
                    if house in hp_points:
                        p = hp_points[house]
                        table_lines.append(
                            f"| {house:10} | {p.get('season', 0):6} | {p.get('month', 0):5} | {p.get('week', 0):4} | {p.get('day', 0):3} |"
                        )
                output.append("\n".join(table_lines))
    except Exception as e:
        print(f"[Context] Error getting house points: {e}")

    # === NPC STORY MILESTONES ===
    if current_speaker:
        try:
            from .world_facts import get_npc_facts
            npc_story = get_npc_facts(
                context.get('missionStatuses'),
                current_speaker,
                player_name=context.get('playerName'),
            )
            if npc_story:
                output.append(npc_story)
        except Exception as e:
            print(f"[WorldFacts] Error getting NPC facts: {e}")

    # === COMMITMENTS ===
    if current_speaker:
        try:
            if load_settings().get('commitments', {}).get('enabled', False):
                from .commitments import build_commitment_context
                commitment_section = build_commitment_context(current_speaker, player_name=context.get('playerName'))
                if commitment_section:
                    output.append(commitment_section)
        except Exception as e:
            print(f"[GameContext] Error building commitment context: {e}")

    # === MAIL (full content) ===
    if current_speaker:
        mail_ctx = _get_mail_context(current_speaker, game_context=context, player_name=player_name)
        if mail_ctx:
            output.append(mail_ctx.strip())

    # === NOTICE BOARDS ===
    if current_speaker:
        board_ctx = _get_board_context(current_speaker)
        if board_ctx:
            output.append(board_ctx.strip())

    if not output:
        return ""

    return "\n\n".join(output)


def format_dynamic_context(context, current_speaker=None, participants=None,
                            observer_mode=False, event_entries=None):
    """Format the volatile (per-turn) portions of game context.

    Returns content that changes every turn: date/time, location, schedule,
    situation header, visibility/stealth/companion/follower status, nearby
    characters, vision, landmarks, letter recency references, and recent events.
    """
    if not context:
        return ""

    player_name = context.get('playerName', 'Unknown')
    player_house = context.get('playerHouse', 'Unknown')
    in_stealth = context.get('inStealth', False)
    current_speaker_name = _get_current_speaker_name(current_speaker)
    speaker_subject = f"You, {current_speaker_name}," if current_speaker_name else "You"

    settings = load_settings()

    # === RESOLVE LOCATION (needed for header + scene) ===
    zone = context.get('zoneLocation', '')
    region = context.get('location', '')
    location_name = zone if zone else (region.replace('_', ' ') if region and region not in ("Hogwarts", "Unknown", "") else '')
    location_clause = f" in {location_name}" if location_name else ""

    output = []

    # === HEADER: Speaking with ===
    header_parts = []
    if observer_mode:
        speaking_with = _format_participant_list(participants)
        if speaking_with:
            header_parts.append(f"{speaker_subject} are currently{location_clause}, in a conversation with {speaking_with}.")
    else:
        speaking_with = _format_participant_list(participants)
        if speaking_with:
            header_parts.append(f"{speaker_subject} are currently{location_clause}, speaking with {speaking_with}.")
        elif player_name and player_name != "Unknown":
            player_desc = f"{speaker_subject} are currently{location_clause}, speaking with {player_name}, a {player_house} student"
            status_parts = []
            if context.get('inCombat'):
                status_parts.append("currently in combat")
            if context.get('isOnMount'):
                mount_type = context.get('mountType', 'broom')
                if mount_type == 'broom':
                    status_parts.append("flying on a broom")
                elif mount_type == 'hippogriff':
                    status_parts.append("riding a hippogriff")
                elif mount_type == 'graphorn':
                    status_parts.append("riding a graphorn")
                else:
                    status_parts.append(f"riding a {mount_type}")
            if context.get('isSwimming'):
                status_parts.append("swimming")
            if context.get('hoodUp'):
                status_parts.append("with their hood up")
            if status_parts:
                player_desc += f" who is {' and '.join(status_parts)}"
            header_parts.append(player_desc + ".")

    # Visibility status (skip in observer mode)
    if not observer_mode:
        if in_stealth:
            header_parts.append(f"{player_name} has the Disillusionment charm active (invisible/hard to see).")
        else:
            header_parts.append(f"{player_name} is visible (no Disillusionment charm).")

    # Companion status (skip in observer mode unless companion is a participant)
    if context.get('hasCompanion') and not observer_mode:
        companion_id = context.get('companionId', '')
        companion_name = get_display_name(companion_id) if companion_id else 'companion'
        companion_status = "invisible (Disillusionment charm)" if in_stealth else "visible"
        if context.get('companionIsSwimming'):
            companion_status += " and swimming"
        if context.get('companionIsOnBroom'):
            companion_status += " and flying on a broom"
        if current_speaker and companion_id and current_speaker == companion_id:
            header_parts.append(f"You are {companion_status}, accompanying {player_name}.")
        else:
            header_parts.append(f"{companion_name} is accompanying {player_name} and is {companion_status}.")

    # Follower status
    if not observer_mode and current_speaker:
        followers = context.get('followers', [])
        if current_speaker.lower() in [f.lower() for f in followers]:
            header_parts.append(f"You are currently following {player_name} on their adventures.")

    if header_parts:
        output.append(" ".join(header_parts))

    # === DATE/TIME/LOCATION ===
    scene_parts = []
    date_formatted = context.get('dateFormatted', '')
    time_formatted = context.get('timeFormatted', '')
    time_period = context.get('timePeriod', 'Day')

    if date_formatted:
        scene_parts.append(f"**Date:** {date_formatted}")

    if time_formatted:
        time_desc = {
            'Night': 'nighttime', 'Dawn': 'early morning', 'Morning': 'morning',
            'Noon': 'midday', 'Afternoon': 'afternoon', 'Evening': 'evening'
        }.get(time_period, '')
        scene_parts.append(f"**Time:** {time_formatted}" + (f" ({time_desc})" if time_desc else ""))

    # === SCHEDULE CONTEXT (if NPC Schedule Enhanced mod enabled) ===
    try:
        from . import mods
        npc_sched_settings = settings.get('game_mods', {}).get('npc_schedule', {})
        sched_context_enabled = npc_sched_settings.get('context_enabled', True)
        sched_mod_installed = mods.is_mod_installed('npc_schedule')
        if current_speaker and sched_context_enabled and sched_mod_installed:
            schedule_line = build_schedule_context(current_speaker, context)
            if schedule_line:
                scene_parts.append(f"**Schedule:** {schedule_line}")
    except Exception as e:
        print(f"[Context] Schedule context error: {e}")

    if location_name:
        scene_parts.append(f"**Your current location:** {location_name}")

    if scene_parts:
        output.append("\n".join(scene_parts))

    # === NEARBY CHARACTERS ===
    nearby = context.get('nearbyNpcs', [])
    nearby_parts = []

    if current_speaker_name:
        nearby_parts.append(f"- {current_speaker_name} (You)")

    if player_name and player_name != "Unknown" and not observer_mode:
        nearby_parts.append(f"- {player_name} (you are responding to them)")

    companion_id = context.get('companionId', '')
    followers = [f.lower() for f in context.get('followers', [])]
    for char in nearby:
        npc_id = char.get('name', 'Unknown')
        if current_speaker and npc_id.lower() == current_speaker.lower():
            continue
        distance_m = round(char.get('distance', 0) / 100)
        npc_name = get_display_name(npc_id)
        guidance = get_editor_guidance(npc_id=npc_id, display_name=npc_name, settings=settings)
        is_companion = companion_id and npc_id.lower() == companion_id.lower() and player_name
        is_follower = npc_id.lower() in followers
        if is_companion:
            tag = f"{player_name}'s companion"
        elif is_follower:
            tag = f"following {player_name}"
        else:
            tag = f"~{distance_m}m away"
        if guidance:
            nearby_parts.append(f"- {npc_name} ({tag}): {guidance}")
        else:
            nearby_parts.append(f"- {npc_name} ({tag})")

    if nearby_parts:
        output.append("**Nearby characters:**\n" + "\n".join(nearby_parts))

    # === VISION CONTEXT ===
    vision_section = ""
    try:
        from vision_agent import get_agent
        agent = get_agent()
        vision_ctx = agent.get_current_context() if agent else None
        if vision_ctx:
            age = time.time() - vision_ctx.get('timestamp', 0)
            ctx_pos = vision_ctx.get('position', {})
            px, py, pz = context.get('x'), context.get('y'), context.get('z')
            if px is not None and ctx_pos:
                dx = px - ctx_pos.get('x', 0)
                dy = py - ctx_pos.get('y', 0)
                dz = pz - ctx_pos.get('z', 0)
                dist = (dx*dx + dy*dy + dz*dz) ** 0.5
                nearby_vision = dist < 500
            else:
                nearby_vision = False
                dist = -1
            if age > 300 and not nearby_vision:
                print(f"[Context] Vision cache expired (age={age:.0f}s, dist={dist:.0f})")
                vision_ctx = None
            elif not nearby_vision:
                if zone or region:
                    ctx_zone = vision_ctx.get('zoneLocation', '')
                    ctx_region = vision_ctx.get('location', '')
                    if zone and ctx_zone and zone.lower() != ctx_zone.lower():
                        print(f"[Context] Vision cache zone mismatch ({ctx_zone} vs {zone}, dist={dist:.0f})")
                        vision_ctx = None
                    elif not zone and region and ctx_region and region.lower() != ctx_region.lower():
                        print(f"[Context] Vision cache region mismatch ({ctx_region} vs {region})")
                        vision_ctx = None
        else:
            if agent:
                print(f"[Context] No vision cache available")
        if vision_ctx:
            vision_parts = []
            if vision_ctx.get('scene'):
                vision_parts.append(f"**Scene:** {vision_ctx['scene']}")
            if vision_ctx.get('notable'):
                vision_parts.append(f"**Notable details:** {vision_ctx['notable']}")
            if vision_ctx.get('player'):
                vision_parts.append(f"**{player_name}:** {vision_ctx['player']}")
            if vision_ctx.get('atmosphere'):
                vision_parts.append(f"**Atmosphere:** {vision_ctx['atmosphere']}")
            if vision_ctx.get('characters'):
                vision_parts.append(f"**Visible:** {vision_ctx['characters']}")
            if vision_parts:
                vision_section = "\n".join(vision_parts)
            elif vision_ctx.get('description'):
                vision_section = vision_ctx['description']
            print(f"[Context] Using cached vision ({len(vision_section)} chars, age={time.time() - vision_ctx.get('timestamp', 0):.0f}s)")

        if agent and agent._capture_in_progress:
            partial = agent.get_partial_description()
            if partial:
                partial_trimmed = partial.rstrip()
                if partial_trimmed:
                    if vision_section:
                        vision_section = f"{vision_section}\n\n(Updating...)\n{partial_trimmed}..."
                    else:
                        vision_section = f"{partial_trimmed}..."
                    print(f"[Context] Using partial vision ({len(partial_trimmed)} chars)")
            else:
                print(f"[Context] Vision capture in progress but no chunks yet")
    except Exception as e:
        print(f"[Context] Vision context error: {e}")

    if vision_section:
        output.append(_format_vision_section(vision_section))

    # === LANDMARKS ===
    landmark_section = ""
    try:
        beacons = get_landmark_beacons()
        if zone and beacons:
            zone_lower = zone.lower()
            beacons = [b for b in beacons if zone_lower not in b['name'].lower()
                      and b['name'].lower() not in zone_lower]
        beacon_str = format_beacons_for_llm(beacons)
        if beacon_str:
            landmark_section = beacon_str
    except Exception as e:
        print(f"[Context] Error getting beacons: {e}")

    if landmark_section:
        output.append(landmark_section)

    # === LETTER RECENCY REFERENCES ===
    if current_speaker:
        mail_refs = _get_mail_recency_references(current_speaker, game_context=context, player_name=player_name)
        if mail_refs:
            output.append(mail_refs)

    # === RECENT EVENTS (non-dialogue history entries) ===
    # Structural events (location, spell, combat, etc.) capped to 30.
    # Ambient NPC chatter capped to 4 most recent — only useful if very recent.
    _EVENT_TYPES = {'location', 'broom', 'mount', 'spell', 'combat', 'commitment', 'mail', 'prompt'}
    if event_entries:
        narration_enabled = settings.get('conversation', {}).get('narration_enabled', False)
        collapsed_events = collapse_consecutive_events(event_entries)
        structural = []
        ambient = []
        for entry in collapsed_events:
            if (entry.get('type') or '') in _EVENT_TYPES:
                structural.append(entry)
            else:
                ambient.append(entry)
        kept_structural = set(id(e) for e in structural[-30:])
        kept_ambient = set(id(e) for e in ambient[-4:])
        kept = kept_structural | kept_ambient
        selected_events = [entry for entry in collapsed_events if id(entry) in kept]
        selected_events = collapse_consecutive_events(selected_events)
        event_lines = []
        for entry in selected_events:
            line = format_dialogue_entry(entry, include_time=True, mark_player=False)
            if line and not narration_enabled:
                line = _clean_history_line_for_prompt(line)
            if line:
                event_lines.append(line)
        if event_lines:
            output.append("**Recent events:**\n" + "\n".join(event_lines))

    if not output:
        return ""

    return "\n\n".join(output)
