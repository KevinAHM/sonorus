"""
ElevenLabs TTS Provider

Voice caching and streaming TTS synthesis using ElevenLabs API.
Supports voice cloning with LRU deletion when plan limit reached.

Key difference from Inworld: ElevenLabs provides character-level timestamps
that are converted to word-level format for lipsync compatibility.
"""
import os
import sys
import time
import base64
import json
from typing import Dict, Optional, Callable

from dotenv import load_dotenv

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from .base import BaseTTSProvider, VoiceCache

# ElevenLabs SDK
ELEVENLABS_AVAILABLE = False
try:
    from elevenlabs.client import ElevenLabs
    from elevenlabs.types import VoiceSettings
    ELEVENLABS_AVAILABLE = True
except ImportError:
    print("[WARN] elevenlabs SDK not installed - run: pip install elevenlabs")

# Parent directory (sonorus/) since this module is in services/tts/
SONORUS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load .env from sonorus directory
load_dotenv(os.path.join(SONORUS_DIR, ".env"))

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
        print(f"[ElevenLabs] Error loading settings: {e}")
    return {}


def _get_elevenlabs_config():
    """Get ElevenLabs configuration from settings.json, fallback to .env"""
    settings = load_settings()
    tts_settings = settings.get('tts', {})
    elevenlabs_settings = tts_settings.get('elevenlabs', {})

    return {
        "api_url": elevenlabs_settings.get('api_url') or os.getenv("ELEVENLABS_API_URL", "https://api.elevenlabs.io"),
        "api_key": elevenlabs_settings.get('api_key') or os.getenv("ELEVENLABS_API_KEY", ""),
        "model": elevenlabs_settings.get('model') or os.getenv("ELEVENLABS_MODEL", "eleven_flash_v2_5"),
        "stability": float(elevenlabs_settings.get('stability', 0.5)),
        "similarity_boost": float(elevenlabs_settings.get('similarity_boost', 0.75)),
        "sample_rate": int(elevenlabs_settings.get('sample_rate', 24000)),
        "speed": float(tts_settings.get('speed', 1.0)),
    }


# ============================================
# Voice Usage Tracking (LRU for auto-deletion)
# ============================================
VOICE_USAGE_FILE = os.path.join(DATA_DIR, "voice_usage.json")


