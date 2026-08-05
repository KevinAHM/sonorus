"""Sonorus enrichment and compatibility filtering for remote speech models."""

from __future__ import annotations

import math
from typing import Optional

from .client import SpeechServerError, language_code

ASR_MODEL_CATALOG = {
    "parakeet-tdt-0.6b-v3": {
        "name": "Parakeet TDT 0.6B v3",
        "description": "Fast multilingual speech recognition with automatic language detection.",
        "rank": 100,
        "recommended": True,
    }
}


def _nonnegative_int(value, field: str):
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or value < 0
        or not float(value).is_integer()
    ):
        raise SpeechServerError("malformed_response", f"ASR {field} is invalid.")
    return int(value)


def _requirements(value) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SpeechServerError("malformed_response", "ASR resource metadata is invalid.")
    result = {
        "componentBytes": _nonnegative_int(value.get("componentBytes"), "component bytes")
    }
    for kind in ("ram", "vram"):
        item = value.get(kind)
        if item is None:
            continue
        if not isinstance(item, dict):
            raise SpeechServerError("malformed_response", "ASR resource metadata is invalid.")
        result[kind] = {
            "estimatedBytes": _nonnegative_int(
                item.get("estimatedBytes"), f"{kind.upper()} estimate"
            ),
            "source": str(item.get("source") or "unavailable"),
            "confidence": str(item.get("confidence") or "unavailable"),
        }
    return result


def _positive_int(value, field: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or value <= 0
        or not float(value).is_integer()
    ):
        raise SpeechServerError("malformed_response", f"ASR {field} is invalid.")
    return int(value)


def _transcription(value) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("audio"), dict):
        raise SpeechServerError("malformed_response", "ASR transcription metadata is invalid.")
    audio = value["audio"]
    encodings = audio.get("encodings")
    sample_rates = audio.get("sampleRates")
    channels = audio.get("channels")
    timestamps = value.get("timestamps")
    detection = value.get("automaticLanguageDetection")
    bias = value.get("biasTerms")
    if (
        not isinstance(encodings, list) or not encodings
        or any(not isinstance(item, str) or not item for item in encodings)
        or not isinstance(sample_rates, list) or not sample_rates
        or not isinstance(channels, list) or not channels
        or not isinstance(timestamps, list) or "none" not in timestamps
        or any(
            not isinstance(item, str) or item not in {"none", "segment", "word"}
            for item in timestamps
        )
        or not isinstance(detection, bool)
        or not isinstance(bias, dict)
        or not isinstance(bias.get("supported"), bool)
        or len(set(encodings)) != len(encodings)
        or len(set(timestamps)) != len(timestamps)
    ):
        raise SpeechServerError("malformed_response", "ASR transcription metadata is invalid.")
    normalized_bias = {"supported": bias["supported"], "maxCount": 0, "maxLength": 0}
    if bias["supported"]:
        normalized_bias.update({
            "maxCount": _positive_int(bias.get("maxCount"), "bias-term count"),
            "maxLength": _positive_int(bias.get("maxLength"), "bias-term length"),
        })
    max_seconds = audio.get("maxSeconds")
    if (
        isinstance(max_seconds, bool)
        or not isinstance(max_seconds, (int, float))
        or not math.isfinite(float(max_seconds))
        or max_seconds <= 0
    ):
        raise SpeechServerError("malformed_response", "ASR maximum duration is invalid.")
    normalized_sample_rates = [_positive_int(rate, "sample rate") for rate in sample_rates]
    normalized_channels = [_positive_int(channel, "channel count") for channel in channels]
    if (
        len(set(normalized_sample_rates)) != len(normalized_sample_rates)
        or len(set(normalized_channels)) != len(normalized_channels)
    ):
        raise SpeechServerError("malformed_response", "ASR audio metadata contains duplicates.")
    return {
        "audio": {
            "encodings": list(encodings),
            "sampleRates": normalized_sample_rates,
            "channels": normalized_channels,
            "maxSeconds": float(max_seconds),
        },
        "automaticLanguageDetection": detection,
        "timestamps": list(timestamps),
        "biasTerms": normalized_bias,
    }


