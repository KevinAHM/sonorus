"""
Base TTS Provider

Abstract base class and shared implementations for TTS providers.
Eliminates code duplication between Inworld and ElevenLabs providers.
"""
import os
import sys
import time
import threading
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Callable, Tuple, Any

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from constants import TTS_BUFFER_SECONDS
from .voice_utils import find_voice_reference

# Lip sync module (optional)
try:
    from audio import lipsync
    LIPSYNC_AVAILABLE = True
except ImportError:
    LIPSYNC_AVAILABLE = False
    print("[WARN] audio.lipsync module not available")


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
                          on_chunk: Callable[[bytes, Optional[Dict]], None]) -> bool:
        """
        Stream TTS synthesis.

        Args:
            text: Text to synthesize
            voice_id: Provider-specific voice ID
            on_chunk: Callback function(pcm_bytes, word_alignment)
                - pcm_bytes: Raw PCM audio data
                - word_alignment: Dict with word timing info or None

        Returns:
            True on success, False on error
        """
        pass

    # ----------------------------------------
    # Optional Hooks (CAN override)
    # ----------------------------------------

    def on_voice_used(self, voice: Dict) -> None:
        """Called when a cloned voice is used. Override for usage tracking."""
        pass

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

        Args:
            character_name: Character name (e.g., "SebastianSallow")
            lang: Language code. Uses provider default if None.
            lua_socket: Socket server for sending notifications

        Returns:
            Voice dict with voiceId

        Raises:
            Exception: With specific reason if voice cannot be obtained
        """
        if lang is None:
            lang = self.get_default_language()

        # Check cache first
        cache = self.get_voice_cache()
        if not cache._loaded:
            # load() raises specific exceptions on failure
            cache.load()

        voice = cache.get(character_name, lang)
        if voice:
            print(f"[{self.name}] Voice found: {character_name}")
            return voice

        # Not in cache - try to clone
        print(f"[{self.name}] Voice not found in {self.name}, attempting to clone: {character_name}")

        ref_path = find_voice_reference(character_name, "15s")
        if not ref_path:
            print(f"[{self.name}] No reference file for: {character_name}")
            raise Exception(f"Voice reference file not found for '{character_name}'. Ensure voice references are extracted.")

        print(f"[{self.name}] Using reference: {os.path.basename(ref_path)}")

        # Notify player that we're cloning
        if lua_socket:
            lua_socket.send_notification("Cloning voice, please wait...")

        cloned = self.clone_voice(character_name, ref_path, lang)
        if not cloned:
            raise Exception(f"Voice cloning failed for '{character_name}'. Check the server logs for details.")
        return cloned

    def speak(self, text: str, character_name: str,
              lang: Optional[str] = None,
              on_start: Optional[Callable] = None,
              on_stop: Optional[Callable] = None,
              on_download_complete: Optional[Callable] = None,
              lua_socket: Any = None,
              initial_positions: Optional[Dict] = None,
              turn_id: Optional[str] = None,
              abort_check: Optional[Callable[[], bool]] = None) -> Dict:
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
            on_stop: Callback when audio playback ends
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
            from audio.spatial import create_tts_stream, get_player
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
        buffer_ready = threading.Event()
        tts_done = threading.Event()
        tts_error = [None]

        # Pre-buffer: wait for enough audio before starting playback
        min_buffer_bytes = bytes_per_second * TTS_BUFFER_SECONDS

        # Create turn for coordinator (accumulates visemes)
        # use_3d=False for player voice (when initial_positions is None)
        if not turn_id:
            turn_id = f"speak_{int(time.time() * 1000)}"
        use_3d = initial_positions is not None
        turn = coordinator.create_turn(turn_id, speaker_id=character_name, use_3d=use_3d) if coordinator else None

        def on_chunk(pcm_bytes, word_timing):
            # Calculate base_time BEFORE adding this chunk's bytes
            chunk_start_bytes = total_bytes[0]
            base_time = chunk_start_bytes / bytes_per_second

            tts_stream.feed(pcm_bytes)
            total_bytes[0] += len(pcm_bytes)

            # Process word timing into visemes FIRST (before signaling buffer ready)
            if LIPSYNC_AVAILABLE:
                visemes = lipsync.process_word_alignment(
                    word_alignment=word_timing,
                    lang=lang,
                    auto_send=False,
                    pcm_data=pcm_bytes,
                    text=text,
                    sample_rate=sample_rate,
                    base_time=base_time
                )
                if turn and visemes:
                    turn.add_visemes(visemes)

            if word_timing:
                word_timings.append(word_timing)

            # THEN check if we have enough buffered to start playback
            if not buffer_ready.is_set():
                if total_bytes[0] >= min_buffer_bytes:
                    buffer_secs = total_bytes[0] / bytes_per_second
                    viseme_count = len(turn.viseme_buffer) if turn else 0
                    print(f"[Speak] Buffer ready: {total_bytes[0]} bytes ({buffer_secs:.1f}s), {viseme_count} visemes")
                    buffer_ready.set()

        def run_tts():
            try:
                success = self.synthesize_stream(text, voice_id, on_chunk)
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
                tts_stream.finish()
                tts_done.set()

        # Start TTS thread
        tts_thread = threading.Thread(target=run_tts, daemon=True)
        tts_thread.start()

        # Wait for buffer to fill (or TTS to complete for short utterances)
        print("[Speak] Pre-buffering (waiting for 2+ chunks or 2s of audio)...")
        if not buffer_ready.wait(timeout=15.0):
            return {"success": False, "word_timings": [], "error": "Timeout waiting for TTS buffer"}

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
            success = coordinator.play_turn(turn_id, player, blocking=True)
        else:
            # Fallback: direct playback (no sync)
            success = player.play_stream(tts_stream, use_3d=use_3d)

        # Signal playback ended
        if on_stop:
            on_stop()

        tts_thread.join(timeout=60.0)

        if tts_error[0]:
            return {"success": False, "word_timings": word_timings, "error": tts_error[0]}

        return {"success": success, "word_timings": word_timings, "error": None}

    def prepare_tts(self, text: str, character_name: str,
                    lang: Optional[str] = None,
                    on_chunk: Optional[Callable] = None,
                    abort_check: Optional[Callable[[], bool]] = None,
                    on_ready: Optional[Callable] = None,
                    lua_socket: Any = None) -> Optional[Tuple]:
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
                      (called with tts_stream, word_timings, visemes)
            lua_socket: Socket server for sending notifications

        Returns:
            (tts_stream, word_timings, visemes) tuple on success, None if failed/aborted
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
            from audio.spatial import create_tts_stream
        except ImportError as e:
            print(f"[PrepareTTS] audio3d not available: {e}")
            return None

        sample_rate = self.get_sample_rate()
        channels = 1
        bytes_per_second = sample_rate * 2 * channels
        tts_stream = create_tts_stream(sample_rate=sample_rate, channels=channels)
        word_timings = []
        all_visemes = []

        # Pre-buffer: wait for enough audio before signaling ready
        min_buffer_bytes = bytes_per_second * TTS_BUFFER_SECONDS
        buffer_ready_signaled = [False]
        total_bytes = [0]

        def chunk_handler(pcm_bytes, word_alignment):
            # Check abort before feeding
            if abort_check and abort_check():
                return

            # Calculate base_time BEFORE adding this chunk's bytes
            chunk_start_bytes = total_bytes[0]
            base_time = chunk_start_bytes / bytes_per_second

            tts_stream.feed(pcm_bytes)
            total_bytes[0] += len(pcm_bytes)

            if word_alignment:
                word_timings.append(word_alignment)
                if on_chunk:
                    on_chunk(word_alignment)

            # Process visemes with gap filling
            if LIPSYNC_AVAILABLE:
                visemes = lipsync.process_word_alignment(
                    word_alignment=word_alignment,
                    lang=lang,
                    auto_send=False,
                    pcm_data=pcm_bytes,
                    text=text,
                    sample_rate=sample_rate,
                    base_time=base_time
                )
                if visemes:
                    all_visemes.extend(visemes)

            # Signal ready early (after enough buffer, not at end!)
            if on_ready and not buffer_ready_signaled[0]:
                if total_bytes[0] >= min_buffer_bytes:
                    buffer_ready_signaled[0] = True
                    buffer_secs = total_bytes[0] / bytes_per_second
                    viseme_count = len(all_visemes)
                    print(f"[PrepareTTS] Buffer ready: {total_bytes[0]} bytes ({buffer_secs:.1f}s), {viseme_count} visemes")
                    on_ready(tts_stream, word_timings, all_visemes)

        print(f"[PrepareTTS] Downloading TTS for {character_name}...")
        success = self.synthesize_stream(text, voice_id, chunk_handler)

        # Check abort after download
        if abort_check and abort_check():
            print(f"[PrepareTTS] Aborted")
            tts_stream.clean_up()
            return None

        # If short utterance that didn't hit threshold, signal ready at end
        if on_ready and not buffer_ready_signaled[0]:
            buffer_secs = total_bytes[0] / bytes_per_second
            viseme_count = len(all_visemes)
            print(f"[PrepareTTS] Short utterance ({buffer_secs:.1f}s), {viseme_count} visemes - signaling ready")
            on_ready(tts_stream, word_timings, all_visemes)

        tts_stream.finish()

        if not success:
            print(f"[PrepareTTS] Synthesis failed")
            tts_stream.clean_up()
            return None

        print(f"[PrepareTTS] Complete: {tts_stream._total_fed} bytes, {len(word_timings)} timing chunks, {len(all_visemes)} visemes")
        return (tts_stream, word_timings, all_visemes)

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
