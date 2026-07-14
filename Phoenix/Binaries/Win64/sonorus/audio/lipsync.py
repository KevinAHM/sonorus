"""
Phoneme-based Lip Sync Module

Converts words → phonemes (via Gruut) → visemes (blendshape values)
Outputs timeline for Lua to read and apply to character.

Gap-filling: ONLY when burst tags like [laughs], [sighs] are detected in text,
amplitude-based visemes fill gaps where no word visemes exist.
Gaps are bounded with closed mouth (jaw=0) at start/end to prevent overlap.
"""
import os
import sys
import json
import time
import re

# Add parent to path for utils imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.settings import SETTINGS_FILE

# Numpy for amplitude analysis
try:
    import numpy as np
    NUMPY_AVAILABLE = True
except ImportError:
    NUMPY_AVAILABLE = False
    print("[WARN] Numpy not available - amplitude gap-fill disabled")

# Toggle for amplitude-based gap filling (for vocal bursts like [laughs], [sighs])
# Set to False to disable gap-filling entirely
AMPLITUDE_GAP_FILL_ENABLED = True  # Amplitude visemes fill gaps in word coverage

# Gruut for phoneme conversion
try:
    from gruut import sentences
    GRUUT_AVAILABLE = True
except ImportError:
    GRUUT_AVAILABLE = False
    print("[WARN] Gruut not available - lip sync will use fallback")

# Flag for external availability checks
LIPSYNC_AVAILABLE = GRUUT_AVAILABLE

# Socket reference (set by server.py to avoid circular import)
_lua_socket = None

def set_lua_socket(socket):
    """Set the lua socket reference for sending visemes."""
    global _lua_socket
    _lua_socket = socket


def get_language():
    """Get language from settings.json, fallback to env"""
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                settings = json.load(f)
            # Inworld uses EN_US format, Gruut uses en-us format
            lang = settings.get('tts', {}).get('inworld', {}).get('language', 'EN_US')
            # Convert EN_US -> en-us for Gruut
            return lang.lower().replace('_', '-')
    except:
        pass
    return os.getenv("SONORUS_LANG", "en-us")


# ============================================
# IPA Phoneme → Viseme Mapping
# Gruut outputs IPA phonemes
# ============================================
# Viseme channels:
#   jaw (0-1)      - jaw_drop via Blueprint
#   smile (0-1)    - smile_l/r via Blueprint (minor accents)
#   funnel (0-1)   - lwr/upr_lip_funl_l/r via Blueprint (minor accents)
#   press (0-1)    - mouth_press direct (M, B, P - lips pressed)
#   lip_up (0-1)   - upr_lip_up_l/r direct (F, V - upper lip raises)
#   ee (0-1)       - ee direct (E/I vowels - wide mouth)
#   o_shape (0-1)  - o direct (O/U vowels - rounded mouth)
#   shh (0-1)      - shh direct (SH, CH, ZH - hushing shape)

