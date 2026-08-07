"""
Shared constants for Sonorus modules.
"""

# Version
VERSION = "1.0.8-p7"

# Cooked package/container ID for the Sonorus Blueprint mod package.
SONORUS_MOD_PACKAGE_ID = 0xA9158FE5FDBBC70D

# Facial emote tags emitted by the LLM and parsed by the server.
# Keep this as the single server-side source of truth.
EMOTE_TAGS = (
    "happy", "content", "tired", "fond", "shy", "beam", "proud",
    "sad", "angry", "annoyed", "surprised", "confused", "cringe",
    "disgusted", "skeptical", "concerned", "sympathy", "afraid",
    "amused", "embarrassed", "relieved", "curious",
    "determined", "mischievous", "smug",
)
EMOTE_TAG_ALIASES = {
    # Positive and warm expressions
    "joy": "happy",
    "happiness": "happy",
    "delight": "happy",
    "joyful": "happy",
    "joyous": "happy",
    "cheerful": "happy",
    "glad": "happy",
    "pleased": "happy",
    "delighted": "happy",
    "elated": "happy",
    "jubilant": "happy",
    "thrilled": "happy",
    "ecstatic": "happy",
    "blissful": "happy",
    "calm": "content",
    "peaceful": "content",
    "serene": "content",
    "satisfied": "content",
    "relaxed": "content",
    "tranquil": "content",
    "comfortable": "content",
    "at-ease": "content",
    "at ease": "content",
    "contentment": "content",
    "exhausted": "tired",
    "weary": "tired",
    "sleepy": "tired",
    "drowsy": "tired",
    "drained": "tired",
    "fatigued": "tired",
    "worn-out": "tired",
    "worn out": "tired",
    "exhaustion": "tired",
    "fatigue": "tired",
    "affectionate": "fond",
    "loving": "fond",
    "tender": "fond",
    "adoring": "fond",
    "warm": "fond",
    "caring": "fond",
    "romantic": "fond",
    "affection": "fond",
    "love": "fond",
    "bashful": "shy",
    "timid": "shy",
    "coy": "shy",
    "sheepish": "shy",
    "reserved": "shy",
    "shyness": "shy",
    "radiant": "beam",
    "beaming": "beam",
    "glowing": "beam",
    "grinning": "beam",
    "excited": "beam",
    "enthusiastic": "beam",
    "exuberant": "beam",
    "excitement": "beam",
    "triumphant": "proud",
    "accomplished": "proud",
    "approving": "proud",
    "impressed": "proud",
    "dignified": "proud",
    "honored": "proud",
    "honoured": "proud",
    "pride": "proud",

    # Sadness, anger, and irritation
    "unhappy": "sad",
    "sorrowful": "sad",
    "heartbroken": "sad",
    "dejected": "sad",
    "melancholy": "sad",
    "mournful": "sad",
    "disappointed": "sad",
    "despondent": "sad",
    "gloomy": "sad",
    "crestfallen": "sad",
    "hurt": "sad",
    "sadness": "sad",
    "sorrow": "sad",
    "grief": "sad",
    "furious": "angry",
    "enraged": "angry",
    "livid": "angry",
    "irate": "angry",
    "incensed": "angry",
    "outraged": "angry",
    "hostile": "angry",
    "wrathful": "angry",
    "anger": "angry",
    "rage": "angry",
    "fury": "angry",
    "irritated": "annoyed",
    "frustrated": "annoyed",
    "exasperated": "annoyed",
    "impatient": "annoyed",
    "bothered": "annoyed",
    "peeved": "annoyed",
    "grumpy": "annoyed",
    "resentful": "annoyed",
    "jealous": "annoyed",
    "jealousy": "annoyed",
    "envious": "annoyed",
    "envy": "annoyed",
    "annoyance": "annoyed",
    "irritation": "annoyed",
    "frustration": "annoyed",

    # Surprise, uncertainty, and aversion
    "astonished": "surprised",
    "amazed": "surprised",
    "startled": "surprised",
    "shocked": "surprised",
    "stunned": "surprised",
    "awestruck": "surprised",
    "speechless": "surprised",
    "surprise": "surprised",
    "shock": "surprised",
    "astonishment": "surprised",
    "puzzled": "confused",
    "bewildered": "confused",
    "baffled": "confused",
    "uncertain": "confused",
    "perplexed": "confused",
    "disoriented": "confused",
    "lost": "confused",
    "confusion": "confused",
    "bewilderment": "confused",
    "bemused": "confused",
    "awkward": "cringe",
    "uncomfortable": "cringe",
    "wincing": "cringe",
    "secondhand-embarrassment": "cringe",
    "secondhand embarrassment": "cringe",
    "repulsed": "disgusted",
    "revolted": "disgusted",
    "appalled": "disgusted",
    "nauseated": "disgusted",
    "sickened": "disgusted",
    "contemptuous": "smug",
    "scornful": "smug",
    "disgust": "disgusted",
    "revulsion": "disgusted",
    "doubtful": "skeptical",
    "suspicious": "skeptical",
    "dubious": "skeptical",
    "unconvinced": "skeptical",
    "incredulous": "skeptical",
    "distrustful": "skeptical",
    "wary": "skeptical",
    "skepticism": "skeptical",
    "scepticism": "skeptical",
    "suspicion": "skeptical",
    "doubt": "skeptical",

    # Concern, compassion, and fear
    "worried": "concerned",
    "anxious": "concerned",
    "uneasy": "concerned",
    "troubled": "concerned",
    "apprehensive": "concerned",
    "cautious": "concerned",
    "solicitous": "concerned",
    "concern": "concerned",
    "worry": "concerned",
    "anxiety": "concerned",
    "sympathetic": "sympathy",
    "compassionate": "sympathy",
    "empathetic": "sympathy",
    "pitying": "sympathy",
    "consoling": "sympathy",
    "comforting": "sympathy",
    "understanding": "sympathy",
    "compassion": "sympathy",
    "empathy": "sympathy",
    "pity": "sympathy",
    "fearful": "afraid",
    "scared": "afraid",
    "frightened": "afraid",
    "terrified": "afraid",
    "horrified": "afraid",
    "panicked": "afraid",
    "alarmed": "afraid",
    "petrified": "afraid",
    "nervous": "afraid",
    "nervousness": "afraid",
    "fear": "afraid",
    "terror": "afraid",
    "panic": "afraid",

    # Social reactions and composure
    "entertained": "amused",
    "tickled": "amused",
    "laughing": "amused",
    "chuckling": "amused",
    "humored": "amused",
    "humoured": "amused",
    "amusement": "amused",
    "abashed": "embarrassed",
    "ashamed": "embarrassed",
    "humiliated": "embarrassed",
    "mortified": "embarrassed",
    "flustered": "embarrassed",
    "guilty": "embarrassed",
    "self-conscious": "embarrassed",
    "self conscious": "embarrassed",
    "embarrassment": "embarrassed",
    "shame": "embarrassed",
    "guilt": "embarrassed",
    "reassured": "relieved",
    "comforted": "relieved",
    "unburdened": "relieved",
    "thankful": "relieved",
    "relief": "relieved",

    # Attention, resolve, and playful superiority
    "interested": "curious",
    "intrigued": "curious",
    "inquisitive": "curious",
    "attentive": "curious",
    "questioning": "curious",
    "fascinated": "curious",
    "curiosity": "curious",
    "interest": "curious",
    "intrigue": "curious",
    "resolute": "determined",
    "focused": "determined",
    "serious": "determined",
    "defiant": "determined",
    "steadfast": "determined",
    "intent": "determined",
    "committed": "determined",
    "determination": "determined",
    "resolve": "determined",
    "defiance": "determined",
    "playful": "mischievous",
    "cheeky": "mischievous",
    "sly": "mischievous",
    "impish": "mischievous",
    "devious": "mischievous",
    "teasing": "mischievous",
    "conspiratorial": "mischievous",
    "flirtatious": "mischievous",
    "mischief": "mischievous",
    "playfulness": "mischievous",
    "arrogant": "smug",
    "haughty": "smug",
    "cocky": "smug",
    "self-satisfied": "smug",
    "superior": "smug",
    "condescending": "smug",
    "sardonic": "smug",
    "self satisfied": "smug",
    "arrogance": "smug",
    "superiority": "smug",
}
EMOTE_TAG_INTENSITY_MULTIPLIERS = {
    "nervous": 0.5,
    "nervousness": 0.5,
}
# Model-facing variants backed by a canonical preset and intensity multiplier.
EMOTE_VARIANT_TAGS = ("nervous",)
EMOTE_TAGS_PROMPT = ", ".join(
    f"[{tag}]" for tag in (*EMOTE_TAGS, *EMOTE_VARIANT_TAGS)
)

