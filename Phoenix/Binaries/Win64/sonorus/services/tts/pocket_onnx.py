"""
Pocket TTS ONNX Provider

Clean provider that delegates to the pocket_tts_onnx component.
Follows existing Inworld/ElevenLabs provider pattern.

Uses pure ONNX inference without PyTorch dependencies.
"""
import os
import sys
from typing import Dict, List, Optional, Callable

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from .base import BaseTTSProvider, VoiceCache
from .voice_utils import parse_hashed_voice_name, compute_reference_hash
from utils.settings import load_settings

# Sample rate (Pocket TTS native)
SAMPLE_RATE = 24_000


# ============================================
# Pocket ONNX Voice Cache
# ============================================

class PocketOnnxVoiceCache(VoiceCache):
    """
    Pocket TTS ONNX voice cache - tracks local voice reference files.

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
        from services.pocket_tts_onnx import VOICE_DIR, _AUDIO_EXTS

        self._voices.clear()
        self._by_id.clear()

        if not VOICE_DIR.exists():
            print(f"[PocketONNX] Voice directory not found: {VOICE_DIR}")
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
            # Handle both "VoiceName.wav" and "VoiceName_reference_15s.wav"
            stem = path.stem
            if stem.endswith("_reference_15s"):
                voice_name = stem[:-14]  # Remove suffix
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
        print(f"[PocketONNX] Found {len(self._voices)} voice references")
        return True


# Module-level singleton cache
_voice_cache: PocketOnnxVoiceCache = None


def _get_voice_cache() -> PocketOnnxVoiceCache:
    """Get or create the singleton voice cache."""
    global _voice_cache
    if _voice_cache is None:
        _voice_cache = PocketOnnxVoiceCache()
    return _voice_cache


def clear_voice_cache():
    """Clear the module-level voice cache."""
    global _voice_cache
    if _voice_cache is not None:
        print("[PocketONNX] Clearing voice cache")
        _voice_cache = None


# ============================================
# Pocket TTS ONNX Provider
# ============================================

class PocketOnnxProvider(BaseTTSProvider):
    """
    Pocket TTS ONNX provider using local ONNX model inference.

    Features:
    - Local ONNX inference (no PyTorch, no API calls)
    - Voice cloning from reference files
    - Word-level timestamps via forced alignment
    - 24kHz native sample rate
    - Models downloaded from HuggingFace: KevinAHM/pocket-tts-onnx
    """

    @property
    def name(self) -> str:
        return "PocketONNX"

    def get_config(self) -> Dict:
        """Get Pocket TTS ONNX configuration from settings."""
        settings = load_settings()
        tts_settings = settings.get('tts', {})
        pocket_settings = tts_settings.get('pocket', {})

        return {
            "device": pocket_settings.get('device', 'cpu'),
            "temperature": float(pocket_settings.get('temperature', 0.7)),
            "lsd_steps": int(pocket_settings.get('lsd_steps', 10)),
            "eos_threshold": float(pocket_settings.get('eos_threshold', -4.0)),
            "cache_size": int(pocket_settings.get('cache_size', 50)),
            "speed": float(tts_settings.get('speed', 1.0)),
            "precision": pocket_settings.get('precision', 'int8'),
        }

    def get_sample_rate(self) -> int:
        return SAMPLE_RATE

    def get_buffer_seconds(self) -> float:
        return 1.0

    def get_default_language(self) -> Optional[str]:
        # Pocket TTS is multilingual, no default language needed
        return None

    def get_voice_cache(self) -> VoiceCache:
        return _get_voice_cache()

    def clone_voice(self, display_name: str, reference_wav_path: str,
                    lang: Optional[str] = None) -> Optional[Dict]:
        """
        "Clone" a voice by registering the reference file.

        For Pocket TTS ONNX, cloning is just making the reference available.
        The actual voice embedding is computed lazily on first use.

        Args:
            display_name: Voice name (e.g., "SebastianSallow")
            reference_wav_path: Path to reference WAV file
            lang: Language code (ignored for Pocket TTS)

        Returns:
            Voice dict on success, None on failure
        """
        import shutil
        from services.pocket_tts_onnx import VOICE_DIR

        if not os.path.exists(reference_wav_path):
            print(f"[PocketONNX] Reference file not found: {reference_wav_path}")
            return None

        # Ensure voice directory exists
        VOICE_DIR.mkdir(parents=True, exist_ok=True)

        # Copy reference to voice_references directory if not already there
        ref_path = VOICE_DIR / os.path.basename(reference_wav_path)
        if not ref_path.exists():
            try:
                shutil.copy2(reference_wav_path, ref_path)
                print(f"[PocketONNX] Copied reference to: {ref_path}")
            except Exception as e:
                print(f"[PocketONNX] Failed to copy reference: {e}")
                return None

        voice = {
            "displayName": display_name,
            "voiceId": display_name,
            "langCode": "EN_US",
            "filePath": str(ref_path),
        }

        # NOTE: Do NOT add to cache here - base class handles it with correct key
        print(f"[PocketONNX] Voice registered: {display_name}")

        return voice

    def delete_voice(self, voice_id: str) -> bool:
        """
        "Delete" a voice by clearing its embedding cache in the worker process.

        For Pocket TTS, we don't delete the voice file itself (that's managed by the
        voice_references directory). Instead, we clear the cached embedding so the
        next synthesis will recompute it from the (potentially changed) reference file.

        Args:
            voice_id: Voice name to clear from embedding cache

        Returns:
            True if successfully signaled, False otherwise
        """
        try:
            from services.pocket_tts_onnx import clear_voice_embedding, _resolve_voice

            # Resolve voice_id to file path (worker uses file paths as cache keys)
            voice_path = _resolve_voice(voice_id)
            if voice_path:
                clear_voice_embedding(voice_path)
                print(f"[PocketONNX] Cleared embedding cache for: {voice_id}")
                return True
            else:
                print(f"[PocketONNX] Voice not found for cache clearing: {voice_id}")
                return False
        except Exception as e:
            print(f"[PocketONNX] Failed to clear embedding cache: {e}")
            return False

    def synthesize_stream(self, text: str, voice_id: str,
                          on_chunk: Callable[[bytes, Optional[Dict]], None],
                          speaker_id: Optional[str] = None) -> bool:
        """
        Stream TTS synthesis using Pocket TTS ONNX model.

        Args:
            text: Text to synthesize
            voice_id: Voice name (matches file in voice_references)
            on_chunk: Callback function(pcm_bytes, word_alignment)
            speaker_id: Optional speaker ID for per-NPC settings

        Returns:
            True on success, False on error
        """
        from services.pocket_tts_onnx import get_synthesizer

        # Fixed temperature - Pocket TTS is tuned for 0.7
        temperature = 0.7
        print(f"[PocketONNX] Synthesizing: voice={voice_id}, temp={temperature:.2f}")

        synthesizer = get_synthesizer()
        return synthesizer.synthesize(text, voice_id, on_chunk, temperature=temperature)

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
                NOTE: PocketONNX calls this reliably once per sentence (synchronous).
                Inworld WS does NOT — it batches flushes. Do not assume 1:1 across providers.
            abort_check: Callable returning True to abort synthesis
            on_voice_switch: Optional callback(byte_position, sentence_idx) used by
                shared subtitle/routing code to confirm sentence boundaries.
                PocketONNX emits this at every sentence boundary because it does
                not provide word timestamps.

        Returns:
            True on success, False on error
        """
        import random
        import numpy as np
        from services.pocket_tts_onnx import (
            _get_manager, _resolve_voice, SAMPLE_RATE,
        )
        from .voice_utils import compute_reference_hash
        from utils.text_utils import preprocess_text, remove_brackets, normalize_for_tts

        temperature = 0.7

        # Resolve default voice upfront
        voice_path = _resolve_voice(voice_id)
        if not voice_path:
            print(f"[PocketONNX] Voice not found: {voice_id}")
            return False

        voice_hash = compute_reference_hash(voice_path)
        if not voice_hash:
            print(f"[PocketONNX] Failed to compute voice hash: {voice_path}")
            return False

        # Cache for per-sentence voice resolution (narration support)
        _voice_cache = {voice_id: (voice_path, voice_hash)}

        def _resolve_voice_for_id(vid):
            """Resolve voice path/hash with caching."""
            if vid in _voice_cache:
                return _voice_cache[vid]
            vpath = _resolve_voice(vid)
            if not vpath:
                print(f"[PocketONNX] Narrator voice not found: {vid}, falling back to default")
                return voice_path, voice_hash
            vhash = compute_reference_hash(vpath)
            if not vhash:
                return voice_path, voice_hash
            _voice_cache[vid] = (vpath, vhash)
            return vpath, vhash

        manager = _get_manager()
        if not manager.ensure_started():
            print("[PocketONNX] Worker process not available")
            return False

        import time
        start_time = time.time()
        total_bytes = 0
        sentence_count = 0
        cumulative_time = 0.0
        is_first_audio = True

        try:
            for item in sentences:
                if abort_check and abort_check():
                    print(f"[PocketONNX] Sentence streaming aborted after {sentence_count} sentences")
                    return total_bytes > 0

                # Unpack: either plain string or (text, per_voice_id) tuple
                if isinstance(item, tuple):
                    sentence, per_voice_id = item
                else:
                    sentence, per_voice_id = item, voice_id

                if not sentence or not sentence.strip():
                    continue

                # Preprocess sentence
                processed = preprocess_text(sentence)
                processed = remove_brackets(processed)
                processed = normalize_for_tts(processed)
                if not processed:
                    continue

                # Resolve voice for this sentence
                sent_voice_path, sent_voice_hash = _resolve_voice_for_id(per_voice_id)

                # Add silence gap between sentences
                if sentence_count > 0:
                    silence_duration = random.uniform(0.25, 1.0)
                    silence_samples = int(silence_duration * SAMPLE_RATE)
                    silence_pcm = np.zeros(silence_samples, dtype=np.int16).tobytes()
                    on_chunk(silence_pcm, None)
                    total_bytes += len(silence_pcm)
                    cumulative_time += silence_duration

                # Confirm the byte position where this sentence starts.
                # Without this, boundary #1 remains unconfirmed for providers
                # that don't emit word timings, and subtitle progression stalls.
                sentence_idx = sentence_count  # 0-based index of upcoming sentence
                if sentence_idx > 0 and on_voice_switch:
                    try:
                        on_voice_switch(total_bytes, sentence_idx)
                    except Exception as e:
                        print(f"[PocketONNX] Boundary callback failed: {e}")

                sentence_count += 1
                voice_tag = f" [{per_voice_id}]" if per_voice_id != voice_id else ""
                print(f"[PocketONNX] Streaming sentence {sentence_count}{voice_tag}: "
                      f"{processed[:60]}{'...' if len(processed) > 60 else ''}")

                success, bytes_produced, cumulative_time = manager.synthesize_sentence(
                    text=processed,
                    voice_path=sent_voice_path,
                    voice_hash=sent_voice_hash,
                    on_chunk=on_chunk,
                    temperature=temperature,
                    cumulative_time=cumulative_time,
                    is_first_audio_chunk=is_first_audio,
                )
                total_bytes += bytes_produced
                if bytes_produced > 0:
                    is_first_audio = False

                if not success:
                    print(f"[PocketONNX] Sentence synthesis failed at sentence {sentence_count}")
                    return total_bytes > 0

                if on_sentence_flushed:
                    on_sentence_flushed()

            proc_time = time.time() - start_time
            duration = total_bytes / (SAMPLE_RATE * 2) if total_bytes > 0 else 0
            rtf = duration / proc_time if proc_time > 0 else 0

            print(f"[PocketONNX] Sentence streaming complete: {sentence_count} sentences, "
                  f"{total_bytes} bytes, {duration:.2f}s audio")
            print(f"[PocketONNX] Stats: {proc_time*1000:.1f}ms | RTF: {rtf:.2f}x")

            return total_bytes > 0

        except Exception as e:
            print(f"[PocketONNX] Sentence streaming failed: {e}")
            import traceback
            traceback.print_exc()
            return total_bytes > 0
