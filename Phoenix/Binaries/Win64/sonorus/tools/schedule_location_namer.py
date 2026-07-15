"""
Offline tool: resolve schedule LocationIDs to display names.
"""

import argparse
import json
import math
import os
import re
import sys

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
_SONORUS_DIR = os.path.dirname(_TOOLS_DIR)
if _SONORUS_DIR not in sys.path:
    sys.path.insert(0, _SONORUS_DIR)

from utils import schedule_cache  # noqa: E402
from utils import schedule_projection  # noqa: E402
from utils.settings import DATA_DIR  # noqa: E402

LANDMARK_MAX_DIST = 8000.0
_PREFIX_RE = re.compile(r"^(HM|HOG|OVL|FT|M|DF|SF)_", re.IGNORECASE)


def _prettify(location_id):
    text = location_id
    while _PREFIX_RE.match(text):
        text = _PREFIX_RE.sub("", text, count=1)
    text = text.replace("_", " ")
    text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text.title() if text else location_id


def resolve_all(locations, registry, localization, landmarks, overrides):
    """locations: {LocationID: row-dict}. Returns {LocationID: {name, world, method}}."""
    sched_to_name = {}
    for entry in registry.values():
        if not isinstance(entry, dict):
            continue
        schedule_id = entry.get("schedule_id")
        localized_id = entry.get("localized_id")
        if schedule_id and localized_id and localization.get(localized_id):
            sched_to_name[schedule_id] = localization[localized_id]

    def registry_name(location_id):
        return sched_to_name.get(location_id)

    def parent_name(location_id, depth=0):
        if depth > 8:
            return None
        row = locations.get(location_id)
        if not row:
            return None
        parent = row.get("ParentLocationID") or ""
        if not parent or parent == location_id:
            return None
        if parent in overrides:
            return overrides[parent]
        hit = registry_name(parent)
        if hit:
            return hit
        return parent_name(parent, depth + 1)

    def landmark_name(row):
        coords = (row.get("XPos"), row.get("YPos"), row.get("ZPos"))
        if any(value is None for value in coords):
            return None, None
        best, best_method, best_dist = None, None, LANDMARK_MAX_DIST
        for landmark in landmarks:
            if landmark.get("world") and row.get("WorldID") and landmark["world"] != row["WorldID"]:
                continue
            d = math.dist(
                coords,
                (landmark["x"], landmark["y"], landmark["z"]),
            )
            if d < best_dist:
                best = landmark["name"]
                best_method = landmark.get("method", "landmark")
                best_dist = d
        return best, best_method

    out = {}
    for location_id, row in locations.items():
        world = row.get("WorldID") or ""
        if location_id in overrides:
            out[location_id] = {"name": overrides[location_id], "world": world, "method": "override"}
            continue
        name = registry_name(location_id)
        if name:
            out[location_id] = {"name": name, "world": world, "method": "registry"}
            continue
        name = parent_name(location_id)
        if name:
            out[location_id] = {"name": name, "world": world, "method": "parent"}
            continue
        name, method = landmark_name(row)
        if name:
            out[location_id] = {"name": name, "world": world, "method": method}
            continue
        out[location_id] = {"name": _prettify(location_id), "world": world, "method": "heuristic"}
    return out


def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def commitment_anchors(spots, registry, localization, locations=None):
    anchors = []
    locations = locations or {}
    for location_id, positions in spots.items():
        registry_entry = registry.get(location_id) or {}
        if not registry_entry:
            registry_entry = next(
                (entry for entry in registry.values()
                 if isinstance(entry, dict) and entry.get("schedule_id") == location_id),
                {},
            )
        localized_id = registry_entry.get("localized_id") if isinstance(registry_entry, dict) else None
        schedule_id = registry_entry.get("schedule_id") if isinstance(registry_entry, dict) else None
        name = localization.get(localized_id) if localized_id else None
        name = name or _prettify(location_id)
        schedule_location = locations.get(schedule_id or location_id) or {}
        for position in positions if isinstance(positions, list) else []:
            if all(position.get(axis) is not None for axis in ("x", "y", "z")):
                anchors.append({
                    "name": name,
                    "x": position["x"],
                    "y": position["y"],
                    "z": position["z"],
                    "world": schedule_location.get("WorldID"),
                    "method": "commitment",
                })
    return anchors


