"""
TTS Provider Package

Provides a unified interface for text-to-speech providers (Inworld, ElevenLabs).
"""
import os
import sys
import importlib

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from utils.settings import load_settings, is_dev_mode

from .base import BaseTTSProvider, VoiceCache

# End marker trimmer for clean audio cutoffs
# DISABLED - alignment model timestamps too inaccurate for reliable trimming
END_TRIMMER_ENABLED = False
try:
    from audio.end_trimmer import EndMarkerTrimmer, pad_text_with_end_marker
    END_TRIMMER_AVAILABLE = END_TRIMMER_ENABLED
except ImportError:
    END_TRIMMER_AVAILABLE = False

# Cached provider instances
_providers = {}


def get_provider():
    """Get the configured TTS provider instance (cached)."""
    settings = load_settings()
    provider_name = settings.get('tts', {}).get('provider', 'inworld')

    if provider_name not in _providers:
        if provider_name == 'none':
            from .none import NoTTSProvider
            _providers[provider_name] = NoTTSProvider()
        elif provider_name == 'elevenlabs':
            from .elevenlabs import ElevenLabsProvider
            _providers[provider_name] = ElevenLabsProvider()
        elif provider_name == 'pocket' or provider_name == 'pocket_onnx':
            from .pocket_onnx import PocketOnnxProvider
            _providers[provider_name] = PocketOnnxProvider()
        elif provider_name == 'omnivoice':
            from .omnivoice import OmniVoiceProvider
            _providers[provider_name] = OmniVoiceProvider()
        elif provider_name == 'universal':
            from .universal import UniversalProvider
            _providers[provider_name] = UniversalProvider()
        elif provider_name == 'omnivoice_cpp':
            provider_module = importlib.import_module('.omnivoice_cpp', __name__)
            _providers[provider_name] = provider_module.OmniVoiceCppProvider()
        else:
            from .inworld import InworldProvider
            _providers[provider_name] = InworldProvider()

    return _providers[provider_name]


def init():
    """Initialize TTS provider (loads voice cache)."""
    return get_provider().init()


def _apply_pronunciation(text):
    """Apply pronunciation replacements and normalize audio tags for current provider."""
    from utils.text_utils import apply_pronunciation_replacements, normalize_audio_tags
    provider = get_provider_name()
    text = normalize_audio_tags(text, provider)
    text = apply_pronunciation_replacements(text, tts_provider=provider)
    return text


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
    text = _apply_pronunciation(text)
    return get_provider().speak(text, character_name, **kwargs)


def speak_streaming(sentence_gen, character_name, **kwargs):
    """
    Stream sentences through TTS and play audio in real-time.
    Sentences are fed to TTS as they arrive from LLM streaming.

    Args:
        sentence_gen: Generator yielding sentence strings or (text, is_narration) tuples
        character_name: Character whose voice to use
        **kwargs: Additional arguments (setup_event, setup_data, etc.)

    Returns:
        {"success": bool, "word_timings": list, "error": str or None}
    """
    # Track original (clean) sentences for subtitle display
    # (pronunciation-fixed text should not appear in subtitles)
    original_sentences = []

    setup_data = kwargs.get('setup_data')

    # Resolve narrator voice if narration is enabled
    settings = load_settings()
    narration_enabled = settings.get('conversation', {}).get('narration_enabled', False)
    if narration_enabled and setup_data is not None:
        narrator_voice_id = _resolve_narrator_voice()
        if narrator_voice_id:
            setup_data['_narrator_voice_id'] = narrator_voice_id

    # Apply pronunciation to each sentence as it arrives
    # Handles both plain strings and (text, is_narration) tuples
    def processed_gen():
        for item in sentence_gen:
            if isinstance(item, tuple):
                text, is_narration = item
                if text:
                    original_sentences.append((text, is_narration))
                    yield (_apply_pronunciation(text), is_narration)
            else:
                if item:
                    original_sentences.append(item)
                    yield _apply_pronunciation(item)

    # Pass clean sentences list through setup_data so base.py can use them
    if setup_data is not None:
        setup_data['_original_sentences'] = original_sentences

    return get_provider().speak_streaming(processed_gen(), character_name, **kwargs)


def _resolve_narrator_voice():
    """Resolve the narrator voice ID based on settings and available voices.

    Returns voice name/ID string, or None if narration not possible.
    """
    settings = load_settings()
    conv = settings.get('conversation', {})

    # Explicit narrator voice setting
    narrator_voice = conv.get('narrator_voice', '').strip()
    if narrator_voice:
        return narrator_voice

    # Check for Narrator reference wav
    from .voice_utils import find_voice_reference
    if find_voice_reference('Narrator'):
        return 'Narrator'

    # Provider-specific fallbacks
    provider_name = get_provider_name()
    if provider_name in ('pocket', 'pocket_onnx', 'omnivoice', 'omnivoice_cpp', 'universal'):
        return 'GreyCat'
    elif provider_name == 'inworld':
        return 'Graham'

    return None


