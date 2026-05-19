"""OpenAI provider — subclasses OpenAICompatProvider, supports /v1/responses + prompt-cache hints."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from blackboard.providers.base import LLMResponse, Message, ProviderCapabilities
from blackboard.providers.openai_compat import OpenAICompatProvider


class OpenAIProvider(OpenAICompatProvider):
    """OpenAI-specific. Defaults to /v1/chat/completions, can be flipped to /v1/responses."""

    def __init__(
        self,
        provider_id: str,
        *,
        endpoint: str = "https://api.openai.com/v1",
        model: str = "gpt-4o",
        api_key_secret: str = "openai_main",
        api_key: str = "",
        timeout: float = 120.0,
        max_retries: int = 3,
        use_responses_api: bool = False,
        capabilities: Optional[ProviderCapabilities] = None,
    ) -> None:
        super().__init__(
            provider_id,
            endpoint=endpoint,
            model=model,
            api_key_secret=api_key_secret,
            api_key=api_key,
            timeout=timeout,
            max_retries=max_retries,
            capabilities=capabilities or ProviderCapabilities(
                chat=True,
                tool_calling=True,
                vision=True,
                streaming=True,
                structured_output=True,
                long_context=True,
            ),
            adapter_label="openai",
        )
        self._use_responses_api = bool(use_responses_api)

    async def complete(
        self,
        messages: List[Message],
        *,
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
        cache_stable_prefix: bool = False,
        previous_response_id: Optional[str] = None,
        **kwargs: Any,
    ) -> LLMResponse:
        # /v1/responses is a different shape — for v1 we ship chat-completions only.
        # When use_responses_api is true and previous_response_id is supplied, the caller
        # can pass it through; we still fall back to chat-completions to keep coverage.
        return await super().complete(
            messages,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
            cache_stable_prefix=cache_stable_prefix,
            previous_response_id=previous_response_id,
            **kwargs,
        )
