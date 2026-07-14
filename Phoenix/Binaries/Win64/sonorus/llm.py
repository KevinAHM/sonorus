"""
LLM utility for multiple providers (Gemini, OpenRouter, OpenAI, Ollama, llama.cpp).
Single module for all LLM operations - text and vision.
"""
import base64
import json
import os
import re
import threading
import time
from urllib.parse import quote
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Optional, List, Dict, Any

import httpx
from openai import DefaultHttpxClient, OpenAI

# Import log_llm for LLM logging (separate file to avoid circular import)
from utils.llm_logging import log_llm

# Google Gemini support
try:
    from google import genai
    from google.genai import types
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False
    print("[LLM] google-genai not installed, Gemini provider unavailable")

# Lazy import event_logger to avoid circular dependencies
_event_logger = None

def _get_event_logger():
    """Lazy import of event_logger"""
    global _event_logger
    if _event_logger is None:
        try:
            import event_logger as el
            _event_logger = el
        except ImportError:
            pass
    return _event_logger


# Store last error for retrieval by callers when chat() returns None
_last_error = None
_last_response_metadata = {}


def get_last_error() -> Optional[str]:
    """Get the last error message from a failed LLM call."""
    return _last_error


def _set_last_error(error: Optional[str]):
    """Set the last error message."""
    global _last_error
    _last_error = error


def get_last_response_metadata() -> Dict[str, Any]:
    """Get metadata from the last successful LLM response."""
    return dict(_last_response_metadata)


def _set_last_response_metadata(metadata: Optional[Dict[str, Any]] = None):
    """Set metadata for the last successful LLM response."""
    global _last_response_metadata
    _last_response_metadata = dict(metadata or {})


def _parse_llm_error(error: Exception) -> str:
    """
    Parse LLM API errors into user-friendly messages for the event log.
    Works for Gemini, OpenRouter, and OpenAI errors.

    Known error patterns:
    - 429 RESOURCE_EXHAUSTED (free-tier rate limit) -> "free tier rate limit exceeded"
    - 503 UNAVAILABLE (overloaded) -> "model overloaded"
    - 400 INVALID_ARGUMENT (bad API key) -> "api key not valid"
    - 401 (auth) -> "api key not valid"
    - 400 "not a valid model ID" -> "invalid model id"
    - Otherwise: extract the 'message' field or return as-is
    """
    error_str = str(error)
    lower_error = error_str.lower()

    # Check for known error codes/patterns
    if '429' in error_str:
        if 'RESOURCE_EXHAUSTED' in error_str:
            if (
                'generaterequestsperminute' in lower_error
                or 'generate_content_free_tier_requests' in lower_error
                or 'retrydelay' in lower_error
                or 'please retry in' in lower_error
            ):
                return "Gemini free tier rate limit exceeded - wait a moment or switch to OpenRouter and deposit $5 for more use"
            if 'free_tier' in lower_error or 'quota' in lower_error:
                return "Gemini free tier quota exhausted - switch to OpenRouter and deposit $5 for more use"
        return "rate limit exceeded"

    if '503' in error_str and 'UNAVAILABLE' in error_str:
        return "model overloaded"

    # Auth errors (401, "No cookie auth credentials", etc.)
    if '401' in error_str or 'UNAUTHENTICATED' in error_str:
        return "api key not valid"

    if 'No cookie auth credentials' in error_str:
        return "api key not valid"

    # Invalid model ID (OpenRouter)
    if 'not a valid model ID' in error_str:
        return "invalid model id"

    # Bad API key (Gemini style)
    if '400' in error_str and 'API key not valid' in error_str:
        return "api key not valid"

    if '400' in error_str and 'INVALID_ARGUMENT' in error_str:
        return "invalid request"

    if '403' in error_str or 'PERMISSION_DENIED' in error_str:
        return "permission denied"

    if '404' in error_str or 'NOT_FOUND' in error_str:
        return "model not found"

    # Try to extract the message from the error dict
    # Format: "Error code: 400 - {'error': {'message': '...'}}"
    try:
        # Find the JSON-like dict in the error string
        import ast
        brace_start = error_str.find('{')
        if brace_start != -1:
            dict_str = error_str[brace_start:]
            error_dict = ast.literal_eval(dict_str)
            if isinstance(error_dict, dict):
                msg = error_dict.get('error', {}).get('message', '')
                if msg:
                    # Return first sentence or first 80 chars
                    first_sentence = msg.split('.')[0].strip()
                    if len(first_sentence) <= 80:
                        return first_sentence.lower()
                    return first_sentence[:80].lower() + "..."
    except:
        pass

    # Fallback: return a truncated version of the original
    if len(error_str) <= 80:
        return error_str
    return error_str[:80] + "..."


# Keep alias for backwards compatibility
_parse_gemini_error = _parse_llm_error

# Minimum max_tokens when reasoning is enabled (thinking needs room)
MIN_REASONING_TOKENS = 8192

# Module state
from utils.settings import DATA_DIR, GEMINI_CHAT_DEFAULT, GEMINI_CHAT_DEFAULT_OR
SETTINGS_FILE = Path(DATA_DIR) / "settings.json"


def load_settings():
    """Load settings from JSON file"""
    try:
        if SETTINGS_FILE.exists():
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        print(f"[LLM] Error loading settings: {e}")
    return {}


# Shared model capabilities cache (from OpenRouter API, used by all providers)
_model_capabilities = {}  # model_id -> {supports_reasoning: bool, full_data: dict}
_openrouter_model_capabilities_by_id = {}  # clean OpenRouter model id -> capability dict
_model_capabilities_fetch_attempted = False
_openrouter_model_ids = []  # Full OpenRouter model IDs for frontend autocomplete
_openrouter_embedding_model_ids = []  # OpenRouter embedding model IDs for frontend autocomplete
_openrouter_vision_model_ids = []  # OpenRouter image-input text model IDs for frontend autocomplete
_openrouter_provider_metadata_cache = {}  # clean_model_id -> (timestamp, provider metadata list)
_OPENROUTER_PROVIDER_METADATA_TTL = 300

# --- Client connection pooling ---
_client_lock = threading.Lock()
_cached_client = None           # OpenAI() instance
_cached_client_key = None       # (provider, api_key, base_url) for debug logging
_last_request_time = 0.0        # For idle detection
_keepalive_timer = None         # threading.Timer ref
_KEEPALIVE_INTERVAL = 45        # Seconds between keep-alive pings
_llamacpp_slot_condition = threading.Condition()
_llamacpp_slot_token = None
_llamacpp_slot_context = None
_llamacpp_slot_acquired_at = None
_llamacpp_slot_cache = None
_llamacpp_slot_cache_key = None


def _get_client():
    """Get a cached OpenAI client, creating one if needed.

    Reuses the same client across calls to avoid TCP+TLS handshake overhead
    (~100-300ms per new client). Thread-safe for concurrent requests.
    """
    global _cached_client, _cached_client_key, _last_request_time
    with _client_lock:
        if _cached_client is not None:
            _last_request_time = time.time()
            return _cached_client

        # Create a new client using existing factory
        client = _create_client()
        if client is None:
            return None

        # Build cache key for debug logging
        settings = load_settings()
        llm_settings = settings.get('llm', {})
        provider = llm_settings.get('provider', 'gemini')
        _cached_client = client
        _cached_client_key = provider
        _last_request_time = time.time()

        print(f"[LLM] Client cached for {provider}")
        _start_keepalive()
        return _cached_client


def _start_keepalive():
    """Start the keep-alive timer (call with _client_lock held)."""
    global _keepalive_timer
    _stop_keepalive_unlocked()
    _keepalive_timer = threading.Timer(_KEEPALIVE_INTERVAL, _keepalive_tick)
    _keepalive_timer.daemon = True
    _keepalive_timer.start()


def _stop_keepalive_unlocked():
    """Stop the keep-alive timer (no lock needed)."""
    global _keepalive_timer
    if _keepalive_timer is not None:
        _keepalive_timer.cancel()
        _keepalive_timer = None


def _keepalive_tick():
    """Ping the cached client to keep TCP connections alive."""
    global _keepalive_timer
    _keepalive_timer = None  # Timer is one-shot

    with _client_lock:
        client = _cached_client
        provider = _cached_client_key
        if client is None:
            return

    # Ping outside lock (client is thread-safe)
    idle = time.time() - _last_request_time
    if idle >= _KEEPALIVE_INTERVAL:
        try:
            # GET /models is supported by llama.cpp; /key exercises auth on OpenRouter/OpenAI.
            endpoint = '/models' if provider == 'llamacpp' else '/key'
            client._client.get(str(client.base_url).rstrip('/') + endpoint)
        except Exception:
            pass  # httpx will reconnect on next real request

    # Reschedule if client still alive
    with _client_lock:
        if _cached_client is not None:
            _start_keepalive()


def invalidate_client():
    """Invalidate the cached client. Call when LLM settings change."""
    global _cached_client, _cached_client_key, _llamacpp_slot_cache, _llamacpp_slot_cache_key
    with _client_lock:
        old_client = _cached_client
        _cached_client = None
        _cached_client_key = None
        _llamacpp_slot_cache = None
        _llamacpp_slot_cache_key = None
        _stop_keepalive_unlocked()

    if old_client is not None:
        print("[LLM] Client cache invalidated")
        try:
            old_client.close()
        except Exception:
            pass


def prewarm_client():
    """Pre-warm the LLM client connection in a background thread.

    Non-blocking, failure is non-fatal. Called at startup and after settings changes.
    """
    settings = load_settings()
    provider = settings.get('llm', {}).get('provider', 'gemini')
    if provider in ('gemini', 'ollama'):
        return  # Native SDK/API paths do not use the shared OpenAI client

    def _warm():
        try:
            client = _get_client()
            if client:
                endpoint = '/models' if _cached_client_key == 'llamacpp' else '/key'
                client._client.get(str(client.base_url).rstrip('/') + endpoint)
                print(f"[LLM] {_cached_client_key} connection pre-warmed")
        except Exception as e:
            print(f"[LLM] Pre-warm ping failed (non-fatal): {e}")

    t = threading.Thread(target=_warm, daemon=True)
    t.start()


def _fetch_openrouter_embedding_model_ids(requests_module) -> list:
    """Fetch OpenRouter embedding model IDs from the dedicated embeddings model endpoint."""
    headers = {}
    try:
        settings = load_settings()
        api_key = settings.get('llm', {}).get('openrouter', {}).get('api_key', '')
        if api_key:
            headers['Authorization'] = f'Bearer {api_key}'
    except Exception:
        pass

    resp = requests_module.get(
        "https://openrouter.ai/api/v1/embeddings/models",
        headers=headers or None,
        timeout=10,
    )
    if not resp.ok:
        print(f"[LLM] Failed to fetch OpenRouter embedding models: {resp.status_code}")
        return []

    model_ids = set()
    for model in resp.json().get('data', []):
        model_id = model.get('id')
        if model_id:
            model_ids.add(model_id)

    # Keep the current default available even if the endpoint omits it transiently.
    model_ids.add('openai/text-embedding-3-small')
    return sorted(model_ids)


def _model_supports_vision(model: dict) -> bool:
    """Return True when OpenRouter reports image input and text output for a model."""
    architecture = model.get('architecture') or {}
    input_modalities = architecture.get('input_modalities') or []
    output_modalities = architecture.get('output_modalities') or []

    input_modalities = {str(modality).lower() for modality in input_modalities}
    output_modalities = {str(modality).lower() for modality in output_modalities}

    if 'image' in input_modalities and (not output_modalities or 'text' in output_modalities):
        return True

    # Older/cached records can include only a compact modality string, e.g. text+image->text.
    modality = str(architecture.get('modality') or '').lower()
    if 'image' in modality:
        source, _, target = modality.partition('->')
        return 'image' in source and (not target or 'text' in target)

    return False


def _clean_model_id(model_id: str) -> str:
    return str(model_id or '').split(':', 1)[0].strip()


def _base_model_name(model_id: str) -> str:
    clean_id = _clean_model_id(model_id)
    return clean_id.split('/', 1)[-1] if '/' in clean_id else clean_id


def _model_supports_reasoning_data(model: dict) -> bool:
    supported = model.get('supported_parameters') or []
    return 'reasoning' in supported or isinstance(model.get('reasoning'), dict)


def _get_model_capability(model_id: str) -> Optional[Dict[str, Any]]:
    clean_id = _clean_model_id(model_id)
    if clean_id in _openrouter_model_capabilities_by_id:
        return _openrouter_model_capabilities_by_id[clean_id]

    base_name = _base_model_name(model_id)
    return _model_capabilities.get(base_name)


def _ensure_model_capabilities_loaded():
    global _model_capabilities_fetch_attempted
    if _model_capabilities or _model_capabilities_fetch_attempted:
        return
    fetch_model_capabilities()


