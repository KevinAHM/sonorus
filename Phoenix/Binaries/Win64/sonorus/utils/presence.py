"""
Presence tracking: turns Lua presence_update messages into flesh intervals
and merges flesh + schedule projection for reads.
"""

import threading

from . import ledger_db
from . import location_names
from . import player_context
from . import schedule_characters
from . import schedule_projection
from .dialogue_db import _game_datetime_to_minutes, _parse_game_date, _parse_game_time


def _message_minutes(msg):
    game_date = _parse_game_date(msg.get("gameDate") or "")
    game_time = _parse_game_time(msg.get("gameTime") or "")
    if game_date is None or game_time is None:
        return None
    return _game_datetime_to_minutes(game_date, game_time)


class PresenceTracker:
    """Applies enter/update/leave changes. One instance per server process."""

    def __init__(self):
        self._lock = threading.Lock()
        self._open = {}
        self._last_minutes = None
        try:
            stale = ledger_db.close_all_open(0)
            if stale:
                print(f"[Presence] Closed {stale} stale open interval(s) from previous session")
        except Exception as exc:
            print(f"[Presence] Stale cleanup failed: {exc}")

    def handle_update(self, msg):
        minutes = _message_minutes(msg)
        if minutes is None:
            return
        with self._lock:
            if self._last_minutes is not None and minutes < self._last_minutes:
                # Save reloads can rewind game time. End future open observations
                # at their own starts, then begin a fresh timeline at the new time.
                ledger_db.close_all_open(minutes)
                self._open.clear()
            self._last_minutes = minutes
            for change in msg.get("changes") or []:
                npc_id = change.get("id")
                if not npc_id:
                    continue
                event = change.get("ev")
                if event == "leave":
                    self._close(npc_id, minutes)
                elif event in ("enter", "update"):
                    self._close(npc_id, minutes)
                    self._open_interval(npc_id, minutes, change)

    def _close(self, npc_id, minutes):
        interval_id = self._open.pop(npc_id, None)
        if interval_id is None:
            row = ledger_db.get_open_interval(npc_id)
            interval_id = row["id"] if row else None
        if interval_id is not None:
            ledger_db.close_interval(interval_id, minutes)

    def _open_interval(self, npc_id, minutes, change):
        x, y, z = change.get("x"), change.get("y"), change.get("z")
        loc_name, loc_id = None, None
        if x is not None and y is not None and z is not None:
            try:
                loc_name, loc_id = location_names.resolve_position(x, y, z)
            except Exception:
                pass
        self._open[npc_id] = ledger_db.open_interval(
            npc_id,
            minutes,
            source="flesh",
            location_id=loc_id,
            location_name=loc_name,
            near_player=bool(change.get("near")),
            eyeshot=bool(change.get("eyes")),
            x=x,
            y=y,
            z=z,
        )

    def shutdown(self):
        with self._lock:
            if self._last_minutes is not None:
                ledger_db.close_all_open(self._last_minutes)
            self._open.clear()


_tracker = None
_tracker_lock = threading.Lock()


def get_tracker():
    global _tracker
    with _tracker_lock:
        if _tracker is None:
            _tracker = PresenceTracker()
        return _tracker


def _close_tracker_for_switch():
    with _tracker_lock:
        if _tracker is not None:
            _tracker.shutdown()


def _reinit_tracker_for_switch(_new_data_dir=None):
    global _tracker
    with _tracker_lock:
        # ledger_db is registered first and may have been reopened by the flush
        # above, so close it once more after the player directory changes.
        ledger_db.close_all()
        _tracker = None


def get_presence(npc_id, t0_minutes, t1_minutes, day_of_week, character_id=None,
                 project_span_fn=None, resolve_location_fn=None):
    """Merge flesh and projected presence; day_of_week is the day containing t0."""
    project_span_fn = project_span_fn or schedule_projection.project_span
    resolve_location_fn = resolve_location_fn or location_names.resolve_location_id
    character_id = character_id or schedule_characters.get_character_id(npc_id)

    flesh = []
    for row in ledger_db.get_intervals(npc_id, t0_minutes, t1_minutes):
        if row["source"] != "flesh":
            continue
        start = max(row["game_minutes_start"], t0_minutes)
        end = min(
            row["game_minutes_end"] if row["game_minutes_end"] is not None else t1_minutes,
            t1_minutes,
        )
        if end > start:
            flesh.append({
                "start_minutes": start,
                "end_minutes": end,
                "location_name": row["location_name"],
                "source": "flesh",
                "near_player": bool(row["near_player"]),
                "eyeshot": bool(row["eyeshot"]),
            })
    flesh.sort(key=lambda segment: segment["start_minutes"])

    segments = []
    cursor = t0_minutes
    query_day_number = t0_minutes // (24 * 60)

    def fill_gap(gap_start, gap_end):
        if gap_end <= gap_start or not character_id:
            return
        gap_cursor = gap_start
        while gap_cursor < gap_end:
            day_number = gap_cursor // (24 * 60)
            day_base = day_number * 24 * 60
            local_start = gap_cursor - day_base
            local_end = min(24 * 60, gap_end - day_base)
            projected_day = (day_of_week + day_number - query_day_number) % 7
            for segment in project_span_fn(
                    character_id, projected_day, local_start, local_end):
                segments.append({
                    "start_minutes": day_base + segment["start_minutes"],
                    "end_minutes": day_base + segment["end_minutes"],
                    "location_id": segment.get("location_id"),
                    "location_name": resolve_location_fn(segment.get("location_id")),
                    "source": "projected",
                    "near_player": False,
                    "eyeshot": False,
                    "specificity": segment.get("specificity", "exact"),
                })
            gap_cursor = day_base + 24 * 60

    for segment in flesh:
        flesh_start = max(cursor, segment["start_minutes"])
        flesh_end = segment["end_minutes"]
        fill_gap(cursor, flesh_start)
        if flesh_end > flesh_start:
            segment = dict(segment)
            segment["start_minutes"] = flesh_start
            segments.append(segment)
            cursor = max(cursor, flesh_end)
    fill_gap(cursor, t1_minutes)
    ledger_db.replace_projected_intervals(
        npc_id,
        t0_minutes,
        t1_minutes,
        [segment for segment in segments if segment["source"] == "projected"],
    )
    return segments


player_context.register(
    name="presence_tracker",
    close_fn=_close_tracker_for_switch,
    reinit_fn=_reinit_tracker_for_switch,
)
