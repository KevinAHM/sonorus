"""Provider-neutral HTTP client for Universal Speech Server."""

from __future__ import annotations

import base64
import copy
import math
import threading
import uuid
from typing import Any, Dict, Optional
from urllib.parse import quote, urlsplit, urlunsplit

import requests

PROTOCOL_VERSION = "2.0"
MASKED_KEY = "********"

GAME_LANGUAGE_CODES = {
    "EN_US": "en", "EN_GB": "en", "DE_DE": "de", "FR_FR": "fr",
    "ES_ES": "es", "ES_MX": "es", "IT_IT": "it", "PT_BR": "pt",
    "JA_JP": "ja", "KO_KR": "ko", "ZH_CN": "zh", "ZH_TW": "zh",
    "PL_PL": "pl", "RU_RU": "ru", "AR_AE": "ar", "NL_NL": "nl",
    "TR_TR": "tr",
}


class SpeechServerError(RuntimeError):
    def __init__(self, code: str, message: str, *, status: int = 502, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details

    def as_dict(self) -> dict:
        result = {"code": self.code, "message": self.message}
        if self.details is not None:
            result["details"] = self.details
        return result


def normalize_url(value: str) -> str:
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise SpeechServerError("invalid_url", "The speech server URL is invalid.", status=400) from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise SpeechServerError("invalid_url", "Use an HTTP or HTTPS speech server URL.", status=400)
    if parsed.username is not None or parsed.password is not None:
        raise SpeechServerError("invalid_url", "Embedded credentials are not allowed in the speech server URL.", status=400)
    if parsed.query or parsed.fragment:
        raise SpeechServerError("invalid_url", "The speech server URL cannot contain a query or fragment.", status=400)
    try:
        host = "127.0.0.1" if parsed.hostname.lower() == "localhost" else parsed.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = parsed.port
    except ValueError as exc:
        raise SpeechServerError("invalid_url", "The speech server URL has an invalid port.", status=400) from exc
    netloc = f"{host}:{port}" if port is not None else host
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), netloc, path, "", ""))


def resolve_draft_key(value: Any, saved_key: str) -> str:
    draft = str(value or "").strip()
    return str(saved_key or "").strip() if draft == MASKED_KEY else draft


def language_code(game_language: str) -> str:
    return GAME_LANGUAGE_CODES.get(
        game_language,
        str(game_language or "").replace("_", "-").split("-", 1)[0].lower(),
    )


def _nonnegative_integer(value, field: str):
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or value < 0
        or not float(value).is_integer()
    ):
        raise SpeechServerError("malformed_response", f"Speech Server {field} is invalid.")
    return int(value)


def _install_component_path(component: str, model_id: str | None = None) -> tuple[str, str]:
    if component == "model":
        if not isinstance(model_id, str) or not model_id.strip() or len(model_id.strip()) > 128:
            raise SpeechServerError(
                "invalid_install_target", "A model ID is required for model installation.", status=400
            )
        model_id = model_id.strip()
        return f"model:{model_id}", f"/v2/models/{quote(model_id, safe='')}"
    if component == "upscaler" and model_id is None:
        return "upscaler", "/v2/upscaler"
    if component == "aligner" and model_id is None:
        return "aligner", "/v2/alignment"
    raise SpeechServerError(
        "invalid_install_target", "The installation target is invalid.", status=400
    )


def _normalize_install_artifact(value: Any, *, locked: bool) -> dict:
    if (
        not isinstance(value, dict)
        or value.get("kind") not in {"primary", "companion", "extra"}
        or not isinstance(value.get("filename"), str)
        or not value["filename"].strip()
        or value["filename"] in {".", ".."}
        or "/" in value["filename"] or "\\" in value["filename"]
    ):
        raise SpeechServerError("malformed_response", "An installation artifact is invalid.")
    result = {"kind": value["kind"], "filename": value["filename"]}
    if locked:
        if (
            not isinstance(value.get("repository"), str) or not value["repository"]
            or not isinstance(value.get("revision"), str) or len(value["revision"]) not in {40, 64}
            or any(character not in "0123456789abcdef" for character in value["revision"].lower())
            or not isinstance(value.get("sha256"), str) or len(value["sha256"]) != 64
            or any(character not in "0123456789abcdef" for character in value["sha256"].lower())
        ):
            raise SpeechServerError("malformed_response", "A locked installation artifact is invalid.")
        size = _nonnegative_integer(value.get("size"), "installation artifact size")
        if size is None:
            raise SpeechServerError("malformed_response", "A locked installation artifact is invalid.")
        result.update({
            "repository": value["repository"], "revision": value["revision"],
            "sha256": value["sha256"], "size": size,
        })
    else:
        approximate = value.get("approximateSize")
        if isinstance(approximate, str):
            approximate = approximate.strip()
            if not approximate or len(approximate) > 128:
                raise SpeechServerError(
                    "malformed_response", "An approximate installation artifact size is invalid."
                )
            result["approximateSize"] = approximate
        else:
            result["approximateSize"] = _nonnegative_integer(
                approximate, "approximate installation artifact size"
            )
    return result


