"""
Settings management for Sonorus.
Handles loading, saving, and merging of configuration settings.
"""

import os
import json
from datetime import date

# Gemini 3 Flash - use GA version after March 2026
GEMINI_3_GA_DATE = date(2026, 3, 1)
GEMINI_3_FLASH = 'gemini-3-flash' if date.today() >= GEMINI_3_GA_DATE else 'gemini-3-flash-preview'

# Directory constants
SONORUS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(SONORUS_DIR, "data")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
CONFIG_HTML = os.path.join(SONORUS_DIR, "config.html")  # Static web asset at root

# Ensure data directory exists
os.makedirs(DATA_DIR, exist_ok=True)

DEFAULT_SETTINGS = {
    "server": {
        "auto_open_config": True
    },
    "tts": {
        "provider": "inworld",
        "speed": 1.0,
        "auto_clone": True,
        "inworld": {"api_url": "https://api.inworld.ai", "workspace_id": "", "api_key": "", "language": "EN_US", "model": "inworld-tts-1-max", "temperature": 1.1},
        "elevenlabs": {"api_url": "https://api.elevenlabs.io", "api_key": "", "plan": "creator", "model": "eleven_flash_v2_5", "stability": 0.5, "similarity_boost": 0.75, "sample_rate": 24000},
        "openai": {"api_key": "", "model": "tts-1", "voice": "alloy", "speed": 1.0}
    },
    "llm": {
        "provider": "gemini",
        "api_key": "",  # Legacy - kept for migration
        "gemini": {
            "api_key": "",
            "reasoning_enabled": False
        },
        "openrouter": {
            "api_key": "",
            "reasoning_enabled": False
        },
        "openai": {
            "api_key": "",
            "api_url": "",
            "reasoning_enabled": False
        }
    },
    "audio": {
        "volume": 100,
        "spatial": True,
        "rolloff": 0.5,
        "mute_original": True
    },
    "lipsync": {
        "enabled": True,
        "default_scale": 1.0,
        "npc_scales": {
            "DuncanHobhouse": 0.8,
            "ZenoviaOggspire": 0.7
        }
    },
    "history": {
        "max_entries": 100,
        "dedup_window": 5,
        "ambient_dedup_window": 15,
        "track_ambient": True,
        "max_spell_entries": 3,
        "realistic_memory": True
    },
    "prompts": {
        "default": "You are {name}, someone in the 1890s wizarding world at Hogwarts or Hogsmeade. Respond in character. Keep responses to 1-3 sentences. The user may be using voice input, so interpret the intent behind their words rather than reacting to odd phrasing or apparent misspellings.\n\nVoice Performance: You may use [square bracket tags] sparingly for AUDIBLE vocal effects only. Valid: [sighs], [laughs], [whispers], [shouts], [clears throat], [pause]. NEVER use visual/physical tags like [smile], [nods], [waves], [grins] - these break the voice system. Most responses need no tags.",
        "bios": {
            "Player": "A new fifth-year student at Hogwarts who started late due to mysterious circumstances. Possesses a rare ability to see and wield ancient magic that most wizards cannot perceive. Currently learning to master this power while uncovering secrets about a goblin rebellion and dark wizards seeking the same ancient magic."
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
        "player_voice_enabled": False,
        "player_voice_name": "",  # Override for player voice (leave empty to auto-detect from game)
        "tts_chunking": "none",  # none | sentence
        "target_selection_model": "gemini-2.5-flash-lite",
        "target_selection_max_tokens": 8192,  # High default for reasoning token budgets
        "interjection_model": "gemini-2.5-flash-lite",
        "interjection_max_tokens": 8192,  # High default for reasoning token budgets
        "actions_enabled": False,  # Experimental: Allow NPCs to use Follow/Leave/Stop actions
        "gear_context": True,  # Include player gear/attire in NPC context
        "mission_context": True  # Include current quest info for companion AI
    },
    "input": {
        "chat_enabled": True,
        "chat_hotkey": "enter",  # Options: enter, k, /, ;, ', y, t, `, backquote
        "stop_hotkey": "delete",  # Hotkey to interrupt all ongoing conversations
        "idle_timeout_minutes": 20  # 0 = disabled, otherwise AI stops after X minutes of no movement
    },
    "stt": {
        "provider": "none",  # "none" | "deepgram" | "whisper"
        "hotkey": "middle_mouse",
        "voice_spells": True,  # Cast spells by saying their names
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
        }
    },
    "performance": {
        "loop_interval_ms": 100  # 100-1000ms, lower = more responsive, higher = better FPS
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
            # Merge with defaults to ensure all keys exist
            return deep_merge(DEFAULT_SETTINGS.copy(), settings)
    except Exception as e:
        print(f"[Settings] Error loading: {e}")
    return DEFAULT_SETTINGS.copy()


def save_settings(settings):
    """Save settings to JSON file"""
    try:
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(settings, f, indent=2)
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