def _load_voice_usage():
    """Load voice usage timestamps from file"""
    try:
        if os.path.exists(VOICE_USAGE_FILE):
            with open(VOICE_USAGE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        print(f"[VoiceUsage] Error loading: {e}")
    return {}


def _save_voice_usage(data):
    """Save voice usage timestamps to file"""
    try:
        with open(VOICE_USAGE_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[VoiceUsage] Error saving: {e}")


def update_voice_usage(provider, voice_name):
    """Update the last-used timestamp for a voice."""
    data = _load_voice_usage()
    if provider not in data:
        data[provider] = {}
    data[provider][voice_name] = int(time.time())
    _save_voice_usage(data)


def get_lru_voice(provider, existing_voice_names):
    """Get the least recently used voice from a list of existing voices."""
    if not existing_voice_names:
        return None

    data = _load_voice_usage()
    provider_data = data.get(provider, {})

    oldest_name = None
    oldest_time = float('inf')

    for name in existing_voice_names:
        last_used = provider_data.get(name, 0)
        if last_used < oldest_time:
            oldest_time = last_used
            oldest_name = name

    return oldest_name or existing_voice_names[0]


def remove_voice_usage(provider, voice_name):
    """Remove a voice from usage tracking (called when voice is deleted)"""
    data = _load_voice_usage()
    if provider in data and voice_name in data[provider]:
        del data[provider][voice_name]
        _save_voice_usage(data)


# ============================================
# Character-to-Word Timestamp Conversion
# ============================================
def convert_char_to_word_alignment(chars, char_starts, char_ends):
    """
    Convert ElevenLabs character-level timestamps to word-level format.

    ElevenLabs provides character-level timing, but lipsync.py expects
    word-level timing in Inworld format.

    Returns:
        Dict in Inworld word_alignment format or None if input is invalid
    """
    if not chars or not char_starts or not char_ends:
        return None

    words = []
    word_starts = []
    word_ends = []

    current_word = ""
    word_start = None
    word_end = None

    for i, char in enumerate(chars):
        start = char_starts[i] if i < len(char_starts) else 0
        end = char_ends[i] if i < len(char_ends) else start

        if char.isspace() or char in '\n\r\t':
            if current_word:
                words.append(current_word)
                word_starts.append(word_start)
                word_ends.append(word_end)
                current_word = ""
                word_start = None
                word_end = None
        else:
            if word_start is None:
                word_start = start
            current_word += char
            word_end = end

    if current_word:
        words.append(current_word)
        word_starts.append(word_start)
        word_ends.append(word_end)

    if not words:
        return None

    return {
        "words": words,
        "wordStartTimeSeconds": word_starts,
        "wordEndTimeSeconds": word_ends
    }


# ============================================
# ElevenLabs Voice Cache
# ============================================
class ElevenLabsVoiceCache(VoiceCache):
    """
    ElevenLabs voice cache - keys by name only (multilingual).
    """

    def _make_cache_key(self, name: str, lang: Optional[str] = None) -> str:
        """Generate cache key - ignore lang for multilingual provider."""
        return name

    def _get_client(self):
        """Create fresh ElevenLabs client."""
        if not ELEVENLABS_AVAILABLE:
            raise RuntimeError("ElevenLabs SDK not installed")
        config = _get_elevenlabs_config()
        api_key = config["api_key"]
        api_url = config["api_url"].rstrip('/')
        if not api_key:
            raise ValueError("ElevenLabs API key not configured (set in Config Page or .env)")
        return ElevenLabs(api_key=api_key, base_url=api_url)

    def load(self) -> bool:
        """Load voices from ElevenLabs API."""
        try:
            client = self._get_client()
            print("[ElevenLabs] Loading voices...")

            response = client.voices.get_all()

            self._voices.clear()
            self._by_id.clear()

            voices = response.voices

            for voice in voices:
                display_name = voice.name
                voice_id = voice.voice_id

                voice_dict = {
                    "displayName": display_name,
                    "voiceId": voice_id,
                    "category": getattr(voice, 'category', 'unknown'),
                    "labels": getattr(voice, 'labels', {}),
                }

                self._voices[display_name] = voice_dict
                if voice_id:
                    self._by_id[voice_id] = voice_dict

            self._loaded = True
            print(f"[ElevenLabs] Loaded {len(self._voices)} voices")
            return True

        except Exception as e:
            print(f"[ElevenLabs] Failed to load voices: {e}")
            return False


# ============================================
# ElevenLabs TTS Provider
# ============================================
# Module-level singleton cache
_voice_cache: ElevenLabsVoiceCache = None


def _get_voice_cache() -> ElevenLabsVoiceCache:
    """Get or create the singleton voice cache."""
    global _voice_cache
    if _voice_cache is None:
        _voice_cache = ElevenLabsVoiceCache()
    return _voice_cache


class ElevenLabsProvider(BaseTTSProvider):
    """
    ElevenLabs TTS provider with LRU voice management.

    Features:
    - Multilingual voice caching (keys by name only)
    - LRU voice deletion when plan limit reached
    - Character-to-word timestamp conversion
    - Voice usage tracking for LRU decisions
    """

    @property
    def name(self) -> str:
        return "ElevenLabs"

    def get_config(self) -> Dict:
        return _get_elevenlabs_config()

    def get_sample_rate(self) -> int:
        return self.get_config().get("sample_rate", 24000)

    def get_default_language(self) -> Optional[str]:
        return None  # ElevenLabs is multilingual

    def get_voice_cache(self) -> ElevenLabsVoiceCache:
        return _get_voice_cache()

    def on_voice_used(self, voice: Dict) -> None:
        """Track usage for LRU deletion."""
        if voice.get("category") == "cloned":
            update_voice_usage("elevenlabs", voice.get("displayName", ""))

    def _delete_oldest_cloned_voice(self) -> bool:
        """
        Delete the least recently used cloned voice to make room for a new one.
        Returns True if a voice was deleted, False otherwise.
        """
        try:
            cache = self.get_voice_cache()
            client = cache._get_client()

            response = client.voices.get_all()
            voices = response.voices

            cloned_voices = {}
            for voice in voices:
                category = getattr(voice, 'category', '')
                if category == 'cloned':
                    cloned_voices[voice.name] = voice

            if not cloned_voices:
                print("[ElevenLabs] No cloned voices to delete")
                return False

            lru_name = get_lru_voice("elevenlabs", list(cloned_voices.keys()))
            if not lru_name or lru_name not in cloned_voices:
                print("[ElevenLabs] Could not determine LRU voice")
                return False

            target = cloned_voices[lru_name]
            voice_id = target.voice_id
            voice_name = target.name

            print(f"[ElevenLabs] Deleting least recently used voice: {voice_name} ({voice_id})")
            client.voices.delete(voice_id)

            # Remove from cache
            if voice_name in cache._voices:
                del cache._voices[voice_name]
            if voice_id in cache._by_id:
                del cache._by_id[voice_id]

            remove_voice_usage("elevenlabs", voice_name)

            el = _get_event_logger()
            if el:
                el.log_voice_clone_event(
                    character_name=voice_name,
                    language="multilingual",
                    reference_filename="",
                    voice_id=voice_id,
                    status="deleted",
                    error="Auto-deleted least recently used voice (plan limit reached)"
                )

            print(f"[ElevenLabs] Deleted voice: {voice_name}")
            return True

        except Exception as e:
            print(f"[ElevenLabs] Failed to delete LRU voice: {e}")
            el = _get_event_logger()
            if el:
                el.log_voice_clone_event(
                    character_name="unknown",
                    language="multilingual",
                    reference_filename="",
                    status="error",
                    error=f"Failed to delete LRU voice: {str(e)}"
                )
            return False

    def clone_voice(self, display_name: str, reference_wav_path: str,
                    lang: Optional[str] = None) -> Optional[Dict]:
        """
        Clone a voice from a reference WAV file using ElevenLabs IVC.

        If the voice limit is reached, automatically deletes the least recently used
        cloned voice and retries.
        """
        if not os.path.exists(reference_wav_path):
            print(f"[ElevenLabs] Reference file not found: {reference_wav_path}")
            return None

        cache = self.get_voice_cache()

        def attempt_clone():
            """Attempt to clone the voice, returns (voice_dict, error_str)"""
            try:
                client = cache._get_client()
                print(f"[ElevenLabs] Cloning voice: {display_name}...")

                # SDK expects opened file handles, not paths
                with open(reference_wav_path, "rb") as f:
                    voice = client.voices.ivc.create(
                        name=display_name,
                        description=f"Cloned voice for {display_name} (Hogwarts Legacy)",
                        files=[f],
                    )

                voice_dict = {
                    "displayName": display_name,
                    "voiceId": voice.voice_id,
                    "category": "cloned",
                }

                cache._voices[display_name] = voice_dict
                cache._by_id[voice.voice_id] = voice_dict

                print(f"[ElevenLabs] Voice cloned: {display_name} -> {voice.voice_id}")
                return voice_dict, None

            except Exception as e:
                return None, str(e)

        # First attempt
        voice_dict, error = attempt_clone()

        if voice_dict:
            el = _get_event_logger()
            if el:
                el.log_voice_clone_event(
                    character_name=display_name,
                    language="multilingual",
                    reference_filename=os.path.basename(reference_wav_path),
                    voice_id=voice_dict["voiceId"],
                    status="success"
                )
            return voice_dict

        # Check if it's a voice limit error
        error_lower = error.lower() if error else ""
        is_limit_error = any(phrase in error_lower for phrase in [
            "voice limit", "maximum", "limit reached", "quota", "too many voices",
            "voice_limit", "max_voices", "clone limit"
        ])

        if is_limit_error:
            print(f"[ElevenLabs] Voice limit reached, attempting to delete LRU voice...")

            el = _get_event_logger()
            if el:
                el.log_voice_clone_event(
                    character_name=display_name,
                    language="multilingual",
                    reference_filename=os.path.basename(reference_wav_path),
                    status="warning",
                    error=f"Voice limit reached: {error}. Attempting to delete LRU voice."
                )

            if self._delete_oldest_cloned_voice():
                print(f"[ElevenLabs] Retrying clone after deletion...")
                voice_dict, retry_error = attempt_clone()

                if voice_dict:
                    el = _get_event_logger()
                    if el:
                        el.log_voice_clone_event(
                            character_name=display_name,
                            language="multilingual",
                            reference_filename=os.path.basename(reference_wav_path),
                            voice_id=voice_dict["voiceId"],
                            status="success",
                            error="Succeeded after auto-deleting LRU voice"
                        )
                    return voice_dict
                else:
                    error = retry_error or error
                    print(f"[ElevenLabs] Clone still failed after deletion: {error}")
                    el = _get_event_logger()
                    if el:
                        el.log_voice_clone_event(
                            character_name=display_name,
                            language="multilingual",
                            reference_filename=os.path.basename(reference_wav_path),
                            status="error",
                            error=f"Clone failed even after deleting LRU voice: {error}"
                        )
                    return None
            else:
                print(f"[ElevenLabs] Could not delete LRU voice")
                el = _get_event_logger()
                if el:
                    el.log_voice_clone_event(
                        character_name=display_name,
                        language="multilingual",
                        reference_filename=os.path.basename(reference_wav_path),
                        status="error",
                        error=f"Voice limit reached and could not delete LRU voice: {error}"
                    )
                return None
        else:
            print(f"[ElevenLabs] Clone failed: {error}")
            el = _get_event_logger()
            if el:
                el.log_voice_clone_event(
                    character_name=display_name,
                    language="multilingual",
                    reference_filename=os.path.basename(reference_wav_path),
                    status="error",
                    error=error
                )
            return None

    def synthesize_stream(self, text: str, voice_id: str,
                          on_chunk: Callable[[bytes, Optional[Dict]], None]) -> bool:
        """
        Stream TTS synthesis from ElevenLabs API.

        Args:
            text: Text to synthesize
            voice_id: ElevenLabs voice ID
            on_chunk: Callback function(pcm_bytes, word_timing)

        Returns:
            True on success, False on error
        """
        config = self.get_config()
        sample_rate = config["sample_rate"]
        model_id = config["model"]
        stability = config["stability"]
        similarity_boost = config["similarity_boost"]

        output_format = f"pcm_{sample_rate}"

        print(f"[ElevenLabs] Model: {model_id}, Stability: {stability}, Similarity: {similarity_boost}")

        try:
            cache = self.get_voice_cache()
            client = cache._get_client()

            print(f"[ElevenLabs] Synthesizing: {text[:80]}...")
            print(f"[ElevenLabs] Voice ID: {voice_id}")

            chunks_received = 0
            total_audio_bytes = 0
            stream_start_time = time.time()
            chunk_recv_times = []

            # Accumulate characters across chunks for word conversion
            accumulated_chars = []
            accumulated_starts = []
            accumulated_ends = []

            # Use stream_with_timestamps for character-level timing data
            response = client.text_to_speech.stream_with_timestamps(
                voice_id=voice_id,
                output_format=output_format,
                text=text,
                model_id=model_id,
                voice_settings=VoiceSettings(
                    stability=stability,
                    similarity_boost=similarity_boost,
                )
            )

            for chunk in response:
                chunk_recv_time = time.time()

                # stream_with_timestamps returns objects with audio_base_64 (base64 string)
                audio_bytes = None
                if hasattr(chunk, 'audio_base_64') and chunk.audio_base_64:
                    audio_bytes = base64.b64decode(chunk.audio_base_64)

                if audio_bytes and len(audio_bytes) > 0:
                    chunks_received += 1
                    total_audio_bytes += len(audio_bytes)
                    chunk_recv_times.append(chunk_recv_time)

                    elapsed = chunk_recv_time - stream_start_time
                    inter_chunk_gap = 0
                    if len(chunk_recv_times) > 1:
                        inter_chunk_gap = chunk_recv_time - chunk_recv_times[-2]

                    print(f"[ElevenLabs] Chunk {chunks_received}: {len(audio_bytes)} bytes, "
                          f"gap={inter_chunk_gap*1000:.0f}ms, elapsed={elapsed:.2f}s")

                    if len(audio_bytes) % 2 != 0:
                        print(f"[ElevenLabs] WARNING: PCM size {len(audio_bytes)} is ODD")

                    # Process character alignment data
                    word_alignment = None
                    if hasattr(chunk, 'alignment') and chunk.alignment:
                        alignment = chunk.alignment
                        # SDK uses: .characters, .character_start_times_seconds, .character_end_times_seconds
                        if hasattr(alignment, 'characters') and alignment.characters:
                            accumulated_chars.extend(alignment.characters)
                        if hasattr(alignment, 'character_start_times_seconds') and alignment.character_start_times_seconds:
                            accumulated_starts.extend(alignment.character_start_times_seconds)
                        if hasattr(alignment, 'character_end_times_seconds') and alignment.character_end_times_seconds:
                            accumulated_ends.extend(alignment.character_end_times_seconds)

                        word_alignment = convert_char_to_word_alignment(
                            accumulated_chars,
                            accumulated_starts,
                            accumulated_ends
                        )

                    on_chunk(audio_bytes, word_alignment)

            # Streaming summary
            total_stream_time = time.time() - stream_start_time
            print(f"[ElevenLabs] Total: {chunks_received} chunks, {total_audio_bytes} bytes in {total_stream_time:.2f}s")
            if chunk_recv_times and len(chunk_recv_times) > 1:
                gaps = [chunk_recv_times[i] - chunk_recv_times[i-1] for i in range(1, len(chunk_recv_times))]
                print(f"[ElevenLabs] Inter-chunk gaps: min={min(gaps)*1000:.0f}ms, max={max(gaps)*1000:.0f}ms, avg={sum(gaps)/len(gaps)*1000:.0f}ms")

            if chunks_received > 0:
                el = _get_event_logger()
                if el:
                    el.log_tts_event(
                        voice_id=voice_id,
                        text_excerpt=text[:100],
                        audio_bytes=total_audio_bytes,
                        text_length=len(text),
                        duration_ms=total_stream_time * 1000,
                        status="success"
                    )

            return chunks_received > 0

        except Exception as e:
            print(f"[ElevenLabs] Synthesis failed: {e}")
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