# TTS audio buffer settings
TTS_BUFFER_SECONDS = 0.6  # Seconds of audio to buffer before playback starts
NPC_QUESTION_FOLLOW_UP_TIMEOUT_SECONDS = 10.0  # Delay after a completed question before an NPC checks back in
RECENT_DIALOGUE_WINDOW_SECONDS = 120  # Real-time seconds to consider dialogue "recent" for attention meter

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
# Extended distance for an actively following companion while walking
# 3000 UE units = ~30 meters - avoids premature "walked away" aborts when they trail behind
FOLLOWING_COMPANION_EARSHOT_DISTANCE = 3000
# Reduced distance when player is invisible (Disillusionment charm)
# 300 UE units = ~3 meters - NPCs can barely notice invisible player
STEALTH_EARSHOT_DISTANCE = 300
# Extended distance when player is on broom - companion can fly alongside at varying distances
# 10000 UE units = ~100 meters - allows companion conversation while flying together
BROOM_EARSHOT_DISTANCE = 10000

# Game window title for detection
GAME_WINDOW_TITLE = "Hogwarts Legacy"

# Languages without voice dubs — these use English voice manifests and references
# while still using their own localization for subtitles and AI text.
UNDUBBED_LANGUAGES = frozenset({"AR_AE", "PL_PL", "RU_RU", "KO_KR", "ZH_CN", "ZH_TW"})