def fetch_model_capabilities():
    """Fetch OpenRouter model lists and extract capabilities for frontend selectors."""
    global _model_capabilities, _openrouter_model_capabilities_by_id
    global _model_capabilities_fetch_attempted, _openrouter_model_ids
    global _openrouter_embedding_model_ids, _openrouter_vision_model_ids
    _model_capabilities_fetch_attempted = True
    try:
        import requests
        resp = requests.get("https://openrouter.ai/api/v1/models", timeout=10)
        if resp.ok:
            _model_capabilities = {}
            _openrouter_model_capabilities_by_id = {}
            model_ids = set()
            vision_model_ids = set()
            for m in resp.json().get('data', []):
                full_id = m['id']
                model_ids.add(full_id)
                if _model_supports_vision(m):
                    vision_model_ids.add(full_id)

                # Strip OpenRouter modifiers (e.g., "model:nitro" -> "model")
                clean_id = _clean_model_id(full_id)

                # Strip provider prefix (e.g., "openai/gpt-5-nano" -> "gpt-5-nano")
                base_name = _base_model_name(clean_id)

                # If duplicate base name, prefer the one that supports reasoning
                caps = {
                    'supports_reasoning': _model_supports_reasoning_data(m),
                    'supports_vision': full_id in vision_model_ids,
                    'full_id': full_id,
                    'reasoning': m.get('reasoning'),
                    'full_data': m
                }
                _openrouter_model_capabilities_by_id[clean_id] = caps
                if base_name in _model_capabilities:
                    caps['supports_vision'] = (
                        caps['supports_vision'] or
                        _model_capabilities[base_name].get('supports_vision', False)
                    )
                    if caps['supports_reasoning'] and not _model_capabilities[base_name]['supports_reasoning']:
                        _model_capabilities[base_name] = caps
                    else:
                        _model_capabilities[base_name]['supports_vision'] = caps['supports_vision']
                else:
                    _model_capabilities[base_name] = caps

            _openrouter_model_ids = sorted(model_ids)
            _openrouter_vision_model_ids = sorted(vision_model_ids)
            print(f"[LLM] Cached capabilities for {len(_model_capabilities)} models")
        else:
            print(f"[LLM] Failed to fetch model capabilities: {resp.status_code}")
            _model_capabilities_fetch_attempted = False

        try:
            _openrouter_embedding_model_ids = _fetch_openrouter_embedding_model_ids(requests)
            print(f"[LLM] Cached {len(_openrouter_embedding_model_ids)} OpenRouter embedding models")
        except Exception as e:
            print(f"[LLM] Failed to fetch OpenRouter embedding models: {e}")
            _openrouter_embedding_model_ids = ['openai/text-embedding-3-small']
    except Exception as e:
        print(f"[LLM] Failed to fetch model capabilities: {e}")
        _openrouter_embedding_model_ids = ['openai/text-embedding-3-small']
        _model_capabilities_fetch_attempted = False


def supports_reasoning(model_id: str) -> bool:
    """Check if model supports reasoning (works for any provider)
    Strips OpenRouter modifiers and provider prefixes."""
    _ensure_model_capabilities_loaded()
    caps = _get_model_capability(model_id)
    return bool(caps and caps.get('supports_reasoning'))


def get_model_capabilities_for_frontend() -> dict:
    """
    Return model capabilities for frontend use.
    Keys are already base model names (provider prefix stripped at fetch time).

    Returns dict like: {"gpt-4o": {"supports_reasoning": false, "full_id": "openai/gpt-4o"}, ...}
    """
    return {
        base_name: {
            'supports_reasoning': caps['supports_reasoning'],
            'supports_vision': caps.get('supports_vision', False),
            'full_id': caps.get('full_id', base_name),
            'reasoning': caps.get('reasoning'),
        }
        for base_name, caps in _model_capabilities.items()
    }


def get_openrouter_model_ids_for_frontend() -> list:
    """Return cached OpenRouter model IDs for frontend autocomplete."""
    return list(_openrouter_model_ids)


def get_openrouter_embedding_model_ids_for_frontend() -> list:
    """Return cached OpenRouter embedding model IDs for frontend autocomplete."""
    if _openrouter_embedding_model_ids:
        return list(_openrouter_embedding_model_ids)
    return ['openai/text-embedding-3-small']


def get_openrouter_vision_model_ids_for_frontend() -> list:
    """Return cached OpenRouter model IDs that support image input and text output."""
    return list(_openrouter_vision_model_ids)


def _openrouter_headers() -> Dict[str, str]:
    headers = {"User-Agent": "Sonorus (Hogwarts Legacy Mod)"}
    try:
        settings = load_settings()
        api_key = settings.get('llm', {}).get('openrouter', {}).get('api_key', '')
        if api_key:
            headers['Authorization'] = f'Bearer {api_key}'
    except Exception:
        pass
    return headers


def _safe_float(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _openrouter_metric_p50(value) -> Optional[float]:
    if isinstance(value, dict):
        for key in ('p50', 'p75', 'p90', 'p99'):
            parsed = _safe_float(value.get(key))
            if parsed is not None:
                return parsed
        return None
    return _safe_float(value)


def _openrouter_latency_seconds(value) -> Optional[float]:
    parsed = _openrouter_metric_p50(value)
    if parsed is None:
        return None
    return parsed / 1000 if isinstance(value, dict) or parsed > 20 else parsed


def _format_per_million(value) -> Optional[float]:
    parsed = _safe_float(value)
    if parsed is None:
        return None
    return parsed * 1_000_000


def _percentile(values: list, percentile: float) -> Optional[float]:
    sorted_values = sorted(v for v in values if v is not None)
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]

    position = (len(sorted_values) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = position - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight


def _normalize_metric(value: float, low: float, high: float, lower_is_better: bool) -> float:
    if high <= low:
        return 0.0
    normalized = (value - low) / (high - low)
    normalized = max(0.0, min(1.0, normalized))
    return normalized if lower_is_better else 1.0 - normalized


def _format_openrouter_provider_detail(provider: dict) -> str:
    prompt = provider.get('prompt_per_million')
    completion = provider.get('completion_per_million')
    cost_parts = []
    if prompt is not None:
        cost_parts.append(f"${prompt:g}/m in")
    if completion is not None:
        cost_parts.append(f"${completion:g}/m out")

    metric_parts = []
    latency = provider.get('latency_seconds')
    throughput = provider.get('throughput_tokens_per_second')
    uptime = provider.get('uptime_last_30m')
    if latency is not None:
        metric_parts.append(f"{latency:.2f}s")
    if throughput is not None:
        metric_parts.append(f"{throughput:g}t/s")
    if uptime is not None:
        uptime_pct = uptime * 100 if uptime <= 1 else uptime
        metric_parts.append(f"{uptime_pct:.1f}% uptime")

    return ', '.join(cost_parts + metric_parts)


def _format_openrouter_provider_label(provider: dict) -> str:
    name = provider.get('provider_name') or provider.get('value') or 'Unknown'
    badge_text = ' '.join(badge.get('icon', '') for badge in provider.get('badges', []) if badge.get('icon'))
    if badge_text:
        name = f"{name} {badge_text}"

    detail = provider.get('detail') or _format_openrouter_provider_detail(provider)
    return f"{name} ({detail})" if detail else name


def _normalize_openrouter_endpoint_provider(endpoint: dict) -> Optional[dict]:
    value = endpoint.get('tag') or endpoint.get('provider_name')
    if not value:
        return None

    pricing = endpoint.get('pricing') or {}
    prompt = _format_per_million(pricing.get('prompt'))
    completion = _format_per_million(pricing.get('completion'))

    provider = {
        'value': value,
        'provider_name': endpoint.get('provider_name') or value,
        'tag': endpoint.get('tag') or value,
        'prompt_per_million': prompt,
        'completion_per_million': completion,
        'latency_seconds': _openrouter_latency_seconds(endpoint.get('latency_last_30m')),
        'throughput_tokens_per_second': _openrouter_metric_p50(endpoint.get('throughput_last_30m')),
        'uptime_last_30m': _safe_float(endpoint.get('uptime_last_30m')),
        'quantization': endpoint.get('quantization'),
        'status': endpoint.get('status'),
        'badges': [],
    }
    return provider


def _assign_openrouter_provider_badges(providers: list) -> None:
    costs = []
    for provider in providers:
        prompt = provider.get('prompt_per_million')
        completion = provider.get('completion_per_million')
        if prompt is not None and completion is not None:
            cost = prompt + completion
            provider['_provider_cost_score'] = cost
            costs.append(cost)

    if costs:
        min_cost = min(costs)
        cost_p25 = _percentile(costs, 0.25)
        cheap_threshold = max(
            cost_p25 if cost_p25 is not None else min_cost,
            min_cost * 1.15,
            min_cost + 0.01,
        )
        for provider in providers:
            cost = provider.get('_provider_cost_score')
            if cost is not None and cost <= cheap_threshold:
                provider['badges'].append({
                    'icon': '💰',
                    'label': 'Cheap',
                    'title': 'Low-cost band for this model',
                })

    latencies = [provider.get('latency_seconds') for provider in providers if provider.get('latency_seconds') is not None]
    throughputs = [provider.get('throughput_tokens_per_second') for provider in providers if provider.get('throughput_tokens_per_second') is not None]
    speed_scores = []
    if latencies or throughputs:
        min_latency, max_latency = (min(latencies), max(latencies)) if latencies else (None, None)
        min_throughput, max_throughput = (min(throughputs), max(throughputs)) if throughputs else (None, None)
        for provider in providers:
            parts = []
            latency = provider.get('latency_seconds')
            throughput = provider.get('throughput_tokens_per_second')
            if latency is not None and min_latency is not None and max_latency is not None:
                parts.append(_normalize_metric(latency, min_latency, max_latency, lower_is_better=True))
            if throughput is not None and min_throughput is not None and max_throughput is not None:
                parts.append(_normalize_metric(throughput, min_throughput, max_throughput, lower_is_better=False))
            if parts:
                score = sum(parts) / len(parts)
                provider['_provider_speed_score'] = score
                speed_scores.append(score)

    if speed_scores:
        min_speed = min(speed_scores)
        speed_threshold = max(_percentile(speed_scores, 0.25) or min_speed, min_speed + 0.08)
        for provider in providers:
            score = provider.get('_provider_speed_score')
            if score is not None and score <= speed_threshold:
                provider['badges'].append({
                    'icon': '⚡',
                    'label': 'Fast',
                    'title': 'Fast band by latency and tokens per second',
                })

    uptimes = [provider.get('uptime_last_30m') for provider in providers if provider.get('uptime_last_30m') is not None]
    if uptimes:
        max_uptime = max(uptimes)
        stable_threshold = max(_percentile(uptimes, 0.75) or max_uptime, 99.5)
        for provider in providers:
            uptime = provider.get('uptime_last_30m')
            if uptime is not None and uptime >= stable_threshold:
                provider['badges'].append({
                    'icon': '🛡️',
                    'label': 'Stable',
                    'title': 'High-uptime band for this model',
                })

    for provider in providers:
        provider['detail'] = _format_openrouter_provider_detail(provider)
        provider['label'] = _format_openrouter_provider_label(provider)


def _cleanup_openrouter_provider_sort_fields(providers: list) -> None:
    for provider in providers:
        provider.pop('_provider_cost_score', None)
        provider.pop('_provider_speed_score', None)


def _has_openrouter_provider_badge(provider: dict, label: str) -> bool:
    return any(badge.get('label') == label for badge in provider.get('badges', []))


def _missing_last(value):
    return value if value is not None else 1_000_000_000


def _provider_sort_key(provider: dict):
    prompt = provider.get('prompt_per_million')
    completion = provider.get('completion_per_million')
    total = (prompt if prompt is not None else 1_000_000_000) + (completion if completion is not None else 1_000_000_000)
    latency = provider.get('latency_seconds')
    speed = provider.get('_provider_speed_score')
    name = provider.get('provider_name') or provider.get('value') or ''
    return (
        0 if _has_openrouter_provider_badge(provider, 'Cheap') else 1,
        _missing_last(speed),
        total,
        _missing_last(latency),
        name.lower(),
    )


def get_openrouter_model_providers_for_frontend(model: str, force_refresh: bool = False) -> list:
    """Fetch provider endpoint metadata for a specific OpenRouter model."""
    clean_model = _strip_openrouter_modifier((model or '').strip())
    if '/' not in clean_model:
        return []

    now = time.time()
    cached = _openrouter_provider_metadata_cache.get(clean_model)
    if cached and not force_refresh and now - cached[0] < _OPENROUTER_PROVIDER_METADATA_TTL:
        return list(cached[1])

    try:
        import requests
        author, slug = clean_model.split('/', 1)
        url = f"https://openrouter.ai/api/v1/models/{quote(author, safe='')}/{quote(slug, safe='')}/endpoints"
        resp = requests.get(url, headers=_openrouter_headers(), timeout=10)
        if not resp.ok:
            print(f"[LLM] Failed to fetch OpenRouter providers for {clean_model}: {resp.status_code}")
            return list(cached[1]) if cached else []

        endpoints = ((resp.json() or {}).get('data') or {}).get('endpoints') or []
        providers = []
        seen = set()
        for endpoint in endpoints:
            provider = _normalize_openrouter_endpoint_provider(endpoint)
            if not provider:
                continue
            key = str(provider['value']).lower()
            if key in seen:
                continue
            seen.add(key)
            providers.append(provider)

        _assign_openrouter_provider_badges(providers)
        providers.sort(key=_provider_sort_key)
        _cleanup_openrouter_provider_sort_fields(providers)
        _openrouter_provider_metadata_cache[clean_model] = (now, providers)
        return list(providers)
    except Exception as e:
        print(f"[LLM] Failed to fetch OpenRouter providers for {clean_model}: {e}")
        return list(cached[1]) if cached else []


def _get_nested_setting_value(settings: dict, section: str, key: str, default=None):
    value = settings.get(section, {})
    for part in key.split('.'):
        if not isinstance(value, dict) or part not in value:
            return default
        value = value[part]
    return value


def _normalize_provider_order(value) -> list:
    if isinstance(value, str):
        providers = [part.strip() for part in value.split(',')]
    elif isinstance(value, list):
        providers = [str(part).strip() for part in value if part is not None]
    else:
        providers = []
    seen = set()
    result = []
    for provider in providers:
        if not provider:
            continue
        key = provider.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(provider)
    return result


def get_openrouter_provider_params(context: str = "chat") -> Dict[str, Any]:
    """Return OpenRouter provider routing params for a call context."""
    from utils.settings import OPENROUTER_PROVIDER_CONTEXT_SETTINGS

    if context not in OPENROUTER_PROVIDER_CONTEXT_SETTINGS:
        return {}

    settings = load_settings()
    section, key = OPENROUTER_PROVIDER_CONTEXT_SETTINGS[context]
    providers = _normalize_provider_order(_get_nested_setting_value(settings, section, key, []))
    if not providers:
        return {}

    allow_fallbacks = settings.get('llm', {}).get('openrouter', {}).get('allow_provider_fallbacks', True)
    return {"provider": {"order": providers, "allow_fallbacks": allow_fallbacks is not False}}


def _strip_openrouter_modifier(model: str) -> str:
    return model.split(':', 1)[0] if isinstance(model, str) and ':' in model else model


def _resolve_openrouter_model(model: str, context: str = None) -> tuple:
    """Resolve OpenRouter model ID and provider routing.
    Returns (request_model, extra_body dict)."""
    user_provider_params = get_openrouter_provider_params(context) if context else {}
    if user_provider_params:
        return _strip_openrouter_modifier(model), user_provider_params
    return model, {}


def _get_provider():
    """Get the current LLM provider from settings"""
    settings = load_settings()
    return settings.get('llm', {}).get('provider', 'gemini')


def _get_api_key(provider: str) -> str:
    """Get API key for the selected provider.

    Order:
    1. Provider-specific key
    2. Legacy shared key (only if no provider-specific keys exist at all)
    3. Provider-specific environment variable
    """
    settings = load_settings()
    llm_settings = settings.get('llm', {})

    # Try provider-specific key first
    provider_key = llm_settings.get(provider, {}).get('api_key', '')
    if provider_key:
        return provider_key

    # Fallback to legacy shared key
    legacy_key = llm_settings.get('api_key', '')
    if legacy_key:
        # Only use legacy key when no provider-specific keys are configured.
        # This avoids accidentally reusing (for example) a Gemini key for OpenRouter.
        has_provider_specific_keys = any(
            llm_settings.get(p, {}).get('api_key', '')
            for p in ('gemini', 'openrouter', 'openai', 'ollama', 'llamacpp')
        )
        if not has_provider_specific_keys:
            return legacy_key

    # Fallback to environment variables
    env_vars = {
        'gemini': 'GEMINI_API_KEY',
        'openrouter': 'OPENROUTER_API_KEY',
        'openai': 'OPENAI_API_KEY',
        'ollama': 'OLLAMA_API_KEY',
        'llamacpp': 'LLAMACPP_API_KEY'
    }
    return os.getenv(env_vars.get(provider, ''), '')


def _create_gemini_client():
    """Create a Gemini client using google-genai"""
    if not GEMINI_AVAILABLE:
        print("[LLM] Gemini not available - google-genai package not installed")
        return None

    api_key = _get_api_key('gemini')

    if not api_key:
        print("[LLM] Warning: No Gemini API key configured")
        return None

    return genai.Client(api_key=api_key)


def _get_llamacpp_api_url(llm_settings: Dict[str, Any]) -> str:
    """Return llama.cpp OpenAI-compatible base URL, accepting root or /v1."""
    api_url = llm_settings.get('llamacpp', {}).get('api_url', '').strip() or "http://127.0.0.1:8080/v1"
    api_url = api_url.rstrip('/')
    if not api_url.lower().endswith('/v1'):
        api_url = f"{api_url}/v1"
    return api_url


def _get_ollama_chat_url(llm_settings: Dict[str, Any]) -> str:
    """Return Ollama chat endpoint, accepting root, /api, or /api/chat."""
    api_url = llm_settings.get('ollama', {}).get('api_url', '').strip() or "https://ollama.com/api/chat"
    api_url = api_url.rstrip('/')
    lower = api_url.lower()
    if lower.endswith('/api/chat'):
        return api_url
    if lower.endswith('/api'):
        return f"{api_url}/chat"
    return f"{api_url}/api/chat"


def _get_ollama_headers(api_url: str) -> Optional[Dict[str, str]]:
    """Build Ollama headers. Cloud requires a bearer token; local endpoints may not."""
    api_key = _get_api_key('ollama')
    if not api_key and 'ollama.com' in api_url.lower():
        print("[LLM] Warning: No Ollama API key configured")
        return None

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _get_llamacpp_slot_cache(settings: Dict[str, Any] = None):
    """Return cached llama.cpp slot-cache helper configured from settings."""
    global _llamacpp_slot_cache, _llamacpp_slot_cache_key
    settings = settings or load_settings()
    llama_settings = settings.get('llm', {}).get('llamacpp', {})
    api_url = _get_llamacpp_api_url(settings.get('llm', {}))
    api_key = _get_api_key('llamacpp')
    cache_key = (
        api_url,
        api_key,
        bool(llama_settings.get('kv_cache_enabled', True)),
        int(llama_settings.get('kv_cache_max_entries', 10) or 10),
        llama_settings.get('kv_cache_slot_save_path', '') or '',
    )
    if _llamacpp_slot_cache is None or _llamacpp_slot_cache_key != cache_key:
        from utils.llamacpp_slot_cache import LlamaCppSlotCache
        _llamacpp_slot_cache = LlamaCppSlotCache(
            api_url=api_url,
            api_key=api_key,
            enabled=llama_settings.get('kv_cache_enabled', True),
            max_entries=llama_settings.get('kv_cache_max_entries', 10),
            slot_save_path=llama_settings.get('kv_cache_slot_save_path') or None,
        )
        _llamacpp_slot_cache_key = cache_key
    return _llamacpp_slot_cache


def _get_llamacpp_request_extra_body() -> Dict[str, Any]:
    """Force llama.cpp requests through slot 0 and enable prompt-cache reuse."""
    return {"id_slot": 0, "cache_prompt": True}


@contextmanager
def _llamacpp_slot_lease(context: str, timeout: float = 15.0, stale_after: float = 180.0):
    """Serialize llama.cpp slot-0 use without allowing an abandoned stream to wedge Sonorus."""
    global _llamacpp_slot_token, _llamacpp_slot_context, _llamacpp_slot_acquired_at
    token = uuid.uuid4().hex
    deadline = time.monotonic() + timeout
    acquired = False

    with _llamacpp_slot_condition:
        while _llamacpp_slot_token is not None:
            held_for = time.monotonic() - (_llamacpp_slot_acquired_at or time.monotonic())
            if held_for >= stale_after:
                print(
                    f"[LlamaCppSlot] Stealing stale slot lease from {_llamacpp_slot_context} "
                    f"after {held_for:.1f}s"
                )
                break

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"Timed out waiting for llama.cpp slot 0 lease held by {_llamacpp_slot_context} "
                    f"for {held_for:.1f}s"
                )
            _llamacpp_slot_condition.wait(timeout=min(remaining, 1.0))

        _llamacpp_slot_token = token
        _llamacpp_slot_context = context
        _llamacpp_slot_acquired_at = time.monotonic()
        acquired = True

    try:
        yield
    finally:
        if acquired:
            with _llamacpp_slot_condition:
                if _llamacpp_slot_token == token:
                    _llamacpp_slot_token = None
                    _llamacpp_slot_context = None
                    _llamacpp_slot_acquired_at = None
                    _llamacpp_slot_condition.notify_all()


