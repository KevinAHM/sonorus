"""
End Marker Trimmer - Provider-agnostic audio trimming using word timestamps.

Appends " End." to text, then trims audio at the last real word boundary
when "End." is detected in alignments. Finds zero-crossing for clean cuts.

Usage:
    trimmer = EndMarkerTrimmer(original_text, on_chunk_callback)
    # Pass chunks through trimmer instead of directly to callback
    for chunk, alignment in tts_stream:
        trimmer.process_chunk(chunk, alignment)
    trimmer.flush()  # Call at end of stream
"""
import numpy as np
from typing import Callable, Dict, List, Optional, Tuple
import re


# Marker appended to text - must be a word that won't appear naturally
END_MARKER = " End."
END_MARKER_WORD = "end"

# Debug flag
DEBUG = True

def _debug(msg: str):
    if DEBUG:
        print(f"[EndTrimmer] {msg}")


def normalize_word(word: str) -> str:
    """Normalize word for matching - lowercase, strip punctuation."""
    return re.sub(r'[^\w]', '', word.lower())


def get_original_words(text: str) -> List[str]:
    """Get normalized words from original text."""
    return [normalize_word(w) for w in text.split() if normalize_word(w)]


def find_silence_after_speech(
    audio: np.ndarray,
    start_sample: int,
    sample_rate: int,
    search_seconds: float = 2.0,
    window_ms: int = 20
) -> int:
    """
    Find the best cut point by looking for silence AFTER the last word.

    Since alignment timestamps have drift (reported times are later than actual),
    we search FORWARD from the approximate end of the last word to find the
    lowest energy region (silence between speech and "End." marker).

    Args:
        audio: Audio samples as float32
        start_sample: Approximate end of last real word (from alignment)
        sample_rate: Audio sample rate
        search_seconds: How far to search forward (default 2s)
        window_ms: Window size for energy calculation (default 20ms)

    Returns:
        Best sample index for cutting
    """
    if len(audio) == 0:
        _debug(f"find_silence_after_speech: empty audio")
        return 0

    # Clamp start to valid range
    start_sample = max(0, min(start_sample, len(audio) - 1))

    # Search window
    search_samples = int(search_seconds * sample_rate)
    end_sample = min(len(audio), start_sample + search_samples)

    _debug(f"find_silence_after_speech: start={start_sample} ({start_sample/sample_rate:.3f}s), "
           f"searching {search_samples} samples ({search_seconds}s) forward")

    if start_sample >= end_sample:
        _debug(f"  -> No room to search, returning start")
        return start_sample

    # Calculate RMS energy in windows
    window_samples = int(window_ms * sample_rate / 1000)
    search_audio = audio[start_sample:end_sample]

    # Compute windowed RMS energy
    num_windows = len(search_audio) // window_samples
    if num_windows == 0:
        # Too short, find lowest sample
        min_idx = np.argmin(np.abs(search_audio))
        result = start_sample + min_idx
        _debug(f"  -> Too short for windows, lowest sample at {result}")
        return result

    energies = []
    for i in range(num_windows):
        window = search_audio[i * window_samples : (i + 1) * window_samples]
        rms = np.sqrt(np.mean(window ** 2))
        energies.append(rms)

    energies = np.array(energies)

    # Find the window with lowest energy
    min_window_idx = np.argmin(energies)
    min_energy = energies[min_window_idx]

    # Get the sample position at the START of that quiet window
    quiet_start = start_sample + min_window_idx * window_samples

    _debug(f"  -> Found {num_windows} windows, min energy={min_energy:.6f} at window {min_window_idx}")
    _debug(f"  -> Quiet region starts at sample {quiet_start} ({quiet_start/sample_rate:.3f}s)")

    # Fine-tune: find zero crossing near the start of quiet window
    fine_search = min(window_samples, 400)  # ~17ms or window size
    fine_start = max(0, quiet_start - fine_search)
    fine_end = min(len(audio), quiet_start + fine_search)
    fine_window = audio[fine_start:fine_end]

    signs = np.sign(fine_window)
    crossings = np.where(np.diff(signs) != 0)[0]

    if len(crossings) > 0:
        # Find crossing nearest to quiet_start
        crossing_positions = fine_start + crossings
        distances = np.abs(crossing_positions - quiet_start)
        nearest_idx = np.argmin(distances)
        result = crossing_positions[nearest_idx]
        _debug(f"  -> Fine-tuned to zero crossing at {result} ({result/sample_rate:.3f}s)")
        return result

    _debug(f"  -> No zero crossing found, using quiet region start")
    return quiet_start


