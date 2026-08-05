"""
OmniVoice API TTS Provider

Remote OmniVoice provider for the standalone omnivoice-api server. This keeps
model inference off the game machine while preserving Sonorus voice cloning,
hash tracking, sentence streaming, and OmniVoice-specific audio handling.
"""
import base64
import functools
import os
import random
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Callable, Dict, Optional
from urllib.parse import quote

import numpy as np
import requests

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from .base import BaseTTSProvider, VoiceCache
from .omnivoice import _OmniVoiceEQ
from .voice_utils import compute_reference_hash, find_voice_reference, parse_hashed_voice_name
from utils.settings import load_settings
from utils.text_utils import preprocess_text


_synthesis_sequence_lock = threading.Lock()
_capabilities_cache: Optional[Dict] = None
_capabilities_cache_key: Optional[tuple] = None
SONORUS_PROTOCOL_VERSION = "1.0"


def _serialized_synthesis(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        with _synthesis_sequence_lock:
            return func(*args, **kwargs)
    return wrapper


def _wrap_omnivoice_api_eq(on_chunk: Callable[[bytes, Optional[Dict]], None], sample_rate: int):
    """Wrap a PCM callback with OmniVoice smoothing EQ at the configured rate."""
    eq = _OmniVoiceEQ(sample_rate)

    def _on_chunk(pcm_bytes, word_alignment):
        on_chunk(eq.process_pcm16(pcm_bytes), word_alignment)

    return _on_chunk


_GAME_LANG_TO_OMNIVOICE = {
    "EN_US": "English", "EN_GB": "English",
    "DE_DE": "German",
    "FR_FR": "French",
    "ES_ES": "Spanish", "ES_MX": "Spanish",
    "IT_IT": "Italian",
    "PT_BR": "Portuguese",
    "JA_JP": "Japanese",
    "KO_KR": "Korean",
    "ZH_CN": "Chinese", "ZH_TW": "Chinese",
    "PL_PL": "Polish",
    "RU_RU": "Russian",
    "AR_AE": "Arabic",
    "NL_NL": "Dutch",
    "TR_TR": "Turkish",
}


def _strip_workspace_prefix(name: str) -> str:
    """Convert omnivoice-api clone IDs like default__Sebastian to Sebastian."""
    if "__" in name:
        return name.split("__", 1)[1]
    return name


def _resource_name_to_voice_id(resource_name: str) -> str:
    """Convert workspaces/{workspace}/voices/{voice} to flat omnivoice-api voiceId."""
    if "/voices/" not in resource_name:
        return resource_name
    parts = resource_name.split("/")
    if len(parts) >= 4:
        return f"{parts[1]}__{parts[3]}"
    return resource_name


def _normalize_lang_code(voice: Dict, fallback: str = "EN_US") -> str:
    lang = voice.get("langCode")
    if lang:
        return str(lang).upper()
    languages = voice.get("languages") or []
    if languages:
        first = str(languages[0]).lower()
        if first == "en":
            return "EN_US"
        if len(first) == 2:
            return f"{first.upper()}_{first.upper()}"
    return fallback


def _read_sidecar_transcript(reference_wav_path: str) -> Optional[str]:
    txt_path = Path(reference_wav_path).with_suffix(".txt")
    try:
        if txt_path.is_file():
            text = txt_path.read_text(encoding="utf-8").strip()
            if text:
                return text
    except Exception as exc:
        print(f"[OmniVoiceAPI] Failed to read transcript {txt_path.name}: {exc}")
    return None


def _get_omnivoice_api_config() -> Dict:
    settings = load_settings()
    tts_settings = settings.get("tts", {})
    omni_settings = tts_settings.get("omnivoice_api", {})
    game_lang = settings.get("setup", {}).get("language", "EN_US")

    api_url = (omni_settings.get("api_url") or "http://127.0.0.1:8000").strip()
    api_url = api_url.replace("://localhost", "://127.0.0.1").rstrip("/")

    return {
        "api_url": api_url,
        "api_key": (omni_settings.get("api_key") or "").strip(),
        "model": omni_settings.get("model", "omnivoice"),
        "num_steps": int(omni_settings.get("num_steps", 32)),
        "first_sentence_steps": int(omni_settings.get("first_sentence_steps", 24)),
        "guidance_scale": float(omni_settings.get("guidance_scale", 2.0)),
        "apply_smoothing_eq": bool(omni_settings.get("apply_smoothing_eq", True)),
        "sample_rate": int(omni_settings.get("sample_rate", 48000)),
        "language": _GAME_LANG_TO_OMNIVOICE.get(game_lang, "English"),
        "speed": float(tts_settings.get("speed", 1.0)),
    }


def _get_headers(config: Optional[Dict] = None) -> Dict[str, str]:
    config = config or _get_omnivoice_api_config()
    headers = {"Content-Type": "application/json"}
    api_key = config.get("api_key", "")
    if api_key:
        headers["Authorization"] = f"Basic {api_key}"
    return headers


def _get_capabilities(config: Optional[Dict] = None, force: bool = False) -> Dict:
    global _capabilities_cache, _capabilities_cache_key
    config = config or _get_omnivoice_api_config()
    cache_key = (config["api_url"], config.get("api_key", ""))
    if not force and _capabilities_cache is not None and _capabilities_cache_key == cache_key:
        return _capabilities_cache

    url = f"{config['api_url']}/sonorus/v1/omnivoice/capabilities"
    try:
        response = requests.get(url, headers=_get_headers(config), timeout=15)
    except requests.exceptions.RequestException as exc:
        raise Exception(f"Cannot connect to OmniVoice API Sonorus capabilities endpoint: {exc}")

    if response.status_code == 401:
        raise Exception("OmniVoice API authentication failed. Check your API key.")
    if response.status_code != 200:
        raise Exception(
            "OmniVoice API does not expose the Sonorus parity endpoint. "
            f"Expected {url}, got HTTP {response.status_code}: {response.text[:300]}"
        )

    data = response.json()
    modes = data.get("generationModes", [])
    if data.get("protocolVersion") != SONORUS_PROTOCOL_VERSION:
        raise Exception(
            f"Unsupported OmniVoice API protocolVersion: {data.get('protocolVersion')}. "
            f"Expected {SONORUS_PROTOCOL_VERSION}."
        )
    if "local_parity" not in modes:
        raise Exception("OmniVoice API is missing required local_parity generation mode.")

    _capabilities_cache = data
    _capabilities_cache_key = cache_key
    return data


class OmniVoiceApiVoiceCache(VoiceCache):
    """Remote OmniVoice voice cache keyed by original name + voice language."""

    def _make_cache_key(self, name: str, lang: Optional[str] = None) -> str:
        if lang and lang != "EN_US":
            return f"{name}_{lang}"
        return name

    def _current_reference_hash(self, name: str, lang: Optional[str]) -> Optional[str]:
        ref_path = find_voice_reference(name, "15s", language=lang or "EN_US")
        return compute_reference_hash(ref_path) if ref_path else None

    def load(self) -> bool:
        config = _get_omnivoice_api_config()
        _get_capabilities(config)
        api_url = config["api_url"]
        url = f"{api_url}/sonorus/v1/omnivoice/voices"

        try:
            print(f"[OmniVoiceAPI] Loading voices from {url}")
            response = requests.get(url, headers=_get_headers(config), timeout=30)
            if response.status_code != 200:
                raise Exception(f"OmniVoice API error: {response.status_code} {response.reason}")

            data = response.json()
            voices = data.get("voices", data.get("data", []))
            self._voices.clear()
            self._by_id.clear()
            self._duplicates_to_delete = []

            for raw_voice in voices:
                voice = dict(raw_voice)
                voice_id = voice.get("voiceId") or voice.get("id") or voice.get("name") or ""
                voice_id = _resource_name_to_voice_id(str(voice_id)) if voice_id else ""
                display_name = voice.get("displayName") or _strip_workspace_prefix(str(voice_id))
                if not voice_id or not display_name:
                    continue

                display_for_parse = _strip_workspace_prefix(str(display_name))
                original_name, detected_lang, ref_hash = parse_hashed_voice_name(display_for_parse)
                metadata_hash = voice.get("referenceHash")
                if metadata_hash:
                    ref_hash = str(metadata_hash).lower()
                if not ref_hash:
                    id_name = _strip_workspace_prefix(str(voice_id))
                    id_original, id_lang, id_hash = parse_hashed_voice_name(id_name)
                    if id_hash:
                        original_name = id_original
                        detected_lang = detected_lang or id_lang
                        ref_hash = id_hash

                effective_lang = detected_lang or _normalize_lang_code(voice)
                if ref_hash:
                    voice["referenceHash"] = ref_hash
                voice["voiceId"] = str(voice_id)
                voice["displayName"] = str(display_name)
                voice.setdefault("langCode", effective_lang)

                key = self._make_cache_key(original_name, effective_lang)
                if key in self._voices:
                    existing = self._voices[key]
                    existing_hash = existing.get("referenceHash")
                    current_hash = self._current_reference_hash(original_name, effective_lang)
                    if current_hash and ref_hash == current_hash and existing_hash != current_hash:
                        print(f"[OmniVoiceAPI] Duplicate {key}: keeping {voice['displayName']} "
                              f"(hash {ref_hash} matches reference)")
                        if existing.get("voiceId"):
                            self._duplicates_to_delete.append((existing["voiceId"], existing.get("displayName", "")))
                            self._by_id.pop(existing["voiceId"], None)
                        self._voices[key] = voice
                        self._by_id[voice["voiceId"]] = voice
                        continue
                    if ref_hash and existing_hash and ref_hash != existing_hash:
                        self._duplicates_to_delete.append((voice["voiceId"], voice["displayName"]))
                    continue

                self._voices[key] = voice
                self._by_id[voice["voiceId"]] = voice

            self._loaded = True
            print(f"[OmniVoiceAPI] Loaded {len(self._voices)} voices")
            return True

        except requests.exceptions.RequestException as exc:
            print(f"[OmniVoiceAPI] Request failed: {exc}")
            raise Exception(f"Cannot connect to OmniVoice API: {exc}")
        except Exception as exc:
            print(f"[OmniVoiceAPI] Failed to load voices: {exc}")
            raise


_voice_cache: Optional[OmniVoiceApiVoiceCache] = None


def _get_voice_cache() -> OmniVoiceApiVoiceCache:
    global _voice_cache
    if _voice_cache is None:
        _voice_cache = OmniVoiceApiVoiceCache()
    return _voice_cache


def clear_voice_cache():
    global _voice_cache, _capabilities_cache, _capabilities_cache_key
    if _voice_cache is not None:
        print("[OmniVoiceAPI] Clearing voice cache")
        _voice_cache = None
    _capabilities_cache = None
    _capabilities_cache_key = None


class OmniVoiceApiProvider(BaseTTSProvider):
    """Remote OmniVoice provider backed by omnivoice-api."""

    def __init__(self):
        self._last_synthesis_error = ""

    @property
    def name(self) -> str:
        return "OmniVoiceAPI"

    def get_config(self) -> Dict:
        return _get_omnivoice_api_config()

    def get_sample_rate(self) -> int:
        config = self.get_config()
        try:
            capabilities = _get_capabilities(config)
            return int(capabilities.get("outputSampleRate") or config["sample_rate"])
        except Exception as exc:
            print(f"[OmniVoiceAPI] Capabilities unavailable, using configured sample rate: {exc}")
            return config["sample_rate"]

    def get_buffer_seconds(self) -> float:
        return 1.0

    def get_default_language(self) -> Optional[str]:
        return None

    def get_voice_cache(self) -> VoiceCache:
        return _get_voice_cache()

    def init(self) -> bool:
        _get_capabilities(self.get_config(), force=True)
        return self.get_voice_cache().load()

    def _record_synthesis_error(self, error: str) -> None:
        self._last_synthesis_error = error or ""

    def should_reclone_after_synthesis_failure(self, voice_id: str) -> bool:
        error = (self._last_synthesis_error or "").lower()
        if not error:
            return False
        return (
            ("voice" in error and "not found" in error)
            or "invalid base64" in error
            or "incorrect padding" in error
            or "http 404" in error
        )

    def _guidance_for_speaker(self, speaker_id: Optional[str], base_cfg: Optional[float] = None) -> float:
        config = self.get_config()
        base_cfg = config.get("guidance_scale", 2.0) if base_cfg is None else base_cfg
        if speaker_id:
            settings = load_settings()
            temp_mod = settings.get("tts", {}).get("npc_temp_modifiers", {}).get(speaker_id, 0.0)
            if temp_mod > 0:
                cfg = min(base_cfg + temp_mod * 10.0, 10.0)
                print(f"[OmniVoiceAPI] Per-NPC CFG for {speaker_id}: {cfg:.1f} "
                      f"(base {base_cfg:.1f} + temp mod {temp_mod:+.1f})")
                return cfg
        return base_cfg

    def on_voice_used(self, voice: Dict) -> None:
        voice_id = voice.get("voiceId") if voice else None
        if not voice_id:
            return

        def _warmup():
            try:
                config = self.get_config()
                _get_capabilities(config)
                url = f"{config['api_url']}/sonorus/v1/omnivoice/voices/{quote(str(voice_id), safe='')}:warmup"
                requests.post(url, headers=_get_headers(config), timeout=5)
            except Exception as exc:
                print(f"[OmniVoiceAPI] Warmup skipped for {voice_id}: {exc}")

        threading.Thread(target=_warmup, name=f"OmniVoiceAPIWarmup-{voice_id}", daemon=True).start()

    def clone_voice(self, display_name: str, reference_wav_path: str,
                    lang: Optional[str] = None) -> Optional[Dict]:
        config = self.get_config()
        _get_capabilities(config)
        api_url = config["api_url"]

        if not os.path.exists(reference_wav_path):
            print(f"[OmniVoiceAPI] Reference file not found: {reference_wav_path}")
            return None

        with open(reference_wav_path, "rb") as f:
            audio_data = base64.b64encode(f.read()).decode("utf-8")

        payload = {
            "displayName": display_name,
            "langCode": lang or "EN_US",
            "audioData": audio_data,
            "referenceHash": compute_reference_hash(reference_wav_path),
            "tags": ["hogwarts-legacy", "auto-cloned"],
        }
        ref_text = _read_sidecar_transcript(reference_wav_path)
        if ref_text:
            payload["refText"] = ref_text

        url = f"{api_url}/sonorus/v1/omnivoice/voices:clone"

        try:
            file_size = os.path.getsize(reference_wav_path)
            print(f"[OmniVoiceAPI] Cloning voice: {display_name}, file size: {file_size / 1024:.1f} KB")
            response = requests.post(url, json=payload, headers=_get_headers(config), timeout=180)
            if response.status_code not in (200, 201):
                print(f"[OmniVoiceAPI] Clone error: HTTP {response.status_code}")
                print(f"[OmniVoiceAPI] Details: {response.text[:300]}")
                return None

            data = response.json()
            voice = data.get("voice", {})
            if not voice.get("voiceId"):
                print("[OmniVoiceAPI] Clone response missing voiceId")
                return None

            print(f"[OmniVoiceAPI] Voice cloned: {display_name} -> {voice.get('voiceId')}")
            return voice

        except requests.exceptions.RequestException as exc:
            print(f"[OmniVoiceAPI] Clone request failed: {exc}")
            return None
        except Exception as exc:
            print(f"[OmniVoiceAPI] Clone failed: {exc}")
            return None

    def delete_voice(self, resource_name: str) -> bool:
        config = self.get_config()
        _get_capabilities(config)
        api_url = config["api_url"]

        voice_id = _resource_name_to_voice_id(resource_name)

        url = f"{api_url}/sonorus/v1/omnivoice/voices/{quote(voice_id, safe='')}"
        try:
            print(f"[OmniVoiceAPI] Deleting voice: {voice_id}")
            response = requests.delete(url, headers=_get_headers(config), timeout=30)
            if response.status_code in (200, 204):
                print(f"[OmniVoiceAPI] Voice deleted or already absent: {voice_id}")
                return True
            print(f"[OmniVoiceAPI] Delete failed: HTTP {response.status_code} {response.text[:300]}")
            return False
        except requests.exceptions.RequestException as exc:
            print(f"[OmniVoiceAPI] Delete request failed: {exc}")
            return False

    def _synthesize_request(self, text: str, voice_id: str,
                            on_chunk: Callable[[bytes, Optional[Dict]], None],
                            num_steps: Optional[int] = None,
                            guidance_scale: Optional[float] = None) -> int:
        config = self.get_config()
        capabilities = _get_capabilities(config)
        api_url = config["api_url"]
        url = f"{api_url}/sonorus/v1/omnivoice/speech"

        payload = {
            "text": text,
            "voiceId": voice_id,
            "responseFormat": "pcm",
            "language": config.get("language", "English"),
            "numSteps": int(num_steps if num_steps is not None else config["num_steps"]),
            "guidanceScale": float(guidance_scale if guidance_scale is not None else config["guidance_scale"]),
        }

        self._record_synthesis_error("")
        try:
            response = requests.post(
                url,
                json=payload,
                headers=_get_headers(config),
                stream=True,
                timeout=(10, 180),
            )
            if response.status_code != 200:
                error_body = response.text[:500]
                self._record_synthesis_error(f"HTTP {response.status_code}: {error_body}")
                print(f"[OmniVoiceAPI] Synthesis HTTP {response.status_code}: {error_body}")
                return 0

            mode = response.headers.get("X-Sonorus-Generation-Mode", "") if hasattr(response, "headers") else ""
            if mode and mode != "local_parity":
                print(f"[OmniVoiceAPI] WARNING: unexpected generation mode from server: {mode}")
            header_sr = response.headers.get("X-Audio-Sample-Rate") if hasattr(response, "headers") else None
            expected_sr = str(capabilities.get("outputSampleRate", config.get("sample_rate", 48000)))
            if header_sr and header_sr != expected_sr:
                print(f"[OmniVoiceAPI] WARNING: server sample-rate header {header_sr} differs from capabilities {expected_sr}")

            total_bytes = 0
            carry = b""
            for chunk in response.iter_content(chunk_size=8192):
                if not chunk:
                    continue
                pcm_bytes = carry + chunk
                if len(pcm_bytes) % 2:
                    carry = pcm_bytes[-1:]
                    pcm_bytes = pcm_bytes[:-1]
                else:
                    carry = b""
                if pcm_bytes:
                    on_chunk(pcm_bytes, None)
                    total_bytes += len(pcm_bytes)

            if carry:
                print("[OmniVoiceAPI] Dropped trailing odd PCM byte")

            return total_bytes

        except requests.exceptions.RequestException as exc:
            self._record_synthesis_error(str(exc))
            print(f"[OmniVoiceAPI] Synthesis request failed: {exc}")
            return 0

    @_serialized_synthesis
    def synthesize_stream(self, text: str, voice_id: str,
                          on_chunk: Callable[[bytes, Optional[Dict]], None],
                          speaker_id: Optional[str] = None) -> bool:
        config = self.get_config()
        sample_rate = self.get_sample_rate()
        eq_on_chunk = (
            _wrap_omnivoice_api_eq(on_chunk, sample_rate)
            if config.get("apply_smoothing_eq", True)
            else on_chunk
        )
        print(f"[OmniVoiceAPI] Synthesizing: voice={voice_id}")
        bytes_produced = self._synthesize_request(
            text=text,
            voice_id=voice_id,
            on_chunk=eq_on_chunk,
            num_steps=config["num_steps"],
            guidance_scale=self._guidance_for_speaker(speaker_id, config["guidance_scale"]),
        )
        return bytes_produced > 0

    @_serialized_synthesis
    def synthesize_stream_sentences(self, sentences, voice_id: str,
                                     on_chunk: Callable[[bytes, Optional[Dict]], None],
                                     speaker_id: Optional[str] = None,
                                     on_sentence_flushed: Callable = None,
                                     abort_check: Callable = None,
                                     on_voice_switch: Callable = None) -> bool:
        config = self.get_config()
        num_steps = config.get("num_steps", 32)
        first_sentence_steps = config.get("first_sentence_steps", 24)
        base_cfg = config.get("guidance_scale", 2.0)
        effective_sr = self.get_sample_rate()
        eq_on_chunk = (
            _wrap_omnivoice_api_eq(on_chunk, effective_sr)
            if config.get("apply_smoothing_eq", True)
            else on_chunk
        )

        guidance = self._guidance_for_speaker(speaker_id, base_cfg)

        start_time = time.time()
        total_bytes = 0
        sentence_count = 0

        try:
            for item in sentences:
                if abort_check and abort_check():
                    print(f"[OmniVoiceAPI] Sentence streaming aborted after {sentence_count} sentences")
                    return total_bytes > 0

                if isinstance(item, tuple):
                    sentence, per_voice_id = item
                else:
                    sentence, per_voice_id = item, voice_id

                if not sentence or not sentence.strip():
                    continue

                processed = preprocess_text(sentence)
                if not processed:
                    continue

                if sentence_count > 0:
                    silence_duration = random.uniform(0.25, 1.0)
                    silence_samples = int(silence_duration * effective_sr)
                    silence_pcm = np.zeros(silence_samples, dtype=np.int16).tobytes()
                    silence_start = total_bytes
                    eq_on_chunk(silence_pcm, None)
                    total_bytes += len(silence_pcm)
                    print(f"[OmniVoiceAPITiming] silence before_sentence={sentence_count + 1} "
                          f"duration={silence_duration:.2f}s bytes={len(silence_pcm)} "
                          f"start_bytes={silence_start} end_bytes={total_bytes}")

                sentence_idx = sentence_count
                if sentence_idx > 0 and on_voice_switch:
                    try:
                        on_voice_switch(total_bytes, sentence_idx)
                        print(f"[OmniVoiceAPITiming] boundary_confirmed idx={sentence_idx} "
                              f"start_bytes={total_bytes} "
                              f"start_time={total_bytes / (effective_sr * 2):.2f}s "
                              f"source=provider_byte_position")
                    except Exception as exc:
                        print(f"[OmniVoiceAPI] Boundary callback failed: {exc}")

                sentence_count += 1
                steps = first_sentence_steps if sentence_count == 1 else num_steps
                sentence_start_bytes = total_bytes
                sentence_start_time = sentence_start_bytes / (effective_sr * 2)
                voice_tag = f" [{per_voice_id}]" if per_voice_id != voice_id else ""
                print(f"[OmniVoiceAPI] Streaming sentence {sentence_count}{voice_tag}: "
                      f"start_bytes={sentence_start_bytes} start_time={sentence_start_time:.2f}s "
                      f"text=\"{processed[:100]}{'...' if len(processed) > 100 else ''}\"")

                bytes_produced = self._synthesize_request(
                    text=processed,
                    voice_id=per_voice_id,
                    on_chunk=eq_on_chunk,
                    num_steps=steps,
                    guidance_scale=guidance,
                )
                total_bytes += bytes_produced
                success = bytes_produced > 0
                print(f"[OmniVoiceAPITiming] sentence_done idx={sentence_count - 1} "
                      f"bytes={bytes_produced} total_bytes={total_bytes} "
                      f"duration={bytes_produced / (effective_sr * 2):.2f}s "
                      f"total_duration={total_bytes / (effective_sr * 2):.2f}s "
                      f"success={success} steps={steps} guidance_scale={guidance}")

                if not success:
                    print(f"[OmniVoiceAPI] Sentence synthesis failed at sentence {sentence_count}")
                    return total_bytes > 0

                if on_sentence_flushed:
                    on_sentence_flushed()

            proc_time = time.time() - start_time
            duration = total_bytes / (effective_sr * 2) if total_bytes > 0 else 0
            rtf = duration / proc_time if proc_time > 0 else 0
            print(f"[OmniVoiceAPI] Sentence streaming complete: {sentence_count} sentences, "
                  f"{total_bytes} bytes, {duration:.2f}s audio")
            print(f"[OmniVoiceAPI] Stats: {proc_time*1000:.1f}ms | RTF: {rtf:.2f}x")
            return total_bytes > 0

        except Exception as exc:
            print(f"[OmniVoiceAPI] Sentence streaming failed: {exc}")
            traceback.print_exc()
            return total_bytes > 0
