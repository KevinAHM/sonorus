"""
LLM utility for multiple providers (Gemini, OpenRouter, OpenAI).
Single module for all LLM operations - text and vision.
"""
import base64
import json
import os
import re
import threading
import time
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


def get_last_error() -> Optional[str]:
    """Get the last error message from a failed LLM call."""
    return _last_error


def _set_last_error(error: Optional[str]):
    """Set the last error message."""
    global _last_error
    _last_error = error


def _parse_llm_error(error: Exception) -> str:
    """
    Parse LLM API errors into user-friendly messages for the event log.
    Works for Gemini, OpenRouter, and OpenAI errors.

    Known error patterns:
    - 429 RESOURCE_EXHAUSTED (free quota) -> "free quota exhausted"
    - 503 UNAVAILABLE (overloaded) -> "model overloaded"
    - 400 INVALID_ARGUMENT (bad API key) -> "api key not valid"
    - 401 (auth) -> "api key not valid"
    - 400 "not a valid model ID" -> "invalid model id"
    - Otherwise: extract the 'message' field or return as-is
    """
    error_str = str(error)

    # Check for known error codes/patterns
    if '429' in error_str:
        if 'RESOURCE_EXHAUSTED' in error_str:
            if 'free_tier' in error_str.lower() or 'quota' in error_str.lower():
                return "daily free quota exhausted"
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
from utils.settings import DATA_DIR
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

# --- Client connection pooling ---
_client_lock = threading.Lock()
_cached_client = None           # OpenAI() instance
_cached_client_key = None       # (provider, api_key, base_url) for debug logging
_last_request_time = 0.0        # For idle detection
_keepalive_timer = None         # threading.Timer ref
_KEEPALIVE_INTERVAL = 45        # Seconds between keep-alive pings


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
        if client is None:
            return

    # Ping outside lock (client is thread-safe)
    idle = time.time() - _last_request_time
    if idle >= _KEEPALIVE_INTERVAL:
        try:
            # GET /key with auth — exercises TCP pool + auth/credit validation
            client._client.get(str(client.base_url).rstrip('/') + '/key')
        except Exception:
            pass  # httpx will reconnect on next real request

    # Reschedule if client still alive
    with _client_lock:
        if _cached_client is not None:
            _start_keepalive()


def invalidate_client():
    """Invalidate the cached client. Call when LLM settings change."""
    global _cached_client, _cached_client_key
    with _client_lock:
        old_client = _cached_client
        _cached_client = None
        _cached_client_key = None
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
    if provider == 'gemini':
        return  # Gemini uses its own client library

    def _warm():
        try:
            client = _get_client()
            if client:
                client._client.get(str(client.base_url).rstrip('/') + '/key')
                print(f"[LLM] {_cached_client_key} connection pre-warmed")
        except Exception as e:
            print(f"[LLM] Pre-warm ping failed (non-fatal): {e}")

    t = threading.Thread(target=_warm, daemon=True)
    t.start()


def fetch_model_capabilities():
    """Fetch OpenRouter model list and extract capabilities for all providers"""
    global _model_capabilities
    try:
        import requests
        resp = requests.get("https://openrouter.ai/api/v1/models", timeout=10)
        if resp.ok:
            for m in resp.json().get('data', []):
                full_id = m['id']
                supported = m.get('supported_parameters', [])

                # Strip OpenRouter modifiers (e.g., "model:nitro" -> "model")
                clean_id = full_id.split(':')[0]

                # Strip provider prefix (e.g., "openai/gpt-5-nano" -> "gpt-5-nano")
                base_name = clean_id.split('/', 1)[-1] if '/' in clean_id else clean_id

                # If duplicate base name, prefer the one that supports reasoning
                caps = {
                    'supports_reasoning': 'reasoning' in supported,
                    'full_id': full_id,
                    'full_data': m
                }
                if base_name in _model_capabilities:
                    if caps['supports_reasoning'] and not _model_capabilities[base_name]['supports_reasoning']:
                        _model_capabilities[base_name] = caps
                else:
                    _model_capabilities[base_name] = caps

            print(f"[LLM] Cached capabilities for {len(_model_capabilities)} models")
        else:
            print(f"[LLM] Failed to fetch model capabilities: {resp.status_code}")
    except Exception as e:
        print(f"[LLM] Failed to fetch model capabilities: {e}")