def _normalize_install_plan(value: dict, expected_component: str) -> dict:
    artifacts = value.get("artifacts")
    locked = value.get("locked")
    acceptance = value.get("requiresLicenseAcceptance")
    if (
        value.get("component") != expected_component
        or not isinstance(value.get("registryBundle"), str) or not value["registryBundle"]
        or not isinstance(value.get("canonicalBackend"), str) or not value["canonicalBackend"]
        or not isinstance(value.get("license"), (str, type(None)))
        or not isinstance(acceptance, bool)
        or not isinstance(locked, bool)
        or not isinstance(artifacts, list) or not artifacts
        or locked == acceptance
        or acceptance and not value.get("license")
    ):
        raise SpeechServerError("malformed_response", "The speech server returned an invalid installation plan.")
    normalized_artifacts = [_normalize_install_artifact(item, locked=locked) for item in artifacts]
    filenames = [item["filename"] for item in normalized_artifacts]
    if (
        sum(item["kind"] == "primary" for item in normalized_artifacts) != 1
        or sum(item["kind"] == "companion" for item in normalized_artifacts) > 1
        or len(filenames) != len(set(filenames))
    ):
        raise SpeechServerError("malformed_response", "The speech server returned an invalid installation plan.")
    total_bytes = _nonnegative_integer(value.get("totalBytes"), "installation size")
    if locked:
        expected_total = sum(item["size"] for item in normalized_artifacts)
        if total_bytes is None or total_bytes != expected_total:
            raise SpeechServerError("malformed_response", "The installation plan size is inconsistent.")
    elif total_bytes is not None:
        raise SpeechServerError("malformed_response", "A gated installation plan cannot be locked yet.")
    return {
        "component": expected_component,
        "registryBundle": value["registryBundle"],
        "canonicalBackend": value["canonicalBackend"],
        "license": value.get("license"),
        "requiresLicenseAcceptance": acceptance,
        "locked": locked,
        "totalBytes": total_bytes,
        "artifacts": normalized_artifacts,
    }


def _normalize_install_job(value: dict, expected_component: str | None = None) -> dict:
    states = {"queued", "resolving", "downloading", "installing", "completed", "failed", "cancelled"}
    component = value.get("component")
    error = value.get("error")
    artifacts = value.get("artifacts")
    if (
        not isinstance(value.get("id"), str) or not value["id"]
        or not isinstance(component, str) or not component
        or (expected_component is not None and component != expected_component)
        or value.get("state") not in states
        or not isinstance(value.get("registryBundle"), str) or not value["registryBundle"]
        or not isinstance(value.get("canonicalBackend"), (str, type(None)))
        or not isinstance(value.get("license"), (str, type(None)))
        or not isinstance(value.get("requiresLicenseAcceptance"), bool)
        or not isinstance(value.get("createdAt"), str) or not value["createdAt"]
        or not isinstance(value.get("updatedAt"), str) or not value["updatedAt"]
        or not isinstance(value.get("currentArtifact"), (str, type(None)))
        or not isinstance(artifacts, list)
        or error is not None and (
            not isinstance(error, dict)
            or not isinstance(error.get("code"), str) or not error["code"]
            or not isinstance(error.get("message"), str) or not error["message"]
        )
    ):
        raise SpeechServerError("malformed_response", "The speech server returned an invalid installation job.")
    downloaded = _nonnegative_integer(value.get("downloadedBytes"), "downloaded byte count")
    total = _nonnegative_integer(value.get("totalBytes"), "installation byte count")
    if downloaded is None or (total is not None and downloaded > total):
        raise SpeechServerError("malformed_response", "The installation progress is invalid.")
    normalized_artifacts = [_normalize_install_artifact(item, locked=True) for item in artifacts]
    state = value["state"]
    if (state in {"failed", "cancelled"}) != (error is not None):
        raise SpeechServerError("malformed_response", "The installation result is inconsistent.")
    return {
        "jobId": value["id"], "component": component,
        "registryBundle": value["registryBundle"], "state": state,
        "createdAt": value["createdAt"], "updatedAt": value["updatedAt"],
        "canonicalBackend": value.get("canonicalBackend"), "license": value.get("license"),
        "requiresLicenseAcceptance": value["requiresLicenseAcceptance"],
        "currentArtifact": value.get("currentArtifact"), "artifacts": normalized_artifacts,
        "downloadedBytes": downloaded, "totalBytes": total,
        "error": copy.deepcopy(error),
    }


