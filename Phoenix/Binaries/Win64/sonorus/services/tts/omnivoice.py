"""
OmniVoice TTS Provider

Thin provider wrapper that implements BaseTTSProvider and delegates to the
OmniVoice engine module (omnivoice_engine.py) for subprocess-based inference.

Follows the same architecture as PocketONNX — the base class speak_streaming()
handles the full pipeline (voice resolution, audio buffering, lipsync
coordination) and calls synthesize_stream_sentences() for per-sentence streaming.
"""
import os
import sys
import math
import functools
import threading
from typing import Dict, Optional, Callable

import numpy as np

try:
    from scipy import signal as scipy_signal
except ImportError:
    scipy_signal = None

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from .base import BaseTTSProvider, VoiceCache
from .voice_utils import parse_hashed_voice_name, compute_reference_hash
from utils.settings import load_settings


OMNIVOICE_EQ_SAMPLE_RATE = 48_000
OMNIVOICE_HIGH_SHELF_FREQ = 4_000.0
OMNIVOICE_HIGH_SHELF_GAIN_DB = -2.0
OMNIVOICE_LOW_PASS_FREQ = 14_000.0
OMNIVOICE_LOW_PASS_Q = 1.0 / math.sqrt(2.0)  # 2-pole Butterworth, 12 dB/octave.
_synthesis_sequence_lock = threading.Lock()