VISEME_MAP = {
    # =============================================
    # Dedicated morph targets = PRIMARY shape, no jaw
    # These morph targets ARE the complete mouth shape
    # =============================================

    # EE shape — wide mouth (ee morph target)
    "i": {"ee": 0.45},                                  # bee
    "ɪ": {"ee": 0.3},                                   # bit
    "e": {"ee": 0.25},                                  # bay
    "ɛ": {"ee": 0.15},                                  # bed
    "ɨ": {"ee": 0.25},                                  # roses

    # O shape — rounded mouth (o morph target)
    "u": {"o_shape": 0.4},                              # boot
    "ʊ": {"o_shape": 0.3},                              # book
    "o": {"o_shape": 0.25},                             # go
    "ɔ": {"o_shape": 0.2},                              # thought

    # Press — lips together (mouth_press morph target)
    "m": {"press": 0.4},
    "p": {"press": 0.35},
    "b": {"press": 0.3},

    # Shh — hushing shape (shh morph target)
    "ʃ": {"shh": 0.3},                                  # ship
    "ʒ": {"shh": 0.25},                                 # measure
    "tʃ": {"shh": 0.2},                                 # chip
    "dʒ": {"shh": 0.2},                                 # judge

    # Semivowels — use dedicated shapes
    "w": {"o_shape": 0.3},                              # we
    "j": {"ee": 0.25},                                  # yes
    "ʍ": {"o_shape": 0.5},                              # which

    # =============================================
    # No dedicated morph target = jaw only
    # =============================================

    # Open vowels — jaw
    "ɑ": {"jaw": 0.5},                                  # father
    "æ": {"jaw": 0.4},                                  # cat
    "ʌ": {"jaw": 0.4},                                  # cup
    "a": {"jaw": 0.5},                                  # general a
    "ə": {"jaw": 0.2},                                  # about (schwa)
    "ɚ": {"jaw": 0.2},                                  # butter

    # Diphthongs — start shape (jaw for open, shape for target)
    "aɪ": {"jaw": 0.3, "ee": 0.1},                      # my
    "aʊ": {"jaw": 0.3, "o_shape": 0.15},                # now
    "ɔɪ": {"o_shape": 0.2, "ee": 0.15},                 # boy
    "eɪ": {"ee": 0.2},                                  # say
    "oʊ": {"o_shape": 0.4},                             # go

    # Consonants — jaw only
    "f": {"jaw": 0.1},
    "v": {"jaw": 0.1},
    "θ": {"jaw": 0.1},                                  # think
    "ð": {"jaw": 0.1},                                  # this
    "t": {"jaw": 0.1},
    "d": {"jaw": 0.1},
    "n": {"jaw": 0.1},
    "s": {"jaw": 0.05},
    "z": {"jaw": 0.05},
    "l": {"jaw": 0.15},
    "ɹ": {"jaw": 0.15},                                 # r sound
    "r": {"jaw": 0.15},
    "k": {"jaw": 0.15},
    "ɡ": {"jaw": 0.15},
    "g": {"jaw": 0.15},
    "ŋ": {"jaw": 0.1},                                  # sing
    "h": {"jaw": 0.2},
    "ʔ": {"jaw": 0.1},                                  # glottal stop
}

# Default viseme for unknown phonemes
DEFAULT_VISEME = {"jaw": 0.15}

# Silence/rest viseme
REST_VISEME = {"jaw": 0.0}

# ============================================
# Amplitude-based Gap Filling
# ============================================

# Only tags that produce actual audio (not modifiers like [angry], [sad])
AUDIO_BURST_TAGS = {
    'laugh', 'laughs', 'laughing',
    'chuckle', 'chuckles', 'chuckling',
    'giggle', 'giggles', 'giggling',
    'sigh', 'sighs', 'sighing',
    'gasp', 'gasps', 'gasping',
    'sob', 'sobs', 'sobbing',
    'cough', 'coughs', 'coughing',
    'hum', 'hums', 'humming',
    'groan', 'groans', 'groaning',
    'cry', 'cries', 'crying',
    'sniff', 'sniffs', 'sniffing',
    'yawn', 'yawns', 'yawning',
}

# Modifiers for detected audio bursts (bonus smile/funnel, not required for gap-fill)
BURST_MODIFIERS = {
    'laugh': {'smile': 0.5, 'funnel': 0},
    'chuckle': {'smile': 0.4, 'funnel': 0},
    'giggle': {'smile': 0.6, 'funnel': 0},
    'sigh': {'smile': 0, 'funnel': 0.3},
    'gasp': {'smile': 0, 'funnel': 0.4},
    'sob': {'smile': 0, 'funnel': 0.2},
    'hum': {'smile': 0.1, 'funnel': 0.2},
    'groan': {'smile': 0, 'funnel': 0.2},
    'cry': {'smile': 0, 'funnel': 0.15},
    'sniff': {'smile': 0, 'funnel': 0.1},
    'yawn': {'smile': 0, 'funnel': 0.5},
}


