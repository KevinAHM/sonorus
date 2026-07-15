"""Read-only comparison of one live NPC sample against schedule projections."""

import math
import threading
from collections import Counter

from . import location_names
from . import schedule_projection


_STATUS_LABELS = {
    "exact_match": "EXACT",
    "acceptable_region_match": "REGION",
    "flesh_deviation": "DEVIATION",
    "projection_unknown": "NO PROJECTION",
    "observed_position_unknown": "NO POSITION",
    "scheduler_location": "SCHEDULED",
    "scheduler_locationless": "LOCATIONLESS",
    "scheduler_lookup_failed": "NO ENTITY",
    "flesh_cache_miss": "FLESH MISS",
    "invalid_observation": "INVALID",
}

_EVENT_LABELS = {
    "identity_appeared": "IDENTITY APPEARED",
    "identity_disappeared": "IDENTITY DISAPPEARED",
    "entered_flesh": "ENTERED FLESH",
    "left_flesh": "LEFT FLESH",
    "transit_started": "TRANSIT STARTED",
    "transit_ended": "TRANSIT ENDED",
    "became_offstage": "BECAME OFFSTAGE",
    "left_offstage": "LEFT OFFSTAGE",
    "state_changed": "STATE CHANGED",
    "location_changed": "LOCATION CHANGED",
    "activity_changed": "ACTIVITY CHANGED",
}

_snapshot_lock = threading.Lock()
_latest_snapshot = None
_snapshot_sequence = 0


def _valid_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _same_name(left, right):
    return bool(left and right and str(left).casefold() == str(right).casefold())


def _deduplicate_observations(observations):
    """Collapse case-only duplicate IDs, preferring flesh and canonical casing."""
    deduplicated = {}
    invalid = []
    for observation in observations:
        if not isinstance(observation, dict):
            invalid.append(observation)
            continue
        character_id = str(observation.get("id") or "").strip()
        if not character_id:
            invalid.append(observation)
            continue
        key = character_id.casefold()
        current = deduplicated.get(key)
        score = (observation.get("source") != "scheduler",
                 character_id != character_id.lower())
        if current is None:
            deduplicated[key] = observation
            continue
        current_id = str(current.get("id") or "")
        current_score = (current.get("source") != "scheduler",
                         current_id != current_id.lower())
        if score > current_score:
            deduplicated[key] = observation
    return list(deduplicated.values()) + invalid


def classify_observation(observation, day_of_week, minutes_of_day, *,
                         project_fn=schedule_projection.project,
                         resolve_position_fn=location_names.resolve_position,
                         resolve_location_fn=location_names.resolve_location,
                         position_matches_fn=location_names.position_matches_location):
    """Classify one observation without writing any ledger state."""
    if not isinstance(observation, dict):
        return {"character_id": "?", "status": "invalid_observation"}

    character_id = str(observation.get("id") or "").strip()
    if not character_id:
        return {"character_id": "?", "status": "invalid_observation"}

    source = observation.get("source") or "flesh"
    if source == "scheduler":
        location_id = observation.get("scheduleLocationId") or ""
        location_name = observation.get("scheduleLocationName") or ""
        if location_id and not location_name:
            resolved = resolve_location_fn(location_id)
            location_name = resolved.get("name") if resolved else ""

        result = {
            "character_id": character_id,
            "source": source,
            "in_flesh": observation.get("inFlesh") is True,
            "cache_miss": observation.get("cacheMiss") is True,
            "observed_name": location_name,
            "observed_location_id": location_id,
            "activity_id": observation.get("activity") or "",
            "activity_type": observation.get("activityType") or "",
            "is_in_transit": observation.get("isInTransit") is True,
        }
        if observation.get("scheduleLookupFailed") is True:
            result["status"] = "scheduler_lookup_failed"
        elif result["in_flesh"] and result["cache_miss"]:
            result["status"] = "flesh_cache_miss"
        elif location_id or location_name:
            result["status"] = "scheduler_location"
        else:
            result["status"] = "scheduler_locationless"
        return result

    coordinates = tuple(observation.get(axis) for axis in ("x", "y", "z"))
    if not all(_valid_number(value) for value in coordinates):
        return {"character_id": character_id, "status": "invalid_observation"}

    x, y, z = coordinates
    projection = project_fn(character_id, day_of_week, minutes_of_day)
    observed_name, observed_location_id = resolve_position_fn(x, y, z)
    result = {
        "character_id": character_id,
        "source": source,
        "actor_key": observation.get("actorKey") or "",
        "x": x,
        "y": y,
        "z": z,
        "observed_name": observed_name,
        "observed_location_id": observed_location_id,
    }

    if projection is None:
        result["status"] = "projection_unknown"
        return result

    projected_location = resolve_location_fn(projection.get("location_id"))
    projected_name = projected_location.get("name") if projected_location else None
    result.update({
        "projected_location_id": projection.get("location_id"),
        "projected_name": projected_name,
        "specificity": projection.get("specificity"),
        "activity_id": projection.get("activity_id"),
        "activity_type": projection.get("activity_type"),
    })

    same_location = observed_location_id == projection.get("location_id")
    if same_location or _same_name(observed_name, projected_name):
        result["status"] = ("acceptable_region_match"
                            if projection.get("specificity") == "region"
                            else "exact_match")
    elif (projection.get("specificity") == "region"
          and position_matches_fn(projection.get("location_id"), x, y, z)):
        result["status"] = "acceptable_region_match"
    elif not observed_name:
        result["status"] = "observed_position_unknown"
    else:
        result["status"] = "flesh_deviation"
    return result