def _segment_text_for_prebuffer(text: str, narration_enabled: bool, narration_min_words: int = 3):
    """Split full response text into sentence/narration segments."""
    from utils.text_utils import split_into_sentences_safe

    if not text or not text.strip():
        return []

    segments = []
    sentence_chunks = split_into_sentences_safe(text)
    if not sentence_chunks:
        sentence_chunks = [text]

    if narration_enabled:
        from utils.narration import parse_segments
        for sentence in sentence_chunks:
            for seg in parse_segments(sentence, narration_min_words=narration_min_words):
                seg_text = (seg.text or "").strip()
                if not seg_text:
                    continue
                segments.append((seg_text, bool(seg.is_narration)))
    else:
        for sentence in sentence_chunks:
            seg_text = (sentence or "").strip()
            if seg_text:
                segments.append((seg_text, False))

    return segments


def prepare_tts(text, character_name, **kwargs):
    """
    Pre-buffer TTS without playing.

    Returns:
        (tts_stream, word_timings, visemes, sentence_boundaries) tuple on success,
        None if failed
    """
    settings = load_settings()
    narration_enabled = settings.get('conversation', {}).get('narration_enabled', False)
    narration_min_words = max(1, int(kwargs.pop('narration_min_words', 3) or 3))

    segmented = _segment_text_for_prebuffer(
        text,
        narration_enabled=narration_enabled,
        narration_min_words=narration_min_words,
    )
    if not segmented:
        processed_text = _apply_pronunciation(text)
        return get_provider().prepare_tts(processed_text, character_name, **kwargs)

    original_sentences = list(segmented)
    narrator_voice_id = _resolve_narrator_voice() if narration_enabled else None

    def processed_gen():
        for seg_text, is_narration in segmented:
            yield (_apply_pronunciation(seg_text), is_narration)

    processed_text = _apply_pronunciation(text)
    return get_provider().prepare_tts(
        processed_text,
        character_name,
        sentence_gen=processed_gen(),
        original_sentences=original_sentences,
        narrator_voice_id=narrator_voice_id,
        **kwargs,
    )


def get_or_create_voice(character_name, lang=None, lua_socket=None):
    """Get voice for character, cloning if necessary."""
    return get_provider().get_or_create_voice(character_name, lang, lua_socket)


def list_voices(lang=None):
    """List available voices."""
    return get_provider().list_voices(lang)


def get_voice(name, lang=None):
    """Get a specific voice by name."""
    return get_provider().get_voice(name, lang)


def refresh_voices(provider_name=None):
    """
    Refresh voice cache for specified provider(s).

    Args:
        provider_name: 'inworld', 'elevenlabs', or None for all cached providers

    Raises:
        Exception: If refresh fails (propagates provider-specific error)
    """
    global _providers

    if provider_name:
        # Refresh specific provider
        if provider_name in _providers:
            print(f"[TTS] Refreshing voice cache for {provider_name}")
            _providers[provider_name].get_voice_cache().refresh()
    else:
        # Refresh all cached providers
        for name, provider in _providers.items():
            print(f"[TTS] Refreshing voice cache for {name}")
            provider.get_voice_cache().refresh()


def clear_provider_cache(provider_name=None):
    """
    Clear cached provider instance(s), forcing re-initialization on next use.

    Args:
        provider_name: 'inworld', 'elevenlabs', 'pocket', or None for all providers
    """
    global _providers

    if provider_name:
        if provider_name in _providers:
            print(f"[TTS] Clearing cached provider: {provider_name}")
            del _providers[provider_name]
        # Also clear provider-specific module caches
        if provider_name == 'inworld':
            from .inworld import clear_voice_cache
            clear_voice_cache()
        elif provider_name == 'elevenlabs':
            from .elevenlabs import clear_voice_cache
            clear_voice_cache()
        elif provider_name == 'pocket' or provider_name == 'pocket_onnx':
            from .pocket_onnx import clear_voice_cache
            clear_voice_cache()
        elif provider_name == 'omnivoice':
            from .omnivoice import clear_voice_cache
            clear_voice_cache()
        elif provider_name == 'universal':
            from .universal import clear_voice_cache
            clear_voice_cache()
        elif provider_name == 'omnivoice_cpp':
            provider_module = importlib.import_module('.omnivoice_cpp', __name__)
            provider_module.clear_voice_cache()
    else:
        print("[TTS] Clearing all cached providers")
        _providers.clear()
        # Clear all provider module caches
        try:
            from .inworld import clear_voice_cache
            clear_voice_cache()
        except ImportError:
            pass
        try:
            from .elevenlabs import clear_voice_cache
            clear_voice_cache()
        except ImportError:
            pass
        try:
            from .pocket_onnx import clear_voice_cache
            clear_voice_cache()
        except ImportError:
            pass
        try:
            from .omnivoice import clear_voice_cache
            clear_voice_cache()
        except ImportError:
            pass
        try:
            from .universal import clear_voice_cache
            clear_voice_cache()
        except ImportError:
            pass
        try:
            provider_module = importlib.import_module('.omnivoice_cpp', __name__)
            provider_module.clear_voice_cache()
        except ImportError:
            pass