def coverage_report(resolved):
    """Usage-weighted naming coverage over validated baseline rows only."""
    weights = {}
    for character_id in schedule_cache.all_character_ids():
        for entry in schedule_cache.get_entries_for_character(character_id):
            row, _reason = schedule_projection.validate_baseline_entry(entry)
            if row is None:
                continue
            start, end = row["start"], row["end"]
            duration = end - start if start < end else 24 * 60 - start + end
            active_days = sum(
                1 for day in schedule_projection.DAY_COLUMNS if row["activity"].get(day)
            )
            weight = duration * active_days / 60.0
            weights[row["location_id"]] = weights.get(row["location_id"], 0.0) + weight

    total = sum(weights.values()) or 1.0
    by_method = {}
    unresolved_weight = []
    for location_id, weight in weights.items():
        method = resolved.get(location_id, {}).get("method", "MISSING")
        by_method[method] = by_method.get(method, 0.0) + weight
        if method in ("heuristic", "MISSING"):
            unresolved_weight.append((weight, location_id))

    print("\n=== Conservative baseline naming coverage (usage-weighted hours) ===")
    for method in ("override", "registry", "parent", "commitment", "landmark", "heuristic", "MISSING"):
        if method in by_method:
            print(f"  {method:10s} {100.0 * by_method[method] / total:5.1f}%")
    exact = sum(value for key, value in by_method.items()
                if key in ("override", "registry", "parent"))
    region = sum(value for key, value in by_method.items()
                 if key in ("commitment", "landmark"))
    print(f"  EXACT      {100.0 * exact / total:5.1f}%")
    print(f"  REGION     {100.0 * region / total:5.1f}%")
    print(f"  APPROVED   {100.0 * (exact + region) / total:5.1f}%")
    print("\nTop 30 heuristic/missing LocationIDs by usage (curation candidates):")
    for weight, location_id in sorted(unresolved_weight, reverse=True)[:30]:
        print(f"  {weight:8.1f}h  {location_id}  -> '{resolved.get(location_id, {}).get('name', '?')}'")


def artifact_path(publish=False):
    filename = ("schedule_location_names.json" if publish
                else "schedule_location_names.candidate.json")
    return os.path.join(DATA_DIR, filename)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--publish",
        action="store_true",
        help="write the reviewed artifact to the runtime filename",
    )
    args = parser.parse_args(argv)
    locations = {row["LocationID"]: row for row in schedule_cache.iter_locations()}
    if not locations:
        print("schedule_cache.db is empty - run the game once so ScheduleDump populates it.")
        sys.exit(1)
    registry = _load_json(os.path.join(DATA_DIR, "location_registry.json"), {})
    localization = _load_json(os.path.join(DATA_DIR, "main_localization.json"), {})
    landmarks = _load_json(os.path.join(DATA_DIR, "landmark_locations.json"), {}).get("landmarks", [])
    spots = _load_json(os.path.join(DATA_DIR, "commitment_spots.json"), {})
    landmarks.extend(commitment_anchors(spots, registry, localization, locations))
    overrides = _load_json(os.path.join(DATA_DIR, "schedule_location_name_overrides.json"), {})

    resolved = resolve_all(locations, registry, localization, landmarks, overrides)
    out_path = artifact_path(publish=args.publish)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(resolved, f, indent=1, ensure_ascii=False)
    print(f"Wrote {len(resolved)} entries to {out_path}")
    coverage_report(resolved)


if __name__ == "__main__":
    main()
