"""
Inworld TTS Provider

Voice caching and streaming TTS synthesis using Inworld AI API.
Supports multilingual voice management with language-aware caching.
"""
import os
import sys
import time
import base64
import json
from typing import Dict, Optional, Callable

import requests

from dotenv import load_dotenv

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from .base import BaseTTSProvider, VoiceCache

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
        print(f"[Inworld] Error loading settings: {e}")
    return {}


def _get_inworld_config():
    """Get Inworld configuration from settings.json, fallback to .env"""
    settings = load_settings()
    tts_settings = settings.get('tts', {})
    inworld_settings = tts_settings.get('inworld', {})

    return {
        "api_url": inworld_settings.get('api_url') or os.getenv("INWORLD_API_URL", "https://api.inworld.ai"),
        "workspace_id": inworld_settings.get('workspace_id') or os.getenv("INWORLD_WORKSPACE_ID", ""),
        "api_key": inworld_settings.get('api_key') or os.getenv("INWORLD_API_KEY", ""),
        "language": inworld_settings.get('language') or os.getenv("INWORLD_LANGUAGE", "EN_US"),
        "sample_rate": int(inworld_settings.get('sample_rate', 48000)),
        "model": inworld_settings.get('model', 'inworld-tts-1-max'),
        "temperature": float(inworld_settings.get('temperature', 1.1)),
        "speed": float(tts_settings.get('speed', 1.0)),
    }