def _create_client():
    """Create a fresh OpenAI client configured for the selected LLM provider"""
    settings = load_settings()
    llm_settings = settings.get('llm', {})
    provider = llm_settings.get('provider', 'gemini')

    openai_api_url = llm_settings.get('openai', {}).get('api_url', '').strip() or "https://api.openai.com/v1"
    llamacpp_api_url = _get_llamacpp_api_url(llm_settings)

    # Get provider-specific API key
    api_key = _get_api_key(provider)

    if not api_key:
        if provider == 'openai' and openai_api_url != "https://api.openai.com/v1":
            api_key = "lm-studio"
        elif provider == 'llamacpp':
            api_key = "llama-cpp"
        else:
            print(f"[LLM] Warning: No API key configured for {provider}")
            return None

    # Extend httpx keepalive so idle connections survive between turns.
    # SDK default is 5s which defeats connection pooling entirely.
    # Expiry must be >> ping interval (45s) so connections aren't dropped
    # before our keepalive timer can exercise them.
    # Timeout: 30s connect, 120s between chunks (read).
    # read must be generous: local models (e.g. Gemma 26B) can take 60s+
    # on prefill before emitting the first token.
    http_client = DefaultHttpxClient(
        timeout=httpx.Timeout(connect=30.0, read=120.0, write=30.0, pool=30.0),
        limits=httpx.Limits(
            max_connections=1000,
            max_keepalive_connections=100,
            keepalive_expiry=300,
        )
    )

    # Configure client based on provider
    if provider == 'openai':
        return OpenAI(api_key=api_key, base_url=openai_api_url, http_client=http_client)
    elif provider == 'ollama':
        return None
    elif provider == 'llamacpp':
        return OpenAI(api_key=api_key, base_url=llamacpp_api_url, http_client=http_client)
    else:
        # Default to OpenRouter
        return OpenAI(
            api_key=api_key,
            base_url="https://openrouter.ai/api/v1",
            http_client=http_client,
        )


def _use_responses_api() -> bool:
    """Check if the OpenAI Responses API should be used.

    This follows the OpenAI provider toggle directly for both the default OpenAI
    endpoint and custom OpenAI-compatible endpoints.
    """
    settings = load_settings()
    if settings.get('llm', {}).get('provider', 'gemini') != 'openai':
        return False
    openai_settings = settings.get('llm', {}).get('openai', {})
    return openai_settings.get('responses_api', False)


def _get_openai_extra_params(model: str) -> Dict[str, Any]:
    """
    Get extra parameters for OpenAI provider (non-reasoning params).
    Reasoning is now handled via get_reasoning_params().
    Returns empty dict for non-OpenAI providers.
    """
    provider = _get_provider()
    if provider != 'openai':
        return {}

    # For models that support it, disable store (don't save for training)
    return {'store': False}


def _get_openai_token_param(max_tokens: int, use_responses: bool) -> Dict[str, int]:
    """
    Return the correct token limit field for the current OpenAI API mode.

    - Responses API uses `max_output_tokens`
    - Chat Completions uses `max_completion_tokens` when Responses mode is enabled
      for the provider (native OpenAI), otherwise `max_tokens` for compatibility
      with custom OpenAI-style endpoints.
    """
    if use_responses:
        return {"max_output_tokens": max_tokens}

    if _use_responses_api():
        return {"max_completion_tokens": max_tokens}

    return {"max_tokens": max_tokens}


def _normalize_model_name(model: str) -> str:
    """Normalize a model ID for capability/compatibility checks."""
    if not model:
        return ""

    normalized = model.strip().lower().split(':', 1)[0]
    if '/' in normalized:
        normalized = normalized.split('/')[-1]
    return normalized


def _model_supports_temperature(model: str) -> bool:
    """
    GPT-5 family models do not accept the temperature parameter.
    """
    return not _normalize_model_name(model).startswith('gpt-5')


def _get_temperature_param(model: str, temperature: float) -> Dict[str, float]:
    """Return temperature param only for model families that support it."""
    if _model_supports_temperature(model):
        return {"temperature": temperature}
    return {}


def _usage_field(usage: Any, field: str, default: Any = None) -> Any:
    """Read a usage field from either an SDK object or a dict-like payload."""
    if usage is None:
        return default
    if isinstance(usage, dict):
        return usage.get(field, default)
    return getattr(usage, field, default)


def _first_usage_field(usage: Any, *fields: str):
    for field in fields:
        value = _usage_field(usage, field)
        if value is not None:
            return value
    return None


