"""
Categorical distance model for owl post delivery time calculation.

Maps NPC and player locations to regions (Hogwarts, Hogsmeade, unknown)
and returns randomized in-game delivery times based on region pairing.
"""

import random


# Region prefixes
_REGION_PREFIXES = {
    "HOG_": "hogwarts",
    "HM_": "hogsmeade",
}

# Delivery time ranges (in-game minutes)
_SAME_REGION = (15, 30)
_CROSS_REGION = (60, 120)
_UNKNOWN_DEFAULT = 20


def _get_region(location_id):
    """Extract region from location ID prefix.

    Returns 'hogwarts', 'hogsmeade', or 'unknown'.
    """
    if not location_id or not isinstance(location_id, str):
        return "unknown"

    for prefix, region in _REGION_PREFIXES.items():
        if location_id.startswith(prefix):
            return region

    return "unknown"


def _get_player_region(game_context):
    """Determine player region from nearest landmark beacon in game context.

    game_context has a 'landmarks' field which is a list.
    Each landmark may be a dict with 'id' key or a string.
    Use the closest landmark's ID prefix.
    """
    if not game_context or not isinstance(game_context, dict):
        return "unknown"

    landmarks = game_context.get("landmarks")
    if not landmarks or not isinstance(landmarks, list):
        return "unknown"

    # First landmark is closest
    first = landmarks[0]

    if isinstance(first, dict):
        landmark_id = first.get("id", "")
    elif isinstance(first, str):
        landmark_id = first
    else:
        return "unknown"

    return _get_region(landmark_id)


def calculate_delivery_minutes(sender_location_id, game_context):
    """Calculate delivery time in game minutes.

    Args:
        sender_location_id: NPC's scheduled location ID (e.g., 'HOG_GreatHall') or None
        game_context: Current game context dict

    Returns:
        int: delivery time in game minutes
    """
    sender_region = _get_region(sender_location_id)
    player_region = _get_player_region(game_context)

    # If either region is unknown, return the configurable flat default
    if sender_region == "unknown" or player_region == "unknown":
        from utils.settings import load_settings
        settings = load_settings()
        return settings.get("owl_post", {}).get("delivery_minutes", _UNKNOWN_DEFAULT)

    # Both regions are known
    if sender_region == player_region:
        return random.randint(*_SAME_REGION)
    else:
        return random.randint(*_CROSS_REGION)
