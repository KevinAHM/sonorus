"""Shared HTTP client and capability catalog for Universal Speech Server."""

from __future__ import annotations

import copy
import math
import re
from typing import Any, Optional

from services.speech_server.client import (
    GAME_LANGUAGE_CODES,
    MASKED_KEY,
    PROTOCOL_VERSION,
    SpeechServerClient,
    SpeechServerError,
    language_code,
    normalize_url,
    resolve_draft_key,
)

UniversalAPIError = SpeechServerError
ALIGNMENT_VRAM_OVERHEAD_BYTES = 500 * 1024 * 1024
UPSCALER_VRAM_OVERHEAD_BYTES = 500 * 1024 * 1024
_CONTROL_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_PARALINGUISTIC_TAG_RE = re.compile(r"^\[[a-z0-9][a-z0-9 _-]{0,62}\]$")

MODEL_CATALOG = {
    "omnivoice": {
        "name": "OmniVoice",
        "rank": 100,
        "recommended": True,
        "recommendation": "Recommended for expressive multilingual character voices.",
        "description": "Separate first-sentence and later-sentence diffusion tuning.",
        "curatedDefaults": {
            "numSteps": 32,
            "firstSegmentSteps": 24,
            "guidanceScale": 2.0,
        },
        "clientControls": {},
        "expressionControl": "guidanceScale",
    },
    "chatterbox": {
        "name": "Chatterbox",
        "rank": 50,
        "recommended": False,
        "recommendation": "Alternative English and multilingual voice-cloning backend.",
        "description": "One step count applies to every sentence.",
        "curatedDefaults": {},
        "clientControls": {},
        "expressionControl": None,
    },
}

LEGACY_CONTROLS = {
    "omnivoice": [
        {"id": "numSteps", "type": "integer", "minimum": 8, "maximum": 64, "step": 4, "default": 32},
        {"id": "firstSegmentSteps", "type": "integer", "minimum": 8, "maximum": 64, "step": 4, "default": 32},
        {"id": "guidanceScale", "type": "number", "minimum": 0.0, "maximum": 10.0, "step": 0.1, "default": 2.0},
    ],
    "chatterbox": [
        {"id": "numSteps", "type": "integer", "minimum": 1, "maximum": 30, "step": 1, "default": 6},
        {"id": "guidanceScale", "type": "number", "minimum": 0.0, "maximum": 2.0, "step": 0.05, "default": 0.5},
        {"id": "exaggeration", "type": "number", "minimum": 0.0, "maximum": 2.0, "step": 0.05, "default": 0.5},
    ],
}


def _control_label(model_id: str, control_id: str) -> str:
    if control_id == "numSteps":
        return "Steps"
    if control_id == "firstSegmentSteps":
        return "First sentence steps"
    if control_id == "guidanceScale":
        return "CFG Weight" if model_id == "chatterbox" else "CFG scale"
    if control_id == "exaggeration":
        return "Exaggeration"
    return control_id


def _normalize_control(model_id: str, control: dict) -> dict:
    try:
        control_id = str(control["id"])
        control_type = str(control["type"])
        minimum = float(control["minimum"])
        maximum = float(control["maximum"])
        step = float(control["step"])
        default = float(control["default"])
    except (KeyError, TypeError, ValueError) as exc:
        raise UniversalAPIError("malformed_response", "A model control schema is invalid.") from exc
    if (
        not _CONTROL_ID_RE.fullmatch(control_id)
        or control_type not in {"integer", "number"}
        or not all(math.isfinite(value) for value in (minimum, maximum, step, default))
        or minimum > maximum
        or step <= 0
        or not minimum <= default <= maximum
    ):
        raise UniversalAPIError("malformed_response", "A model control schema is invalid.")
    if control_type == "integer" and any(
        not value.is_integer() for value in (minimum, maximum, step, default)
    ):
        raise UniversalAPIError("malformed_response", "An integer model control contains non-integer values.")
    increments = (default - minimum) / step
    if not math.isclose(increments, round(increments), abs_tol=1e-7):
        raise UniversalAPIError(
            "malformed_response", "A model control default does not match its step."
        )
    cast = int if control_type == "integer" else float
    return {
        "id": control_id,
        "type": control_type,
        "minimum": cast(minimum),
        "maximum": cast(maximum),
        "step": cast(step),
        "default": cast(default),
        "label": _control_label(model_id, control_id),
    }


