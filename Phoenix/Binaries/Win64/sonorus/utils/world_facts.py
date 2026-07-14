"""
World facts system — derives world state and NPC-specific story milestones
from game mission completion statuses.

Mission statuses are probed by Lua from MissionManager and sent as a dict
of {mission_id: status_int} in the game context. This module reads the
fact definitions from sonorus/data/world_facts.json and returns formatted
text for injection into LLM prompts.

Status enum: 0=PreActive 1=Activating 2=Active 3=PostActive 4=Complete 5=Failed 6=Invalid
"""

import json
import os

_FACTS_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'world_facts.json')
_facts_cache = None


def _load_facts():
    """Load and cache the world facts definition file."""
    global _facts_cache
    if _facts_cache is not None:
        return _facts_cache
    try:
        with open(_FACTS_PATH, 'r', encoding='utf-8') as f:
            _facts_cache = json.load(f)
    except Exception as e:
        print(f"[WorldFacts] Failed to load {_FACTS_PATH}: {e}")
        _facts_cache = {}
    return _facts_cache


def get_global_facts(mission_statuses, player_name=None):
    """Get global world facts text to append to world_lore.

    Args:
        mission_statuses: Dict of {mission_id: status_int} from Lua context.
        player_name: Player name for {player_name} substitution.

    Returns:
        String of global facts to append to world_lore, or empty string.
    """
    if not mission_statuses:
        return ""

    facts = _load_facts()
    if not facts:
        return ""

    player = player_name or "the player"
    lines = []

    for entry in facts.get("global_facts", []):
        mission = entry.get("mission")
        min_status = entry.get("min_status", 4)
        status = mission_statuses.get(mission)
        if status is not None and status >= min_status:
            lines.append(entry['fact'].replace('{player_name}', player))

    return "\n".join(lines)


def get_npc_facts(mission_statuses, speaker_id, player_name=None):
    """Get NPC-specific story milestone facts.

    Args:
        mission_statuses: Dict of {mission_id: status_int} from Lua context.
        speaker_id: NPC speaker ID to get facts for.
        player_name: Player name for {player_name} substitution.

    Returns:
        Formatted '## Your Story' section string, or empty string.
    """
    if not mission_statuses or not speaker_id:
        return ""

    facts = _load_facts()
    if not facts:
        return ""

    player = player_name or "the player"
    lines = []

    for entry in facts.get("npc_facts", {}).get(speaker_id, []):
        mission = entry.get("mission")
        min_status = entry.get("min_status", 4)
        status = mission_statuses.get(mission)
        if status is not None and status >= min_status:
            lines.append(f"- {entry['fact'].replace('{player_name}', player)}")

    if not lines:
        return ""

    return "## Your Story\n" + "\n".join(lines)