class EndMarkerTrimmer:
    """
    Streaming-compatible trimmer that passes through audio until near the end.

    Flow:
    1. Text is padded with " End." before sending to TTS
    2. Audio chunks stream through normally until we approach the end
    3. When last ~3 original words are seen, start buffering
    4. When "End." is detected, trim at last real word and flush buffer
    5. If stream ends without "End.", flush buffer as-is

    This preserves streaming latency while still trimming cleanly at the end.
    """

    def __init__(
        self,
        original_text: str,
        on_chunk: Callable[[bytes, Optional[Dict]], None],
        sample_rate: int = 24000,
        bytes_per_sample: int = 2  # 16-bit PCM
    ):
        """
        Args:
            original_text: Text WITHOUT the End. marker
            on_chunk: Callback for yielding audio chunks
            sample_rate: Audio sample rate
            bytes_per_sample: Bytes per sample (2 for 16-bit PCM)
        """
        self.original_text = original_text
        self.original_words = get_original_words(original_text)
        self.on_chunk = on_chunk
        self.sample_rate = sample_rate
        self.bytes_per_sample = bytes_per_sample

        # How many words from the end to start buffering
        self.buffer_threshold = min(4, max(1, len(self.original_words) - 1))

        # Track all word timings for trim calculation
        self.all_words: List[str] = []
        self.all_word_starts: List[float] = []
        self.all_word_ends: List[float] = []

        # Tail buffer (only recent chunks, not all)
        self.tail_audio: List[bytes] = []
        self.tail_alignments: List[Dict] = []
        self.tail_start_time: float = 0.0  # Audio time when tail buffering started

        # Cumulative audio tracking
        self.total_audio_bytes: int = 0
        self.total_audio_time: float = 0.0

        # State
        self.buffering_tail = False
        self.end_marker_seen = False
        self.flushed = False

        _debug(f"Init: original_words={self.original_words}, buffer_threshold={self.buffer_threshold}")

    def process_chunk(self, audio_bytes: bytes, alignment: Optional[Dict]):
        """
        Process an audio chunk. Streams through until near end, then buffers.
        """
        if self.flushed:
            self.on_chunk(audio_bytes, alignment)
            return

        # Track audio timing
        chunk_duration = 0.0
        if audio_bytes:
            chunk_samples = len(audio_bytes) // self.bytes_per_sample
            chunk_duration = chunk_samples / self.sample_rate

        # Process alignment to track words
        chunk_words = []
        if alignment:
            words = alignment.get("words", [])
            word_starts = alignment.get("wordStartTimeSeconds", [])
            word_ends = alignment.get("wordEndTimeSeconds", [])

            _debug(f"Chunk alignment: words={words}, starts={word_starts}, ends={word_ends}")

            for i, word in enumerate(words):
                normalized = normalize_word(word)
                self.all_words.append(normalized)
                # Track start time (use end of previous word if not provided)
                start_time = word_starts[i] if i < len(word_starts) else (self.all_word_ends[-1] if self.all_word_ends else 0.0)
                end_time = word_ends[i] if i < len(word_ends) else start_time
                self.all_word_starts.append(start_time)
                self.all_word_ends.append(end_time)
                chunk_words.append(normalized)

                # Check for End. marker
                if normalized == END_MARKER_WORD:
                    is_real = self._is_real_end_marker()
                    _debug(f"  Found 'end' word - is_real_end_marker={is_real}")
                    if is_real:
                        self.end_marker_seen = True
        else:
            _debug(f"Chunk: {len(audio_bytes) if audio_bytes else 0} bytes, NO alignment")

        # Determine if we should start buffering the tail
        if not self.buffering_tail and len(self.original_words) > 0:
            # Check how many original words we've seen
            matched_count = self._count_matched_words()
            remaining = len(self.original_words) - matched_count

            if remaining <= self.buffer_threshold:
                self.buffering_tail = True
                self.tail_start_time = self.total_audio_time
                _debug(f"Started buffering tail at {self.tail_start_time:.3f}s (matched={matched_count}, remaining={remaining})")

        # Handle the chunk based on state
        if self.end_marker_seen:
            # End marker found - add to buffer and flush trimmed
            _debug(f"End marker seen - adding to buffer and flushing")
            if audio_bytes:
                self.tail_audio.append(audio_bytes)
            if alignment:
                self.tail_alignments.append(alignment)
            self._flush_trimmed()
        elif self.buffering_tail:
            # Near the end - buffer this chunk
            if audio_bytes:
                self.tail_audio.append(audio_bytes)
            if alignment:
                self.tail_alignments.append(alignment)
        else:
            # Normal streaming - pass through
            self.on_chunk(audio_bytes, alignment)

        # Update totals
        if audio_bytes:
            self.total_audio_bytes += len(audio_bytes)
            self.total_audio_time += chunk_duration

    def _count_matched_words(self) -> int:
        """Count how many original words have been seen in order."""
        if not self.original_words or not self.all_words:
            return 0

        matched = 0
        seen_idx = 0

        for orig_word in self.original_words:
            # Look for this word in remaining seen words
            while seen_idx < len(self.all_words):
                if self.all_words[seen_idx] == orig_word:
                    matched += 1
                    seen_idx += 1
                    break
                seen_idx += 1
            else:
                break  # Didn't find the word

        return matched

    def _is_real_end_marker(self) -> bool:
        """Verify this 'end' is our marker by checking preceding words."""
        if len(self.all_words) < 2:
            _debug(f"  _is_real_end_marker: too few words ({len(self.all_words)})")
            return False

        words_before_end = self.all_words[:-1]
        if not words_before_end:
            return False

        match_count = min(3, len(self.original_words), len(words_before_end))
        if match_count == 0:
            return True

        original_tail = self.original_words[-match_count:]
        seen_tail = words_before_end[-match_count:]

        matches = original_tail == seen_tail
        _debug(f"  _is_real_end_marker: comparing original_tail={original_tail} vs seen_tail={seen_tail} -> {matches}")
        return matches

    def _flush_trimmed(self):
        """Trim buffered tail audio at last real word and flush."""
        if self.flushed:
            return
        self.flushed = True

        _debug(f"_flush_trimmed: tail_audio chunks={len(self.tail_audio)}, tail_start_time={self.tail_start_time:.3f}s")
        _debug(f"  all_words={self.all_words}")
        _debug(f"  all_word_starts={self.all_word_starts}")
        _debug(f"  all_word_ends={self.all_word_ends}")

        if not self.tail_audio:
            _debug("  No tail audio to flush")
            return

        # Concatenate tail audio
        tail_bytes = b''.join(self.tail_audio)
        if not tail_bytes:
            _debug("  Empty tail bytes")
            return

        _debug(f"  Tail audio: {len(tail_bytes)} bytes")

        # Convert to numpy
        audio = np.frombuffer(tail_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        _debug(f"  Audio samples: {len(audio)} ({len(audio)/self.sample_rate:.3f}s)")

        # Get approximate end of last real word (alignment has drift, so this is rough)
        last_word_end = self._get_last_real_word_end()
        _debug(f"  Last real word end (from alignment): {last_word_end}")

        if last_word_end is not None and last_word_end > self.tail_start_time:
            # Convert to sample position relative to tail buffer
            search_start_time = last_word_end - self.tail_start_time
            search_start_sample = int(search_start_time * self.sample_rate)
            _debug(f"  Search start: {search_start_time:.3f}s relative to tail, sample {search_start_sample}")

            if 0 < search_start_sample < len(audio):
                # Find silence after the last word - search up to 2 seconds forward
                cut_sample = find_silence_after_speech(
                    audio,
                    search_start_sample,
                    self.sample_rate,
                    search_seconds=2.0,
                    window_ms=20
                )
                _debug(f"  Cutting at sample {cut_sample} ({cut_sample/self.sample_rate:.3f}s)")
                audio = audio[:cut_sample]
            else:
                _debug(f"  search_start_sample {search_start_sample} out of range [0, {len(audio)}), not trimming")
        else:
            _debug(f"  last_word_end ({last_word_end}) not > tail_start_time ({self.tail_start_time}), not trimming")

        # Fade out
        fade_samples = int(self.sample_rate * 0.005)
        if len(audio) > fade_samples:
            fade_curve = np.linspace(1, 0, fade_samples)
            audio[-fade_samples:] *= fade_curve
            _debug(f"  Applied {fade_samples} sample fade out")

        # Convert back
        pcm_int16 = (audio * 32767).astype(np.int16)
        trimmed_bytes = pcm_int16.tobytes()
        _debug(f"  Final output: {len(trimmed_bytes)} bytes ({len(audio)/self.sample_rate:.3f}s)")

        # Build trimmed alignment
        trimmed_alignment = self._get_trimmed_alignment()

        self.on_chunk(trimmed_bytes, trimmed_alignment)

    def _get_last_real_word_end(self) -> Optional[float]:
        """
        Get the end time of the last real word (before "End." marker).

        Since alignment has drift, this is just a starting point for silence search.
        """
        if len(self.all_words) < 2:
            return None

        # Find "End." marker index
        end_marker_idx = None
        for i in range(len(self.all_words) - 1, -1, -1):
            if self.all_words[i] == END_MARKER_WORD:
                end_marker_idx = i
                break

        if end_marker_idx is None or end_marker_idx == 0:
            # No marker or no word before it
            if self.all_word_ends:
                return self.all_word_ends[-1]
            return None

        # Get the word BEFORE "End."
        last_real_idx = end_marker_idx - 1
        last_real_word = self.all_words[last_real_idx]
        last_real_end = self.all_word_ends[last_real_idx] if last_real_idx < len(self.all_word_ends) else None

        _debug(f"  _get_last_real_word_end: last real word '{last_real_word}' at idx {last_real_idx}, end={last_real_end}")
        return last_real_end

    def _get_trimmed_alignment(self) -> Optional[Dict]:
        """Build alignment excluding End. marker, only for tail portion."""
        # Merge tail alignments
        words = []
        starts = []
        ends = []

        for align in self.tail_alignments:
            if align:
                words.extend(align.get("words", []))
                starts.extend(align.get("wordStartTimeSeconds", []))
                ends.extend(align.get("wordEndTimeSeconds", []))

        if not words:
            return None

        # Filter out "End." and variants
        filtered_words = []
        filtered_starts = []
        filtered_ends = []

        for w, s, e in zip(words, starts, ends):
            if normalize_word(w) != END_MARKER_WORD:
                filtered_words.append(w)
                filtered_starts.append(s)
                filtered_ends.append(e)

        if not filtered_words:
            return None

        return {
            "words": filtered_words,
            "wordStartTimeSeconds": filtered_starts,
            "wordEndTimeSeconds": filtered_ends
        }

    def flush(self):
        """Call at stream end if End. marker wasn't seen."""
        if self.flushed:
            return
        self.flushed = True

        _debug(f"flush() called - End marker was NOT seen")
        _debug(f"  all_words seen: {self.all_words}")

        if not self.tail_audio:
            _debug("  No tail audio buffered")
            return

        # No End. marker - flush tail as-is
        tail_bytes = b''.join(self.tail_audio)
        _debug(f"  Flushing {len(tail_bytes)} bytes untrimmed")

        # Merge alignments
        alignment = None
        if self.tail_alignments:
            words = []
            starts = []
            ends = []
            for align in self.tail_alignments:
                if align:
                    words.extend(align.get("words", []))
                    starts.extend(align.get("wordStartTimeSeconds", []))
                    ends.extend(align.get("wordEndTimeSeconds", []))
            if words:
                alignment = {
                    "words": words,
                    "wordStartTimeSeconds": starts,
                    "wordEndTimeSeconds": ends
                }

        self.on_chunk(tail_bytes, alignment)


def pad_text_with_end_marker(text: str) -> str:
    """Add End. marker to text for trimming.

    For text without sentence-ending punctuation (like player input),
    adds a period first to create a natural boundary before the marker.
    """
    text = text.strip()
    if not text:
        return END_MARKER.strip()

    # Sentence-ending punctuation
    sentence_endings = '.!?'

    # If text doesn't end with sentence punctuation, add a period
    # This helps with non-LLM text like "hey what's going on" -> "hey what's going on."
    if text[-1] not in sentence_endings:
        text = text + '.'

    result = text + END_MARKER
    _debug(f"pad_text_with_end_marker: '{text[:50]}...' -> added '{END_MARKER}'")
    return result