def _normalize_paralinguistic_tags(value: Any, capability_version: int) -> list[dict]:
    if capability_version < 8:
        return []
    if not isinstance(value, list):
        raise UniversalAPIError(
            "malformed_response", "A model paralinguistic-tag schema is invalid."
        )
    normalized = []
    accepted = set()
    for item in value:
        if not isinstance(item, dict):
            raise UniversalAPIError(
                "malformed_response", "A model paralinguistic-tag schema is invalid."
            )
        token = item.get("token")
        aliases = item.get("aliases", [])
        description = item.get("description")
        if (
            not isinstance(token, str)
            or not _PARALINGUISTIC_TAG_RE.fullmatch(token)
            or not isinstance(description, str)
            or not description.strip()
            or len(description) > 256
            or not isinstance(aliases, list)
            or any(
                not isinstance(alias, str)
                or not _PARALINGUISTIC_TAG_RE.fullmatch(alias)
                for alias in aliases
            )
        ):
            raise UniversalAPIError(
                "malformed_response", "A model paralinguistic-tag schema is invalid."
            )
        values = [token, *aliases]
        if len(set(values)) != len(values) or accepted.intersection(values):
            raise UniversalAPIError(
                "malformed_response", "A model repeats a paralinguistic tag."
            )
        accepted.update(values)
        normalized.append(
            {
                "token": token,
                "aliases": list(aliases),
                "description": description.strip(),
            }
        )
    return normalized


def select_tts_model(capabilities: dict, requested_model_id: str | None) -> dict | None:
    """Resolve the one globally selected Universal TTS model."""
    models = capabilities.get("compatibleModels", [])
    by_id = {model.get("id"): model for model in models if isinstance(model, dict)}
    if isinstance(requested_model_id, str) and requested_model_id in by_id:
        return by_id[requested_model_id]
    return by_id.get(capabilities.get("recommendedModelId"))


def model_profile(model: dict, saved_profile: Optional[dict] = None, upscaler: Optional[dict] = None) -> dict:
    """Apply saved -> curated -> server precedence for advertised controls."""
    saved_profile = saved_profile if isinstance(saved_profile, dict) else {}
    saved_options = saved_profile.get("options", {})
    saved_options = saved_options if isinstance(saved_options, dict) else {}
    catalog = MODEL_CATALOG.get(model.get("id"), {})
    curated = catalog.get("curatedDefaults", {})
    options = {}
    for control in model.get("controls", []):
        control_id = control.get("id")
        candidates = []
        if control_id in saved_options:
            candidates.append(saved_options[control_id])
        if control_id in curated:
            candidates.append(curated[control_id])
        candidates.append(control.get("default"))
        for value in candidates:
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                continue
            if not math.isfinite(float(value)):
                continue
            if control.get("type") == "integer" and not float(value).is_integer():
                continue
            minimum = float(control.get("minimum"))
            maximum = float(control.get("maximum"))
            step = float(control.get("step"))
            increments = (float(value) - minimum) / step
            if minimum <= float(value) <= maximum and math.isclose(
                increments, round(increments), abs_tol=1e-7
            ):
                options[control_id] = int(value) if control.get("type") == "integer" else float(value)
                break

    eligible = bool(
        upscaler
        and int(model.get("sampleRate") or 0) < 44100
        and int(upscaler.get("sampleRate") or 0) >= 44100
    )
    profile = {"options": options, "upscale": False}
    if eligible:
        saved_upscale = saved_profile.get("upscale", True)
        profile["upscale"] = saved_upscale if isinstance(saved_upscale, bool) else True
    batching_eligible = bool(
        model.get("segmentation") and model.get("alignmentCompatible")
    )
    saved_batching = saved_profile.get("adaptive_batching", True)
    profile["adaptive_batching"] = (
        saved_batching if isinstance(saved_batching, bool) else True
    ) if batching_eligible else False
    return profile


def compatible_model(model: dict, game_language: str) -> bool:
    if model.get("task", "tts") != "tts" or not model.get("cloning", False):
        return False
    wanted = GAME_LANGUAGE_CODES.get(game_language, str(game_language or "").split("_", 1)[0].lower())
    languages = {str(value).lower().replace("_", "-").split("-", 1)[0] for value in model.get("languages", [])}
    # Unknown language support is not universal support.  The configuration UI
    # must only offer models that explicitly advertise the selected language
    # (or the protocol's wildcard).
    return bool(languages) and ("*" in languages or wanted in languages)


def _optional_nonnegative_int(value: Any, field: str) -> Optional[int]:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or value < 0
        or not float(value).is_integer()
    ):
        raise UniversalAPIError("malformed_response", f"Speech server {field} is invalid.")
    return int(value)


