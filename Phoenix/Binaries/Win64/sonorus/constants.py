"""
Shared constants for Sonorus modules.
"""

# Version
VERSION = "1.0.6"

# TTS audio buffer settings
TTS_BUFFER_SECONDS = 0.6  # Seconds of audio to buffer before playback starts

# Landmark beacon settings
LANDMARK_MAX_DISTANCE = 500000  # ~5km in UE units
LANDMARK_VERTICAL_THRESHOLD = 500  # ~5m - include "above"/"below" if Z diff exceeds this
LANDMARK_BEACON_COUNT = 8  # Number of nearest beacons to include

# Dialogue dedup settings
DIALOGUE_DEDUP_MINUTES = 5  # Don't show same NPC line if said within this many minutes
DIALOGUE_HISTORY_LIMIT = 30  # Max lines to include in LLM context

# Conversation earshot - max distance for NPCs to participate in AI conversations
# 1000 UE units = ~10 meters - realistic "earshot" for multi-NPC dialogue
CONVERSATION_EARSHOT_DISTANCE = 1000
# Reduced distance when player is invisible (Disillusionment charm)
# 300 UE units = ~3 meters - NPCs can barely notice invisible player
STEALTH_EARSHOT_DISTANCE = 300
# Extended distance when player is on broom - companion can fly alongside at varying distances
# 10000 UE units = ~100 meters - allows companion conversation while flying together
BROOM_EARSHOT_DISTANCE = 10000

# Game window title for detection
GAME_WINDOW_TITLE = "Hogwarts Legacy"

# Voice name translation map
# Some NPCs use location-prefixed IDs in-game (e.g., "HOG_Sanctum_Guardian1")
# but their voice references are stored under shorter names (e.g., "Guardian1").
VOICE_NAME_ALIASES = {
    "HOG_Sanctum_Guardian1": "Guardian1",   # Percival Rackham
    "HOG_Sanctum_Guardian2": "Guardian2",   # Charles Rookwood
    "HOG_Sanctum_Guardian3": "Guardian3",   # Twig
    "HOG_Sanctum_Guardian4": "Guardian4",   # San Bakar
    "HOG_Sanctum_Guardian5": "Guardian5",   # Isidora the Bold
    "HOG_Sanctum_Guardian5y11": "Guardian5y11",
    "HOG_Sanctum_Guardian5y17": "Guardian5y17",
}


def resolve_voice_name(voice_name: str) -> str:
    """Translate a game voice ID to its voice reference name, if aliased."""
    return VOICE_NAME_ALIASES.get(voice_name, voice_name)


# Commitment System
COMMITMENT_TRAVEL_TIME_MIN = 15      # Override applied this many game minutes before start time
COMMITMENT_WAIT_TIME_MIN = 45        # No-show declared after waiting this long past start time
COMMITMENT_MIN_WINDOW_MIN = 60       # Minimum total block: travel + wait (for conflict detection)
COMMITMENT_MAX_CONTEXT_HISTORY = 20  # Max commitments shown in NPC prompt

# Location -> Activity mapping for schedule overrides (V1: all-day 0-2400 activities only)
LOCATION_ACTIVITIES = {
    # Hogsmeade
    "HM_ThreeBroomsticks": {"activity": "HM_ThreeBroomsticksHours", "display": "Three Broomsticks", "type": "FreeTime"},
    "HM_Hogshead": {"activity": "HM_HogsHeadHours", "display": "Hog's Head", "type": "FreeTime"},
    "HM_TheGraveyard": {"activity": "HM_GraveyardHours", "display": "Hogsmeade Graveyard", "type": "FreeTime"},
    "HM_TheOldFool": {"activity": "HM_TheOldFoolHours", "display": "The Old Fool", "type": "FreeTime"},
    "HM_TwistedAlley": {"activity": "HM_TwistedAlleyHours", "display": "Twisted Alley", "type": "FreeTime"},
    "HM_VilliageGates": {"activity": "HM_VilliageGatesHours", "display": "Hogsmeade Village Gates", "type": "FreeTime"},
    "HM_WaterMill": {"activity": "HM_WaterMillHours", "display": "Hogsmeade Water Mill", "type": "FreeTime"},
    # Hogwarts - Great Hall & Courtyards
    "HOG_GreatHall": {"activity": "ForcedNavigation", "display": "The Great Hall", "type": "FreeTime"},
    "HOG_QuadCourtyard": {"activity": "Quad_Courtyard_Mingle", "display": "Quad Courtyard", "type": "Mingle"},
    "HOG_TransfigurationCourtyard": {"activity": "Transfiguration_Courtyard_Mingle", "display": "Transfiguration Courtyard", "type": "Mingle"},
    "HOG_ViaductEntrance": {"activity": "Viaduct_Entrance_FreeTime", "display": "Viaduct Entrance", "type": "FreeTime"},
    # Hogwarts - Common Rooms
    "HOG_GryffindorTower": {"activity": "Gryffindor_CommonRoom_Clean", "display": "Gryffindor Common Room", "type": "FreeTime"},
    "HOG_HufflepuffBasement": {"activity": "Hufflepuff_CommonRoom_Clean", "display": "Hufflepuff Common Room", "type": "FreeTime"},
    "HOG_Ravenclaw_CommonRoom": {"activity": "Ravenclaw_CommonRoom_Clean", "display": "Ravenclaw Common Room", "type": "FreeTime"},
    "HOG_Slytherin_CommonRoom": {"activity": "Slytherin_CommonRoom_Clean", "display": "Slytherin Common Room", "type": "MissionCritical"},
    # Hogwarts - Other
    "HOG_AstronomyTower": {"activity": "HOG_AstronomyTower", "display": "Astronomy Tower", "type": "Mingle"},
    "HOG_Boathouse": {"activity": "HOG_Boathouse_FreeTime", "display": "The Boathouse", "type": "FreeTime"},
    "HOG_HospitalWing": {"activity": "HospitalWing_Mingle", "display": "Hospital Wing", "type": "Mingle"},
    "HOG_Owlery": {"activity": "HOG_Owlery_Mingle", "display": "The Owlery", "type": "Mingle"},
    "HOG_FacultyTower": {"activity": "Faculty_Tower_Mingle", "display": "Faculty Tower", "type": "Mingle"},
    "HOG_StoneBridge": {"activity": "HOG_StoneBridge_Mingle", "display": "Stone Bridge", "type": "Mingle"},
    "HOG_SuspensionBridge": {"activity": "HOG_SuspensionBridge_Mingle", "display": "Suspension Bridge", "type": "Mingle"},
    "HOG_WoodenBridge": {"activity": "HOG_WoodenBridge_Mingle", "display": "Wooden Bridge", "type": "Mingle"},
    "HOG_Greenhouses": {"activity": "HOG_Greenhouses_FreeTime", "display": "The Greenhouses", "type": "FreeTime"},
    "HOG_PondDock": {"activity": "HOG_PondDock", "display": "The Pond Dock", "type": "FreeTime"},
}

# Graphiti memory settings
# Previous episodes included for entity deduplication context (graphiti default is 10)
EPISODE_CONTEXT_WINDOW = 3