def amplitude_visemes_for_audio(pcm_data: bytes, sample_rate: int = 44100,
                                 frame_ms: int = 16) -> list:
    """
    Generate raw amplitude visemes for entire audio chunk.
    Returns visemes at ~60fps with jaw based on RMS amplitude.

    Args:
        pcm_data: Raw 16-bit PCM audio bytes
        sample_rate: Audio sample rate (default 44100)
        frame_ms: Frame interval in ms (16ms ≈ 60fps)

    Returns:
        List of viseme dicts with t, jaw, smile, funnel, _amplitude marker
    """
    if not NUMPY_AVAILABLE or not pcm_data:
        return []

    try:
        samples = np.frombuffer(pcm_data, dtype=np.int16).astype(np.float32)
        samples = samples / 32768.0  # Normalize to -1 to 1

        frame_samples = int(sample_rate * frame_ms / 1000)
        if frame_samples < 1:
            frame_samples = 1

        visemes = []
        for i in range(0, len(samples) - frame_samples, frame_samples):
            window = samples[i:i + frame_samples]
            rms = float(np.sqrt(np.mean(window ** 2)))  # Convert to native Python float

            # Scale RMS to jaw (0-1), with threshold for silence
            # Log curve compresses loud peaks while preserving quiet detail;
            # per-character viseme scale in Lua multiplies on top of this
            if rms < 0.02:
                jaw = 0.0
            else:
                linear = min(1.0, (rms - 0.02) * 2.0)
                jaw = np.log1p(linear * 4.0) / np.log1p(4.0) * 0.45  # log compression + 45% cap

            visemes.append({
                't': round(float(i / sample_rate), 3),  # Ensure native float
                'jaw': round(float(jaw), 2),  # Ensure native float
                'smile': 0.0,
                'funnel': 0.0,
                '_amplitude': True  # Marker for amplitude-generated
            })

        return visemes
    except Exception as e:
        print(f"[Lipsync] Amplitude analysis error: {e}")
        return []


def find_coverage_gaps(word_visemes: list, audio_end: float,
                       audio_start: float = 0, min_gap_ms: float = 1000) -> list:
    """
    Find time ranges where word visemes don't provide coverage.

    Args:
        word_visemes: List of viseme dicts with 't' timestamps
        audio_end: End time of audio segment (base_time + chunk_duration)
        audio_start: Start time of audio segment (base_time for this chunk)
        min_gap_ms: Minimum gap size to consider (default 1000ms = 1s)
                    Only fills significant gaps like [laughs], [sighs], not word spacing

    Returns:
        List of (start, end) tuples representing gaps
    """
    print(f"[Lipsync] find_coverage_gaps: audio_start={audio_start:.3f}s, audio_end={audio_end:.3f}s, min_gap={min_gap_ms}ms")

    if not word_visemes:
        print(f"[Lipsync]   No word visemes - entire audio is a gap")
        return [(audio_start, audio_end)] if audio_end > audio_start else []

    gaps = []
    min_gap = min_gap_ms / 1000
    buffer = 0.05  # 50ms buffer around word visemes

    # Sort by time
    sorted_visemes = sorted(word_visemes, key=lambda v: v.get('t', 0))

    first_t = sorted_visemes[0].get('t', 0)
    last_t = sorted_visemes[-1].get('t', 0)
    print(f"[Lipsync]   Word viseme range: {first_t:.3f}s - {last_t:.3f}s ({len(sorted_visemes)} visemes)")

    # Gap before first viseme (but not before audio_start)
    if first_t - audio_start > min_gap:
        gap = (audio_start, first_t - buffer)
        gaps.append(gap)
        print(f"[Lipsync]   Gap before first word: {gap[0]:.3f}s - {gap[1]:.3f}s ({(gap[1]-gap[0])*1000:.0f}ms)")

    # Gaps between visemes
    for i in range(len(sorted_visemes) - 1):
        curr_t = sorted_visemes[i].get('t', 0)
        next_t = sorted_visemes[i + 1].get('t', 0)
        gap_size = next_t - curr_t

        if gap_size > min_gap:
            gap = (curr_t + buffer, next_t - buffer)
            gaps.append(gap)
            print(f"[Lipsync]   Gap between visemes: {gap[0]:.3f}s - {gap[1]:.3f}s ({(gap[1]-gap[0])*1000:.0f}ms)")

    # Gap after last viseme
    if audio_end - last_t > min_gap:
        gap = (last_t + buffer, audio_end)
        gaps.append(gap)
        print(f"[Lipsync]   Gap after last word: {gap[0]:.3f}s - {gap[1]:.3f}s ({(gap[1]-gap[0])*1000:.0f}ms)")

    if not gaps:
        print(f"[Lipsync]   No gaps >= {min_gap_ms}ms found")

    return gaps


