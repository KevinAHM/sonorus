"""
Playback Coordinator

Synchronizes TTS audio playback with lipsync visemes.
Handles:
- Per-turn viseme accumulation during pre-buffering
- Handshake with Lua before audio starts (lipsync_start → lipsync_ready)
- Continuous viseme streaming during playback
- Audio position sync for drift correction
"""
import os
import sys
import time
import threading
from typing import List, Dict, Optional, Callable

# Add parent to path for utils imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.settings import load_settings
from utils.profiler import Profiler

# Get shared profiler instance for timing
_profiler = Profiler.get("chat_flow")


def remove_unpaired_double_quotes(text: str) -> str:
    """Remove one dangling `\"` when quote count is odd."""
    if not text or '"' not in text:
        return text

    quote_indices = [i for i, ch in enumerate(text) if ch == '"']
    if len(quote_indices) % 2 == 0:
        return text

    stripped_right = text.rstrip()
    if stripped_right.endswith('"'):
        remove_idx = text.rfind('"')
    else:
        stripped_left = text.lstrip()
        if stripped_left.startswith('"'):
            remove_idx = len(text) - len(stripped_left)
        else:
            remove_idx = quote_indices[-1]

    return text[:remove_idx] + text[remove_idx + 1:]


class TurnState:
    """State for a single conversation turn's playback."""

    def __init__(self, turn_id: str, speaker_id: str = None, use_3d: bool = True,
                 reverb_auxbus: str = None, reverb_send: float = 1.0):
        self.turn_id = turn_id
        self.speaker_id = speaker_id         # Character ID for Lua
        self.use_3d = use_3d                 # False for player voice (centered stereo)
        self.reverb_auxbus = reverb_auxbus   # Reverb preset name from game
        self.reverb_send = reverb_send       # Reverb wet/dry mix (0.0-1.0)
        self.viseme_buffer: List[Dict] = []  # Accumulated visemes
        self.viseme_source: Optional[List[Dict]] = None  # External source for streaming (pre-buffered path)
        self.visemes_sent_idx: int = 0       # How many sent to Lua
        self.audio_stream = None             # TTSStream reference
        self.playback_started: bool = False
        self.playback_start_time: float = 0  # Wall clock time when audio started
        self.audio_position: float = 0.0     # Current playback position (seconds)
        self.created_at: float = time.time()
        # For interrupt trimming
        self.original_text: str = ""         # Full text being spoken
        self.word_timings: List[Dict] = []   # Word-level timestamps from TTS
        self.word_timings_source: Optional[List[Dict]] = None  # External source for streaming updates
        # On-demand viseme generation: store raw data, generate lazily
        self._raw_pcm = bytearray()          # All PCM concatenated
        self._pcm_sample_rate: int = 48000   # Set by store_pcm
        self._pcm_channels: int = 1          # Mono by default
        self._word_alignments: List[Dict] = []  # Accumulated word alignments (absolute timestamps)
        self._vis_gen_time: float = 0.0      # Visemes generated up to this time (seconds)
        self._vis_gen_lock = threading.Lock() # Protects lazy generation
        self._word_gen_idx: int = 0          # Next unprocessed word alignment index
        self._lang: Optional[str] = None     # Language for phoneme conversion
        # Per-sentence subtitle tracking
        self.sentence_boundaries: List[Dict] = []
        self._last_subtitle_idx: int = -1
        self._sentence_subtitles: bool = True  # Per-sentence updates vs full-text-at-once
        # Narration: high-water mark for viseme zeroing (avoids re-scanning)
        self._narr_zero_idx: int = 0

    def add_visemes(self, visemes: List[Dict]):
        """Add visemes to buffer (called as TTS chunks arrive)."""
        self.viseme_buffer.extend(visemes)

    def get_unsent_visemes(self) -> List[Dict]:
        """Get visemes that haven't been sent to Lua yet."""
        # First, pull any new visemes from source list (if connected)
        # This handles streaming visemes for pre-buffered playback
        if self.viseme_source is not None:
            source_len = len(self.viseme_source)
            buffer_len = len(self.viseme_buffer)
            if source_len > buffer_len:
                # New visemes arrived in source - copy them to buffer
                new_from_source = self.viseme_source[buffer_len:]
                self.viseme_buffer.extend(new_from_source)

        # Now return unsent visemes as before
        unsent = self.viseme_buffer[self.visemes_sent_idx:]
        self.visemes_sent_idx = len(self.viseme_buffer)
        return unsent

    def get_all_visemes(self) -> List[Dict]:
        """Get all visemes (for initial send with lipsync_start)."""
        # Pull any new visemes from source first
        if self.viseme_source is not None:
            source_len = len(self.viseme_source)
            buffer_len = len(self.viseme_buffer)
            if source_len > buffer_len:
                new_from_source = self.viseme_source[buffer_len:]
                self.viseme_buffer.extend(new_from_source)

        self.visemes_sent_idx = len(self.viseme_buffer)
        return list(self.viseme_buffer)

    def set_viseme_source(self, source_list: List[Dict]):
        """Connect to external viseme source for streaming updates.

        Used for pre-buffered playback where visemes may still be arriving
        from the buffer_thread while playback has started.
        """
        self.viseme_source = source_list
        # Copy current contents to buffer
        self.viseme_buffer = list(source_list)

    def add_word_timing(self, word_timing: Dict):
        """Add word timing chunk from TTS."""
        if word_timing:
            self.word_timings.append(word_timing)

    def set_word_timings_source(self, source_list: List[Dict]):
        """Connect to external word timings source for streaming updates.

        Used for pre-buffered playback where word timings may still be arriving
        from the buffer_thread while playback has started.
        """
        self.word_timings_source = source_list
        # Copy current contents
        self.word_timings = list(source_list)

    def _sync_word_timings_from_source(self):
        """Pull any new word timings from source list."""
        if self.word_timings_source is not None:
            source_len = len(self.word_timings_source)
            current_len = len(self.word_timings)
            if source_len > current_len:
                # New timings arrived - copy them
                new_from_source = self.word_timings_source[current_len:]
                self.word_timings.extend(new_from_source)

    def get_text_at_time(self, elapsed_seconds: float) -> str:
        """
        Get the text that was spoken up to elapsed_seconds.
        Returns text with em-dash if interrupted mid-sentence.

        Args:
            elapsed_seconds: Time since playback started

        Returns:
            Trimmed text with em-dash, or full text if timing unavailable
        """
        # Sync any new word timings from source (for pre-buffered playback)
        self._sync_word_timings_from_source()

        # Try word-level timings first (ElevenLabs, Inworld provide these)
        if self.word_timings and self.original_text:
            all_words = []
            all_end_times = []
            for timing in self.word_timings:
                words = timing.get("words", [])
                end_times = timing.get("wordEndTimeSeconds", [])
                all_words.extend(words)
                all_end_times.extend(end_times)

            if all_words:
                last_word_idx = -1
                for i, end_time in enumerate(all_end_times):
                    if end_time <= elapsed_seconds:
                        last_word_idx = i
                    else:
                        break

                if last_word_idx < 0:
                    return ""

                num_words_spoken = last_word_idx + 1
                original_words = self.original_text.split()

                if num_words_spoken >= len(original_words):
                    return self.original_text

                trimmed = " ".join(original_words[:num_words_spoken])
                trimmed = trimmed.rstrip(".,!?;:") + "—"
                return trimmed

        # Fallback: estimate from sentence boundary timestamps (PocketONNX, etc.)
        # sentence_boundaries = [{'text': 'First sentence.', 'start_time': 0.0}, ...]
        if self.sentence_boundaries:
            return self._estimate_text_from_sentences(elapsed_seconds)

        # No timing data at all
        return self.original_text

    def _estimate_text_from_sentences(self, elapsed_seconds: float) -> str:
        """Estimate spoken text using sentence boundary timestamps and word-rate estimation."""
        boundaries = self.sentence_boundaries

        # Find which sentence was playing at elapsed_seconds
        current_idx = 0
        for i, b in enumerate(boundaries):
            if b['start_time'] <= elapsed_seconds:
                current_idx = i
            else:
                break

        # Collect fully completed sentences (before current)
        completed = [boundaries[i]['text'] for i in range(current_idx)]

        # Estimate fraction of current sentence spoken
        current = boundaries[current_idx]
        sent_start = current['start_time']

        if current_idx + 1 < len(boundaries):
            sent_end = boundaries[current_idx + 1]['start_time']
        else:
            # Last sentence - estimate duration from word count (~3 words/sec)
            word_count = len(current['text'].split())
            sent_end = sent_start + max(word_count / 3.0, 0.5)

        sent_duration = sent_end - sent_start
        time_in = elapsed_seconds - sent_start
        fraction = min(time_in / sent_duration, 1.0) if sent_duration > 0 else 1.0

        current_words = current['text'].split()
        words_spoken = max(1, round(len(current_words) * fraction))

        if words_spoken >= len(current_words):
            # Full current sentence spoken
            completed.append(current['text'])
            result = " ".join(completed)
            if current_idx + 1 < len(boundaries):
                # More sentences exist that weren't spoken
                result = result.rstrip(".,!?;:") + "—"
            return result
        else:
            # Partial current sentence
            partial = " ".join(current_words[:words_spoken])
            completed.append(partial)
            result = " ".join(completed)
            result = result.rstrip(".,!?;:") + "—"
            return result

    def _get_boundary_time(self, boundary: Dict, idx: int) -> Optional[float]:
        """Return a reliable boundary start time, or None if not confirmed yet."""
        channels = max(1, int(getattr(self, '_pcm_channels', 1) or 1))
        bps = self._pcm_sample_rate * 2 * channels
        sb = boundary.get('start_bytes')
        if sb is not None and sb >= 0 and bps > 0:
            return sb / bps

        st = boundary.get('start_time', 0.0)
        if idx == 0:
            return float(st)

        if boundary.get('start_time_confirmed'):
            return float(st)

        return None

    # --- On-demand viseme generation ---

    def store_pcm(self, pcm_bytes: bytes, sample_rate: int = 48000, channels: int = 1):
        """Store raw PCM audio for lazy viseme generation."""
        if pcm_bytes:
            with self._vis_gen_lock:
                self._raw_pcm.extend(pcm_bytes)
                self._pcm_sample_rate = sample_rate
                self._pcm_channels = max(1, int(channels or 1))

    def store_word_alignment(self, word_alignment: Dict):
        """Store word alignment data (sync or async) for lazy viseme generation."""
        if word_alignment and word_alignment.get("words"):
            with self._vis_gen_lock:
                self._word_alignments.append(word_alignment)

    def get_audio_duration(self) -> float:
        """Total audio duration in seconds from stored PCM."""
        if not self._raw_pcm or self._pcm_sample_rate == 0:
            return 0.0
        channels = max(1, int(getattr(self, '_pcm_channels', 1) or 1))
        return len(self._raw_pcm) / (self._pcm_sample_rate * 2 * channels)

    def _build_narration_ranges(self, audio_dur: float) -> List[tuple]:
        """Build narration time ranges from sentence_boundaries.

        Each boundary with is_narration=True spans from its start_time
        to the next boundary's start_time (or audio_dur for the last).

        For async providers (Inworld WS), start_time begins at 0 until word
        timing corrects it. start_bytes is set by on_voice_switch callbacks
        at voice boundaries (dialogue↔narration). These are reliable because
        the provider waits for all audio to flush before switching voices.
        """
        if not self.sentence_boundaries:
            return []

        ranges = []
        for i, b in enumerate(self.sentence_boundaries):
            if not b.get('is_narration'):
                continue
            start = self._get_boundary_time(b, i)
            if start is None:
                continue  # No timing info available yet
            # End is next boundary's start, or audio duration
            if i + 1 < len(self.sentence_boundaries):
                end = self._get_boundary_time(self.sentence_boundaries[i + 1], i + 1)
                if end is None:
                    end = audio_dur
            else:
                end = audio_dur
            if end > start:
                ranges.append((start, end))
        return ranges

    def generate_pending_visemes(self):
        """
        Generate visemes for any audio not yet processed.

        Two passes:
        1. Amplitude pass: generates amplitude visemes (marked with _amplitude)
           for new PCM. Keeps lip movement flowing immediately.
        2. Word pass: when async word timestamps arrive, generates proper
           word visemes and REMOVES amplitude frames in that time range
           from the buffer to prevent oscillation.
        """
        with self._vis_gen_lock:
            try:
                from audio import lipsync
            except ImportError:
                return

            channels = max(1, int(getattr(self, '_pcm_channels', 1) or 1))
            bps = self._pcm_sample_rate * 2 * channels  # bytes per second
            audio_dur = self.get_audio_duration()

            # Pass 1: Amplitude for any new PCM beyond _vis_gen_time
            if audio_dur > self._vis_gen_time:
                gen_from = self._vis_gen_time
                start_byte = int(gen_from * bps) & ~1  # 2-byte align
                end_byte = int(audio_dur * bps) & ~1
                pcm_slice = bytes(self._raw_pcm[start_byte:end_byte])

                if pcm_slice:
                    amp_visemes = lipsync.amplitude_visemes_for_audio(
                        pcm_slice, self._pcm_sample_rate
                    )
                    for v in amp_visemes:
                        v['t'] += gen_from
                    # _amplitude marker preserved from amplitude_visemes_for_audio
                    self.viseme_buffer.extend(amp_visemes)

                self._vis_gen_time = audio_dur

            # Pass 2: Word visemes for any unprocessed word alignments
            while self._word_gen_idx < len(self._word_alignments):
                wa = self._word_alignments[self._word_gen_idx]
                self._word_gen_idx += 1

                wa_words = wa.get("words", [])
                wa_starts = wa.get("wordStartTimeSeconds", [])
                wa_ends = wa.get("wordEndTimeSeconds", [])
                if not wa_words:
                    continue

                # Time range this word alignment covers
                first_start = wa_starts[0] if wa_starts else 0
                last_end = wa_ends[-1] if wa_ends else audio_dur

                # Remove unsent amplitude frames in this time range
                sent_idx = self.visemes_sent_idx
                keep = self.viseme_buffer[:sent_idx]  # Already sent — keep
                for v in self.viseme_buffer[sent_idx:]:
                    if v.get('_amplitude') and first_start <= v.get('t', 0) <= last_end:
                        continue  # Drop amplitude frame in word range
                    keep.append(v)
                self.viseme_buffer = keep
                self._narr_zero_idx = 0  # Reset - buffer was rebuilt

                # Get PCM covering this word alignment's time range
                pcm_start = int(first_start * bps) & ~1  # 2-byte align
                pcm_end = int(min(last_end, audio_dur) * bps) & ~1
                pcm_slice = bytes(self._raw_pcm[pcm_start:pcm_end])

                word_visemes = lipsync.process_word_alignment(
                    word_alignment=wa,
                    lang=self._lang,
                    auto_send=False,
                    pcm_data=pcm_slice,
                    text=self.original_text,
                    sample_rate=self._pcm_sample_rate,
                    base_time=first_start
                )
                if word_visemes:
                    self.viseme_buffer.extend(word_visemes)

            # Zero visemes during narration ranges (NPC holds neutral face)
            # Only process visemes added since last zeroing pass
            narr_ranges = self._build_narration_ranges(audio_dur)
            if narr_ranges:
                zeroed_count = 0
                scan_start = self._narr_zero_idx
                for v in self.viseme_buffer[scan_start:]:
                    vt = v.get('t', 0)
                    for nr_start, nr_end in narr_ranges:
                        if nr_start <= vt <= nr_end:
                            for key in list(v.keys()):
                                if key not in ('t', '_amplitude'):
                                    v[key] = 0.0
                            zeroed_count += 1
                            break
                self._narr_zero_idx = len(self.viseme_buffer)
                if zeroed_count > 0:
                    total_scanned = len(self.viseme_buffer) - scan_start
                    print(f"[Narration] Zeroed {zeroed_count}/{total_scanned} visemes "
                          f"(ranges={[(f'{s:.2f}', f'{e:.2f}') for s, e in narr_ranges]})")

            # Sort buffer by time after both passes.
            # Pass 1 (amplitude) and Pass 2 (word) append in generation order,
            # not time order — surviving amplitude frames for gaps are followed
            # by word frames covering earlier times. Lua's GetVisemeAtTime does
            # a linear scan assuming sorted order, so unsorted frames cause word
            # visemes to be unreachable behind amplitude gap pairs.
            self.viseme_buffer.sort(key=lambda v: v.get('t', 0))

