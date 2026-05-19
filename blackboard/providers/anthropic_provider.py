"""Anthropic provider — /v1/messages with tool use, streaming, cache_control hints."""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx

from blackboard.kernel.logger import describe_error, get_logger
from blackboard.providers.base import (
    AIProvider,
    LLMResponse,
    Message,
    ProviderCapabilities,
    ProviderError,
    ProviderHealth,
)
from blackboard.providers.secrets import resolve as resolve_secret

logger = get_logger("providers.anthropic")

_DEFAULT_VERSION = "2023-06-01"


class AnthropicProvider(AIProvider):
    type = "llm_api"
    _STRUCTURED_OUTPUT_TOOL_NAME = "emit_structured_response"

    def __init__(
        self,
        provider_id: str,
        *,
        endpoint: str = "https://api.anthropic.com/v1",
        model: str = "claude-sonnet-4-5",
        api_key_secret: str = "anthropic_main",
        api_key: str = "",
        timeout: float = 120.0,
        max_retries: int = 3,
        capabilities: Optional[ProviderCapabilities] = None,
    ) -> None:
        self.id = provider_id
        self.name = provider_id
        self.model = model
        self.capabilities = capabilities or ProviderCapabilities(
            chat=True, tool_calling=True, vision=True, streaming=True, structured_output=True, long_context=True
        )
        self._endpoint = endpoint.rstrip("/")
        self._api_key_secret = api_key_secret
        self._api_key_inline = (api_key or "").strip()
        self._timeout = float(timeout)
        self._max_retries = int(max_retries)
        self._client: Optional[httpx.AsyncClient] = None
        self._lock = asyncio.Lock()

    def set_api_key(self, value: str) -> None:
        self._api_key_inline = (value or "").strip()

    def set_model(self, value: str) -> None:
        self.model = str(value or "").strip()

    async def list_models(self) -> List[str]:
        models = [
            "claude-sonnet-4-5",
            "claude-opus-4-1",
            "claude-opus-4-20250514",
            "claude-sonnet-4-20250514",
            "claude-3-7-sonnet-20250219",
            "claude-3-5-sonnet-20241022",
            "claude-3-5-haiku-20241022",
        ]
        if self.model and self.model not in models:
            models.insert(0, self.model)
        return models

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            async with self._lock:
                if self._client is None or self._client.is_closed:
                    self._client = httpx.AsyncClient(timeout=httpx.Timeout(self._timeout))
        return self._client

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "anthropic-version": _DEFAULT_VERSION,
        }
        key = self._api_key_inline or (resolve_secret(self._api_key_secret) if self._api_key_secret else "")
        if key:
            headers["x-api-key"] = key
        return headers

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def health(self) -> ProviderHealth:
        started = time.monotonic()
        # Anthropic has no public /models GET; do a tiny messages call instead.
        try:
            client = await self._get_client()
            payload = {
                "model": self.model,
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "hi"}],
            }
            r = await client.post(f"{self._endpoint}/messages", headers=self._headers(), json=payload)
            ok = r.status_code < 500
            return ProviderHealth(
                ok=ok,
                latency_ms=int((time.monotonic() - started) * 1000),
                error="" if ok else f"HTTP {r.status_code}: {r.text[:120]}",
            )
        except Exception as exc:
            return ProviderHealth(
                ok=False,
                latency_ms=int((time.monotonic() - started) * 1000),
                error=describe_error(exc, "health check failed"),
            )

    @staticmethod
    def _split_messages(messages: List[Message]) -> tuple[str, List[Dict[str, Any]]]:
        system_parts: List[str] = []
        out: List[Dict[str, Any]] = []
        for msg in messages:
            if msg.role == "system":
                system_parts.append(msg.content)
                continue
            role = msg.role if msg.role in ("user", "assistant") else "user"
            out.append({"role": role, "content": msg.content})
        return ("\n\n".join(system_parts).strip(), out)

    @staticmethod
    def _convert_tools(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for tool in tools or []:
            fn = tool.get("function") or tool
            name = fn.get("name") or tool.get("name")
            if not name:
                continue
            out.append({
                "name": name,
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters") or fn.get("input_schema") or {"type": "object", "properties": {}},
            })
        return out

    @classmethod
    def _structured_tool_from_response_format(cls, response_format: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(response_format, dict):
            return None
        if str(response_format.get("type") or "").strip().lower() != "json_schema":
            return None
        payload = response_format.get("json_schema") or {}
        if not isinstance(payload, dict):
            return None
        schema = payload.get("schema")
        if not isinstance(schema, dict):
            return None
        name = str(payload.get("name") or cls._STRUCTURED_OUTPUT_TOOL_NAME).strip() or cls._STRUCTURED_OUTPUT_TOOL_NAME
        return {
            "name": name,
            "description": "Emit a structured JSON response that exactly matches the requested schema.",
            "input_schema": schema,
        }

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
        system_text, conv = self._split_messages(messages)
        response_format = kwargs.get("response_format")
        structured_tool = self._structured_tool_from_response_format(response_format) if not tools else None
        payload: Dict[str, Any] = {
            "model": kwargs.get("model") or self.model,
            "max_tokens": int(max_tokens or 4096),
            "temperature": float(temperature),
            "messages": conv,
        }
        if system_text:
            if cache_stable_prefix:
                payload["system"] = [{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}]
            else:
                payload["system"] = system_text
        if tools:
            payload["tools"] = self._convert_tools(tools)
        elif structured_tool is not None:
            payload["tools"] = [structured_tool]
            payload["tool_choice"] = {"type": "tool", "name": structured_tool["name"]}
            payload["temperature"] = 0.0

        url = f"{self._endpoint}/messages"
        last_error: Optional[Exception] = None
        for attempt in range(self._max_retries + 1):
            try:
                client = await self._get_client()
                r = await client.post(url, json=payload, headers=self._headers())
                if r.status_code == 429:
                    raise ProviderError(f"{self.id}: rate limited", retryable=True, rate_limited=True)
                if r.status_code >= 500:
                    raise ProviderError(f"{self.id}: HTTP {r.status_code}: {r.text[:200]}", retryable=True)
                if r.status_code >= 400:
                    raise ProviderError(f"{self.id}: HTTP {r.status_code}: {r.text[:400]}", retryable=False)
                data = r.json()
                content_blocks = data.get("content") or []
                text_parts: List[str] = []
                tool_calls: List[Dict[str, Any]] = []
                structured_content: Optional[str] = None
                for block in content_blocks:
                    btype = block.get("type")
                    if btype == "text":
                        text_parts.append(str(block.get("text") or ""))
                    elif btype == "tool_use":
                        if structured_tool is not None and str(block.get("name") or "") == structured_tool["name"]:
                            structured_content = json.dumps(block.get("input") or {})
                        tool_calls.append({
                            "id": block.get("id", ""),
                            "type": "function",
                            "function": {
                                "name": block.get("name", ""),
                                "arguments": json.dumps(block.get("input") or {}),
                            },
                        })
                usage = data.get("usage") or {}
                return LLMResponse(
                    content=structured_content if structured_content is not None else "".join(text_parts),
                    model=str(data.get("model") or payload["model"]),
                    tokens_prompt=int(usage.get("input_tokens") or 0),
                    tokens_completion=int(usage.get("output_tokens") or 0),
                    finish_reason=str(data.get("stop_reason") or "stop"),
                    tool_calls=tool_calls,
                    raw=data,
                )
            except ProviderError as exc:
                last_error = exc
                if not exc.retryable or attempt >= self._max_retries:
                    raise
            except (httpx.HTTPError, asyncio.TimeoutError) as exc:
                last_error = exc
                if attempt >= self._max_retries:
                    raise ProviderError(f"{self.id}: {describe_error(exc, 'request failed')}", retryable=True) from exc
            await asyncio.sleep(min(1.5 * (2 ** attempt), 12.0))
        raise ProviderError(f"{self.id}: exhausted retries", retryable=False)

    async def stream(
        self,
        messages: List[Message],
        *,
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        # Minimal streaming — full SSE protocol is more involved. For v1 we fall back to
        # a non-streaming complete() and yield the result in one chunk.
        response = await self.complete(
            messages, tools=tools, temperature=temperature, max_tokens=max_tokens, **kwargs
        )
        yield {
            "type": "usage",
            "prompt_tokens": int(response.tokens_prompt or 0),
            "completion_tokens": int(response.tokens_completion or 0),
            "model": str(response.model or self.model),
        }
        yield response.content