def _normalize_requirements(value: Any) -> dict:
    if not isinstance(value, dict):
        return {}
    result = {
        "componentBytes": _optional_nonnegative_int(
            value.get("componentBytes"), "component byte estimate"
        )
    }
    for kind in ("ram", "vram"):
        raw = value.get(kind)
        if not isinstance(raw, dict):
            continue
        result[kind] = {
            "estimatedBytes": _optional_nonnegative_int(
                raw.get("estimatedBytes"), f"{kind.upper()} estimate"
            ),
            "source": str(raw.get("source") or "unavailable"),
            "confidence": str(raw.get("confidence") or "unavailable"),
        }
    return result


def _installation_flags(value: dict, capability_version: int) -> dict:
    available = value.get("available", True)
    if not isinstance(available, bool):
        raise UniversalAPIError("malformed_response", "A component availability flag is invalid.")
    if capability_version < 7:
        return {
            "available": available, "installed": available,
            "installable": False, "registryBundle": None,
        }
    installed = value.get("installed")
    installable = value.get("installable")
    installation = value.get("installation")
    if (
        not isinstance(installed, bool)
        or not isinstance(installable, bool)
        or available != installed
        or not isinstance(installation, dict)
        or installation.get("installed") is not installed
        or installation.get("installable") is not installable
        or not isinstance(installation.get("registryBundle"), (str, type(None)))
        or installable != bool(installation.get("registryBundle"))
    ):
        raise UniversalAPIError("malformed_response", "Component installation metadata is invalid.")
    return {
        "available": available, "installed": installed, "installable": installable,
        "registryBundle": installation.get("registryBundle"),
    }


def _normalize_resources(value: dict) -> dict:
    def counter(raw: Any, field: str) -> Optional[dict]:
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise UniversalAPIError("malformed_response", f"Speech server {field} telemetry is invalid.")
        return {
            key: _optional_nonnegative_int(raw.get(key), f"{field} {key}")
            for key in ("totalBytes", "usedBytes", "freeBytes")
        }

    raw_gpus = value.get("gpus", [])
    raw_loaded = value.get("loadedModelIds", [])
    if not isinstance(raw_gpus, list) or not isinstance(raw_loaded, list):
        raise UniversalAPIError("malformed_response", "Speech server resource telemetry is invalid.")
    gpus = []
    for raw in raw_gpus:
        if not isinstance(raw, dict):
            raise UniversalAPIError("malformed_response", "Speech server GPU telemetry is invalid.")
        gpu = counter(raw, "GPU")
        gpu.update({
            "index": _optional_nonnegative_int(raw.get("index"), "GPU index"),
            "name": str(raw.get("name") or "NVIDIA GPU"),
        })
        gpus.append(gpu)
    if any(not isinstance(model_id, str) for model_id in raw_loaded):
        raise UniversalAPIError("malformed_response", "Speech server loaded model IDs are invalid.")
    if not isinstance(value.get("upscalerLoaded", False), bool):
        raise UniversalAPIError(
            "malformed_response", "Speech server upscaler load state is invalid."
        )
    if not isinstance(value.get("alignerLoaded", False), bool):
        raise UniversalAPIError(
            "malformed_response", "Speech server aligner load state is invalid."
        )
    raw_components = value.get("components", [])
    if not isinstance(raw_components, list):
        raise UniversalAPIError(
            "malformed_response", "Speech server component residency is invalid."
        )
    components = []
    for raw in raw_components:
        if (
            not isinstance(raw, dict)
            or raw.get("kind") not in {"model", "upscaler", "aligner"}
            or not isinstance(raw.get("id"), str)
            or not raw["id"]
            or any(not isinstance(raw.get(field), bool) for field in (
                "loaded", "busy", "evictable", "sticky"
            ))
        ):
            raise UniversalAPIError(
                "malformed_response", "Speech server component residency is invalid."
            )
        components.append({
            "kind": raw["kind"],
            "id": raw["id"],
            "loaded": raw["loaded"],
            "busy": raw["busy"],
            "evictable": raw["evictable"],
            "sticky": raw["sticky"],
            "resources": _normalize_requirements(raw.get("resources")),
        })
    return {
        "sampledAt": str(value.get("sampledAt") or ""),
        "ram": counter(value.get("ram"), "RAM"),
        "processRamBytes": _optional_nonnegative_int(
            value.get("processRamBytes"), "process RAM"
        ),
        "gpus": gpus,
        "gpuTelemetry": str(value.get("gpuTelemetry") or "unavailable"),
        "gpuTelemetryError": (
            str(value["gpuTelemetryError"])
            if value.get("gpuTelemetryError") is not None
            else None
        ),
        "loadedModelIds": list(raw_loaded),
        "upscalerLoaded": bool(value.get("upscalerLoaded", False)),
        "alignerLoaded": bool(value.get("alignerLoaded", False)),
        "components": components,
    }