class PlaybackCoordinator:
    """
    Coordinates TTS audio playback with lipsync visemes.

    Usage:
        coordinator = PlaybackCoordinator(lua_socket)

        # Pre-buffer phase (in TTS callback):
        turn = coordinator.create_turn(turn_id)
        turn.add_visemes(visemes_from_chunk)
        turn.audio_stream = tts_stream

        # Playback phase:
        coordinator.play_turn(turn_id)
    """

    def __init__(self, lua_socket):
        self.lua_socket = lua_socket
        self.turns: Dict[str, TurnState] = {}
        self.current_turn_id: Optional[str] = None

        # Handshake synchronization
        self._lipsync_ready_event = threading.Event()
        self._lipsync_ready_turn_id: Optional[str] = None
        self._lipsync_ready_lock = threading.Lock()

        # Sync loop control
        self._sync_thread: Optional[threading.Thread] = None
        self._stop_sync = threading.Event()

        # Audio position callback (set by audio player)
        self._get_audio_position: Optional[Callable[[], float]] = None

        # Pause state for soft interrupt
        self._paused_at: Optional[float] = None

    def stop_current(self):
        """Stop current turn's sync loop and clear state.

        Call this when aborting a conversation to prevent the old turn's
        sync loop from sending visemes that interfere with new playback.
        """
        if self._sync_thread and self._sync_thread.is_alive():
            print("[Coordinator] Stopping sync loop...")
            self._stop_sync.set()
            self._sync_thread.join(timeout=0.5)
            if self._sync_thread.is_alive():
                print("[Coordinator] Warning: Sync thread didn't stop in time")

        # Clear current turn
        if self.current_turn_id:
            print(f"[Coordinator] Clearing current turn: {self.current_turn_id}")
            self.current_turn_id = None

        self._paused_at = None

    def pause(self):
        """Freeze audio position reporting (for soft interrupt)."""
        turn = self.turns.get(self.current_turn_id) if self.current_turn_id else None
        if turn and turn.playback_started:
            pos = self._get_audio_position_safe(turn)
            if pos is not None:
                self._paused_at = pos
                self.lua_socket.send({
                    "type": "lipsync_pause",
                    "turn_id": turn.turn_id,
                })
                print(f"[Coordinator] Paused at {pos:.2f}s")

    def resume(self, pause_duration: float):
        """Unfreeze position, adjust timing for pause duration."""
        if self._paused_at is None:
            return
        turn = self.turns.get(self.current_turn_id) if self.current_turn_id else None
        if turn and turn.playback_started:
            turn.playback_start_time += pause_duration
            self.lua_socket.send({
                "type": "lipsync_resume",
                "turn_id": turn.turn_id,
                "pause_duration": pause_duration,
            })
            print(f"[Coordinator] Resumed (shifted start by {pause_duration:.1f}s)")
        self._paused_at = None

    def get_interrupted_text(self) -> Optional[str]:
        """
        Get the text that was spoken before interruption.

        Returns:
            Trimmed text with em-dash, or None if no active turn
        """
        if not self.current_turn_id:
            return None

        turn = self.turns.get(self.current_turn_id)
        if not turn or not turn.playback_started:
            return None

        # Use paused position if available (soft interrupt pauses audio on VAD,
        # but stop_conversation runs later after STT transcription — wall clock
        # elapsed would include the pause duration, overcounting spoken text)
        if self._paused_at is not None:
            elapsed = self._paused_at
            print(f"[Coordinator] Using paused position: {elapsed:.2f}s")
        else:
            elapsed = time.time() - turn.playback_start_time

        trimmed = turn.get_text_at_time(elapsed)

        if trimmed:
            print(f"[Coordinator] Interrupted at {elapsed:.2f}s: '{trimmed[:50]}...'")
        return trimmed

    def create_turn(self, turn_id: str, speaker_id: str = None, use_3d: bool = True,
                    reverb_auxbus: str = None, reverb_send: float = 1.0) -> TurnState:
        """Create a new turn for pre-buffering."""
        turn = TurnState(turn_id, speaker_id, use_3d=use_3d,
                         reverb_auxbus=reverb_auxbus, reverb_send=reverb_send)
        self.turns[turn_id] = turn
        # Cleanup old turns (keep last 5)
        if len(self.turns) > 5:
            oldest = sorted(self.turns.values(), key=lambda t: t.created_at)[0]
            del self.turns[oldest.turn_id]
        return turn

    def get_turn(self, turn_id: str) -> Optional[TurnState]:
        """Get existing turn state."""
        return self.turns.get(turn_id)

    def on_lipsync_ready(self, turn_id: str):
        """Called by socket handler when Lua acknowledges lipsync_start."""
        with self._lipsync_ready_lock:
            self._lipsync_ready_turn_id = turn_id
        self._lipsync_ready_event.set()
        print(f"[Coordinator] Received lipsync_ready for turn {turn_id}")

    def set_audio_position_callback(self, callback: Callable[[], float]):
        """Set callback to get current audio playback position."""
        self._get_audio_position = callback

    def play_turn(self, turn_id: str, audio_player, blocking: bool = True,
                  abort_check: Callable[[], bool] = None) -> bool:
        """
        Play a turn with synchronized lipsync.

        Args:
            turn_id: Turn to play
            audio_player: Audio3DPlayer instance
            blocking: If True, blocks until playback complete
            abort_check: Callback that returns True if playback should abort (epoch stale)

        Returns:
            True if playback started successfully
        """
        turn = self.turns.get(turn_id)
        if not turn:
            print(f"[Coordinator] Unknown turn: {turn_id}")
            return False

        if not turn.audio_stream:
            print(f"[Coordinator] No audio stream for turn: {turn_id}")
            return False

        # Ensure boundary byte->time conversion uses actual stream format.
        # Pre-buffered turns may not have stored PCM yet, so relying on defaults
        # (48k) can halve timing for 24k streams and desync subtitles/routing.
        stream_sr = getattr(turn.audio_stream, 'sample_rate', None)
        if stream_sr:
            turn._pcm_sample_rate = int(stream_sr)
        stream_ch = getattr(turn.audio_stream, 'channels', None)
        if stream_ch:
            turn._pcm_channels = max(1, int(stream_ch))

        # Check abort before starting
        if abort_check and abort_check():
            print(f"[Coordinator] Abort before start: {turn_id}")
            return False

        self.current_turn_id = turn_id

        def do_playback():
            try:
                # 1. Generate visemes on-demand from stored PCM + word timestamps
                turn.generate_pending_visemes()
                initial_visemes = turn.get_all_visemes()
                print(f"[Coordinator] Starting turn {turn_id} with {len(initial_visemes)} initial visemes")

                # 2. Wait for previous turn's mouth animation to complete
                # This prevents the new lipsync_start from interrupting the closing animation
                if turn.use_3d:
                    _profiler.mark("waiting_prev_turn")
                self.lua_socket.wait_for_turn_complete(timeout=1.0)
                if turn.use_3d:
                    _profiler.mark("prev_turn_done")

                # Check abort after wait
                if abort_check and abort_check():
                    print(f"[Coordinator] Abort after turn wait: {turn_id}")
                    return False

                # 3. Look up per-character lipsync scale and fallback setting
                settings = load_settings()
                lipsync_settings = settings.get('lipsync', {})
                npc_scales = lipsync_settings.get('npc_scales', {})
                default_scale = lipsync_settings.get('default_scale', 1.0)
                scale = npc_scales.get(turn.speaker_id, default_scale)
                fallback = lipsync_settings.get('fallback', False)

                # 4. Mark new turn starting and send lipsync_start
                self.lua_socket.mark_turn_started()
                self._lipsync_ready_event.clear()
                self.lua_socket.send_lipsync_start(
                    speaker=turn.speaker_id,
                    turn_id=turn_id,
                    visemes=self._format_visemes(initial_visemes),
                    scale=scale,
                    fallback=fallback
                )
                if turn.use_3d:
                    _profiler.mark("lipsync_sent")

                # 3. Wait for Lua acknowledgment
                ack_timeout = 0.15  # 150ms max wait
                if not self._lipsync_ready_event.wait(timeout=ack_timeout):
                    print(f"[Coordinator] Warning: No lipsync_ready within {ack_timeout*1000:.0f}ms, starting anyway")
                else:
                    with self._lipsync_ready_lock:
                        if self._lipsync_ready_turn_id != turn_id:
                            print(f"[Coordinator] Warning: lipsync_ready for wrong turn "
                                  f"(got {self._lipsync_ready_turn_id}, expected {turn_id})")

                # Check abort before starting audio
                if abort_check and abort_check():
                    print(f"[Coordinator] Abort before audio: {turn_id}")
                    return False

                # 4. Mark playback started (store in turn for sync loop access)
                turn.playback_started = True
                turn.playback_start_time = time.time()
                playback_start_time = turn.playback_start_time

                # 5. Start sync loop (sends new visemes + audio position)
                self._stop_sync.clear()
                self._sync_thread = threading.Thread(
                    target=self._sync_loop,
                    args=(turn, abort_check),  # Pass abort_check so each loop has its own
                    daemon=True
                )
                self._sync_thread.start()

                # 6. Play audio (blocks until done or aborted)
                print(f"[Coordinator] Starting audio playback for turn {turn_id}")
                # Mark NPC audio start for profiling (skip player turns)
                if turn.use_3d:  # 3D audio = NPC, not player
                    _profiler.mark("npc_audio_start")
                    _profiler.print_time_to_audio()
                success = audio_player.play_stream(
                    turn.audio_stream,
                    use_3d=turn.use_3d,
                    reverb_auxbus=turn.reverb_auxbus,
                    reverb_send=turn.reverb_send,
                    abort_check=abort_check,  # Pass epoch check to audio layer
                    sentence_boundaries=turn.sentence_boundaries,
                )

                # 7. Stop sync loop
                self._stop_sync.set()
                if self._sync_thread:
                    self._sync_thread.join(timeout=1.0)

                playback_duration = time.time() - playback_start_time
                print(f"[Coordinator] Turn {turn_id} complete: {playback_duration:.2f}s, "
                      f"{len(turn.viseme_buffer)} total visemes")

                return success

            except Exception as e:
                print(f"[Coordinator] Playback error: {e}")
                import traceback
                traceback.print_exc()
                return False
            finally:
                self.current_turn_id = None

        if blocking:
            return do_playback()
        else:
            thread = threading.Thread(target=do_playback, daemon=True)
            thread.start()
            return True

    def add_visemes_to_current(self, visemes: List[Dict]):
        """Add visemes to currently playing turn (for streaming)."""
        if self.current_turn_id:
            turn = self.turns.get(self.current_turn_id)
            if turn:
                turn.add_visemes(visemes)

    def _sync_loop(self, turn: TurnState, abort_check: Callable[[], bool] = None):
        """Background loop: sends new visemes and audio position sync."""
        last_sync_time = 0
        sync_interval = 0.1  # 100ms between audio_sync messages

        while not self._stop_sync.is_set():
            # Check epoch abort - exit immediately if stale
            # Note: abort_check is passed as parameter, not instance variable,
            # so each sync loop has its own callback that checks its own epoch
            if abort_check and abort_check():
                print(f"[Coordinator] Sync loop abort (epoch stale)")
                break

            now = time.time()

            # Generate visemes on-demand for any new audio + timestamps
            turn.generate_pending_visemes()

            # Send any new visemes
            new_visemes = turn.get_unsent_visemes()
            if new_visemes:
                self.lua_socket.send({
                    "type": "visemes",
                    "turn_id": turn.turn_id,
                    "frames": self._format_visemes(new_visemes)
                })
                print(f"[Coordinator] Sent {len(new_visemes)} streaming visemes")

            # Send audio position sync
            if now - last_sync_time >= sync_interval:
                audio_pos = self._get_audio_position_safe(turn)
                if audio_pos is not None:
                    self.lua_socket.send({
                        "type": "audio_sync",
                        "turn_id": turn.turn_id,
                        "position": audio_pos
                    })
                    turn.audio_position = audio_pos

                    # Check sentence boundaries for per-sentence subtitles
                    if turn.sentence_boundaries and turn._sentence_subtitles:
                        current_idx = turn._last_subtitle_idx
                        next_idx = current_idx + 1
                        # Monotonic progression only: never move subtitle index backwards.
                        while next_idx < len(turn.sentence_boundaries):
                            boundary = turn.sentence_boundaries[next_idx]
                            st = turn._get_boundary_time(boundary, next_idx)
                            if st is None:
                                break
                            if st <= audio_pos:
                                current_idx = next_idx
                                next_idx += 1
                                continue
                            break
                        if current_idx != turn._last_subtitle_idx and current_idx >= 0:
                            turn._last_subtitle_idx = current_idx
                            boundary = turn.sentence_boundaries[current_idx]
                            is_narr = boundary.get('is_narration', False)
                            bt = turn._get_boundary_time(boundary, current_idx)
                            if bt is None:
                                bt = boundary.get('start_time', 0.0)
                            print(f"[Subtitle] idx={current_idx} at audio_pos={audio_pos:.2f}s "
                                  f"(boundary_start={bt:.2f}s, narration={is_narr})")
                            msg = {
                                "type": "subtitle_update",
                                "turn_id": turn.turn_id,
                                "text": remove_unpaired_double_quotes(boundary['text']),
                                "sentence_idx": current_idx,
                                "total_sentences": len(turn.sentence_boundaries),
                                "is_narration": bool(is_narr),
                            }
                            self.lua_socket.send(msg)

                last_sync_time = now

            time.sleep(0.02)  # 50Hz check rate

    def _get_audio_position_safe(self, turn: TurnState) -> Optional[float]:
        """Get audio position - uses wall clock since playback start."""
        # Return frozen position if paused
        if self._paused_at is not None:
            return self._paused_at

        # Try callback first (if set by audio player)
        if self._get_audio_position:
            try:
                return self._get_audio_position()
            except:
                pass

        # Primary: Use wall clock time since playback started
        # This is accurate because playback_start_time is set right before audio.play()
        if turn.playback_started and turn.playback_start_time > 0:
            return time.time() - turn.playback_start_time

        return None

    def _format_visemes(self, visemes: List[Dict]) -> List[List]:
        """Format visemes for socket transmission:
        [t, jaw, smile, funnel, press, lip_up, ee, o_shape, shh]

        Note: Normalization happens in Lua after all frames are accumulated.
        """
        formatted = []
        for v in visemes:
            if isinstance(v, dict):
                formatted.append([
                    v.get('t', 0),
                    v.get('jaw', 0),
                    v.get('smile', 0),
                    v.get('funnel', 0),
                    v.get('press', 0),
                    v.get('lip_up', 0),
                    v.get('ee', 0),
                    v.get('o_shape', 0),
                    v.get('shh', 0),
                ])
            elif isinstance(v, (list, tuple)) and len(v) >= 4:
                # Pad with zeros if old format (4 values) or take up to 9
                row = list(v[:9])
                while len(row) < 9:
                    row.append(0)
                formatted.append(row)
        return formatted


# Global coordinator instance (set by server.py)
_coordinator: Optional[PlaybackCoordinator] = None


def get_coordinator() -> Optional[PlaybackCoordinator]:
    """Get the global coordinator instance."""
    return _coordinator


def init_coordinator(lua_socket) -> PlaybackCoordinator:
    """Initialize the global coordinator."""
    global _coordinator
    _coordinator = PlaybackCoordinator(lua_socket)
    return _coordinator