def get_voice_language(language: str) -> str:
    """Map a game language to its voice language.

    Undubbed languages (no voice dub files in the game) fall back to EN_US
    for voice manifests, voice references, and voice cloning.
    """
    return "EN_US" if language in UNDUBBED_LANGUAGES else language


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

# Owl Post
OWL_POST_ENABLED = True                 # Master toggle (also controllable via settings.json owl_post.enabled)
OWL_MAIL_MIN_DISTANCE = 5000           # Min UE units (~50m) before NPC can write
OWL_MAIL_CONVERSATION_COOLDOWN = 300    # Seconds since last in-person conversation before NPC can write
OWL_MAIL_ORCHESTRATOR_INTERVAL = 180    # Seconds (real-time) between mail orchestrator ticks
OWL_BOARD_ORCHESTRATOR_INTERVAL = 300   # Seconds (real-time) between board orchestrator ticks
OWL_BOARD_UNREAD_CAP = 8               # Max unread posts per board before orchestrator skips it
OWL_BOARD_REPLY_STAGGER_MIN = 10       # Min in-game minutes between staggered reply visibility
OWL_BOARD_REPLY_STAGGER_MAX = 30       # Max in-game minutes between staggered reply visibility
OWL_MAIL_UNSOLICITED_QUIET_HOURS_START = 22  # 10:00 PM
OWL_MAIL_UNSOLICITED_QUIET_HOURS_END = 6     # 6:00 AM
OWL_MAIL_CONTEXT_LIMIT = 3             # Max recent mail exchanges to include in conversation context
OWL_MAIL_MIN_NEW_ENTRIES = 8             # Min new qualifying events before re-evaluating an NPC for mail
OWL_MAIL_MIN_HOURS_SINCE_CONTACT = 6    # Min in-game hours since last interaction before unsolicited mail
OWL_MAIL_DIALOGUE_CHAR_BUDGET = 40000   # Max characters of dialogue context for letter generation (~10k tokens)
EXCLUDED_NPC_ID_PREFIXES = frozenset({
    "t3",
    "midres",
    # Generic hostile/combat archetypes can appear in dialogue history but do
    # not have persistent schedulable actors for Owl Post or commitments.
    "darkwizard",
    "dw_poacher",
    "poacher",
    "ashwinder",
    "loyalist",
    "inferius",
    "trollgeneric",
    "spider",
    "dugbog",
    "wolf",
    "mongrel",
    "animagus",
})