def _serialized_synthesis(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        with _synthesis_sequence_lock:
            return func(*args, **kwargs)
    return wrapper


class _StreamingBiquad:
    """Small stateful biquad for int16 PCM callback processing."""

    def __init__(self, b, a):
        self.b = np.asarray(b, dtype=np.float64)
        self.a = np.asarray(a, dtype=np.float64)
        self.zi = np.zeros(2, dtype=np.float64)

    def process(self, samples: np.ndarray) -> np.ndarray:
        if samples.size == 0:
            return samples
        if scipy_signal is not None:
            filtered, self.zi = scipy_signal.lfilter(self.b, self.a, samples, zi=self.zi)
            return filtered.astype(np.float32, copy=False)

        out = np.empty_like(samples, dtype=np.float32)
        z1, z2 = float(self.zi[0]), float(self.zi[1])
        b0, b1, b2 = self.b
        _, a1, a2 = self.a
        for idx, sample in enumerate(samples):
            y = b0 * sample + z1
            z1 = b1 * sample - a1 * y + z2
            z2 = b2 * sample - a2 * y
            out[idx] = y
        self.zi[0] = z1
        self.zi[1] = z2
        return out


def _rbj_high_shelf(sample_rate: int, freq: float, gain_db: float, slope: float = 1.0):
    """RBJ high-shelf coefficients, normalized for a0 = 1."""
    a_gain = 10.0 ** (gain_db / 40.0)
    omega = 2.0 * math.pi * freq / sample_rate
    sin_omega = math.sin(omega)
    cos_omega = math.cos(omega)
    alpha = sin_omega / 2.0 * math.sqrt((a_gain + 1.0 / a_gain) * (1.0 / slope - 1.0) + 2.0)
    beta = 2.0 * math.sqrt(a_gain) * alpha

    b0 = a_gain * ((a_gain + 1.0) + (a_gain - 1.0) * cos_omega + beta)
    b1 = -2.0 * a_gain * ((a_gain - 1.0) + (a_gain + 1.0) * cos_omega)
    b2 = a_gain * ((a_gain + 1.0) + (a_gain - 1.0) * cos_omega - beta)
    a0 = (a_gain + 1.0) - (a_gain - 1.0) * cos_omega + beta
    a1 = 2.0 * ((a_gain - 1.0) - (a_gain + 1.0) * cos_omega)
    a2 = (a_gain + 1.0) - (a_gain - 1.0) * cos_omega - beta

    return [b0 / a0, b1 / a0, b2 / a0], [1.0, a1 / a0, a2 / a0]


def _rbj_low_pass(sample_rate: int, freq: float, q: float):
    """RBJ 2-pole low-pass coefficients, normalized for a0 = 1."""
    omega = 2.0 * math.pi * freq / sample_rate
    sin_omega = math.sin(omega)
    cos_omega = math.cos(omega)
    alpha = sin_omega / (2.0 * q)

    b0 = (1.0 - cos_omega) / 2.0
    b1 = 1.0 - cos_omega
    b2 = (1.0 - cos_omega) / 2.0
    a0 = 1.0 + alpha
    a1 = -2.0 * cos_omega
    a2 = 1.0 - alpha

    return [b0 / a0, b1 / a0, b2 / a0], [1.0, a1 / a0, a2 / a0]


class _OmniVoiceEQ:
    """OmniVoice-only EQ: -2 dB high shelf at 4 kHz, 14 kHz 12 dB/oct LPF."""

    def __init__(self, sample_rate: int = OMNIVOICE_EQ_SAMPLE_RATE):
        shelf_b, shelf_a = _rbj_high_shelf(
            sample_rate,
            OMNIVOICE_HIGH_SHELF_FREQ,
            OMNIVOICE_HIGH_SHELF_GAIN_DB,
        )
        lowpass_b, lowpass_a = _rbj_low_pass(
            sample_rate,
            OMNIVOICE_LOW_PASS_FREQ,
            OMNIVOICE_LOW_PASS_Q,
        )
        self._filters = [
            _StreamingBiquad(shelf_b, shelf_a),
            _StreamingBiquad(lowpass_b, lowpass_a),
        ]

    def process_pcm16(self, pcm_bytes: bytes) -> bytes:
        if not pcm_bytes:
            return pcm_bytes
        if len(pcm_bytes) % 2:
            return pcm_bytes

        samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        for audio_filter in self._filters:
            samples = audio_filter.process(samples)
        samples = np.clip(samples * 32768.0, -32768, 32767).astype(np.int16)
        return samples.tobytes()


def _wrap_omnivoice_eq(on_chunk: Callable[[bytes, Optional[Dict]], None]):
    """Wrap an OmniVoice PCM callback with the provider-specific EQ."""
    eq = _OmniVoiceEQ()

    def _on_chunk(pcm_bytes, word_alignment):
        on_chunk(eq.process_pcm16(pcm_bytes), word_alignment)

    return _on_chunk


# ============================================
# OmniVoice Voice Cache
# ============================================

class OmniVoiceVoiceCache(VoiceCache):
    """
    OmniVoice voice cache - tracks local voice reference files.

    Keys by name + language for language-specific voice references.
    """

    def _make_cache_key(self, name: str, lang: Optional[str] = None) -> str:
        """Generate cache key - include language for language-specific references."""
        if lang and lang != "EN_US":
            return f"{name}_{lang}"
        return name

    def load(self) -> bool:
        """
        Load available voices from voice_references directory.

        Returns True on success (even if no voices found).
        """
        from services.omnivoice_engine import VOICE_DIR, _AUDIO_EXTS

        self._voices.clear()
        self._by_id.clear()

        if not VOICE_DIR.exists():
            print(f"[OmniVoice] Voice directory not found: {VOICE_DIR}")
            self._loaded = True
            return True

        # Scan for voice files
        seen = set()
        for path in VOICE_DIR.iterdir():
            if not path.is_file():
                continue
            if path.suffix.lower() not in _AUDIO_EXTS:
                continue

            # Extract voice name from stem
            # Handle "VoiceName.wav", "VoiceName_reference_15s.wav", and "VoiceName_reference.wav"
            stem = path.stem
            if stem.endswith("_reference_15s"):
                voice_name = stem[:-14]  # Remove "_reference_15s"
            elif stem.endswith("_reference"):
                voice_name = stem[:-10]  # Remove "_reference"
            else:
                voice_name = stem

            if voice_name in seen:
                continue
            seen.add(voice_name)

            # Compute hash of reference file
            ref_hash = compute_reference_hash(str(path))

            voice = {
                "displayName": voice_name,
                "voiceId": voice_name,  # Use name as ID (local files)
                "langCode": "EN_US",
                "filePath": str(path),
            }

            # Store hash in voice dict
            if ref_hash:
                voice["referenceHash"] = ref_hash

            # Parse voice name to extract original name, language suffix, and hash
            # e.g., "PlayerMale_DE_DE_a1b2c3d4" -> ("PlayerMale", "DE_DE", "a1b2c3d4")
            original_name, detected_lang, name_hash = parse_hashed_voice_name(voice_name)

            # If hash is in the name, store it (though for local it's computed from file)
            if name_hash and not voice.get("referenceHash"):
                voice["referenceHash"] = name_hash

            # Use _make_cache_key to get the correct key
            cache_key = self._make_cache_key(original_name, detected_lang)
            self._voices[cache_key] = voice
            self._by_id[cache_key] = voice

        self._loaded = True
        print(f"[OmniVoice] Found {len(self._voices)} voice references")
        return True


# Module-level singleton cache
_voice_cache: OmniVoiceVoiceCache = None


def _get_voice_cache() -> OmniVoiceVoiceCache:
    """Get or create the singleton voice cache."""
    global _voice_cache
    if _voice_cache is None:
        _voice_cache = OmniVoiceVoiceCache()
    return _voice_cache


def clear_voice_cache():
    """Clear the module-level voice cache."""
    global _voice_cache
    if _voice_cache is not None:
        print("[OmniVoice] Clearing voice cache")
        _voice_cache = None


# ============================================
# OmniVoice Provider
# ============================================

class OmniVoiceProvider(BaseTTSProvider):
    """
    OmniVoice TTS provider using local model inference in a subprocess.

    Features:
    - Local inference via subprocess (torch/CUDA isolated from Flask)
    - Voice cloning from reference files with pretokenization
    - Optional 48kHz upscaling via VoxCPM2 AudioVAE
    - 24kHz native sample rate (48kHz with upscale)
    - Models downloaded from HuggingFace: k2-fsa/OmniVoice
    """

    @property
    def name(self) -> str:
        return "OmniVoice"

    def get_config(self) -> Dict:
        """Get OmniVoice configuration from settings."""
        settings = load_settings()
        tts_settings = settings.get('tts', {})
        omni_settings = tts_settings.get('omnivoice', {})

        return {
            "num_steps": int(omni_settings.get('num_steps', 32)),
            "first_sentence_steps": int(omni_settings.get('first_sentence_steps', 24)),
            "guidance_scale": float(omni_settings.get('guidance_scale', 2.0)),
            "apply_smoothing_eq": bool(omni_settings.get('apply_smoothing_eq', True)),
            "speed": float(tts_settings.get('speed', 1.0)),
        }

    def get_sample_rate(self) -> int:
        """Upscaler is always on — always return 48 kHz."""
        return 48_000

    def get_buffer_seconds(self) -> float:
        return 1.0

    def get_default_language(self) -> Optional[str]:
        # OmniVoice is multilingual, no default language needed
        return None

    def get_voice_cache(self) -> VoiceCache:
        return _get_voice_cache()

    def clone_voice(self, display_name: str, reference_wav_path: str,
                    lang: Optional[str] = None) -> Optional[Dict]:
        """
        "Clone" a voice by registering the reference file.

        For OmniVoice, cloning is making the reference available and optionally
        pretokenizing it in the worker process so the first synthesis is fast.

        Args:
            display_name: Voice name (e.g., "SebastianSallow")
            reference_wav_path: Path to reference WAV file
            lang: Language code (ignored for OmniVoice)

        Returns:
            Voice dict on success, None on failure
        """
        import shutil
        from pathlib import Path
        from services.omnivoice_engine import (
            VOICE_DIR,
            _get_manager,
            ensure_voice_reference_transcript,
            is_loaded,
        )

        if not os.path.exists(reference_wav_path):
            print(f"[OmniVoice] Reference file not found: {reference_wav_path}")
            return None

        # Ensure voice directory exists
        VOICE_DIR.mkdir(parents=True, exist_ok=True)

        source_path = Path(reference_wav_path).resolve()
        voice_root = VOICE_DIR.resolve()

        # Keep language-specific references in their language directory. Copy
        # only truly external/custom references into the root voice directory.
        try:
            source_path.relative_to(voice_root)
            ref_path = source_path
        except ValueError:
            ref_path = VOICE_DIR / os.path.basename(reference_wav_path)

        if ref_path != source_path and not ref_path.exists():
            try:
                shutil.copy2(reference_wav_path, ref_path)
                print(f"[OmniVoice] Copied reference to: {ref_path}")
            except Exception as e:
                print(f"[OmniVoice] Failed to copy reference: {e}")
                return None

        voice = {
            "displayName": display_name,
            "voiceId": display_name,
            "langCode": lang or "EN_US",
            "filePath": str(ref_path),
        }

        ref_text = ensure_voice_reference_transcript(str(ref_path))
        if ref_text:
            print(f"[OmniVoice] Transcript ready for {ref_path.name} ({len(ref_text)} chars)")

        # If the worker is already running, pretokenize in the background
        if is_loaded():
            try:
                manager = _get_manager()
                manager.pretokenize_voice(str(ref_path), ref_text=ref_text)
            except Exception as e:
                print(f"[OmniVoice] Pretokenize failed (non-fatal): {e}")

        # NOTE: Do NOT add to cache here - base class handles it with correct key
        print(f"[OmniVoice] Voice registered: {display_name}")

        return voice

    def delete_voice(self, voice_id: str) -> bool:
        """
        "Delete" a voice by clearing its prompt cache in the worker process.

        For OmniVoice, we don't delete the voice file itself (that's managed by the
        voice_references directory). Instead, we clear the cached voice prompt so the
        next synthesis will recompute it from the (potentially changed) reference file.

        Args:
            voice_id: Voice name to clear from prompt cache

        Returns:
            True if successfully signaled, False otherwise
        """
        try:
            from services.omnivoice_engine import clear_voice_prompt, _resolve_voice

            # Resolve voice_id to file path (worker uses file paths as cache keys)
            voice_path = _resolve_voice(voice_id)
            if voice_path:
                clear_voice_prompt(voice_path)
                print(f"[OmniVoice] Cleared prompt cache for: {voice_id}")
                return True
            else:
                print(f"[OmniVoice] Voice not found for cache clearing: {voice_id}")
                return False
        except Exception as e:
            print(f"[OmniVoice] Failed to clear prompt cache: {e}")
            return False

    @_serialized_synthesis
    def synthesize_stream(self, text: str, voice_id: str,
                          on_chunk: Callable[[bytes, Optional[Dict]], None],
                          speaker_id: Optional[str] = None) -> bool:
        """
        Stream TTS synthesis using OmniVoice model.

        Args:
            text: Text to synthesize
            voice_id: Voice name (matches file in voice_references)
            on_chunk: Callback function(pcm_bytes, word_alignment)
            speaker_id: Optional speaker ID for per-NPC settings

        Returns:
            True on success, False on error
        """
        from services.omnivoice_engine import _get_manager, _resolve_voice

        voice_path = _resolve_voice(voice_id)
        if not voice_path:
            print(f"[OmniVoice] Voice not found: {voice_id}")
            return False

        print(f"[OmniVoice] Synthesizing: voice={voice_id}")

        manager = _get_manager()
        if not manager.ensure_started():
            print("[OmniVoice] Worker process not available")
            return False

        config = self.get_config()
        eq_on_chunk = _wrap_omnivoice_eq(on_chunk) if config.get("apply_smoothing_eq", True) else on_chunk
        success, bytes_produced = manager.synthesize_sentence(
            text=text,
            voice_path=voice_path,
            on_chunk=eq_on_chunk,
        )
        return success

    @_serialized_synthesis
    def synthesize_stream_sentences(self, sentences, voice_id: str,
                                     on_chunk: Callable[[bytes, Optional[Dict]], None],
                                     speaker_id: Optional[str] = None,
                                     on_sentence_flushed: Callable = None,
                                     abort_check: Callable = None,
                                     on_voice_switch: Callable = None) -> bool:
        """
        Synthesize sentences one at a time as they arrive from the LLM.

        Each sentence is synthesized immediately when the generator yields it,
        allowing audio playback to start while the LLM is still generating
        subsequent sentences.

        Args:
            sentences: Iterable/generator of sentence strings (blocks on LLM)
            voice_id: Voice name (matches file in voice_references)
            on_chunk: Callback(pcm_bytes, word_alignment) per audio chunk
            speaker_id: Optional speaker ID for logging
            on_sentence_flushed: Optional callback when a sentence's audio completes.
            abort_check: Callable returning True to abort synthesis
            on_voice_switch: Optional callback(byte_position, sentence_idx) used by
                shared subtitle/routing code to confirm sentence boundaries.

        Returns:
            True on success, False on error
        """
        import random
        import time
        from services.omnivoice_engine import (
            _get_manager, _resolve_voice,
            SAMPLE_RATE, UPSCALE_SAMPLE_RATE,
        )
        from .voice_utils import compute_reference_hash
        from utils.text_utils import preprocess_text

        config = self.get_config()
        num_steps = config.get("num_steps", 32)
        first_sentence_steps = config.get("first_sentence_steps", 24)
        base_cfg = config.get("guidance_scale", 2.0)
        effective_sr = UPSCALE_SAMPLE_RATE
        eq_on_chunk = _wrap_omnivoice_eq(on_chunk) if config.get("apply_smoothing_eq", True) else on_chunk

        # Per-NPC temperature modifier → CFG boost (upward only, clamped to 10)
        npc_cfg = None
        if speaker_id:
            settings = load_settings()
            temp_mod = settings.get('tts', {}).get('npc_temp_modifiers', {}).get(speaker_id, 0.0)
            if temp_mod > 0:
                npc_cfg = min(base_cfg + temp_mod * 10.0, 10.0)
                print(f"[OmniVoice] Per-NPC CFG for {speaker_id}: {npc_cfg:.1f} "
                      f"(base {base_cfg:.1f} + temp mod {temp_mod:+.1f})")

        # Resolve default voice upfront
        voice_path = _resolve_voice(voice_id)
        if not voice_path:
            print(f"[OmniVoice] Voice not found: {voice_id}")
            return False

        voice_hash = compute_reference_hash(voice_path)
        if not voice_hash:
            print(f"[OmniVoice] Failed to compute voice hash: {voice_path}")
            return False

        # Cache for per-sentence voice resolution (narration support)
        _local_voice_cache = {voice_id: (voice_path, voice_hash)}

        def _resolve_voice_for_id(vid):
            """Resolve voice path/hash with caching."""
            if vid in _local_voice_cache:
                return _local_voice_cache[vid]
            vpath = _resolve_voice(vid)
            if not vpath:
                print(f"[OmniVoice] Narrator voice not found: {vid}, falling back to default")
                return voice_path, voice_hash
            vhash = compute_reference_hash(vpath)
            if not vhash:
                return voice_path, voice_hash
            _local_voice_cache[vid] = (vpath, vhash)
            return vpath, vhash

        manager = _get_manager()
        if not manager.ensure_started():
            print("[OmniVoice] Worker process not available")
            return False

        start_time = time.time()
        total_bytes = 0
        sentence_count = 0

        try:
            for item in sentences:
                if abort_check and abort_check():
                    print(f"[OmniVoice] Sentence streaming aborted after {sentence_count} sentences")
                    return total_bytes > 0

                # Unpack: either plain string or (text, per_voice_id) tuple
                if isinstance(item, tuple):
                    sentence, per_voice_id = item
                else:
                    sentence, per_voice_id = item, voice_id

                if not sentence or not sentence.strip():
                    continue

                # Light preprocess only — the worker's omni_preprocess_text handles
                # audio tag filtering (keeps [laughter], strips [happy] etc.)
                processed = preprocess_text(sentence)
                if not processed:
                    continue

                # Resolve voice for this sentence
                sent_voice_path, sent_voice_hash = _resolve_voice_for_id(per_voice_id)

                # Add silence gap between sentences
                if sentence_count > 0:
                    silence_duration = random.uniform(0.25, 1.0)
                    silence_samples = int(silence_duration * effective_sr)
                    silence_pcm = np.zeros(silence_samples, dtype=np.int16).tobytes()
                    silence_start = total_bytes
                    eq_on_chunk(silence_pcm, None)
                    total_bytes += len(silence_pcm)
                    print(f"[OmniVoiceTiming] silence before_sentence={sentence_count + 1} "
                          f"duration={silence_duration:.2f}s bytes={len(silence_pcm)} "
                          f"start_bytes={silence_start} end_bytes={total_bytes}")

                # Confirm the byte position where this sentence starts.
                # Without this, boundary #1 remains unconfirmed for providers
                # that don't emit word timings, and subtitle progression stalls.
                sentence_idx = sentence_count  # 0-based index of upcoming sentence
                if sentence_idx > 0 and on_voice_switch:
                    try:
                        on_voice_switch(total_bytes, sentence_idx)
                        print(f"[OmniVoiceTiming] boundary_confirmed idx={sentence_idx} "
                              f"start_bytes={total_bytes} "
                              f"start_time={total_bytes / (effective_sr * 2):.2f}s "
                              f"source=provider_byte_position")
                    except Exception as e:
                        print(f"[OmniVoice] Boundary callback failed: {e}")

                sentence_count += 1
                voice_tag = f" [{per_voice_id}]" if per_voice_id != voice_id else ""
                sentence_start_bytes = total_bytes
                sentence_start_time = sentence_start_bytes / (effective_sr * 2)
                print(f"[OmniVoice] Streaming sentence {sentence_count}{voice_tag}: "
                      f"start_bytes={sentence_start_bytes} start_time={sentence_start_time:.2f}s "
                      f"text=\"{processed[:100]}{'...' if len(processed) > 100 else ''}\"")

                # Use fewer steps for the first sentence (faster time-to-audio)
                steps = first_sentence_steps if sentence_count == 1 else num_steps

                success, bytes_produced = manager.synthesize_sentence(
                    text=processed,
                    voice_path=sent_voice_path,
                    on_chunk=eq_on_chunk,
                    num_steps=steps,
                    guidance_scale=npc_cfg,
                )
                total_bytes += bytes_produced
                print(f"[OmniVoiceTiming] sentence_done idx={sentence_count - 1} "
                      f"bytes={bytes_produced} total_bytes={total_bytes} "
                      f"duration={bytes_produced / (effective_sr * 2):.2f}s "
                      f"total_duration={total_bytes / (effective_sr * 2):.2f}s "
                      f"success={success} steps={steps} "
                      f"guidance_scale={npc_cfg if npc_cfg is not None else 'default'}")

                if not success:
                    print(f"[OmniVoice] Sentence synthesis failed at sentence {sentence_count}")
                    return total_bytes > 0

                if on_sentence_flushed:
                    on_sentence_flushed()

            proc_time = time.time() - start_time
            duration = total_bytes / (effective_sr * 2) if total_bytes > 0 else 0
            rtf = duration / proc_time if proc_time > 0 else 0

            print(f"[OmniVoice] Sentence streaming complete: {sentence_count} sentences, "
                  f"{total_bytes} bytes, {duration:.2f}s audio")
            print(f"[OmniVoice] Stats: {proc_time*1000:.1f}ms | RTF: {rtf:.2f}x")

            return total_bytes > 0

        except Exception as e:
            print(f"[OmniVoice] Sentence streaming failed: {e}")
            import traceback
            traceback.print_exc()
            return total_bytes > 0