def _requirement_bytes(model: Optional[dict], resource: str) -> Optional[int]:
    if not model:
        return 0
    requirement = model.get("resources", {}).get(resource, {})
    value = requirement.get("estimatedBytes") if isinstance(requirement, dict) else None
    return (
        int(value)
        if isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and value >= 0
        else None
    )


def calculate_fit(
    model: dict,
    upscaler: Optional[dict],
    resources: Optional[dict],
    upscale: bool,
    aligner: Optional[dict] = None,
    adaptive_batching: bool = False,
    resident_limit: int = 1,
) -> dict:
    if not resources:
        return {"status": "unknown", "ratio": None, "estimated": True}
    loaded = set(resources.get("loadedModelIds") or [])
    model_loaded = model.get("id") in loaded
    upscaler_loaded = bool(resources.get("upscalerLoaded"))
    aligner_loaded = bool(resources.get("alignerLoaded"))
    stack_resident = (
        model_loaded
        and (not upscale or upscaler_loaded)
        and (not adaptive_batching or aligner_loaded)
    )
    resident_components = [
        component for component in resources.get("components", [])
        if isinstance(component, dict)
        and component.get("kind") == "model"
        and component.get("loaded")
    ]
    evictions_needed = (
        max(0, len(resident_components) + 1 - max(1, resident_limit))
        if not model_loaded else 0
    )
    victims = [
        component for component in resident_components if component.get("evictable")
    ][:evictions_needed]
    busy = evictions_needed > len(victims)
    ratios = []
    requirements = {}
    gpu_free = [
        gpu.get("freeBytes")
        for gpu in resources.get("gpus", [])
        if isinstance(gpu, dict)
        and isinstance(gpu.get("freeBytes"), (int, float))
        and not isinstance(gpu.get("freeBytes"), bool)
        and math.isfinite(float(gpu["freeBytes"]))
    ]
    for kind, available in (
        ("ram", (resources.get("ram") or {}).get("freeBytes")),
        ("vram", max(gpu_free, default=None)),
    ):
        model_bytes = 0 if model_loaded else _requirement_bytes(model, kind)
        upscaler_bytes = 0
        if upscale and not upscaler_loaded:
            upscaler_bytes = _requirement_bytes(upscaler, kind)
            if kind == "vram":
                upscaler_bytes = max(
                    upscaler_bytes or 0, UPSCALER_VRAM_OVERHEAD_BYTES
                )
        aligner_bytes = 0
        if adaptive_batching and not aligner_loaded:
            aligner_bytes = _requirement_bytes(aligner, kind)
            if kind == "vram":
                aligner_bytes = max(aligner_bytes or 0, ALIGNMENT_VRAM_OVERHEAD_BYTES)
        if model_bytes is None or upscaler_bytes is None or aligner_bytes is None:
            requirements[kind] = None
            continue
        required = model_bytes + upscaler_bytes + aligner_bytes
        reclaimable = sum(
            _requirement_bytes(component, kind) or 0 for component in victims
        )
        requirements[kind] = required
        if available is not None and required > 0:
            projected_available = float(available) + reclaimable
            ratios.append(projected_available / required)
    if busy:
        status, ratio = "busy", None
    elif stack_resident:
        # A resident stack needs no additional allocation. Infinity is a useful
        # internal ratio but is not valid JSON and causes browser response
        # parsing to fail, so represent the unbounded ratio as null.
        status, ratio = "comfortable", None
    elif not ratios:
        status, ratio = "unknown", None
    else:
        ratio = min(ratios)
        status = "comfortable" if ratio >= 1.5 else "tight" if ratio >= 1.0 else "insufficient"
    return {"status": status, "ratio": ratio, "requirements": requirements, "estimated": True}