OWL_MAIL_PORTRAIT_NPCS = frozenset({
    "FerdinandOctaviusPratt",
    "FatLady",
    "MaryDunne",
    "LethiaBurbley",
    "SirCadogan",
    "MusicConductor",
    "SylviaPembroke",
    "OgleThePortrait",
})

OWL_MAIL_GHOST_NPCS = frozenset({
    "BloodyBaron",
    "CuthbertBinns",
    "ExtraGhostCharacterM2",
    "FatFriar",
    "NearlyHeadlessNick",
    "Peeves",
    "RichardJackdaw",
    "RichardJackdawHead",
})

OWL_MAIL_PERMANENTLY_DEAD_NPCS = frozenset({
    "Armour1",
    "Armour2",
    "Centaur1",
    "Centaur2",
    "FGMKitchenF",
    "FGMKitchenM",
    "GreyCat",
    "GreyLady",
    "Guardian1",       # Percival Rackham
    "Guardian2",       # Charles Rookwood
    "Guardian3",       # Niamh Fitzgerald
    "Guardian4",       # San Bakar
    "Guardian5",       # Isidora Morganach
    "Guardian5y11",
    "Guardian5y17",
    "MagicalWell",
    "NiamhFitzgerald",
    "PlayerFemale",
    "PlayerMale",
    "PrivateGoblinBanker",
    "Ranrak",          # Ranrok
    "SilvanusSelwyn",
    "SortingHat",
    "StaffroomGargoyleB",
    "TalkingMirror",
    "TheophilusHarlow",
    "TownCrier",
    "VictorRookwood",
})

OWL_MAIL_QUEST_DEATH_STATES = {
    "EleazarFig": ("FGS_01", 4),
    "Lodgok": ("GT03", 4),
    "SolomonSallow": ("EVL_03", 4),
}


def _expand_voice_aliases(npc_ids) -> frozenset[str]:
    """Return lowercase NPC IDs including voice alias variants."""
    alias_lookup = {alias.lower(): target.lower() for alias, target in VOICE_NAME_ALIASES.items()}
    reverse_lookup = {}
    for alias, target in alias_lookup.items():
        reverse_lookup.setdefault(target, set()).add(alias)

    expanded = set()
    for npc_id in npc_ids or ():
        npc_key = str(npc_id or "").strip().lower()
        if not npc_key:
            continue
        expanded.add(npc_key)
        mapped = alias_lookup.get(npc_key)
        if mapped:
            expanded.add(mapped)
        expanded.update(reverse_lookup.get(npc_key, ()))

    return frozenset(expanded)


OWL_MAIL_STATIC_EXCLUDED_NPCS = _expand_voice_aliases(
    OWL_MAIL_PORTRAIT_NPCS
    | OWL_MAIL_GHOST_NPCS
    | OWL_MAIL_PERMANENTLY_DEAD_NPCS
)


def get_excluded_npcs(mission_statuses: dict | None = None) -> set[str]:
    """Get lowercase NPC IDs excluded from interactive systems (owl mail, commitments, etc.)."""
    blocked = set(OWL_MAIL_STATIC_EXCLUDED_NPCS)
    statuses = mission_statuses or {}

    for npc_id, (mission_id, min_status) in OWL_MAIL_QUEST_DEATH_STATES.items():
        status = statuses.get(mission_id)
        if status is not None and status >= min_status:
            blocked.update(_expand_voice_aliases((npc_id,)))

    return blocked


def is_excluded_npc(npc_id: str, mission_statuses: dict | None = None) -> bool:
    """Return True if an NPC is excluded from interactive systems (owl mail, commitments, etc.)."""
    npc_key = str(npc_id or "").strip().lower()
    if not npc_key:
        return False
    if any(npc_key.startswith(prefix) for prefix in EXCLUDED_NPC_ID_PREFIXES):
        return True
    return npc_key in get_excluded_npcs(mission_statuses)