def enrich_asr_capabilities(
    raw: dict, game_language: str, resources: Optional[dict] = None
) -> dict:
    version = raw.get("capabilitiesVersion", 1)
    if not isinstance(version, int) or isinstance(version, bool):
        raise SpeechServerError("malformed_response", "Capabilities contained an invalid version.")
    if resources is not None and not isinstance(resources, dict):
        raise SpeechServerError("malformed_response", "Resource telemetry is invalid.")
    raw_loaded = (resources or {}).get("loadedModelIds") or []
    if not isinstance(raw_loaded, list) or any(
        not isinstance(model_id, str) for model_id in raw_loaded
    ):
        raise SpeechServerError("malformed_response", "Loaded model metadata is invalid.")
    loaded = set(raw_loaded)
    wanted = language_code(game_language)
    models = []
    seen_model_ids = set()
    if version >= 6:
        for item in raw.get("models", []):
            if not isinstance(item, dict) or item.get("task") != "asr":
                continue
            model_id = item.get("id")
            transcription = item.get("transcription")
            languages = item.get("languages")
            available = item.get("available", True)
            installed = item.get("installed", available)
            installable = item.get("installable", False)
            installation = item.get("installation")
            if (
                not isinstance(model_id, str) or not model_id.strip()
                or not isinstance(transcription, dict)
                or not isinstance(languages, list) or not languages
                or any(not isinstance(language, str) or not language.strip() for language in languages)
                or not isinstance(available, bool)
                or not isinstance(installed, bool)
                or not isinstance(installable, bool)
                or available != installed
                or (
                    version >= 7
                    and (
                        not isinstance(installation, dict)
                        or installation.get("installed") is not installed
                        or installation.get("installable") is not installable
                        or not isinstance(installation.get("registryBundle"), (str, type(None)))
                        or installable != bool(installation.get("registryBundle"))
                    )
                )
            ):
                raise SpeechServerError("malformed_response", "Capabilities contained an invalid ASR model.")
            if model_id in seen_model_ids:
                raise SpeechServerError(
                    "malformed_response", "Capabilities contained duplicate ASR model IDs."
                )
            seen_model_ids.add(model_id)
            normalized_languages = [language.strip() for language in languages]
            language_ids = {
                language.lower().replace("_", "-").split("-", 1)[0]
                for language in normalized_languages
            }
            compatible = wanted in language_ids or "*" in language_ids
            if not compatible or not (installed or installable):
                continue
            normalized_transcription = _transcription(transcription)
            if (
                "pcm_s16le" not in normalized_transcription["audio"]["encodings"]
                or 1 not in normalized_transcription["audio"]["channels"]
            ):
                continue
            catalog = ASR_MODEL_CATALOG.get(model_id, {})
            models.append(
                {
                    "id": model_id,
                    "name": catalog.get("name") or model_id,
                    "backend": str(item.get("backend") or "unknown"),
                    "description": catalog.get("description") or "Server-provided speech recognition model.",
                    "languages": normalized_languages,
                    "transcription": normalized_transcription,
                    "resources": _requirements(item.get("resources")),
                    "loaded": model_id in loaded,
                    "available": available,
                    "installed": installed,
                    "installable": installable,
                    "registryBundle": (
                        installation.get("registryBundle")
                        if isinstance(installation, dict) else None
                    ),
                    "recommended": bool(catalog.get("recommended", False)),
                    "rank": int(catalog.get("rank", 0)),
                }
            )
    models.sort(key=lambda model: (-model["rank"], model["name"].lower()))
    recommended = next((model["id"] for model in models if model["recommended"]), None)
    if recommended is None and models:
        recommended = models[0]["id"]
    return {
        "capabilitiesVersion": version,
        "compatibleASRModels": models,
        "recommendedASRModelId": recommended,
        "asrAvailable": version >= 6,
    }
