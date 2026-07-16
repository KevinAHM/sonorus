"""
OmniVoice.cpp TTS Provider

Thin provider wrapper that implements BaseTTSProvider and delegates to the
omnivoice.cpp engine module (omnivoice_cpp_engine.py) for subprocess-based
inference. This is the native (llama.cpp-style) backend counterpart to the
torch OmniVoice provider. Its native 24 kHz output is reconstructed at 48 kHz
through the VoxCPM2 AudioVAE upscaler before playback.

Follows the same architecture as the torch OmniVoice provider — the base class
speak_streaming() handles the full pipeline (voice resolution, audio buffering,
lipsync coordination) and calls synthesize_stream_sentences() for per-sentence
streaming.
"""
import os
import sys
import functools
import threading
from typing import Dict, Optional, Callable

import numpy as np

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from .base import BaseTTSProvider, VoiceCache
from .voice_utils import parse_hashed_voice_name, compute_reference_hash
# The smoothing EQ is pure numpy/scipy (no torch), so we reuse it directly from
# the torch provider rather than copying it. Importing omnivoice.py does not drag
# in any heavy dependencies — its module-level imports are the same torch-free set.
from .omnivoice import _wrap_omnivoice_eq
from utils.settings import load_settings


_synthesis_sequence_lock = threading.Lock()


