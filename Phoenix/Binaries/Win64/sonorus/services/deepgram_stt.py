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

# Spell names for keyword boosting (tricky pronunciations)
# These help Deepgram recognize Harry Potter spell incantations
SPELL_KEYTERMS = [
    # Two-word spells (most likely to be misheard)
    "Avada Kedavra",
    "Wingardium Leviosa",
    "Arresto Momentum",
    "Petrificus Totalus",
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
    "Transformation",
    "Disillusionment",
]


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
        from deepgram import DeepgramClient

        # Load settings fresh
        settings = load_settings()
        dg_settings = settings.get('stt', {}).get('deepgram', {})

        # Get API key
        api_key = dg_settings.get('api_key')
        if not api_key:
            raise ValueError("Deepgram API key not configured")

        # Create fresh client (v5 uses explicit api_key parameter)
        client = DeepgramClient(api_key=api_key)

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

        # Determine model and keyword parameter
        model = dg_settings.get('model', 'nova-3')
        is_nova3 = 'nova-3' in model.lower()

        # Build transcription parameters
        transcribe_params = {
            'request': wav_data,
            'model': model,
            'language': dg_settings.get('language', 'en-US'),
            'mip_opt_out': mip_opt_out,
            'smart_format': True,  # Intelligent formatting with punctuation/capitalization
            'filler_words': True,  # Include disfluencies like "uh", "um"
        }

        # Add spell keywords based on model
        # Nova-3 uses 'keyterm', Nova-2 uses 'keywords' with intensifiers
        if is_nova3:
            transcribe_params['keyterm'] = SPELL_KEYTERMS
        else:
            # Nova-2: keywords require intensifier (e.g., "spell:2")
            transcribe_params['keywords'] = [f"{spell}:2" for spell in SPELL_KEYTERMS]

        # v5 API: parameters passed directly instead of PrerecordedOptions
        response = client.listen.v1.media.transcribe_file(**transcribe_params)

        # Extract result
        result = response.results.channels[0].alternatives[0]
        text = result.transcript.strip()
        confidence = result.confidence if hasattr(result, 'confidence') else 1.0

        print(f"[STT/Deepgram] Transcribed: \"{text}\" (conf: {confidence:.2f})")

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