def _normalize_segmentation(value: Any) -> Optional[dict]:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise UniversalAPIError("malformed_response", "A model segmentation policy is invalid.")
    try:
        result = {
            "estimator": str(value["estimator"]),
            "minSeconds": float(value["minSeconds"]),
            "targetSeconds": float(value["targetSeconds"]),
            "maxSeconds": float(value["maxSeconds"]),
            "fallbackCharactersPerSecond": float(value["fallbackCharactersPerSecond"]),
            "fallbackWordsPerSecond": float(value["fallbackWordsPerSecond"]),
            "safetyFactor": float(value["safetyFactor"]),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise UniversalAPIError("malformed_response", "A model segmentation policy is invalid.") from exc
    numeric = [value for key, value in result.items() if key != "estimator"]
    if (
        result["estimator"] != "reference-rate"
        or not all(math.isfinite(number) and number > 0 for number in numeric)
        or not result["minSeconds"] <= result["targetSeconds"] <= result["maxSeconds"]
        or result["safetyFactor"] < 1
    ):
        raise UniversalAPIError("malformed_response", "A model segmentation policy is invalid.")
    return result


def _normalize_voice_reference(value: Any, capability_version: int, model_id: str) -> dict:
    if capability_version < 4:
        return {
            "transcript": "required" if model_id == "omnivoice" else "unused",
            "preparation": {"mode": "lazy", "inputs": ["audio"], "revision": None},
        }
    if not isinstance(value, dict):
        raise UniversalAPIError(
            "malformed_response", "A model voice-reference policy is invalid."
        )
    transcript = value.get("transcript")
    preparation = value.get("preparation")
    if transcript not in {"required", "optional", "unused"} or not isinstance(
        preparation, dict
    ):
        raise UniversalAPIError(
            "malformed_response", "A model voice-reference policy is invalid."
        )
    mode = preparation.get("mode")
    inputs = preparation.get("inputs")
    revision = preparation.get("revision")
    if (
        mode not in {"persistent", "lazy"}
        or not isinstance(inputs, list)
        or not inputs
        or any(item not in {"audio", "transcript"} for item in inputs)
        or len(inputs) != len(set(inputs))
        or not isinstance(revision, str)
        or not revision.strip()
        or ("transcript" in inputs and transcript == "unused")
    ):
        raise UniversalAPIError(
            "malformed_response", "A model voice-reference policy is invalid."
        )
    return {
        "transcript": transcript,
        "preparation": {
            "mode": mode,
            "inputs": list(inputs),
            "revision": revision.strip(),
        },
    }


def _language_code(game_language: str) -> str:
    return language_code(game_language)


class UniversalSpeechClient(SpeechServerClient):
    def resources(self) -> dict:
        return _normalize_resources(
            self._request("GET", "/v2/resources", timeout=(2.0, 5.0))
        )

    def stack_warmup(
        self, *, tts_model: str | None, asr_model: str | None,
        upscale: bool = False, alignment: bool = False,
    ) -> dict:
        result = super().stack_warmup(
            tts_model=tts_model,
            asr_model=asr_model,
            upscale=upscale,
            alignment=alignment,
        )
        result["resources"] = _normalize_resources(result["resources"])
        return result

    def voices(self) -> dict:
        data = self._request("GET", "/v2/voices")
        voices = data.get("voices")
        if not isinstance(voices, list) or any(
            not isinstance(voice, dict)
            or not isinstance(voice.get("voiceId"), str)
            or not voice["voiceId"]
            for voice in voices
        ):
            raise UniversalAPIError(
                "malformed_response", "The speech server returned an invalid voice list."
            )
        return {"voices": voices}

    def clone_voice(self, payload: dict) -> dict:
        voice = self._request(
            "POST", "/v2/voices", json=payload, timeout=(10.0, 120.0)
        )
        if not isinstance(voice.get("voiceId"), str) or not voice["voiceId"]:
            raise UniversalAPIError(
                "malformed_response", "The speech server returned an invalid cloned voice."
            )
        return voice

    def delete_voice(self, voice_id: str) -> dict:
        from urllib.parse import quote
        return self._request("DELETE", f"/v2/voices/{quote(voice_id, safe='')}")

    def warmup(
        self, model_id: str, *, upscale: bool = False, alignment: bool = False
    ) -> dict:
        from urllib.parse import quote, urlencode
        query = urlencode({
            "upscale": str(bool(upscale)).lower(),
            "alignment": str(bool(alignment)).lower(),
        })
        result = self._request(
            "POST",
            f"/v2/models/{quote(model_id, safe='')}:warmup?{query}",
            timeout=(3.0, 300.0),
        )
        if alignment and result.get("alignerLoaded") is not True:
            raise UniversalAPIError(
                "aligner_warmup_unavailable",
                "The speech server did not load the CTC aligner.",
                status=409,
            )
        return result

    def load_plan(
        self, model_id: str, *, upscale: bool = False,
        adaptive_batching: bool = False,
    ) -> dict:
        from urllib.parse import quote
        result = self._request(
            "POST",
            f"/v2/models/{quote(model_id, safe='')}:plan",
            json={
                "upscale": bool(upscale),
                "adaptiveBatching": bool(adaptive_batching),
            },
            timeout=(2.0, 8.0),
        )
        fit = result.get("fit")
        def actions(field: str) -> list[dict]:
            raw_actions = result.get(field)
            if not isinstance(raw_actions, list):
                raise UniversalAPIError(
                    "malformed_response", "The speech server returned an invalid load plan."
                )
            normalized = []
            for action in raw_actions:
                if (
                    not isinstance(action, dict)
                    or action.get("kind") not in {"model", "upscaler", "aligner"}
                    or not isinstance(action.get("id"), str)
                    or not action["id"]
                ):
                    raise UniversalAPIError(
                        "malformed_response", "The speech server returned an invalid load plan."
                    )
                normalized.append({"kind": action["kind"], "id": action["id"]})
            return normalized

        if (
            result.get("modelId") != model_id
            or not isinstance(result.get("upscale"), bool)
            or result["upscale"] != bool(upscale)
            or not isinstance(result.get("adaptiveBatching"), bool)
            or result["adaptiveBatching"] != bool(adaptive_batching)
            or not isinstance(fit, dict)
            or fit.get("status") not in {
                "comfortable", "tight", "insufficient", "busy", "unknown"
            }
        ):
            raise UniversalAPIError(
                "malformed_response", "The speech server returned an invalid load plan."
            )
        ratio = fit.get("ratio")
        if ratio is not None and (
            not isinstance(ratio, (int, float))
            or isinstance(ratio, bool)
            or not math.isfinite(float(ratio))
            or ratio < 0
        ):
            raise UniversalAPIError(
                "malformed_response", "The speech server returned an invalid load plan."
            )
        raw_requirements = result.get("requirements", {})
        if not isinstance(raw_requirements, dict):
            raise UniversalAPIError(
                "malformed_response", "The speech server returned an invalid load plan."
            )
        requirements = {}
        for kind in ("ram", "vram"):
            raw_requirement = raw_requirements.get(kind)
            if raw_requirement is None:
                continue
            if not isinstance(raw_requirement, dict):
                raise UniversalAPIError(
                    "malformed_response", "The speech server returned an invalid load plan."
                )
            requirements[kind] = {
                field: _optional_nonnegative_int(
                    raw_requirement.get(field), f"load-plan {kind} {field}"
                )
                for field in (
                    "additionalBytes", "reclaimableBytes", "projectedFreeBytes"
                )
            }
        gpu_index = _optional_nonnegative_int(
            result.get("gpuIndex"), "load-plan GPU index"
        )
        return {
            "modelId": model_id,
            "upscale": result.get("upscale") is True,
            "adaptiveBatching": result.get("adaptiveBatching") is True,
            "reuse": actions("reuse"),
            "load": actions("load"),
            "evict": actions("evict"),
            "busy": actions("busy"),
            "fit": {
                "status": fit["status"],
                "ratio": float(ratio) if ratio is not None else None,
                "estimated": fit.get("estimated") is not False,
            },
            "requirements": requirements,
            "gpuIndex": gpu_index,
            "sampledAt": str(result.get("sampledAt") or ""),
        }

    def prepare_voice(self, model_id: str, voice_id: str) -> dict:
        from urllib.parse import quote
        result = self._request(
            "POST",
            f"/v2/models/{quote(model_id, safe='')}/voices/"
            f"{quote(voice_id, safe='')}:prepare",
            timeout=(5.0, 300.0),
        )
        preparation = result.get("preparation")
        if (
            result.get("prepared") is not True
            or result.get("modelId") != model_id
            or result.get("voiceId") != voice_id
            or not isinstance(preparation, dict)
            or not isinstance(preparation.get("revision"), str)
            or not isinstance(preparation.get("inputHashes"), dict)
        ):
            raise UniversalAPIError(
                "malformed_response",
                "The speech server returned an invalid voice-preparation result.",
            )
        return result

    def enriched_capabilities(self, game_language: str, resources: Optional[dict] = None, force: bool = False) -> dict:
        raw = self.capabilities(force=force)
        try:
            raw_capability_version = raw.get("capabilitiesVersion", 1)
            if (
                not isinstance(raw_capability_version, int)
                or isinstance(raw_capability_version, bool)
                or raw_capability_version <= 0
            ):
                raise ValueError("invalid version")
            capability_version = raw_capability_version
        except (TypeError, ValueError) as exc:
            raise UniversalAPIError("malformed_response", "Capabilities contained an invalid version.") from exc
        raw_resident_limit = raw.get("residentLimit", 1)
        if (
            not isinstance(raw_resident_limit, int)
            or isinstance(raw_resident_limit, bool)
            or raw_resident_limit <= 0
        ):
            raise UniversalAPIError(
                "malformed_response", "Capabilities contained an invalid resident limit."
            )
        resident_limit = raw_resident_limit
        raw_upscaler_value = raw.get("upscaler")
        if raw_upscaler_value is not None and not isinstance(raw_upscaler_value, dict):
            raise UniversalAPIError(
                "malformed_response", "Capabilities contained an invalid upscaler."
            )
        raw_upscaler = raw_upscaler_value
        upscaler = None
        if raw_upscaler:
            try:
                install = _installation_flags(raw_upscaler, capability_version)
                if not isinstance(raw_upscaler.get("id"), str) or not isinstance(
                    raw_upscaler.get("sampleRate"), int
                ) or isinstance(raw_upscaler.get("sampleRate"), bool):
                    raise ValueError("invalid upscaler fields")
                upscaler = {
                    "id": str(raw_upscaler.get("id") or ""),
                    "backend": str(raw_upscaler.get("backend") or ""),
                    "sampleRate": raw_upscaler.get("sampleRate"),
                    **install,
                    "resources": _normalize_requirements(raw_upscaler.get("resources")),
                }
            except (TypeError, ValueError) as exc:
                raise UniversalAPIError("malformed_response", "Capabilities contained an invalid upscaler.") from exc
            if not upscaler["id"] or upscaler["sampleRate"] <= 0:
                raise UniversalAPIError(
                    "malformed_response", "Capabilities contained an invalid upscaler."
                )
            if capability_version < 7 and not upscaler["available"]:
                upscaler = None
        raw_alignment_value = raw.get("alignment")
        if raw_alignment_value is not None and not isinstance(raw_alignment_value, dict):
            raise UniversalAPIError(
                "malformed_response", "Capabilities contained an invalid aligner."
            )
        raw_alignment = raw_alignment_value
        alignment = None
        if raw_alignment and capability_version >= 3:
            install = _installation_flags(raw_alignment, capability_version)
            languages = raw_alignment.get("languages", [])
            timing_modes = raw_alignment.get("timingModes", [])
            if (
                not isinstance(raw_alignment.get("id"), str)
                or not raw_alignment["id"].strip()
                or not isinstance(languages, list)
                or any(not isinstance(value, str) or not value.strip() for value in languages)
                or not isinstance(timing_modes, list)
                or any(mode not in {"auto", "word"} for mode in timing_modes)
            ):
                raise UniversalAPIError(
                    "malformed_response", "Capabilities contained an invalid aligner."
                )
            alignment = {
                "id": raw_alignment["id"],
                "backend": str(raw_alignment.get("backend") or "unknown"),
                **install,
                "languages": [value.strip() for value in languages],
                "timingModes": list(timing_modes),
                "resources": _normalize_requirements(raw_alignment.get("resources")),
            }
        models = []
        seen_models = set()
        for raw_model in raw.get("models", []):
            if (
                not isinstance(raw_model, dict)
                or not isinstance(raw_model.get("id"), str)
                or not raw_model["id"].strip()
                or not isinstance(raw_model.get("cloning", False), bool)
                or not isinstance(raw_model.get("sampleRate"), int)
                or isinstance(raw_model.get("sampleRate"), bool)
            ):
                raise UniversalAPIError(
                    "malformed_response", "Capabilities contained an invalid model."
                )
            install = _installation_flags(raw_model, capability_version)
            model_id = str(raw_model["id"])
            if model_id in seen_models:
                raise UniversalAPIError(
                    "malformed_response", "Capabilities contained duplicate model IDs."
                )
            seen_models.add(model_id)
            if str(raw_model.get("task") or "tts") != "tts":
                continue
            catalog = MODEL_CATALOG.get(model_id, {})
            controls = (
                raw_model.get("controls", [])
                if capability_version >= 2
                else LEGACY_CONTROLS.get(model_id, [])
            )
            if not isinstance(controls, list):
                raise UniversalAPIError(
                    "malformed_response", "A model control schema is invalid."
                )
            normalized_controls = []
            seen_controls = set()
            for control in controls:
                if not isinstance(control, dict) or not control.get("id"):
                    raise UniversalAPIError("malformed_response", "A model control schema is invalid.")
                normalized = _normalize_control(model_id, control)
                if normalized["id"] in seen_controls:
                    raise UniversalAPIError(
                        "malformed_response", "A model repeats a control ID."
                    )
                seen_controls.add(normalized["id"])
                normalized_controls.append(normalized)
            try:
                sample_rate = raw_model.get("sampleRate")
                raw_languages = raw_model.get("languages", [])
                raw_audio_tags = raw_model.get("audioTags", [])
                if not isinstance(raw_languages, list) or not raw_languages or any(
                    not isinstance(value, str) or not value.strip()
                    for value in raw_languages
                ):
                    raise ValueError("invalid languages")
                if not isinstance(raw_audio_tags, list) or any(
                    not isinstance(value, str) or not value.strip()
                    for value in raw_audio_tags
                ):
                    raise ValueError("invalid audio tags")
                languages = [value.strip() for value in raw_languages]
            except (TypeError, ValueError) as exc:
                raise UniversalAPIError("malformed_response", "Capabilities contained an invalid model.") from exc
            if sample_rate <= 0:
                raise UniversalAPIError(
                    "malformed_response", "Capabilities contained an invalid model."
                )
            model = {
                "id": model_id,
                "name": catalog.get("name", model_id),
                "backend": str(raw_model.get("backend") or "unknown"),
                "task": str(raw_model.get("task") or "tts"),
                "sampleRate": sample_rate,
                **install,
                "cloning": bool(raw_model.get("cloning", False)),
                "languages": languages,
                "controls": normalized_controls,
                "resources": _normalize_requirements(raw_model.get("resources")),
                "rank": int(catalog.get("rank", 0)),
                "recommended": bool(catalog.get("recommended", False)),
                "recommendation": catalog.get("recommendation"),
                "description": catalog.get("description") or "Server-provided voice-cloning model.",
                "clientControls": copy.deepcopy(catalog.get("clientControls", {})),
                "expressionControl": catalog.get("expressionControl"),
                "segmentation": (
                    _normalize_segmentation(raw_model.get("segmentation"))
                    if capability_version >= 3
                    else None
                ),
                "textProfile": str(raw_model.get("textProfile") or "plain"),
                "audioTags": [value.strip() for value in raw_audio_tags],
                "paralinguisticTags": _normalize_paralinguistic_tags(
                    raw_model.get("paralinguisticTags", []), capability_version
                ),
                "voiceReference": _normalize_voice_reference(
                    raw_model.get("voiceReference"), capability_version, model_id
                ),
            }
            model["compatible"] = bool(
                (model["installed"] or model["installable"])
                and compatible_model(model, game_language)
            )
            aligner_languages = {
                value.lower().replace("_", "-").split("-", 1)[0]
                for value in (alignment or {}).get("languages", [])
            }
            model["alignmentCompatible"] = bool(
                alignment
                and (alignment.get("installed") or alignment.get("installable"))
                and "auto" in alignment.get("timingModes", [])
                and (_language_code(game_language) in aligner_languages or "*" in aligner_languages)
            )
            model["upscaleEligible"] = bool(
                upscaler and (upscaler.get("installed") or upscaler.get("installable"))
                and model["sampleRate"] < 44100 <= upscaler["sampleRate"]
            )
            model["loaded"] = model_id in set((resources or {}).get("loadedModelIds") or [])
            default_profile = model_profile(model, None, upscaler)
            model["defaults"] = default_profile
            model["fit"] = calculate_fit(
                model,
                upscaler,
                resources,
                default_profile["upscale"],
                alignment,
                default_profile.get("adaptive_batching", False),
                resident_limit,
            )
            models.append(model)
        compatible = [model for model in models if model["compatible"]]
        viable = [model for model in compatible if model["fit"]["status"] != "insufficient"]
        ranked = viable or compatible
        ranked.sort(key=lambda model: (-model["rank"], model["name"].lower()))
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilitiesVersion": capability_version,
            "registryRevision": str(raw.get("registryRevision") or ""),
            "residentLimit": resident_limit,
            "models": models,
            "compatibleModels": compatible,
            "recommendedModelId": ranked[0]["id"] if ranked else None,
            "upscaler": upscaler,
            "alignment": alignment,
            "resourcesAvailable": capability_version >= 2,
            "loadPlanning": bool(
                capability_version >= 5 and raw.get("loadPlanning") is True
            ),
        }