def _validate_stack_request(expected: dict) -> None:
    tts_model = expected["tts_model"]
    asr_model = expected["asr_model"]
    upscale = expected["upscale"]
    alignment = expected["alignment"]
    if (
        (tts_model is not None and (not isinstance(tts_model, str) or not tts_model.strip()))
        or (asr_model is not None and (not isinstance(asr_model, str) or not asr_model.strip()))
        or (tts_model is None and asr_model is None)
        or (tts_model is not None and tts_model == asr_model)
        or not isinstance(upscale, bool) or not isinstance(alignment, bool)
        or ((upscale or alignment) and tts_model is None)
    ):
        raise SpeechServerError(
            "invalid_load_plan", "The requested speech stack is invalid.", status=400
        )


def _normalize_stack_plan(result: dict, expected: dict) -> dict:
    fit = result.get("fit")
    resident_limit = result.get("residentLimit")
    capacity = result.get("residentCapacitySatisfied")
    desired = result.get("desiredModels")
    if (
        not isinstance(fit, dict)
        or fit.get("status") not in {"comfortable", "tight", "insufficient", "busy", "unknown"}
        or isinstance(resident_limit, bool) or not isinstance(resident_limit, int)
        or resident_limit <= 0
        or not isinstance(capacity, bool)
        or not isinstance(desired, list)
        or result.get("upscale") is not expected["upscale"]
        or result.get("alignment") is not expected["alignment"]
    ):
        raise SpeechServerError("malformed_response", "The speech server returned an invalid stack plan.")
    ratio = fit.get("ratio")
    if ratio is not None and (
        isinstance(ratio, bool) or not isinstance(ratio, (int, float))
        or not math.isfinite(float(ratio)) or ratio < 0
    ):
        raise SpeechServerError("malformed_response", "The speech server returned an invalid stack plan.")

    normalized_desired = []
    for item in desired:
        if (
            not isinstance(item, dict) or not isinstance(item.get("id"), str)
            or not item["id"] or item.get("task") not in {"tts", "asr"}
        ):
            raise SpeechServerError("malformed_response", "The speech server returned an invalid stack plan.")
        normalized_desired.append({"id": item["id"], "task": item["task"]})
    expected_models = {
        model_id: task
        for task, model_id in (
            ("tts", expected["tts_model"]), ("asr", expected["asr_model"])
        )
        if model_id
    }
    desired_models = {item["id"]: item["task"] for item in normalized_desired}
    if (
        len(normalized_desired) != len(desired_models)
        or desired_models != expected_models
    ):
        raise SpeechServerError("malformed_response", "The speech server returned a plan for different models.")

    def actions(field: str) -> list[dict]:
        raw = result.get(field)
        if not isinstance(raw, list):
            raise SpeechServerError("malformed_response", "The speech server returned an invalid stack plan.")
        normalized = []
        for item in raw:
            if (
                not isinstance(item, dict)
                or item.get("kind") not in {"model", "upscaler", "aligner"}
                or not isinstance(item.get("id"), str) or not item["id"]
                or (
                    item["kind"] == "model"
                    and item.get("task") not in {"tts", "asr"}
                )
                or (item["kind"] != "model" and "task" in item)
            ):
                raise SpeechServerError("malformed_response", "The speech server returned an invalid stack plan.")
            normalized.append({
                "kind": item["kind"], "id": item["id"],
                **({"task": item["task"]} if "task" in item else {}),
            })
        return normalized

    requirements = result.get("requirements")
    if not isinstance(requirements, dict):
        raise SpeechServerError("malformed_response", "The speech server returned invalid stack requirements.")
    normalized_requirements = {}
    for kind in ("ram", "vram"):
        item = requirements.get(kind)
        if not isinstance(item, dict):
            raise SpeechServerError("malformed_response", "The speech server returned invalid stack requirements.")
        item_ratio = item.get("ratio")
        if item_ratio is not None and (
            isinstance(item_ratio, bool) or not isinstance(item_ratio, (int, float))
            or not math.isfinite(float(item_ratio)) or item_ratio < 0
        ):
            raise SpeechServerError("malformed_response", "The speech server returned invalid stack requirements.")
        normalized_requirements[kind] = {
            "additionalBytes": _nonnegative_integer(item.get("additionalBytes"), f"{kind} requirement"),
            "reclaimableBytes": _nonnegative_integer(item.get("reclaimableBytes"), f"{kind} reclaimable memory"),
            "projectedFreeBytes": _nonnegative_integer(item.get("projectedFreeBytes"), f"{kind} projected free memory"),
            "ratio": float(item_ratio) if item_ratio is not None else None,
        }
    normalized_actions = {
        field: actions(field) for field in ("reuse", "load", "evict", "busy")
    }
    all_action_keys = [
        (item["kind"], item["id"])
        for field in normalized_actions.values()
        for item in field
    ]
    desired_actions = [
        item
        for field in ("reuse", "load")
        for item in normalized_actions[field]
        if item["kind"] == "model"
    ]
    desired_action_models = {item["id"]: item["task"] for item in desired_actions}
    obsolete_actions = normalized_actions["evict"] + normalized_actions["busy"]
    option_actions = {
        kind: sum(
            item["kind"] == kind
            for field in normalized_actions.values()
            for item in field
        )
        for kind in ("upscaler", "aligner")
    }
    if (
        len(all_action_keys) != len(set(all_action_keys))
        or len(desired_actions) != len(desired_action_models)
        or desired_action_models != expected_models
        or any(item["kind"] != "model" for item in obsolete_actions)
        or any(item["id"] in expected_models for item in obsolete_actions)
        or option_actions["upscaler"] != int(expected["upscale"])
        or option_actions["aligner"] != int(expected["alignment"])
        or (not capacity and fit["status"] != "insufficient")
        or (normalized_actions["busy"] and fit["status"] != "busy")
        or (not normalized_actions["busy"] and fit["status"] == "busy")
    ):
        raise SpeechServerError("malformed_response", "The speech server returned an inconsistent stack plan.")
    estimated = fit.get("estimated", True)
    sampled_at = result.get("sampledAt", "")
    if not isinstance(estimated, bool) or not isinstance(sampled_at, str):
        raise SpeechServerError("malformed_response", "The speech server returned an invalid stack plan.")
    return {
        "desiredModels": normalized_desired,
        "upscale": expected["upscale"],
        "alignment": expected["alignment"],
        "residentLimit": resident_limit,
        "residentCapacitySatisfied": capacity,
        "reuse": normalized_actions["reuse"],
        "load": normalized_actions["load"],
        "evict": normalized_actions["evict"],
        "busy": normalized_actions["busy"],
        "requirements": normalized_requirements,
        "fit": {
            "status": fit["status"],
            "ratio": float(ratio) if ratio is not None else None,
            "estimated": estimated,
        },
        "gpuIndex": _nonnegative_integer(result.get("gpuIndex"), "GPU index"),
        "sampledAt": sampled_at,
    }


