from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

from blackboard.kernel.logger import describe_error, get_logger
from blackboard.providers.base import LLMResponse, Message, ProviderCapabilities, ProviderError, ProviderHealth
from blackboard.providers.base import AIProvider

logger = get_logger("providers.llama_cpp_python")


class LlamaCppPythonProvider(AIProvider):
    type = "local_model"

    def __init__(
        self,
        provider_id: str,
        *,
        model: str,
        model_path: str = "",
        model_path_env: str = "",
        timeout: float = 120.0,
        max_retries: int = 1,
        capabilities: Optional[ProviderCapabilities] = None,
        n_ctx: int = 4096,
        n_threads: int = 4,
        n_gpu_layers: int = 0,
        chat_format: str = "",
        verbose: bool = False,
    ) -> None:
        self.id = provider_id
        self.name = provider_id
        self.model = str(model or "").strip() or "local-gguf"
        self.capabilities = capabilities or ProviderCapabilities(chat=True, streaming=True)
        self._model_path = str(model_path or "").strip()
        self._model_path_env = str(model_path_env or "").strip()
        self._timeout = float(timeout)
        self._max_retries = max(1, int(max_retries or 1))
        self._n_ctx = max(512, int(n_ctx or 4096))
        self._n_threads = max(1, int(n_threads or 4))
        self._n_gpu_layers = int(n_gpu_layers or 0)
        self._chat_format = str(chat_format or "").strip()
        self._verbose = bool(verbose)
        self._lock = threading.Lock()
        self._llm: Any = None
        self._loaded_path = ""

    def set_model(self, value: str) -> None:
        self.model = str(value or "").strip() or self.model

    def _resolve_model_path(self) -> str:
        raw = self._model_path
        if not raw and self._model_path_env:
            raw = str(os.environ.get(self._model_path_env) or "").strip()
        value = os.path.expandvars(os.path.expanduser(str(raw or "").strip()))
        return value

    @staticmethod
    def _serialize_messages(messages: List[Message]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for msg in messages:
            out.append({"role": str(msg.role or "user"), "content": str(msg.content or "")})
        return out

    def _load_sync(self) -> None:
        model_path = self._resolve_model_path()
        if not model_path:
            raise ProviderError(f"{self.id}: model path is not configured", retryable=False)
        if not Path(model_path).exists():
            raise ProviderError(f"{self.id}: model file not found: {model_path}", retryable=False)
        with self._lock:
            if self._llm is not None and self._loaded_path == model_path:
                return
            try:
                from llama_cpp import Llama
            except Exception as exc:
                raise ProviderError(f"{self.id}: llama_cpp import failed: {describe_error(exc)}", retryable=False) from exc
            kwargs: Dict[str, Any] = {
                "model_path": model_path,
                "n_ctx": self._n_ctx,
                "n_threads": self._n_threads,
                "n_gpu_layers": self._n_gpu_layers,
                "verbose": self._verbose,
            }
            if self._chat_format:
                kwargs["chat_format"] = self._chat_format
            self._llm = Llama(**kwargs)
            self._loaded_path = model_path

    def _complete_sync(
        self,
        messages: List[Message],
        *,
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> LLMResponse:
        self._load_sync()
        payload: Dict[str, Any] = {
            "messages": self._serialize_messages(messages),
            "temperature": float(temperature),
        }
        if max_tokens is not None:
            payload["max_tokens"] = int(max_tokens)
        if "top_p" in kwargs:
            payload["top_p"] = kwargs["top_p"]
        if "stop" in kwargs:
            payload["stop"] = kwargs["stop"]
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = kwargs.get("tool_choice", "auto")
        with self._lock:
            assert self._llm is not None
            response = self._llm.create_chat_completion(**payload)
        choices = response.get("choices") or []
        if not choices:
            return LLMResponse(content="", model=self.model, raw=response, finish_reason="empty")
        choice = choices[0] or {}
        message = choice.get("message") or {}
        usage = response.get("usage") or {}
        return LLMResponse(
            content=str(message.get("content") or ""),
            model=str(response.get("model") or self.model),
            tokens_prompt=int(usage.get("prompt_tokens") or 0),
            tokens_completion=int(usage.get("completion_tokens") or 0),
            finish_reason=str(choice.get("finish_reason") or "stop"),
            tool_calls=list(message.get("tool_calls") or []),
            raw=response,
        )

    async def health(self) -> ProviderHealth:
        model_path = self._resolve_model_path()
        if not model_path:
            return ProviderHealth(ok=False, error="model path not configured")
        if not Path(model_path).exists():
            return ProviderHealth(ok=False, error=f"model file not found: {model_path}")
        try:
            import llama_cpp  # noqa: F401
        except Exception as exc:
            return ProviderHealth(ok=False, error=describe_error(exc, "llama_cpp import failed"))
        return ProviderHealth(ok=True, detail={"model_path": model_path})

    async def list_models(self) -> List[str]:
        model_path = self._resolve_model_path()
        names = [self.model]
        if model_path:
            stem = Path(model_path).stem
            if stem and stem not in names:
                names.append(stem)
        return names

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
        last_error: Optional[Exception] = None
        for attempt in range(self._max_retries):
            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(
                        self._complete_sync,
                        messages,
                        tools=tools,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        previous_response_id=previous_response_id,
                        cache_stable_prefix=cache_stable_prefix,
                        **kwargs,
                    ),
                    timeout=self._timeout,
                )
            except ProviderError as exc:
                last_error = exc
                if attempt >= self._max_retries - 1 or not exc.retryable:
                    raise
            except asyncio.TimeoutError as exc:
                last_error = exc
                if attempt >= self._max_retries - 1:
                    raise ProviderError(f"{self.id}: request timed out", retryable=True) from exc
            except Exception as exc:
                last_error = exc
                if attempt >= self._max_retries - 1:
                    raise ProviderError(f"{self.id}: {describe_error(exc, 'request failed')}", retryable=False) from exc
        raise ProviderError(f"{self.id}: exhausted retries ({describe_error(last_error)})", retryable=False)

    async def stream(
        self,
        messages: List[Message],
        *,
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        response = await self.complete(
            messages,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        )
        yield {
            "type": "usage",
            "prompt_tokens": int(response.tokens_prompt or 0),
            "completion_tokens": int(response.tokens_completion or 0),
            "model": str(response.model or self.model),
        }
        if response.content:
            yield response.content

    async def close(self) -> None:
        with self._lock:
            self._llm = None
            self._loaded_path = ""