def fill_gaps_with_amplitude(word_visemes: list, amplitude_visemes: list,
                              gaps: list, burst_type: str = None) -> list:
    """
    Fill gaps in word viseme coverage with amplitude visemes.
    ONLY called when a burst tag ([laugh], [sigh], etc.) is detected.

    Word visemes are ALWAYS preserved exactly as-is.
    Amplitude visemes are placed STRICTLY within gaps - no overlap.
    Each gap starts and ends with closed mouth (jaw=0).

    Args:
        word_visemes: Original word-based visemes (preserved exactly)
        amplitude_visemes: Amplitude-generated visemes for gap filling
        gaps: List of (start, end) time tuples from find_coverage_gaps
        burst_type: The detected burst type (e.g., 'laugh', 'sigh')

    Returns:
        Combined list sorted by time
    """
    if not amplitude_visemes or not gaps:
        print(f"[Lipsync] Gap-fill skipped: no amplitude visemes or no gaps")
        return word_visemes

    # Start with word visemes (preserved exactly)
    result = [v.copy() for v in word_visemes]

    # Get word viseme timestamps for overlap checking
    word_times = set()
    for v in word_visemes:
        word_times.add(round(v.get('t', 0), 3))

    print(f"[Lipsync] Gap-fill for burst '{burst_type}': {len(gaps)} gap(s), {len(word_visemes)} word visemes")

    for gap_idx, (gap_start, gap_end) in enumerate(gaps):
        if gap_end <= gap_start:
            print(f"[Lipsync]   Gap {gap_idx}: invalid (end <= start), skipping")
            continue

        gap_duration = gap_end - gap_start
        print(f"[Lipsync]   Gap {gap_idx}: {gap_start:.3f}s - {gap_end:.3f}s ({gap_duration:.3f}s)")

        # Add opening closure frame at gap start
        opening_frame = {
            't': round(gap_start, 3),
            'jaw': 0.0,
            'smile': 0.0,
            'funnel': 0.0,
            '_amplitude': True
        }

        # Check no overlap with word visemes
        if round(gap_start, 3) not in word_times:
            result.append(opening_frame)
            print(f"[Lipsync]     Added opening closure at {gap_start:.3f}s")
        else:
            print(f"[Lipsync]     Opening closure skipped - overlaps word viseme at {gap_start:.3f}s")

        # Find amplitude visemes STRICTLY within this gap (with margin)
        margin = 0.05  # 50ms margin from gap edges
        inner_start = gap_start + margin
        inner_end = gap_end - margin

        gap_visemes = []
        for v in amplitude_visemes:
            t = v.get('t', 0)
            # Strictly within gap (not at edges) and has amplitude
            if inner_start < t < inner_end and v.get('jaw', 0) > 0.1:
                # Double-check no overlap with word visemes
                if round(t, 3) not in word_times:
                    gap_visemes.append(v.copy())

        print(f"[Lipsync]     Found {len(gap_visemes)} amplitude visemes in gap interior")
        result.extend(gap_visemes)

        # Add closing closure frame at gap end
        closing_frame = {
            't': round(gap_end, 3),
            'jaw': 0.0,
            'smile': 0.0,
            'funnel': 0.0,
            '_amplitude': True
        }

        if round(gap_end, 3) not in word_times:
            result.append(closing_frame)
            print(f"[Lipsync]     Added closing closure at {gap_end:.3f}s")
        else:
            print(f"[Lipsync]     Closing closure skipped - overlaps word viseme at {gap_end:.3f}s")

    # Sort by time
    result.sort(key=lambda v: v.get('t', 0))
    return result


def detect_audio_burst_tag(text: str) -> str | None:
    """
    Detect ONLY audio-producing tags from whitelist, not modifiers.

    Args:
        text: Input text that may contain [tag] markers

    Returns:
        Base form of detected burst (e.g., 'laugh') or None
    """
    if not text:
        return None

    matches = re.findall(r'\[(\w+)\]', text.lower())
    for tag in matches:
        if tag in AUDIO_BURST_TAGS:
            # Return base form for modifier lookup
            for base in BURST_MODIFIERS:
                if tag == base or tag.startswith(base):
                    return base
    return None


