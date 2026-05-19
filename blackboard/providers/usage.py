"""Provider-level usage tally — rolling per-profile prompt/completion token counts.

Lightweight aggregator with persistence. Exposed via /api/usage and the Settings panel.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, Optional

from blackboard.governors.budget import get_budget_governor
from blackboard.governors.health import get_health_governor


@dataclass
class ProviderUsage:
    profile_id: str
    role: str = ""
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    last_ts: float = 0.0
    last_model: str = ""

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class ToolUsage:
    tool_name: str
    calls: int = 0
    success: int = 0
    failed: int = 0
    timeout: int = 0
    total_elapsed_ms: float = 0.0
    last_ts: float = 0.0


class UsageTracker:
    def __init__(self, path: Optional[Path] = None) -> None:
        self._lock = threading.RLock()
        self._by_profile: Dict[str, ProviderUsage] = {}
        self._by_tool: Dict[str, ToolUsage] = {}
        self._path = Path(path).resolve() if path else None
        self._load()

    def _load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            return
        providers = payload.get("providers") or {}
        tools = payload.get("tools") or {}
        if isinstance(providers, dict):
            for profile_id, raw in providers.items():
                item = dict(raw or {})
                self._by_profile[str(profile_id)] = ProviderUsage(
                    profile_id=str(item.get("profile_id") or profile_id),
                    role=str(item.get("role") or ""),
                    calls=int(item.get("calls") or 0),
                    prompt_tokens=int(item.get("prompt_tokens") or 0),
                    completion_tokens=int(item.get("completion_tokens") or 0),
                    last_ts=float(item.get("last_ts") or 0.0),
                    last_model=str(item.get("last_model") or ""),
                )
        if isinstance(tools, dict):
            for tool_name, raw in tools.items():
                item = dict(raw or {})
                self._by_tool[str(tool_name)] = ToolUsage(
                    tool_name=str(item.get("tool_name") or tool_name),
                    calls=int(item.get("calls") or 0),
                    success=int(item.get("success") or 0),
                    failed=int(item.get("failed") or 0),
                    timeout=int(item.get("timeout") or 0),
                    total_elapsed_ms=float(item.get("total_elapsed_ms") or 0.0),
                    last_ts=float(item.get("last_ts") or 0.0),
                )

    def _save(self) -> None:
        if self._path is None:
            return
        snapshot = self.snapshot()
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(snapshot, indent=2, sort_keys=True), encoding="utf-8")
        except Exception:
            return

    def record(self, *, profile_id: str, role: str, prompt: int, completion: int, model: str = "") -> None:
        with self._lock:
            entry = self._by_profile.setdefault(profile_id, ProviderUsage(profile_id=profile_id))
            entry.role = role or entry.role
            entry.calls += 1
            entry.prompt_tokens += max(0, int(prompt or 0))
            entry.completion_tokens += max(0, int(completion or 0))
            entry.last_ts = time.time()
            entry.last_model = model or entry.last_model
        self._save()

    def record_tool_call(self, *, tool_name: str, success: bool, elapsed_ms: float = 0.0, timed_out: bool = False) -> None:
        with self._lock:
            entry = self._by_tool.setdefault(tool_name, ToolUsage(tool_name=tool_name))
            entry.calls += 1
            entry.total_elapsed_ms += max(0.0, float(elapsed_ms or 0.0))
            entry.last_ts = time.time()
            if timed_out:
                entry.timeout += 1
            elif success:
                entry.success += 1
            else:
                entry.failed += 1
        self._save()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            providers = {
                pid: {
                    "profile_id": e.profile_id,
                    "role": e.role,
                    "calls": e.calls,
                    "prompt_tokens": e.prompt_tokens,
                    "completion_tokens": e.completion_tokens,
                    "total_tokens": e.total_tokens,
                    "last_ts": e.last_ts,
                    "last_model": e.last_model,
                }
                for pid, e in self._by_profile.items()
            }
            tools = {
                name: {
                    "tool_name": e.tool_name,
                    "calls": e.calls,
                    "success": e.success,
                    "failed": e.failed,
                    "timeout": e.timeout,
                    "success_rate": round(e.success / max(1, e.calls), 3),
                    "total_elapsed_ms": round(e.total_elapsed_ms, 1),
                    "avg_elapsed_ms": round(e.total_elapsed_ms / max(1, e.calls), 1),
                    "last_ts": e.last_ts,
                }
                for name, e in self._by_tool.items()
            }
            total_provider_calls = sum(int(item["calls"] or 0) for item in providers.values())
            total_prompt_tokens = sum(int(item["prompt_tokens"] or 0) for item in providers.values())
            total_completion_tokens = sum(int(item["completion_tokens"] or 0) for item in providers.values())
            total_tool_calls = sum(int(item["calls"] or 0) for item in tools.values())
            return {
                "providers": providers,
                "tools": tools,
                "totals": {
                    "provider_profiles": len(providers),
                    "provider_calls": total_provider_calls,
                    "prompt_tokens": total_prompt_tokens,
                    "completion_tokens": total_completion_tokens,
                    "total_tokens": total_prompt_tokens + total_completion_tokens,
                    "tool_calls": total_tool_calls,
                },
            }

    def reset(self) -> None:
        with self._lock:
            self._by_profile.clear()
            self._by_tool.clear()
        self._save()


def record_provider_usage(
    *,
    profile_id: str,
    role: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    model: str = "",
    user_id: str = "default",
    session_id: str = "",
) -> Dict[str, Any]:
    get_usage_tracker().record(
        profile_id=str(profile_id or "unknown"),
        role=str(role or ""),
        prompt=int(prompt_tokens or 0),
        completion=int(completion_tokens or 0),
        model=str(model or ""),
    )
    return get_budget_governor().record(
        user_id=str(user_id or "default"),
        session_id=str(session_id or ""),
        provider_id=str(profile_id or "unknown"),
        model=str(model or "default"),
        prompt_tokens=int(prompt_tokens or 0),
        completion_tokens=int(completion_tokens or 0),
    )


async def call_and_record_provider(
    provider: Any,
    *,
    role: str,
    session_id: str,
    call: Callable[[], Awaitable[Any]],
    user_id: str = "default",
    record_health: bool = True,
) -> Any:
    started = time.monotonic()
    profile_id = str(getattr(provider, "id", "") or "unknown")
    try:
        response = await call()
        duration_ms = (time.monotonic() - started) * 1000
        if record_health:
            get_health_governor().record_call(f"provider:{profile_id}", success=True, duration_ms=duration_ms)
        record_provider_usage(
            profile_id=profile_id,
            role=role,
            prompt_tokens=int(getattr(response, "tokens_prompt", 0) or 0),
            completion_tokens=int(getattr(response, "tokens_completion", 0) or 0),
            model=str(getattr(response, "model", "") or getattr(provider, "model", "") or "default"),
            user_id=user_id,
            session_id=session_id,
        )
        return response
    except Exception:
        duration_ms = (time.monotonic() - started) * 1000
        if record_health:
            get_health_governor().record_call(f"provider:{profile_id}", success=False, duration_ms=duration_ms)
        raise


async def stream_and_record_provider(
    provider: Any,
    *,
    role: str,
    session_id: str,
    stream: Callable[[], AsyncIterator[Any]],
    user_id: str = "default",
    record_health: bool = True,
) -> AsyncIterator[Any]:
    started = time.monotonic()
    profile_id = str(getattr(provider, "id", "") or "unknown")
    prompt_tokens = 0
    completion_tokens = 0
    model = str(getattr(provider, "model", "") or "default")
    try:
        async for chunk in stream():
            if isinstance(chunk, dict) and str(chunk.get("type") or "") == "usage":
                prompt_tokens = int(chunk.get("prompt_tokens") or prompt_tokens or 0)
                completion_tokens = int(chunk.get("completion_tokens") or completion_tokens or 0)
                model = str(chunk.get("model") or model or "default")
                continue
            yield chunk
        duration_ms = (time.monotonic() - started) * 1000
        if record_health:
            get_health_governor().record_call(f"provider:{profile_id}", success=True, duration_ms=duration_ms)
        record_provider_usage(
            profile_id=profile_id,
            role=role,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            model=model,
            user_id=user_id,
            session_id=session_id,
        )
    except Exception:
        duration_ms = (time.monotonic() - started) * 1000
        if record_health:
            get_health_governor().record_call(f"provider:{profile_id}", success=False, duration_ms=duration_ms)
        raise


_tracker: Optional[UsageTracker] = None


def init_usage_tracker(data_root: Path | str | None = None) -> UsageTracker:
    global _tracker
    path = None
    if data_root:
        path = Path(data_root).resolve() / "usage" / "stats.json"
    _tracker = UsageTracker(path=path)
    return _tracker


def get_usage_tracker() -> UsageTracker:
    global _tracker
    if _tracker is None:
        _tracker = UsageTracker()
    return _tracker