def build_report(message, **classify_kwargs):
    """Build a socket response and printable details for one sample payload."""
    try:
        day_of_week = int(message.get("dayOfWeek"))
        minutes_of_day = int(message.get("minutesOfDay"))
    except (TypeError, ValueError):
        day_of_week = -1
        minutes_of_day = -1

    observations = message.get("observations")
    if not isinstance(observations, list):
        observations = []
    observations = _deduplicate_observations(observations)

    if not 0 <= day_of_week <= 6 or not 0 <= minutes_of_day < 24 * 60:
        return {
            "type": "presence_validation_result",
            "hint": "Presence validation: invalid game time",
            "counts": {},
            "results": [],
            "error": "invalid_game_time",
        }

    results = [
        classify_observation(item, day_of_week, minutes_of_day, **classify_kwargs)
        for item in observations
    ]
    counts = {status: 0 for status in _STATUS_LABELS}
    for result in results:
        counts[result["status"]] += 1

    hint = (
        f"Presence validation: {len(results)} NPCs\n"
        f"Exact {counts['exact_match']} | Region {counts['acceptable_region_match']} | "
        f"Deviated {counts['flesh_deviation']}\n"
        f"No projection {counts['projection_unknown']} | "
        f"No position {counts['observed_position_unknown']}\n"
        f"Scheduled {counts['scheduler_location']} | "
        f"Locationless {counts['scheduler_locationless']} | "
        f"Flesh miss {counts['flesh_cache_miss']} | "
        f"No entity {counts['scheduler_lookup_failed']} | "
        f"Invalid {counts['invalid_observation']}"
    )
    return {
        "type": "presence_validation_result",
        "hint": hint,
        "counts": counts,
        "results": results,
        "gameDate": message.get("gameDate") or "",
        "gameTime": message.get("gameTime") or "",
        "dayOfWeek": day_of_week,
        "minutesOfDay": minutes_of_day,
    }


def _snapshot_mode(result):
    status = result.get("status")
    if status == "invalid_observation":
        return "invalid"
    if result.get("source") != "scheduler":
        return "flesh"
    if status == "scheduler_location":
        return "transit" if result.get("is_in_transit") else "scheduled"
    if status == "scheduler_locationless":
        return "offstage"
    if status == "scheduler_lookup_failed":
        return "unmanaged"
    if status == "flesh_cache_miss":
        return "flesh_missing"
    return "unknown"


