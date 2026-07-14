"""
Inworld TTS Provider

Voice caching and streaming TTS synthesis using Inworld AI API.
Supports multilingual voice management with language-aware caching.
"""
import os
import re
import sys
import time
import base64
import json
from typing import Dict, Optional, Callable

import requests

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from .base import BaseTTSProvider, VoiceCache
from .voice_utils import parse_hashed_voice_name
from .elevenlabs import update_voice_usage, get_lru_voice, remove_voice_usage
from utils.text_utils import localize_audio_tags
from .inworld_ws import InworldWebSocket, WS_AVAILABLE, WS_ENDPOINT

# Canonical tags that Inworld TTS supports (audio tags produce audible sounds).
INWORLD_AUDIO_TAGS = {'laugh', 'sigh', 'breathe', 'cough', 'clear_throat', 'yawn'}

# Variant -> base form mapping (LLM may emit [laughs], [sighing], etc.)
_TAG_NORMALIZE = {
    'laughs': 'laugh', 'laughing': 'laugh',
    'sighs': 'sigh', 'sighing': 'sigh',
    'breathes': 'breathe', 'breathing': 'breathe',
    'coughs': 'cough', 'coughing': 'cough',
    'clears throat': 'clear_throat', 'clearing throat': 'clear_throat',
    'yawns': 'yawn', 'yawning': 'yawn',
    'afraid': 'fearful',
}

_BRACKET_TAG_RE = re.compile(r'\[([^\]]+)\]')


def _filter_inworld_tags(text: str) -> str:
    """Strip unsupported bracket tags, normalize variants to base form.

    [laughs] -> [laugh], [sighing] -> [sigh], [whisper] -> stripped, etc.
    """
    allowed = INWORLD_AUDIO_TAGS

    def _replace(m):
        tag = m.group(1).lower().strip()
        # Normalize variant to base form
        tag = _TAG_NORMALIZE.get(tag, tag)
        if tag in allowed:
            return f'[{tag}]'
        return ''

    filtered = _BRACKET_TAG_RE.sub(_replace, text)
    return re.sub(r'  +', ' ', filtered).strip()


# Parent directory (sonorus/) since this module is in services/tts/
SONORUS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Data directory for config files
from utils.settings import DATA_DIR

# Lazy import event_logger to avoid circular dependencies
_event_logger = None


def _get_event_logger():
    """Lazy import of event_logger"""
    global _event_logger
    if _event_logger is None:
        try:
            import event_logger as el
            _event_logger = el
        except ImportError:
            pass
    return _event_logger


# ============================================
# Configuration
# ============================================
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")


def load_settings():
    """Load settings from JSON file"""
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        print(f"[Inworld] Error loading settings: {e}")
    return {}


def _get_inworld_config():
    """Get Inworld configuration from settings.json, fallback to .env"""
    settings = load_settings()
    tts_settings = settings.get('tts', {})
    inworld_settings = tts_settings.get('inworld', {})

    # Always use game language (from setup) to prevent sync issues
    language = settings.get('setup', {}).get('language', 'EN_US')

    # Avoid "localhost" — DNS/IPv6 resolution can add seconds of latency.
    api_url = inworld_settings.get('api_url', "").strip() or "https://api.inworld.ai"
    api_url = api_url.replace("://localhost", "://127.0.0.1")

    return {
        "api_url": api_url,
        "api_key": inworld_settings.get('api_key', ""),
        "language": language,
        "sample_rate": int(inworld_settings.get('sample_rate', 48000)),
        "model": inworld_settings.get('model', 'inworld-tts-1.5-max'),
        "temperature": float(inworld_settings.get('temperature', 1.1)),
        "speed": float(tts_settings.get('speed', 1.0)),
    }


def _get_auth_header():
    """Build Basic auth header for Inworld API"""
    config = _get_inworld_config()
    api_key = config["api_key"]
    if not api_key:
        raise ValueError("Inworld API key not configured (set in Config Page)")
    # API key is already base64 encoded (username:password format)
    return f"Basic {api_key}"


# Inworld supported language codes (in their expected format)
# Maps base language to Inworld's supported locale format
INWORLD_LANG_MAP = {
    "en": "EN_US",
    "zh": "ZH_CN",
    "nl": "NL_NL",
    "fr": "FR_FR",
    "de": "DE_DE",
    "it": "IT_IT",
    "ja": "JA_JP",
    "ko": "KO_KR",
    "pl": "PL_PL",
    "pt": "PT_BR",
    "ru": "RU_RU",
    "es": "ES_ES",
}


def _to_inworld_lang(game_lang: str) -> str:
    """Convert game language code to Inworld-supported format.
    
    Game uses locale codes like "ES_MX", "DE_DE", "EN_US".
    Inworld expects specific locale codes like "ES_ES", "DE_DE", "EN_US".
    
    Args:
        game_lang: Game language code (e.g., "ES_MX", "EN_US")
        
    Returns:
        Inworld-compatible language code (e.g., "ES_ES", "EN_US")
    """
    # Split at underscore and take first part, lowercase for lookup
    base_lang = game_lang.split("_")[0].lower()
    
    # Map to Inworld's supported locale format
    if base_lang in INWORLD_LANG_MAP:
        return INWORLD_LANG_MAP[base_lang]
    
    print(f"[Inworld] Warning: Language '{game_lang}' -> '{base_lang}' not in supported list, using 'EN_US'")
    return "EN_US"