def _extract_openrouter_usage_metrics(usage: Any) -> Dict[str, Any]:
    """Extract OpenRouter token and cost metrics from a usage payload when present."""
    if usage is None:
        return {
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "reasoning_tokens": None,
            "cost_total": None,
            "cost_upstream_inference": None,
        }

    cost_details = _usage_field(usage, 'cost_details')
    cost_total = _usage_field(usage, 'cost')
    cost_upstream_inference = _usage_field(cost_details, 'upstream_inference_cost')
    if cost_total == 0 and cost_upstream_inference not in (None, 0):
        cost_total = cost_upstream_inference
    completion_details = _first_usage_field(usage, 'completion_tokens_details', 'completion_details', 'output_tokens_details')
    reasoning_tokens = _first_usage_field(completion_details, 'reasoning_tokens', 'reasoning') if completion_details else None
    if reasoning_tokens is None:
        reasoning_tokens = _first_usage_field(usage, 'reasoning_tokens', 'reasoning')

    return {
        "input_tokens": _usage_field(usage, 'prompt_tokens'),
        "output_tokens": _usage_field(usage, 'completion_tokens'),
        "total_tokens": _usage_field(usage, 'total_tokens'),
        "reasoning_tokens": reasoning_tokens,
        "cost_total": cost_total,
        "cost_upstream_inference": cost_upstream_inference,
    }


def _safe_positive_int(value) -> Optional[int]:
    try:
        parsed = int(float(value))
        return parsed if parsed > 0 else None
    except (TypeError, ValueError):
        return None


def _extract_openrouter_message_reasoning(response: Any) -> Dict[str, Any]:
    """Extract message-level OpenRouter reasoning fields when providers return them."""
    empty_result = {
        "has_reasoning": False,
        "reasoning_text_chars": None,
        "reasoning_details_count": None,
        "reasoning_signature_details_count": None,
    }
    choices = _usage_field(response, 'choices') or []
    if not choices:
        return empty_result

    first_choice = choices[0]
    message = _usage_field(first_choice, 'message')
    if message is None:
        return empty_result

    reasoning_text = _usage_field(message, 'reasoning')
    reasoning_details = _usage_field(message, 'reasoning_details')
    reasoning_text_chars = len(reasoning_text) if isinstance(reasoning_text, str) and reasoning_text else None

    reasoning_details_count = None
    reasoning_signature_details_count = None
    if isinstance(reasoning_details, list) and reasoning_details:
        text_details = []
        signature_details = []
        for detail in reasoning_details:
            if not isinstance(detail, dict):
                continue
            detail_text = detail.get('text') or detail.get('content') or detail.get('data')
            if isinstance(detail_text, str) and detail_text.strip():
                text_details.append(detail)
            elif detail.get('signature'):
                signature_details.append(detail)
        reasoning_details_count = len(text_details) if text_details else None
        reasoning_signature_details_count = len(signature_details) if signature_details else None

    return {
        "has_reasoning": bool(reasoning_text_chars or reasoning_details_count),
        "reasoning_text_chars": reasoning_text_chars,
        "reasoning_details_count": reasoning_details_count,
        "reasoning_signature_details_count": reasoning_signature_details_count,
    }


