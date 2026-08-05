"""Conservative schedule projection over the validated Main baseline."""

from collections import Counter, defaultdict

from . import location_names
from . import schedule_cache


BASELINE_SCHEDULE_KEY = "Main"
DAY_COLUMNS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

_REGION_ACTIVITY_TYPES = {"patrol"}


def _military_to_minutes(value, allow_2400=False):
    if value is None:
        return None
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    if allow_2400 and value == 2400:
        return 24 * 60
    hour, minute = divmod(value, 100)
    if not 0 <= hour < 24 or not 0 <= minute < 60:
        return None
    return hour * 60 + minute


def _effective_time(entry, activity, override_column, activity_column):
    value = entry.get(override_column)
    if value is None or value < 0:
        value = activity.get(activity_column)
    return value


def validate_baseline_entry(entry):
    """Return validated row metadata, or (None, exclusion_reason)."""
    if entry.get("EntryTypeID") != "Time":
        return None, "entry_type_not_time"
    if (entry.get("ScheduleKeys") or "").strip() != BASELINE_SCHEDULE_KEY:
        return None, "not_exact_main"

    activity = schedule_cache.get_activity(entry.get("ActivityID") or "")
    if activity is None:
        return None, "missing_activity"

    raw_start = _effective_time(entry, activity, "OverrideStartTime", "StartTime")
    raw_end = _effective_time(entry, activity, "OverrideEndTime", "EndTime")
    if raw_start == 2400:
        return None, "sentinel_start_2400"

    start = _military_to_minutes(raw_start)
    end = _military_to_minutes(raw_end, allow_2400=True)
    if start is None or end is None:
        return None, "invalid_time"
    if start == end:
        return None, "empty_window"

    location_id = entry.get("OverrideLocationID") or activity.get("LocationID")
    if not location_id:
        return None, "missing_location_id"
    if schedule_cache.get_location(location_id) is None:
        return None, "unresolved_location_id"

    return {
        "entry": entry,
        "activity": activity,
        "start": start,
        "end": end,
        "location_id": location_id,
    }, None


def _windows_for_day(row, day_of_week):
    day_col = DAY_COLUMNS[day_of_week % 7]
    previous_day_col = DAY_COLUMNS[(day_of_week - 1) % 7]
    activity = row["activity"]
    start, end = row["start"], row["end"]

    if start < end:
        return [(start, end)] if activity.get(day_col) else []

    windows = []
    if activity.get(day_col):
        windows.append((start, 24 * 60))
    if activity.get(previous_day_col) and end > 0:
        windows.append((0, end))
    return windows


def _candidate_windows(character_id, day_of_week):
    candidates = []
    for entry in schedule_cache.get_entries_for_character(character_id):
        row, _reason = validate_baseline_entry(entry)
        if row is None:
            continue
        for start, end in _windows_for_day(row, day_of_week):
            candidates.append((row, start, end))
    return candidates


def project(character_id, day_of_week, minutes_of_day):
    """Return one unambiguous Main-baseline projection, otherwise None."""
    if not 0 <= minutes_of_day < 24 * 60:
        return None
    matches = [
        (row, start, end)
        for row, start, end in _candidate_windows(character_id, day_of_week)
        if start <= minutes_of_day < end
    ]
    if len(matches) != 1:
        return None

    row, start, end = matches[0]
    activity = row["activity"]
    activity_type = activity.get("ActivityTypeID") or ""
    resolved_location = location_names.resolve_location(row["location_id"])
    if resolved_location is None:
        return None
    specificity = resolved_location["specificity"]
    if activity_type.casefold() in _REGION_ACTIVITY_TYPES:
        specificity = "region"
    return {
        "activity_id": activity.get("ActivityID"),
        "activity_type": activity_type,
        "location_id": row["location_id"],
        "start_minutes": start,
        "end_minutes": end,
        "location_method": resolved_location["method"],
        "specificity": specificity,
    }


def project_span(character_id, day_of_week, start_minutes, end_minutes):
    """Return unambiguous Main-baseline segments over a same-day span."""
    start_minutes = max(0, start_minutes)
    end_minutes = min(24 * 60, end_minutes)
    if end_minutes <= start_minutes:
        return []

    boundaries = {start_minutes, end_minutes}
    for _row, start, end in _candidate_windows(character_id, day_of_week):
        if start_minutes < start < end_minutes:
            boundaries.add(start)
        if start_minutes < end < end_minutes:
            boundaries.add(end)

    segments = []
    points = sorted(boundaries)
    for cursor, segment_end in zip(points, points[1:]):
        current = project(character_id, day_of_week, cursor)
        if current is None:
            continue
        segment = dict(current)
        segment["start_minutes"] = cursor
        segment["end_minutes"] = segment_end
        if (segments
                and segments[-1]["end_minutes"] == cursor
                and all(segments[-1].get(key) == segment.get(key)
                        for key in ("activity_id", "activity_type", "location_id", "specificity"))):
            segments[-1]["end_minutes"] = segment_end
        else:
            segments.append(segment)
    return segments


def baseline_validation_report():
    """Describe baseline eligibility, coverage, and exact overlap failures."""
    exclusions = Counter()
    eligible_by_character = defaultdict(list)
    total_entries = 0

    for character_id in sorted(schedule_cache.all_character_ids()):
        for entry in schedule_cache.get_entries_for_character(character_id):
            total_entries += 1
            row, reason = validate_baseline_entry(entry)
            if row is None:
                exclusions[reason] += 1
            else:
                eligible_by_character[character_id].append(row)

    ambiguous_segments = []
    covered_minutes = 0
    possible_minutes = len(eligible_by_character) * 7 * 24 * 60

    for character_id, rows in eligible_by_character.items():
        for day_of_week in range(7):
            windows = []
            for row in rows:
                for start, end in _windows_for_day(row, day_of_week):
                    windows.append((start, end, row["activity"].get("ActivityID")))
            boundaries = sorted({point for start, end, _activity_id in windows
                                 for point in (start, end)})
            for start, end in zip(boundaries, boundaries[1:]):
                activity_ids = sorted(
                    activity_id
                    for window_start, window_end, activity_id in windows
                    if window_start <= start < window_end
                )
                if activity_ids:
                    covered_minutes += end - start
                if len(activity_ids) > 1:
                    ambiguous_segments.append({
                        "character_id": character_id,
                        "day_of_week": day_of_week,
                        "start_minutes": start,
                        "end_minutes": end,
                        "activity_ids": activity_ids,
                    })

    return {
        "dump_id": schedule_cache.get_completed_dump_id(),
        "dump_complete": schedule_cache.is_dump_complete(),
        "total_entries": total_entries,
        "eligible_rows": sum(len(rows) for rows in eligible_by_character.values()),
        "eligible_characters": len(eligible_by_character),
        "eligible_character_ids": sorted(eligible_by_character),
        "exclusion_reasons": dict(sorted(exclusions.items())),
        "covered_minutes": covered_minutes,
        "possible_minutes": possible_minutes,
        "coverage_percent": round(100.0 * covered_minutes / possible_minutes, 2)
        if possible_minutes else 0.0,
        "ambiguous_characters": len({row["character_id"] for row in ambiguous_segments}),
        "ambiguous_segments": ambiguous_segments,
    }
