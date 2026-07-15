"""
Settings management for Sonorus.
Handles loading, saving, and merging of configuration settings.
"""

import os
import json
import copy
import shutil
from datetime import date, datetime

# Gemini model defaults follow Google's expected preview deprecation dates.
GEMINI_3_5_SWITCH_DATE = date(2027, 6, 1)
GEMINI_CHAT_DEFAULT = 'gemini-3.5-flash' if date.today() >= GEMINI_3_5_SWITCH_DATE else 'gemini-3-flash-preview'
GEMINI_CHAT_DEFAULT_OR = 'google/gemini-3.5-flash' if date.today() >= GEMINI_3_5_SWITCH_DATE else 'google/gemini-3-flash-preview'

GEMINI_2_5_FLASH_LITE_SWITCH_DATE = date(2026, 10, 15)
GEMINI_3_1_FLASH_LITE_SWITCH_DATE = date(2027, 5, 7)
if date.today() >= GEMINI_3_1_FLASH_LITE_SWITCH_DATE:
    GEMINI_FLASH_LITE_DEFAULT = 'gemini-3.5-flash-lite'
    GEMINI_FLASH_LITE_DEFAULT_OR = 'google/gemini-3.5-flash-lite'
elif date.today() >= GEMINI_2_5_FLASH_LITE_SWITCH_DATE:
    GEMINI_FLASH_LITE_DEFAULT = 'gemini-3.1-flash-lite'
    GEMINI_FLASH_LITE_DEFAULT_OR = 'google/gemini-3.1-flash-lite'
else:
    GEMINI_FLASH_LITE_DEFAULT = 'gemini-2.5-flash-lite'
    GEMINI_FLASH_LITE_DEFAULT_OR = 'google/gemini-2.5-flash-lite'

# Fact extraction and deduplication adopt Gemini 3.1 Flash Lite immediately,
# then follow its already-scheduled May 2027 replacement.
GRAPHITI_FACT_EXTRACTION_DEFAULT = (
    'gemini-3.5-flash-lite'
    if date.today() >= GEMINI_3_1_FLASH_LITE_SWITCH_DATE
    else 'gemini-3.1-flash-lite'
)
GRAPHITI_FACT_EXTRACTION_DEFAULT_OR = f'google/{GRAPHITI_FACT_EXTRACTION_DEFAULT}'
GRAPHITI_FACT_DEDUPLICATION_DEFAULT = GRAPHITI_FACT_EXTRACTION_DEFAULT
GRAPHITI_FACT_DEDUPLICATION_DEFAULT_OR = GRAPHITI_FACT_EXTRACTION_DEFAULT_OR

# ============================================
# Per-Model Reasoning Settings
# ============================================
# Maps LLM call context to the setting path for that model's reasoning toggle.
# Used by llm.py to look up per-model reasoning state.
# Format: context -> (section, key) in settings.json
#
# To add a new model input with reasoning toggle:
# 1. Add the input ID to js/reasoning-toggle.js modelInputs array
# 2. Add the context mapping here
# 3. The setting will be stored as {section}.{key} (e.g., conversation.chat_model_reasoning)
#
# Multiple contexts can map to the same setting if they use the same model.
REASONING_CONTEXT_SETTINGS = {
    # Conversation models
    'chat': ('conversation', 'chat_model_reasoning'),
    'target_selection': ('conversation', 'target_selection_model_reasoning'),
    'interjection': ('conversation', 'interjection_model_reasoning'),
    'commentary_selection': ('conversation', 'commentary_model_reasoning'),
    'input_correction': ('conversation', 'input_correction_model_reasoning'),

    # Memory models - primary contexts
    'chapter': ('memory', 'chapter_model_reasoning'),
    'prose': ('memory', 'prose_model_reasoning'),
    'graphiti': ('memory', 'graphiti_model_reasoning'),
    'graphiti_small': ('memory', 'graphiti_small_model_reasoning'),
    'cognis_memory': ('memory', 'graphiti_model_reasoning'),  # Legacy extraction context
    'reranker': ('memory', 'reranker_model_reasoning'),

    # Memory models - additional contexts that use the same models
    'chapter_detection': ('memory', 'chapter_model_reasoning'),      # Uses chapter_model
    'migration_chapter': ('memory', 'chapter_model_reasoning'),      # Uses chapter_model
    'bio_generation': ('memory', 'prose_model_reasoning'),           # Uses prose_model
    'bio_update': ('memory', 'prose_model_reasoning'),               # Uses prose_model
    'episode_generation': ('memory', 'prose_model_reasoning'),       # Uses prose_model
    'memory_prose': ('memory', 'prose_model_reasoning'),             # Uses prose_model
    'search_intent': ('memory', 'reranker_model_reasoning'),         # Uses reranker_model

    # Agents
    'vision': ('agents', 'vision_reasoning'),

    # Owl Post
    'owl_mail_classifier': ('owl_post', 'orchestrator_model_reasoning'),
    'owl_mail_generate': ('owl_post', 'mail_model_reasoning'),
    'owl_mail_summarize': ('owl_post', 'summarize_model_reasoning'),
    'owl_board_generate': ('owl_post', 'board_model_reasoning'),
    'owl_board_reply': ('owl_post', 'board_model_reasoning'),  # Same as board_generate
}

# Maps LLM call context to OpenRouter provider routing settings.
# Provider lists are arrays of OpenRouter provider names, stored next to each model setting.
OPENROUTER_PROVIDER_CONTEXT_SETTINGS = {
    # Conversation models
    'chat': ('conversation', 'chat_model_providers'),
    'target_selection': ('conversation', 'target_selection_model_providers'),
    'interjection': ('conversation', 'interjection_model_providers'),
    'commentary_selection': ('conversation', 'commentary_model_providers'),
    'input_correction': ('conversation', 'input_correction_model_providers'),

    # Memory models
    'chapter': ('memory', 'chapter_model_providers'),
    'prose': ('memory', 'prose_model_providers'),
    'graphiti': ('memory', 'graphiti_model_providers'),
    'graphiti_small': ('memory', 'graphiti_small_model_providers'),
    'cognis_memory': ('memory', 'graphiti_model_providers'),  # Legacy extraction context
    'reranker': ('memory', 'reranker_model_providers'),
    'chapter_detection': ('memory', 'chapter_model_providers'),
    'migration_chapter': ('memory', 'chapter_model_providers'),
    'bio_generation': ('memory', 'prose_model_providers'),
    'bio_update': ('memory', 'prose_model_providers'),
    'episode_generation': ('memory', 'prose_model_providers'),
    'memory_prose': ('memory', 'prose_model_providers'),
    'search_intent': ('memory', 'reranker_model_providers'),

    # Agents
    'vision': ('agents', 'vision.llm.providers'),

    # Owl Post
    'owl_mail_classifier': ('owl_post', 'orchestrator_model_providers'),
    'owl_mail_generate': ('owl_post', 'mail_model_providers'),
    'owl_mail_summarize': ('owl_post', 'summarize_model_providers'),
    'owl_board_generate': ('owl_post', 'board_model_providers'),
    'owl_board_reply': ('owl_post', 'board_model_providers'),

    # Commitments
    'location_resolver': ('commitment', 'location_resolver_model_providers'),
}

# Directory constants
SONORUS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(SONORUS_DIR, "data")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
CONFIG_HTML = os.path.join(SONORUS_DIR, "config.html")  # Static web asset at root

# Ensure data directory exists
os.makedirs(DATA_DIR, exist_ok=True)