# ============================================
# Inworld Voice Cache
# ============================================
class InworldVoiceCache(VoiceCache):
    """
    Inworld voice cache - keys by name + language.

    Keys voices by "{displayName}_{langCode}" for language-specific lookup.
    """

    def __init__(self):
        super().__init__()
        self._default_lang = "EN_US"

    def _make_cache_key(self, name: str, lang: Optional[str] = None) -> str:
        """Generate cache key with language suffix."""
        lang = lang or self._default_lang
        return f"{name}_{lang}"

    def load(self) -> bool:
        """Load voices from Inworld API.

        Raises:
            Exception: With specific error message for API failures
        """
        config = _get_inworld_config()
        api_url = config["api_url"].rstrip('/')
        self._default_lang = config["language"]

        url = f"{api_url}/voices/v1/voices"

        headers = {
            "Authorization": _get_auth_header(),
            "Content-Type": "application/json",
        }

        try:
            print("[Inworld] Loading voices from API key workspace...")
            response = requests.get(url, headers=headers, timeout=30)

            if response.status_code == 401:
                print(f"[Inworld] API error: 401 Unauthorized")
                raise Exception("Inworld API key is invalid. Check your API key in TTS settings.")

            if response.status_code == 403:
                print(f"[Inworld] API error: 403 Forbidden")
                raise Exception("Inworld API key does not have access to the Voice API. Check that your API key has Voice API read/write access.")

            if response.status_code == 404:
                print(f"[Inworld] API error: 404 Not Found", url)
                raise Exception("Inworld Voice API endpoint not found. Check your API URL in TTS settings.")

            if response.status_code != 200:
                print(f"[Inworld] API error: {response.status_code} {response.reason}")
                raise Exception(f"Inworld API error: {response.status_code} {response.reason}")

            data = response.json()
            voices = data.get("voices", [])
            self._voices.clear()
            self._by_id.clear()

            # Track duplicates for logging
            duplicates = {}  # key -> list of (display_name, hash)

            # The short Voice API path derives the workspace from the API key.
            # Keep only user-created voices and ignore built-in/system voices.
            workspace_voices = [
                v for v in voices
                if v.get("source") == "IVC"
                or (not v.get("source") and "__" in v.get("voiceId", ""))
            ]
            skipped = len(voices) - len(workspace_voices)
            if skipped > 0:
                print(f"[Inworld] Ignored {skipped} built-in/system voices")

            for voice in workspace_voices:
                display_name = voice.get("displayName", "")
                lang_code = voice.get("langCode", "EN_US")
                voice_id = voice.get("voiceId", "")

                # Parse voice name to extract original name, language suffix, and hash
                # e.g., "PlayerMale_DE_DE_a1b2c3d4" -> ("PlayerMale", "DE_DE", "a1b2c3d4")
                original_name, detected_lang, ref_hash = parse_hashed_voice_name(display_name)

                # Use detected language from name if present, otherwise use API's langCode
                effective_lang = detected_lang or lang_code

                # Store hash in voice dict for later comparison
                if ref_hash:
                    voice["referenceHash"] = ref_hash

                # Build cache key
                key = f"{original_name}_{effective_lang}"

                # Check for duplicates - if key already exists, we need to pick the right one
                if key in self._voices:
                    existing = self._voices[key]
                    existing_hash = existing.get("referenceHash")
                    existing_name = existing.get("displayName", "")

                    # Track for logging - use 'name' (resource name) for deletion, not 'voiceId'
                    # name format: workspaces/{workspace}/voices/{voice}
                    # voiceId format: {workspace}__{voice} (for public TTS API only)
                    if key not in duplicates:
                        duplicates[key] = [(existing_name, existing_hash, existing.get("name"))]
                    duplicates[key].append((display_name, ref_hash, voice.get("name")))

                    # Check which hash matches the current reference file
                    from .voice_utils import find_voice_reference, compute_reference_hash
                    ref_path = find_voice_reference(original_name, "15s", language=effective_lang)
                    if ref_path:
                        current_hash = compute_reference_hash(ref_path)
                        if current_hash:
                            if ref_hash == current_hash and existing_hash != current_hash:
                                # New voice matches current reference, replace existing
                                print(f"[Inworld] Duplicate {key}: keeping {display_name} (hash {ref_hash} matches reference)")
                                self._voices[key] = voice
                                if voice_id:
                                    self._by_id[voice_id] = voice
                            # else: existing matches or neither matches, keep existing
                            continue

                    # No reference file or can't determine - keep first one
                    continue

                self._voices[key] = voice

                # Also index by voiceId
                if voice_id:
                    self._by_id[voice_id] = voice

            # Track duplicates for cleanup by provider
            self._duplicates_to_delete = []
            if duplicates:
                print(f"[Inworld] WARNING: Found {len(duplicates)} duplicate voice(s) in workspace:")
                for key, entries in duplicates.items():
                    kept_voice = self._voices.get(key)
                    kept_display = kept_voice.get("displayName", "unknown") if kept_voice else "unknown"
                    kept_hash = kept_voice.get("referenceHash", "no-hash") if kept_voice else "no-hash"
                    # Use 'name' (resource name) for comparison, not 'voiceId'
                    kept_resource_name = kept_voice.get("name") if kept_voice else None
                    print(f"[Inworld]   {key}: kept={kept_display} (hash={kept_hash})")
                    for display_name, hash_val, resource_name in entries:
                        if resource_name and resource_name != kept_resource_name:
                            print(f"[Inworld]     duplicate: {display_name} (hash={hash_val or 'none'}, name={resource_name})")
                            self._duplicates_to_delete.append((resource_name, display_name))
                        else:
                            print(f"[Inworld]     (kept): {display_name} (hash={hash_val or 'none'}, name={resource_name})")

            self._loaded = True
            print(f"[Inworld] Loaded {len(self._voices)} unique voices (from {len(voices)} total)")
            return True

        except requests.exceptions.RequestException as e:
            print(f"[Inworld] Request failed: {e}")
            raise Exception(f"Cannot connect to Inworld API: {e}")
        except Exception as e:
            # Re-raise our own exceptions, wrap others
            if "Inworld" in str(e):
                raise
            print(f"[Inworld] Failed to load voices: {e}")
            raise Exception(f"Failed to load Inworld voices: {e}")


