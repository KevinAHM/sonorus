"""
Settings management for Sonorus.
Handles loading, saving, and merging of configuration settings.
"""

import os
import json
from datetime import date

# Gemini 3 Flash - use GA version after June 2026
GEMINI_3_GA_DATE = date(2026, 6, 1)
GEMINI_3_FLASH = 'gemini-3-flash' if date.today() >= GEMINI_3_GA_DATE else 'gemini-3-flash-preview'

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
    'input_correction': ('conversation', 'input_correction_model_reasoning'),

    # Memory models - primary contexts
    'chapter': ('memory', 'chapter_model_reasoning'),
    'prose': ('memory', 'prose_model_reasoning'),
    'graphiti': ('memory', 'graphiti_model_reasoning'),
    'graphiti_small': ('memory', 'graphiti_small_model_reasoning'),
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
}

# Directory constants
SONORUS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(SONORUS_DIR, "data")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
CONFIG_HTML = os.path.join(SONORUS_DIR, "config.html")  # Static web asset at root

# Ensure data directory exists
os.makedirs(DATA_DIR, exist_ok=True)

# Dev mode cache (initialized on first access, updated on save_settings)
_dev_mode_cache = None

# Model preset cache (loaded from JSON, used for provider-aware upgrade fixes)
_MODEL_PRESETS = None
_MODEL_FIELDS = None

# Track which provider preset fixes have been logged (to avoid spam on repeated load_settings calls)
_logged_preset_fixes = set()