# Settings file cache (mtime-based invalidation)
_settings_cache = None
_settings_mtime = None

# Dev mode cache (initialized on first access, updated on save_settings)
_dev_mode_cache = None

# Model preset cache (loaded from JSON, used for provider-aware upgrade fixes)
_MODEL_PRESETS = None
_MODEL_FIELDS = None
_MODEL_PROVIDER_ROUTES = None

# Track which provider preset fixes have been logged (to avoid spam on repeated load_settings calls)
_logged_preset_fixes = set()

DEPRECATED_MODEL_REPLACEMENTS = {
    "x-ai/grok-4.1-fast": "google/gemini-3.1-flash-lite",
    "gemini-3.1-flash-lite-preview": "gemini-3.1-flash-lite",
    "google/gemini-3.1-flash-lite-preview": "google/gemini-3.1-flash-lite",
    "gemini-3-flash": "gemini-3-flash-preview",
    "google/gemini-3-flash": "google/gemini-3-flash-preview",
}
DEPRECATED_MODEL_MIGRATION_KEY = "deprecated_models_to_current_gemini_defaults_v3"
GRAPHITI_FACT_EXTRACTION_MIGRATION_KEY = "graphiti_fact_extraction_to_gemini_3_1_flash_lite_v1"
GRAPHITI_FACT_DEDUPLICATION_MIGRATION_KEY = "graphiti_fact_deduplication_to_gemini_3_1_flash_lite_v1"
INWORLD_TTS_2_DEFAULT_MIGRATION_KEY = "inworld_tts_default_to_2_v1"
LLAMACPP_SLOT_SAVE_PATH_REMOVAL_MIGRATION_KEY = "llamacpp_slot_save_path_removed_v1"
DATED_MODEL_MIGRATIONS = (
    (
        date(2026, 10, 15),
        "gemini_2_5_flash_lite_deprecation_2026_10_15_v1",
        {
            "gemini-2.5-flash-lite": "gemini-3.1-flash-lite",
            "google/gemini-2.5-flash-lite": "google/gemini-3.1-flash-lite",
        },
    ),
    (
        date(2027, 5, 7),
        "gemini_3_1_flash_lite_deprecation_2027_05_07_v1",
        {
            "gemini-3.1-flash-lite": "gemini-3.5-flash-lite",
            "google/gemini-3.1-flash-lite": "google/gemini-3.5-flash-lite",
        },
    ),
    (
        date(2027, 6, 1),
        "gemini_3_flash_preview_deprecation_2027_06_01_v1",
        {
            "gemini-3-flash-preview": "gemini-3.5-flash",
            "google/gemini-3-flash-preview": "google/gemini-3.5-flash",
        },
    ),
)
OPENAI_RESPONSES_DEFAULT_MIGRATION_KEY = "openai_responses_api_default_enabled_v1"

# ============================================
# VR Preset Overrides
# ============================================
# When VR is active and vr.preset_enabled is True, these settings are overridden
# at read time (load_settings / get_setting). The overrides are ephemeral —
# they never persist to disk and revert the instant VR disconnects.
VR_PRESET_OVERRIDES = {
    'conversation.target_selection_use_crosshair': True,
    'agents.vision.wait_for_capture': False,
    'conversation.player_voice_enabled': False,
    'conversation.companion_move_enabled': False,
    'stt.voice_spells': True,
    'open_mic.enabled': True,
}
# "at most" overrides: only applied when the user's value is greater
VR_PRESET_CEILINGS = {
    'open_mic.turn_timeout_secs': 2.0,
}

# Callback set by vr module after successful init (avoids circular import)
_vr_active_callback = None


def register_vr_callback(callback):
    """Register a callback to check VR active state. Called by vr module after init."""
    global _vr_active_callback
    _vr_active_callback = callback