def _normalize_snapshot_state(result):
    location_name = result.get("observed_name") or ""
    location_id = result.get("observed_location_id") or ""
    return {
        "character_id": result.get("character_id") or "?",
        "source": result.get("source") or "unknown",
        "mode": _snapshot_mode(result),
        "status": result.get("status") or "unknown",
        "location_name": location_name,
        "location_id": location_id,
        "location_key": (location_name or location_id).casefold(),
        "activity_id": result.get("activity_id") or "",
        "activity_type": result.get("activity_type") or "",
    }


def build_snapshot(report):
    """Normalize a validation report into stable, coordinate-free NPC states."""
    states = {}
    for result in report.get("results") or []:
        state = _normalize_snapshot_state(result)
        states[state["character_id"].casefold()] = state
    return {
        "gameDate": report.get("gameDate") or "",
        "gameTime": report.get("gameTime") or "",
        "states": states,
    }


def _event(event_type, old_state, new_state):
    state = new_state or old_state or {}
    return {
        "type": event_type,
        "character_id": state.get("character_id") or "?",
        "old": old_state,
        "new": new_state,
    }


def diff_snapshots(previous, current):
    """Return meaningful semantic transitions between normalized snapshots."""
    old_states = previous.get("states") or {}
    new_states = current.get("states") or {}
    events = []

    for key in sorted(new_states.keys() - old_states.keys()):
        events.append(_event("identity_appeared", None, new_states[key]))
    for key in sorted(old_states.keys() - new_states.keys()):
        events.append(_event("identity_disappeared", old_states[key], None))

    for key in sorted(old_states.keys() & new_states.keys()):
        old = old_states[key]
        new = new_states[key]
        old_mode = old["mode"]
        new_mode = new["mode"]
        emitted_mode_event = False

        if (old_mode == "flesh") != (new_mode == "flesh"):
            event_type = "entered_flesh" if new_mode == "flesh" else "left_flesh"
            events.append(_event(event_type, old, new))
            emitted_mode_event = True
        if (old_mode == "transit") != (new_mode == "transit"):
            event_type = "transit_started" if new_mode == "transit" else "transit_ended"
            events.append(_event(event_type, old, new))
            emitted_mode_event = True
        if (old_mode == "offstage") != (new_mode == "offstage"):
            event_type = "became_offstage" if new_mode == "offstage" else "left_offstage"
            events.append(_event(event_type, old, new))
            emitted_mode_event = True
        if old_mode != new_mode and not emitted_mode_event:
            events.append(_event("state_changed", old, new))

        if old["location_key"] != new["location_key"] \
                and (old["location_key"] or new["location_key"]):
            events.append(_event("location_changed", old, new))

        if old["source"] == "scheduler" and new["source"] == "scheduler" \
                and old["activity_id"] and new["activity_id"] \
                and old["activity_id"] != new["activity_id"]:
            events.append(_event("activity_changed", old, new))

    events.sort(key=lambda item: (item["character_id"].casefold(), item["type"]))
    return events


def _snapshot_summary(previous, current, sequence):
    events = diff_snapshots(previous, current) if previous else []
    counts = Counter(event["type"] for event in events)
    return {
        "sequence": sequence,
        "baseline": previous is None,
        "identity_count": len(current["states"]),
        "previousGameDate": previous.get("gameDate", "") if previous else "",
        "previousGameTime": previous.get("gameTime", "") if previous else "",
        "gameDate": current.get("gameDate", ""),
        "gameTime": current.get("gameTime", ""),
        "event_count": len(events),
        "changed_characters": len({event["character_id"].casefold() for event in events}),
        "counts": dict(sorted(counts.items())),
        "events": events,
    }


def update_snapshot(report):
    """Store one in-memory diagnostic snapshot and diff it against the prior one."""
    global _latest_snapshot, _snapshot_sequence
    current = build_snapshot(report)
    with _snapshot_lock:
        previous = _latest_snapshot
        _snapshot_sequence += 1
        summary = _snapshot_summary(previous, current, _snapshot_sequence)
        _latest_snapshot = current
    return summary