def _estimate_visible_response_tokens(text: str) -> int:
    stripped = (text or "").strip()
    if not stripped:
        return 0
    # A conservative approximation used only to flag wildly disproportionate outputs.
    return max(1, (len(stripped) + 3) // 4)


def _infer_openrouter_hidden_reasoning_tokens(
    usage_metrics: Dict[str, Any],
    reasoning_request,
    response_text: Optional[str] = None,
) -> Optional[int]:
    if not _openrouter_reasoning_request_is_off(reasoning_request):
        return None
    if _safe_positive_int((usage_metrics or {}).get('reasoning_tokens')):
        return None

    output_tokens = _safe_positive_int((usage_metrics or {}).get('output_tokens'))
    if not output_tokens or response_text is None:
        return None

    visible_tokens = _estimate_visible_response_tokens(response_text)
    threshold = max(64, visible_tokens * 4 + 32)
    if output_tokens < threshold:
        return None
    return max(1, output_tokens - visible_tokens)


def _openrouter_reasoning_request_is_off(reasoning_request) -> bool:
    if reasoning_request is None:
        return True
    if not isinstance(reasoning_request, dict):
        return True
    if reasoning_request.get('enabled') is False:
        return True
    if reasoning_request.get('max_tokens') == 0:
        return True
    if str(reasoning_request.get('effort', '')).lower() in ('minimal', 'none', 'off', 'disabled'):
        return True
    return False


def _openrouter_reasoning_warning(
    usage_metrics: Dict[str, Any],
    reasoning_request,
    message_reasoning: Optional[Dict[str, Any]] = None,
    response_text: Optional[str] = None,
) -> Optional[str]:
    if not _openrouter_reasoning_request_is_off(reasoning_request):
        return None

    reasoning_tokens = _safe_positive_int((usage_metrics or {}).get('reasoning_tokens'))
    if reasoning_tokens:
        return (
            f"OpenRouter reported {reasoning_tokens} reasoning tokens even though Sonorus did not enable reasoning. "
            "This provider may ignore the reasoning toggle."
        )

    message_reasoning = message_reasoning or {}
    if message_reasoning.get("has_reasoning"):
        details = []
        if message_reasoning.get("reasoning_text_chars"):
            details.append(f"{message_reasoning['reasoning_text_chars']} reasoning chars")
        if message_reasoning.get("reasoning_details_count"):
            details.append(f"{message_reasoning['reasoning_details_count']} reasoning detail block(s)")
        suffix = f" ({', '.join(details)})" if details else ""
        return (
            f"OpenRouter returned message-level reasoning{suffix} even though Sonorus did not enable reasoning. "
            "This provider may ignore the reasoning toggle."
        )

    inferred_tokens = _infer_openrouter_hidden_reasoning_tokens(usage_metrics, reasoning_request, response_text)
    if inferred_tokens:
        usage_metrics["reasoning_tokens"] = inferred_tokens
        usage_metrics["reasoning_tokens_inferred"] = True
        return (
            f"OpenRouter reported {usage_metrics.get('output_tokens')} output tokens for a tiny visible response while "
            f"Sonorus did not enable reasoning; about {inferred_tokens} tokens look like hidden reasoning."
        )

    return None


def _format_openrouter_response_summary(
    text: str,
    usage_metrics: Optional[Dict[str, Any]] = None,
    response_provider: Optional[str] = None,
    response_model: Optional[str] = None,
) -> str:
    parts = [f"{len(text or '')} chars"]
    usage_metrics = usage_metrics or {}
    token_parts = []
    for key, label in (
        ("input_tokens", "in"),
        ("output_tokens", "out"),
        ("reasoning_tokens", "reasoning"),
        ("total_tokens", "total"),
    ):
        value = usage_metrics.get(key)
        if value is not None:
            suffix = "~" if key == "reasoning_tokens" and usage_metrics.get("reasoning_tokens_inferred") else ""
            token_parts.append(f"{label}={suffix}{value}")
    if token_parts:
        parts.append("tokens " + " ".join(token_parts))
    if response_provider:
        parts.append(f"provider={response_provider}")
    if response_model:
        parts.append(f"response_model={response_model}")
    return ", ".join(parts)


def _extract_openrouter_response_provider(response: Any) -> Optional[str]:
    provider = _usage_field(response, 'provider')
    return str(provider) if provider else None


def _extract_openrouter_response_model(response: Any) -> Optional[str]:
    response_model = _usage_field(response, 'model')
    return str(response_model) if response_model else None


def _build_openrouter_log_metadata(
    response: Any = None,
    usage_metrics: Optional[Dict[str, Any]] = None,
    reasoning_request: Any = None,
    reasoning_warning: Optional[str] = None,
    response_provider: Optional[str] = None,
    response_model: Optional[str] = None,
) -> Dict[str, Any]:
    usage_metrics = usage_metrics or {}
    provider = response_provider or _extract_openrouter_response_provider(response)
    model = response_model or _extract_openrouter_response_model(response)
    return {
        "provider_used": provider,
        "response_model": model,
        "input_tokens": usage_metrics.get("input_tokens"),
        "output_tokens": usage_metrics.get("output_tokens"),
        "total_tokens": usage_metrics.get("total_tokens"),
        "reasoning_tokens": usage_metrics.get("reasoning_tokens"),
        "reasoning_tokens_inferred": usage_metrics.get("reasoning_tokens_inferred"),
        "reasoning_text_chars": usage_metrics.get("reasoning_text_chars"),
        "reasoning_details_count": usage_metrics.get("reasoning_details_count"),
        "reasoning_signature_details_count": usage_metrics.get("reasoning_signature_details_count"),
        "cost_total": usage_metrics.get("cost_total"),
        "cost_upstream_inference": usage_metrics.get("cost_upstream_inference"),
        "reasoning_requested": reasoning_request,
        "warning": reasoning_warning,
    }


def _extract_gemini_usage_metrics(usage: Any) -> Dict[str, Any]:
    """Extract Gemini token metrics from usage metadata when present."""
    if usage is None:
        return {
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
        }

    return {
        "input_tokens": _usage_field(usage, 'prompt_token_count'),
        "output_tokens": _usage_field(usage, 'candidates_token_count'),
        "total_tokens": _usage_field(usage, 'total_token_count'),
    }


def _convert_openai_message_content_to_responses(content: Any) -> Any:
    """
    Convert chat-style multimodal message content to Responses API content parts.

    Chat Completions vision messages use `text` / `image_url`.
    Responses API expects `input_text` / `input_image`.
    """
    if not isinstance(content, list):
        return content

    converted = []
    for part in content:
        if not isinstance(part, dict):
            converted.append(part)
            continue

        part_type = part.get('type')
        if part_type == 'text':
            converted.append({
                "type": "input_text",
                "text": part.get('text', '')
            })
        elif part_type == 'image_url':
            image_url = part.get('image_url')
            if isinstance(image_url, dict):
                image_url = image_url.get('url', '')
            converted.append({
                "type": "input_image",
                "image_url": image_url or ''
            })
        else:
            converted.append(part)

    return converted


def _convert_openai_messages_to_responses_input(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert OpenAI chat-style messages to Responses API input items."""
    input_messages = []
    for msg in messages:
        role = msg.get('role', 'user')
        content = _convert_openai_message_content_to_responses(msg.get('content', ''))

        if role == 'system':
            role = 'developer'

        input_messages.append({
            "role": role,
            "content": content
        })

    return input_messages


# =============================================================================
# Provider-specific reasoning formatters
# =============================================================================

_OPENROUTER_DEFAULT_REASONING_EFFORT = "medium"


def _normalize_openrouter_reasoning_metadata(model: str) -> Dict[str, Any]:
    caps = _get_model_capability(model) or {}
    reasoning = caps.get('reasoning')
    if not isinstance(reasoning, dict):
        reasoning = ((caps.get('full_data') or {}).get('reasoning') or {})
    if not isinstance(reasoning, dict):
        reasoning = {}

    supported_efforts = reasoning.get('supported_efforts')
    if isinstance(supported_efforts, list):
        supported_efforts = [
            str(effort).lower()
            for effort in supported_efforts
            if effort is not None and str(effort).strip()
        ]
    elif supported_efforts is None:
        supported_efforts = None
    else:
        supported_efforts = []

    default_effort = reasoning.get('default_effort')
    if default_effort is not None:
        default_effort = str(default_effort).lower()

    return {
        "mandatory": reasoning.get('mandatory') is True,
        "default_enabled": reasoning.get('default_enabled'),
        "supported_efforts": supported_efforts,
        "default_effort": default_effort,
        "supports_max_tokens": reasoning.get('supports_max_tokens') is True,
        "raw": reasoning,
    }


def _openrouter_effort_supported(effort: str, supported_efforts) -> bool:
    return supported_efforts is None or effort in supported_efforts


def _choose_openrouter_enabled_effort(reasoning_meta: Dict[str, Any]) -> Optional[str]:
    supported_efforts = reasoning_meta.get("supported_efforts")
    default_effort = reasoning_meta.get("default_effort")

    if default_effort and default_effort != "none" and _openrouter_effort_supported(default_effort, supported_efforts):
        return default_effort

    if isinstance(supported_efforts, list) and supported_efforts:
        non_none = [effort for effort in supported_efforts if effort != "none"]
        if _OPENROUTER_DEFAULT_REASONING_EFFORT in non_none:
            return _OPENROUTER_DEFAULT_REASONING_EFFORT
        return non_none[-1] if non_none else None

    if supported_efforts is None:
        return _OPENROUTER_DEFAULT_REASONING_EFFORT

    return None


def _choose_openrouter_lowest_effort(reasoning_meta: Dict[str, Any]) -> Optional[str]:
    supported_efforts = reasoning_meta.get("supported_efforts")
    if isinstance(supported_efforts, list) and supported_efforts:
        non_none = [effort for effort in supported_efforts if effort != "none"]
        return non_none[-1] if non_none else "none"
    if supported_efforts is None:
        return "minimal"
    return None


def _openrouter_reasoning_budget(max_tokens: int, enabled: bool) -> int:
    max_tokens = max(1, int(max_tokens or 1))
    if not enabled:
        return 1
    budget = int(max_tokens * 0.5)
    return max(1, min(budget, max_tokens - 1 if max_tokens > 1 else 1))


def _format_reasoning_openrouter(model: str, max_tokens: int, enabled: bool) -> Dict[str, Any]:
    """Format reasoning params for OpenRouter using per-model metadata."""
    reasoning_meta = _normalize_openrouter_reasoning_metadata(model)

    if enabled:
        if reasoning_meta["supports_max_tokens"]:
            return {"reasoning": {"max_tokens": _openrouter_reasoning_budget(max_tokens, enabled=True)}}

        effort = _choose_openrouter_enabled_effort(reasoning_meta)
        if effort:
            return {"reasoning": {"effort": effort}}

        return {"reasoning": {"enabled": True}}

    if not reasoning_meta["mandatory"]:
        return {"reasoning": {"enabled": False}}

    if reasoning_meta["supports_max_tokens"]:
        return {"reasoning": {"max_tokens": _openrouter_reasoning_budget(max_tokens, enabled=False)}}

    effort = _choose_openrouter_lowest_effort(reasoning_meta)
    if effort:
        return {"reasoning": {"effort": effort}}

    return {"reasoning": {"enabled": True}}


def _format_reasoning_gemini(model: str, max_tokens: int, enabled: bool) -> Dict[str, Any]:
    """Format reasoning params for native Gemini API

    Returns thinking_config dict to be passed to GenerateContentConfig.
    - Gemini 3+: uses thinking_level ("minimal", "low", "medium", "high")
    - Gemini 2.x: uses thinking_budget (0 = off, higher = more tokens)
    """
    model_lower = model.lower()

    # Gemini 3+ uses thinking_level (future-proofed for 4, 5, etc.)
    if re.search(r'gemini-?[3-9]', model_lower):
        if enabled:
            return {"thinking_level": "medium"}
        else:
            return {"thinking_level": "minimal"}

    # Gemini 2.x and earlier use thinking_budget
    # Minimum is 512, maximum is 24576
    if enabled:
        budget = max(512, min(max_tokens // 2, 24576))
        return {"thinking_budget": budget}
    else:
        return {"thinking_budget": 0}


def _format_reasoning_openai(model: str, max_tokens: int, enabled: bool) -> Dict[str, Any]:
    """Format reasoning params for native OpenAI API (responses.create)

    Reasoning models use reasoning={"effort": "..."} parameter.
    - "low": minimal reasoning, faster responses
    - "medium": balanced (default)
    - "high": deep reasoning for complex tasks
    """
    if enabled:
        return {"reasoning": {"effort": "medium"}}
    else:
        # Explicitly set low effort to minimize reasoning when disabled
        return {"reasoning": {"effort": "low"}}


def get_reasoning_params(provider: str, model: str, max_tokens: int, context: str = "chat") -> Dict[str, Any]:
    """Get reasoning params for any provider (unified router)

    Always returns proper reasoning params (enabled or disabled format).
    OpenRouter uses cached per-model metadata to choose the safest shape.

    Checks both master toggle and per-model toggle:
    - Master OFF → reasoning disabled
    - Master ON + per-model OFF → reasoning disabled
    - Master ON + per-model ON + model supports → reasoning enabled

    Args:
        provider: LLM provider (gemini, openrouter, openai)
        model: Model ID
        max_tokens: Max tokens for response
        context: Usage context - maps to per-model reasoning setting (see REASONING_CONTEXT_SETTINGS)
    """
    from utils.settings import REASONING_CONTEXT_SETTINGS

    settings = load_settings()

    # Determine if reasoning should be enabled
    # 1. Check master toggle (provider-level reasoning_enabled)
    master_enabled = settings.get('llm', {}).get(provider, {}).get('reasoning_enabled', True)

    # 2. Check per-model setting (see REASONING_CONTEXT_SETTINGS in utils/settings.py)
    per_model_enabled = False
    if context in REASONING_CONTEXT_SETTINGS:
        section, key = REASONING_CONTEXT_SETTINGS[context]
        per_model_enabled = settings.get(section, {}).get(key, False)
    # Unknown contexts don't get reasoning (must be explicitly configured)

    # Final enabled state: both master AND per-model must be on
    enabled = master_enabled and per_model_enabled

    # OpenAI without Responses API cannot use reasoning params
    if provider == 'openai' and not _use_responses_api():
        return {}

    if provider == 'openrouter':
        _ensure_model_capabilities_loaded()
        caps = _get_model_capability(model)
        if caps is not None and not caps.get('supports_reasoning'):
            return {}
        if caps is None and enabled:
            return {}
    elif not supports_reasoning(model):
        return {}

    # Always call format functions - they return proper "off" values when disabled
    result = {}
    if provider == 'openrouter':
        result = _format_reasoning_openrouter(model, max_tokens, enabled)
    elif provider == 'gemini':
        result = _format_reasoning_gemini(model, max_tokens, enabled)
    elif provider == 'openai':
        result = _format_reasoning_openai(model, max_tokens, enabled)

    if result:
        status = "enabled" if enabled else "disabled"
        print(f"[LLM] Reasoning {status} for {model} ({context}): {result}")

    return result


def is_reasoning_enabled(model: str, context: str) -> bool:
    """Check if reasoning will be enabled for a model/context combo.

    Used to adjust max_tokens before making LLM calls.
    """
    from utils.settings import REASONING_CONTEXT_SETTINGS

    # Must support reasoning
    if not supports_reasoning(model):
        return False

    settings = load_settings()
    provider = _get_provider()

    # Check master toggle
    master_enabled = settings.get('llm', {}).get(provider, {}).get('reasoning_enabled', True)
    if not master_enabled:
        return False

    # Check per-model setting
    if context in REASONING_CONTEXT_SETTINGS:
        section, key = REASONING_CONTEXT_SETTINGS[context]
        return settings.get(section, {}).get(key, False)

    return False


def adjust_max_tokens_for_reasoning(model: str, context: str, max_tokens: int) -> int:
    """Adjust max_tokens if reasoning is enabled for this model/context.

    Returns max_tokens bumped up to MIN_REASONING_TOKENS if reasoning is on
    and current value is lower.
    """
    if is_reasoning_enabled(model, context) and max_tokens < MIN_REASONING_TOKENS:
        print(f"[LLM] Reasoning enabled, adjusting max_tokens {max_tokens} -> {MIN_REASONING_TOKENS}")
        return MIN_REASONING_TOKENS
    return max_tokens


def _convert_messages_to_gemini(messages):
    """Convert OpenAI-style messages to Gemini format.
    Handles both plain text content and multipart content (text + images)."""
    system_instruction = None
    contents = []

    for msg in messages:
        role = msg.get('role', 'user')
        content = msg.get('content', '')

        if role == 'system':
            system_instruction = content
            continue

        gemini_role = 'model' if role == 'assistant' else 'user'

        # Multipart content (list of text/image parts)
        if isinstance(content, list):
            parts = []
            for part in content:
                if part.get('type') == 'text':
                    parts.append(types.Part.from_text(text=part['text']))
                elif part.get('type') == 'image_url':
                    url = part['image_url']['url']
                    # Extract base64 data from data URI
                    if url.startswith('data:'):
                        # "data:image/jpeg;base64,<data>"
                        header, b64_data = url.split(',', 1)
                        mime_type = header.split(':')[1].split(';')[0]
                        image_bytes = base64.b64decode(b64_data)
                        parts.append(types.Part.from_bytes(data=image_bytes, mime_type=mime_type))
            contents.append(types.Content(role=gemini_role, parts=parts))
        else:
            # Plain text content
            contents.append(types.Content(role=gemini_role, parts=[types.Part.from_text(text=content)]))

    return system_instruction, contents


def _chat_gemini(messages: List[Dict[str, Any]],
                 model: str,
                 temperature: float,
                 max_tokens: int,
                 context: str) -> Optional[str]:
    """Send chat request using Google Gemini API"""
    client = _create_gemini_client()
    if not client:
        return None

    try:
        start_time = time.time()

        # Convert OpenAI-style messages to Gemini format
        system_instruction, contents = _convert_messages_to_gemini(messages)

        # Get reasoning config for Gemini (pass context for per-model settings)
        reasoning_params = get_reasoning_params('gemini', model, max_tokens, context)

        # Build thinking config if reasoning params exist
        thinking_config = None
        if reasoning_params:
            thinking_config = types.ThinkingConfig(**reasoning_params)

        # Build generation config
        config = types.GenerateContentConfig(
            temperature=temperature,
            max_output_tokens=max_tokens,
            system_instruction=system_instruction,
            thinking_config=thinking_config
        )

        response = client.models.generate_content(
            model=model,
            contents=contents,
            config=config
        )
        duration_ms = (time.time() - start_time) * 1000

        result_text = (response.text or "").strip()

        # Log to file
        payload = {"model": model, "temperature": temperature, "max_tokens": max_tokens, "messages": messages}
        log_llm(payload, response=result_text)

        # Log event
        el = _get_event_logger()
        if el:
            usage = response.usage_metadata
            el.log_llm_event(
                model=model,
                context=context,
                input_tokens=usage.prompt_token_count if usage else None,
                output_tokens=usage.candidates_token_count if usage else None,
                total_tokens=usage.total_token_count if usage else None,
                duration_ms=duration_ms
            )

        return result_text

    except Exception as e:
        print(f"[LLM] Gemini error: {e}")
        friendly_error = _parse_llm_error(e)
        _set_last_error(friendly_error)
        payload = {"model": model, "temperature": temperature, "max_tokens": max_tokens, "messages": messages}
        log_llm(payload, error=str(e))
        el = _get_event_logger()
        if el:
            el.log_llm_event(model=model, context=context, status="error", error=friendly_error)
        return None


def _chat_openai(messages: List[Dict[str, Any]],
                 model: str,
                 temperature: float,
                 max_tokens: int,
                 context: str) -> Optional[str]:
    """Send chat request using OpenAI API (responses or chat completions)"""
    client = _get_client()
    if not client:
        return None

    use_responses = _use_responses_api()

    try:
        start_time = time.time()
        api_mode = "responses" if use_responses else "chat.completions"
        print(f"[LLM] Request: {model} ({context}), {api_mode} API, max_tokens={max_tokens}")

        if use_responses:
            # --- Responses API path ---
            input_messages = _convert_openai_messages_to_responses_input(messages)

            request_params = {
                "model": model,
                "input": input_messages,
                **_get_openai_token_param(max_tokens, use_responses=True),
            }

            reasoning_params = get_reasoning_params('openai', model, max_tokens, context)
            if reasoning_params:
                request_params.update(reasoning_params)

            response = client.responses.create(**request_params)
            duration_ms = (time.time() - start_time) * 1000

            if getattr(response, 'status', None) == 'incomplete':
                details = getattr(response, 'incomplete_details', None)
                reason = getattr(details, 'reason', 'unknown') if details else 'unknown'
                print(f"[LLM] Response incomplete: {reason}")

            result_text = (response.output_text or "").strip()

            usage = getattr(response, 'usage', None)
            input_tokens = usage.input_tokens if usage else None
            output_tokens = usage.output_tokens if usage else None
            total_tokens = (usage.input_tokens + usage.output_tokens) if usage else None

        else:
            # --- Chat Completions API path (standard OpenAI-compatible) ---
            request_params = {
                "model": model,
                "messages": messages,
                **_get_temperature_param(model, temperature),
                **_get_openai_token_param(max_tokens, use_responses=False),
            }
            if _get_provider() == 'llamacpp':
                request_params["extra_body"] = _get_llamacpp_request_extra_body()

            response = client.chat.completions.create(**request_params)
            duration_ms = (time.time() - start_time) * 1000

            content = None
            if response.choices and response.choices[0].message:
                content = response.choices[0].message.content
            result_text = (content or "").strip()

            usage = response.usage if hasattr(response, 'usage') else None
            input_tokens = usage.prompt_tokens if usage else None
            output_tokens = usage.completion_tokens if usage else None
            total_tokens = usage.total_tokens if usage else None

        # --- Common: handle empty response, logging ---
        if not result_text:
            error_detail = f"Empty response from {api_mode} API"
            print(f"[LLM] {error_detail} from {model}")
            payload = {"model": model, "temperature": temperature, "max_tokens": max_tokens, "messages": messages}
            log_llm(payload, error=error_detail)
            el = _get_event_logger()
            if el:
                el.log_llm_event(model=model, context=context, status="error", error=error_detail)
            return None

        payload = {"model": model, "temperature": temperature, "max_tokens": max_tokens, "messages": messages}
        log_llm(payload, response=result_text)

        el = _get_event_logger()
        if el:
            el.log_llm_event(
                model=model,
                context=context,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                duration_ms=duration_ms
            )

        print(f"[LLM] Response: {model} ({len(result_text)} chars, {duration_ms:.0f}ms)")
        return result_text

    except Exception as e:
        print(f"[LLM] Error from {model}: {e}")
        friendly_error = _parse_llm_error(e)
        _set_last_error(friendly_error)
        payload = {"model": model, "temperature": temperature, "max_tokens": max_tokens, "messages": messages}
        log_llm(payload, error=str(e))
        el = _get_event_logger()
        if el:
            el.log_llm_event(model=model, context=context, status="error", error=friendly_error)
        return None


def _chat_ollama(messages: List[Dict[str, Any]],
                 model: str,
                 temperature: float,
                 max_tokens: int,
                 context: str) -> Optional[str]:
    """Send chat request using Ollama's native /api/chat endpoint."""
    settings = load_settings()
    api_url = _get_ollama_chat_url(settings.get('llm', {}))
    headers = _get_ollama_headers(api_url)
    if headers is None:
        return None

    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_predict": max_tokens,
        },
    }

    try:
        start_time = time.time()
        print(f"[LLM] Ollama request: {model} ({context}), max_tokens={max_tokens}")

        with httpx.Client(timeout=httpx.Timeout(connect=30.0, read=120.0, write=30.0, pool=30.0)) as client:
            response = client.post(api_url, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()

        duration_ms = (time.time() - start_time) * 1000
        content = (data.get('message') or {}).get('content') or data.get('response') or ''
        result_text = content.strip()

        if not result_text:
            error_detail = "Empty response from Ollama API"
            print(f"[LLM] {error_detail} from {model}")
            log_llm(payload, error=error_detail)
            el = _get_event_logger()
            if el:
                el.log_llm_event(model=model, context=context, status="error", error=error_detail)
            return None

        log_llm(payload, response=result_text)

        input_tokens = data.get('prompt_eval_count')
        output_tokens = data.get('eval_count')
        total_tokens = (
            input_tokens + output_tokens
            if input_tokens is not None and output_tokens is not None
            else None
        )
        el = _get_event_logger()
        if el:
            el.log_llm_event(
                model=model,
                context=context,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                duration_ms=duration_ms
            )

        print(f"[LLM] Ollama response: {model} ({len(result_text)} chars, {duration_ms:.0f}ms)")
        return result_text

    except Exception as e:
        print(f"[LLM] Ollama error from {model}: {e}")
        friendly_error = _parse_llm_error(e)
        _set_last_error(friendly_error)
        log_llm(payload, error=str(e))
        el = _get_event_logger()
        if el:
            el.log_llm_event(model=model, context=context, status="error", error=friendly_error)
        return None


def chat(messages: List[Dict[str, Any]],
         model: str = None,
         temperature: float = 0.8,
         max_tokens: int = 8192,
         context: str = "chat",
         kv_cache_prefix: Any = None,
         kv_cache_context: str = None) -> Optional[str]:
    """
    Send a chat completion request to the configured LLM provider.

    Args:
        messages: List of message dicts with role/content
        model: Model ID (default from settings)
        temperature: Sampling temperature
        max_tokens: Max response tokens
        context: Context for logging ("chat", "target_selection", "interjection", "vision", "sentiment")

    Returns:
        Response text or None on failure (check get_last_error() for details)
    """
    _set_last_error(None)  # Clear any stale error
    _set_last_response_metadata(None)
    settings = load_settings()
    provider = _get_provider()

    # Model should always be provided by caller, but default to the chat model
    model = model or settings.get('conversation', {}).get('chat_model', GEMINI_CHAT_DEFAULT)

    # Adjust max_tokens if reasoning is enabled (thinking needs more tokens)
    max_tokens = adjust_max_tokens_for_reasoning(model, context, max_tokens)

    # Route to provider-specific implementations
    if provider == 'gemini':
        return _chat_gemini(messages, model, temperature, max_tokens, context)

    if provider == 'llamacpp':
        try:
            with _llamacpp_slot_lease(context):
                cache = _get_llamacpp_slot_cache(settings)
                if kv_cache_prefix is not None:
                    restored = cache.restore(kv_cache_prefix, model, kv_cache_context or context)
                    if restored.hit:
                        print(f"[LlamaCppKV] Restored {restored.filename} for {kv_cache_context or context}")
                result = _chat_openai(messages, model, temperature, max_tokens, context)
                if result and kv_cache_prefix is not None:
                    saved = cache.save(kv_cache_prefix, model, kv_cache_context or context)
                    if saved.success:
                        evicted = f", evicted={saved.evicted}" if saved.evicted else ""
                        print(f"[LlamaCppKV] Saved {saved.filename} for {kv_cache_context or context}{evicted}")
                return result
        except TimeoutError as e:
            print(f"[LlamaCppSlot] {e}")
            _set_last_error(str(e))
            return None

    if provider == 'openai':
        return _chat_openai(messages, model, temperature, max_tokens, context)

    if provider == 'ollama':
        return _chat_ollama(messages, model, temperature, max_tokens, context)

    # OpenRouter path (uses chat.completions API with extra_body for reasoning)
    t_entry = time.perf_counter()
    client = _get_client()
    if not client:
        return None
    t_client = time.perf_counter()

    try:
        request_model, extra_body = _resolve_openrouter_model(model, context)
        if request_model != model:
            print(f"[LLM] Request: {model} -> {request_model} with provider routing ({context})")
        else:
            print(f"[LLM] Request: {model} ({context})")

        # Build request parameters
        request_params = {
            "model": request_model,
            "messages": messages,
            **_get_temperature_param(request_model, temperature),
            "max_tokens": max_tokens,
            "extra_headers": {
                "HTTP-Referer": "https://sonorus.github.io/",
                "X-Title": "Sonorus (Hogwarts Legacy Mod)"
            }
        }

        # Add reasoning params for OpenRouter (uses extra_body)
        reasoning_params = get_reasoning_params('openrouter', request_model, max_tokens, context)
        if reasoning_params:
            extra_body.update(reasoning_params)
        extra_body.setdefault('usage', {'include': True})

        if extra_body:
            request_params['extra_body'] = extra_body

        t_pre = time.perf_counter()
        response = client.chat.completions.create(**request_params)
        t_post = time.perf_counter()

        # Check for empty response
        content = None
        if response.choices and response.choices[0].message:
            content = response.choices[0].message.content

        if not content:
            # Log full response for debugging
            error_detail = "Empty response"
            if hasattr(response, 'choices') and response.choices:
                choice = response.choices[0]
                finish_reason = getattr(choice, 'finish_reason', None)
                error_detail = f"Empty content (finish_reason={finish_reason})"
                # Check for error in message
                if hasattr(choice, 'message'):
                    msg = choice.message
                    if hasattr(msg, 'refusal') and msg.refusal:
                        error_detail = f"Refusal: {msg.refusal}"
            # Check for error field in response
            if hasattr(response, 'error') and response.error:
                error_detail = f"API error: {response.error}"

            print(f"[LLM] {error_detail} from {model}")
            print(f"[LLM] Full response: {response}")
            payload = {"model": model, "temperature": temperature, "max_tokens": max_tokens, "messages": messages}
            log_llm(payload, error=error_detail)
            el = _get_event_logger()
            if el:
                el.log_llm_event(model=model, context=context, status="error", error=error_detail)
            return None

        result_text = content.strip()

        # Log event with token counts and latency
        duration_ms = (t_post - t_pre) * 1000
        usage_metrics = _extract_openrouter_usage_metrics(getattr(response, 'usage', None))
        reasoning_request = reasoning_params.get("reasoning") if reasoning_params else None
        message_reasoning = _extract_openrouter_message_reasoning(response)
        usage_metrics["reasoning_text_chars"] = message_reasoning.get("reasoning_text_chars")
        usage_metrics["reasoning_details_count"] = message_reasoning.get("reasoning_details_count")
        usage_metrics["reasoning_signature_details_count"] = message_reasoning.get("reasoning_signature_details_count")
        reasoning_warning = _openrouter_reasoning_warning(
            usage_metrics,
            reasoning_request,
            message_reasoning=message_reasoning,
            response_text=result_text,
        )
        response_provider = _extract_openrouter_response_provider(response)
        response_model = _extract_openrouter_response_model(response)
        log_metadata = _build_openrouter_log_metadata(
            response=response,
            usage_metrics=usage_metrics,
            reasoning_request=reasoning_request,
            reasoning_warning=reasoning_warning,
            response_provider=response_provider,
            response_model=response_model,
        )
        _set_last_response_metadata({
            "provider": "openrouter",
            "provider_used": response_provider,
            "model": model,
            "response_model": response_model,
            "request_model": request_model,
            "context": context,
            "usage": usage_metrics,
            "reasoning_requested": reasoning_request,
            "warning": reasoning_warning,
        })
        payload = {"model": model, "temperature": temperature, "max_tokens": max_tokens, "messages": messages}
        log_llm(payload, response=result_text, metadata=log_metadata)
        el = _get_event_logger()
        if el:
            el.log_llm_event(
                model=model,
                context=context,
                input_tokens=usage_metrics["input_tokens"],
                output_tokens=usage_metrics["output_tokens"],
                total_tokens=usage_metrics["total_tokens"],
                reasoning_tokens=usage_metrics["reasoning_tokens"],
                cost_total=usage_metrics["cost_total"],
                cost_upstream_inference=usage_metrics["cost_upstream_inference"],
                provider_used=response_provider,
                response_model=response_model,
                duration_ms=duration_ms,
                status="warning" if reasoning_warning else "success",
                warning=reasoning_warning,
            )

        # Profiling: break down where time went
        client_ms = (t_client - t_entry) * 1000
        build_ms = (t_pre - t_client) * 1000
        net_ms = (t_post - t_pre) * 1000
        or_latency = or_gen = or_tokens = None
        if hasattr(response, 'usage') and response.usage:
            u = response.usage
            or_tokens = u.completion_tokens
            # OpenRouter extended fields (may not exist on all responses)
            or_latency = getattr(u, 'latency_ms', None)
            or_gen = getattr(u, 'generation_time', None)
            if or_latency is None and hasattr(response, '_raw_response'):
                # Try response headers
                try:
                    headers = response._raw_response.headers
                    or_latency = headers.get('x-latency-ms')
                except Exception:
                    pass

        profile = f"client={client_ms:.0f}ms build={build_ms:.0f}ms net={net_ms:.0f}ms"
        if or_latency or or_gen:
            profile += f" (OR: latency={or_latency}ms gen={or_gen}ms)"
        summary = _format_openrouter_response_summary(
            result_text,
            usage_metrics=usage_metrics,
            response_provider=response_provider,
            response_model=response_model,
        )
        print(f"[LLM] Response: {model} ({summary}, {net_ms:.0f}ms) [{profile}]")
        if reasoning_warning:
            print(f"[LLM] Warning: {reasoning_warning}")
        return result_text

    except Exception as e:
        print(f"[LLM] Error from {model}: {e}")
        friendly_error = _parse_llm_error(e)
        _set_last_error(friendly_error)
        payload = {"model": model, "temperature": temperature, "max_tokens": max_tokens, "messages": messages}
        log_llm(payload, error=str(e))
        # Log error event
        el = _get_event_logger()
        if el:
            el.log_llm_event(model=model, context=context, status="error", error=friendly_error)
        return None


def chat_stream(messages: List[Dict[str, Any]],
                model: str = None,
                temperature: float = 0.8,
                max_tokens: int = 8192,
                context: str = "chat",
                kv_cache_prefix: Any = None,
                kv_cache_context: str = None):
    """
    Stream a chat completion, yielding text chunks as they arrive.

    Supports OpenRouter, OpenAI, and Gemini providers with streaming.
    The full accumulated response is logged after streaming completes.

    Args:
        messages: List of message dicts with role/content
        model: Model ID (default from settings)
        temperature: Sampling temperature
        max_tokens: Max response tokens
        context: Context for logging

    Yields:
        str: Text chunks as they arrive from the LLM

    Note:
        If streaming fails or isn't supported, falls back to non-streaming
        and yields the complete response as a single chunk.
    """
    _set_last_error(None)
    settings = load_settings()
    provider = _get_provider()
    model = model or settings.get('conversation', {}).get('chat_model', GEMINI_CHAT_DEFAULT_OR)
    max_tokens = adjust_max_tokens_for_reasoning(model, context, max_tokens)

    print(f"[LLM] Request (streaming): {model} ({context})")

    try:
        if provider == 'gemini':
            yield from _chat_stream_gemini(messages, model, temperature, max_tokens, context)
        elif provider == 'llamacpp':
            try:
                with _llamacpp_slot_lease(context):
                    cache = _get_llamacpp_slot_cache(settings)
                    if kv_cache_prefix is not None:
                        restored = cache.restore(kv_cache_prefix, model, kv_cache_context or context)
                        if restored.hit:
                            print(f"[LlamaCppKV] Restored {restored.filename} for {kv_cache_context or context}")
                    yielded_any = False
                    completed = False
                    try:
                        for chunk in _chat_stream_openai(messages, model, temperature, max_tokens, context):
                            yielded_any = True
                            yield chunk
                        completed = True
                    finally:
                        if completed and yielded_any and kv_cache_prefix is not None:
                            saved = cache.save(kv_cache_prefix, model, kv_cache_context or context)
                            if saved.success:
                                evicted = f", evicted={saved.evicted}" if saved.evicted else ""
                                print(f"[LlamaCppKV] Saved {saved.filename} for {kv_cache_context or context}{evicted}")
            except TimeoutError as e:
                print(f"[LlamaCppSlot] {e}")
                _set_last_error(str(e))
                return
        elif provider == 'openai':
            yield from _chat_stream_openai(messages, model, temperature, max_tokens, context)
        elif provider == 'ollama':
            yield from _chat_stream_ollama(messages, model, temperature, max_tokens, context)
        else:
            # OpenRouter - uses OpenAI-compatible streaming
            yield from _chat_stream_openrouter(messages, model, temperature, max_tokens, context)
    except Exception as e:
        print(f"[LLM] Streaming error, falling back to non-streaming: {e}")
        # Fallback: use non-streaming and yield complete response
        result = chat(messages, model=model, temperature=temperature,
                      max_tokens=max_tokens, context=context)
        if result:
            yield result


def _chat_stream_openrouter(messages, model, temperature, max_tokens, context):
    """Stream via OpenRouter (OpenAI-compatible API)."""
    client = _get_client()
    if not client:
        return

    start_time = time.time()
    request_model, extra_body = _resolve_openrouter_model(model, context)

    request_params = {
        "model": request_model,
        "messages": messages,
        **_get_temperature_param(request_model, temperature),
        "max_tokens": max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
        "extra_headers": {
            "HTTP-Referer": "https://sonorus.github.io/",
            "X-Title": "Sonorus (Hogwarts Legacy Mod)"
        }
    }

    reasoning_params = get_reasoning_params('openrouter', request_model, max_tokens, context)
    if reasoning_params:
        extra_body.update(reasoning_params)
    extra_body.setdefault('usage', {'include': True})
    if extra_body:
        request_params['extra_body'] = extra_body

    accumulated = []
    usage = None
    response_provider = None
    response_model = None
    error_occurred = False
    try:
        stream = client.chat.completions.create(**request_params)
        for chunk in stream:
            response_provider = _extract_openrouter_response_provider(chunk) or response_provider
            response_model = _extract_openrouter_response_model(chunk) or response_model
            if getattr(chunk, 'usage', None):
                usage = chunk.usage
            if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                text = chunk.choices[0].delta.content
                accumulated.append(text)
                yield text

    except Exception as e:
        error_occurred = True
        print(f"[LLM] OpenRouter streaming error: {e}")
        friendly_error = _parse_llm_error(e)
        _set_last_error(friendly_error)
        el = _get_event_logger()
        if el:
            el.log_llm_event(model=model, context=context, status="error", error=friendly_error)
    finally:
        # Log even when the consumer abandons the stream early (e.g. target selection)
        if not error_occurred:
            duration_ms = (time.time() - start_time) * 1000
            full_response = "".join(accumulated)

            if full_response:
                payload = {"model": model, "temperature": temperature, "max_tokens": max_tokens, "messages": messages}
                usage_metrics = _extract_openrouter_usage_metrics(usage)
                reasoning_request = reasoning_params.get("reasoning") if reasoning_params else None
                reasoning_warning = _openrouter_reasoning_warning(
                    usage_metrics,
                    reasoning_request,
                    response_text=full_response,
                )
                log_metadata = _build_openrouter_log_metadata(
                    usage_metrics=usage_metrics,
                    reasoning_request=reasoning_request,
                    reasoning_warning=reasoning_warning,
                    response_provider=response_provider,
                    response_model=response_model,
                )
                log_llm(payload, response=full_response, metadata=log_metadata)
                summary = _format_openrouter_response_summary(
                    full_response,
                    usage_metrics=usage_metrics,
                    response_provider=response_provider,
                    response_model=response_model,
                )
                print(f"[LLM] Response (streamed): {model} ({summary}, {duration_ms:.0f}ms)")
                if reasoning_warning:
                    print(f"[LLM] Warning: {reasoning_warning}")

                el = _get_event_logger()
                if el:
                    el.log_llm_event(
                        model=model,
                        context=context,
                        duration_ms=duration_ms,
                        input_tokens=usage_metrics["input_tokens"],
                        output_tokens=usage_metrics["output_tokens"],
                        total_tokens=usage_metrics["total_tokens"],
                        reasoning_tokens=usage_metrics["reasoning_tokens"],
                        cost_total=usage_metrics["cost_total"],
                        cost_upstream_inference=usage_metrics["cost_upstream_inference"],
                        provider_used=response_provider,
                        response_model=response_model,
                        status="warning" if reasoning_warning else "success",
                        warning=reasoning_warning,
                    )
            else:
                print(f"[LLM] Empty streaming response from {model}")


def _chat_stream_gemini(messages, model, temperature, max_tokens, context):
    """Stream via Google Gemini API."""
    if not GEMINI_AVAILABLE:
        # Fallback to non-streaming
        result = _chat_gemini(messages, model, temperature, max_tokens, context)
        if result:
            yield result
        return

    client = _create_gemini_client()
    if not client:
        return

    start_time = time.time()

    # Convert messages to Gemini format (handles multipart content including images)
    system_instruction, contents = _convert_messages_to_gemini(messages)

    reasoning_params = get_reasoning_params('gemini', model, max_tokens, context)
    thinking_config = types.ThinkingConfig(**reasoning_params) if reasoning_params else None

    config = types.GenerateContentConfig(
        temperature=temperature,
        max_output_tokens=max_tokens,
        system_instruction=system_instruction,
        thinking_config=thinking_config
    )

    accumulated = []
    usage = None
    error_occurred = False
    try:
        stream = client.models.generate_content_stream(
            model=model,
            contents=contents,
            config=config
        )
        for chunk in stream:
            if getattr(chunk, 'usage_metadata', None):
                usage = chunk.usage_metadata
            if chunk.text:
                accumulated.append(chunk.text)
                yield chunk.text

    except Exception as e:
        error_occurred = True
        print(f"[LLM] Gemini streaming error: {e}")
        friendly_error = _parse_llm_error(e)
        _set_last_error(friendly_error)
        el = _get_event_logger()
        if el:
            el.log_llm_event(model=model, context=context, status="error", error=friendly_error)
    finally:
        if not error_occurred:
            duration_ms = (time.time() - start_time) * 1000
            full_response = "".join(accumulated)

            if full_response:
                payload = {"model": model, "temperature": temperature, "max_tokens": max_tokens, "messages": messages}
                log_llm(payload, response=full_response)
                print(f"[LLM] Response (streamed): {model} ({len(full_response)} chars, {duration_ms:.0f}ms)")

                el = _get_event_logger()
                if el:
                    usage_metrics = _extract_gemini_usage_metrics(usage)
                    el.log_llm_event(
                        model=model,
                        context=context,
                        duration_ms=duration_ms,
                        input_tokens=usage_metrics["input_tokens"],
                        output_tokens=usage_metrics["output_tokens"],
                        total_tokens=usage_metrics["total_tokens"],
                    )


def _chat_stream_openai(messages, model, temperature, max_tokens, context):
    """Stream via OpenAI API (responses or chat completions)."""
    client = _get_client()
    if not client:
        return

    start_time = time.time()
    use_responses = _use_responses_api()
    api_mode = "responses" if use_responses else "chat.completions"

    accumulated = []
    usage = None
    error_occurred = False
    try:
        if use_responses:
            input_messages = _convert_openai_messages_to_responses_input(messages)

            request_params = {
                "model": model,
                "input": input_messages,
                **_get_openai_token_param(max_tokens, use_responses=True),
                **_get_temperature_param(model, temperature),
            }

            reasoning_params = get_reasoning_params('openai', model, max_tokens, context)
            if reasoning_params:
                request_params.update(reasoning_params)

            with client.responses.stream(**request_params) as stream:
                for event in stream:
                    if event.type == "response.output_text.delta" and getattr(event, 'delta', None):
                        text = event.delta
                        accumulated.append(text)
                        yield text

                final_response = stream.get_final_response()
                usage = getattr(final_response, 'usage', None)
        else:
            request_params = {
                "model": model,
                "messages": messages,
                **_get_temperature_param(model, temperature),
                **_get_openai_token_param(max_tokens, use_responses=False),
                "stream": True,
            }
            if _get_provider() != 'llamacpp':
                request_params["stream_options"] = {"include_usage": True}
            else:
                request_params["extra_body"] = _get_llamacpp_request_extra_body()

            stream = client.chat.completions.create(**request_params)
            for chunk in stream:
                if getattr(chunk, 'usage', None):
                    usage = chunk.usage
                if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                    text = chunk.choices[0].delta.content
                    accumulated.append(text)
                    yield text

    except Exception as e:
        error_occurred = True
        print(f"[LLM] OpenAI streaming error: {e}")
        friendly_error = _parse_llm_error(e)
        _set_last_error(friendly_error)
        el = _get_event_logger()
        if el:
            el.log_llm_event(model=model, context=context, status="error", error=friendly_error)
    finally:
        if not error_occurred:
            duration_ms = (time.time() - start_time) * 1000
            full_response = "".join(accumulated)

            if full_response:
                payload = {"model": model, "temperature": temperature, "max_tokens": max_tokens, "messages": messages}
                log_llm(payload, response=full_response)
                print(f"[LLM] Response (streamed): {model} via {api_mode} ({len(full_response)} chars, {duration_ms:.0f}ms)")

                el = _get_event_logger()
                if el:
                    input_tokens = getattr(usage, 'input_tokens', None) if usage else None
                    output_tokens = getattr(usage, 'output_tokens', None) if usage else None
                    total_tokens = (input_tokens + output_tokens) if usage and input_tokens is not None and output_tokens is not None else None
                    el.log_llm_event(
                        model=model,
                        context=context,
                        duration_ms=duration_ms,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        total_tokens=total_tokens,
                    )


def _chat_stream_ollama(messages, model, temperature, max_tokens, context):
    """Stream via Ollama's native /api/chat endpoint."""
    settings = load_settings()
    api_url = _get_ollama_chat_url(settings.get('llm', {}))
    headers = _get_ollama_headers(api_url)
    if headers is None:
        return

    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "options": {
            "temperature": temperature,
            "num_predict": max_tokens,
        },
    }

    start_time = time.time()
    accumulated = []
    usage = {}
    error_occurred = False

    try:
        with httpx.stream(
            "POST",
            api_url,
            headers=headers,
            json=payload,
            timeout=httpx.Timeout(connect=30.0, read=120.0, write=30.0, pool=30.0),
        ) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line:
                    continue
                data = json.loads(line)
                usage = data
                content = (data.get('message') or {}).get('content') or ''
                if content:
                    accumulated.append(content)
                    yield content
                if data.get('done') is True:
                    break

    except Exception as e:
        error_occurred = True
        print(f"[LLM] Ollama streaming error: {e}")
        friendly_error = _parse_llm_error(e)
        _set_last_error(friendly_error)
        el = _get_event_logger()
        if el:
            el.log_llm_event(model=model, context=context, status="error", error=friendly_error)
    finally:
        if not error_occurred:
            duration_ms = (time.time() - start_time) * 1000
            full_response = "".join(accumulated)

            if full_response:
                log_llm(payload, response=full_response)
                print(f"[LLM] Ollama response (streamed): {model} ({len(full_response)} chars, {duration_ms:.0f}ms)")

                input_tokens = usage.get('prompt_eval_count')
                output_tokens = usage.get('eval_count')
                total_tokens = (
                    input_tokens + output_tokens
                    if input_tokens is not None and output_tokens is not None
                    else None
                )
                el = _get_event_logger()
                if el:
                    el.log_llm_event(
                        model=model,
                        context=context,
                        duration_ms=duration_ms,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        total_tokens=total_tokens,
                    )
            else:
                print(f"[LLM] Empty Ollama streaming response from {model}")