def _load_model_presets():
    """Load model presets from JSON file (cached)."""
    global _MODEL_PRESETS, _MODEL_FIELDS, _MODEL_PROVIDER_ROUTES
    if _MODEL_PRESETS is None:
        preset_file = os.path.join(DATA_DIR, "model_presets.json")
        try:
            with open(preset_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                # Extract presets (skip underscore-prefixed keys)
                _MODEL_PRESETS = {k: v for k, v in data.items() if not k.startswith('_')}
                _MODEL_FIELDS = data.get('_model_fields', {})
                _MODEL_PROVIDER_ROUTES = data.get('_provider_routes', {})
        except Exception as e:
            print(f"[Settings] Warning: Could not load model presets: {e}")
            _MODEL_PRESETS = {}
            _MODEL_FIELDS = {}
            _MODEL_PROVIDER_ROUTES = {}
    return _MODEL_PRESETS, _MODEL_FIELDS, _MODEL_PROVIDER_ROUTES


def _get_nested_value(obj, path):
    """Get value from nested dict using dot path (e.g., 'conversation.chat_model')."""
    parts = path.split('.')
    value = obj
    for part in parts:
        if isinstance(value, dict) and part in value:
            value = value[part]
        else:
            return None
    return value


def _set_nested_value(obj, path, value):
    """Set value in nested dict using dot path."""
    parts = path.split('.')
    target = obj
    for part in parts[:-1]:
        if part not in target:
            target[part] = {}
        target = target[part]
    target[parts[-1]] = value


def _provider_route_path_for_model_path(path):
    """Return the provider-list setting path paired with a model setting path."""
    if path.endswith('.model'):
        return path.rsplit('.', 1)[0] + '.providers'
    if path.endswith('_model'):
        return path + '_providers'
    return None


def _strip_model_modifier(model_name):
    return model_name.split(':', 1)[0] if isinstance(model_name, str) and ':' in model_name else model_name


def _models_match_for_provider_route(current_model, preset_model):
    """Return True when a saved model is the preset model that owns a provider route."""
    if not isinstance(current_model, str) or not isinstance(preset_model, str):
        return False
    current_model = current_model.strip()
    preset_model = preset_model.strip()
    return current_model == preset_model or _strip_model_modifier(current_model) == _strip_model_modifier(preset_model)


def _backfill_missing_provider_route_presets(settings):
    """Backfill provider routes only for saved models that match shipped presets."""
    provider = settings.get('llm', {}).get('provider', 'gemini')
    presets, fields, provider_routes = _load_model_presets()
    routes = provider_routes.get(provider, {})
    provider_presets = presets.get(provider, {})
    if not routes or not provider_presets:
        return settings

    for key, providers in routes.items():
        model_path = fields.get(key)
        provider_path = _provider_route_path_for_model_path(model_path or '')
        if not model_path or not provider_path:
            continue
        if _get_nested_value(settings, provider_path) is not None:
            continue
        current_model = _get_nested_value(settings, model_path)
        preset_model = provider_presets.get(key)
        if _models_match_for_provider_route(current_model, preset_model):
            _set_nested_value(settings, provider_path, list(providers))

    return settings


def _has_provider_prefix(model_name):
    """Check if model name has provider prefix (e.g., 'google/gemini')."""
    if not model_name or not isinstance(model_name, str):
        return False
    return '/' in model_name


def _apply_provider_presets(settings, provider):
    """
    Apply provider-specific model presets for fields that don't match the provider's pattern.

    Detection Logic:
    - OpenRouter models MUST contain '/' (provider/model format)
    - Gemini models MUST NOT contain '/'
    - If pattern doesn't match, apply provider preset

    This catches upgrade mismatches where new fields got Gemini defaults on non-Gemini providers.
    """
    presets, fields, provider_routes = _load_model_presets()

    # Skip if no presets or provider not found
    if not presets or provider not in presets:
        return

    # Copy provider presets to avoid mutating the cache
    provider_presets = presets[provider].copy()

    # Apply Gemini chat default date logic (always, to ensure consistency)
    if provider == 'gemini':
        provider_presets['chat'] = GEMINI_CHAT_DEFAULT
    elif provider == 'openrouter':
        provider_presets['chat'] = GEMINI_CHAT_DEFAULT_OR

    # Keep provider presets current after each dated model deprecation.
    for switch_date, _migration_key, replacements in DATED_MODEL_MIGRATIONS:
        if date.today() < switch_date:
            continue
        for key, model in provider_presets.items():
            if isinstance(model, str):
                provider_presets[key], _changed = _replace_model_id(model, replacements)

    # Check each model field
    for key, path in fields.items():
        current_value = _get_nested_value(settings, path)

        # Skip if field doesn't exist or is None
        if current_value is None:
            continue

        # Skip if not a string
        if not isinstance(current_value, str):
            continue

        current_value = current_value.strip()
        if not current_value:
            continue

        # Detect pattern mismatch
        has_prefix = _has_provider_prefix(current_value)
        needs_correction = False

        if provider == 'openrouter' and not has_prefix:
            # OpenRouter model lacks provider prefix → likely Gemini default
            needs_correction = True
        elif provider == 'gemini' and has_prefix:
            # Gemini model has provider prefix → likely OpenRouter default
            needs_correction = True
        # OpenAI: no validation (as per requirements)

        # Apply correction if needed
        if needs_correction and key in provider_presets:
            new_value = provider_presets[key]
            _set_nested_value(settings, path, new_value)
            provider_route = provider_routes.get(provider, {}).get(key)
            provider_path = _provider_route_path_for_model_path(path)
            if provider_route and provider_path and _get_nested_value(settings, provider_path) in (None, []):
                _set_nested_value(settings, provider_path, list(provider_route))
            # Only log each fix once per session to avoid spam
            fix_key = f"{provider}:{path}"
            if fix_key not in _logged_preset_fixes:
                _logged_preset_fixes.add(fix_key)
                print(f"[Settings] Upgrade fix ({provider}): {path} = {current_value} -> {new_value}")


DEFAULT_SETTINGS = {
    "ui": {
        "simple_mode": True  # Hide advanced config controls by default
    },
    "dev": {
        "enabled": False  # Dev mode - enables debug prints, profiling, F7/F11 hotkeys
    },
    "vr": {
        "preset_enabled": True,  # Apply VR-optimized settings when VR headset is detected
    },
    "server": {
        "enabled": True,  # Master toggle - when off, disables all mod functions except communication
        "auto_open_config": True
    },
    "tts": {
        "provider": "inworld",
        "speed": 1.0,
        "archive_enabled": True,
        "auto_clone": True,
        "npc_temp_modifiers": {"Sirona": 0.2, "ClementineWillardsey": 0.2, "SebastianSallow": 0.2},
        "npc_model_overrides": {"MirabelGarlick": "inworld-tts-1.5-max"},
        "inworld": {"api_url": "https://api.inworld.ai", "api_key": "", "model": "inworld-tts-2", "temperature": 1.1, "speaking_rate": 1.0, "emote_passthrough": True, "dynamic_delivery": True},
        "elevenlabs": {"api_url": "https://api.elevenlabs.io", "api_key": "", "plan": "creator", "model": "eleven_v3", "stability": 0.5, "similarity_boost": 0.75, "sample_rate": 24000},
        "openai": {"api_key": "", "model": "tts-1", "voice": "alloy", "speed": 1.0},
        "pocket": {"device": "cpu", "temperature": 0.7, "lsd_steps": 1, "eos_threshold": -4.0, "cache_size": 50},
        "omnivoice": {"device": "auto", "num_steps": 32, "first_sentence_steps": 24, "guidance_scale": 2.0, "apply_smoothing_eq": True},
        "omnivoice_api": {"api_url": "http://127.0.0.1:8000", "api_key": "", "num_steps": 32, "first_sentence_steps": 24, "guidance_scale": 2.0, "apply_smoothing_eq": True, "sample_rate": 48000},
        "voxcpm": {"target_rtfx": 0.9, "inference_timesteps": 7, "gpu_yield_interval": 2, "cfg_value": 2.0, "min_yield_ms": 1.0, "max_yield_ms": 50.0}
    },
    "llm": {
        "provider": "gemini",
        "api_key": "",  # Legacy - kept for migration
        "gemini": {
            "api_key": "",
            "reasoning_enabled": True,  # Master switch for per-model reasoning toggles
            "disable_input_correction": False,
            "disable_vision": False,
            "disable_owl_post": False,
            "disable_memory": True
        },
        "openrouter": {
            "api_key": "",
            "reasoning_enabled": True,  # Master switch for per-model reasoning toggles
            "allow_provider_fallbacks": True,
            "disable_input_correction": False,
            "disable_vision": False,
            "disable_owl_post": False,
            "disable_memory": False
        },
        "openai": {
            "api_key": "",
            "api_url": "",
            "responses_api": True,  # Use Responses API (vs Chat Completions).
            "reasoning_enabled": True,  # Master switch for per-model reasoning toggles
            "disable_input_correction": False,
            "disable_vision": False,
            "disable_owl_post": False,
            "disable_memory": False
        },
        "ollama": {
            "api_key": "",
            "api_url": "https://ollama.com/api/chat",
            "disable_input_correction": True,
            "disable_vision": True,
            "disable_owl_post": True,
            "disable_memory": True
        },
        "llamacpp": {
            "api_key": "",
            "api_url": "http://127.0.0.1:8080/v1",
            "kv_cache_enabled": True,
            "kv_cache_max_entries": 10,
            "disable_input_correction": True,
            "disable_vision": True,
            "disable_owl_post": True,
            "disable_memory": True
        }
    },
    "audio": {
        "volume": 100,
        "narration_volume": 80,
        "spatial": True,
        "reverb": True,
        "camera_offset": 0.0,
        "vr_tracking": True,  # Use VR headset tracking for 3D audio (via UEVR plugin)
        "mute_original": True,
        "pronunciation_replacements": {
            "Accio": "Ackio|/ˈæk.i.oʊ/",
            "O.W.L.s": "Owls",
            "Stupefy": "/ˈstuː.pɪ.faɪ/",
            "Legilimens": "Lehjillihmenz|/lɛˈdʒɪl.ɪ.mɛnz/",
            "Crucio": "Kroosheeoh|/ˈkruː.ʃi.oʊ/",
            "Levioso": "Leveeohso|/ˌlɛv.iˈoʊ.soʊ/",
            "Alohomora": "/ˌæl.oʊ.hoʊˈmɔːr.ə/",
            "Petrificus Totalus": "/pɛˈtrɪf.ɪ.kəs toʊˈtæl.əs/",
            "Ominis": "ominous|/ˈɑː.mə.nəs/",
            "Natsai": "Notsigh|/ˈnɑːt.saɪ/",
            "Onai": "Ohnigh|/oʊˈnaɪ/",
            "Ranrok": "Ran-rock|/ˈræn.rɒk/",
            "Morganach": "Morganakh|/ˈmɔːr.ɡən.ɑːx/",
            "you wound me": "you woond me|you /wuːnd/ me",
            "lead the way": "leed the way|/liːd/ the way"
        }
    },
    "lipsync": {
        "enabled": True,
        "default_scale": 1.0,
        "npc_scales": {
            "DuncanHobhouse": 0.8,
            "ZenoviaNoke": 0.7
        }
    },
    "history": {
        "max_entries": 100,
        "bulk_drop_ratio": 0.25,
        "max_location_entries": 2,  # Max location transitions in AI context
        "max_spell_entries": 3,
        "dedup_window": 5,
        "ambient_dedup_window": 15,
        "track_ambient": True,
        "track_cutscene": True,  # Track cutscene dialogue
        "realistic_memory": True
    },
    "commitments": {
        "enabled": True,  # NPC meeting commitments (schedule overrides)
    },
    "commitment": {
        "location_resolver_model_providers": [],
    },
    "owl_post": {
        "enabled": True,
        "boards_enabled": True,  # Notice board system (NPC threads & replies)
        "orchestrator_model": "",  # Classifier/decisions (empty = use interjection_model)
        "orchestrator_model_reasoning": False,
        "orchestrator_model_providers": [],
        "mail_model": "",          # Letter generation (empty = use chat_model)
        "mail_model_reasoning": False,
        "mail_model_providers": [],
        "board_model": "",         # Board thread/reply generation (empty = use chat_model)
        "board_model_reasoning": False,
        "board_model_providers": [],
        "summarize_model": "",     # Letter summarizer (empty = use orchestrator_model)
        "summarize_model_reasoning": False,
        "summarize_model_providers": [],
        "mail_interval": 180,      # seconds between mail orchestrator checks
        "board_interval": 300,     # seconds between board orchestrator checks
        "conversation_cooldown": 300,  # seconds after conversation before NPC can write
        "delivery_minutes": 20,    # in-game minutes for owl delivery
        "max_board_posts_per_day": 0,  # 0 = unlimited; max threads generated per in-game day
        "custom_characters": [],   # Mail-only custom correspondents
    },
    "memory": {
        "enabled": False,  # Disabled by default until enabled in settings
        "embedding_model": "text-embedding-3-small",  # Vector embedding model for memory search (OpenAI/OpenRouter only)
        "chapter_model": "gpt-4.1-nano",  # Model for chapter detection
        "chapter_model_providers": [],
        "graphiti_model": GRAPHITI_FACT_EXTRACTION_DEFAULT,  # Main model for memory fact extraction
        "graphiti_model_providers": [],
        "graphiti_small_model": GRAPHITI_FACT_DEDUPLICATION_DEFAULT,  # Model for fact merge/dedup decisions
        "graphiti_small_model_providers": [],
        "reranker_model": "gpt-4.1-nano",  # Tiny model for reranking (boolean classifier)
        "reranker_model_providers": [],
        "prose_model": "gpt-4.1-nano",  # Model for prose generation
        "prose_model_providers": [],
        "max_concurrency": 2,  # Max parallel LLM calls during memory processing
        "chapter_entry_threshold": 30,  # Min dialogue entries before triggering chapter detection
        "include_cutscene": True,  # Include cutscene dialogue in chapter memories/indexing
        "whitelisted_npcs_only": False,  # Cost-saving mode: only designated NPCs use long-term memory
        "npc_long_term_memory": {}  # Per-NPC allowlist when whitelisted_npcs_only is enabled
    },
    "commentary": {
        "enabled": True,
        "use_vision": True,
        "aggregation_window_seconds": 4,
        "global_cooldown_seconds": 60,
        "event_cooldowns": {
            "cinematic:end": 30,
            "combat:end": 60,
            "location:change": 180
        },
        "location_revisit_cooldown_game_minutes": 720,
        "same_location_cooldown_seconds": 600,
        "notable_locations": [
            "The Map Chamber",
            "Map Chamber Entrance",
            "Room of Requirement",
            "The Room of Requirement",
            "The Undercroft",
            "Undercroft",
            "Vivarium",
            "Slytherin Common Room",
            "Gryffindor Common Room",
            "Ravenclaw Common Room",
            "Hufflepuff Common Room",
            "Feldcroft",
            "Feldcroft Region"
        ]
    },
    "prompts": {
        "default": "You are {name}, someone in the 1890s wizarding world at Hogwarts or Hogsmeade. Respond in character. Keep responses to 1-3 sentences.\n\n{speech_rules}",
        "world_lore": "",
        "scene_continuation": """Continue this scene. Everything below has already been said. Write the NEW line that comes next — do NOT repeat any line from the scene.

## Characters present
{nearby_str}
{extra_context}
## Scene so far
{dialogue_str}

## Instructions
The last line above has already been spoken. Write the response — the next NEW line of dialogue from whichever character would naturally reply.
{address_rules}
Output exactly three lines:
<Speaker Name>: <short 1 sentence dialogue line>
Replying to: <Character Name or Nobody>
How I know: <1 Sentence Explanation>

- The first line must include a speaker name from `Characters present`, followed by a colon and a short 1 sentence dialogue line.
- The speaker's target must be one of the names in `Characters present`, or `Nobody`.
- Use `Nobody` for self-talk, muttering, or lines not directed at a specific person.
- If the conversation has reached a natural end (farewells, thank-you exchanges, or departing words), output:
Narrator: The conversation pauses.
Replying to: Nobody
How I know: The narrator does not speak to anybody.
- Output nothing else.

## Formatting
 1. Line 1 must be exactly: <Speaker Name>: <short 1 sentence dialogue line>
 2. Line 2 must be exactly: Replying to: <{character_names_pipe}|Nobody>
 3. Line 3 must be exactly one sentence.
 4. Plain text, no markdown.""",
        "event_commentary_selector": """# Role
You are deciding whether a companion should make an unprompted remark about something that just happened in the game world. No conversation is active, so speaking should be rare.

# Context
Player character: {player_name}
Location: {current_location}
Time: {time_of_day}
Time since last unprompted comment: {time_since_last_comment}
Chatter frequency: {frequency_label}
Primary trigger to react to: {primary_event}

Characters who may comment:
{eligible_speakers}

Recent events (newest first):
{recent_events}

Recent dialogue (oldest to newest):
{recent_dialogue}

Examples of locations that may be more comment-worthy in some contexts:
{notable_locations}

# Rules
- Silence is usually correct.
- Treat `Primary trigger to react to` as the main moment. Do not let a routine location change or movement detail override it.
- Judge the moment from the specific character's point of view, not just the event name.
- A place may matter because it is personally meaningful, unfamiliar, or unusually strange for that character.
- Locations like the Map Chamber, Room of Requirement, a Vivarium, the Undercroft, and house common rooms that are not a character's own are often more comment-worthy than ordinary Hogwarts traversal, but only when the current moment and character perspective support it.
- Ordinary movement through normal Hogwarts spaces should usually get no comment.
- If a cinematic just ended and recent cutscene dialogue is present, treat that dialogue as the immediate thing the companion is reacting to.
- This is a companion remark addressed to {player_name}, not a continuation of a cutscene or a reply to another NPC.
- If anyone speaks, it must be one of the listed characters and they must be speaking to {player_name}.
- Keep the topic hook short and concrete.

# Output
Output exactly these eight lines and nothing else:
Scene: <one short sentence of grounded third-person scene narration>
Special or unusual relevance: <none or one short sentence>
Worth commenting: <yes|no>
Why: <one short sentence>
Who speaks: <None or exact character name>
Directed to: <None or {player_name}>
Topic: <2-5 word hook or none>
Timing: <immediate|considering|contemplation|none>""",
        "interjection_prompt_mode": """Select which NPC should speak next in this scene.

## What Just Happened
{last_speaker_name} said to {last_target_name}: "{last_message}"

## NPCs in This Scene
{nearby_str}

## Conversation History
{dialogue_str}

## Rules
- ONLY select from the NPCs listed above
- Return "0" if the conversation has reached a natural conclusion
- Keep the scene flowing naturally between the participants

## When NO ONE Should Speak (return "0")
CHECK THIS FIRST - if ANY of these apply, return "0":
- {last_speaker_name} directly asked {player_name} a question
- {last_speaker_name} requested {player_name} to do or demonstrate something
- The conversation is clearly waiting for {player_name}'s response

These situations give {player_name} the floor - NPCs should NOT interject.

## Output Format
EXACTLY one of:
- "0" = Scene ends (conversation complete)
- "NpcId>NpcId" = NPC speaks to another NPC{player_option}

Output ONLY the result, nothing else. NPC ID must match exactly from list.""",
        "owl_board_rules": "",
        "owl_mail_classifier": "Would {npc_name} have a reason to write to {player_name}? Consider: unresolved topics, promises, emotional follow-ups, sharing something new, or friendly correspondence.\n\nReply with ONLY \"yes\" or \"no\".",
        "owl_mail_letter": "Write a short letter (3-6 sentences) that feels personal and in-character. Include a subject line.\nYou may introduce new topics, ideas, or things happening in your life — but do not fabricate outcomes to conversations that happened. If you asked someone for something and they never confirmed, do not claim they delivered.\nFormat your response as:\nSubject: [subject line]\n\n[letter body]\n\nEnd with a sign-off and your first name (e.g. \"Warmly, Ada\" or \"Until next time, James\"). Choose a sign-off that fits your personality.",
        "owl_board_thread": "Write a board thread with:\n1. A topic post by one of the participants (pick whoever fits best)\n2. 2-4 replies from other participants\n\nEach post should be 1-3 sentences, casual and in-character. The topic should feel organic — gossip, questions, observations, complaints, jokes, study questions, etc.\n\nCharacters only know what they would realistically know. They do not use names or terms for places, people, or things they have no knowledge of. If a character would not know about something, they describe it indirectly.\n\nThe title is written by the posting character — it should read like something they actually wrote on the board, not a summary or label.",
        "owl_board_reply": "Write 2-3 replies from the available responders, reacting to the conversation so far (especially the most recent post). Each reply 1-3 sentences, casual and in-character.",
        "static_bios": {
            "VENDORQuillShop": "Ethel Wigley is the bird-like proprietor of Scrivenshaft's Quill Shop in Hogsmeade. She insists that only fallen feathers are used in the quills she sells. She is known to leave treats for the cats that linger around her shop. She may be related to Gertrude Wigley.",
            "AugustusHill": "Augustus Hill is the shopkeeper of Gladrags Wizardwear in Hogsmeade. He is unrelenting and always responds with positive feedback to any choice of attire. He is the father of Rosie Hill and father-in-law of Otto Dibble.",
            "LottieFeatherbottom": "Lottie Featherbottom is the postmaster at the Hogsmeade Post Office. She is generally welcoming to those who enter, but shows judgement towards messily dressed people, occasionally claiming that some hadn't even bothered to get dressed. She has a low tolerance for wanton use of magic inside the Post Office, threatening to hex perpetrators and warning that the owls, such as Bertram, might attack them if they continued.",
            "ThaddeusTravers": "Thaddeus Travers is a member of the Sacred Twenty-Eight Travers family and the slightly inattentive proprietor of Dervish and Banges in Hogsmeade. He may be related to Ailsa Travers, Torquil Travers, and Sloan Travers.",
            "HerbertFleming": "Herbert Fleming is a 5th year Slytherin student of quiet but deliberate habits, the sort who seems forgettable until one realizes he has been paying attention to everything. Born into a respectable but declining wizarding family from the south coast of England, Herbert was raised with the uneasy knowledge that the Fleming name still opened doors, though fewer each year. At Hogwarts, he has learned to make up the difference with polish, patience, and a talent for being useful to the right people at the right time. He is particularly skilled with Charms and Arithmancy, not because he is naturally brilliant, but because he has the discipline to work a problem until it yields. Herbert dislikes spectacle and avoids duels when possible, preferring clever enchantments, well-timed remarks, and small social debts that can be collected later.\n\nIn the Slytherin common room, Herbert is most often seen half-listening from the edge of a conversation, neatly dressed, pale-haired, and wearing the faintly bored expression of someone trying not to appear too interested. He plays wizard’s chess not out of obsession, but because he enjoys games where temperament matters as much as strategy; unfortunately, his caution can make him predictable, a flaw Imelda Reyes was more than happy to point out after one especially public loss. Herbert bore the insult with stiff dignity, though anyone watching closely might have noticed the color rise in his ears. For all his careful manners, there is a stubborn streak beneath them: Herbert Fleming does not mind losing nearly as much as he minds being underestimated."
        },
        "editor_guidance": {
            "MirabelGarlick": ""
        }
    },
    "agents": {
        "vision": {
            "enabled": True,
            "cooldown_seconds": 5,  # Minimum time between input-triggered captures
            "wait_for_capture": True,  # Wait for vision capture before AI responds
            "wait_timeout_seconds": 5,  # Max time to wait for a capture when wait_for_capture is enabled
            "llm": {
                "model": GEMINI_FLASH_LITE_DEFAULT,
                "temperature": 0.7,
                "max_tokens": 8192,  # High default for reasoning token budgets
                "providers": []
            }
        }
    },
    "conversation": {
        "chat_model": GEMINI_CHAT_DEFAULT,
        "chat_model_providers": [],
        "temperature": 1.0,
        "max_tokens": 8192,  # High default for reasoning token budgets
        "max_turns": 6,
        "player_voice_enabled": True,
        "player_voice_spatial": True,  # 3D spatial audio for player voice (disable for VR mods etc.)
        "player_voice_name": "",  # Override for player voice (leave empty to auto-detect from game)
        "target_selection_model": GEMINI_FLASH_LITE_DEFAULT,
        "target_selection_model_providers": [],
        "speaker_selection_max_tokens": 512,  # Dialogue line + target identification + reasoning
        "target_selection_use_crosshair": True,  # Bypass target LLM - use looked-at NPC directly (falls back to LLM if no NPC in crosshair)
        "interjection_model": GEMINI_FLASH_LITE_DEFAULT,
        "interjection_model_providers": [],
        "commentary_model": GEMINI_FLASH_LITE_DEFAULT,
        "commentary_model_providers": [],
        "commentary_max_tokens": 8192,  # High default for reasoning token budgets
        "commentary_model_reasoning": False,
        "input_correction_model_providers": [],
        # input_correction_enabled: Provider-aware default (JS handles) - True for OpenRouter/OpenAI, False for Gemini
        # input_correction_model: Provider-aware default (JS + model_presets.json handles)
        "sentence_subtitles": True,  # True = update subtitle per-sentence as NPC speaks, False = show full text at once
        "actions_enabled": False,  # Experimental: Allow NPCs to use Join/Leave actions
        "followers_enabled": True,  # Allow NPCs to follow/stop following the player via actions (gated by actions_enabled)
        "gear_context": True,  # Include player gear/attire in NPC context
        "mission_context": True,  # Include current quest info for companion AI
        "auto_mute_ambient": False,  # Auto-mute repeated ambient NPC callouts (blocklist system)
        "companion_callout_block_minutes": 1440,  # Deprecated — kept for backwards compat
        "companion_move_enabled": True,  # Allow voice commands to move companion ("go over there", "get out of the way")
        "companion_follow_distance_m": 2.0,  # How close companion follows (meters). Default 2.0m = 200uu
        "emotes_enabled": True,  # Prompt LLM to emit [emotion] tags for facial animation (provider-agnostic)
        "freeform_emote_tags": True,  # Allow improvised tags; aliases are baseline, memory embeddings are optional
        "attention_meter_enabled": True,  # NPCs notice when player stares at them up close and react
        "attention_cold_approach_enabled": True,  # NPCs react to player approaching without prior conversation (requires attention_meter_enabled)
        "gaze_enabled": True,  # NPCs turn head/eyes toward player during conversations and ambient encounters
        "narration_enabled": False,  # Allow inline *narration* with a separate narrator voice
        "spatial_grounding_enabled": True,  # Keep narrated locations consistent with visual context (requires narration_enabled)
        "narrator_voice": "",  # Voice name for narrator (empty = provider default: PocketONNX "GreyCat", Inworld "Graham")
        "conversation_fpv": True,  # Auto-enable first-person view during conversations
        "conversation_fpv_transition": "normal",  # Fade transition duration for first-person conversations: normal, fast, off
        "conversation_look_at_speaker": True,  # Camera looks at the speaking NPC during conversations
        "followup_nudge": True,  # NPCs follow up if they asked a question and player doesn't respond
        "farewell_line": True,  # NPCs say goodbye before walking away after a conversation goes idle
        "npc_llm_model_overrides": {},  # Per-NPC LLM model overrides (e.g., {"SebastianSallow": "anthropic/claude-sonnet-4"})
    },
    "input": {
        "chat_enabled": True,
        "chat_hotkey": "enter",  # Options: enter, f1-f12
        "stop_hotkey": "delete",  # Hotkey to interrupt all ongoing conversations
        "mode_hotkey": "home",  # Hotkey to cycle conversation modes (default/1to1/continuous)
        "fpv_hotkey": "insert",  # Hotkey to toggle first-person view
        "owlpost_hotkey": "backquote",  # Hotkey to toggle Owl Post overlay (`, ~, f1-f12, home, end, insert)
        "idle_timeout_minutes": 20,  # 0 = disabled, otherwise AI stops after X minutes of no movement
        "preview_lock": True  # Lock NPC in place while typing/speaking (before sending message)
    },
    "time_dilation": {
        "enabled": True,  # Master toggle for time dilation control
        # Rates as multipliers of realtime:
        # 1.0 = 1:1 realtime, 3.0 = 3x faster, 0.5 = half speed
        "day_rate": 3.0,  # Time rate during day - 3x realtime
        "night_rate": 3.0,  # Time rate during night - 3x realtime
        "day_start_hour": 6,  # Hour when day begins (0-23)
        "night_start_hour": 18  # Hour when night begins (0-23)
    },
    "stt": {
        "provider": "none",  # "none" | "deepgram" | "whisper" | "parakeet" | "canary" | "moonshine"
        "hotkey": "middle_mouse",
        "voice_spells": True,  # Cast spells by saying their names
        "spell_detection_threshold": 0.8,  # Wakeword confidence threshold (0.0-1.0)
        "mic_gain_db": 0,  # Microphone gain boost in dB (0 = no boost, max 20)
        "sample_rate": 16000,
        "channels": 1,
        "deepgram": {
            "api_key": "",
            "model": "nova-3",
            "language": "en-US",
            "model_improvement": False
        },
        "whisper": {
            "api_key": "",  # Falls back to llm.api_key if empty
            "api_url": "https://api.openai.com/v1",
            "model": "whisper-1",
            "language": ""  # Empty for auto-detect
        },
        "parakeet": {},  # Local ONNX model, no configuration needed
        "canary": {},  # Local ONNX model, no configuration needed
        "moonshine": {}  # Local ONNX model, no configuration needed
    },
    "open_mic": {
        "enabled": False,  # Toggle open mic mode (continuous VAD-based listening)
        "vad_threshold": 0.5,  # VAD sensitivity (0.0-1.0, higher = less sensitive)
        "turn_timeout_secs": 3.0,  # Fallback silence timeout before forcing turn complete
        "pre_speech_ms": 1000,  # Audio buffer before VAD trigger to include (1 second)
        "utterance_end_ms": 500  # Silence duration before checking if done speaking
    },
    "performance": {
        "loop_interval_ms": 100  # 100-1000ms, lower = more responsive, higher = better FPS
    },
    "game_mods": {
        "house_points": {
            "context_enabled": True,   # NPCs know house points standings
            "teacher_actions": True    # Teachers can award/deduct points
        },
        "npc_schedule": {
            "context_enabled": True,           # NPCs know class schedules
            "notifications_enabled": True      # In-game notifications on period changes
        }
    }
}


def deep_merge(base, override):
    """Deep merge override into base dict"""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


LLM_PROVIDER_FEATURE_DISABLE_KEYS = {
    "input_correction": "disable_input_correction",
    "vision": "disable_vision",
    "owl_post": "disable_owl_post",
    "memory": "disable_memory",
}


def is_llm_provider_feature_disabled(feature, settings=None):
    """Return True when the active LLM provider gates off an auxiliary feature."""
    if settings is None:
        settings = load_settings()

    if feature == "owl_post":
        # Existing provider hard gate: Gemini cannot safely run Owl Post/memory-style flows.
        provider = settings.get("llm", {}).get("provider", "gemini")
        if provider == "gemini":
            return True

    if feature not in LLM_PROVIDER_FEATURE_DISABLE_KEYS:
        return False

    llm_settings = settings.get("llm", {})
    provider = llm_settings.get("provider", "gemini")
    provider_settings = llm_settings.get(provider, {})
    key = LLM_PROVIDER_FEATURE_DISABLE_KEYS[feature]
    default = DEFAULT_SETTINGS.get("llm", {}).get(provider, {}).get(key, False)
    return provider_settings.get(key, default) is True


def _normalize_enabled_npc_ids(memory_settings):
    """Return the saved long-term-memory allowlist as normalized NPC ids."""
    enabled = set()
    raw_allowlist = memory_settings.get('npc_long_term_memory', {})
    if not isinstance(raw_allowlist, dict):
        return enabled

    for npc_id, is_enabled in raw_allowlist.items():
        if is_enabled is True and npc_id:
            enabled.add(str(npc_id).strip().lower())
    return enabled


def _is_effective_memory_enabled_for_npc(settings, npc_id):
    """Check effective long-term memory state from raw saved settings."""
    if not npc_id or str(npc_id).strip().lower() == 'player':
        return False

    memory_settings = settings.get('memory', {})
    if not memory_settings.get('enabled', False):
        return False

    if not memory_settings.get('whitelisted_npcs_only', False):
        return True

    return str(npc_id).strip().lower() in _normalize_enabled_npc_ids(memory_settings)


def _backup_settings_for_split_migration():
    """Write a backup of settings.json before the static_bios split migration."""
    if not os.path.exists(SETTINGS_FILE):
        return None

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(DATA_DIR, f"settings.pre_static_bios_split.{timestamp}.json")
    try:
        shutil.copy2(SETTINGS_FILE, backup_path)
        return backup_path
    except Exception as exc:
        print(f"[Settings] Warning: Failed to back up pre-split settings: {exc}")
        return None


def _migrate_character_bios(settings):
    """Split legacy overloaded character text into static_bios and editor_guidance."""
    prompts = settings.get('prompts')
    if not isinstance(prompts, dict):
        return settings, False

    if 'static_bios' in prompts:
        return settings, False

    overloaded = None
    source_key = None

    if isinstance(prompts.get('editor_guidance'), dict) and prompts.get('editor_guidance'):
        overloaded = prompts.get('editor_guidance')
        source_key = 'editor_guidance'
    elif isinstance(prompts.get('bios'), dict) and prompts.get('bios'):
        overloaded = prompts.get('bios')
        source_key = 'bios'

    if not overloaded:
        return settings, False

    static_bios = {}

    for npc_id, text in overloaded.items():
        if not isinstance(text, str):
            continue
        cleaned = text.strip()
        if not cleaned:
            continue

        if str(npc_id).strip() == 'Player':
            static_bios['Player'] = cleaned
        else:
            static_bios[npc_id] = cleaned

    prompts['static_bios'] = static_bios
    prompts['editor_guidance'] = {}
    if source_key == 'bios':
        prompts.pop('bios', None)
    settings['prompts'] = prompts
    return settings, True


def _replace_deprecated_model_values(value):
    """Recursively replace deprecated model IDs in saved settings."""
    if isinstance(value, dict):
        changed = False
        for key, child in list(value.items()):
            new_child, child_changed = _replace_deprecated_model_values(child)
            if child_changed:
                value[key] = new_child
                changed = True
        return value, changed

    if isinstance(value, list):
        changed = False
        for index, child in enumerate(value):
            new_child, child_changed = _replace_deprecated_model_values(child)
            if child_changed:
                value[index] = new_child
                changed = True
        return value, changed

    if isinstance(value, str):
        replacement = DEPRECATED_MODEL_REPLACEMENTS.get(value.strip())
        if replacement and value != replacement:
            return replacement, True

    return value, False


def _replace_model_id(model, replacements):
    """Replace an exact model ID while preserving OpenRouter suffixes such as :nitro."""
    stripped = model.strip()
    replacement = replacements.get(stripped)
    if replacement:
        return replacement, model != replacement

    for deprecated, current in replacements.items():
        if stripped.startswith(f"{deprecated}:"):
            replacement = f"{current}{stripped[len(deprecated):]}"
            return replacement, model != replacement

    return model, False


def _replace_model_values(value, replacements):
    """Recursively replace exact saved model IDs using a supplied mapping."""
    if isinstance(value, dict):
        changed = False
        for key, child in list(value.items()):
            new_child, child_changed = _replace_model_values(child, replacements)
            if child_changed:
                value[key] = new_child
                changed = True
        return value, changed

    if isinstance(value, list):
        changed = False
        for index, child in enumerate(value):
            new_child, child_changed = _replace_model_values(child, replacements)
            if child_changed:
                value[index] = new_child
                changed = True
        return value, changed

    if isinstance(value, str):
        return _replace_model_id(value, replacements)

    return value, False


def _migrate_deprecated_models(settings):
    """One-time migration for deprecated saved model IDs."""
    migrations = settings.setdefault('migrations', {})
    if migrations.get(DEPRECATED_MODEL_MIGRATION_KEY):
        return settings, False

    settings, changed = _replace_deprecated_model_values(settings)
    settings.setdefault('migrations', {})[DEPRECATED_MODEL_MIGRATION_KEY] = True
    return settings, changed or True


def _migrate_dated_models(settings, current_date=None):
    """Apply each due model deprecation once, allowing later manual overrides."""
    current_date = current_date or date.today()
    changed = False
    applied = []

    for switch_date, migration_key, replacements in DATED_MODEL_MIGRATIONS:
        migrations = settings.get('migrations', {})
        if current_date < switch_date or migrations.get(migration_key):
            continue

        settings, _models_changed = _replace_model_values(settings, replacements)
        settings.setdefault('migrations', {})[migration_key] = True
        changed = True
        applied.append(migration_key)

    return settings, changed, applied


def _migrate_graphiti_fact_extraction_model(settings):
    """Set the provider-appropriate fact extraction model exactly once."""
    migrations = settings.get('migrations', {})
    if migrations.get(GRAPHITI_FACT_EXTRACTION_MIGRATION_KEY):
        return settings, False

    provider = settings.get('llm', {}).get('provider', 'gemini')
    if provider not in ('gemini', 'openrouter'):
        return settings, False

    memory = settings.setdefault('memory', {})
    if provider == 'openrouter':
        memory['graphiti_model'] = GRAPHITI_FACT_EXTRACTION_DEFAULT_OR
        memory['graphiti_model_providers'] = ['google-ai-studio', 'google-vertex']
    else:
        memory['graphiti_model'] = GRAPHITI_FACT_EXTRACTION_DEFAULT
        memory['graphiti_model_providers'] = []

    settings.setdefault('migrations', {})[GRAPHITI_FACT_EXTRACTION_MIGRATION_KEY] = True
    return settings, True


def _migrate_graphiti_fact_deduplication_model(settings):
    """Set the provider-appropriate fact deduplication model exactly once."""
    migrations = settings.get('migrations', {})
    if migrations.get(GRAPHITI_FACT_DEDUPLICATION_MIGRATION_KEY):
        return settings, False

    provider = settings.get('llm', {}).get('provider', 'gemini')
    if provider not in ('gemini', 'openrouter'):
        return settings, False

    memory = settings.setdefault('memory', {})
    if provider == 'openrouter':
        memory['graphiti_small_model'] = GRAPHITI_FACT_DEDUPLICATION_DEFAULT_OR
        memory['graphiti_small_model_providers'] = ['google-ai-studio', 'google-vertex']
    else:
        memory['graphiti_small_model'] = GRAPHITI_FACT_DEDUPLICATION_DEFAULT
        memory['graphiti_small_model_providers'] = []

    settings.setdefault('migrations', {})[GRAPHITI_FACT_DEDUPLICATION_MIGRATION_KEY] = True
    return settings, True


def _migrate_openai_responses_default(settings):
    """Enable Responses API by default for direct OpenAI while preserving custom endpoints."""
    migrations = settings.setdefault('migrations', {})
    if migrations.get(OPENAI_RESPONSES_DEFAULT_MIGRATION_KEY):
        return settings, False

    openai_settings = settings.setdefault('llm', {}).setdefault('openai', {})
    api_url = (openai_settings.get('api_url') or '').strip().lower()
    is_direct_openai = api_url == '' or 'openai.com' in api_url
    changed = False

    if is_direct_openai:
        if openai_settings.get('responses_api') is not True:
            openai_settings['responses_api'] = True
            changed = True
        migrations[OPENAI_RESPONSES_DEFAULT_MIGRATION_KEY] = True
        return settings, changed or True

    return settings, False


def _migrate_inworld_tts_2_default(settings):
    """Move the old Inworld default model to TTS 2 exactly once."""
    migrations = settings.setdefault('migrations', {})
    if migrations.get(INWORLD_TTS_2_DEFAULT_MIGRATION_KEY):
        return settings, False

    inworld_settings = settings.setdefault('tts', {}).setdefault('inworld', {})
    if inworld_settings.get('model', 'inworld-tts-1.5-max') == 'inworld-tts-1.5-max':
        inworld_settings['model'] = 'inworld-tts-2'
        migrations[INWORLD_TTS_2_DEFAULT_MIGRATION_KEY] = True
        return settings, True

    migrations[INWORLD_TTS_2_DEFAULT_MIGRATION_KEY] = True
    return settings, True


def _migrate_llamacpp_slot_save_path(settings):
    """Remove the obsolete Sonorus-local slot snapshot cleanup path once."""
    migrations = settings.get('migrations', {})
    if migrations.get(LLAMACPP_SLOT_SAVE_PATH_REMOVAL_MIGRATION_KEY):
        return settings, False

    llamacpp_settings = settings.get('llm', {}).get('llamacpp', {})
    if 'kv_cache_slot_save_path' not in llamacpp_settings:
        return settings, False

    llamacpp_settings.pop('kv_cache_slot_save_path')
    settings.setdefault('migrations', {})[LLAMACPP_SLOT_SAVE_PATH_REMOVAL_MIGRATION_KEY] = True
    return settings, True


def load_settings(raw=False):
    """Load settings from JSON file, cached by file mtime.

    Args:
        raw: If True, return saved values only (for config API / saving).
             If False (default), apply ephemeral VR preset overrides.
    """
    global _settings_cache, _settings_mtime
    try:
        if os.path.exists(SETTINGS_FILE):
            mtime = os.path.getmtime(SETTINGS_FILE)

            if _settings_cache is not None and _settings_mtime == mtime:
                merged = _settings_cache
            else:
                with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                    settings = json.load(f)

                settings, bios_migrated = _migrate_character_bios(settings)
                if bios_migrated:
                    backup_path = _backup_settings_for_split_migration()
                    if save_settings(settings):
                        print("[Settings] Split legacy character bios into prompts.static_bios + prompts.editor_guidance")
                        if backup_path:
                            print(f"[Settings] Backed up pre-split settings to {backup_path}")
                    else:
                        print("[Settings] Warning: Failed to persist static_bios migration")

                settings, models_migrated = _migrate_deprecated_models(settings)
                if models_migrated:
                    if save_settings(settings):
                        print("[Settings] Migrated deprecated model IDs to current Gemini defaults")
                    else:
                        print("[Settings] Warning: Failed to persist deprecated model migration")

                settings, graphiti_model_migrated = _migrate_graphiti_fact_extraction_model(settings)
                if graphiti_model_migrated:
                    if save_settings(settings):
                        print("[Settings] Updated long-term memory fact extraction model")
                    else:
                        print("[Settings] Warning: Failed to persist fact extraction model migration")

                settings, graphiti_small_model_migrated = _migrate_graphiti_fact_deduplication_model(settings)
                if graphiti_small_model_migrated:
                    if save_settings(settings):
                        print("[Settings] Updated long-term memory fact deduplication model")
                    else:
                        print("[Settings] Warning: Failed to persist fact deduplication model migration")

                settings, dated_models_migrated, dated_migrations = _migrate_dated_models(settings)
                if dated_models_migrated:
                    if save_settings(settings):
                        print(f"[Settings] Applied dated model migrations: {', '.join(dated_migrations)}")
                    else:
                        print("[Settings] Warning: Failed to persist dated model migrations")

                settings, openai_responses_migrated = _migrate_openai_responses_default(settings)
                if openai_responses_migrated:
                    if save_settings(settings):
                        print("[Settings] Applied OpenAI Responses API default for direct OpenAI endpoints")
                    else:
                        print("[Settings] Warning: Failed to persist OpenAI Responses API default migration")

                settings, inworld_tts_migrated = _migrate_inworld_tts_2_default(settings)
                if inworld_tts_migrated:
                    if save_settings(settings):
                        print("[Settings] Applied Inworld TTS 2 default migration")
                    else:
                        print("[Settings] Warning: Failed to persist Inworld TTS 2 default migration")

                settings, llamacpp_path_migrated = _migrate_llamacpp_slot_save_path(settings)
                if llamacpp_path_migrated:
                    if save_settings(settings):
                        print("[Settings] Removed obsolete llama.cpp slot save path setting")
                    else:
                        print("[Settings] Warning: Failed to persist llama.cpp slot save path migration")

                settings = _backfill_missing_provider_route_presets(settings)

                # Merge with defaults to ensure all keys exist
                merged = deep_merge(DEFAULT_SETTINGS.copy(), settings)

                # Apply provider-specific presets for upgrade mismatches
                provider = merged.get('llm', {}).get('provider', 'gemini')
                if provider != 'gemini':  # Only fix non-Gemini (Gemini is the default)
                    _apply_provider_presets(merged, provider)

                _settings_cache = merged
                _settings_mtime = mtime

            # Apply VR preset overrides on a copy (ephemeral, never saved to disk)
            if not raw and _vr_active_callback and _vr_active_callback():
                if merged.get('vr', {}).get('preset_enabled', True):
                    merged = copy.deepcopy(merged)
                    for path, value in VR_PRESET_OVERRIDES.items():
                        _set_nested_value(merged, path, value)
                    for path, ceiling in VR_PRESET_CEILINGS.items():
                        current = _get_nested_value(merged, path)
                        if current is None or current > ceiling:
                            _set_nested_value(merged, path, ceiling)

            return merged
    except Exception as e:
        print(f"[Settings] Error loading: {e}")
    return DEFAULT_SETTINGS.copy()


def save_settings(settings):
    """Save settings to JSON file and update cached state."""
    global _dev_mode_cache, _settings_cache, _settings_mtime
    try:
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(settings, f, indent=2)
        # Update dev mode cache when settings change
        _dev_mode_cache = settings.get('dev', {}).get('enabled', False)
        # Invalidate settings cache so next load_settings re-reads
        _settings_cache = None
        _settings_mtime = None
        return True
    except Exception as e:
        print(f"[Settings] Error saving: {e}")
        return False


def get_setting(path, default=None):
    """Get a setting by dot-notation path (e.g., 'llm.model')"""
    settings = load_settings()
    parts = path.split('.')
    value = settings
    for part in parts:
        if isinstance(value, dict) and part in value:
            value = value[part]
        else:
            return default
    return value


def read_file(name):
    """Read a file from SONORUS_DIR"""
    path = os.path.join(SONORUS_DIR, name)
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return f.read().strip()
    except:
        return ""


def write_file(name, content):
    """Write to file in sonorus directory"""
    path = os.path.join(SONORUS_DIR, name)
    try:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)
    except Exception as e:
        print(f"[ERROR] Failed to write {name}: {e}")


_COMMITMENT_SPOTS_FILE = os.path.join(SONORUS_DIR, "data", "commitment_spots.json")

def load_commitment_spots():
    """Load commitment spots from JSON file."""
    try:
        with open(_COMMITMENT_SPOTS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_commitment_spots(spots):
    """Save commitment spots to JSON file."""
    with open(_COMMITMENT_SPOTS_FILE, 'w', encoding='utf-8') as f:
        json.dump(spots, f, indent=2)


# ============================================
# Dev Mode (cached, updated when settings change)
# ============================================
def is_dev_mode():
    """Check if dev mode is enabled (cached for performance)."""
    global _dev_mode_cache
    if _dev_mode_cache is None:
        settings = load_settings()
        _dev_mode_cache = settings.get('dev', {}).get('enabled', False)
    return _dev_mode_cache


def set_dev_mode(enabled: bool):
    """Update cached dev mode state. Call when settings change."""
    global _dev_mode_cache
    _dev_mode_cache = enabled


def dev_print(*args):
    """Print only when dev mode is enabled."""
    if is_dev_mode():
        print(*args)