def reset_snapshot_for_tests():
    global _latest_snapshot, _snapshot_sequence
    with _snapshot_lock:
        _latest_snapshot = None
        _snapshot_sequence = 0


def _describe_state(state):
    if not state:
        return "absent"
    location = state.get("location_name") or state.get("location_id") or "?"
    mode = state.get("mode") or "unknown"
    if mode == "transit":
        return f"transit to {location}"
    if mode == "offstage":
        return f"offstage ({state.get('activity_id') or '?'})"
    if mode == "scheduled":
        return f"scheduled at {location}"
    if mode == "flesh":
        return f"flesh at {location}"
    return mode


def _describe_event(event):
    old = event.get("old") or {}
    new = event.get("new") or {}
    if event["type"] == "activity_changed":
        return f"{old.get('activity_id') or '?'} -> {new.get('activity_id') or '?'}"
    if event["type"] == "location_changed":
        old_location = old.get("location_name") or old.get("location_id") or "?"
        new_location = new.get("location_name") or new.get("location_id") or "?"
        return f"{old_location} -> {new_location}"
    return f"{_describe_state(event.get('old'))} -> {_describe_state(event.get('new'))}"


def _snapshot_hint(summary):
    if summary["baseline"]:
        return f"Snapshot baseline stored: {summary['identity_count']} identities"
    counts = summary["counts"]
    if not summary["event_count"]:
        return "Snapshot transitions: none"
    return (
        f"Transitions {summary['event_count']} across {summary['changed_characters']} NPCs\n"
        f"Flesh +{counts.get('entered_flesh', 0)}/-{counts.get('left_flesh', 0)} | "
        f"Location {counts.get('location_changed', 0)} | "
        f"Transit +{counts.get('transit_started', 0)}/-{counts.get('transit_ended', 0)} | "
        f"Offstage +{counts.get('became_offstage', 0)}/-{counts.get('left_offstage', 0)} | "
        f"Activity {counts.get('activity_changed', 0)}"
    )


def handle_sample(message):
    """Log a detailed report and return its concise in-game response."""
    report = build_report(message)
    print(
        f"[PresenceValidation] {report.get('gameDate', '')} {report.get('gameTime', '')} "
        f"sampled={len(report['results'])}"
    )
    for result in report["results"]:
        status = _STATUS_LABELS[result["status"]]
        observed = result.get("observed_name") or "?"
        activity = result.get("activity_id") or "?"
        if result.get("source") == "scheduler":
            print(
                f"[PresenceValidation] {status:13} {result['character_id']}: "
                f"scheduler_location={observed!r} "
                f"location_id={result.get('observed_location_id') or '?'!r} "
                f"activity={activity!r} "
                f"type={result.get('activity_type') or '?'!r} "
                f"in_flesh={result.get('in_flesh', False)} "
                f"transit={result.get('is_in_transit', False)}"
            )
        else:
            projected = result.get("projected_name") or "?"
            print(
                f"[PresenceValidation] {status:13} {result['character_id']}: "
                f"observed={observed!r} projected={projected!r} activity={activity!r} "
                f"pos=({result.get('x', 0):.0f}, {result.get('y', 0):.0f}, "
                f"{result.get('z', 0):.0f})"
            )
    print(f"[PresenceValidation] counts={report['counts']}")
    if "error" not in report:
        snapshot = update_snapshot(report)
        report["snapshot"] = snapshot
        report["hint"] = f"{report['hint']}\n{_snapshot_hint(snapshot)}"
        if snapshot["baseline"]:
            print(
                f"[PresenceSnapshot] baseline #{snapshot['sequence']} stored: "
                f"{snapshot['identity_count']} identities"
            )
        else:
            print(
                f"[PresenceSnapshot] snapshot #{snapshot['sequence']}: "
                f"events={snapshot['event_count']} "
                f"changed_characters={snapshot['changed_characters']}"
            )
            for event in snapshot["events"]:
                print(
                    f"[PresenceSnapshot] {_EVENT_LABELS[event['type']]:20} "
                    f"{event['character_id']}: {_describe_event(event)}"
                )
    return report