def chat_simple(prompt: str, system: str = None,
                model: str = None, temperature: float = 0.8,
                max_tokens: int = 8192, context: str = "chat") -> Optional[str]:
    """
    Simple chat with prompt string (convenience wrapper).

    Args:
        prompt: User message
        system: System message (optional)
        model: Model ID
        temperature: Sampling temperature
        max_tokens: Max response tokens
        context: Context for logging ("chat", "target_selection", "interjection", "vision", "sentiment")

    Returns:
        Response text or None on failure
    """
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    return chat(messages, model=model, temperature=temperature, max_tokens=max_tokens, context=context)


def test_embedding(model: str = None, text: str = "Sonorus embedding setup test") -> Optional[Dict[str, Any]]:
    """Test the configured memory embedding route for the active provider."""
    _set_last_error(None)
    settings = load_settings()
    memory_settings = settings.get('memory', {})
    model = model or memory_settings.get('embedding_model') or 'text-embedding-3-small'

    try:
        start_time = time.time()
        from cognis.embeddings.gemini import OpenAICompatibleEmbedder

        embedder = OpenAICompatibleEmbedder(model=model)
        result = embedder.embed_query(text)
        vectors = getattr(result, 'embeddings', {}) or {}
        if not vectors:
            raise ValueError("No embeddings returned")

        vector = next(iter(vectors.values()))
        dimensions = len(vector) if vector is not None else 0
        if dimensions <= 0:
            raise ValueError("Empty embedding vector returned")

        duration_ms = (time.time() - start_time) * 1000
        print(f"[LLM] Embedding test: {model} ({dimensions} dims, {duration_ms:.0f}ms)")
        return {
            "model": model,
            "dimensions": dimensions,
            "duration_ms": duration_ms,
        }

    except Exception as e:
        print(f"[LLM] Embedding test error from {model}: {e}")
        friendly_error = _parse_llm_error(e)
        _set_last_error(friendly_error)
        return None