# ============================================
# Inworld TTS Provider
# ============================================
# Module-level singleton cache
_voice_cache: InworldVoiceCache = None


def _get_voice_cache() -> InworldVoiceCache:
    """Get or create the singleton voice cache."""
    global _voice_cache
    if _voice_cache is None:
        _voice_cache = InworldVoiceCache()
    return _voice_cache


def clear_voice_cache():
    """Clear the module-level voice cache, forcing reload on next use."""
    global _voice_cache
    if _voice_cache is not None:
        print("[Inworld] Clearing voice cache")
        _voice_cache = None


class InworldProvider(BaseTTSProvider):
    """
    Inworld TTS provider with language-aware voice management.

    Features:
    - Language-aware voice caching ("{name}_{lang}")
    - WAV header stripping from stream chunks
    - Word-level timestamps (native from API)
    - Persistent WebSocket connection for low-latency streaming
    - Sentence-level pipelining for LLM→TTS streaming
    """

    def __init__(self):
        self._ws: Optional[InworldWebSocket] = None
        self._ws_lock = __import__('threading').Lock()
        self._last_synthesis_error = ""

    @property
    def name(self) -> str:
        return "Inworld"

    @property
    def ws_connected(self) -> bool:
        """Whether the WebSocket connection is active."""
        return self._ws is not None and self._ws.connected

    def get_config(self) -> Dict:
        return _get_inworld_config()

    def get_sample_rate(self) -> int:
        return _get_inworld_config()["sample_rate"]

    def get_default_language(self) -> Optional[str]:
        return self.get_config().get("language", "EN_US")

    def get_voice_cache(self) -> VoiceCache:
        return _get_voice_cache()

    def on_voice_used(self, voice: Dict) -> None:
        """Track voice usage for LRU deletion."""
        display_name = voice.get("displayName", "")
        tags = voice.get("tags", [])
        if "auto-cloned" in tags and display_name:
            update_voice_usage("inworld", display_name)

    def _record_synthesis_error(self, error: str) -> None:
        self._last_synthesis_error = error or ""

    def should_reclone_after_synthesis_failure(self, voice_id: str) -> bool:
        error = (self._last_synthesis_error or "").lower()
        if not error:
            return False
        return (
            "unknown voice" in error
            or "invalid voice" in error
            or (voice_id and voice_id.lower() in error and "not found" in error)
        )

    # ─── WebSocket lifecycle ───────────────────────────────────────────

    @staticmethod
    def _ws_supported() -> bool:
        """WebSocket is only supported on the base Inworld API URL."""
        config = _get_inworld_config()
        api_url = config.get('api_url', '').rstrip('/')
        return api_url in ('', 'https://api.inworld.ai')

    def connect_websocket(self, log: bool = False):
        """
        Establish persistent WebSocket connection to Inworld TTS API.
        Call this when Inworld is selected as the TTS provider.
        Safe to call multiple times (idempotent).
        Skipped when api_url is not the base Inworld API (use HTTP :stream instead).
        """
        if not WS_AVAILABLE:
            print("[Inworld] WebSocket unavailable (websocket-client not installed)")
            return False

        if not self._ws_supported():
            return False

        with self._ws_lock:
            if self._ws and self._ws.connected:
                return True

            try:
                auth = _get_auth_header()
                config = self.get_config()
                ws_url = config.get('api_url', 'https://api.inworld.ai').rstrip('/')
                # Convert http(s) to ws(s) for WebSocket endpoint
                ws_url = ws_url.replace('https://', 'wss://').replace('http://', 'ws://')
                ws_url = f"{ws_url}/tts/v1/voice:streamBidirectional"

                self._ws = InworldWebSocket(
                    auth_header=auth,
                    ws_url=ws_url,
                    log=log
                )
                self._ws.connect()
                print(f"[Inworld] WebSocket connected")
                return True
            except Exception as e:
                print(f"[Inworld] WebSocket connection failed: {e}")
                self._ws = None
                return False

    def disconnect_websocket(self):
        """
        Close WebSocket connection.
        Call when switching away from Inworld provider or shutting down.
        """
        with self._ws_lock:
            if self._ws:
                try:
                    self._ws.disconnect()
                except Exception as e:
                    print(f"[Inworld] WebSocket disconnect error: {e}")
                self._ws = None
                print("[Inworld] WebSocket disconnected")

    def ensure_websocket(self) -> bool:
        """Ensure WebSocket is connected, connecting if needed."""
        if self._ws and self._ws.connected:
            return True
        return self.connect_websocket()

    def _delete_oldest_cloned_voice(self) -> bool:
        """
        Delete the least recently used cloned voice to make room for a new one.
        Returns True if a voice was deleted, False otherwise.
        """
        try:
            config = self.get_config()
            api_url = config["api_url"].rstrip('/')

            # Fetch current voices from API
            headers = {
                "Authorization": _get_auth_header(),
                "Content-Type": "application/json",
            }
            response = requests.get(f"{api_url}/voices/v1/voices", headers=headers, timeout=30)
            if response.status_code != 200:
                print(f"[Inworld] Failed to list voices for LRU deletion: {response.status_code}")
                return False

            voices = response.json().get("voices", [])

            # Filter to our auto-cloned voices only
            cloned_voices = {}
            for voice in voices:
                tags = voice.get("tags", [])
                if "auto-cloned" in tags:
                    cloned_voices[voice.get("displayName", "")] = voice

            if not cloned_voices:
                print("[Inworld] No cloned voices to delete")
                return False

            lru_name = get_lru_voice("inworld", list(cloned_voices.keys()))
            if not lru_name or lru_name not in cloned_voices:
                print("[Inworld] Could not determine LRU voice")
                return False

            target = cloned_voices[lru_name]
            voice_id = target.get("voiceId", "")

            print(f"[Inworld] Deleting least recently used voice: {lru_name} ({voice_id})")

            if not self.delete_voice(voice_id):
                print(f"[Inworld] Failed to delete LRU voice: {lru_name}")
                return False

            # Remove from cache
            cache = self.get_voice_cache()
            original_name, detected_lang, _ = parse_hashed_voice_name(lru_name)
            cache_key = cache._make_cache_key(original_name, detected_lang or cache._default_lang)
            if cache_key in cache._voices:
                del cache._voices[cache_key]
            if voice_id in cache._by_id:
                del cache._by_id[voice_id]

            remove_voice_usage("inworld", lru_name)

            el = _get_event_logger()
            if el:
                el.log_voice_clone_event(
                    character_name=lru_name,
                    language="unknown",
                    reference_filename="",
                    voice_id=voice_id,
                    status="deleted",
                    error="Auto-deleted least recently used voice (plan limit reached)"
                )

            print(f"[Inworld] Deleted LRU voice: {lru_name}")
            return True

        except Exception as e:
            print(f"[Inworld] Failed to delete LRU voice: {e}")
            return False

    def clone_voice(self, display_name: str, reference_wav_path: str,
                    lang: Optional[str] = None) -> Optional[Dict]:
        """
        Clone a voice from a reference WAV file.
        If the voice limit is reached, auto-deletes the least recently used clone and retries.

        Args:
            display_name: Name for the cloned voice (e.g., "SebastianSallow")
            reference_wav_path: Path to reference WAV file
            lang: Language code (e.g., "EN_US"). Uses config default if None.

        Returns:
            Voice dict on success, None on failure
        """
        config = self.get_config()
        api_url = config["api_url"].rstrip('/')
        if lang is None:
            lang = config["language"]

        if not os.path.exists(reference_wav_path):
            print(f"[Inworld] Reference file not found: {reference_wav_path}")
            return None

        # Read and encode the audio file
        with open(reference_wav_path, "rb") as f:
            audio_data = base64.b64encode(f.read()).decode("utf-8")

        url = f"{api_url}/voices/v1/voices:clone"

        # Convert game language (e.g., "ES_MX") to Inworld format (e.g., "es")
        inworld_lang = _to_inworld_lang(lang)

        payload = {
            "displayName": display_name,
            "langCode": inworld_lang,
            "voiceSamples": [
                {"audioData": audio_data}
            ],
            "description": f"Cloned voice for {display_name} ({lang})",
            "tags": ["hogwarts-legacy", "auto-cloned"],
        }

        headers = {
            "Authorization": _get_auth_header(),
            "Content-Type": "application/json",
        }

        def attempt_clone():
            file_size = os.path.getsize(reference_wav_path)
            print(f"[Inworld] Cloning voice: {display_name} (game: {lang} -> inworld: {inworld_lang}), file size: {file_size / 1024:.1f} KB...")
            resp = requests.post(url, json=payload, headers=headers, timeout=180)

            if resp.status_code != 200:
                error_msg = f"HTTP {resp.status_code}"
                error_body = resp.text[:300] if resp.text else ""
                return None, error_msg, error_body

            data = resp.json()
            voice = data.get("voice", {})
            voice_id = voice.get("voiceId", "")
            if voice_id:
                return voice, None, None
            return None, "Missing voiceId in response", ""

        try:
            voice, error_msg, error_body = attempt_clone()

            if voice:
                voice_id = voice.get("voiceId", "")
                print(f"[Inworld] Voice cloned: {display_name} -> {voice_id}")
                el = _get_event_logger()
                if el:
                    el.log_voice_clone_event(
                        character_name=display_name,
                        language=lang,
                        reference_filename=os.path.basename(reference_wav_path),
                        voice_id=voice_id,
                        status="success"
                    )
                return voice

            # Check if it's a voice limit error
            full_error = f"{error_msg}: {error_body}".lower()
            is_limit_error = any(phrase in full_error for phrase in [
                "voice limit", "maximum", "limit reached", "quota",
                "too many voices", "voice_limit", "max_voices", "clone limit",
                "exceeded", "resource_exhausted",
            ])

            if is_limit_error:
                print(f"[Inworld] Voice limit reached, attempting to delete LRU voice...")
                el = _get_event_logger()
                if el:
                    el.log_voice_clone_event(
                        character_name=display_name,
                        language=lang,
                        reference_filename=os.path.basename(reference_wav_path),
                        status="warning",
                        error=f"Voice limit reached: {error_msg}. Attempting to delete LRU voice."
                    )

                if self._delete_oldest_cloned_voice():
                    print(f"[Inworld] Retrying clone after deletion...")
                    voice, retry_error_msg, retry_error_body = attempt_clone()

                    if voice:
                        voice_id = voice.get("voiceId", "")
                        print(f"[Inworld] Voice cloned after LRU deletion: {display_name} -> {voice_id}")
                        el = _get_event_logger()
                        if el:
                            el.log_voice_clone_event(
                                character_name=display_name,
                                language=lang,
                                reference_filename=os.path.basename(reference_wav_path),
                                voice_id=voice_id,
                                status="success",
                                error="Succeeded after auto-deleting LRU voice"
                            )
                        return voice
                    else:
                        error_msg = retry_error_msg or error_msg
                        error_body = retry_error_body or error_body
                        print(f"[Inworld] Clone still failed after deletion: {error_msg}")
                else:
                    print(f"[Inworld] Could not delete LRU voice")

            # All paths that didn't return — log the error
            print(f"[Inworld] Clone error: {error_msg}")
            if error_body:
                print(f"[Inworld] Details: {error_body}")
            el = _get_event_logger()
            if el:
                el.log_voice_clone_event(
                    character_name=display_name,
                    language=lang,
                    reference_filename=os.path.basename(reference_wav_path),
                    status="error",
                    error=f"{error_msg}: {error_body}"
                )
            return None

        except requests.exceptions.Timeout:
            print(f"[Inworld] Clone timed out after 180s")
            el = _get_event_logger()
            if el:
                el.log_voice_clone_event(
                    character_name=display_name,
                    language=lang,
                    reference_filename=os.path.basename(reference_wav_path),
                    status="error",
                    error="Request timed out after 180 seconds"
                )
            return None
        except requests.exceptions.RequestException as e:
            print(f"[Inworld] Clone request failed: {e}")
            el = _get_event_logger()
            if el:
                el.log_voice_clone_event(
                    character_name=display_name,
                    language=lang,
                    reference_filename=os.path.basename(reference_wav_path),
                    status="error",
                    error=f"Request failed: {str(e)}"
                )
            return None
        except Exception as e:
            print(f"[Inworld] Clone failed: {e}")
            el = _get_event_logger()
            if el:
                el.log_voice_clone_event(
                    character_name=display_name,
                    language=lang,
                    reference_filename=os.path.basename(reference_wav_path),
                    status="error",
                    error=str(e)
                )
            return None

    def delete_voice(self, resource_name: str) -> bool:
        """
        Delete a voice from Inworld workspace.

        Args:
            resource_name: Inworld voice resource name (format: workspaces/{workspace}/voices/{voice})
                          or just the voice name part

        Returns:
            True if deleted successfully, False otherwise
        """
        config = self.get_config()
        api_url = config["api_url"].rstrip('/')

        # Accept either voiceId (workspace__voice) or resource name (workspaces/.../voices/voice)
        # Convert resource name to voiceId if needed
        voice_id = resource_name
        if "/voices/" in resource_name:
            # Extract voice part from resource name and build voiceId
            parts = resource_name.split("/")
            # Format: workspaces/{workspace}/voices/{voice}
            if len(parts) >= 4:
                workspace = parts[1]
                voice_part = parts[3]
                voice_id = f"{workspace}__{voice_part}"

        url = f"{api_url}/voices/v1/voices/{voice_id}"

        headers = {
            "Authorization": _get_auth_header(),
            "Content-Type": "application/json",
        }

        try:
            print(f"[Inworld] Deleting voice: {voice_id}")
            response = requests.delete(url, headers=headers, timeout=30)

            if response.status_code == 200:
                print(f"[Inworld] Voice deleted: {voice_id}")
                # Note: Cache is cleared on next reload, no need to remove individual entries
                return True
            elif response.status_code == 404:
                # Voice already doesn't exist - treat as success
                print(f"[Inworld] Voice already deleted or doesn't exist: {voice_id}")
                return True
            else:
                print(f"[Inworld] Delete failed: {response.status_code} {response.text[:200]}")
                return False

        except requests.exceptions.RequestException as e:
            print(f"[Inworld] Delete request failed: {e}")
            return False
        except Exception as e:
            print(f"[Inworld] Delete failed: {e}")
            return False

    def _resolve_tts_params(self, speaker_id: Optional[str] = None):
        """Resolve common TTS parameters (model, temperature, speed, language)."""
        config = self.get_config()
        settings = load_settings()
        default_model = config.get('model', 'inworld-tts-1.5-max')
        model_id = self.resolve_model_override(default_model, speaker_id)
        base_temperature = config.get('temperature', 1.1)
        speaking_rate = config.get('speed', 1.0)
        language = config.get('language', 'EN_US')

        # Per-NPC temperature modifier
        temp_modifier = 0.0
        if speaker_id:
            npc_temp_mods = settings.get('tts', {}).get('npc_temp_modifiers', {})
            temp_modifier = npc_temp_mods.get(speaker_id, 0.0)
        temperature = min(base_temperature + temp_modifier, 2.0)

        # Audio tag localization
        inworld_settings = settings.get('tts', {}).get('inworld', {})
        localize_tags = inworld_settings.get('localize_audio_tags', True)

        temp_info = f"{temperature:.2f}" + (f" (base {base_temperature:.1f} + mod {temp_modifier:+.2f})" if temp_modifier != 0 else "")
        print(f"[Inworld] Model: {model_id}, Temp: {temp_info}, Speed: {speaking_rate}")

        return {
            'model_id': model_id,
            'temperature': temperature,
            'speaking_rate': speaking_rate,
            'sample_rate': config.get('sample_rate', 48000),
            'language': language,
            'localize_tags': localize_tags,
            'api_url': config.get('api_url', 'https://api.inworld.ai').rstrip('/'),
        }

    def _localize_text(self, text: str, language: str, localize_tags: bool) -> str:
        """Filter unsupported tags, then localize audio tags for non-English."""
        # Strip any bracket tags Inworld doesn't support
        text = _filter_inworld_tags(text)
        if localize_tags and not language.startswith('EN'):
            original = text
            text = localize_audio_tags(text, language)
            if text != original:
                print(f"[Inworld] Localized audio tags for {language}")
        return text

    def synthesize_stream(self, text: str, voice_id: str,
                          on_chunk: Callable[[bytes, Optional[Dict]], None],
                          speaker_id: Optional[str] = None) -> bool:
        """
        Stream TTS synthesis from Inworld API.
        Routes through WebSocket when connected, falls back to HTTP.

        Args:
            text: Text to synthesize
            voice_id: Inworld voice ID (e.g., "workspace__voicename")
            on_chunk: Callback function(pcm_bytes, word_timing)
            speaker_id: Optional speaker ID for per-NPC temp modifier

        Returns:
            True on success, False on error
        """
        self._record_synthesis_error("")
        params = self._resolve_tts_params(speaker_id)
        text = self._localize_text(text, params['language'], params['localize_tags'])

        # Route through WebSocket if connected (reconnect if disconnected)
        if not self.ws_connected:
            self.ensure_websocket()
        if self.ws_connected:
            return self._synthesize_via_ws(text, voice_id, on_chunk, speaker_id, params)

        # Fall back to HTTP streaming
        return self._synthesize_via_http(text, voice_id, on_chunk, speaker_id, params)

    def synthesize_stream_sentences(self, sentences, voice_id: str,
                                     on_chunk: Callable[[bytes, Optional[Dict]], None],
                                     speaker_id: Optional[str] = None,
                                     on_sentence_flushed: Callable = None,
                                     abort_check: Callable = None,
                                     on_voice_switch: Callable = None) -> bool:
        """
        Synthesize sentences one at a time via WebSocket, pipelining audio.
        Each sentence is sent immediately; audio chunks start arriving before
        all sentences are submitted. Falls back to joining sentences and using
        regular synthesize_stream if WebSocket is unavailable.

        Args:
            sentences: Iterable of sentence strings (can be a generator that
                       blocks while waiting for LLM tokens)
            voice_id: Inworld voice ID
            on_chunk: Callback(pcm_bytes, word_alignment) per audio chunk
            speaker_id: Speaker ID for per-NPC temp modifier
            on_sentence_flushed: Optional callback when a sentence's audio completes.
                WARNING: NOT called once per sentence for Inworld WS. Flushes
                are batched — 4 sentences may produce only 2 callbacks. Do NOT
                use this to count sentences or index into per-sentence data.
            on_voice_switch: Optional callback(byte_position, sentence_idx) called
                at voice boundaries in multi-voice mode. Fires AFTER flushing the
                previous voice, so byte_position is accurate.

        Returns:
            True on success, False on error
        """
        self._record_synthesis_error("")
        params = self._resolve_tts_params(speaker_id)

        # Detect multi-voice by peeking at the first item from the generator.
        # If it's a (text, voice_id) tuple, we're in narration mode and need
        # the multi-voice path. Must NOT materialize the generator to preserve
        # streaming latency (generator blocks on LLM tokens).
        _iter = iter(sentences)
        first_item = next(_iter, None)
        if first_item is None:
            return False
        has_multi_voice = isinstance(first_item, tuple)

        import itertools
        # Reconstruct full iterator with the peeked item put back
        full_iter = itertools.chain([first_item], _iter)

        # Lazy localization — applies per-sentence as generator yields
        def localized_gen():
            for item in full_iter:
                if isinstance(item, tuple):
                    text, per_vid = item
                    if text and text.strip():
                        loc = self._localize_text(text.strip(), params['language'], params['localize_tags'])
                        yield (loc, per_vid)
                else:
                    if item and item.strip():
                        yield self._localize_text(item.strip(), params['language'], params['localize_tags'])

        # Try to reconnect WebSocket if disconnected
        if not self.ws_connected:
            self.ensure_websocket()

        # Use WebSocket sentence streaming if connected
        if self.ws_connected:
            print(f"[Inworld] Streaming sentences via WebSocket (multi_voice={has_multi_voice})")
            print(f"[Inworld] Voice ID: {voice_id}")
            try:
                if has_multi_voice:
                    success = self._ws.synthesize_sentences_multi_voice(
                        sentences=localized_gen(),
                        default_voice_id=voice_id,
                        model_id=params['model_id'],
                        temperature=params['temperature'],
                        on_chunk=on_chunk,
                        speaker_id=speaker_id,
                        sample_rate=params['sample_rate'],
                        speed=params['speaking_rate'],
                        on_sentence_flushed=on_sentence_flushed,
                        abort_check=abort_check,
                        on_voice_switch=on_voice_switch,
                    )
                    if not success:
                        self._record_synthesis_error(getattr(self._ws, "last_error", ""))
                    return success
                else:
                    success = self._ws.synthesize_sentences(
                        sentences=localized_gen(),
                        voice_id=voice_id,
                        model_id=params['model_id'],
                        temperature=params['temperature'],
                        on_chunk=on_chunk,
                        speaker_id=speaker_id,
                        sample_rate=params['sample_rate'],
                        speed=params['speaking_rate'],
                        on_sentence_flushed=on_sentence_flushed,
                        abort_check=abort_check
                    )
                    if not success:
                        self._record_synthesis_error(getattr(self._ws, "last_error", ""))
                    return success
            except Exception as e:
                self._record_synthesis_error(str(e))
                print(f"[Inworld] WebSocket sentence streaming failed: {e}")
                import traceback
                traceback.print_exc()
                # Don't fall back - sentences iterator is consumed
                return False

        # Fallback: collect all sentences and synthesize as one block via HTTP
        print(f"[Inworld] WebSocket unavailable, collecting sentences for HTTP")
        all_items = list(localized_gen())
        all_text = " ".join(x if isinstance(x, str) else x[0] for x in all_items)
        if not all_text.strip():
            return False
        return self._synthesize_via_http(all_text, voice_id, on_chunk, speaker_id, params)

    def _synthesize_via_ws(self, text: str, voice_id: str,
                           on_chunk: Callable, speaker_id: Optional[str],
                           params: dict) -> bool:
        """Synthesize via persistent WebSocket connection with sentence pipelining."""
        from utils.text_utils import split_into_sentences_safe

        sentences = split_into_sentences_safe(text)
        use_pipelining = len(sentences) > 1

        if use_pipelining:
            print(f"[Inworld] Synthesizing via WebSocket ({len(sentences)} sentences): {text[:80]}...")
        else:
            print(f"[Inworld] Synthesizing via WebSocket: {text}")
        print(f"[Inworld] Voice ID: {voice_id}")

        stream_start = time.time()
        try:
            if use_pipelining:
                success = self._ws.synthesize_sentences(
                    sentences=sentences,
                    voice_id=voice_id,
                    model_id=params['model_id'],
                    temperature=params['temperature'],
                    on_chunk=on_chunk,
                    speaker_id=speaker_id,
                    sample_rate=params['sample_rate'],
                    speed=params['speaking_rate']
                )
            else:
                success = self._ws.synthesize(
                    text=text,
                    voice_id=voice_id,
                    model_id=params['model_id'],
                    temperature=params['temperature'],
                    on_chunk=on_chunk,
                    speaker_id=speaker_id,
                    sample_rate=params['sample_rate'],
                    speed=params['speaking_rate']
                )

            # Log TTS event
            elapsed = time.time() - stream_start
            el = _get_event_logger()
            if el:
                el.log_tts_event(
                    voice_id=voice_id,
                    text_excerpt=text[:100],
                    audio_bytes=0,  # tracked by WS context
                    text_length=len(text),
                    duration_ms=elapsed * 1000,
                    status="success" if success else "error"
                )

            if not success:
                print(f"[Inworld] WebSocket synthesis failed, falling back to HTTP")
                return self._synthesize_via_http(text, voice_id, on_chunk, speaker_id, params)

            return success
        except Exception as e:
            self._record_synthesis_error(str(e))
            print(f"[Inworld] WebSocket synthesis error: {e}, falling back to HTTP")
            return self._synthesize_via_http(text, voice_id, on_chunk, speaker_id, params)

    def _synthesize_via_http(self, text: str, voice_id: str,
                             on_chunk: Callable, speaker_id: Optional[str],
                             params: dict) -> bool:
        """Original HTTP streaming synthesis (fallback path)."""
        api_url = params['api_url']
        url = f"{api_url}/tts/v1/voice:stream"

        payload = {
            "text": text,
            "voiceId": voice_id,
            "modelId": params['model_id'],
            "audioConfig": {
                "audioEncoding": "LINEAR16",
                "sampleRateHertz": params['sample_rate'],
                "speakingRate": params['speaking_rate'],
            },
            "temperature": params['temperature'],
            "timestampType": "WORD",
        }

        headers = {
            "Authorization": _get_auth_header(),
            "Content-Type": "application/json",
        }

        try:
            print(f"[Inworld] Synthesizing text (HTTP): {text}")
            print(f"[Inworld] Voice ID: {voice_id}")

            response = requests.post(url, json=payload, headers=headers, stream=True, timeout=60)
            print(f"[Inworld] Response status: {response.status_code}")

            if response.status_code != 200:
                print(f"[Inworld] HTTP Error: {response.status_code}")
                error_body = response.text[:500]
                self._record_synthesis_error(error_body)
                print(f"[Inworld] Body: {error_body}")
                return False

            chunks_received = 0
            total_audio_bytes = 0
            stream_start_time = time.time()
            chunk_recv_times = []

            # Stream lines as they arrive
            for line in response.iter_lines():
                if not line:
                    continue

                try:
                    chunk_recv_time = time.time()
                    data = json.loads(line.decode("utf-8"))

                    if "error" in data:
                        print(f"[Inworld] Stream error: {data['error']}")
                        self._record_synthesis_error(str(data['error']))
                        return False

                    result = data.get("result", {})
                    audio_b64 = result.get("audioContent", "")
                    word_alignment = result.get("timestampInfo", {}).get("wordAlignment")

                    # Log emote detection
                    if word_alignment:
                        words = word_alignment.get("words", [])
                        starts = word_alignment.get("wordStartTimeSeconds", [])
                        ends = word_alignment.get("wordEndTimeSeconds", [])
                        for i, word in enumerate(words):
                            if (word.startswith('[') or word.startswith('<') or word.startswith('*') or
                                word.endswith(']') or word.endswith('>') or word.endswith('*')):
                                start_t = starts[i] if i < len(starts) else -1
                                end_t = ends[i] if i < len(ends) else -1
                                print(f"[Inworld] Emote detected: '{word}' at {start_t:.3f}s-{end_t:.3f}s")

                    if audio_b64:
                        audio_bytes = base64.b64decode(audio_b64)
                        raw_size = len(audio_bytes)

                        # Strip WAV header if present (starts with "RIFF")
                        header_type = "RAW"
                        header_size = 0
                        data_pos = -1

                        if audio_bytes[:4] == b'RIFF':
                            header_type = "WAV"
                            data_pos = audio_bytes.find(b'data')
                            if data_pos != -1:
                                header_size = data_pos + 8
                                pcm_bytes = audio_bytes[data_pos + 8:]
                            else:
                                header_size = 44
                                pcm_bytes = audio_bytes[44:]
                        else:
                            pcm_bytes = audio_bytes

                        chunks_received += 1
                        total_audio_bytes += len(pcm_bytes)

                        # Timing diagnostics
                        elapsed = chunk_recv_time - stream_start_time
                        chunk_recv_times.append(chunk_recv_time)
                        inter_chunk_gap = 0
                        if len(chunk_recv_times) > 1:
                            inter_chunk_gap = chunk_recv_time - chunk_recv_times[-2]

                        if header_type == "WAV":
                            print(f"[Inworld] Chunk {chunks_received}: raw={raw_size}, pcm={len(pcm_bytes)}, "
                                  f"header={header_type} (data_pos={data_pos}, stripped={header_size}), "
                                  f"gap={inter_chunk_gap*1000:.0f}ms, elapsed={elapsed:.2f}s")
                        else:
                            print(f"[Inworld] Chunk {chunks_received}: raw={raw_size}, pcm={len(pcm_bytes)}, "
                                  f"header={header_type}, gap={inter_chunk_gap*1000:.0f}ms, elapsed={elapsed:.2f}s")

                        # Validate PCM data integrity
                        if len(pcm_bytes) % 2 != 0:
                            print(f"[Inworld] WARNING: PCM size {len(pcm_bytes)} is ODD (should be even for 16-bit)")

                        if len(pcm_bytes) > 4 and b'RIFF' in pcm_bytes[4:]:
                            embedded_pos = pcm_bytes.find(b'RIFF', 4)
                            print(f"[Inworld] WARNING: Found embedded RIFF header at PCM position {embedded_pos}!")

                        # Feed to audio player
                        on_chunk(pcm_bytes, word_alignment)

                except json.JSONDecodeError as e:
                    print(f"[Inworld] JSON error: {e}")
                    continue

            # Streaming summary
            total_stream_time = time.time() - stream_start_time
            print(f"[Inworld] Total: {chunks_received} chunks, {total_audio_bytes} bytes in {total_stream_time:.2f}s")
            if chunk_recv_times and len(chunk_recv_times) > 1:
                gaps = [chunk_recv_times[i] - chunk_recv_times[i-1] for i in range(1, len(chunk_recv_times))]
                print(f"[Inworld] Inter-chunk gaps: min={min(gaps)*1000:.0f}ms, max={max(gaps)*1000:.0f}ms, avg={sum(gaps)/len(gaps)*1000:.0f}ms")

            # Log TTS event on success
            if chunks_received > 0:
                el = _get_event_logger()
                if el:
                    request_latency_ms = total_stream_time * 1000
                    el.log_tts_event(
                        voice_id=voice_id,
                        text_excerpt=text[:100],
                        audio_bytes=total_audio_bytes,
                        text_length=len(text),
                        duration_ms=request_latency_ms,
                        status="success"
                    )

            return chunks_received > 0

        except requests.exceptions.RequestException as e:
            self._record_synthesis_error(str(e))
            print(f"[Inworld] Request failed: {e}")
            el = _get_event_logger()
            if el:
                el.log_tts_event(
                    voice_id=voice_id,
                    text_excerpt=text[:100],
                    audio_bytes=0,
                    text_length=len(text),
                    status="error",
                    error=f"Request failed: {str(e)}"
                )
            return False
        except Exception as e:
            self._record_synthesis_error(str(e))
            print(f"[Inworld] Synthesis failed: {e}")
            import traceback
            traceback.print_exc()
            el = _get_event_logger()
            if el:
                el.log_tts_event(
                    voice_id=voice_id,
                    text_excerpt=text[:100],
                    audio_bytes=0,
                    text_length=len(text),
                    status="error",
                    error=f"Synthesis failed: {str(e)}"
                )
            return False