def _normalize_stack_warmup(result: dict, expected: dict) -> dict:
    raw_results = result.get("results")
    resources = result.get("resources")
    if (
        not isinstance(result.get("success"), bool)
        or not isinstance(raw_results, list) or not raw_results
        or not isinstance(resources, dict)
    ):
        raise SpeechServerError("malformed_response", "The speech server returned an invalid warmup result.")
    normalized_results = []
    for item in raw_results:
        if (
            not isinstance(item, dict)
            or item.get("kind") not in {"model", "upscaler", "aligner"}
            or not isinstance(item.get("id"), str) or not item["id"]
            or not isinstance(item.get("loaded"), bool)
            or (
                "error" in item
                and (not isinstance(item["error"], str) or not item["error"].strip())
            )
            or (item.get("loaded") is True and "error" in item)
            or (item.get("loaded") is False and "error" not in item)
        ):
            raise SpeechServerError("malformed_response", "The speech server returned an invalid warmup result.")
        normalized_results.append({
            "kind": item["kind"], "id": item["id"], "loaded": item["loaded"],
            **({"error": item["error"]} if "error" in item else {}),
        })
    model_results = [item["id"] for item in normalized_results if item["kind"] == "model"]
    expected_models = [
        model_id for model_id in (expected["tts_model"], expected["asr_model"])
        if model_id
    ]
    kinds = [item["kind"] for item in normalized_results]
    if (
        len(model_results) != len(set(model_results))
        or set(model_results) != set(expected_models)
        or kinds.count("upscaler") != int(expected["upscale"])
        or kinds.count("aligner") != int(expected["alignment"])
        or result["success"] != all(item["loaded"] for item in normalized_results)
    ):
        raise SpeechServerError("malformed_response", "The speech server returned an inconsistent warmup result.")
    return {"success": result["success"], "results": normalized_results, "resources": resources}


