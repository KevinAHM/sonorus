"""
No-op TTS Provider (Subtitles Only)

Provides subtitle display with synthetic lip sync, but no audio playback.
Works WITH the existing TTS pipeline by generating silence + synthetic word timings.
"""

import re
from typing import Any, Callable, Dict, Optional

from .base import BaseTTSProvider, VoiceCache

# Try to import textstat for syllable counting
try:
    import textstat
    TEXTSTAT_AVAILABLE = True
except ImportError:
    TEXTSTAT_AVAILABLE = False
    print("[NoTTS] textstat not available - using character-based estimation")


# ============================================
# Duration & Word Alignment Estimation
# ============================================

def estimate_speaking_duration(text: str, lang: str = "en") -> float:
    """
    Estimate how long text would take to speak aloud.
    Returns duration in seconds (minimum 2.0).
    """
    if not text or not text.strip():
        return 2.0

    if TEXTSTAT_AVAILABLE:
        try:
            lang_code = lang[:2].lower() if lang else "en"
            textstat.set_lang(lang_code)
            syllables = textstat.syllable_count(text)
            if syllables > 0:
                # ~4 syllables/second = natural speech pace
                return max(2.0, syllables / 4.0)
        except Exception as e:
            print(f"[NoTTS] textstat error: {e}")

    # Fallback: character-based (~10 chars/sec)
    char_count = len(text.replace(" ", ""))
    return max(2.0, char_count / 10.0)


def generate_word_alignment(text: str, total_duration: float) -> dict:
    """
    Generate synthetic word timings in the same format as real TTS providers.
    This feeds directly into lipsync.process_word_alignment().
    """
    # Tokenize - keep words, skip punctuation
    words = re.findall(r'\b\w+\b', text)
    if not words:
        return {
            "words": [],
            "wordStartTimeSeconds": [],
            "wordEndTimeSeconds": []
        }

    # Count syllables per word
    syllables = []
    for word in words:
        if TEXTSTAT_AVAILABLE:
            try:
                count = textstat.syllable_count(word)
                syllables.append(max(1, count))
            except:
                syllables.append(max(1, len(word) // 3))
        else:
            syllables.append(max(1, len(word) // 3))

    total_syllables = sum(syllables) or 1

    # Distribute time proportionally by syllable count
    starts = []
    ends = []
    current_time = 0.0

    for syls in syllables:
        starts.append(current_time)
        word_duration = (syls / total_syllables) * total_duration
        current_time += word_duration
        ends.append(current_time)

    return {
        "words": words,
        "wordStartTimeSeconds": starts,
        "wordEndTimeSeconds": ends
    }


# ============================================
# Dummy Voice Cache
# ============================================

class DummyVoiceCache(VoiceCache):
    """No-op voice cache for the none provider."""

    def __init__(self):
        super().__init__()
        self._loaded = True

    def _make_cache_key(self, name: str, lang: Optional[str] = None) -> str:
        return name

    def load(self) -> bool:
        return True

    def save(self) -> None:
        pass

    def refresh(self) -> None:
        pass

    def get(self, name: str, lang: Optional[str] = None) -> Optional[Dict]:
        return None

    def add(self, name: str, voice: Dict, lang: Optional[str] = None) -> None:
        pass

    def list_all(self, lang: Optional[str] = None) -> list:
        return []


_dummy_cache = DummyVoiceCache()


# ============================================
# NoTTSProvider
# ============================================

class NoTTSProvider(BaseTTSProvider):
    """
    Subtitle-only TTS provider.

    Works WITH the existing pipeline by:
    1. Generating synthetic word timings (like TTS API would provide)
    2. Feeding silence + word timings through synthesize_stream()
    3. Letting base.speak() handle coordinator, lipsync, playback

    The audio player plays silence while visemes animate the face.
    """

    @property
    def name(self) -> str:
        return "none"

    def get_config(self) -> dict:
        return {}

    def get_sample_rate(self) -> int:
        return 24000  # Match typical TTS sample rate

    def get_voice_cache(self) -> VoiceCache:
        return _dummy_cache

    def clone_voice(self, display_name: str, reference_wav_path: str,
                    lang: Optional[str] = None) -> Optional[Dict]:
        return None

    def get_or_create_voice(self, character_name: str,
                            lang: Optional[str] = None,
                            lua_socket: Any = None) -> Optional[Dict]:
        """Return a placeholder voice - no actual voice needed."""
        return {"voiceId": "none", "name": character_name}

    def synthesize_stream(self, text: str, voice_id: str,
                          on_chunk: Callable[[bytes, Optional[Dict]], None],
                          speaker_id: Optional[str] = None) -> bool:
        """
        Generate silence + synthetic word timings.

        Instead of calling a TTS API, we:
        1. Generate word alignment based on estimated duration
        2. Feed silence PCM chunks with word timing data
        3. The base speak() processes this through lipsync automatically
        """
        # Strip [emotion] tags for duration estimation
        clean_text = re.sub(r'\[\w+\]\s*', '', text)

        # Estimate duration and generate word alignment
        duration = estimate_speaking_duration(clean_text)
        word_alignment = generate_word_alignment(clean_text, duration)

        print(f"[NoTTS] Synthesizing: {duration:.1f}s, {len(word_alignment.get('words', []))} words")

        # Calculate how much silence to generate
        sample_rate = self.get_sample_rate()
        bytes_per_second = sample_rate * 2  # 16-bit mono
        total_bytes = int(duration * bytes_per_second)

        # Generate silence in chunks (like streaming TTS would)
        # Use ~0.5s chunks to match typical TTS streaming
        chunk_size = int(0.5 * bytes_per_second)
        bytes_sent = 0

        # Send first chunk with word alignment (like real TTS does)
        first_chunk = bytes(min(chunk_size, total_bytes))
        on_chunk(first_chunk, word_alignment)
        bytes_sent += len(first_chunk)

        # Send remaining silence chunks (no word timing, already sent)
        while bytes_sent < total_bytes:
            remaining = total_bytes - bytes_sent
            chunk = bytes(min(chunk_size, remaining))
            on_chunk(chunk, None)
            bytes_sent += len(chunk)

        print(f"[NoTTS] Synthesized {bytes_sent} bytes of silence")
        return True