def _load_model_presets():
    """Load model presets from JSON file (cached)."""
    global _MODEL_PRESETS, _MODEL_FIELDS
    if _MODEL_PRESETS is None:
        preset_file = os.path.join(DATA_DIR, "model_presets.json")
        try:
            with open(preset_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                # Extract presets (skip underscore-prefixed keys)
                _MODEL_PRESETS = {k: v for k, v in data.items() if not k.startswith('_')}
                _MODEL_FIELDS = data.get('_model_fields', {})
        except Exception as e:
            print(f"[Settings] Warning: Could not load model presets: {e}")
            _MODEL_PRESETS = {}
            _MODEL_FIELDS = {}
    return _MODEL_PRESETS, _MODEL_FIELDS


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
    presets, fields = _load_model_presets()

    # Skip if no presets or provider not found
    if not presets or provider not in presets:
        return

    # Copy provider presets to avoid mutating the cache
    provider_presets = presets[provider].copy()

    # Apply Gemini 3 GA date logic (always, to ensure consistency)
    if provider == 'gemini':
        provider_presets['chat'] = GEMINI_3_FLASH
    elif provider == 'openrouter':
        is_ga = date.today() >= GEMINI_3_GA_DATE
        provider_presets['chat'] = 'google/gemini-3-flash' if is_ga else 'google/gemini-3-flash-preview'

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
            # Only log each fix once per session to avoid spam
            fix_key = f"{provider}:{path}"
            if fix_key not in _logged_preset_fixes:
                _logged_preset_fixes.add(fix_key)
                print(f"[Settings] Upgrade fix ({provider}): {path} = {current_value} -> {new_value}")


DEFAULT_SETTINGS = {
    "dev": {
        "enabled": False  # Dev mode - enables debug prints, profiling, F7/F11 hotkeys
    },
    "server": {
        "enabled": True,  # Master toggle - when off, disables all mod functions except communication
        "auto_open_config": True
    },
    "tts": {
        "provider": "inworld",
        "speed": 1.0,
        "auto_clone": True,
        "npc_temp_modifiers": {"Sirona": 0.2, "ClementineWillardsey": 0.2, "SebastianSallow": 0.2},
        "npc_model_overrides": {"MirabelGarlick": "inworld-tts-1.5-max"},
        "inworld": {"api_url": "https://api.inworld.ai", "workspace_id": "", "api_key": "", "model": "inworld-tts-1.5-max", "temperature": 1.1, "emotion_delivery": False},
        "elevenlabs": {"api_url": "https://api.elevenlabs.io", "api_key": "", "plan": "creator", "model": "eleven_v3", "stability": 0.5, "similarity_boost": 0.75, "sample_rate": 24000},
        "openai": {"api_key": "", "model": "tts-1", "voice": "alloy", "speed": 1.0},
        "pocket": {"device": "cpu", "temperature": 0.7, "lsd_steps": 1, "eos_threshold": -4.0, "cache_size": 50},
        "voxcpm": {"target_rtfx": 0.9, "inference_timesteps": 7, "gpu_yield_interval": 2, "cfg_value": 2.0, "min_yield_ms": 1.0, "max_yield_ms": 50.0}
    },
    "llm": {
        "provider": "gemini",
        "api_key": "",  # Legacy - kept for migration
        "gemini": {
            "api_key": "",
            "reasoning_enabled": True  # Master switch for per-model reasoning toggles
        },
        "openrouter": {
            "api_key": "",
            "reasoning_enabled": True  # Master switch for per-model reasoning toggles
        },
        "openai": {
            "api_key": "",
            "api_url": "",
            "responses_api": False,  # Use Responses API (vs Chat Completions). Backend auto-enables for openai.com URLs.
            "reasoning_enabled": True  # Master switch for per-model reasoning toggles
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
            "Ominis": "/ˈɑː.mɪ.nɪs/",
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
        "fallback": False,  # Use fallback lipsync when alignment unavailable
        "npc_scales": {
            "DuncanHobhouse": 0.8,
            "ZenoviaOggspire": 0.7
        }
    },
    "history": {
        "max_entries": 100,
        "max_location_entries": 2,  # Max location transitions in AI context
        "max_spell_entries": 3,
        "dedup_window": 5,
        "ambient_dedup_window": 15,
        "track_ambient": True,
        "track_cutscene": True,  # Track cutscene dialogue
        "realistic_memory": True
    },
    "commitments": {
        "enabled": False,  # Experimental: NPC meeting commitments (schedule overrides)
    },
    "memory": {
        "enabled": False,  # Disabled by default until enabled in settings
        "chapter_model": "gemini-2.5-flash-lite",  # Model for chapter detection
        "graphiti_model": "gemini-2.5-flash-lite",  # Main model for Graphiti entity extraction
        "graphiti_small_model": "gemini-2.5-flash-lite",  # Smaller model for simpler Graphiti tasks
        "reranker_model": "gemini-2.5-flash-lite",  # Tiny model for reranking (boolean classifier)
        "prose_model": "gemini-2.5-flash-lite",  # Model for prose generation
        "max_concurrency": 2,  # Max parallel LLM calls during memory processing
        "chapter_entry_threshold": 30  # Min dialogue entries before triggering chapter detection
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

Output exactly three lines:
<Speaker Name>: <dialogue line>
Replying to: <Character Name or Nobody>
How I know: <1 Sentence Explanation>

- The first line must include a speaker name from `Characters present`, followed by a colon and the dialogue.
- The speaker's target must be one of the names in `Characters present`, or `Nobody`.
- Use `Nobody` for self-talk, muttering, or lines not directed at a specific person.
- If no one would speak, output:
Narrator: The conversation pauses.
Replying to: Nobody
How I know: The narrator does not speak to anybody.
- Output nothing else.

## Formatting
 1. Line 1 must be exactly: <Speaker Name>: <dialogue line>
 2. Line 2 must be exactly: Replying to: <{character_names_pipe}|Nobody>
 3. Line 3 must be exactly one sentence.
 4. Plain text, no markdown.""",
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
        "editor_guidance": {
            "Player": "A new fifth-year student at Hogwarts who started late due to mysterious circumstances. Possesses a rare ability to see and wield ancient magic that most wizards cannot perceive. Currently learning to master this power while uncovering secrets about a goblin rebellion and dark wizards seeking the same ancient magic.",
            "MirabelGarlick": ""
        }
    },
    "agents": {
        "vision": {
            "enabled": True,
            "cooldown_seconds": 5,  # Minimum time between input-triggered captures
            "wait_for_capture": True,  # Wait for vision capture before AI responds
            "llm": {
                "model": "gemini-2.5-flash-lite",
                "temperature": 0.7,
                "max_tokens": 8192  # High default for reasoning token budgets
            }
        }
    },
    "conversation": {
        "chat_model": GEMINI_3_FLASH,
        "temperature": 1.0,
        "max_tokens": 8192,  # High default for reasoning token budgets
        "max_turns": 6,
        "player_voice_enabled": True,
        "player_voice_spatial": True,  # 3D spatial audio for player voice (disable for VR mods etc.)
        "player_voice_name": "",  # Override for player voice (leave empty to auto-detect from game)
        "target_selection_model": "gemini-2.5-flash-lite",
        "speaker_selection_max_tokens": 512,  # Dialogue line + target identification + reasoning
        "target_selection_use_crosshair": False,  # Bypass target LLM - use looked-at NPC directly (falls back to LLM if no NPC in crosshair)
        "interjection_model": "gemini-2.5-flash-lite",
        # input_correction_enabled: Provider-aware default (JS handles) - True for OpenRouter/OpenAI, False for Gemini
        # input_correction_model: Provider-aware default (JS + model_presets.json handles)
        "sentence_subtitles": True,  # True = update subtitle per-sentence as NPC speaks, False = show full text at once
        "actions_enabled": False,  # Experimental: Allow NPCs to use Join/Leave actions
        "gear_context": True,  # Include player gear/attire in NPC context
        "mission_context": True,  # Include current quest info for companion AI
        "companion_callout_block_minutes": 1440,  # Block repeated companion callouts (1440 = 1 game day)
        "companion_move_enabled": True,  # Allow voice commands to move companion ("go over there", "get out of the way")
        "companion_follow_distance_m": 2.0,  # How close companion follows (meters). Default 2.0m = 200uu
        "narration_enabled": True,  # Allow inline *narration* with a separate narrator voice
        "narrator_voice": ""  # Voice name for narrator (empty = provider default: PocketONNX "GreyCat", Inworld "Graham")
    },
    "input": {
        "chat_enabled": True,
        "chat_hotkey": "enter",  # Options: enter, f1-f12
        "stop_hotkey": "delete",  # Hotkey to interrupt all ongoing conversations
        "mode_hotkey": "home",  # Hotkey to cycle conversation modes (default/1to1/continuous)
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
        }
    }
}


def deep_merge(base, override):
    """Deep merge override into base dict"""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_settings():
    """Load settings from JSON file"""
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                settings = json.load(f)

            # Migration: bios -> editor_guidance
            prompts = settings.get('prompts', {})
            if 'bios' in prompts and 'editor_guidance' not in prompts:
                prompts['editor_guidance'] = prompts.pop('bios')
                settings['prompts'] = prompts
                print("[Settings] Migrated prompts.bios -> prompts.editor_guidance")

            # Merge with defaults to ensure all keys exist
            merged = deep_merge(DEFAULT_SETTINGS.copy(), settings)

            # Apply provider-specific presets for upgrade mismatches
            provider = merged.get('llm', {}).get('provider', 'gemini')
            if provider != 'gemini':  # Only fix non-Gemini (Gemini is the default)
                _apply_provider_presets(merged, provider)

            return merged
    except Exception as e:
        print(f"[Settings] Error loading: {e}")
    return DEFAULT_SETTINGS.copy()


def save_settings(settings):
    """Save settings to JSON file and update cached state."""
    global _dev_mode_cache
    try:
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(settings, f, indent=2)
        # Update dev mode cache when settings change
        _dev_mode_cache = settings.get('dev', {}).get('enabled', False)
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