def supports_reasoning(model_id: str) -> bool:
    """Check if model supports reasoning (works for any provider)
    Strips OpenRouter modifiers and provider prefixes."""
    # Strip OpenRouter modifiers (e.g., "model:nitro" -> "model")
    clean_id = model_id.split(':')[0] if ':' in model_id else model_id

    # Strip provider prefix (e.g., "openai/gpt-5-nano" -> "gpt-5-nano")
    base_name = clean_id.split('/', 1)[-1] if '/' in clean_id else clean_id

    # Lookup by base name
    if base_name in _model_capabilities:
        return _model_capabilities[base_name]['supports_reasoning']
    return False


def get_model_capabilities_for_frontend() -> dict:
    """
    Return model capabilities for frontend use.
    Keys are already base model names (provider prefix stripped at fetch time).

    Returns dict like: {"gpt-4o": {"supports_reasoning": false, "full_id": "openai/gpt-4o"}, ...}
    """
    return {
        base_name: {
            'supports_reasoning': caps['supports_reasoning'],
            'full_id': caps.get('full_id', base_name)
        }
        for base_name, caps in _model_capabilities.items()
    }


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
            for p in ('gemini', 'openrouter', 'openai')
        )
        if not has_provider_specific_keys:
            return legacy_key

    # Fallback to environment variables
    env_vars = {
        'gemini': 'GEMINI_API_KEY',
        'openrouter': 'OPENROUTER_API_KEY',
        'openai': 'OPENAI_API_KEY'
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


def _create_client():
    """Create a fresh OpenAI client configured for the selected LLM provider"""
    settings = load_settings()
    llm_settings = settings.get('llm', {})
    provider = llm_settings.get('provider', 'gemini')

    api_url = llm_settings.get('openai', {}).get('api_url', '').strip() or "https://api.openai.com/v1"

    # Get provider-specific API key
    api_key = _get_api_key(provider)

    if not api_key:
        if provider == 'openai' and api_url != "https://api.openai.com/v1":
            api_key = "lm-studio"
        else:
            print(f"[LLM] Warning: No API key configured for {provider}")
            return None

    # Extend httpx keepalive so idle connections survive between turns.
    # SDK default is 5s which defeats connection pooling entirely.
    # Expiry must be >> ping interval (45s) so connections aren't dropped
    # before our keepalive timer can exercise them.
    # DefaultHttpxClient preserves SDK defaults (600s timeout, 1000 max conn, etc.)
    http_client = DefaultHttpxClient(
        limits=httpx.Limits(
            max_connections=1000,
            max_keepalive_connections=100,
            keepalive_expiry=300,
        )
    )

    # Configure client based on provider
    if provider == 'openai':
        return OpenAI(api_key=api_key, base_url=api_url, http_client=http_client)
    else:
        # Default to OpenRouter
        return OpenAI(
            api_key=api_key,
            base_url="https://openrouter.ai/api/v1",
            http_client=http_client,
        )


def _use_responses_api() -> bool:
    """Check if the OpenAI Responses API should be used.

    Returns True for default OpenAI endpoint, or when explicitly enabled for custom endpoints.
    Custom non-OpenAI endpoints default to Chat Completions API (responses_api=False).
    """
    settings = load_settings()
    openai_settings = settings.get('llm', {}).get('openai', {})
    api_url = (openai_settings.get('api_url', '') or '').strip()

    # Default OpenAI endpoint always uses responses API
    if not api_url or 'openai.com' in api_url.lower():
        return True

    # Custom endpoint: check the toggle
    return openai_settings.get('responses_api', True)


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


# =============================================================================
# Provider-specific reasoning formatters
# =============================================================================

def _format_reasoning_openrouter(model: str, max_tokens: int, enabled: bool) -> Dict[str, Any]:
    """Format reasoning params for OpenRouter API"""
    model_lower = model.lower()

    # x-ai/ models: can disable reasoning entirely via enabled: false
    if model_lower.startswith('x-ai/'):
        if enabled:
            return {"reasoning": {"effort": "medium", "enabled": True}}
        return {"reasoning": {"enabled": False}}

    # openai/ use effort-based (must always send)
    # Note: "none" not universally supported, use "minimal" for OFF
    if model_lower.startswith('openai/'):
        effort = "medium" if enabled else "minimal"
        return {"reasoning": {"effort": effort}}

    # google/ and anthropic/ use token-based
    elif model_lower.startswith(('google/', 'anthropic/')):
        if enabled:
            return {"reasoning": {"max_tokens": max_tokens // 2}}
        return {}  # No explicit OFF for token-based

    # Default: effort-based
    effort = "medium" if enabled else "minimal"
    return {"reasoning": {"effort": effort}}


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
    The format functions return appropriate "off" values like {"effort": "minimal"}.

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

    # Check if model supports reasoning at all
    if not supports_reasoning(model):
        return {}

    # OpenAI without Responses API cannot use reasoning params
    if provider == 'openai' and not _use_responses_api():
        return {}

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
        # Extract system message if present
        system_instruction = None
        contents = []

        for msg in messages:
            role = msg.get('role', 'user')
            content = msg.get('content', '')

            if role == 'system':
                system_instruction = content
            elif role == 'assistant':
                contents.append(types.Content(role='model', parts=[types.Part.from_text(text=content)]))
            else:  # user
                contents.append(types.Content(role='user', parts=[types.Part.from_text(text=content)]))

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
            input_messages = []
            for msg in messages:
                role = msg.get('role', 'user')
                content = msg.get('content', '')
                if role == 'system':
                    input_messages.append({"role": "developer", "content": content})
                else:
                    input_messages.append({"role": role, "content": content})

            request_params = {
                "model": model,
                "input": input_messages,
                "max_output_tokens": max_tokens,
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
                "temperature": temperature,
                "max_tokens": max_tokens,
            }

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


def chat(messages: List[Dict[str, Any]],
         model: str = None,
         temperature: float = 0.8,
         max_tokens: int = 8192,
         context: str = "chat") -> Optional[str]:
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
    settings = load_settings()
    provider = _get_provider()

    # Model should always be provided by caller, but default to the chat model
    model = model or settings.get('conversation', {}).get('chat_model', 'gemini-3-flash-preview')

    # Adjust max_tokens if reasoning is enabled (thinking needs more tokens)
    max_tokens = adjust_max_tokens_for_reasoning(model, context, max_tokens)

    # Route to provider-specific implementations
    if provider == 'gemini':
        return _chat_gemini(messages, model, temperature, max_tokens, context)

    if provider == 'openai':
        return _chat_openai(messages, model, temperature, max_tokens, context)

    # OpenRouter path (uses chat.completions API with extra_body for reasoning)
    t_entry = time.perf_counter()
    client = _get_client()
    if not client:
        return None
    t_client = time.perf_counter()

    try:
        # Handle nitro model - use fast providers and strip suffix
        extra_body = {}
        request_model = model
        if model == 'meta-llama/llama-3.1-8b-instruct:nitro':
            request_model = 'meta-llama/llama-3.1-8b-instruct'
            extra_body['provider'] = {
                'order': ['Friendli', 'Cerebras', 'SambaNova', 'DeepInfra']
            }
            print(f"[LLM] Request: {model} -> {request_model} with nitro providers ({context})")
        else:
            print(f"[LLM] Request: {model} ({context})")

        # Build request parameters
        request_params = {
            "model": request_model,
            "messages": messages,
            "temperature": temperature,
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

        # Log to file
        payload = {"model": model, "temperature": temperature, "max_tokens": max_tokens, "messages": messages}
        log_llm(payload, response=result_text)

        # Log event with token counts and latency
        duration_ms = (t_post - t_pre) * 1000
        el = _get_event_logger()
        if el:
            usage = response.usage
            el.log_llm_event(
                model=model,
                context=context,
                input_tokens=usage.prompt_tokens if usage else None,
                output_tokens=usage.completion_tokens if usage else None,
                total_tokens=usage.total_tokens if usage else None,
                duration_ms=duration_ms
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
        print(f"[LLM] Response: {model} ({len(result_text)} chars, {net_ms:.0f}ms) [{profile}]")
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
                context: str = "chat"):
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
    model = model or settings.get('conversation', {}).get('chat_model', 'google/gemini-3-flash-preview:nitro')
    max_tokens = adjust_max_tokens_for_reasoning(model, context, max_tokens)

    print(f"[LLM] Request (streaming): {model} ({context})")

    try:
        if provider == 'gemini':
            yield from _chat_stream_gemini(messages, model, temperature, max_tokens, context)
        elif provider == 'openai':
            yield from _chat_stream_openai(messages, model, temperature, max_tokens, context)
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
    extra_body = {}
    request_model = model

    # Handle nitro model
    if model == 'meta-llama/llama-3.1-8b-instruct:nitro':
        request_model = 'meta-llama/llama-3.1-8b-instruct'
        extra_body['provider'] = {
            'order': ['Friendli', 'Cerebras', 'SambaNova', 'DeepInfra']
        }

    # Handle nitro suffix for other models
    if ':nitro' in model and request_model == model:
        request_model = model  # OpenRouter handles :nitro suffix

    request_params = {
        "model": request_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": True,
        "extra_headers": {
            "HTTP-Referer": "https://sonorus.github.io/",
            "X-Title": "Sonorus (Hogwarts Legacy Mod)"
        }
    }

    reasoning_params = get_reasoning_params('openrouter', request_model, max_tokens, context)
    if reasoning_params:
        extra_body.update(reasoning_params)
    if extra_body:
        request_params['extra_body'] = extra_body

    accumulated = []
    try:
        stream = client.chat.completions.create(**request_params)
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                text = chunk.choices[0].delta.content
                accumulated.append(text)
                yield text

        duration_ms = (time.time() - start_time) * 1000
        full_response = "".join(accumulated)

        if full_response:
            payload = {"model": model, "temperature": temperature, "max_tokens": max_tokens, "messages": messages}
            log_llm(payload, response=full_response)
            print(f"[LLM] Response (streamed): {model} ({len(full_response)} chars, {duration_ms:.0f}ms)")

            el = _get_event_logger()
            if el:
                el.log_llm_event(model=model, context=context, duration_ms=duration_ms)
        else:
            print(f"[LLM] Empty streaming response from {model}")

    except Exception as e:
        print(f"[LLM] OpenRouter streaming error: {e}")
        friendly_error = _parse_llm_error(e)
        _set_last_error(friendly_error)
        el = _get_event_logger()
        if el:
            el.log_llm_event(model=model, context=context, status="error", error=friendly_error)


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

    # Convert messages to Gemini format
    system_instruction = None
    contents = []
    for msg in messages:
        role = msg.get('role', 'user')
        content = msg.get('content', '')
        if role == 'system':
            system_instruction = content
        elif role == 'assistant':
            contents.append(types.Content(role='model', parts=[types.Part.from_text(text=content)]))
        else:
            contents.append(types.Content(role='user', parts=[types.Part.from_text(text=content)]))

    reasoning_params = get_reasoning_params('gemini', model, max_tokens, context)
    thinking_config = types.ThinkingConfig(**reasoning_params) if reasoning_params else None

    config = types.GenerateContentConfig(
        temperature=temperature,
        max_output_tokens=max_tokens,
        system_instruction=system_instruction,
        thinking_config=thinking_config
    )

    accumulated = []
    try:
        stream = client.models.generate_content_stream(
            model=model,
            contents=contents,
            config=config
        )
        for chunk in stream:
            if chunk.text:
                accumulated.append(chunk.text)
                yield chunk.text

        duration_ms = (time.time() - start_time) * 1000
        full_response = "".join(accumulated)

        if full_response:
            payload = {"model": model, "temperature": temperature, "max_tokens": max_tokens, "messages": messages}
            log_llm(payload, response=full_response)
            print(f"[LLM] Response (streamed): {model} ({len(full_response)} chars, {duration_ms:.0f}ms)")

            el = _get_event_logger()
            if el:
                usage = None
                el.log_llm_event(model=model, context=context, duration_ms=duration_ms)

    except Exception as e:
        print(f"[LLM] Gemini streaming error: {e}")
        friendly_error = _parse_llm_error(e)
        _set_last_error(friendly_error)
        el = _get_event_logger()
        if el:
            el.log_llm_event(model=model, context=context, status="error", error=friendly_error)


def _chat_stream_openai(messages, model, temperature, max_tokens, context):
    """Stream via OpenAI API (chat completions)."""
    client = _get_client()
    if not client:
        return

    start_time = time.time()

    request_params = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": True,
    }

    accumulated = []
    try:
        stream = client.chat.completions.create(**request_params)
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                text = chunk.choices[0].delta.content
                accumulated.append(text)
                yield text

        duration_ms = (time.time() - start_time) * 1000
        full_response = "".join(accumulated)

        if full_response:
            payload = {"model": model, "temperature": temperature, "max_tokens": max_tokens, "messages": messages}
            log_llm(payload, response=full_response)
            print(f"[LLM] Response (streamed): {model} ({len(full_response)} chars, {duration_ms:.0f}ms)")

            el = _get_event_logger()
            if el:
                el.log_llm_event(model=model, context=context, duration_ms=duration_ms)

    except Exception as e:
        print(f"[LLM] OpenAI streaming error: {e}")
        friendly_error = _parse_llm_error(e)
        _set_last_error(friendly_error)
        el = _get_event_logger()
        if el:
            el.log_llm_event(model=model, context=context, status="error", error=friendly_error)


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
                "max_output_tokens": max_tokens,
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
                "temperature": temperature,
                "max_tokens": max_tokens,
            }

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
                     max_tokens: int = 8192) -> Optional[str]:
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

    if provider == 'openai':
        return _chat_with_vision_openai(prompt, image_b64, model, temperature, max_tokens)

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

        # Build request parameters
        request_params = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "extra_headers": {
                "HTTP-Referer": "https://sonorus.github.io/",
                "X-Title": "Sonorus (Hogwarts Legacy Mod)"
            }
        }

        # Add reasoning params for OpenRouter (uses extra_body)
        reasoning_params = get_reasoning_params('openrouter', model, max_tokens, 'vision')
        if reasoning_params:
            request_params['extra_body'] = reasoning_params

        response = client.chat.completions.create(**request_params)
        duration_ms = (time.time() - start_time) * 1000

        result_text = response.choices[0].message.content.strip()

        # Log to file (vision prompt as user message, note image was included)
        log_messages = [{"role": "user", "content": f"[Vision request with image]\n\n{prompt}"}]
        payload = {"model": model, "temperature": temperature, "max_tokens": max_tokens, "messages": log_messages}
        log_llm(payload, response=result_text)

        # Log vision event with token counts and latency
        el = _get_event_logger()
        if el:
            usage = response.usage
            el.log_llm_event(
                model=model,
                context="vision",
                input_tokens=usage.prompt_tokens if usage else None,
                output_tokens=usage.completion_tokens if usage else None,
                total_tokens=usage.total_tokens if usage else None,
                duration_ms=duration_ms
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
    api_key = settings.get('llm', {}).get('api_key') or os.getenv('GEMINI_API_KEY', '')
    provider = settings.get('llm', {}).get('provider', 'gemini')
    chat_model = settings.get('conversation', {}).get('chat_model', 'gemini-3-flash-preview')

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