def _get_auth_header():
    """Build Basic auth header for Inworld API"""
    config = _get_inworld_config()
    api_key = config["api_key"]
    if not api_key:
        raise ValueError("Inworld API key not configured (set in Config Page or .env)")
    # API key from .env is already base64 encoded (username:password format)
    return f"Basic {api_key}"


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
        workspace = config["workspace_id"]
        api_url = config["api_url"].rstrip('/')
        self._default_lang = config["language"]

        if not workspace:
            print("[Inworld] No workspace ID configured")
            raise Exception("Inworld workspace ID not configured. Set it in TTS settings.")

        url = f"{api_url}/voices/v1/workspaces/{workspace}/voices"

        headers = {
            "Authorization": _get_auth_header(),
            "Content-Type": "application/json",
        }

        try:
            print(f"[Inworld] Loading voices from {workspace}...")
            response = requests.get(url, headers=headers, timeout=30)

            if response.status_code == 401:
                print(f"[Inworld] API error: 401 Unauthorized")
                raise Exception("Inworld API key is invalid. Check your API key in TTS settings.")

            if response.status_code == 403:
                print(f"[Inworld] API error: 403 Forbidden")
                raise Exception("Inworld API key does not have access to this workspace. Check that your API key is valid for this workspace ID.")

            if response.status_code == 404:
                print(f"[Inworld] API error: 404 Not Found")
                raise Exception(f"Inworld workspace '{workspace}' not found. Check your workspace ID in TTS settings.")

            if response.status_code != 200:
                print(f"[Inworld] API error: {response.status_code} {response.reason}")
                raise Exception(f"Inworld API error: {response.status_code} {response.reason}")

            data = response.json()
            voices = data.get("voices", [])
            self._voices.clear()
            self._by_id.clear()

            for voice in voices:
                display_name = voice.get("displayName", "")
                lang_code = voice.get("langCode", "EN_US")
                voice_id = voice.get("voiceId", "")

                # Key by name + language
                key = f"{display_name}_{lang_code}"
                self._voices[key] = voice

                # Also index by voiceId
                if voice_id:
                    self._by_id[voice_id] = voice

            self._loaded = True
            print(f"[Inworld] Loaded {len(voices)} voices")
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
    """

    @property
    def name(self) -> str:
        return "Inworld"

    def get_config(self) -> Dict:
        return _get_inworld_config()

    def get_sample_rate(self) -> int:
        return 48000  # Fixed for Inworld

    def get_default_language(self) -> Optional[str]:
        return self.get_config().get("language", "EN_US")

    def get_voice_cache(self) -> VoiceCache:
        return _get_voice_cache()

    def clone_voice(self, display_name: str, reference_wav_path: str,
                    lang: Optional[str] = None) -> Optional[Dict]:
        """
        Clone a voice from a reference WAV file.

        Args:
            display_name: Name for the cloned voice (e.g., "SebastianSallow")
            reference_wav_path: Path to reference WAV file
            lang: Language code (e.g., "EN_US"). Uses config default if None.

        Returns:
            Voice dict on success, None on failure
        """
        config = self.get_config()
        workspace = config["workspace_id"]
        api_url = config["api_url"].rstrip('/')
        if lang is None:
            lang = config["language"]

        if not workspace:
            print("[Inworld] No workspace ID configured")
            return None

        if not os.path.exists(reference_wav_path):
            print(f"[Inworld] Reference file not found: {reference_wav_path}")
            return None

        # Read and encode the audio file
        with open(reference_wav_path, "rb") as f:
            audio_data = base64.b64encode(f.read()).decode("utf-8")

        url = f"{api_url}/voices/v1/workspaces/{workspace}/voices:clone"

        payload = {
            "displayName": display_name,
            "langCode": lang,
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

        try:
            file_size = os.path.getsize(reference_wav_path)
            print(f"[Inworld] Cloning voice: {display_name} ({lang}), file size: {file_size / 1024:.1f} KB...")

            response = requests.post(url, json=payload, headers=headers, timeout=180)

            if response.status_code != 200:
                error_msg = f"HTTP {response.status_code}"
                error_body = response.text[:300] if response.text else ""
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

            data = response.json()
            voice = data.get("voice", {})
            voice_id = voice.get("voiceId", "")

            if voice_id:
                print(f"[Inworld] Voice cloned: {display_name} -> {voice_id}")

                # Log voice clone event
                el = _get_event_logger()
                if el:
                    el.log_voice_clone_event(
                        character_name=display_name,
                        language=lang,
                        reference_filename=os.path.basename(reference_wav_path),
                        voice_id=voice_id,
                        status="success"
                    )

                # Add to cache
                cache = self.get_voice_cache()
                cache.add(voice, lang)

                return voice
            else:
                print(f"[Inworld] Clone response missing voiceId")
                el = _get_event_logger()
                if el:
                    el.log_voice_clone_event(
                        character_name=display_name,
                        language=lang,
                        reference_filename=os.path.basename(reference_wav_path),
                        status="error",
                        error="Missing voiceId in response"
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

    def synthesize_stream(self, text: str, voice_id: str,
                          on_chunk: Callable[[bytes, Optional[Dict]], None]) -> bool:
        """
        Stream TTS synthesis from Inworld API.

        Args:
            text: Text to synthesize
            voice_id: Inworld voice ID (e.g., "workspace__voicename")
            on_chunk: Callback function(pcm_bytes, word_timing)

        Returns:
            True on success, False on error
        """
        config = self.get_config()
        model_id = config.get('model', 'inworld-tts-1-max')
        temperature = config.get('temperature', 1.1)
        speaking_rate = config.get('speed', 1.0)
        api_url = config.get('api_url', 'https://api.inworld.ai').rstrip('/')

        url = f"{api_url}/tts/v1/voice:stream"

        payload = {
            "text": text,
            "voiceId": voice_id,
            "modelId": model_id,
            "audioConfig": {
                "audioEncoding": "LINEAR16",
                "sampleRateHertz": 48000,
                "speakingRate": speaking_rate,
            },
            "temperature": temperature,
            "timestampType": "WORD",
        }

        print(f"[Inworld] Model: {model_id}, Temp: {temperature}, Speed: {speaking_rate}")

        headers = {
            "Authorization": _get_auth_header(),
            "Content-Type": "application/json",
        }

        try:
            print(f"[Inworld] Synthesizing text: {text}")
            print(f"[Inworld] Voice ID: {voice_id}")

            response = requests.post(url, json=payload, headers=headers, stream=True, timeout=60)
            print(f"[Inworld] Response status: {response.status_code}")

            if response.status_code != 200:
                print(f"[Inworld] HTTP Error: {response.status_code}")
                print(f"[Inworld] Body: {response.text[:500]}")
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
