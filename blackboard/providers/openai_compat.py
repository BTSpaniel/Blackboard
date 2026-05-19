"""OpenAI-compatible HTTP adapter — works for OpenAI, Fireworks, llama-server, Groq, OpenRouter, custom.

Ported from luna/workers/llm/adapters/api_adapter.py with retry + simple backoff.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx

from blackboard.kernel.logger import describe_error, get_logger
from blackboard.providers.base import (
    AIProvider,
    ExecuteInput,
    ExecuteOutput,
    LLMResponse,
    Message,
    ProviderCapabilities,
    ProviderError,
    ProviderHealth,
)
from blackboard.providers.secrets import resolve as resolve_secret

logger = get_logger("providers.openai_compat")


def _is_retryable_tool_generation_failure(body_text: str, *, tools_present: bool) -> bool:
    if not tools_present:
        return False
    text = str(body_text or "").strip()
    if not text:
        return False
    lowered = text.lower()
    if "tool_use_failed" in lowered or "failed to call a function" in lowered or "failed_generation" in lowered:
        return True
    try:
        payload = json.loads(text)
    except Exception:
        return False
    if not isinstance(payload, dict):
        return False
    error = payload.get("error") or {}
    if not isinstance(error, dict):
        return False
    code = str(error.get("code") or "").strip().lower()
    message = str(error.get("message") or "").strip().lower()
    return code == "tool_use_failed" or "failed to call a function" in message or "failed_generation" in message


class OpenAICompatProvider(AIProvider):
    """Generic chat-completions adapter (POST /chat/completions)."""

    type = "llm_api"

    def __init__(
        self,
        provider_id: str,
        *,
        endpoint: str,
        model: str,
        api_key_secret: str = "",
        api_key: str = "",
        timeout: float = 120.0,
        max_retries: int = 3,
        capabilities: Optional[ProviderCapabilities] = None,
        adapter_label: str = "openai_compat",
    ) -> None:
        self.id = provider_id
        self.name = provider_id
        self.model = model
        self.capabilities = capabilities or ProviderCapabilities(chat=True, tool_calling=True, streaming=True)
        self._endpoint = endpoint.rstrip("/")
        self._api_key_secret = api_key_secret or ""
        self._api_key_inline = (api_key or "").strip()  # Luna-style: literal key in config.yaml
        self._timeout = float(timeout)
        self._max_retries = int(max_retries)
        self._adapter_label = adapter_label
        self._client: Optional[httpx.AsyncClient] = None
        self._client_lock = asyncio.Lock()

    def set_api_key(self, value: str) -> None:
        """Update the in-memory inline key (used by /api/providers/{id}/key)."""
        self._api_key_inline = (value or "").strip()

    def set_model(self, value: str) -> None:
        self.model = str(value or "").strip()

    # ── HTTP plumbing ────────────────────────────────────────────

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            async with self._client_lock:
                if self._client is None or self._client.is_closed:
                    self._client = httpx.AsyncClient(
                        timeout=httpx.Timeout(self._timeout),
                        http2=False,
                    )
        return self._client

    def _headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {"Content-Type": "application/json"}
        # Resolution order: inline literal → secret_id (env/keyring/fallback_env).
        key = self._api_key_inline or (resolve_secret(self._api_key_secret) if self._api_key_secret else "")
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    # ── Health ───────────────────────────────────────────────────

    async def health(self) -> ProviderHealth:
        started = time.monotonic()
        try:
            client = await self._get_client()
            url = f"{self._endpoint}/models"
            r = await client.get(url, headers=self._headers())
            ok = r.status_code < 500
            return ProviderHealth(
                ok=ok,
                latency_ms=int((time.monotonic() - started) * 1000),
                error="" if ok else f"HTTP {r.status_code}",
            )
        except Exception as exc:
            return ProviderHealth(
                ok=False,
                latency_ms=int((time.monotonic() - started) * 1000),
                error=describe_error(exc, "health check failed"),
            )

    async def list_models(self) -> List[str]:
        client = await self._get_client()
        response = await client.get(f"{self._endpoint}/models", headers=self._headers())
        response.raise_for_status()
        data = response.json()
        models: List[str] = []
        for item in data.get("data") or []:
            if isinstance(item, dict) and item.get("id"):
                models.append(str(item["id"]))
            elif isinstance(item, str):
                models.append(item)
        return sorted(set(models))

    # ── Messages -> wire format ─────────────────────────────────

    @staticmethod
    def _serialize_messages(messages: List[Message]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for msg in messages:
            entry: Dict[str, Any] = {"role": msg.role, "content": msg.content}
            if msg.name:
                entry["name"] = msg.name
            if msg.tool_call_id:
                entry["tool_call_id"] = msg.tool_call_id
            if msg.tool_calls:
                entry["tool_calls"] = msg.tool_calls
            out.append(entry)
        return out

    @staticmethod
    def _parse_response(data: Dict[str, Any], model: str) -> LLMResponse:
        choices = data.get("choices") or []
        if not choices:
            return LLMResponse(content="", model=model, raw=data, finish_reason="empty")
        choice = choices[0]
        msg = choice.get("message") or {}
        content = str(msg.get("content") or "")
        tool_calls = msg.get("tool_calls") or []
        usage = data.get("usage") or {}
        return LLMResponse(
            content=content,
            model=str(data.get("model") or model),
            tokens_prompt=int(usage.get("prompt_tokens") or 0),
            tokens_completion=int(usage.get("completion_tokens") or 0),
            finish_reason=str(choice.get("finish_reason") or "stop"),
            tool_calls=list(tool_calls),
            raw=data,
        )

    # ── Complete ─────────────────────────────────────────────────

    async def complete(
        self,
        messages: List[Message],
        *,
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
        cache_stable_prefix: bool = False,  # honored by OpenAI provider, ignored here
        previous_response_id: Optional[str] = None,
        **kwargs: Any,
    ) -> LLMResponse:
        payload: Dict[str, Any] = {
            "model": kwargs.get("model") or self.model,
            "messages": self._serialize_messages(messages),
            "temperature": float(temperature),
        }
        if max_tokens is not None:
            payload["max_tokens"] = int(max_tokens)
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = kwargs.get("tool_choice", "auto")
        for key in ("top_p", "stop", "response_format"):
            if key in kwargs:
                payload[key] = kwargs[key]

        url = f"{self._endpoint}/chat/completions"
        last_error: Optional[Exception] = None
        for attempt in range(self._max_retries + 1):
            try:
                client = await self._get_client()
                r = await client.post(url, json=payload, headers=self._headers())
                if r.status_code == 429:
                    raise ProviderError(f"{self.id}: HTTP 429: {r.text[:400]}", retryable=True, rate_limited=True)
                if r.status_code >= 500:
                    raise ProviderError(f"{self.id}: HTTP {r.status_code}: {r.text[:200]}", retryable=True)
                if r.status_code >= 400:
                    raise ProviderError(
                        f"{self.id}: HTTP {r.status_code}: {r.text[:400]}",
                        retryable=_is_retryable_tool_generation_failure(r.text, tools_present=bool(tools)),
                    )
                data = r.json()
                return self._parse_response(data, model=payload["model"])
            except ProviderError as exc:
                last_error = exc
                if exc.rate_limited or not exc.retryable or attempt >= self._max_retries:
                    raise
            except (httpx.HTTPError, asyncio.TimeoutError) as exc:
                last_error = exc
                if attempt >= self._max_retries:
                    raise ProviderError(f"{self.id}: {describe_error(exc, 'request failed')}", retryable=True) from exc
            # backoff
            delay = min(1.5 * (2 ** attempt), 12.0)
            if isinstance(last_error, ProviderError) and last_error.rate_limited:
                delay = max(delay, 5.0)
            await asyncio.sleep(delay)
        raise ProviderError(f"{self.id}: exhausted retries", retryable=False)

    # ── Stream ───────────────────────────────────────────────────

    async def stream(
        self,
        messages: List[Message],
        *,
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        payload: Dict[str, Any] = {
            "model": kwargs.get("model") or self.model,
            "messages": self._serialize_messages(messages),
            "temperature": float(temperature),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if max_tokens is not None:
            payload["max_tokens"] = int(max_tokens)
        if tools:
            payload["tools"] = tools

        url = f"{self._endpoint}/chat/completions"
        last_error: Optional[Exception] = None
        for attempt in range(self._max_retries + 1):
            try:
                client = await self._get_client()
                async with client.stream("POST", url, json=payload, headers=self._headers()) as response:
                    if response.status_code >= 400:
                        body = await response.aread()
                        detail = repr(body[:400])
                        body_text = body.decode("utf-8", errors="replace")
                        if response.status_code == 429:
                            raise ProviderError(f"{self.id}: HTTP 429: {detail}", retryable=True, rate_limited=True)
                        if response.status_code >= 500:
                            raise ProviderError(f"{self.id}: HTTP {response.status_code}: {detail}", retryable=True)
                        raise ProviderError(
                            f"{self.id}: HTTP {response.status_code}: {detail}",
                            retryable=_is_retryable_tool_generation_failure(body_text, tools_present=bool(tools)),
                        )
                    async for raw in response.aiter_lines():
                        if not raw or not raw.startswith("data:"):
                            continue
                        line = raw[5:].strip()
                        if line == "[DONE]":
                            return
                        try:
                            chunk = json.loads(line)
                        except Exception:
                            continue
                        usage = chunk.get("usage") or {}
                        if usage:
                            yield {
                                "type": "usage",
                                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                                "completion_tokens": int(usage.get("completion_tokens") or 0),
                                "model": str(chunk.get("model") or payload["model"]),
                            }
                        choices = chunk.get("choices") or []
                        if not choices:
                            continue
                        delta_obj = choices[0].get("delta") or {}
                        reasoning = delta_obj.get("reasoning_content") or delta_obj.get("reasoning") or delta_obj.get("thinking")
                        if reasoning:
                            yield {"type": "thinking", "content": str(reasoning)}
                        delta = delta_obj.get("content")
                        if delta:
                            yield str(delta)
                    return
            except ProviderError as exc:
                last_error = exc
                if exc.rate_limited or not exc.retryable or attempt >= self._max_retries:
                    raise
            except (httpx.HTTPError, asyncio.TimeoutError) as exc:
                last_error = exc
                if attempt >= self._max_retries:
                    raise ProviderError(f"{self.id}: {describe_error(exc, 'stream request failed')}", retryable=True) from exc
            delay = min(1.5 * (2 ** attempt), 12.0)
            if isinstance(last_error, ProviderError) and last_error.rate_limited:
                delay = max(delay, 5.0)
            await asyncio.sleep(delay)
        raise ProviderError(f"{self.id}: exhausted stream retries", retryable=False)

    # ── Coding-cli is N/A here ───────────────────────────────────

    async def execute(self, input: ExecuteInput) -> ExecuteOutput:
        raise NotImplementedError("openai_compat is not a coding_cli provider")
