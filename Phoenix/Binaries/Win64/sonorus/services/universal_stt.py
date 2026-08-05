"""Universal Speech Server ASR provider."""

from __future__ import annotations

from services.speech_server import SpeechServerClient, SpeechServerError, language_code
from services.speech_server.catalog import enrich_asr_capabilities
from services.stt_terms import all_keyterms
from utils.settings import load_settings


def _configuration():
    settings = load_settings()
    if not isinstance(settings, dict):
        raise SpeechServerError("invalid_configuration", "Universal ASR settings are invalid.")
    connection = settings.get("speech_server", {})
    stt = settings.get("stt", {})
    setup = settings.get("setup", {})
    if (
        not isinstance(connection, dict)
        or not isinstance(stt, dict)
        or not isinstance(setup, dict)
    ):
        raise SpeechServerError("invalid_configuration", "Universal ASR settings are invalid.")
    universal = stt.get("universal", {})
    if not isinstance(universal, dict):
        raise SpeechServerError("invalid_configuration", "Universal ASR settings are invalid.")
    model_id = str(universal.get("model") or "")
    client = SpeechServerClient(
        connection.get("api_url") or "http://127.0.0.1:8100",
        connection.get("api_key") or "",
    )
    game_language = setup.get("language", "EN_US")
    if not isinstance(game_language, str) or not game_language.strip():
        raise SpeechServerError("invalid_configuration", "The game language is invalid.")
    return settings, client, model_id, game_language


def _selected_model(client, model_id, game_language, settings=None):
    capabilities = client.capabilities()
    version = capabilities.get("capabilitiesVersion", 1)
    if isinstance(version, bool) or not isinstance(version, int):
        raise SpeechServerError(
            "malformed_response", "The speech server returned an invalid capability version."
        )
    if version < 6:
        raise SpeechServerError(
            "capability_upgrade_required",
            "Universal ASR requires speech-server capabilities version 6.",
            status=409,
        )
    tts = (settings or {}).get("tts", {})
    if (
        isinstance(tts, dict)
        and tts.get("provider") == "universal"
        and capabilities.get("residentLimit", 1) < 2
    ):
        raise SpeechServerError(
            "resident_limit",
            "The speech server must allow two resident models when Universal TTS and ASR are both selected.",
            status=409,
        )
    catalog = enrich_asr_capabilities(capabilities, game_language)
    model = next(
        (item for item in catalog["compatibleASRModels"] if item["id"] == model_id),
        None,
    )
    if model is None:
        raise SpeechServerError("model_not_found", "The selected Universal ASR model is unavailable.", status=409)
    if model.get("installed", model.get("available", True)) is not True:
        raise SpeechServerError(
            "model_not_installed",
            "The selected Universal ASR model must be installed before transcription.",
            status=409,
        )
    return model


def _resample_pcm16_mono(audio_data: bytes, source_rate: int, target_rate: int) -> bytes:
    if (
        isinstance(source_rate, bool) or not isinstance(source_rate, int)
        or isinstance(target_rate, bool) or not isinstance(target_rate, int)
        or source_rate <= 0 or target_rate <= 0
    ):
        raise SpeechServerError("unsupported_audio", "Audio sample rates must be positive.", status=400)
    if not isinstance(audio_data, (bytes, bytearray, memoryview)):
        raise SpeechServerError("unsupported_audio", "Audio must be PCM16 bytes.", status=400)
    pcm_bytes = bytes(audio_data)
    if not pcm_bytes or len(pcm_bytes) % 2:
        raise SpeechServerError(
            "unsupported_audio", "PCM16 audio must contain complete samples.", status=400
        )
    if source_rate == target_rate:
        return pcm_bytes

    import math
    import numpy as np
    from scipy.signal import resample_poly

    samples = np.frombuffer(pcm_bytes, dtype="<i2").astype(np.float32)
    divisor = math.gcd(int(source_rate), int(target_rate))
    converted = resample_poly(
        samples, int(target_rate) // divisor, int(source_rate) // divisor
    )
    return np.clip(np.rint(converted), -32768, 32767).astype("<i2").tobytes()


def _bias_terms(settings: dict, transcription: dict) -> list[str]:
    bias = transcription.get("biasTerms") or {}
    if (
        settings.get("stt", {}).get("voice_spells", True) is not True
        or bias.get("supported") is not True
    ):
        return []
    max_count = max(0, int(bias.get("maxCount") or 0))
    max_length = max(0, int(bias.get("maxLength") or 0))
    if not max_count or not max_length:
        return []
    return [term for term in all_keyterms() if len(term) <= max_length][:max_count]


def transcribe(audio_data: bytes, sample_rate: int = 16000) -> dict:
    try:
        settings, client, model_id, game_language = _configuration()
        model = _selected_model(client, model_id, game_language, settings)
        transcription = model.get("transcription") or {}
        automatic = transcription.get("automaticLanguageDetection") is True
        supported_rates = transcription.get("audio", {}).get("sampleRates") or []
        if not supported_rates:
            raise SpeechServerError(
                "malformed_response", "The selected ASR model has no supported sample rate."
            )
        target_rate = (
            sample_rate if sample_rate in supported_rates
            else 16000 if 16000 in supported_rates
            else supported_rates[0]
        )
        normalized_audio = _resample_pcm16_mono(audio_data, sample_rate, target_rate)
        result = client.transcribe(
            model_id,
            normalized_audio,
            sample_rate=target_rate,
            language="auto" if automatic else language_code(game_language),
            bias_terms=_bias_terms(settings, transcription),
            timestamps="none",
        )
        return {
            "success": True,
            "text": result["text"].strip(),
            "confidence": result.get("confidence"),
            "error": None,
        }
    except SpeechServerError as exc:
        return {"success": False, "text": "", "confidence": None, "error": exc.message}
    except Exception as exc:
        return {"success": False, "text": "", "confidence": None, "error": str(exc)}


def is_available() -> bool:
    try:
        _settings, client, model_id, game_language = _configuration()
        _selected_model(client, model_id, game_language, _settings)
        return True
    except (SpeechServerError, ValueError):
        return False
