"""
Base TTS Provider

Abstract base class and shared implementations for TTS providers.
Eliminates code duplication between Inworld and ElevenLabs providers.
"""
import os
import re
import sys
import time
import threading
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Callable, Tuple, Any, Iterable

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from constants import TTS_BUFFER_SECONDS
from utils.tts_archive import write_or_stage_history_entry_archive
from utils.viseme_utils import neutralize_narration_visemes
from .voice_utils import (
    find_voice_reference,
    compute_reference_hash,
    load_voice_hashes,
    save_voice_hash,
    remove_voice_hash,
    build_hashed_voice_name,
)

# Per-voice locking to prevent concurrent clone requests for same voice
_clone_locks: Dict[str, threading.Lock] = {}
_clone_locks_lock = threading.Lock()
_BRACKET_TAG_RE = re.compile(r"\[[^\]]*\]")
_SPEECH_TOKEN_RE = re.compile(r"[^\W_]+(?:['’-][^\W_]+)*", re.UNICODE)


def _speech_tokens(text: str) -> List[str]:
    """Return spoken word-like tokens, ignoring emote tags and punctuation-only timing entries."""
    cleaned = _BRACKET_TAG_RE.sub(" ", text or "")
    return _SPEECH_TOKEN_RE.findall(cleaned)


def _timed_speech_tokens(words: List[str], starts: List[float], ends: Optional[List[float]]) -> List[Dict]:
    """Flatten provider timing entries into spoken tokens with usable start/end estimates."""
    tokens = []
    for raw_idx, raw_word in enumerate(words or []):
        if raw_idx >= len(starts):
            break
        parts = _speech_tokens(str(raw_word or ""))
        if not parts:
            continue

        start = starts[raw_idx]
        end = ends[raw_idx] if ends and raw_idx < len(ends) else start
        try:
            start_f = float(start)
        except (TypeError, ValueError):
            continue
        try:
            end_f = float(end)
        except (TypeError, ValueError):
            end_f = start_f

        span = max(0.0, end_f - start_f)
        part_count = max(1, len(parts))
        for part_idx, part in enumerate(parts):
            part_start = start_f + (span * part_idx / part_count)
            part_end = start_f + (span * (part_idx + 1) / part_count)
            tokens.append({
                "token": part,
                "start": part_start,
                "end": part_end,
                "raw_index": raw_idx,
                "raw_word": raw_word,
                "part_index": part_idx,
                "part_count": part_count,
            })
    return tokens


def _get_clone_lock(cache_key: str) -> threading.Lock:
    """Get or create a lock for cloning a specific voice."""
    with _clone_locks_lock:
        if cache_key not in _clone_locks:
            _clone_locks[cache_key] = threading.Lock()
        return _clone_locks[cache_key]

# Lip sync module (optional)
try:
    from audio import lipsync
    LIPSYNC_AVAILABLE = True
except ImportError as e:
    LIPSYNC_AVAILABLE = False
    print(f"[WARN] audio.lipsync module not available: {e}")
    import traceback
    print(traceback.format_exc().rstrip())

# End marker trimmer for clean audio cutoffs
# DISABLED - alignment model timestamps too inaccurate for reliable trimming
END_TRIMMER_ENABLED = False
try:
    from audio.end_trimmer import EndMarkerTrimmer, pad_text_with_end_marker
    END_TRIMMER_AVAILABLE = END_TRIMMER_ENABLED
except ImportError:
    END_TRIMMER_AVAILABLE = False


# ============================================
# Voice Cache Base Class
# ============================================
class VoiceCache(ABC):
    """
    Base class for voice caching.

    Subclasses implement _make_cache_key() for provider-specific keying:
    - Inworld: "{name}_{lang}" (language-aware)
    - ElevenLabs: "{name}" (multilingual)
    """

    def __init__(self):
        self._voices: Dict[str, Dict] = {}  # key -> voice dict
        self._by_id: Dict[str, Dict] = {}   # voiceId -> voice dict
        self._loaded: bool = False
        self._duplicates_to_delete: List[Tuple[str, str]] = []  # (voiceId, displayName)

    @abstractmethod
    def _make_cache_key(self, name: str, lang: Optional[str] = None) -> str:
        """Generate cache key. Override for language-aware vs multilingual."""
        pass

    @abstractmethod
    def load(self) -> bool:
        """Load voices from provider API. Returns True on success."""
        pass

    def get(self, name: str, lang: Optional[str] = None) -> Optional[Dict]:
        """Get voice by display name and optional language."""
        if not self._loaded:
            self.load()
        return self._voices.get(self._make_cache_key(name, lang))

    def get_by_id(self, voice_id: str) -> Optional[Dict]:
        """Get voice by voiceId."""
        if not self._loaded:
            self.load()
        return self._by_id.get(voice_id)

    def list(self, lang: Optional[str] = None) -> List[Dict]:
        """List all voices, optionally filtered by language."""
        if not self._loaded:
            self.load()
        if lang is None:
            return list(self._voices.values())
        return [v for v in self._voices.values() if v.get("langCode") == lang]

    def refresh(self) -> bool:
        """Force reload voices from API."""
        self._loaded = False
        return self.load()

    def add(self, voice: Dict, lang: Optional[str] = None):
        """Add a voice to the cache."""
        display_name = voice.get("displayName", "")
        voice_id = voice.get("voiceId", "")
        voice_lang = lang or voice.get("langCode")

        key = self._make_cache_key(display_name, voice_lang)
        self._voices[key] = voice
        if voice_id:
            self._by_id[voice_id] = voice

    def remove(self, name: str, lang: Optional[str] = None) -> Optional[Dict]:
        """Remove a voice from the cache by name and language. Returns the removed voice or None."""
        key = self._make_cache_key(name, lang)
        voice = self._voices.pop(key, None)
        if voice and voice.get("voiceId"):
            self._by_id.pop(voice["voiceId"], None)
        return voice