def is_available() -> bool:
    """Check if TTS is properly configured."""
    settings = load_settings()
    tts_settings = settings.get('tts', {})
    provider = tts_settings.get('provider', 'inworld')

    if provider == 'none':
        return True  # Always available - no external dependencies
    elif provider == 'pocket' or provider == 'pocket_onnx':
        return True  # Local ONNX inference - always available
    elif provider == 'inworld':
        inworld = tts_settings.get('inworld', {})
        return bool(inworld.get('api_key'))
    elif provider == 'elevenlabs':
        elevenlabs = tts_settings.get('elevenlabs', {})
        return bool(elevenlabs.get('api_key'))
    elif provider == 'omnivoice':
        return True  # Local GPU inference - always available
    elif provider == 'universal':
        connection = settings.get('speech_server', {})
        return bool((connection.get('api_url') or '').strip())
    elif provider == 'omnivoice_cpp':
        try:
            engine_module = importlib.import_module('services.omnivoice_cpp_engine')
            return engine_module.is_available()
        except Exception:
            return False

    return False


def get_provider_name() -> str:
    """Get current provider name."""
    settings = load_settings()
    return settings.get('tts', {}).get('provider', 'inworld')


def synthesize_to_bytes(text, character_name, lang=None):
    """Synthesize text to raw PCM audio bytes."""
    text = _apply_pronunciation(text)
    provider = get_provider()
    # get_or_create_voice now raises specific exceptions on failure
    voice = provider.get_or_create_voice(character_name, lang)

    voice_id = voice.get('voiceId') or voice.get('voice_id')
    if not voice_id:
        raise Exception(f"Voice '{character_name}' has no voice ID. Try refreshing the voice cache.")

    sample_rate = provider.get_sample_rate()
    pcm_chunks = []
    total_word_timestamps = [0]  # Use list for mutable access in callback

    def on_chunk(pcm_bytes, word_timing):
        if pcm_bytes:
            pcm_chunks.append(pcm_bytes)
        if word_timing:
            words = word_timing.get("words", [])
            total_word_timestamps[0] += len(words)

    def synthesize_once(current_voice_id):
        # Use end marker trimmer for clean audio cutoffs
        if END_TRIMMER_AVAILABLE:
            padded_text = pad_text_with_end_marker(text)
            trimmer = EndMarkerTrimmer(
                original_text=text,
                on_chunk=on_chunk,
                sample_rate=sample_rate,
                bytes_per_sample=2  # 16-bit PCM
            )
            ok = provider.synthesize_stream(padded_text, current_voice_id, trimmer.process_chunk)
            trimmer.flush()
            return ok
        return provider.synthesize_stream(text, current_voice_id, on_chunk)

    success = synthesize_once(voice_id)
    if (
        not success
        and not pcm_chunks
        and provider.should_reclone_after_synthesis_failure(voice_id)
    ):
        print(f"[TTS] Cached voice is stale; recloning {character_name} and retrying synthesis")
        provider.invalidate_cached_voice(character_name, lang, voice_id)
        voice = provider.get_or_create_voice(character_name, lang)
        voice_id = voice.get('voiceId') or voice.get('voice_id')
        if not voice_id:
            raise Exception(f"Voice '{character_name}' has no voice ID after recloning.")
        success = synthesize_once(voice_id)

    if not success:
        raise Exception("TTS synthesis failed.")
    if not pcm_chunks:
        raise Exception("No audio data received.")

    provider_name = get_provider_name()
    print(f"[{provider_name.title()}] Word timestamps received: {total_word_timestamps[0]}")

    audio_data = b''.join(pcm_chunks)

    # Save debug audio in dev mode
    if is_dev_mode():
        try:
            import wave
            debug_path = os.path.join(os.path.dirname(__file__), '..', '..', 'debug_tts_output.wav')
            with wave.open(debug_path, 'wb') as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)  # 16-bit
                wf.setframerate(sample_rate)
                wf.writeframes(audio_data)
            print(f"[TTS] Debug audio saved: {debug_path}")
        except Exception as e:
            print(f"[TTS] Failed to save debug audio: {e}")

    return audio_data, sample_rate