# Location -> Activity mapping for schedule overrides (V1: all-day 0-2400 activities only)
# Keys are canonical mod keys from location_registry.json.
# "activity" values are game ActivityIDs — these are NOT mod keys, do not rename them.
LOCATION_ACTIVITIES = {
    # Hogsmeade
    "HM_ThreeBroomsticks": {"activity": "HM_ThreeBroomsticksHours", "display": "Three Broomsticks", "type": "FreeTime"},
    "HM_Hogshead": {"activity": "HM_HogsHeadHours", "display": "Hog's Head", "type": "FreeTime"},
    "HM_TheOldFool": {"activity": "HM_TheOldFoolHours", "display": "The Old Fool", "type": "FreeTime"},
    "HM_SpireAlley": {"activity": "HM_TwistedAlleyHours", "display": "Spire Alley", "type": "FreeTime"},
    "HM_WaterMill": {"activity": "HM_WaterMillHours", "display": "Hogsmeade Water Mill", "type": "FreeTime"},
    # Hogwarts - Great Hall & Courtyards
    "GreatHall": {"activity": "ForcedNavigation", "display": "The Great Hall", "type": "FreeTime"},
    "QuadCourtyard": {"activity": "Quad_Courtyard_Mingle", "display": "Quad Courtyard", "type": "Mingle"},
    "TransfigurationCourtyard": {"activity": "Transfiguration_Courtyard_Mingle", "display": "Transfiguration Courtyard", "type": "Mingle"},
    "ViaductEntrance": {"activity": "Viaduct_Entrance_FreeTime", "display": "Viaduct Entrance", "type": "FreeTime"},
    # Hogwarts - Common Rooms
    "GryffindorTower": {"activity": "Gryffindor_CommonRoom_Clean", "display": "Gryffindor Common Room", "type": "FreeTime"},
    "HufflepuffBasement": {"activity": "Hufflepuff_CommonRoom_Clean", "display": "Hufflepuff Common Room", "type": "FreeTime"},
    "RavenclawCommonRoom": {"activity": "Ravenclaw_CommonRoom_Clean", "display": "Ravenclaw Common Room", "type": "FreeTime"},
    "SlytherinCommonRoom": {"activity": "Slytherin_CommonRoom_Clean", "display": "Slytherin Common Room", "type": "MissionCritical"},
    # Hogwarts - Other
    # "AstronomyTower": {"activity": "HOG_AstronomyTower", "display": "Astronomy Tower", "type": "Mingle"},
    "Boathouse": {"activity": "HOG_Boathouse_FreeTime", "display": "The Boathouse", "type": "FreeTime"},
    "HospitalWing": {"activity": "HospitalWing_Mingle", "display": "Hospital Wing", "type": "Mingle"},
    "Owlery": {"activity": "HOG_Owlery_Mingle", "display": "The Owlery", "type": "Mingle"},
    "FacultyTower": {"activity": "Faculty_Tower_Mingle", "display": "Faculty Tower", "type": "Mingle"},
    "SuspensionBridge": {"activity": "HOG_SuspensionBridge_Mingle", "display": "Suspension Bridge", "type": "Mingle"},
    "WoodenBridge": {"activity": "HOG_WoodenBridge_Mingle", "display": "Wooden Bridge", "type": "Mingle"},
    "Greenhouses": {"activity": "HOG_Greenhouses_FreeTime", "display": "The Greenhouses", "type": "FreeTime"},
    "PondDock": {"activity": "HOG_PondDock", "display": "The Pond Dock", "type": "FreeTime"},
}

# VR Bridge Plugin (downloaded on demand — not bundled to avoid browser/AV false positives)
VR_BRIDGE_DLL_URL = "https://github.com/KevinAHM/sonorus/releases/download/v1.0.0-vr/sonorus_vr_bridge.dll"
VR_BRIDGE_DLL_SHA256 = "52ac224c0e00866553514fc9d2315140c1e18f5c9927f31baab49b95b72d70ff"