# ============================================
# Base TTS Provider
# ============================================
class BaseTTSProvider(ABC):
    """
    Abstract base class for TTS providers.

    Implements common speak/prepare_tts logic.
    Subclasses implement provider-specific API calls.
    """

    # ----------------------------------------
    # Abstract Properties/Methods (MUST override)
    # ----------------------------------------

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider name for logging (e.g., 'Inworld', 'ElevenLabs')."""
        pass

    @abstractmethod
    def get_config(self) -> Dict:
        """Get provider configuration from settings."""
        pass

    @abstractmethod
    def get_sample_rate(self) -> int:
        """Get audio sample rate for this provider."""
        pass

    def get_buffer_seconds(self) -> float:
        """Seconds of audio to buffer before playback starts. Override per-provider."""
        return TTS_BUFFER_SECONDS

    # Provider name prefixes that should NOT be used with other providers
    _MODEL_PREFIX_BLOCKLIST: Dict[str, List[str]] = {
        'inworld': ['eleven_', 'eleven-'],
        'elevenlabs': ['inworld-'],
    }

    def resolve_model_override(self, default_model: str, speaker_id: Optional[str] = None) -> str:
        """
        Resolve per-character TTS model override.

        Checks player_voice_model (for player characters) and npc_model_overrides
        (for NPCs). Validates that the override model doesn't belong to a different
        provider based on known prefix patterns.

        Args:
            default_model: The provider's default model from config
            speaker_id: Character name (e.g., "SebastianSallow", "PlayerMale")

        Returns:
            Model ID to use (override if valid, otherwise default)
        """
        if not speaker_id:
            return default_model

        from utils.settings import load_settings
        settings = load_settings()

        override = None

        # Check player voice model override
        if speaker_id.startswith('Player'):
            override = settings.get('conversation', {}).get('player_voice_model', '')

        # Check per-NPC model override (falls through if player override was empty)
        if not override:
            override = settings.get('tts', {}).get('npc_model_overrides', {}).get(speaker_id, '')

        if not override:
            return default_model

        # Validate: reject models that clearly belong to a different provider
        provider_name = self.name.lower()
        blocked = self._MODEL_PREFIX_BLOCKLIST.get(provider_name, [])
        override_lower = override.lower()
        for prefix in blocked:
            if override_lower.startswith(prefix):
                print(f"[{self.name}] Ignoring model override '{override}' for {speaker_id} "
                      f"— '{prefix}' prefix belongs to a different provider")
                return default_model

        print(f"[{self.name}] Using model override '{override}' for {speaker_id}")
        return override

    @abstractmethod
    def get_voice_cache(self) -> VoiceCache:
        """Get provider's voice cache instance."""
        pass

    @abstractmethod
    def clone_voice(self, display_name: str, reference_wav_path: str,
                    lang: Optional[str] = None) -> Optional[Dict]:
        """Clone a voice from reference audio. Returns voice dict or None."""
        pass

    @abstractmethod
    def synthesize_stream(self, text: str, voice_id: str,
                          on_chunk: Callable[[bytes, Optional[Dict]], None],
                          speaker_id: Optional[str] = None) -> bool:
        """
        Stream TTS synthesis.

        Args:
            text: Text to synthesize
            voice_id: Provider-specific voice ID
            on_chunk: Callback function(pcm_bytes, word_alignment)
                - pcm_bytes: Raw PCM audio data
                - word_alignment: Dict with word timing info or None
            speaker_id: Optional speaker ID for per-NPC settings (e.g., temp modifier)

        Returns:
            True on success, False on error
        """
        pass

    # ----------------------------------------
    # Optional Hooks (CAN override)
    # ----------------------------------------

    def delete_voice(self, voice_id: str) -> bool:
        """
        Delete a voice from the provider (optional, for hash-based recloning).

        Override this method to enable automatic deletion of old voices
        when reference files change. Default implementation does nothing.

        Args:
            voice_id: Provider-specific voice ID to delete

        Returns:
            True if deleted successfully, False otherwise
        """
        return False

    def cleanup_duplicate_voices(self) -> int:
        """
        Delete duplicate voices that were detected during cache load.

        Returns:
            Number of voices successfully deleted
        """
        cache = self.get_voice_cache()
        duplicates = cache._duplicates_to_delete

        if not duplicates:
            return 0

        print(f"[{self.name}] Cleaning up {len(duplicates)} duplicate voice(s)...")
        deleted_count = 0

        for voice_id, display_name in duplicates:
            try:
                if self.delete_voice(voice_id):
                    deleted_count += 1
            except Exception as e:
                print(f"[{self.name}] Failed to delete {display_name}: {e}")

        cache._duplicates_to_delete = []
        print(f"[{self.name}] Cleanup complete: {deleted_count}/{len(duplicates)} deleted")
        return deleted_count

    def on_voice_used(self, voice: Dict) -> None:
        """Called when a cloned voice is used. Override for usage tracking."""
        pass

    def should_reclone_after_synthesis_failure(self, voice_id: str) -> bool:
        """Whether the last synthesis failure means the provider-side voice is gone."""
        return False

    def invalidate_cached_voice(self, character_name: str, lang: Optional[str], voice_id: Optional[str]) -> None:
        """Drop a stale cached voice so get_or_create_voice can reclone it."""
        cache = self.get_voice_cache()
        cached_voice = cache._by_id.get(voice_id) if voice_id else None
        cached_lang = cached_voice.get("langCode") if cached_voice else None
        resolved_lang = cached_lang
        if not resolved_lang and lang:
            from constants import get_voice_language
            resolved_lang = get_voice_language(lang)
        cache.remove(character_name, resolved_lang or lang)
        if voice_id:
            cache._by_id.pop(voice_id, None)

    def _finalize_tts_archive(self, history_entry: Optional[Dict], speaker_id: str,
                              text: str, pcm_bytes: bytearray,
                              sample_rate: int, channels: int = 1) -> None:
        """Persist or stage synthesized PCM for later WAV export."""
        if not pcm_bytes:
            return

        archive_speaker_id = speaker_id
        archive_text = text or ""
        if isinstance(history_entry, dict):
            archive_speaker_id = (
                history_entry.get("_tts_archive_speaker_id")
                or history_entry.get("voiceName")
                or history_entry.get("speaker")
                or archive_speaker_id
            )
            if history_entry.get("text"):
                archive_text = history_entry.get("text") or archive_text

        try:
            write_or_stage_history_entry_archive(
                entry=history_entry,
                pcm_bytes=bytes(pcm_bytes),
                sample_rate=sample_rate,
                channels=channels,
                speaker_id=archive_speaker_id,
                text=archive_text,
            )
        except Exception as e:
            print(f"[{self.name}] TTS archive failed: {e}")

    def get_default_language(self) -> Optional[str]:
        """Get default language. Returns None for multilingual providers."""
        return None

    # ----------------------------------------
    # Shared Implementations
    # ----------------------------------------

    def get_or_create_voice(self, character_name: str,
                            lang: Optional[str] = None,
                            lua_socket: Any = None) -> Optional[Dict]:
        """
        Get a voice for a character, cloning it if necessary.

        Implements hash-based voice tracking:
        1. Find reference file and compute hash
        2. Look up voice by character name in cache
        3. If found, verify hash matches current reference file
        4. If no stored hash (first run), adopt the voice and save hash
        5. If hash mismatch, reclone with new hash-suffixed name
        6. Delete old voice after successful reclone (best-effort)

        Args:
            character_name: Character name (e.g., "SebastianSallow")
            lang: Language code. Uses provider default or game language if None.
            lua_socket: Socket server for sending notifications

        Returns:
            Voice dict with voiceId

        Raises:
            Exception: With specific reason if voice cannot be obtained
        """
        if lang is None:
            lang = self.get_default_language()
            # If provider is multilingual (returns None), use game language
            if lang is None:
                from services.tts.voice_utils import get_game_language
                lang = get_game_language()

        # Map to voice language — undubbed languages use EN_US voice refs/clones
        from constants import get_voice_language
        lang = get_voice_language(lang)

        cache = self.get_voice_cache()
        if not cache._loaded:
            cache.load()

        # Find reference file first (needed for hash computation)
        ref_path = find_voice_reference(character_name, "15s", language=lang)

        # Build cache key for locking
        legacy_cache_key = cache._make_cache_key(character_name, lang)
        clone_lock = _get_clone_lock(legacy_cache_key)

        with clone_lock:
            # Compute hash if reference exists
            ref_hash = None
            if ref_path:
                ref_hash = compute_reference_hash(ref_path)

            # Try to find voice by original name (cache key is always original name)
            # The referenceHash is stored as a field in the voice dict, not in the cache key
            voice = cache.get(character_name, lang)

            if voice:
                # Voice exists in cache - verify hash matches current reference
                stored_hash = voice.get("referenceHash")
                voice_hashes = load_voice_hashes()
                legacy_key = f"{character_name}_{lang}" if lang else character_name

                if stored_hash is None:
                    # Voice in cache has no hash - check persistent storage
                    stored_hash = voice_hashes.get(legacy_key)

                if stored_hash is None:
                    # First run with this voice (no stored hash) - adopt it
                    if ref_hash:
                        print(f"[{self.name}] Adopting voice {character_name} with hash {ref_hash}")
                        save_voice_hash(legacy_key, ref_hash)
                        voice["referenceHash"] = ref_hash
                    print(f"[{self.name}] Voice found: {character_name}")
                    return voice

                if stored_hash == ref_hash:
                    # Hash matches - use existing voice
                    print(f"[{self.name}] Voice found (hash valid): {character_name}")
                    return voice

                # Hash mismatch - reference file changed, need to reclone
                print(f"[{self.name}] Reference changed for {character_name}: {stored_hash} -> {ref_hash}")

                if not ref_path:
                    print(f"[{self.name}] No reference file for recloning, using existing voice")
                    return voice

                # Remember old voice for deletion after successful clone
                old_voice = voice
                old_voice_id = old_voice.get("voiceId")

                # Continue to cloning below with the new hash
            else:
                old_voice = None
                old_voice_id = None

            # Clone new voice (no existing voice, or hash mismatch)
            if not ref_path:
                print(f"[{self.name}] No reference file for: {character_name} (language: {lang})")
                raise Exception(f"Voice reference file not found for '{character_name}' in language '{lang}'. Ensure voice references are extracted for this language.")

            print(f"[{self.name}] Using reference: {os.path.basename(ref_path)}")

            # Build clone name with hash
            if ref_hash:
                clone_name = build_hashed_voice_name(character_name, lang, ref_hash)
            elif lang and lang != "EN_US":
                clone_name = f"{character_name}_{lang}"
            else:
                clone_name = character_name

            print(f"[{self.name}] Cloning voice: {clone_name}")

            # Notify player that we're cloning
            if lua_socket:
                lua_socket.send_notification("Cloning voice, please wait...")

            cloned = self.clone_voice(clone_name, ref_path, lang)
            if not cloned:
                raise Exception(f"Voice cloning failed for '{character_name}'. Check the server logs for details.")

            # Store hash in voice dict
            if ref_hash:
                cloned["referenceHash"] = ref_hash

            # Add to cache with ORIGINAL character_name so future lookups find it
            cache_key = cache._make_cache_key(character_name, lang)
            cache._voices[cache_key] = cloned
            if cloned.get("voiceId"):
                cache._by_id[cloned["voiceId"]] = cloned

            # Delete old voice after successful clone (best-effort, don't block on failure)
            if old_voice_id:
                try:
                    print(f"[{self.name}] Deleting old voice: {old_voice_id}")
                    deleted = self.delete_voice(old_voice_id)
                    if deleted:
                        print(f"[{self.name}] Old voice deleted successfully")
                        # Clean up hash tracking for old voice
                        legacy_key = f"{character_name}_{lang}" if lang else character_name
                        remove_voice_hash(legacy_key)
                    else:
                        print(f"[{self.name}] Old voice deletion not implemented or failed (non-critical)")
                except Exception as e:
                    # Don't fail the voice creation if deletion fails
                    print(f"[{self.name}] Failed to delete old voice (non-critical): {e}")
            elif old_voice:
                # old_voice exists but has no voiceId - shouldn't happen normally
                print(f"[{self.name}] WARNING: Old voice has no voiceId, cannot delete: {old_voice.get('displayName', 'unknown')}")

            return cloned

    def speak(self, text: str, character_name: str,
              lang: Optional[str] = None,
              on_start: Optional[Callable] = None,
              on_stop: Optional[Callable[[bool], None]] = None,
              on_download_complete: Optional[Callable] = None,
              lua_socket: Any = None,
              initial_positions: Optional[Dict] = None,
              turn_id: Optional[str] = None,
              abort_check: Optional[Callable[[], bool]] = None,
              history_entry: Optional[Dict] = None,
              profiler: Any = None,
              reverb_auxbus: Optional[str] = None,
              reverb_send: float = 1.0) -> Dict:
        """
        Speak text as a character with 3D audio.
        Streams TTS and plays audio in real-time.

        Uses PlaybackCoordinator for synchronized lipsync:
        1. Accumulates visemes during pre-buffering
        2. Sends lipsync_start with initial visemes
        3. Waits for lipsync_ready from Lua
        4. Starts audio with continuous sync

        Args:
            text: Text to speak
            character_name: Character whose voice to use
            lang: Language code (provider-specific)
            on_start: Callback when audio playback actually starts
            on_stop: Callback when audio playback ends. Receives True only when
                playback completed normally, False for aborted/interrupted/error paths.
            on_download_complete: Callback when TTS download finishes
            lua_socket: Socket server for real-time position updates
            initial_positions: Dict with camX/Y/Z, camYaw, npcX/Y/Z for 3D position
            turn_id: Turn identifier for coordinator
            abort_check: Callable that returns True if we should abort

        Returns:
            {"success": bool, "word_timings": list, "error": str or None}
        """
        # Check for abort before starting
        if abort_check and abort_check():
            return {"success": False, "word_timings": [], "error": "Aborted"}

        try:
            from audio import create_tts_stream, get_player
        except ImportError as e:
            return {"success": False, "word_timings": [], "error": f"audio3d not available: {e}"}

        # Get coordinator for synchronized playback
        try:
            from audio.playback import get_coordinator
            coordinator = get_coordinator()
        except ImportError:
            coordinator = None

        # Get or create voice
        voice = self.get_or_create_voice(character_name, lang, lua_socket)
        if not voice:
            return {"success": False, "word_timings": [], "error": f"No voice for {character_name}"}

        voice_id = voice.get("voiceId")
        if not voice_id:
            return {"success": False, "word_timings": [], "error": "Voice has no voiceId"}

        # Track usage for LRU (provider-specific hook)
        self.on_voice_used(voice)

        # Create TTS stream
        sample_rate = self.get_sample_rate()
        channels = 1
        bytes_per_second = sample_rate * 2 * channels  # 16-bit = 2 bytes per sample
        tts_stream = create_tts_stream(sample_rate=sample_rate, channels=channels)
        word_timings = []
        total_bytes = [0]
        archive_pcm = bytearray()
        buffer_ready = threading.Event()
        tts_done = threading.Event()
        tts_error = [None]

        # Pre-buffer: wait for enough audio before starting playback
        buffer_seconds = self.get_buffer_seconds()
        min_buffer_bytes = bytes_per_second * buffer_seconds

        # Create turn for coordinator (accumulates visemes)
        # use_3d=False for player voice (when initial_positions is None)
        if not turn_id:
            turn_id = f"speak_{int(time.time() * 1000)}"
        use_3d = initial_positions is not None
        turn = coordinator.create_turn(turn_id, speaker_id=character_name, use_3d=use_3d,
                                       reverb_auxbus=reverb_auxbus, reverb_send=reverb_send) if coordinator else None
        if turn:
            turn.original_text = text  # Store for interrupt trimming

        first_chunk_received = [False]  # Track first chunk for profiling
        first_visemes_received = [False]  # Track first visemes
        chunk_count = [0]

        def on_chunk(pcm_bytes, word_timing):
            chunk_count[0] += 1

            # Profile first chunk arrival
            if not first_chunk_received[0]:
                first_chunk_received[0] = True
                if profiler:
                    profiler.mark("tts_first_chunk")
                print(f"[Speak] PROFILE: First audio chunk received ({len(pcm_bytes)} bytes)")

            # Calculate base_time BEFORE adding this chunk's bytes
            chunk_start_bytes = total_bytes[0]
            base_time = chunk_start_bytes / bytes_per_second

            tts_stream.feed(pcm_bytes)
            if pcm_bytes:
                archive_pcm.extend(pcm_bytes)
            total_bytes[0] += len(pcm_bytes)

            # Process word timing into visemes FIRST (before signaling buffer ready)
            if LIPSYNC_AVAILABLE:
                if word_timing:
                    # Word-level alignment available (e.g. Inworld, ElevenLabs)
                    visemes = lipsync.process_word_alignment(
                        word_alignment=word_timing,
                        lang=lang,
                        auto_send=False,
                        pcm_data=pcm_bytes,
                        text=text,
                        sample_rate=sample_rate,
                        base_time=base_time
                    )
                else:
                    # No alignment - generate amplitude visemes from PCM (Pocket TTS)
                    visemes = lipsync.amplitude_visemes_for_audio(
                        pcm_bytes, sample_rate
                    )
                    # Offset timestamps to chunk position in total audio
                    for v in visemes:
                        v['t'] += base_time
                if turn and visemes:
                    turn.add_visemes(visemes)
                    # Profile first visemes
                    if not first_visemes_received[0] and len(turn.viseme_buffer) > 0:
                        first_visemes_received[0] = True
                        if profiler:
                            profiler.mark("tts_first_visemes")
                        print(f"[Speak] PROFILE: First visemes generated ({len(visemes)} visemes, chunk #{chunk_count[0]})")

            if word_timing:
                word_timings.append(word_timing)
                # Store in turn for interrupt trimming
                if turn:
                    turn.add_word_timing(word_timing)

            # THEN check if we have enough buffered to start playback
            if not buffer_ready.is_set():
                viseme_count = len(turn.viseme_buffer) if turn else 0
                has_enough_audio = total_bytes[0] >= min_buffer_bytes
                has_visemes = viseme_count > 0

                # Log buffer status periodically
                if chunk_count[0] <= 5 or chunk_count[0] % 5 == 0:
                    buffer_secs = total_bytes[0] / bytes_per_second
                    print(f"[Speak] PROFILE: Chunk #{chunk_count[0]}: {buffer_secs:.2f}s audio, {viseme_count} visemes, ready={has_enough_audio and has_visemes}")

                if has_enough_audio and has_visemes:
                    buffer_secs = total_bytes[0] / bytes_per_second
                    print(f"[Speak] Buffer ready: {total_bytes[0]} bytes ({buffer_secs:.1f}s), {viseme_count} visemes")
                    if profiler:
                        profiler.mark("tts_buffer_ready")
                    buffer_ready.set()

        def run_tts():
            nonlocal voice, voice_id

            def synthesize_once(current_voice_id):
                if END_TRIMMER_AVAILABLE:
                    padded_text = pad_text_with_end_marker(text)
                    trimmer = EndMarkerTrimmer(
                        original_text=text,
                        on_chunk=on_chunk,
                        sample_rate=sample_rate,
                        bytes_per_sample=2  # 16-bit PCM
                    )
                    success = self.synthesize_stream(padded_text, current_voice_id, trimmer.process_chunk, speaker_id=character_name)
                    trimmer.flush()  # Flush any remaining buffered audio
                    return success
                return self.synthesize_stream(text, current_voice_id, on_chunk, speaker_id=character_name)

            try:
                if profiler:
                    profiler.mark("tts_synthesis_start")
                print(f"[Speak] PROFILE: TTS synthesis starting...")

                success = synthesize_once(voice_id)
                if (
                    not success
                    and total_bytes[0] == 0
                    and self.should_reclone_after_synthesis_failure(voice_id)
                ):
                    print(f"[{self.name}] Cached voice is stale; recloning {character_name} and retrying synthesis")
                    self.invalidate_cached_voice(character_name, lang, voice_id)
                    voice = self.get_or_create_voice(character_name, lang, lua_socket)
                    voice_id = voice.get("voiceId") if voice else None
                    if voice_id:
                        self.on_voice_used(voice)
                        success = synthesize_once(voice_id)
                    else:
                        success = False

                if profiler:
                    profiler.mark("synthesize_stream done")
                # If we never hit the buffer threshold, signal ready anyway (short utterances)
                if not buffer_ready.is_set():
                    buffer_secs = total_bytes[0] / bytes_per_second
                    print(f"[Speak] Short utterance ({buffer_secs:.1f}s) - starting playback")
                    buffer_ready.set()
                # Signal download complete
                if on_download_complete and success:
                    on_download_complete()
                if not success:
                    tts_error[0] = "TTS synthesis failed"
            except Exception as e:
                tts_error[0] = str(e)
            finally:
                self._finalize_tts_archive(
                    history_entry=history_entry,
                    speaker_id=character_name,
                    text=text,
                    pcm_bytes=archive_pcm,
                    sample_rate=sample_rate,
                    channels=channels,
                )
                tts_stream.finish()
                tts_done.set()

        # Helper to safely call profiler
        def prof(label):
            if profiler:
                profiler.mark(label)

        # Start TTS thread
        prof("TTS thread starting")
        tts_thread = threading.Thread(target=run_tts, daemon=True)
        tts_thread.start()

        # Wait for buffer to fill (or TTS to complete for short utterances)
        print(f"[Speak] Pre-buffering (waiting for {buffer_seconds}s of audio)...")
        prof("waiting for buffer_ready")
        if not buffer_ready.wait(timeout=30.0):
            return {"success": False, "word_timings": [], "error": "Timeout waiting for TTS buffer"}
        prof("buffer_ready signaled")

        # Check for abort after buffering
        if abort_check and abort_check():
            print("[Speak] Aborted after buffering")
            return {"success": False, "word_timings": [], "error": "Aborted"}

        if tts_error[0]:
            return {"success": False, "word_timings": [], "error": tts_error[0]}

        # Signal playback starting
        if on_start:
            on_start()
        print("[Speak] Playing...")
        prof("audio playback starting")

        # Play audio (blocks until done)
        player = get_player()

        # Set socket for real-time position updates
        if lua_socket:
            player.position_reader.set_socket(lua_socket)

        # Set initial 3D positions DIRECTLY (eliminates race condition)
        if initial_positions and initial_positions.get("npcX") is not None:
            cam = (initial_positions.get("camX", 0), initial_positions.get("camY", 0), initial_positions.get("camZ", 0))
            npc = (initial_positions.get("npcX", 0), initial_positions.get("npcY", 0), initial_positions.get("npcZ", 0))
            yaw = initial_positions.get("camYaw", 0)
            player.position_reader.set_initial_positions(cam, yaw, npc)
        else:
            # No positions = player voice (use_3d=False handles centered stereo)
            print("[Speak] No 3D positions - using centered stereo playback")

        # Use coordinator for synchronized playback if available
        if coordinator and turn:
            turn.audio_stream = tts_stream
            print(f"[Speak] Starting with coordinator: {len(turn.viseme_buffer)} initial visemes")
            success = coordinator.play_turn(turn_id, player, blocking=True, abort_check=abort_check)
        else:
            # Fallback: direct playback (no sync)
            success = player.play_stream(tts_stream, use_3d=use_3d, abort_check=abort_check)

        # Signal playback ended
        if on_stop:
            on_stop(bool(success))

        tts_thread.join(timeout=60.0)

        if tts_error[0]:
            return {"success": False, "word_timings": word_timings, "error": tts_error[0]}

        return {"success": success, "word_timings": word_timings, "error": None}

    @staticmethod
    def _count_speech_words(text: str) -> int:
        """Count spoken words, excluding bracket tags like [laugh]."""
        return len(_speech_tokens(text))

    @staticmethod
    def _short_log_text(text: str, limit: int = 120) -> str:
        text = re.sub(r'\s+', ' ', text or '').strip()
        if len(text) <= limit:
            return text
        return text[:limit - 3] + "..."

    def _log_sentence_boundaries(self, sentence_boundaries: List[Dict],
                                 label: str, bytes_per_second: int) -> None:
        print(f"[TTSBoundaryTable] label={label} count={len(sentence_boundaries)} "
              f"bytes_per_second={bytes_per_second}")
        for idx, boundary in enumerate(sentence_boundaries):
            print(
                f"[TTSBoundary] label={label} idx={idx}/{len(sentence_boundaries)} "
                f"source={boundary.get('start_time_source', 'unknown')} "
                f"start_time={float(boundary.get('start_time', 0.0) or 0.0):.2f}s "
                f"start_bytes={boundary.get('start_bytes')} "
                f"confirmed={bool(boundary.get('start_time_confirmed'))} "
                f"narration={bool(boundary.get('is_narration', False))} "
                f"words={self._count_speech_words(boundary.get('text', ''))} "
                f"text=\"{self._short_log_text(boundary.get('text', ''))}\""
            )

    def _update_sentence_boundaries_from_word_timing(
        self,
        sentence_boundaries: List[Dict],
        word_timing: Optional[Dict],
        timed_words_total: List[int],
    ) -> None:
        """Refine boundary start_time values using absolute word timing timestamps."""
        if not word_timing or not sentence_boundaries:
            return

        new_words = word_timing.get("words", [])
        new_starts = word_timing.get("wordStartTimeSeconds", [])
        new_ends = word_timing.get("wordEndTimeSeconds", [])
        if not new_words or not new_starts:
            return

        token_spans = _timed_speech_tokens(new_words, new_starts, new_ends)
        prev_total = timed_words_total[0]
        new_total = prev_total + len(token_spans)
        first_start = new_starts[0] if new_starts else None
        last_end = new_ends[-1] if new_ends else None
        raw_preview = " ".join(str(w) for w in new_words[:4])
        token_preview = " ".join(t["token"] for t in token_spans[:6])
        print(f"[WordTiming] raw_words={len(new_words)} speech_tokens={len(token_spans)} "
              f"cumulative={prev_total}->{new_total} "
              f"range={first_start if first_start is not None else 'n/a'}-"
              f"{last_end if last_end is not None else 'n/a'} "
              f"first=\"{new_words[0]}\" last=\"{new_words[-1]}\" "
              f"raw_preview=\"{self._short_log_text(raw_preview, 80)}\" "
              f"tokens=\"{self._short_log_text(token_preview, 80)}\"")
        if not token_spans:
            return

        cumulative = 0
        for si, boundary in enumerate(sentence_boundaries):
            cumulative += self._count_speech_words(boundary.get('text', ''))
            # Use confirmation state, not provisional start_time value.
            # For async streaming providers, later sentences may be yielded after
            # some bytes are already buffered, so start_time can be non-zero but
            # still unconfirmed and in need of correction.
            if si + 1 < len(sentence_boundaries) and prev_total <= cumulative < new_total:
                local_idx = cumulative - prev_total
                token_info = token_spans[local_idx] if local_idx < len(token_spans) else None
                candidate = token_info["start"] if token_info else None
                if sentence_boundaries[si + 1].get('start_time_confirmed', False):
                    print(f"[BoundaryCorrectionSkipped] idx={si + 1}/{len(sentence_boundaries)} "
                          f"confirmed=true source={sentence_boundaries[si + 1].get('start_time_source', 'unknown')} "
                          f"current={sentence_boundaries[si + 1].get('start_time')} "
                          f"candidate={candidate if candidate is not None else 'n/a'} "
                          f"word_boundary_index={cumulative} local_idx={local_idx} "
                          f"token=\"{token_info['token'] if token_info else 'n/a'}\" "
                          f"raw=\"{token_info['raw_word'] if token_info else 'n/a'}\" "
                          f"text=\"{self._short_log_text(sentence_boundaries[si + 1].get('text', ''))}\"")
                else:
                    if candidate is not None:
                        old_start = sentence_boundaries[si + 1].get('start_time')
                        old_source = sentence_boundaries[si + 1].get('start_time_source', 'unknown')
                        sentence_boundaries[si + 1]['start_time'] = candidate
                        sentence_boundaries[si + 1]['start_time_source'] = 'word_alignment'
                        sentence_boundaries[si + 1]['start_time_confirmed'] = True
                        print(f"[BoundaryCorrection] idx={si + 1}/{len(sentence_boundaries)} "
                              f"old={old_start if old_start is not None else 'n/a'} "
                              f"old_source={old_source} new={candidate:.2f}s "
                              f"word_boundary_index={cumulative} local_idx={local_idx} "
                              f"token=\"{token_info['token']}\" raw=\"{token_info['raw_word']}\" "
                              f"text=\"{self._short_log_text(sentence_boundaries[si + 1].get('text', ''))}\"")
            if cumulative >= new_total:
                break

        timed_words_total[0] = new_total

    def _synthesize_sentence_stream(
        self,
        sentence_gen: Iterable,
        voice_id: str,
        on_chunk: Callable[[bytes, Optional[Dict]], None],
        speaker_id: str,
        total_bytes: List[int],
        bytes_per_second: int,
        sentence_boundaries: List[Dict],
        original_sentences: Optional[List] = None,
        narrator_voice_id: Optional[str] = None,
        lang: Optional[str] = None,
        abort_check: Optional[Callable[[], bool]] = None,
        log_prefix: str = "[SpeakStream]",
    ) -> bool:
        """
        Shared sentence-stream synthesis path used by live streaming and pre-buffer.
        """
        if hasattr(self, 'synthesize_stream_sentences'):
            current_voice_id = voice_id
            cached_items = []

            def tracking_gen(replay_items=None):
                sent_idx = 0
                if replay_items is not None:
                    import itertools
                    source = itertools.chain(replay_items, sentence_gen)
                else:
                    source = sentence_gen
                for item in source:
                    if replay_items is None:
                        cached_items.append(item)
                    if abort_check and abort_check():
                        print(f"{log_prefix} Sentence stream aborted before sentence {sent_idx}")
                        return
                    if isinstance(item, tuple):
                        sentence, is_narration = item
                    else:
                        sentence, is_narration = item, False

                    if not sentence:
                        continue

                    start_time = total_bytes[0] / bytes_per_second if bytes_per_second > 0 else 0.0

                    if original_sentences and sent_idx < len(original_sentences):
                        orig = original_sentences[sent_idx]
                        if isinstance(orig, tuple):
                            clean_text, clean_is_narration = orig
                            is_narration = bool(clean_is_narration)
                        else:
                            clean_text = orig
                    else:
                        clean_text = sentence

                    sentence_boundaries.append({
                        'text': clean_text,
                        'start_time': start_time,
                        'is_narration': is_narration,
                        'start_bytes': 0 if sent_idx == 0 else None,
                        'start_time_confirmed': (sent_idx == 0),
                        'start_time_source': 'first_sentence' if sent_idx == 0 else 'provisional_total_bytes',
                    })
                    print(f"{log_prefix} Boundary created idx={sent_idx} "
                          f"source={sentence_boundaries[-1]['start_time_source']} "
                          f"provisional_start={start_time:.2f}s total_bytes={total_bytes[0]} "
                          f"confirmed={sentence_boundaries[-1]['start_time_confirmed']} "
                          f"narration={is_narration} "
                          f"text=\"{self._short_log_text(clean_text)}\"")
                    sent_idx += 1

                    if narrator_voice_id and is_narration:
                        yield (sentence, narrator_voice_id)
                    elif narrator_voice_id:
                        yield (sentence, current_voice_id)
                    else:
                        yield sentence

            def on_voice_switch(byte_position: int, sentence_idx: int):
                if sentence_idx < len(sentence_boundaries):
                    sentence_boundaries[sentence_idx]['start_bytes'] = byte_position
                    if bytes_per_second > 0:
                        sentence_boundaries[sentence_idx]['start_time'] = byte_position / bytes_per_second
                    sentence_boundaries[sentence_idx]['start_time_source'] = 'voice_switch_bytes'
                    sentence_boundaries[sentence_idx]['start_time_confirmed'] = True
                    print(f"{log_prefix} Voice switch at sentence {sentence_idx}: "
                          f"{byte_position} bytes = {byte_position / max(1, bytes_per_second):.2f}s")

            success = self.synthesize_stream_sentences(
                tracking_gen(), current_voice_id, on_chunk,
                speaker_id=speaker_id,
                abort_check=abort_check,
                on_voice_switch=on_voice_switch,
            )
            if (
                not success
                and total_bytes[0] == 0
                and self.should_reclone_after_synthesis_failure(current_voice_id)
            ):
                print(f"[{self.name}] Cached voice is stale; recloning {speaker_id} and retrying sentence synthesis")
                self.invalidate_cached_voice(speaker_id, lang, current_voice_id)
                voice = self.get_or_create_voice(speaker_id, lang)
                current_voice_id = voice.get("voiceId") if voice else None
                if current_voice_id:
                    self.on_voice_used(voice)
                    sentence_boundaries.clear()
                    return self.synthesize_stream_sentences(
                        tracking_gen(replay_items=list(cached_items)), current_voice_id, on_chunk,
                        speaker_id=speaker_id,
                        abort_check=abort_check,
                        on_voice_switch=on_voice_switch,
                    )
            return success

        # Fallback for providers without sentence streaming support.
        all_items = list(sentence_gen)
        texts = [s[0] if isinstance(s, tuple) else s for s in all_items if s]
        full = " ".join(texts)
        if not full.strip():
            return False
        return self.synthesize_stream(full, voice_id, on_chunk, speaker_id=speaker_id)

    def speak_streaming(self, sentence_gen, character_name: str,
                         full_text_holder: dict,
                         setup_event: threading.Event = None,
                         setup_data: dict = None,
                         lang: Optional[str] = None,
                         on_start: Optional[Callable] = None,
                       on_stop: Optional[Callable[[bool], None]] = None,
                         on_download_complete: Optional[Callable] = None,
                         lua_socket: Any = None,
                         turn_id: Optional[str] = None,
                         abort_check: Optional[Callable[[], bool]] = None,
                         history_entry: Optional[Dict] = None,
                         profiler: Any = None) -> Dict:
        """
        Stream sentences through TTS and play audio in real-time.

        Like speak(), but accepts a sentence generator instead of full text.
        Sentences are synthesized via WebSocket pipelining as they arrive
        from LLM streaming, so audio starts before LLM finishes.

        The caller sets setup_event after play_turn completes (providing
        positions). This method waits for setup_event before starting
        audio playback, but TTS synthesis runs in parallel.

        Args:
            sentence_gen: Generator yielding sentence strings (blocks on LLM)
            character_name: Character whose voice to use
            full_text_holder: Mutable dict; key 'text' set to full response
                              when generator exhausts. Key 'error' set on failure.
            setup_event: Event caller signals after play_turn/position setup.
                         If None, playback starts as soon as buffer is ready.
            setup_data: Mutable dict; caller populates with 'positions' and
                        'turn_id' before signaling setup_event.
            lang: Language code
            on_start: Callback when audio playback starts
            on_stop: Callback when audio playback ends. Receives True only when
                playback completed normally, False for aborted/interrupted/error paths.
            on_download_complete: Callback when TTS synthesis finishes
            lua_socket: Socket for real-time position updates
            turn_id: Default turn ID (may be overridden by setup_data)
            abort_check: Callable that returns True to abort
            profiler: Profiler instance

        Returns:
            {"success": bool, "word_timings": list, "error": str or None}
        """
        if abort_check and abort_check():
            return {"success": False, "word_timings": [], "error": "Aborted"}

        try:
            from audio import create_tts_stream, get_player
        except ImportError as e:
            return {"success": False, "word_timings": [], "error": f"audio3d not available: {e}"}

        try:
            from audio.playback import get_coordinator
            coordinator = get_coordinator()
        except ImportError:
            coordinator = None

        # Get or create voice
        voice = self.get_or_create_voice(character_name, lang, lua_socket)
        if not voice:
            return {"success": False, "word_timings": [], "error": f"No voice for {character_name}"}

        voice_id = voice.get("voiceId")
        if not voice_id:
            return {"success": False, "word_timings": [], "error": "Voice has no voiceId"}

        self.on_voice_used(voice)

        # Create TTS stream
        sample_rate = self.get_sample_rate()
        channels = 1
        bytes_per_second = sample_rate * 2 * channels
        tts_stream = create_tts_stream(sample_rate=sample_rate, channels=channels)
        word_timings = []
        total_bytes = [0]
        archive_pcm = bytearray()
        buffer_ready = threading.Event()
        tts_done = threading.Event()
        tts_error = [None]

        sentence_boundaries = []

        buffer_seconds = self.get_buffer_seconds()
        min_buffer_bytes = bytes_per_second * buffer_seconds

        # Create turn (may be replaced after setup_event)
        if not turn_id:
            turn_id = f"stream_{int(time.time() * 1000)}"
        turn = coordinator.create_turn(turn_id, speaker_id=character_name, use_3d=True) if coordinator else None

        if turn:
            turn._lang = lang
            turn._pcm_sample_rate = sample_rate
            turn.sentence_boundaries = sentence_boundaries

        chunk_count = [0]
        first_chunk_received = [False]
        _timed_words_total = [0]  # Cumulative word count from word_timing data

        def on_chunk(pcm_bytes, word_timing):
            chunk_count[0] += 1
            if pcm_bytes and not first_chunk_received[0]:
                first_chunk_received[0] = True
                if profiler:
                    profiler.mark("tts_first_chunk")
                print(f"[SpeakStream] First audio chunk received ({len(pcm_bytes)} bytes)")

            # Feed audio to playback stream
            if pcm_bytes:
                tts_stream.feed(pcm_bytes)
                archive_pcm.extend(pcm_bytes)
                total_bytes[0] += len(pcm_bytes)

            # Store raw data in turn for on-demand viseme generation
            if turn:
                if pcm_bytes:
                    turn.store_pcm(pcm_bytes, sample_rate, channels)
                if word_timing:
                    turn.store_word_alignment(word_timing)
                    word_timings.append(word_timing)
                    turn.add_word_timing(word_timing)
                # Keep original_text synced for interrupt trimming
                # (text accumulates in full_text_holder as LLM streams)
                current_text = full_text_holder.get('text', '')
                if current_text:
                    turn.original_text = current_text

            # Refine boundary times using absolute word timing data.
            self._update_sentence_boundaries_from_word_timing(
                sentence_boundaries, word_timing, _timed_words_total
            )

            # Check buffer threshold (audio only, visemes generated on-demand)
            if pcm_bytes and not buffer_ready.is_set():
                has_enough = total_bytes[0] >= min_buffer_bytes
                if has_enough:
                    buffer_secs = total_bytes[0] / bytes_per_second
                    print(f"[SpeakStream] Buffer ready: {buffer_secs:.1f}s audio")
                    buffer_ready.set()

        def run_streaming_tts():
            try:
                if profiler:
                    profiler.mark("tts_synthesis_start")

                print(f"[SpeakStream] Starting sentence streaming synthesis for {character_name}")
                original_sentences = setup_data.get('_original_sentences') if setup_data else None
                narrator_voice_id = setup_data.get('_narrator_voice_id') if setup_data else None
                success = self._synthesize_sentence_stream(
                    sentence_gen=sentence_gen,
                    voice_id=voice_id,
                    on_chunk=on_chunk,
                    speaker_id=character_name,
                    total_bytes=total_bytes,
                    bytes_per_second=bytes_per_second,
                    sentence_boundaries=sentence_boundaries,
                    original_sentences=original_sentences,
                    narrator_voice_id=narrator_voice_id,
                    lang=lang,
                    abort_check=abort_check,
                    log_prefix="[SpeakStream]",
                )
                print(f"[SpeakStream] Sentence streaming complete: success={success}, "
                      f"{total_bytes[0]} bytes, {chunk_count[0]} chunks")
                self._log_sentence_boundaries(
                    sentence_boundaries,
                    label=f"speak_streaming_complete:{character_name}",
                    bytes_per_second=bytes_per_second,
                )

                if not buffer_ready.is_set():
                    buffer_secs = total_bytes[0] / bytes_per_second
                    print(f"[SpeakStream] Short utterance ({buffer_secs:.1f}s) - starting playback")
                    buffer_ready.set()

                if on_download_complete:
                    if success:
                        print(f"[SpeakStream] Signaling download complete")
                    else:
                        print(f"[SpeakStream] Signaling download complete (aborted/failed)")
                    on_download_complete()
                if not success:
                    tts_error[0] = "TTS synthesis failed"
                    print(f"[SpeakStream] TTS synthesis failed")
            except Exception as e:
                tts_error[0] = str(e)
                print(f"[SpeakStream] Exception in TTS thread: {e}")
                import traceback
                traceback.print_exc()
            finally:
                runtime_history_entry = history_entry
                if runtime_history_entry is None and setup_data:
                    runtime_history_entry = setup_data.get('_history_entry')
                self._finalize_tts_archive(
                    history_entry=runtime_history_entry,
                    speaker_id=character_name,
                    text=full_text_holder.get('text', ''),
                    pcm_bytes=archive_pcm,
                    sample_rate=sample_rate,
                    channels=channels,
                )
                tts_stream.finish()
                tts_done.set()
                print(f"[SpeakStream] TTS thread finished")

        # Start TTS thread (runs in parallel with LLM streaming)
        tts_thread = threading.Thread(target=run_streaming_tts, daemon=True)
        tts_thread.start()

        def request_stream_abort(reason: str):
            print(f"[SpeakStream] Aborting sentence stream: {reason}")
            if setup_data is not None:
                setup_data['_abort'] = True
            if setup_event:
                setup_event.set()
            close_fn = getattr(sentence_gen, 'close', None)
            if close_fn:
                try:
                    close_fn()
                except ValueError:
                    # The generator is currently executing in the synthesis thread.
                    # The abort flag above lets it stop at the next yield boundary.
                    pass
                except Exception as e:
                    print(f"[SpeakStream] Error closing sentence stream: {e}")

        # Wait for buffer to fill
        # Use a long timeout to accommodate slow local LLMs (e.g. 30-60s to first sentence)
        print(f"[SpeakStream] Pre-buffering (waiting for {buffer_seconds}s of audio)...")
        if not buffer_ready.wait(timeout=120.0):
            request_stream_abort("buffer timeout")
            return {"success": False, "word_timings": [], "error": "Timeout waiting for TTS buffer"}

        # Signal external buffer_ready for early play_turn (streaming latency optimization)
        if setup_data and setup_data.get('_buffer_ready'):
            setup_data['_buffer_ready'].set()

        if abort_check and abort_check():
            request_stream_abort("abort check")
            return {"success": False, "word_timings": [], "error": "Aborted"}

        if tts_error[0]:
            request_stream_abort("tts error")
            return {"success": False, "word_timings": [], "error": tts_error[0]}

        # Wait for setup (play_turn + positions) from caller
        if setup_event:
            print("[SpeakStream] Waiting for play_turn setup...")
            if not setup_event.wait(timeout=15.0):
                request_stream_abort("setup timeout")
                return {"success": False, "word_timings": [], "error": "Timeout waiting for setup"}

            if setup_data and setup_data.get('_abort'):
                request_stream_abort("caller abort")
                return {"success": False, "word_timings": [], "error": "Aborted by caller"}

            if profiler:
                profiler.mark("setup_received")

            # Apply positions from setup_data
            if setup_data:
                positions = setup_data.get('positions')
                new_turn_id = setup_data.get('turn_id')
                if new_turn_id and turn and new_turn_id != turn_id:
                    print(f"[SpeakStream] Re-keying turn: {turn_id} -> {new_turn_id}")
                    # Re-key in coordinator's dict so play_turn can find it
                    if coordinator and turn_id in coordinator.turns:
                        del coordinator.turns[turn_id]
                        coordinator.turns[new_turn_id] = turn
                    turn.turn_id = new_turn_id
                    turn_id = new_turn_id
                print(f"[SpeakStream] Setup received: turn_id={turn_id}, has_positions={positions is not None}")

                # Pass subtitle mode to turn for sync loop
                if turn:
                    turn._sentence_subtitles = setup_data.get('_sentence_subtitles', True)

        if on_start:
            on_start()
        print("[SpeakStream] Playing...")

        # Play audio
        player = get_player()
        if lua_socket:
            player.position_reader.set_socket(lua_socket)

        # Set 3D positions (from setup_data if available, else no positions)
        positions = setup_data.get('positions') if setup_data else None
        if positions and positions.get("npcX") is not None:
            cam = (positions.get("camX", 0), positions.get("camY", 0), positions.get("camZ", 0))
            npc = (positions.get("npcX", 0), positions.get("npcY", 0), positions.get("npcZ", 0))
            yaw = positions.get("camYaw", 0)
            player.position_reader.set_initial_positions(cam, yaw, npc)

        if coordinator and turn:
            turn.audio_stream = tts_stream
            print(f"[SpeakStream] Starting with coordinator: {len(turn.viseme_buffer)} initial visemes")
            success = coordinator.play_turn(turn.turn_id, player, blocking=True, abort_check=abort_check)
        else:
            success = player.play_stream(tts_stream, use_3d=True, abort_check=abort_check)

        print(f"[SpeakStream] Playback finished: success={success}")
        if on_stop:
            on_stop(bool(success))

        print(f"[SpeakStream] Waiting for TTS thread to join...")
        tts_thread.join(timeout=60.0)
        print(f"[SpeakStream] TTS thread joined, total_bytes={total_bytes[0]}, chunks={chunk_count[0]}")

        if tts_error[0]:
            return {"success": False, "word_timings": word_timings, "error": tts_error[0]}

        return {"success": success, "word_timings": word_timings, "error": None}

    def prepare_tts(self, text: str, character_name: str,
                    lang: Optional[str] = None,
                    on_chunk: Optional[Callable] = None,
                    abort_check: Optional[Callable[[], bool]] = None,
                    on_ready: Optional[Callable] = None,
                    lua_socket: Any = None,
                    sentence_gen: Optional[Iterable] = None,
                    original_sentences: Optional[List] = None,
                    narrator_voice_id: Optional[str] = None,
                    history_entry: Optional[Dict] = None) -> Optional[Tuple]:
        """
        Download TTS audio into buffer without playing.
        Used for pre-buffering the next response while current audio plays.

        Args:
            text: Text to synthesize
            character_name: Voice to use
            lang: Language code (uses provider default if None)
            on_chunk: Optional callback for word timings (word_alignment dict)
            abort_check: Callable that returns True if we should abort
            on_ready: Callback when enough audio is buffered to start playing
                      (called with tts_stream, word_timings, visemes, sentence_boundaries)
            lua_socket: Socket server for sending notifications
            sentence_gen: Optional segmented sentence generator for narration/subtitles
            original_sentences: Clean text list matching sentence_gen ordering
            narrator_voice_id: Optional narrator voice id for narration segments

        Returns:
            (tts_stream, word_timings, visemes, sentence_boundaries) on success,
            None if failed/aborted
        """
        voice = self.get_or_create_voice(character_name, lang, lua_socket)
        if not voice:
            print(f"[PrepareTTS] No voice for {character_name}")
            return None

        voice_id = voice.get("voiceId")
        if not voice_id:
            print(f"[PrepareTTS] Voice has no voiceId")
            return None

        # Track usage for LRU (provider-specific hook)
        self.on_voice_used(voice)

        try:
            from audio import create_tts_stream
        except ImportError as e:
            print(f"[PrepareTTS] audio3d not available: {e}")
            return None

        sample_rate = self.get_sample_rate()
        channels = 1
        bytes_per_second = sample_rate * 2 * channels
        tts_stream = create_tts_stream(sample_rate=sample_rate, channels=channels)
        word_timings = []
        all_visemes = []
        sentence_boundaries = []
        raw_pcm = bytearray()  # Fallback source if per-chunk viseme extraction misses

        # Pre-buffer: wait for enough audio before signaling ready
        min_buffer_bytes = bytes_per_second * self.get_buffer_seconds()
        buffer_ready_signaled = [False]
        total_bytes = [0]
        timed_words_total = [0]

        def signal_ready():
            if not on_ready:
                return
            import inspect

            # Backward compatibility with existing 3-arg callbacks.
            # Avoid broad try/except TypeError, which can mask callback bugs.
            params = inspect.signature(on_ready).parameters.values()
            has_varargs = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in params)
            positional = [
                p for p in params
                if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
            ]
            if has_varargs or len(positional) >= 4:
                on_ready(tts_stream, word_timings, all_visemes, sentence_boundaries)
            else:
                on_ready(tts_stream, word_timings, all_visemes)

        def chunk_handler(pcm_bytes, word_alignment):
            # Check abort before feeding
            if abort_check and abort_check():
                return

            # Calculate base_time BEFORE adding this chunk's bytes
            chunk_start_bytes = total_bytes[0]
            base_time = chunk_start_bytes / bytes_per_second

            tts_stream.feed(pcm_bytes)
            if pcm_bytes:
                raw_pcm.extend(pcm_bytes)
            total_bytes[0] += len(pcm_bytes)

            if word_alignment:
                word_timings.append(word_alignment)
                if on_chunk:
                    on_chunk(word_alignment)

            self._update_sentence_boundaries_from_word_timing(
                sentence_boundaries, word_alignment, timed_words_total
            )

            # Process visemes
            if LIPSYNC_AVAILABLE:
                if word_alignment:
                    visemes = lipsync.process_word_alignment(
                        word_alignment=word_alignment,
                        lang=lang,
                        auto_send=False,
                        pcm_data=pcm_bytes,
                        text=text,
                        sample_rate=sample_rate,
                        base_time=base_time
                    )
                else:
                    visemes = lipsync.amplitude_visemes_for_audio(
                        pcm_bytes, sample_rate
                    )
                    for v in visemes:
                        v['t'] += base_time
                if visemes:
                    all_visemes.extend(visemes)
                    narr_result = neutralize_narration_visemes(
                        all_visemes,
                        sentence_boundaries,
                        audio_duration=total_bytes[0] / bytes_per_second if bytes_per_second > 0 else 0.0,
                        sample_rate=sample_rate,
                        channels=channels,
                    )
                    if narr_result["zeroed"] > 0 or narr_result["guards"] > 0:
                        print(f"[Narration] Prebuffer zeroed {narr_result['zeroed']} visemes, "
                              f"added {narr_result['guards']} guards "
                              f"(ranges={[(f'{s:.2f}', f'{e:.2f}') for s, e in narr_result['ranges']]})")

            # Signal ready early (after enough buffer AND visemes available)
            # We delay until visemes are ready to ensure lip sync works
            if on_ready and not buffer_ready_signaled[0]:
                has_enough_audio = total_bytes[0] >= min_buffer_bytes
                has_visemes = len(all_visemes) > 0

                if has_enough_audio and has_visemes:
                    buffer_ready_signaled[0] = True
                    buffer_secs = total_bytes[0] / bytes_per_second
                    viseme_count = len(all_visemes)
                    print(f"[PrepareTTS] Buffer ready: {total_bytes[0]} bytes ({buffer_secs:.1f}s), {viseme_count} visemes")
                    signal_ready()

        print(f"[PrepareTTS] Downloading TTS for {character_name}...")

        def synthesize_once(current_voice_id):
            # Use end marker trimmer for clean audio cutoffs
            if END_TRIMMER_AVAILABLE:
                padded_text = pad_text_with_end_marker(text)
                trimmer = EndMarkerTrimmer(
                    original_text=text,
                    on_chunk=chunk_handler,
                    sample_rate=sample_rate,
                    bytes_per_sample=2
                )
                ok = self.synthesize_stream(padded_text, current_voice_id, trimmer.process_chunk, speaker_id=character_name)
                trimmer.flush()
                return ok
            return self.synthesize_stream(text, current_voice_id, chunk_handler, speaker_id=character_name)

        if sentence_gen is not None:
            success = self._synthesize_sentence_stream(
                sentence_gen=sentence_gen,
                voice_id=voice_id,
                on_chunk=chunk_handler,
                speaker_id=character_name,
                total_bytes=total_bytes,
                bytes_per_second=bytes_per_second,
                sentence_boundaries=sentence_boundaries,
                original_sentences=original_sentences,
                narrator_voice_id=narrator_voice_id,
                lang=lang,
                abort_check=abort_check,
                log_prefix="[PrepareTTS]",
            )
        else:
            success = synthesize_once(voice_id)
            if (
                not success
                and not raw_pcm
                and self.should_reclone_after_synthesis_failure(voice_id)
            ):
                print(f"[{self.name}] Cached voice is stale; recloning {character_name} and retrying prebuffer")
                self.invalidate_cached_voice(character_name, lang, voice_id)
                voice = self.get_or_create_voice(character_name, lang, lua_socket)
                voice_id = voice.get("voiceId") if voice else None
                if voice_id:
                    self.on_voice_used(voice)
                    success = synthesize_once(voice_id)
                else:
                    success = False

        # Check abort after download
        if abort_check and abort_check():
            print(f"[PrepareTTS] Aborted")
            tts_stream.clean_up()
            return None

        # Defensive fallback: if chunk-level viseme extraction produced nothing
        # (observed on some Pocket prebuffer paths), generate amplitude visemes
        # from the fully buffered PCM so prebuffered turns still animate.
        if not all_visemes and raw_pcm:
            lipsync_mod = lipsync if LIPSYNC_AVAILABLE else None
            if lipsync_mod is None:
                try:
                    from audio import lipsync as lipsync_mod
                except ImportError:
                    try:
                        from sonorus.audio import lipsync as lipsync_mod
                    except ImportError:
                        lipsync_mod = None
            if lipsync_mod is not None:
                try:
                    fallback_visemes = lipsync_mod.amplitude_visemes_for_audio(
                        bytes(raw_pcm), sample_rate
                    )
                    if fallback_visemes:
                        all_visemes.extend(fallback_visemes)
                        narr_result = neutralize_narration_visemes(
                            all_visemes,
                            sentence_boundaries,
                            audio_duration=total_bytes[0] / bytes_per_second if bytes_per_second > 0 else 0.0,
                            sample_rate=sample_rate,
                            channels=channels,
                        )
                        if narr_result["zeroed"] > 0 or narr_result["guards"] > 0:
                            print(f"[Narration] Prebuffer zeroed {narr_result['zeroed']} visemes, "
                                  f"added {narr_result['guards']} guards "
                                  f"(ranges={[(f'{s:.2f}', f'{e:.2f}') for s, e in narr_result['ranges']]})")
                        print(f"[PrepareTTS] Fallback amplitude visemes: {len(fallback_visemes)}")
                except Exception as e:
                    print(f"[PrepareTTS] Fallback viseme generation error: {e}")

        # If synthesis complete and never signaled ready (short utterance or late visemes), signal now
        if on_ready and not buffer_ready_signaled[0]:
            buffer_secs = total_bytes[0] / bytes_per_second
            viseme_count = len(all_visemes)
            print(f"[PrepareTTS] Synthesis complete ({buffer_secs:.1f}s, {viseme_count} visemes) - signaling ready")
            signal_ready()

        tts_stream.finish()

        self._finalize_tts_archive(
            history_entry=history_entry,
            speaker_id=character_name,
            text=text,
            pcm_bytes=raw_pcm,
            sample_rate=sample_rate,
            channels=channels,
        )

        if not success:
            print(f"[PrepareTTS] Synthesis failed")
            tts_stream.clean_up()
            return None

        print(f"[PrepareTTS] Complete: {tts_stream._total_fed} bytes, {len(word_timings)} timing chunks, "
              f"{len(all_visemes)} visemes, {len(sentence_boundaries)} boundaries")
        self._log_sentence_boundaries(
            sentence_boundaries,
            label=f"prepare_tts_complete:{character_name}",
            bytes_per_second=bytes_per_second,
        )
        return (tts_stream, word_timings, all_visemes, sentence_boundaries)

    # ----------------------------------------
    # Convenience Wrappers
    # ----------------------------------------

    def get_voice(self, name: str, lang: Optional[str] = None) -> Optional[Dict]:
        """Get a voice by name (convenience wrapper)."""
        return self.get_voice_cache().get(name, lang)

    def list_voices(self, lang: Optional[str] = None) -> List[Dict]:
        """List all voices (convenience wrapper)."""
        return self.get_voice_cache().list(lang)

    def init(self) -> bool:
        """Initialize the provider (loads voice cache)."""
        return self.get_voice_cache().load()

    def add_to_cache(self, voice: Dict, lang: Optional[str] = None):
        """Add a voice to the cache (used after cloning)."""
        self.get_voice_cache().add(voice, lang)