def _chat_with_vision_gemini(prompt: str, image_b64: str,
                              model: str, temperature: float,
                              max_tokens: int) -> Optional[str]:
    """Vision chat using Google Gemini API"""
    client = _create_gemini_client()
    if not client:
        return None

    try:
        start_time = time.time()

        # Decode base64 image
        image_bytes = base64.b64decode(image_b64)

        # Build content with text and image
        contents = [
            types.Content(
                role='user',
                parts=[
                    types.Part.from_text(text=prompt),
                    types.Part.from_bytes(data=image_bytes, mime_type='image/jpeg')
                ]
            )
        ]

        # Get reasoning config for Gemini (vision context for per-model settings)
        reasoning_params = get_reasoning_params('gemini', model, max_tokens, 'vision')

        # Build thinking config if reasoning params exist
        thinking_config = None
        if reasoning_params:
            thinking_config = types.ThinkingConfig(**reasoning_params)

        config = types.GenerateContentConfig(
            temperature=temperature,
            max_output_tokens=max_tokens,
            thinking_config=thinking_config
        )

        response = client.models.generate_content(
            model=model,
            contents=contents,
            config=config
        )
        duration_ms = (time.time() - start_time) * 1000

        result_text = (response.text or "").strip()

        # Log to file (vision prompt as user message, note image was included)
        messages = [{"role": "user", "content": f"[Vision request with image]\n\n{prompt}"}]
        payload = {"model": model, "temperature": temperature, "max_tokens": max_tokens, "messages": messages}
        log_llm(payload, response=result_text)

        # Log event
        el = _get_event_logger()
        if el:
            usage = response.usage_metadata
            el.log_llm_event(
                model=model,
                context="vision",
                input_tokens=usage.prompt_token_count if usage else None,
                output_tokens=usage.candidates_token_count if usage else None,
                total_tokens=usage.total_token_count if usage else None,
                duration_ms=duration_ms
            )

        return result_text

    except Exception as e:
        print(f"[LLM] Gemini vision error: {e}")
        friendly_error = _parse_llm_error(e)
        _set_last_error(friendly_error)
        messages = [{"role": "user", "content": f"[Vision request with image]\n\n{prompt}"}]
        payload = {"model": model, "temperature": temperature, "max_tokens": max_tokens, "messages": messages}
        log_llm(payload, error=str(e))
        el = _get_event_logger()
        if el:
            el.log_llm_event(model=model, context="vision", status="error", error=friendly_error)
        return None


