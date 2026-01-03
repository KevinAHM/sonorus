"""
LLM utility for multiple providers (Gemini, OpenRouter, OpenAI).
Single module for all LLM operations - text and vision.
"""
import base64
import json
import os
import re
import time
from pathlib import Path
from typing import Optional, List, Dict, Any

# Load .env from script directory
try:
    from dotenv import load_dotenv
    _env_path = Path(__file__).parent / ".env"
    load_dotenv(_env_path)
except ImportError:
    pass

from openai import OpenAI

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


def fetch_model_capabilities():
    """Fetch OpenRouter model list and extract capabilities for all providers"""
    global _model_capabilities
    try:
        import requests
        resp = requests.get("https://openrouter.ai/api/v1/models", timeout=10)
        if resp.ok:
            for m in resp.json().get('data', []):
                model_id = m['id']
                supported = m.get('supported_parameters', [])
                _model_capabilities[model_id] = {
                    'supports_reasoning': 'reasoning' in supported,
                    'full_data': m
                }
            print(f"[LLM] Cached capabilities for {len(_model_capabilities)} models")
        else:
            print(f"[LLM] Failed to fetch model capabilities: {resp.status_code}")
    except Exception as e:
        print(f"[LLM] Failed to fetch model capabilities: {e}")


def supports_reasoning(model_id: str) -> bool:
    """Check if model supports reasoning (works for any provider)"""
    # Direct lookup
    if model_id in _model_capabilities:
        return _model_capabilities[model_id]['supports_reasoning']
    # Try with common prefixes for native provider models
    for prefix in ['google/', 'openai/', 'anthropic/']:
        prefixed = prefix + model_id
        if prefixed in _model_capabilities:
            return _model_capabilities[prefixed]['supports_reasoning']
    return False


def _get_provider():
    """Get the current LLM provider from settings"""
    settings = load_settings()
    return settings.get('llm', {}).get('provider', 'gemini')


