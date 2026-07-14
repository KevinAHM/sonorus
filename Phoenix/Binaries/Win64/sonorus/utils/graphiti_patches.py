"""
Monkey-patches for Graphiti LLM clients to support reasoning params.
KuzuDriver is now modified directly in graphiti_core/driver/kuzu_driver.py.
"""

import json
import json_repair
from typing import Any

_patched = False
_openai_responses_patched = False


def patch_graphiti_reasoning():
    """
    Monkey-patch Graphiti's OpenAI clients to pass reasoning params via extra_body.
    This allows disabling reasoning on models like grok-4.1-fast via OpenRouter.

    Safe to call multiple times - only patches once.
    """
    global _patched
    if _patched:
        return

    try:
        from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient
        from graphiti_core.llm_client.config import DEFAULT_MAX_TOKENS, ModelSize
        from graphiti_core.llm_client.errors import RateLimitError
        from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient
        from graphiti_core.helpers import semaphore_gather
        import openai
        import llm as llm_module
    except ImportError as e:
        print(f"[Graphiti Patches] Failed to import: {e}")
        return

    async def _patched_generate_response(
        self,
        messages: list,
        response_model: Any = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        model_size: ModelSize = ModelSize.medium,
    ) -> dict[str, Any]:
        """Patched _generate_response that injects reasoning params."""
        self._sonorus_last_usage = None
        openai_messages = []
        for m in messages:
            m.content = self._clean_input(m.content)
            if m.role == 'user':
                openai_messages.append({'role': 'user', 'content': m.content})
            elif m.role == 'system':
                openai_messages.append({'role': 'system', 'content': m.content})

        try:
            # Prepare response format
            response_format: dict[str, Any] = {'type': 'json_object'}
            if response_model is not None:
                schema_name = getattr(response_model, '__name__', 'structured_response')
                json_schema = response_model.model_json_schema()
                response_format = {
                    'type': 'json_schema',
                    'json_schema': {
                        'name': schema_name,
                        'schema': json_schema,
                    },
                }

            # Respect Graphiti's requested model size so small prompts can use small_model.
            if model_size == ModelSize.small:
                model = self.small_model or self.model or 'gpt-4.1-mini'
            else:
                model = self.model or 'gpt-4.1-mini'

            requested_max_tokens = max_tokens or self.max_tokens
            provider = llm_module._get_provider()
            context = 'graphiti_small' if model_size == ModelSize.small else 'graphiti'

            # Build request with reasoning params
            request_model = model
            extra_body = {}
            if provider == 'openrouter':
                request_model, extra_body = llm_module._resolve_openrouter_model(model, context)

            request_kwargs: dict[str, Any] = {
                'model': request_model,
                'messages': openai_messages,
                'temperature': self.temperature,
                'max_tokens': requested_max_tokens,
                'response_format': response_format,
            }

            # Get reasoning params - use model_size to determine context
            extra_body.update(llm_module.get_reasoning_params(provider, request_model, requested_max_tokens, context))
            if extra_body:
                request_kwargs['extra_body'] = extra_body

            response = await self.client.chat.completions.create(**request_kwargs)
            self._sonorus_last_usage = getattr(response, 'usage', None)
            result = response.choices[0].message.content or ''
            return json_repair.loads(result)
        except openai.RateLimitError as e:
            raise RateLimitError from e
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f'Error in generating LLM response: {e}')
            raise

    async def _patched_rank(self, query: str, passages: list[str]) -> list[tuple[str, float]]:
        """Patched rank method that injects reasoning params."""
        import numpy as np

        openai_messages_list = [
            [
                {'role': 'system', 'content': 'You are an expert tasked with determining whether the passage is relevant to the query'},
                {'role': 'user', 'content': f"""
                       Respond with "True" if PASSAGE is relevant to QUERY and "False" otherwise.
                       <PASSAGE>
                       {passage}
                       </PASSAGE>
                       <QUERY>
                       {query}
                       </QUERY>
                       """},
            ]
            for passage in passages
        ]

        try:
            # Get reasoning params for reranker (explicitly reranker context since this is the reranker client)
            provider = llm_module._get_provider()
            request_model = self.config.model or 'gpt-4.1-nano'
            extra_body = {}
            if provider == 'openrouter':
                request_model, extra_body = llm_module._resolve_openrouter_model(request_model, 'reranker')
            extra_body.update(llm_module.get_reasoning_params(provider, request_model, 1024, 'reranker'))

            async def create_completion(openai_messages):
                request_kwargs: dict[str, Any] = {
                    'model': request_model,
                    'messages': openai_messages,
                    'temperature': 0.1,
                    'max_tokens': 1,
                    'logit_bias': {'6432': 1, '7983': 1},
                    'logprobs': True,
                    'top_logprobs': 2,
                }
                if extra_body:
                    request_kwargs['extra_body'] = extra_body
                return await self.client.chat.completions.create(**request_kwargs)

            responses = await semaphore_gather(
                *[create_completion(msgs) for msgs in openai_messages_list]
            )

            responses_top_logprobs = [
                response.choices[0].logprobs.content[0].top_logprobs
                if response.choices[0].logprobs is not None
                and response.choices[0].logprobs.content is not None
                else []
                for response in responses
            ]

            scores: list[float] = []
            for top_logprobs in responses_top_logprobs:
                if len(top_logprobs) == 0:
                    continue
                norm_logprobs = np.exp(top_logprobs[0].logprob)
                if top_logprobs[0].token.strip().split(' ')[0].lower() == 'true':
                    scores.append(norm_logprobs)
                else:
                    scores.append(1 - norm_logprobs)

            results = [(passage, score) for passage, score in zip(passages, scores, strict=True)]
            results.sort(reverse=True, key=lambda x: x[1])
            return results
        except openai.RateLimitError as e:
            raise RateLimitError from e
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f'Error in generating LLM response: {e}')
            raise

    # Apply patches
    OpenAIGenericClient._generate_response = _patched_generate_response
    OpenAIRerankerClient.rank = _patched_rank

    _patched = True
    print("[Graphiti Patches] Applied reasoning params patches")