def _chat_with_vision_openai(prompt: str, image_b64: str,
                              model: str, temperature: float,
                              max_tokens: int) -> Optional[str]:
    """Vision chat using OpenAI API (responses or chat completions)"""
    client = _get_client()
    if not client:
        return None

    use_responses = _use_responses_api()

    try:
        start_time = time.time()
        api_mode = "responses" if use_responses else "chat.completions"
        print(f"[LLM] Vision request: {model} ({api_mode} API)")

        if use_responses:
            # --- Responses API path ---
            input_content = [
                {"type": "input_text", "text": prompt},
                {"type": "input_image", "image_url": f"data:image/jpeg;base64,{image_b64}"}
            ]

            request_params = {
                "model": model,
                "input": [{"role": "user", "content": input_content}],
                **_get_openai_token_param(max_tokens, use_responses=True),
            }

            reasoning_params = get_reasoning_params('openai', model, max_tokens, 'vision')
            if reasoning_params:
                request_params.update(reasoning_params)

            response = client.responses.create(**request_params)
            duration_ms = (time.time() - start_time) * 1000

            if getattr(response, 'status', None) == 'incomplete':
                details = getattr(response, 'incomplete_details', None)
                reason = getattr(details, 'reason', 'unknown') if details else 'unknown'
                print(f"[LLM] Vision response incomplete: {reason}")

            result_text = (response.output_text or "").strip()

            usage = getattr(response, 'usage', None)
            input_tokens = usage.input_tokens if usage else None
            output_tokens = usage.output_tokens if usage else None
            total_tokens = (usage.input_tokens + usage.output_tokens) if usage else None

        else:
            # --- Chat Completions API path (standard vision format) ---
            vision_messages = [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}}
                ]
            }]

            request_params = {
                "model": model,
                "messages": vision_messages,
                **_get_temperature_param(model, temperature),
                **_get_openai_token_param(max_tokens, use_responses=False),
            }
            if _get_provider() == 'llamacpp':
                request_params["extra_body"] = _get_llamacpp_request_extra_body()

            response = client.chat.completions.create(**request_params)
            duration_ms = (time.time() - start_time) * 1000

            content = None
            if response.choices and response.choices[0].message:
                content = response.choices[0].message.content
            result_text = (content or "").strip()

            usage = response.usage if hasattr(response, 'usage') else None
            input_tokens = usage.prompt_tokens if usage else None
            output_tokens = usage.completion_tokens if usage else None
            total_tokens = usage.total_tokens if usage else None

        # --- Common: handle empty response, logging ---
        if not result_text:
            error_detail = f"Empty vision response from {api_mode} API"
            print(f"[LLM] {error_detail} from {model}")
            log_messages = [{"role": "user", "content": f"[Vision request with image]\n\n{prompt}"}]
            payload = {"model": model, "temperature": temperature, "max_tokens": max_tokens, "messages": log_messages}
            log_llm(payload, error=error_detail)
            el = _get_event_logger()
            if el:
                el.log_llm_event(model=model, context="vision", status="error", error=error_detail)
            return None

        log_messages = [{"role": "user", "content": f"[Vision request with image]\n\n{prompt}"}]
        payload = {"model": model, "temperature": temperature, "max_tokens": max_tokens, "messages": log_messages}
        log_llm(payload, response=result_text)

        el = _get_event_logger()
        if el:
            el.log_llm_event(
                model=model,
                context="vision",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                duration_ms=duration_ms
            )

        print(f"[LLM] Vision response: {model} ({len(result_text)} chars, {duration_ms:.0f}ms)")
        return result_text

    except Exception as e:
        print(f"[LLM] OpenAI vision error: {e}")
        friendly_error = _parse_llm_error(e)
        _set_last_error(friendly_error)
        log_messages = [{"role": "user", "content": f"[Vision request with image]\n\n{prompt}"}]
        payload = {"model": model, "temperature": temperature, "max_tokens": max_tokens, "messages": log_messages}
        log_llm(payload, error=str(e))
        el = _get_event_logger()
        if el:
            el.log_llm_event(model=model, context="vision", status="error", error=friendly_error)
        return None


def chat_with_vision(prompt: str, image_b64: str,
                     model: str = None, temperature: float = 0.7,
                     max_tokens: int = 8192,
                     kv_cache_prefix: Any = None,
                     kv_cache_context: str = "vision") -> Optional[str]:
    """
    Vision-enabled chat completion with base64 image.

    Args:
        prompt: Text prompt
        image_b64: Base64-encoded image (JPEG or PNG)
        model: Vision model ID (default from settings)
        temperature: Sampling temperature
        max_tokens: Max response tokens

    Returns:
        Response text or None on failure
    """
    settings = load_settings()
    provider = _get_provider()
    model = model or settings.get('agents', {}).get('vision', {}).get('llm', {}).get('model', 'gemini-2.5-flash-lite')

    # Adjust max_tokens if reasoning is enabled (thinking needs more tokens)
    max_tokens = adjust_max_tokens_for_reasoning(model, 'vision', max_tokens)

    # Route to provider-specific implementations
    if provider == 'gemini':
        return _chat_with_vision_gemini(prompt, image_b64, model, temperature, max_tokens)

    if provider == 'llamacpp':
        try:
            with _llamacpp_slot_lease(kv_cache_context or 'vision'):
                cache = _get_llamacpp_slot_cache(settings)
                if kv_cache_prefix is not None:
                    restored = cache.restore(kv_cache_prefix, model, kv_cache_context or 'vision')
                    if restored.hit:
                        print(f"[LlamaCppKV] Restored {restored.filename} for {kv_cache_context or 'vision'}")
                result = _chat_with_vision_openai(prompt, image_b64, model, temperature, max_tokens)
                if result and kv_cache_prefix is not None:
                    saved = cache.save(kv_cache_prefix, model, kv_cache_context or 'vision')
                    if saved.success:
                        evicted = f", evicted={saved.evicted}" if saved.evicted else ""
                        print(f"[LlamaCppKV] Saved {saved.filename} for {kv_cache_context or 'vision'}{evicted}")
                return result
        except TimeoutError as e:
            print(f"[LlamaCppSlot] {e}")
            _set_last_error(str(e))
            return None

    if provider == 'openai':
        return _chat_with_vision_openai(prompt, image_b64, model, temperature, max_tokens)

    if provider == 'ollama':
        error_detail = "Vision is disabled for Ollama provider"
        print(f"[LLM] {error_detail}")
        _set_last_error(error_detail)
        return None

    # OpenRouter path (uses chat.completions API)
    client = _get_client()
    if not client:
        return None

    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}}
        ]
    }]

    try:
        start_time = time.time()

        request_model, extra_body = _resolve_openrouter_model(model, 'vision')

        # Build request parameters
        request_params = {
            "model": request_model,
            "messages": messages,
            **_get_temperature_param(request_model, temperature),
            "max_tokens": max_tokens,
            "extra_headers": {
                "HTTP-Referer": "https://sonorus.github.io/",
                "X-Title": "Sonorus (Hogwarts Legacy Mod)"
            }
        }

        # Add reasoning params for OpenRouter (uses extra_body)
        reasoning_params = get_reasoning_params('openrouter', request_model, max_tokens, 'vision')
        if reasoning_params:
            extra_body.update(reasoning_params)
        extra_body.setdefault('usage', {'include': True})

        if extra_body:
            request_params['extra_body'] = extra_body

        response = client.chat.completions.create(**request_params)
        duration_ms = (time.time() - start_time) * 1000

        result_text = response.choices[0].message.content.strip()

        # Log vision event with token counts and latency
        log_messages = [{"role": "user", "content": f"[Vision request with image]\n\n{prompt}"}]
        payload = {"model": model, "temperature": temperature, "max_tokens": max_tokens, "messages": log_messages}
        usage_metrics = _extract_openrouter_usage_metrics(getattr(response, 'usage', None))
        reasoning_request = reasoning_params.get("reasoning") if reasoning_params else None
        message_reasoning = _extract_openrouter_message_reasoning(response)
        usage_metrics["reasoning_text_chars"] = message_reasoning.get("reasoning_text_chars")
        usage_metrics["reasoning_details_count"] = message_reasoning.get("reasoning_details_count")
        usage_metrics["reasoning_signature_details_count"] = message_reasoning.get("reasoning_signature_details_count")
        reasoning_warning = _openrouter_reasoning_warning(
            usage_metrics,
            reasoning_request,
            message_reasoning=message_reasoning,
            response_text=result_text,
        )
        response_provider = _extract_openrouter_response_provider(response)
        response_model = _extract_openrouter_response_model(response)
        log_metadata = _build_openrouter_log_metadata(
            response=response,
            usage_metrics=usage_metrics,
            reasoning_request=reasoning_request,
            reasoning_warning=reasoning_warning,
            response_provider=response_provider,
            response_model=response_model,
        )
        log_llm(payload, response=result_text, metadata=log_metadata)

        el = _get_event_logger()
        if el:
            el.log_llm_event(
                model=model,
                context="vision",
                input_tokens=usage_metrics["input_tokens"],
                output_tokens=usage_metrics["output_tokens"],
                total_tokens=usage_metrics["total_tokens"],
                reasoning_tokens=usage_metrics["reasoning_tokens"],
                cost_total=usage_metrics["cost_total"],
                cost_upstream_inference=usage_metrics["cost_upstream_inference"],
                provider_used=response_provider,
                response_model=response_model,
                duration_ms=duration_ms,
                status="warning" if reasoning_warning else "success",
                warning=reasoning_warning,
            )

        return result_text

    except Exception as e:
        print(f"[LLM] Vision error: {e}")
        friendly_error = _parse_llm_error(e)
        _set_last_error(friendly_error)
        log_messages = [{"role": "user", "content": f"[Vision request with image]\n\n{prompt}"}]
        payload = {"model": model, "temperature": temperature, "max_tokens": max_tokens, "messages": log_messages}
        log_llm(payload, error=str(e))
        # Log error event
        el = _get_event_logger()
        if el:
            el.log_llm_event(model=model, context="vision", status="error", error=friendly_error)
        return None


if __name__ == "__main__":
    import sys

    settings = load_settings()
    provider = settings.get('llm', {}).get('provider', 'gemini')
    api_key = _get_api_key(provider)
    chat_model = settings.get('conversation', {}).get('chat_model', GEMINI_CHAT_DEFAULT)

    if len(sys.argv) < 2:
        print("Usage: python llm.py <prompt>")
        print(f"\nConfiguration:")
        print(f"  Provider: {provider}")
        print(f"  API Key: {'configured' if api_key else 'not set'}")
        print(f"  Chat Model: {chat_model}")
        print(f"  Gemini Available: {GEMINI_AVAILABLE}")
        sys.exit(0)

    prompt = " ".join(sys.argv[1:])
    result = chat_simple(prompt)
    print(result)
