"""
Runtime resolution of schedule LocationIDs and raw world positions to display names.
"""

import json
import math
import os

from . import schedule_cache
from .landmarks import load_landmarks
from .settings import DATA_DIR

LANDMARK_MAX_DIST = 10000.0

_EXACT_METHODS = {"override", "registry", "parent"}
_REGION_METHODS = {"commitment", "landmark"}

_names = None
_names_mtime = None
_loaded_names_path = None
_names_path_override = None
_locations_override = None
_landmarks_override = None


def reset_for_tests(names_path=None, locations=None, landmarks=None):
    global _names, _names_mtime, _loaded_names_path
    global _names_path_override, _locations_override, _landmarks_override
    _names = None
    _names_mtime = None
    _loaded_names_path = None
    _names_path_override = names_path
    _locations_override = locations
    _landmarks_override = landmarks


def _load_names():
    global _names, _names_mtime, _loaded_names_path
    path = _names_path_override or os.path.join(DATA_DIR, "schedule_location_names.json")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = None
    if (_names is not None and path == _loaded_names_path
            and mtime is not None and mtime == _names_mtime):
        return _names
    try:
        with open(path, "r", encoding="utf-8") as f:
            _names = json.load(f)
    except Exception:
        _names = {}
    _names_mtime = mtime
    _loaded_names_path = path
    return _names


def _all_locations():
    if _locations_override is not None:
        return _locations_override
    return list(schedule_cache.iter_locations())


def _all_landmarks():
    if _landmarks_override is not None:
        return _landmarks_override
    return load_landmarks()


def resolve_location(location_id):
    """Return approved name metadata, rejecting heuristic or unknown methods."""
    entry = _load_names().get(location_id)
    if not isinstance(entry, dict) or not entry.get("name"):
        return None
    method = entry.get("method")
    if method in _EXACT_METHODS:
        specificity = "exact"
    elif method in _REGION_METHODS:
        specificity = "region"
    else:
        return None
    return {
        "name": entry["name"],
        "method": method,
        "specificity": specificity,
        "world": entry.get("world"),
    }


def resolve_location_id(location_id):
    """LocationID -> approved display name, or None if unknown."""
    resolved = resolve_location(location_id)
    return resolved["name"] if resolved else None


def resolve_position(x, y, z):
    """(x, y, z) -> (display_name or None, location_id or None)."""
    best_lid, best_vol = None, None
    for row in _all_locations():
        ox, oy, oz = row.get("VolumeOriginX"), row.get("VolumeOriginY"), row.get("VolumeOriginZ")
        ex, ey, ez = row.get("VolumeExtentX"), row.get("VolumeExtentY"), row.get("VolumeExtentZ")
        if None in (ox, oy, oz, ex, ey, ez) or ex <= 0 or ey <= 0 or ez <= 0:
            continue
        if abs(x - ox) <= ex and abs(y - oy) <= ey and abs(z - oz) <= ez:
            vol = ex * ey * ez
            if best_vol is None or vol < best_vol:
                name = resolve_location_id(row["LocationID"])
                if name:
                    best_lid, best_vol = row["LocationID"], vol
    if best_lid:
        return resolve_location_id(best_lid), best_lid

    best_name, best_dist = None, LANDMARK_MAX_DIST
    for landmark in _all_landmarks():
        d = math.dist((x, y, z), (landmark["x"], landmark["y"], landmark["z"]))
        if d < best_dist:
            best_name, best_dist = landmark.get("name"), d
    return best_name, None


def position_matches_location(location_id, x, y, z):
    """Return whether a world point is compatible with an approved location."""
    expected = resolve_location(location_id)
    if expected is None:
        return False

    expected_name = expected["name"].casefold()
    observed_name, observed_id = resolve_position(x, y, z)
    if observed_id == location_id:
        return True
    if observed_name and observed_name.casefold() == expected_name:
        return True

    for row in _all_locations():
        if row.get("LocationID") != location_id:
            continue
        ox, oy, oz = row.get("VolumeOriginX"), row.get("VolumeOriginY"), row.get("VolumeOriginZ")
        ex, ey, ez = row.get("VolumeExtentX"), row.get("VolumeExtentY"), row.get("VolumeExtentZ")
        if None not in (ox, oy, oz, ex, ey, ez) and ex > 0 and ey > 0 and ez > 0:
            if abs(x - ox) <= ex and abs(y - oy) <= ey and abs(z - oz) <= ez:
                return True
        break

    for landmark in _all_landmarks():
        name = landmark.get("name")
        if not name or name.casefold() != expected_name:
            continue
        distance = math.dist((x, y, z), (landmark["x"], landmark["y"], landmark["z"]))
        if distance < LANDMARK_MAX_DIST:
            return True
    return False
