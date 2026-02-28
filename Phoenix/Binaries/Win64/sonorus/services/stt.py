"""
Speech-to-Text service wrapper.
Provides unified interface that switches based on settings.
"""
import os
import re
import sys
import time

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.settings import load_settings


# ============================================================================
# Spell Correction Maps
# ============================================================================
# Applied when voice_spells is enabled to fix common STT mistranscriptions.
#
# ALWAYS_REPLACE: Substring matches — words/phrases nobody would actually say.
#                 Replaced wherever they appear in the transcription.
#
# EXACT_REPLACE:  Full-match only — words/phrases that could be normal speech.
#                 Only replaced when the entire transcription matches.

ALWAYS_REPLACE = {
    "eckio": "accio",
    "ackeo": "accio",
    "akeo": "accio",
    "eckeo": "accio",
    "acio": "accio",
    "akio": "accio",
    "ackio": "accio",
    "hack you": "accio",
    "expel the arms": "expelliarmus",
    "expel the armas": "expelliarmus",
    "expel yarmus": "expelliarmus",
    "belly armas": "expelliarmus",
    "can fringo": "confringo",
    "kun fringo": "confringo",
    "hello gamora": "alohomora",
    "hello homora": "alohomora",
    "aloha mora": "alohomora",
    "alohamora": "alohomora",
    "alo hamora": "alohomora",
    "alo gamora": "alohomora",
    "defendo": "diffindo",
    "patronus totalis": "petrificus totalus",
    "the pulsor": "depulso",
    "flip endow": "flipendo",
    "vada cadavera": "avada kedavra",
    "avada cadaver": "avada kedavra",
    "ravelio": "revelio",
    "ruvelo": "revelio",
    "repero": "reparo",
    "portago": "protego",
    "clacius": "glacius",
    "levy also": "levioso",
}

EXACT_REPLACE = {
    "winning the floor": "wingardium leviosa",
    "glaciers": "glacius",
    "let me yourself": "levioso",
    "aloha": "alohomora",
    "knox": "nox",
    "fucks": "nox",
}

# Pre-compile regex patterns for ALWAYS_REPLACE (longest first to avoid partial clobber)
_ALWAYS_PATTERNS = []
for _phrase in sorted(ALWAYS_REPLACE.keys(), key=len, reverse=True):
    _pattern = re.compile(r'\b' + re.escape(_phrase) + r'\b', re.IGNORECASE)
    _ALWAYS_PATTERNS.append((_pattern, ALWAYS_REPLACE[_phrase]))

# Pre-normalize EXACT_REPLACE keys
_EXACT_NORMALIZED = {}
for _key, _val in EXACT_REPLACE.items():
    _norm = re.sub(r'[^\w\s]', '', _key.lower()).strip()
    _norm = ' '.join(_norm.split())
    _EXACT_NORMALIZED[_norm] = _val


def _correct_spell_transcription(text: str) -> str:
    """
    Fix common STT mistranscriptions of spell names.

    Applies two tiers:
    1. ALWAYS_REPLACE — substring word-boundary matches (things nobody would say)
    2. EXACT_REPLACE — full-text matches only (things that could be normal speech)
    """
    if not text:
        return text

    # Normalize for comparison: lowercase, strip punctuation, collapse whitespace
    normalized = re.sub(r'[^\w\s]', '', text.lower()).strip()
    normalized = ' '.join(normalized.split())

    # Tier 2: Exact match (check first since it's the whole utterance)
    if normalized in _EXACT_NORMALIZED:
        corrected = _EXACT_NORMALIZED[normalized]
        print(f"[STT] Spell correction (exact): \"{text}\" -> \"{corrected}\"")
        return corrected

    # Tier 1: Substring word-boundary replacements (on original text to preserve casing/punctuation)
    corrected = text
    changed = False
    for pattern, replacement in _ALWAYS_PATTERNS:
        new_text = pattern.sub(replacement, corrected)
        if new_text != corrected:
            changed = True
            corrected = new_text

    if changed:
        print(f"[STT] Spell correction (substring): \"{text}\" -> \"{corrected}\"")
        return corrected

    return text


def get_provider():
    """Get the configured STT provider module (fresh each call)."""
    settings = load_settings()
    provider_name = settings.get('stt', {}).get('provider', 'none')

    if provider_name == 'none':
        return None
    elif provider_name == 'whisper':
        from . import whisper_stt as provider_module
    elif provider_name == 'parakeet':
        from . import parakeet_stt as provider_module
    elif provider_name == 'canary':
        from . import canary_stt as provider_module
    elif provider_name == 'moonshine':
        from . import moonshine_stt as provider_module
    else:
        from . import deepgram_stt as provider_module

    return provider_module


def transcribe(audio_data: bytes, sample_rate: int = 16000) -> dict:
    """
    Transcribe audio to text.

    Args:
        audio_data: Raw PCM audio bytes (16-bit mono)
        sample_rate: Audio sample rate (default 16000)

    Returns:
        {
            "success": bool,
            "text": str,           # Transcribed text
            "confidence": float,   # 0.0-1.0 if available
            "error": str or None
        }
    """
    provider = get_provider()
    if provider is None:
        return {"success": False, "text": "", "confidence": 0.0, "error": "STT provider is disabled"}

    t0 = time.perf_counter()
    result = provider.transcribe(audio_data, sample_rate)
    elapsed_ms = (time.perf_counter() - t0) * 1000

    # Apply spell correction if voice_spells is enabled
    # Helps the text-match fallback catch spells that wakeword detection missed
    if result["success"] and result["text"]:
        settings = load_settings()
        if settings.get('stt', {}).get('voice_spells', True):
            result["text"] = _correct_spell_transcription(result["text"])

    provider_name = get_provider_name()
    if result["success"]:
        print(f"[STT/{provider_name}] Transcribed: \"{result['text']}\" (conf: {result['confidence']:.2f}) [{elapsed_ms:.0f}ms]")
    else:
        print(f"[STT/{provider_name}] Failed ({result.get('error', 'unknown')}) [{elapsed_ms:.0f}ms]")

    return result


def is_available() -> bool:
    """Check if STT is properly configured."""
    settings = load_settings()
    stt_settings = settings.get('stt', {})

    provider = stt_settings.get('provider', 'none')

    # Provider set to "none" means STT is disabled
    if provider == 'none':
        return False

    if provider == 'deepgram':
        return bool(stt_settings.get('deepgram', {}).get('api_key'))
    elif provider == 'whisper':
        # Whisper falls back to LLM API key
        whisper_key = stt_settings.get('whisper', {}).get('api_key')
        llm_key = settings.get('llm', {}).get('api_key')
        return bool(whisper_key or llm_key)
    elif provider == 'parakeet':
        # Local model - always available (downloads on first use)
        return True
    elif provider == 'canary':
        # Local model - always available (downloads on first use)
        return True
    elif provider == 'moonshine':
        # Local model - always available (downloads on first use)
        return True

    return False


def get_provider_name() -> str:
    """Get current provider name."""
    settings = load_settings()
    return settings.get('stt', {}).get('provider', 'none')
