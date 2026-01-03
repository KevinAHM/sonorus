"""
Whisper STT provider.
Uses OpenAI's Whisper API or compatible local endpoints for speech recognition.
"""
import os
import sys
import io
import wave

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.settings import load_settings


def transcribe(audio_data: bytes, sample_rate: int = 16000) -> dict:
    """
    Transcribe audio using Whisper.

    Args:
        audio_data: Raw PCM audio bytes (16-bit mono)
        sample_rate: Audio sample rate

    Returns:
        {"success": bool, "text": str, "confidence": float, "error": str}
    """
    try:
        from openai import OpenAI

        # Load settings fresh
        settings = load_settings()
        whisper_settings = settings.get('stt', {}).get('whisper', {})

        # Fall back to LLM API key if STT-specific key not set
        api_key = whisper_settings.get('api_key')
        if not api_key:
            api_key = settings.get('llm', {}).get('api_key')

        if not api_key:
            raise ValueError("Whisper API key not configured")

        # Support custom API URL for local Whisper servers
        api_url = whisper_settings.get('api_url', 'https://api.openai.com/v1')

        # Create fresh client
        client = OpenAI(api_key=api_key, base_url=api_url)

        model = whisper_settings.get('model', 'whisper-1')
        language = whisper_settings.get('language', '')

        # Convert PCM to WAV in memory
        wav_buffer = io.BytesIO()
        with wave.open(wav_buffer, 'wb') as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)  # 16-bit
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(audio_data)
        wav_buffer.seek(0)
        wav_buffer.name = "audio.wav"  # OpenAI needs a filename

        # Build transcription params
        params = {
            "model": model,
            "file": wav_buffer,
        }

        # Only include language if specified (empty = auto-detect)
        if language:
            params["language"] = language

        # Transcribe
        response = client.audio.transcriptions.create(**params)

        text = response.text.strip()
        print(f"[STT/Whisper] Transcribed: \"{text}\"")

        return {
            "success": True,
            "text": text,
            "confidence": 1.0,  # Whisper doesn't provide confidence
            "error": None
        }

    except Exception as e:
        print(f"[STT/Whisper] Error: {e}")
        return {
            "success": False,
            "text": "",
            "confidence": 0.0,
            "error": str(e)
        }