def apply_burst_modifiers(visemes: list, burst_type: str) -> list:
    """
    Apply smile/funnel modifiers to amplitude visemes based on burst type.
    Only modifies amplitude-generated visemes (marked with _amplitude).
    Word visemes are never modified.

    Args:
        visemes: Combined viseme list
        burst_type: Detected burst type (e.g., 'laugh', 'sigh')

    Returns:
        Modified viseme list
    """
    if burst_type not in BURST_MODIFIERS:
        return visemes

    mods = BURST_MODIFIERS[burst_type]

    for v in visemes:
        # Only modify amplitude-generated visemes
        if v.get('_amplitude') and v.get('jaw', 0) > 0.05:
            intensity = float(v['jaw'])  # Ensure native float
            v['smile'] = round(float(mods.get('smile', 0) * intensity), 2)
            v['funnel'] = round(float(mods.get('funnel', 0) * intensity), 2)

    return visemes


def word_to_phonemes(word, lang=None):
    """Convert a word to list of IPA phonemes using Gruut."""
    if not GRUUT_AVAILABLE:
        return []

    lang = lang or get_language()

    try:
        for sent in sentences(word, lang=lang):
            for w in sent:
                if w.phonemes:
                    return list(w.phonemes)
    except Exception as e:
        print(f"[Lipsync] Gruut error for '{word}': {e}")

    return []


def phoneme_to_viseme(phoneme):
    """Map IPA phoneme to viseme blendshape values."""
    # Try exact match first
    if phoneme in VISEME_MAP:
        return VISEME_MAP[phoneme]

    # Strip stress markers (ˈ ˌ) and digits
    clean = ''.join(c for c in phoneme if c not in 'ˈˌ' and not c.isdigit())
    if clean in VISEME_MAP:
        return VISEME_MAP[clean]

    # Try first character of cleaned phoneme for diphthongs
    if len(clean) > 1 and clean[0] in VISEME_MAP:
        return VISEME_MAP[clean[0]]

    return DEFAULT_VISEME


def debug_visemes(text, lang=None):
    """Debug: show phoneme → viseme mapping for a text string.

    Call from Python console or import:
        from sonorus.audio.lipsync import debug_visemes
        debug_visemes("Hello, my name is Sebastian")
    """
    if not GRUUT_AVAILABLE:
        print("[debug_visemes] Gruut not available")
        return

    lang = lang or get_language()
    print(f"\n{'='*70}")
    print(f"TEXT: {text}")
    print(f"{'='*70}")

    words = text.split()
    for word in words:
        phonemes = word_to_phonemes(word.strip('.,!?;:'), lang)
        if not phonemes:
            print(f"\n  {word}: (no phonemes)")
            continue

        print(f"\n  {word}: [{' '.join(phonemes)}]")
        for ph in phonemes:
            vis = phoneme_to_viseme(ph)
            channels = ', '.join(f"{k}={v}" for k, v in vis.items() if v != 0)
            print(f"    {ph:>4s} → {channels}")

    # Also show what the full frame sequence would look like with fake timing
    print(f"\n{'='*70}")
    print("FRAME SEQUENCE (fake 300ms/word timing):")
    print(f"{'='*70}")
    print(f"  {'t':>6s}  {'jaw':>5s}  {'smil':>5s}  {'funl':>5s}  {'pres':>5s}  {'lipu':>5s}  {'ee':>5s}  {'o':>5s}  {'shh':>5s}  word/phoneme")
    print(f"  {'':->6s}  {'':->5s}  {'':->5s}  {'':->5s}  {'':->5s}  {'':->5s}  {'':->5s}  {'':->5s}  {'':->5s}  {'':->10s}")

    t_ms = 0
    for word in words:
        clean = word.strip('.,!?;:')
        frames = process_word_timing(clean, t_ms, t_ms + 300, lang)
        for f_time, vis, w in frames:
            jaw = vis.get('jaw', 0)
            smile = vis.get('smile', 0)
            funnel = vis.get('funnel', 0)
            press = vis.get('press', 0)
            lip_up = vis.get('lip_up', 0)
            ee = vis.get('ee', 0)
            o_shape = vis.get('o_shape', 0)
            shh = vis.get('shh', 0)
            # Show which phoneme this frame represents
            print(f"  {f_time:6.3f}  {jaw:5.2f}  {smile:5.2f}  {funnel:5.2f}  {press:5.2f}  {lip_up:5.2f}  {ee:5.2f}  {o_shape:5.2f}  {shh:5.2f}  {w}")
        t_ms += 320  # 300ms word + 20ms gap


