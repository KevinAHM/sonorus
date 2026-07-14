"""
Deepgram STT provider.
Uses Deepgram's Nova models for high-quality transcription.
Requires deepgram-sdk v5.0.0+
"""
import os
import sys
import io
import wave

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.settings import load_settings

# Keyword boosting terms for Deepgram STT.
# Keep categories separate so name/location additions are explicit and easy to maintain.
_client = None
_client_api_key = None


CHARACTER_KEYTERMS = [
    # Character names commonly mangled by STT
    "Garlick",
    "Professor Garlick",
    "Mirabel Garlick",
    "Ominis",
    "Deek",
    "Garreth",
]


SPELL_KEYTERMS = [
    # Two-word spells (most likely to be misheard)
    "Avada Kedavra",
    "Wingardium Leviosa",
    "Arresto Momentum",
    "Petrificus Totalus",
    "Expecto Patronum",
    "Animagus Form",
    "Bat Bogey",
    "Fiend Fyre",
    # Latin-derived spells
    "Levioso",
    "Accio",
    "Depulso",
    "Descendo",
    "Flipendo",
    "Glacius",
    "Incendio",
    "Confringo",
    "Diffindo",
    "Expelliarmus",
    "Expulso",
    "Crucio",
    "Imperio",
    "Stupefy",
    "Lumos",
    "Nox",
    "Reparo",
    "Revelio",
    "Protego",
    "Confundo",
    "Oppugno",
    "Obliviate",
    "Episkey",
    "Evanesco",
    "Conjuration",
    "Alohomora",
    "Reducio",
    "Reducto",
    "Apparition",
    "Bombarda",
]


LOCATION_KEYTERMS = [
    # Location names
    "Hogsmeade"
]


ALL_KEYTERMS = CHARACTER_KEYTERMS + SPELL_KEYTERMS + LOCATION_KEYTERMS


def _get_client():
    """Get or create a cached DeepgramClient with persistent connections."""
    global _client, _client_api_key
    import httpx
    from deepgram import DeepgramClient

    settings = load_settings()
    api_key = settings.get('stt', {}).get('deepgram', {}).get('api_key')
    if not api_key:
        raise ValueError("Deepgram API key not configured")

    if _client is None or _client_api_key != api_key:
        _client = DeepgramClient(
            api_key=api_key,
            httpx_client=httpx.Client(
                timeout=60,
                follow_redirects=True,
                limits=httpx.Limits(
                    max_connections=10,
                    max_keepalive_connections=5,
                    keepalive_expiry=300,
                ),
            ),
        )
        _client_api_key = api_key
    return _client


def warm_up():
    """Pre-warm the Deepgram client and TLS connection. Non-blocking."""
    import threading

    def _warm():
        try:
            client = _get_client()
            # List models via SDK to warm up its internal httpx connection pool
            client.manage.v1.models.list()
            print("[STT/Deepgram] Connection pre-warmed")
        except Exception as e:
            print(f"[STT/Deepgram] Pre-warm failed (non-fatal): {e}")

    threading.Thread(target=_warm, daemon=True).start()


def transcribe(audio_data: bytes, sample_rate: int = 16000) -> dict:
    """
    Transcribe audio using Deepgram.

    Args:
        audio_data: Raw PCM audio bytes (16-bit mono)
        sample_rate: Audio sample rate

    Returns:
        {"success": bool, "text": str, "confidence": float, "error": str}
    """
    try:
        client = _get_client()
        dg_settings = load_settings().get('stt', {}).get('deepgram', {})

        # Convert PCM to WAV in memory
        wav_buffer = io.BytesIO()
        with wave.open(wav_buffer, 'wb') as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)  # 16-bit
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(audio_data)
        wav_data = wav_buffer.getvalue()

        # mip_opt_out = opt OUT of Model Improvement Program
        # When model_improvement is False in settings, we opt OUT (mip_opt_out=True)
        mip_opt_out = not dg_settings.get('model_improvement', False)

        # Determine model and keyword parameter. Nova-3 is English-only in
        # practice, so fall back for non-English setups instead of returning
        # empty transcripts for languages like Japanese.
        model = dg_settings.get('model', 'nova-3')
        language = dg_settings.get('language', 'en-US')
        if model.lower().startswith('nova-3') and not language.lower().startswith('en'):
            print(f"[STT/Deepgram] {model} does not support language={language}; using nova-2")
            model = 'nova-2'
        is_nova3 = 'nova-3' in model.lower()

        # Build transcription parameters
        transcribe_params = {
            'request': wav_data,
            'model': model,
            'language': language,
            'mip_opt_out': mip_opt_out,
            'smart_format': True,  # Intelligent formatting with punctuation/capitalization
            'filler_words': True,  # Include disfluencies like "uh", "um"
        }

        # Add keyword boosting terms based on model
        # Nova-3 uses 'keyterm', Nova-2 uses 'keywords' with intensifiers
        if is_nova3:
            transcribe_params['keyterm'] = ALL_KEYTERMS
        else:
            # Nova-2: keywords require intensifier (e.g., "spell:2")
            transcribe_params['keywords'] = [f"{term}:2" for term in ALL_KEYTERMS]

        # v5 API: parameters passed directly instead of PrerecordedOptions
        response = client.listen.v1.media.transcribe_file(**transcribe_params)

        # Extract result
        result = response.results.channels[0].alternatives[0]
        text = result.transcript.strip()
        confidence = result.confidence if hasattr(result, 'confidence') else 1.0

        return {
            "success": True,
            "text": text,
            "confidence": confidence,
            "error": None
        }

    except Exception as e:
        print(f"[STT/Deepgram] Error: {e}")
        return {
            "success": False,
            "text": "",
            "confidence": 0.0,
            "error": str(e)
        }