def _get_api_key(provider: str) -> str:
    """Get API key for the specified provider, with fallback to legacy key"""
    settings = load_settings()
    llm_settings = settings.get('llm', {})

    # Try provider-specific key first
    provider_key = llm_settings.get(provider, {}).get('api_key', '')
    if provider_key:
        return provider_key

    # Fallback to legacy shared key
    legacy_key = llm_settings.get('api_key', '')
    if legacy_key:
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

    # Get provider-specific API key
    api_key = _get_api_key(provider)

    if not api_key:
        print(f"[LLM] Warning: No API key configured for {provider}")
        return None

    # Configure client based on provider
    if provider == 'openai':
        # Use OpenAI API (with optional custom endpoint)
        api_url = llm_settings.get('openai', {}).get('api_url', '').strip()
        if not api_url:
            api_url = "https://api.openai.com/v1"
        return OpenAI(api_key=api_key, base_url=api_url)
    else:
        # Default to OpenRouter
        return OpenAI(
            api_key=api_key,
            base_url="https://openrouter.ai/api/v1"
        )


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

    # grok/ and openai/ use effort-based (must always send)
    # Note: "none" not universally supported, use "minimal" for OFF
    if model_lower.startswith(('grok/', 'openai/')):
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
    if enabled:
        return {"thinking_budget": max_tokens // 2}
    else:
        return {"thinking_budget": 0}


def _format_reasoning_openai(model: str, max_tokens: int, enabled: bool) -> Dict[str, Any]:
    """Format reasoning params for native OpenAI API

    Reasoning models use reasoning={"effort": "..."} parameter.
    - "low": minimal reasoning, faster responses
    - "medium": balanced (default)
    - "high": deep reasoning for complex tasks
    """
    if enabled:
        return {"reasoning": {"effort": "medium"}}
    else:
        return {"reasoning": {"effort": "low"}}


def get_reasoning_params(provider: str, model: str, max_tokens: int) -> Dict[str, Any]:
    """Get reasoning params for any provider (unified router)"""
    # Check if model supports reasoning
    if not supports_reasoning(model):
        return {}

    # Get provider's reasoning setting
    settings = load_settings()
    enabled = settings.get('llm', {}).get(provider, {}).get('reasoning_enabled', False)

    # Route to provider-specific formatter
    result = {}
    if provider == 'openrouter':
        result = _format_reasoning_openrouter(model, max_tokens, enabled)
    elif provider == 'gemini':
        result = _format_reasoning_gemini(model, max_tokens, enabled)
    elif provider == 'openai':
        result = _format_reasoning_openai(model, max_tokens, enabled)

    if result:
        print(f"[LLM] Reasoning params for {model}: {result} (enabled={enabled})")

    return result


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

        # Get reasoning config for Gemini
        reasoning_params = get_reasoning_params('gemini', model, max_tokens)

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

        result_text = response.text.strip()

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
        payload = {"model": model, "temperature": temperature, "max_tokens": max_tokens, "messages": messages}
        log_llm(payload, error=str(e))
        el = _get_event_logger()
        if el:
            el.log_llm_event(model=model, context=context, status="error", error=str(e))
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
        Response text or None on failure
    """
    settings = load_settings()
    provider = _get_provider()

    # Model should always be provided by caller, but default to the chat model
    model = model or settings.get('conversation', {}).get('chat_model', 'gemini-3-flash-preview')

    # Route to Gemini if that's the provider
    if provider == 'gemini':
        return _chat_gemini(messages, model, temperature, max_tokens, context)

    # OpenRouter / OpenAI path
    client = _create_client()
    if not client:
        return None

    try:
        start_time = time.time()
        print(f"[LLM] Request: {model} ({context})")

        # Build request parameters
        request_params = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "extra_headers": {
                "HTTP-Referer": "https://hogwarts-legacy-mod",
                "X-Title": "Hogwarts AI NPC"
            }
        }

        # Add OpenAI-specific params (non-reasoning)
        extra_params = _get_openai_extra_params(model)
        request_params.update(extra_params)

        # Add reasoning params (provider-aware)
        reasoning_params = get_reasoning_params(provider, model, max_tokens)
        if reasoning_params:
            if provider == 'openrouter':
                # OpenRouter uses extra_body for non-standard params
                request_params['extra_body'] = reasoning_params
            else:
                # Native OpenAI supports reasoning param directly
                request_params.update(reasoning_params)

        response = client.chat.completions.create(**request_params)
        duration_ms = (time.time() - start_time) * 1000

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

        print(f"[LLM] Response: {model} ({len(result_text)} chars, {duration_ms:.0f}ms)")
        return result_text

    except Exception as e:
        print(f"[LLM] Error from {model}: {e}")
        payload = {"model": model, "temperature": temperature, "max_tokens": max_tokens, "messages": messages}
        log_llm(payload, error=str(e))
        # Log error event
        el = _get_event_logger()
        if el:
            el.log_llm_event(model=model, context=context, status="error", error=str(e))
        return None


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

        # Get reasoning config for Gemini
        reasoning_params = get_reasoning_params('gemini', model, max_tokens)

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

        result_text = response.text.strip()

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
        messages = [{"role": "user", "content": f"[Vision request with image]\n\n{prompt}"}]
        payload = {"model": model, "temperature": temperature, "max_tokens": max_tokens, "messages": messages}
        log_llm(payload, error=str(e))
        el = _get_event_logger()
        if el:
            el.log_llm_event(model=model, context="vision", status="error", error=str(e))
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

    # Route to Gemini if that's the provider
    if provider == 'gemini':
        return _chat_with_vision_gemini(prompt, image_b64, model, temperature, max_tokens)

    # OpenRouter / OpenAI path
    client = _create_client()
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
                "HTTP-Referer": "https://hogwarts-legacy-mod",
                "X-Title": "Hogwarts AI Vision"
            }
        }

        # Add OpenAI-specific params (non-reasoning)
        extra_params = _get_openai_extra_params(model)
        request_params.update(extra_params)

        # Add reasoning params (provider-aware)
        reasoning_params = get_reasoning_params(provider, model, max_tokens)
        if reasoning_params:
            if provider == 'openrouter':
                # OpenRouter uses extra_body for non-standard params
                request_params['extra_body'] = reasoning_params
            else:
                # Native OpenAI supports reasoning param directly
                request_params.update(reasoning_params)

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
        log_messages = [{"role": "user", "content": f"[Vision request with image]\n\n{prompt}"}]
        payload = {"model": model, "temperature": temperature, "max_tokens": max_tokens, "messages": log_messages}
        log_llm(payload, error=str(e))
        # Log error event
        el = _get_event_logger()
        if el:
            el.log_llm_event(model=model, context="vision", status="error", error=str(e))
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