def process_word_timing(word, start_ms, end_ms, lang=None):
    """
    Convert a word with timing into viseme frames.

    Returns list of (time_sec, viseme_dict, word) tuples.
    The word is included for debugging purposes.
    """
    phonemes = word_to_phonemes(word, lang)

    if not phonemes:
        # Fallback: simple open/close for unknown words
        mid_time = (start_ms + end_ms) / 2 / 1000.0
        return [
            (start_ms / 1000.0, REST_VISEME, word),
            (mid_time, {"jaw": 0.3, "smile": 0.0, "funnel": 0.0}, word),
            (end_ms / 1000.0, REST_VISEME, word),
        ]

    duration_ms = end_ms - start_ms
    phoneme_duration = duration_ms / len(phonemes)

    frames = []

    # Add opening rest frame at word start - keeps mouth closed until word begins
    # Prevents premature mouth movement during gaps between words
    frames.append((start_ms / 1000.0, REST_VISEME, word))

    # Small offset so first phoneme comes slightly after the rest frame
    phoneme_offset = 20  # 20ms after word start (in ms, matching start_ms/end_ms units)

    for i, phoneme in enumerate(phonemes):
        t = start_ms + phoneme_offset + (i * phoneme_duration * 0.9)  # Compress phonemes slightly
        viseme = phoneme_to_viseme(phoneme)
        frames.append((t / 1000.0, viseme, word))

    # Add closing frame at word end - allows mouth to close between words
    # This prevents the "floor effect" where jaw interpolates word-to-word without returning to 0
    frames.append((end_ms / 1000.0, REST_VISEME, word))

    return frames


# ============================================
# Socket-based Viseme Sending
# ============================================

def send_visemes(frames):
    """Send viseme frames via socket.

    Frames can be 2-tuple (t, viseme) or 3-tuple (t, viseme, word).
    Word info is stripped before sending to Lua.
    """
    if not _lua_socket:
        print("[Lipsync] No socket - frames dropped")
        return
    # Handle both 2-tuple and 3-tuple frames (word is optional third element)
    socket_frames = []
    for frame in frames:
        t = frame[0]
        v = frame[1]
        socket_frames.append([t, v.get("jaw", 0), v.get("smile", 0), v.get("funnel", 0),
                              v.get("press", 0), v.get("lip_up", 0), v.get("ee", 0),
                              v.get("o_shape", 0), v.get("shh", 0)])
    _lua_socket.send_visemes(socket_frames)
    print(f"[Lipsync] Sent {len(socket_frames)} frames via socket")


