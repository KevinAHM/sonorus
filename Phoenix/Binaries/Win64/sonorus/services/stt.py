"""
Speech-to-Text service wrapper.
Provides unified interface that switches based on settings.
"""
import os
import sys

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.settings import load_settings


def get_provider():
    """Get the configured STT provider module (fresh each call)."""
    settings = load_settings()
    provider_name = settings.get('stt', {}).get('provider', 'none')

    if provider_name == 'none':
        return None
    elif provider_name == 'whisper':
        from . import whisper_stt as provider_module
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
    return provider.transcribe(audio_data, sample_rate)


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

    return False


def get_provider_name() -> str:
    """Get current provider name."""
    settings = load_settings()
    return settings.get('stt', {}).get('provider', 'none')
