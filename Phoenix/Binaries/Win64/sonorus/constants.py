"""
Shared constants for Sonorus modules.
"""

# Version
VERSION = "1.0.2"

# TTS audio buffer settings
TTS_BUFFER_SECONDS = 2  # Seconds of audio to buffer before playback starts

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