def process_word_alignment(word_alignment, lang=None, auto_send=True,
                           pcm_data: bytes = None, text: str = None,
                           sample_rate: int = 44100, base_time: float = 0,
                           add_closure: bool = False):
    """
    Process TTS word alignment data into visemes with optional gap filling.

    Inworld format:
    {
      "words": ["What", "a", "wonderful", ...],
      "wordStartTimeSeconds": [1.246, 1.511, 1.613, ...],
      "wordEndTimeSeconds": [1.47, 1.531, 1.979, ...]
    }

    Gap filling: When pcm_data is provided, amplitude-based visemes fill gaps
    where word visemes don't cover (e.g., [laughs], [sighs], silence gaps).
    Word visemes are ALWAYS preserved exactly - gap filling only adds to gaps.

    Args:
        word_alignment: Dict with words and timing from Inworld
        lang: Language code for phoneme conversion
        auto_send: If True, sends visemes to Lua immediately (legacy behavior).
                   If False, only returns visemes for coordinator to manage.
        pcm_data: Optional raw 16-bit PCM audio for amplitude gap-filling
        text: Optional original text for burst tag detection ([laughs] etc)
        sample_rate: Audio sample rate (default 44100)
        base_time: Time offset to add to all timestamps (default 0)
        add_closure: If True, adds mouth closure frames at the end (only for final chunk)

    Returns:
        List of viseme dicts: [{t, jaw, smile, funnel}, ...]
    """
    # Handle empty/missing word alignment
    if not word_alignment or isinstance(word_alignment, str):
        words = []
        starts = []
        ends = []
    else:
        words = word_alignment.get("words", [])
        starts = word_alignment.get("wordStartTimeSeconds", [])
        ends = word_alignment.get("wordEndTimeSeconds", [])

    print(f"[Lipsync] Processing {len(words)} words")

    all_frames = []
    for i, word in enumerate(words):
        if word.strip():
            start_ms = (starts[i] if i < len(starts) else 0) * 1000
            end_ms = (ends[i] if i < len(ends) else starts[i] + 0.1) * 1000
            all_frames.extend(process_word_timing(word, start_ms, end_ms, lang))

    # Convert to normalized format: [{t, jaw, smile, funnel}, ...]
    # NOTE: Inworld word times are ABSOLUTE from utterance start, NOT chunk-relative
    # So we do NOT add base_time here - that's only for amplitude visemes
    word_visemes = []
    for frame in all_frames:
        t = frame[0]  # Already absolute - don't add base_time!
        v = frame[1]
        word_visemes.append({
            "t": t,
            "jaw": v.get("jaw", 0),
            "smile": v.get("smile", 0),
            "funnel": v.get("funnel", 0),
            "press": v.get("press", 0),
            "lip_up": v.get("lip_up", 0),
            "ee": v.get("ee", 0),
            "o_shape": v.get("o_shape", 0),
            "shh": v.get("shh", 0),
        })

    # Gap filling: amplitude visemes fill any gaps in word coverage
    gap_filled = False
    amplitude_count = 0
    burst = detect_audio_burst_tag(text) if text else None

    if pcm_data and NUMPY_AVAILABLE and AMPLITUDE_GAP_FILL_ENABLED:
        # Calculate audio duration for this chunk
        chunk_duration = len(pcm_data) / 2 / sample_rate  # 16-bit = 2 bytes/sample
        audio_end = base_time + chunk_duration

        # Generate amplitude visemes for entire audio chunk (relative to chunk start)
        amp_visemes = amplitude_visemes_for_audio(pcm_data, sample_rate)

        # Offset amplitude timestamps by base_time (they're chunk-relative, need absolute)
        for v in amp_visemes:
            v['t'] += base_time

        # Filter word visemes to only those within this chunk's time range
        chunk_word_visemes = [v for v in word_visemes
                             if base_time <= v.get('t', 0) <= audio_end]

        # Find gaps in word viseme coverage (within this chunk's time range)
        gaps = find_coverage_gaps(chunk_word_visemes, audio_end, audio_start=base_time)

        if gaps:
            # Fill gaps with amplitude visemes (word visemes preserved exactly)
            combined = fill_gaps_with_amplitude(word_visemes, amp_visemes, gaps, burst)

            # Apply burst modifiers if burst tag detected (smile/funnel for laughs etc)
            if burst:
                combined = apply_burst_modifiers(combined, burst)

            # Count how many amplitude visemes were added
            amplitude_count = sum(1 for v in combined if v.get('_amplitude'))
            if amplitude_count > 0:
                gap_filled = True

            # Clean up internal markers
            for v in combined:
                v.pop('_amplitude', None)

            word_visemes = combined

    # Add smooth mouth closure frames (200ms, 4 steps) AFTER gap filling
    # Only add closure for the FINAL chunk, not intermediate chunks
    if add_closure and word_visemes:
        last_viseme = word_visemes[-1]
        last_time = last_viseme.get('t', 0)

        # Check if mouth is already nearly closed
        all_channels = ["jaw", "smile", "funnel", "press", "lip_up", "ee", "o_shape", "shh"]
        max_val = max(last_viseme.get(ch, 0) for ch in all_channels)

        if max_val > 0.05:  # Only add closure if mouth is open
            closure_steps = 4
            closure_duration = 0.2  # 200ms total
            step_duration = closure_duration / closure_steps

            for i in range(1, closure_steps + 1):
                t = last_time + (i * step_duration)
                alpha = i / closure_steps  # 0.25, 0.5, 0.75, 1.0
                # Lerp from last_viseme to REST_VISEME (all zeros)
                frame = {
                    "t": t,
                }
                for ch in all_channels:
                    frame[ch] = round(last_viseme.get(ch, 0) * (1 - alpha), 2)
                word_visemes.append(frame)
                # Also add to all_frames for debug output
                all_frames.append((t - base_time, frame, "[closure]"))

            print(f"[Lipsync] Added {closure_steps} closure frames over {closure_duration*1000:.0f}ms")

    if word_visemes:
        # Ensure all values are native Python types (not numpy) for JSON serialization
        for v in word_visemes:
            for key in ['t', 'jaw', 'smile', 'funnel', 'press', 'lip_up', 'ee', 'o_shape', 'shh']:
                v[key] = float(v.get(key, 0))

        # Legacy behavior: auto-send to Lua
        if auto_send:
            # Convert back to tuple format for send_visemes
            tuple_frames = [(v['t'], v, "[gap-fill]" if v.get('_amplitude') else "?") for v in word_visemes]
            send_visemes(tuple_frames)

    return word_visemes


