"""
voice_id <-> scheduler CharacterID mapping.
"""

import json
import os
import re

from . import schedule_cache
from .settings import DATA_DIR

OVERRIDES_PATH = os.path.join(DATA_DIR, "schedule_character_overrides.json")

_overrides = None
_norm_index = None
_norm_index_dump_id = None


def reset_for_tests(overrides=None):
    global _overrides, _norm_index, _norm_index_dump_id
    _overrides = overrides
    _norm_index = None
    _norm_index_dump_id = None


def _normalize(value):
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def _load_overrides():
    global _overrides
    if _overrides is not None:
        return _overrides
    try:
        with open(OVERRIDES_PATH, "r", encoding="utf-8") as f:
            _overrides = json.load(f)
    except Exception:
        _overrides = {}
    return _overrides


def _index():
    global _norm_index, _norm_index_dump_id
    dump_id = schedule_cache.get_completed_dump_id()
    if _norm_index is None or dump_id != _norm_index_dump_id:
        candidates = {}
        for character_id in schedule_cache.all_character_ids():
            candidates.setdefault(_normalize(character_id), []).append(character_id)
        _norm_index = {
            normalized: values[0]
            for normalized, values in candidates.items()
            if normalized and len(values) == 1
        }
        _norm_index_dump_id = dump_id
    return _norm_index


def invalidate_index():
    global _norm_index, _norm_index_dump_id
    _norm_index = None
    _norm_index_dump_id = None


def get_character_id(voice_id):
    """Scheduler CharacterID for a mod voice id, or None."""
    override = _load_overrides().get(voice_id)
    if override:
        return override
    return _index().get(_normalize(voice_id))


def coverage_report(voice_ids):
    resolved = {voice_id: get_character_id(voice_id) for voice_id in voice_ids}
    unmatched = [voice_id for voice_id, character_id in resolved.items() if not character_id]
    matched = len(voice_ids) - len(unmatched)
    known = schedule_cache.all_character_ids()
    mapped = {character_id for character_id in resolved.values() if character_id}
    return {
        "total_voice_ids": len(voice_ids),
        "matched": matched,
        "unmatched_voice_ids": sorted(unmatched),
        "unmapped_character_ids": sorted(known - mapped),
    }


def voice_ids_from_manifest(manifest):
    if isinstance(manifest, dict) and isinstance(manifest.get("voices"), dict):
        return list(manifest["voices"])
    if isinstance(manifest, dict):
        return list(manifest)
    return [entry.get("id") or entry.get("name")
            for entry in manifest if isinstance(entry, dict)]


if __name__ == "__main__":
    manifest_path = os.path.join(DATA_DIR, "voice_manifest.json")
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    voice_ids = voice_ids_from_manifest(manifest)
    report = coverage_report([voice_id for voice_id in voice_ids if voice_id])
    print(f"voice ids: {report['total_voice_ids']}, matched: {report['matched']}")
    print(f"unmatched voice ids ({len(report['unmatched_voice_ids'])}):")
    for voice_id in report["unmatched_voice_ids"]:
        print(f"  {voice_id}")
