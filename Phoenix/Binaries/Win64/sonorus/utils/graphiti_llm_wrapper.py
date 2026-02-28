"""
Logging wrapper for Graphiti LLM clients.
Intercepts all LLM calls made by Graphiti and logs them to Sonorus's logging systems.
"""

import time
import typing
from typing import Any

from pydantic import BaseModel

# Import Graphiti's base classes
try:
    from graphiti_core.llm_client.client import LLMClient
    from graphiti_core.llm_client.config import LLMConfig, ModelSize
    from graphiti_core.prompts.models import Message
    _graphiti_available = True
except ImportError:
    _graphiti_available = False
    LLMClient = object  # Fallback for type hints

# Import Sonorus logging
from .llm_logging import log_graphiti_llm
from event_logger import log_llm_event


class LoggingLLMClientWrapper(LLMClient if _graphiti_available else object):
    """
    Wrapper around any Graphiti LLMClient that logs all calls to Sonorus's logging systems.

    Logs to:
    - logs/llm_YYYY-MM-DD.txt (detailed request/response)
    - data/system_events.json (structured event for dashboard)
    """

    # Session-level token tracking
    _session_tokens = 0

    def __init__(self, inner_client: Any, context_prefix: str = "graphiti"):
        """
        Args:
            inner_client: The actual Graphiti LLM client (GeminiClient, OpenAIClient, etc.)
            context_prefix: Prefix for the context field in logs (e.g., "graphiti", "graphiti_memory")
        """
        if not _graphiti_available:
            raise ImportError("graphiti-core is not installed")

        self._inner = inner_client
        self._context_prefix = context_prefix

        # Copy attributes from inner client for compatibility
        self.config = inner_client.config
        self.model = inner_client.model
        self.small_model = getattr(inner_client, 'small_model', None)
        self.temperature = inner_client.temperature
        self.max_tokens = inner_client.max_tokens
        self.cache_enabled = inner_client.cache_enabled
        self.cache_dir = inner_client.cache_dir
        self.tracer = inner_client.tracer

    @staticmethod
    def _simplify_prompt_name(prompt_name: str | None) -> str:
        """Simplify long prompt names like 'extract_nodes.extract_summary' to 'extract_summary'."""
        if not prompt_name:
            return ""
        # Take only the last part after any dots
        parts = prompt_name.split('.')
        return parts[-1] if parts else prompt_name

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """Rough token estimate: ~4 characters per token."""
        return len(text) // 4 if text else 0

    @classmethod
    def reset_session_tokens(cls):
        """Reset the session token counter."""
        cls._session_tokens = 0

    @classmethod
    def get_session_tokens(cls) -> int:
        """Get total tokens used this session."""
        return cls._session_tokens

    def set_tracer(self, tracer: Any) -> None:
        """Pass through to inner client."""
        self._inner.set_tracer(tracer)
        self.tracer = tracer

    async def _generate_response(
        self,
        messages: list,
        response_model: type[BaseModel] | None = None,
        max_tokens: int = 8192,
        model_size: Any = None,
    ) -> dict[str, typing.Any]:
        """Delegate to inner client's _generate_response."""
        # Default model_size to medium if None
        if model_size is None:
            model_size = ModelSize.medium
        return await self._inner._generate_response(messages, response_model, max_tokens, model_size)

    async def generate_response(
        self,
        messages: list,
        response_model: type[BaseModel] | None = None,
        max_tokens: int | None = None,
        model_size: Any = None,
        group_id: str | None = None,
        prompt_name: str | None = None,
    ) -> dict[str, typing.Any]:
        """
        Intercept generate_response, log, then delegate to inner client.
        """
        import json

        # Default model_size to medium if None (required for tracing)
        if model_size is None:
            model_size = ModelSize.medium

        # Simplify prompt name for display
        simple_name = self._simplify_prompt_name(prompt_name)
        context = f"graphiti:{simple_name}" if simple_name else "graphiti"

        # Get model name - simplify if it has a provider prefix
        model_name = self.model or "unknown"
        display_model = model_name.split('/')[-1] if '/' in model_name else model_name

        # Estimate input tokens
        input_text = " ".join(m.content for m in messages)
        input_tokens = self._estimate_tokens(input_text)

        # Build payload for log_llm (detailed file logging)
        payload = {
            'model': model_name,
            'temperature': self.temperature,
            'max_tokens': max_tokens or self.max_tokens,
            'messages': [{'role': m.role, 'content': m.content} for m in messages]
        }

        start_time = time.time()
        response = None
        error_msg = None

        try:
            # Call the inner client
            response = await self._inner.generate_response(
                messages=messages,
                response_model=response_model,
                max_tokens=max_tokens,
                model_size=model_size,
                group_id=group_id,
                prompt_name=prompt_name
            )

            duration_ms = (time.time() - start_time) * 1000

            # Estimate output tokens from response
            response_str = json.dumps(response, indent=2) if response else ""
            output_tokens = self._estimate_tokens(response_str)
            total_tokens = input_tokens + output_tokens

            # Track session tokens
            LoggingLLMClientWrapper._session_tokens += total_tokens

            # Log to file (logs/llm_YYYY-MM-DD.txt)
            log_graphiti_llm(payload, response=response_str)

            # Log to event system with token estimates
            log_llm_event(
                model=model_name,
                context=context,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                duration_ms=duration_ms,
                status="success"
            )

            print(f"[Graphiti] {simple_name}: {display_model} ~{total_tokens}tok {duration_ms:.0f}ms")

            return response

        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            error_msg = str(e)

            # Log error to file
            log_graphiti_llm(payload, error=error_msg)

            # Log error to event system
            log_llm_event(
                model=model_name,
                context=context,
                input_tokens=input_tokens,
                duration_ms=duration_ms,
                status="error",
                error=error_msg
            )

            print(f"[Graphiti] {simple_name}: {display_model} - ERROR: {error_msg}")

            raise


def wrap_graphiti_client(client: Any, context_prefix: str = "graphiti") -> Any:
    """
    Convenience function to wrap a Graphiti LLM client with logging.

    Args:
        client: The Graphiti LLM client to wrap
        context_prefix: Prefix for log context (default: "graphiti")

    Returns:
        LoggingLLMClientWrapper that logs all LLM calls

    Usage:
        from graphiti_core.llm_client.gemini_client import GeminiClient

        client = GeminiClient(config=LLMConfig(api_key=key, model=model))
        wrapped_client = wrap_graphiti_client(client, "graphiti_memory")

        # Use wrapped_client when initializing Graphiti
        graphiti = Graphiti(graph_driver=kuzu_driver, llm_client=wrapped_client, ...)
    """
    if not _graphiti_available:
        print("[Graphiti] Warning: graphiti-core not installed, returning unwrapped client")
        return client

    return LoggingLLMClientWrapper(client, context_prefix)