def generate(pcm_data: bytes, text: str = None, word_alignment: dict = None,
             base_time: float = 0, sample_rate: int = 44100, auto_send: bool = False,
             add_closure: bool = False) -> list:
    """
    Generate visemes for audio chunk with automatic gap filling.

    This is the primary entry point for lipsync generation. It:
    1. Generates word visemes from alignment data (if provided)
    2. Fills gaps with amplitude-based visemes (laughs, sighs, silence)
    3. Applies burst modifiers for detected emotion tags
    4. Optionally adds smooth closure frames at the end (for final chunk only)

    Args:
        pcm_data: Raw 16-bit PCM audio bytes (required)
        text: Original text for burst tag detection (optional)
        word_alignment: Inworld word timing data (optional)
        base_time: Time offset for this chunk in overall audio
        sample_rate: Audio sample rate (default 44100)
        auto_send: If True, sends visemes to Lua immediately
        add_closure: If True, adds mouth closure at end (only for final chunk)

    Returns:
        List of viseme dicts: [{t, jaw, smile, funnel}, ...]
    """
    return process_word_alignment(
        word_alignment=word_alignment,
        auto_send=auto_send,
        pcm_data=pcm_data,
        text=text,
        sample_rate=sample_rate,
        base_time=base_time,
        add_closure=add_closure
    )


# ============================================
# Test
# ============================================
if __name__ == "__main__":
    print("Testing lipsync module...")
    print(f"Gruut available: {GRUUT_AVAILABLE}")
    print(f"Numpy available: {NUMPY_AVAILABLE}")
    print(f"Language: {get_language()}")

    # Test word to phonemes
    test_words = ["Hello", "world", "beautiful", "wizard"]
    for word in test_words:
        phonemes = word_to_phonemes(word)
        print(f"  {word}: {phonemes}")

        for p in phonemes:
            v = phoneme_to_viseme(p)
            print(f"    {p} -> jaw={v['jaw']:.2f}, smile={v['smile']:.2f}, funnel={v['funnel']:.2f}")

    # Test word timing processing
    print("\nTesting word timing...")
    frames = process_word_timing("Hello", 0, 500)
    for t, v, w in frames:
        print(f"  {t:.3f}s: jaw={v['jaw']:.2f}, smile={v['smile']:.2f}, funnel={v['funnel']:.2f}")

    # Test burst tag detection
    print("\nTesting burst tag detection...")
    test_texts = [
        "[laughs] That's funny!",
        "[angry] I'm upset",  # Not an audio burst
        "[sighs] Whatever...",
        "No tags here",
        "[giggles] Tee hee!",
    ]
    for text in test_texts:
        burst = detect_audio_burst_tag(text)
        print(f"  '{text}' -> burst='{burst}'")

    # Test amplitude visemes (synthetic audio)
    if NUMPY_AVAILABLE:
        print("\nTesting amplitude visemes...")
        # Generate 1 second of synthetic audio (sine wave)
        sample_rate = 44100
        duration = 1.0
        t = np.linspace(0, duration, int(sample_rate * duration))
        # Amplitude envelope: fade in, sustain, fade out
        envelope = np.minimum(t * 4, 1.0) * np.minimum((duration - t) * 4, 1.0)
        audio = (envelope * np.sin(2 * np.pi * 440 * t) * 16000).astype(np.int16)
        pcm_data = audio.tobytes()

        amp_visemes = amplitude_visemes_for_audio(pcm_data, sample_rate)
        print(f"  Generated {len(amp_visemes)} amplitude visemes for {duration}s audio")
        if amp_visemes:
            print(f"  First: t={amp_visemes[0]['t']:.3f}, jaw={amp_visemes[0]['jaw']:.2f}")
            print(f"  Last:  t={amp_visemes[-1]['t']:.3f}, jaw={amp_visemes[-1]['jaw']:.2f}")

    print("\nDone!")
