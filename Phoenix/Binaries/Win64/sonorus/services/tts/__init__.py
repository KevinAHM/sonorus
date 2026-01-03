"""
TTS Provider Package

Provides a unified interface for text-to-speech providers (Inworld, ElevenLabs).
"""
import os
import sys

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from utils.settings import load_settings

from .base import BaseTTSProvider, VoiceCache

# Cached provider instances
_providers = {}


def get_provider():
    """Get the configured TTS provider instance (cached)."""
    settings = load_settings()
    provider_name = settings.get('tts', {}).get('provider', 'inworld')

    if provider_name not in _providers:
        if provider_name == 'elevenlabs':
            from .elevenlabs import ElevenLabsProvider
            _providers[provider_name] = ElevenLabsProvider()
        else:
            from .inworld import InworldProvider
            _providers[provider_name] = InworldProvider()

    return _providers[provider_name]


def init():
    """Initialize TTS provider (loads voice cache)."""
    return get_provider().init()


def speak(text, character_name, **kwargs):
    """
    Speak text as a character.

    Args:
        text: Text to speak
        character_name: Character whose voice to use
        **kwargs: Additional arguments passed to provider

    Returns:
        {"success": bool, "word_timings": list, "error": str or None}
    """
    return get_provider().speak(text, character_name, **kwargs)


def prepare_tts(text, character_name, **kwargs):
    """
    Pre-buffer TTS without playing.

    Returns:
        (tts_stream, word_timings, visemes) tuple on success, None if failed
    """
    return get_provider().prepare_tts(text, character_name, **kwargs)


def get_or_create_voice(character_name, lang=None):
    """Get voice for character, cloning if necessary."""
    return get_provider().get_or_create_voice(character_name, lang)


def list_voices(lang=None):
    """List available voices."""
    return get_provider().list_voices(lang)


def get_voice(name, lang=None):
    """Get a specific voice by name."""
    return get_provider().get_voice(name, lang)


def is_available() -> bool:
    """Check if TTS is properly configured."""
    settings = load_settings()
    tts_settings = settings.get('tts', {})
    provider = tts_settings.get('provider', 'inworld')

    if provider == 'inworld':
        inworld = tts_settings.get('inworld', {})
        return bool(inworld.get('api_key') and inworld.get('workspace_id'))
    elif provider == 'elevenlabs':
        elevenlabs = tts_settings.get('elevenlabs', {})
        return bool(elevenlabs.get('api_key'))

    return False


def get_provider_name() -> str:
    """Get current provider name."""
    settings = load_settings()
    return settings.get('tts', {}).get('provider', 'inworld')


def synthesize_to_bytes(text, character_name, lang=None):
    """Synthesize text to raw PCM audio bytes."""
    provider = get_provider()
    voice = provider.get_or_create_voice(character_name, lang)
    if not voice:
        raise Exception(f"Voice '{character_name}' not found.")

    voice_id = voice.get('voiceId') or voice.get('voice_id')
    if not voice_id:
        raise Exception(f"Voice ID not found for '{character_name}'")

    pcm_chunks = []
    def on_chunk(pcm_bytes, word_timing):
        if pcm_bytes:
            pcm_chunks.append(pcm_bytes)

    success = provider.synthesize_stream(text, voice_id, on_chunk)
    if not success:
        raise Exception("TTS synthesis failed.")
    if not pcm_chunks:
        raise Exception("No audio data received.")

    return b''.join(pcm_chunks), provider.get_sample_rate()