class SpeechServerClient:
    _capability_cache: Dict[tuple[str, str], dict] = {}
    _cache_lock = threading.RLock()

    def __init__(self, api_url: str, api_key: str = "", session=None):
        self.api_url = normalize_url(api_url)
        self.api_key = str(api_key or "").strip()
        self.session = session or requests.Session()

    @property
    def ws_url(self) -> str:
        return self.api_url.replace("http://", "ws://", 1).replace("https://", "wss://", 1)

    @property
    def headers(self) -> dict:
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Basic {self.api_key}"
        return headers

    def _request(self, method: str, path: str, **kwargs) -> dict:
        try:
            response = self.session.request(
                method,
                f"{self.api_url}{path}",
                headers=self.headers,
                timeout=kwargs.pop("timeout", (3.0, 15.0)),
                allow_redirects=False,
                **kwargs,
            )
        except requests.exceptions.Timeout as exc:
            raise SpeechServerError("timeout", "The speech server did not respond in time.", status=504) from exc
        except requests.exceptions.ConnectionError as exc:
            raise SpeechServerError("unreachable_server", "The speech server could not be reached.") from exc
        except requests.exceptions.RequestException as exc:
            raise SpeechServerError("unreachable_server", "The speech server request failed.") from exc
        if 300 <= response.status_code < 400:
            raise SpeechServerError("redirect_rejected", "The speech server attempted an unsafe redirect.", status=400)
        if response.status_code == 401:
            raise SpeechServerError("authentication_failure", "The speech server rejected the API key.", status=401)
        if response.status_code >= 400:
            code = "server_error"
            message = f"The speech server returned HTTP {response.status_code}."
            details = None
            try:
                error = response.json().get("error", {})
                if isinstance(error, dict):
                    code = str(error.get("code") or code)
                    message = str(error.get("message") or message)
                    details = error.get("details")
            except (AttributeError, TypeError, ValueError):
                pass
            raise SpeechServerError(code, message, status=response.status_code, details=details)
        try:
            data = response.json()
        except (ValueError, TypeError) as exc:
            raise SpeechServerError("malformed_response", "The speech server returned invalid JSON.") from exc
        if not isinstance(data, dict):
            raise SpeechServerError("malformed_response", "The speech server returned an invalid response.")
        return data

    @classmethod
    def invalidate_cache(cls, api_url: Optional[str] = None, api_key: Optional[str] = None):
        with cls._cache_lock:
            if api_url is None:
                cls._capability_cache.clear()
                return
            cls._capability_cache.pop((normalize_url(api_url), str(api_key or "").strip()), None)

    def capabilities(self, force: bool = False) -> dict:
        key = (self.api_url, self.api_key)
        with self._cache_lock:
            if not force and key in self._capability_cache:
                return copy.deepcopy(self._capability_cache[key])
        data = self._request("GET", "/v2/capabilities")
        if data.get("protocolVersion") != PROTOCOL_VERSION:
            raise SpeechServerError(
                "protocol_mismatch",
                f"Speech protocol {data.get('protocolVersion')!r} is incompatible; {PROTOCOL_VERSION} is required.",
                status=409,
            )
        if not isinstance(data.get("models"), list):
            raise SpeechServerError("malformed_response", "Capabilities did not contain a model list.")
        version = data.get("capabilitiesVersion", 1)
        if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
            raise SpeechServerError(
                "malformed_response", "Capabilities contained an invalid version."
            )
        resident_limit = data.get("residentLimit", 1)
        if (
            isinstance(resident_limit, bool)
            or not isinstance(resident_limit, int)
            or resident_limit <= 0
        ):
            raise SpeechServerError(
                "malformed_response", "Capabilities contained an invalid resident limit."
            )
        with self._cache_lock:
            self._capability_cache[key] = copy.deepcopy(data)
        return data

    def resources(self) -> dict:
        data = self._request("GET", "/v2/resources", timeout=(2.0, 5.0))
        if not isinstance(data.get("components", []), list):
            raise SpeechServerError("malformed_response", "The speech server returned invalid resources.")
        return data

    def active_installations(self, capabilities: dict | None = None) -> list[dict]:
        raw = capabilities if capabilities is not None else self.capabilities()
        if not isinstance(raw, dict):
            raise SpeechServerError("malformed_response", "Capabilities are invalid.")
        sources: list[tuple[str, Any]] = []
        for model in raw.get("models", []):
            if not isinstance(model, dict) or not isinstance(model.get("id"), str):
                raise SpeechServerError("malformed_response", "Capabilities contain an invalid model.")
            sources.append((f"model:{model['id']}", model.get("installation")))
        for component, field in (("upscaler", "upscaler"), ("aligner", "alignment")):
            value = raw.get(field)
            if value is not None and not isinstance(value, dict):
                raise SpeechServerError("malformed_response", "Capabilities contain an invalid component.")
            if value is not None:
                sources.append((component, value.get("installation")))
        jobs = []
        seen = set()
        for component, installation in sources:
            if installation is None:
                continue
            if not isinstance(installation, dict):
                raise SpeechServerError("malformed_response", "Component installation metadata is invalid.")
            raw_job = installation.get("job")
            if raw_job is None:
                continue
            normalized = _normalize_install_job(raw_job, component)
            if normalized["jobId"] in seen:
                raise SpeechServerError("malformed_response", "Capabilities repeat an installation job.")
            seen.add(normalized["jobId"])
            jobs.append(normalized)
        return jobs

    def install_plan(self, component: str, model_id: str | None = None) -> dict:
        expected, path = _install_component_path(component, model_id)
        result = self._request("GET", f"{path}:install-plan", timeout=(3.0, 120.0))
        return _normalize_install_plan(result, expected)

    def start_install(
        self, component: str, model_id: str | None = None, *, accept_license: bool = False
    ) -> dict:
        if not isinstance(accept_license, bool):
            raise SpeechServerError(
                "invalid_install_request", "License acceptance must be a boolean.", status=400
            )
        expected, path = _install_component_path(component, model_id)
        result = self._request(
            "POST", f"{path}:install", json={"acceptLicense": accept_license},
            timeout=(3.0, 30.0),
        )
        return _normalize_install_job(result, expected)

    def installation(self, job_id: str) -> dict:
        if not isinstance(job_id, str) or not job_id.strip() or len(job_id.strip()) > 128:
            raise SpeechServerError("invalid_install_job", "The installation job ID is invalid.", status=400)
        result = self._request(
            "GET", f"/v2/installations/{quote(job_id.strip(), safe='')}", timeout=(3.0, 30.0)
        )
        normalized = _normalize_install_job(result)
        if normalized["jobId"] != job_id.strip():
            raise SpeechServerError("malformed_response", "The speech server returned a different installation job.")
        if normalized["state"] == "completed":
            self.invalidate_cache(self.api_url, self.api_key)
        return normalized

    def cancel_installation(self, job_id: str) -> dict:
        if not isinstance(job_id, str) or not job_id.strip() or len(job_id.strip()) > 128:
            raise SpeechServerError("invalid_install_job", "The installation job ID is invalid.", status=400)
        result = self._request(
            "DELETE", f"/v2/installations/{quote(job_id.strip(), safe='')}", timeout=(3.0, 30.0)
        )
        normalized = _normalize_install_job(result)
        if normalized["jobId"] != job_id.strip():
            raise SpeechServerError("malformed_response", "The speech server returned a different installation job.")
        return normalized

    def stack_plan(
        self, *, tts_model: str | None, asr_model: str | None,
        upscale: bool = False, alignment: bool = False,
    ) -> dict:
        expected = {
            "tts_model": tts_model,
            "asr_model": asr_model,
            "upscale": upscale,
            "alignment": alignment,
        }
        _validate_stack_request(expected)
        result = self._request(
            "POST",
            "/v2/stack:plan",
            json={
                "ttsModel": expected["tts_model"],
                "asrModel": expected["asr_model"],
                "upscale": expected["upscale"],
                "alignment": expected["alignment"],
            },
            timeout=(2.0, 8.0),
        )
        return _normalize_stack_plan(result, expected)

    def stack_warmup(
        self, *, tts_model: str | None, asr_model: str | None,
        upscale: bool = False, alignment: bool = False,
    ) -> dict:
        expected = {
            "tts_model": tts_model,
            "asr_model": asr_model,
            "upscale": upscale,
            "alignment": alignment,
        }
        # Apply the same local validation used by planning before a long-running
        # warmup request is sent.
        _validate_stack_request(expected)
        result = self._request(
            "POST",
            "/v2/stack:warmup",
            json={
                "ttsModel": tts_model,
                "asrModel": asr_model,
                "upscale": upscale,
                "alignment": alignment,
            },
            timeout=(3.0, 300.0),
        )
        return _normalize_stack_warmup(result, expected)

    def transcribe(
        self,
        model_id: str,
        audio_data: bytes,
        *,
        sample_rate: int = 16000,
        language: str = "auto",
        bias_terms: list[str] | None = None,
        timestamps: str = "none",
    ) -> dict:
        if (
            not isinstance(model_id, str) or not model_id.strip()
            or not isinstance(audio_data, (bytes, bytearray, memoryview))
            or not audio_data or len(audio_data) % 2
            or isinstance(sample_rate, bool) or not isinstance(sample_rate, int)
            or sample_rate <= 0
        ):
            raise SpeechServerError("unsupported_audio", "The transcription request is invalid.", status=400)
        if not isinstance(language, str) or not language.strip():
            raise SpeechServerError("unsupported_language", "The transcription language is invalid.", status=400)
        if timestamps not in {"none", "segment", "word"}:
            raise SpeechServerError("unsupported_timestamps", "The timestamp mode is invalid.", status=400)
        if bias_terms is not None and not isinstance(bias_terms, (list, tuple)):
            raise SpeechServerError("unsupported_bias_terms", "Bias terms must be a list.", status=400)
        if any(not isinstance(term, str) or not term.strip() for term in (bias_terms or ())):
            raise SpeechServerError("unsupported_bias_terms", "Bias terms are invalid.", status=400)
        request_id = str(uuid.uuid4())
        result = self._request(
            "POST",
            "/v2/transcribe",
            json={
                "requestId": request_id,
                "model": model_id,
                "audioData": base64.b64encode(bytes(audio_data)).decode("ascii"),
                "audio": {
                    "encoding": "pcm_s16le",
                    "sampleRate": sample_rate,
                    "channels": 1,
                },
                "language": language,
                "biasTerms": list(bias_terms or ()),
                "timestamps": timestamps,
            },
            timeout=(3.0, 120.0),
        )
        if (
            result.get("requestId") != request_id
            or result.get("model") != model_id
            or not isinstance(result.get("text"), str)
        ):
            raise SpeechServerError("malformed_response", "The speech server returned an invalid transcript.")
        confidence = result.get("confidence")
        if confidence is not None and (
            isinstance(confidence, bool) or not isinstance(confidence, (int, float))
            or not math.isfinite(float(confidence)) or not 0 <= confidence <= 1
        ):
            raise SpeechServerError("malformed_response", "The speech server returned invalid confidence metadata.")
        return result