# Graphiti memory settings
# Previous episodes included for entity deduplication context (graphiti default is 10)
EPISODE_CONTEXT_WINDOW = 3


LLM_COST_FEATURES = {
    "conversation": {
        "label": "Conversation",
        "contexts": {
            "chat": "Chat",
            "target_selection": "Target Selection",
            "interjection": "Interjection",
            "commentary_selection": "Commentary Selection",
            "input_correction": "Input Correction",
            "search_intent": "Search Intent",
        },
    },
    "agents": {
        "label": "Agents",
        "contexts": {
            "prompt_parser": "Prompt Parser",
            "move_classifier": "Move Classifier",
            "rhetorical_classifier": "Rhetorical Classifier",
            "location_resolver": "Location Resolver",
        },
    },
    "owl_mail": {
        "label": "Owl Mail",
        "contexts": {
            "owl_mail_generate": "Generate",
            "owl_mail_classifier": "Classifier",
            "owl_mail_summarize": "Summarizer",
        },
    },
    "owl_board": {
        "label": "Owl Board",
        "contexts": {
            "owl_board_generate": "Generate Thread",
            "owl_board_reply": "Reply",
        },
    },
    "memory": {
        "label": "Memory",
        "contexts": {
            "memory_prose": "Memory Prose",
            "episode_generation": "Episode Generation",
            "chapter_detection": "Chapter Detection",
            "migration_chapter": "Migration Chapter",
            "graphiti": "Graphiti",
            "graphiti_small": "Graphiti Small",
        },
    },
    "characters": {
        "label": "Characters",
        "contexts": {
            "bio_generation": "Bio Generation",
            "bio_update": "Bio Update",
        },
    },
    "vision": {
        "label": "Vision",
        "contexts": {
            "vision": "Vision",
        },
    },
    "setup": {
        "label": "Setup",
        "contexts": {
            "setup_test": "Setup Test",
        },
    },
    "evaluation": {
        "label": "Evaluation",
        "contexts": {
            "eval": "Eval",
        },
    },
}

_LLM_COST_CONTEXT_INDEX = {}
for _feature_slug, _feature_data in LLM_COST_FEATURES.items():
    for _context_slug, _context_label in _feature_data.get("contexts", {}).items():
        _LLM_COST_CONTEXT_INDEX[_context_slug] = {
            "feature_slug": _feature_slug,
            "feature_label": _feature_data.get("label", _feature_slug),
            "module_label": _context_label,
        }


def _titleize_context_slug(context: str) -> str:
    if not context:
        return "Unknown"
    return " ".join(part.capitalize() for part in str(context).replace(":", " ").replace("_", " ").split())


def resolve_llm_cost_context(context: str) -> dict:
    """Resolve an LLM context slug into feature/module labels for cost reporting."""
    normalized = str(context or "").strip()
    if not normalized:
        return {
            "feature_slug": "other",
            "feature_label": "Other",
            "module_slug": "unknown",
            "module_label": "Unknown",
        }

    if normalized in _LLM_COST_CONTEXT_INDEX:
        mapped = _LLM_COST_CONTEXT_INDEX[normalized]
        return {
            "feature_slug": mapped["feature_slug"],
            "feature_label": mapped["feature_label"],
            "module_slug": normalized,
            "module_label": mapped["module_label"],
        }

    if normalized.startswith("graphiti_small:"):
        prompt_name = normalized.split(":", 1)[1]
        return {
            "feature_slug": "memory",
            "feature_label": "Memory",
            "module_slug": normalized,
            "module_label": f"Graphiti Small: {_titleize_context_slug(prompt_name)}",
        }

    if normalized.startswith("graphiti:"):
        prompt_name = normalized.split(":", 1)[1]
        return {
            "feature_slug": "memory",
            "feature_label": "Memory",
            "module_slug": normalized,
            "module_label": f"Graphiti: {_titleize_context_slug(prompt_name)}",
        }

    return {
        "feature_slug": "other",
        "feature_label": "Other",
        "module_slug": normalized,
        "module_label": _titleize_context_slug(normalized),
    }
