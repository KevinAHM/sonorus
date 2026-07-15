"""Print a validation report for the conservative schedule baseline."""

import json
import os
import sys


_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
_SONORUS_DIR = os.path.dirname(_TOOLS_DIR)
if _SONORUS_DIR not in sys.path:
    sys.path.insert(0, _SONORUS_DIR)

from utils import schedule_projection  # noqa: E402
from utils import schedule_characters  # noqa: E402
from utils.settings import DATA_DIR  # noqa: E402


def add_voice_coverage(report, manifest):
    voice_ids = schedule_characters.voice_ids_from_manifest(manifest)
    eligible = set(report["eligible_character_ids"])
    mapped = {
        voice_id: schedule_characters.get_character_id(voice_id)
        for voice_id in voice_ids
    }
    matched_characters = {character_id for character_id in mapped.values()
                          if character_id in eligible}
    report["voice_coverage"] = {
        "manifest_voice_ids": len(voice_ids),
        "matched_eligible_characters": len(matched_characters),
        "eligible_character_match_percent": round(
            100.0 * len(matched_characters) / len(eligible), 2
        ) if eligible else 0.0,
        "unmatched_eligible_character_ids": sorted(eligible - matched_characters),
    }
    return report


def main():
    report = schedule_projection.baseline_validation_report()
    manifest_path = os.path.join(DATA_DIR, "voice_manifest.json")
    try:
        with open(manifest_path, "r", encoding="utf-8") as file:
            manifest = json.load(file)
    except (OSError, ValueError):
        manifest = {}
    print(json.dumps(add_voice_coverage(report, manifest), indent=2))


if __name__ == "__main__":
    main()