def patch_openai_no_responses_api():
    """
    Monkey-patch OpenAIClient to use chat.completions.create instead of the Responses API.

    The bug: Graphiti's OpenAIClient._create_structured_completion() unconditionally uses
    client.responses.parse() (the OpenAI Responses API). When a user points the OpenAI
    provider at a custom proxy/endpoint that doesn't support the Responses API, all
    structured output calls (entity extraction, relationship extraction, etc.) fail with
    404 because the /responses endpoint doesn't exist on the proxy.

    The fix: override _generate_response on OpenAIClient to use chat.completions.create
    with json_schema response_format, bypassing both _create_structured_completion (which
    uses responses.parse) and _handle_structured_response (which expects response.output_text).

    Only call this when the user has responses_api disabled in settings.
    Safe to call multiple times - only patches once.
    """
    global _openai_responses_patched
    if _openai_responses_patched:
        return

    try:
        from graphiti_core.llm_client.openai_client import OpenAIClient
        from graphiti_core.llm_client.config import DEFAULT_MAX_TOKENS, ModelSize
        from graphiti_core.llm_client.errors import RateLimitError
        import openai
        import llm as llm_module
    except ImportError as e:
        print(f"[Graphiti Patches] Failed to import for OpenAI responses patch: {e}")
        return

    async def _patched_generate_response(
        self,
        messages: list,
        response_model: Any = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        model_size: ModelSize = ModelSize.medium,
    ) -> dict[str, Any]:
        """Patched _generate_response: chat.completions instead of responses API."""
        self._sonorus_last_usage = None
        openai_messages = self._convert_messages_to_openai_format(messages)
        model = self._get_model_for_size(model_size)
        provider = llm_module._get_provider()
        context = 'graphiti_small' if model_size == ModelSize.small else 'graphiti'

        try:
            # Build response format
            response_format: dict[str, Any] = {'type': 'json_object'}
            if response_model is not None:
                schema_name = getattr(response_model, '__name__', 'structured_response')
                json_schema = response_model.model_json_schema()
                response_format = {
                    'type': 'json_schema',
                    'json_schema': {
                        'name': schema_name,
                        'schema': json_schema,
                    },
                }

            request_model = model
            extra_body = {}
            if provider == 'openrouter':
                request_model, extra_body = llm_module._resolve_openrouter_model(model, context)

            request_kwargs: dict[str, Any] = {
                'model': request_model,
                'messages': openai_messages,
                'temperature': self.temperature,
                'max_tokens': max_tokens or self.max_tokens,
                'response_format': response_format,
            }

            # Inject reasoning params via extra_body
            extra_body.update(llm_module.get_reasoning_params(provider, request_model, self.max_tokens, context))
            if extra_body:
                request_kwargs['extra_body'] = extra_body

            response = await self.client.chat.completions.create(**request_kwargs)
            self._sonorus_last_usage = getattr(response, 'usage', None)
            result = response.choices[0].message.content or '{}'
            return json.loads(result)
        except openai.RateLimitError as e:
            raise RateLimitError from e
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f'Error in generating LLM response: {e}')
            raise

    OpenAIClient._generate_response = _patched_generate_response

    _openai_responses_patched = True
    print("[Graphiti Patches] Patched OpenAIClient: using chat.completions instead of responses API")


def patch_kuzu_driver():
    """No-op: KuzuDriver is now modified directly in graphiti_core/driver/kuzu_driver.py."""
    pass