def _serialized_synthesis(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        with _synthesis_sequence_lock:
            return func(*args, **kwargs)
    return wrapper


# ============================================
# OmniVoice.cpp Voice Cache
# ============================================

class OmniVoiceCppVoiceCache(VoiceCache):
    """
    OmniVoice.cpp voice cache - tracks local voice reference files.

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
        # VOICE_DIR / _AUDIO_EXTS have identical semantics across both engine
        # modules; import them from the torch-free omnivoice_engine module.
        from services.omnivoice_engine import VOICE_DIR, _AUDIO_EXTS

        self._voices.clear()
        self._by_id.clear()

        if not VOICE_DIR.exists():
            print(f"[OmniVoiceCpp] Voice directory not found: {VOICE_DIR}")
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
        print(f"[OmniVoiceCpp] Found {len(self._voices)} voice references")
        return True


# Module-level singleton cache
_voice_cache: OmniVoiceCppVoiceCache = None


def _get_voice_cache() -> OmniVoiceCppVoiceCache:
    """Get or create the singleton voice cache."""
    global _voice_cache
    if _voice_cache is None:
        _voice_cache = OmniVoiceCppVoiceCache()
    return _voice_cache


def clear_voice_cache():
    """Clear the module-level voice cache."""
    global _voice_cache
    if _voice_cache is not None:
        print("[OmniVoiceCpp] Clearing voice cache")
        _voice_cache = None


# ============================================
# OmniVoice.cpp Provider
# ============================================

class OmniVoiceCppProvider(BaseTTSProvider):
    """
    OmniVoice.cpp TTS provider using local model inference in a subprocess.

    Features:
    - Local native inference via subprocess (isolated from Flask)
    - Voice cloning from reference files with pretokenization
    - 48kHz output through the VoxCPM2 AudioVAE upscaler
    - Models loaded by the omnivoice.cpp engine backend
    """

    def name(self) -> str:
        return "omnivoice_cpp"

    def get_config(self) -> Dict:
        """Get OmniVoice.cpp configuration from settings."""
        settings = load_settings()
        tts_settings = settings.get('tts', {})
        omni_settings = tts_settings.get('omnivoice_cpp', {})

        return {
            "device": str(omni_settings.get('device', 'auto')),
            "num_steps": int(omni_settings.get('num_steps', 32)),
            "first_sentence_steps": int(omni_settings.get('first_sentence_steps', 24)),
            "guidance_scale": float(omni_settings.get('guidance_scale', 2.0)),
            "apply_smoothing_eq": bool(omni_settings.get('apply_smoothing_eq', True)),
            "seed": int(omni_settings.get('seed', 42)),
            "speed": float(tts_settings.get('speed', 1.0)),
        }

    def get_sample_rate(self) -> int:
        """Return the VoxCPM2 AudioVAE output sample rate."""
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

        For OmniVoice.cpp, cloning is making the reference available and optionally
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
        from services.omnivoice_cpp_engine import _get_manager, is_loaded
        from services.omnivoice_engine import (
            VOICE_DIR,
            ensure_voice_reference_transcript,
        )

        if not os.path.exists(reference_wav_path):
            print(f"[OmniVoiceCpp] Reference file not found: {reference_wav_path}")
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
                print(f"[OmniVoiceCpp] Copied reference to: {ref_path}")
            except Exception as e:
                print(f"[OmniVoiceCpp] Failed to copy reference: {e}")
                return None

        voice = {
            "displayName": display_name,
            "voiceId": display_name,
            "langCode": lang or "EN_US",
            "filePath": str(ref_path),
        }

        ref_text = ensure_voice_reference_transcript(str(ref_path))
        if ref_text:
            print(f"[OmniVoiceCpp] Transcript ready for {ref_path.name} ({len(ref_text)} chars)")

        # If the worker is already running, pretokenize in the background
        if is_loaded():
            try:
                manager = _get_manager()
                manager.pretokenize_voice(str(ref_path), ref_text=ref_text)
            except Exception as e:
                print(f"[OmniVoiceCpp] Pretokenize failed (non-fatal): {e}")

        # NOTE: Do NOT add to cache here - base class handles it with correct key
        print(f"[OmniVoiceCpp] Voice registered: {display_name}")

        return voice

    def delete_voice(self, voice_id: str) -> bool:
        """
        "Delete" a voice by clearing its prompt cache in the worker process.

        For OmniVoice.cpp, we don't delete the voice file itself (that's managed by
        the voice_references directory). Instead, we clear the cached voice prompt so
        the next synthesis will recompute it from the (potentially changed) reference
        file.

        Args:
            voice_id: Voice name to clear from prompt cache

        Returns:
            True if successfully signaled, False otherwise
        """
        try:
            from services.omnivoice_cpp_engine import _get_manager
            from services.omnivoice_engine import _resolve_voice

            # Resolve voice_id to file path (worker uses file paths as cache keys)
            voice_path = _resolve_voice(voice_id)
            if voice_path:
                _get_manager().clear_voice_prompt(voice_path)
                print(f"[OmniVoiceCpp] Cleared prompt cache for: {voice_id}")
                return True
            else:
                print(f"[OmniVoiceCpp] Voice not found for cache clearing: {voice_id}")
                return False
        except Exception as e:
            print(f"[OmniVoiceCpp] Failed to clear prompt cache: {e}")
            return False

    @_serialized_synthesis
    def synthesize_stream(self, text: str, voice_id: str,
                          on_chunk: Callable[[bytes, Optional[Dict]], None],
                          speaker_id: Optional[str] = None) -> bool:
        """
        Stream TTS synthesis using the OmniVoice.cpp model.

        Args:
            text: Text to synthesize
            voice_id: Voice name (matches file in voice_references)
            on_chunk: Callback function(pcm_bytes, word_alignment)
            speaker_id: Optional speaker ID for per-NPC settings

        Returns:
            True on success, False on error
        """
        from services.omnivoice_cpp_engine import _get_manager
        from services.omnivoice_engine import _resolve_voice

        voice_path = _resolve_voice(voice_id)
        if not voice_path:
            print(f"[OmniVoiceCpp] Voice not found: {voice_id}")
            return False

        print(f"[OmniVoiceCpp] Synthesizing: voice={voice_id}")

        manager = _get_manager()
        if not manager.ensure_started():
            print("[OmniVoiceCpp] Worker process not available")
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
        from services.omnivoice_cpp_engine import _get_manager, OUTPUT_SAMPLE_RATE
        from services.omnivoice_engine import _resolve_voice
        from .voice_utils import compute_reference_hash
        from utils.text_utils import preprocess_text

        config = self.get_config()
        num_steps = config.get("num_steps", 32)
        first_sentence_steps = config.get("first_sentence_steps", 24)
        base_cfg = config.get("guidance_scale", 2.0)
        seed = config.get("seed", 42)
        effective_sr = OUTPUT_SAMPLE_RATE
        eq_on_chunk = _wrap_omnivoice_eq(on_chunk) if config.get("apply_smoothing_eq", True) else on_chunk

        # Per-NPC temperature modifier → CFG boost (upward only, clamped to 10)
        npc_cfg = None
        if speaker_id:
            settings = load_settings()
            temp_mod = settings.get('tts', {}).get('npc_temp_modifiers', {}).get(speaker_id, 0.0)
            if temp_mod > 0:
                npc_cfg = min(base_cfg + temp_mod * 10.0, 10.0)
                print(f"[OmniVoiceCpp] Per-NPC CFG for {speaker_id}: {npc_cfg:.1f} "
                      f"(base {base_cfg:.1f} + temp mod {temp_mod:+.1f})")

        # Resolve default voice upfront
        voice_path = _resolve_voice(voice_id)
        if not voice_path:
            print(f"[OmniVoiceCpp] Voice not found: {voice_id}")
            return False

        voice_hash = compute_reference_hash(voice_path)
        if not voice_hash:
            print(f"[OmniVoiceCpp] Failed to compute voice hash: {voice_path}")
            return False

        # Cache for per-sentence voice resolution (narration support)
        _local_voice_cache = {voice_id: (voice_path, voice_hash)}

        def _resolve_voice_for_id(vid):
            """Resolve voice path/hash with caching."""
            if vid in _local_voice_cache:
                return _local_voice_cache[vid]
            vpath = _resolve_voice(vid)
            if not vpath:
                print(f"[OmniVoiceCpp] Narrator voice not found: {vid}, falling back to default")
                return voice_path, voice_hash
            vhash = compute_reference_hash(vpath)
            if not vhash:
                return voice_path, voice_hash
            _local_voice_cache[vid] = (vpath, vhash)
            return vpath, vhash

        manager = _get_manager()
        if not manager.ensure_started():
            print("[OmniVoiceCpp] Worker process not available")
            return False

        start_time = time.time()
        total_bytes = 0
        sentence_count = 0

        try:
            for item in sentences:
                if abort_check and abort_check():
                    print(f"[OmniVoiceCpp] Sentence streaming aborted after {sentence_count} sentences")
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
                    print(f"[OmniVoiceCppTiming] silence before_sentence={sentence_count + 1} "
                          f"duration={silence_duration:.2f}s bytes={len(silence_pcm)} "
                          f"start_bytes={silence_start} end_bytes={total_bytes}")

                # Confirm the byte position where this sentence starts.
                # Without this, boundary #1 remains unconfirmed for providers
                # that don't emit word timings, and subtitle progression stalls.
                sentence_idx = sentence_count  # 0-based index of upcoming sentence
                if sentence_idx > 0 and on_voice_switch:
                    try:
                        on_voice_switch(total_bytes, sentence_idx)
                        print(f"[OmniVoiceCppTiming] boundary_confirmed idx={sentence_idx} "
                              f"start_bytes={total_bytes} "
                              f"start_time={total_bytes / (effective_sr * 2):.2f}s "
                              f"source=provider_byte_position")
                    except Exception as e:
                        print(f"[OmniVoiceCpp] Boundary callback failed: {e}")

                sentence_count += 1
                voice_tag = f" [{per_voice_id}]" if per_voice_id != voice_id else ""
                sentence_start_bytes = total_bytes
                sentence_start_time = sentence_start_bytes / (effective_sr * 2)
                print(f"[OmniVoiceCpp] Streaming sentence {sentence_count}{voice_tag}: "
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
                    seed=seed,
                )
                total_bytes += bytes_produced
                print(f"[OmniVoiceCppTiming] sentence_done idx={sentence_count - 1} "
                      f"bytes={bytes_produced} total_bytes={total_bytes} "
                      f"duration={bytes_produced / (effective_sr * 2):.2f}s "
                      f"total_duration={total_bytes / (effective_sr * 2):.2f}s "
                      f"success={success} steps={steps} "
                      f"guidance_scale={npc_cfg if npc_cfg is not None else 'default'}")

                if not success:
                    print(f"[OmniVoiceCpp] Sentence synthesis failed at sentence {sentence_count}")
                    return total_bytes > 0

                if on_sentence_flushed:
                    on_sentence_flushed()

            proc_time = time.time() - start_time
            duration = total_bytes / (effective_sr * 2) if total_bytes > 0 else 0
            rtf = duration / proc_time if proc_time > 0 else 0

            print(f"[OmniVoiceCpp] Sentence streaming complete: {sentence_count} sentences, "
                  f"{total_bytes} bytes, {duration:.2f}s audio")
            print(f"[OmniVoiceCpp] Stats: {proc_time*1000:.1f}ms | RTF: {rtf:.2f}x")

            return total_bytes > 0

        except Exception as e:
            print(f"[OmniVoiceCpp] Sentence streaming failed: {e}")
            import traceback
            traceback.print_exc()
            return total_bytes > 0
