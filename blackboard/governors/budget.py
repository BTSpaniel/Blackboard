"""Budget governor for provider token and cost limits."""
from __future__ import annotations

import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Dict, Optional, Tuple

from blackboard.providers.base import ProviderError

_PRICING: Dict[str, Tuple[float, float]] = {
    "gpt-4o": (0.0025, 0.010),
    "gpt-4o-mini": (0.00015, 0.0006),
    "gpt-4-turbo": (0.010, 0.030),
    "gpt-3.5-turbo": (0.0005, 0.0015),
    "claude-sonnet-4-5": (0.003, 0.015),
    "claude-3-5-sonnet": (0.003, 0.015),
    "claude-3-haiku": (0.00025, 0.00125),
    "default": (0.002, 0.008),
}


@dataclass
class UsageRecord:
    timestamp: float
    tokens: int
    cost_usd: float


@dataclass
class BudgetEntry:
    key: str
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    total_calls: int = 0
    records: Deque[UsageRecord] = field(default_factory=deque, repr=False)

    def record(self, tokens: int, cost_usd: float) -> None:
        self.total_tokens += int(tokens or 0)
        self.total_cost_usd += float(cost_usd or 0.0)
        self.total_calls += 1
        self.records.append(UsageRecord(timestamp=time.monotonic(), tokens=int(tokens or 0), cost_usd=float(cost_usd or 0.0)))

    def window_tokens(self, seconds: float) -> int:
        cutoff = time.monotonic() - seconds
        return sum(item.tokens for item in self.records if item.timestamp >= cutoff)

    def window_cost(self, seconds: float) -> float:
        cutoff = time.monotonic() - seconds
        return sum(item.cost_usd for item in self.records if item.timestamp >= cutoff)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "total_tokens": self.total_tokens,
            "total_cost_usd": round(self.total_cost_usd, 6),
            "total_calls": self.total_calls,
        }


class BudgetGovernor:
    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        cfg = dict(config or {})
        self.enabled = bool(cfg.get("enabled", True))
        self.max_tokens_per_min = _optional_int(cfg.get("max_tokens_per_min"))
        self.max_cost_per_day_usd = _optional_float(cfg.get("max_cost_per_day_usd"))
        self.max_tokens_per_session = _optional_int(cfg.get("max_tokens_per_session"))
        self.estimated_tokens_per_call = int(cfg.get("estimated_tokens_per_call") or 4000)
        self._entries: Dict[str, BudgetEntry] = {}
        self._lock = threading.RLock()
        self._path = Path(cfg["_path"]).resolve() if cfg.get("_path") else None
        self._load()

    def _load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            return
        for raw in list(payload.get("entries") or []):
            item = dict(raw or {})
            key = str(item.get("key") or "")
            if not key:
                continue
            self._entries[key] = BudgetEntry(
                key=key,
                total_tokens=int(item.get("total_tokens") or 0),
                total_cost_usd=float(item.get("total_cost_usd") or 0.0),
                total_calls=int(item.get("total_calls") or 0),
            )

    def _save(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "entries": [entry.to_dict() for entry in self._entries.values()],
            }
            self._path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        except Exception:
            return

    def check(self, *, user_id: str = "default", session_id: str = "", estimated_tokens: Optional[int] = None) -> Dict[str, Any]:
        if not self.enabled:
            return {"allowed": True, "disabled": True}
        estimated = int(estimated_tokens or self.estimated_tokens_per_call)
        with self._lock:
            if self.max_tokens_per_min:
                used = self._entry(f"user:{user_id}").window_tokens(60.0)
                if used + estimated > self.max_tokens_per_min:
                    return {"allowed": False, "reason": f"Rate limit: {used}/{self.max_tokens_per_min} tokens/min"}
            if self.max_cost_per_day_usd:
                spent = self._entry(f"user:{user_id}").window_cost(86400.0)
                if spent >= self.max_cost_per_day_usd:
                    return {"allowed": False, "reason": f"Daily budget: ${spent:.4f}/${self.max_cost_per_day_usd:.2f}"}
            if session_id and self.max_tokens_per_session:
                used = self._entry(f"session:{session_id}").total_tokens
                if used + estimated > self.max_tokens_per_session:
                    return {"allowed": False, "reason": f"Session limit: {used}/{self.max_tokens_per_session} tokens"}
        return {"allowed": True}

    def enforce(self, *, user_id: str = "default", session_id: str = "", estimated_tokens: Optional[int] = None) -> None:
        result = self.check(user_id=user_id, session_id=session_id, estimated_tokens=estimated_tokens)
        if not result.get("allowed", False):
            raise ProviderError(str(result.get("reason") or "budget denied request"), retryable=False, rate_limited=True)

    def record(self, *, user_id: str = "default", session_id: str = "", provider_id: str = "", model: str = "default", prompt_tokens: int = 0, completion_tokens: int = 0) -> Dict[str, Any]:
        tokens = max(0, int(prompt_tokens or 0)) + max(0, int(completion_tokens or 0))
        cost = estimate_cost(model, int(prompt_tokens or 0), int(completion_tokens or 0))
        with self._lock:
            self._entry(f"user:{user_id}").record(tokens, cost)
            if session_id:
                self._entry(f"session:{session_id}").record(tokens, cost)
            if str(provider_id or "").strip():
                self._entry(f"provider:{str(provider_id).strip()}").record(tokens, cost)
            self._save()
        return {"recorded": True, "tokens": tokens, "cost_usd": round(cost, 6)}

    def status(self, key: str = "") -> Dict[str, Any]:
        with self._lock:
            if key:
                return self._entry(key).to_dict()
            totals_entries = [entry for key, entry in self._entries.items() if key.startswith("user:")] or list(self._entries.values())
            total_tokens = sum(entry.total_tokens for entry in totals_entries)
            total_cost = sum(entry.total_cost_usd for entry in totals_entries)
            total_calls = sum(entry.total_calls for entry in totals_entries)
            providers = {
                key.split(":", 1)[1]: {
                    "provider_id": key.split(":", 1)[1],
                    "total_tokens": entry.total_tokens,
                    "total_cost_usd": round(entry.total_cost_usd, 6),
                    "total_calls": entry.total_calls,
                }
                for key, entry in self._entries.items()
                if key.startswith("provider:")
            }
            return {
                "enabled": self.enabled,
                "total_tokens": total_tokens,
                "total_cost_usd": round(total_cost, 6),
                "total_calls": total_calls,
                "keys_tracked": len(self._entries),
                "providers": providers,
                "limits": {
                    "max_tokens_per_min": self.max_tokens_per_min,
                    "max_cost_per_day_usd": self.max_cost_per_day_usd,
                    "max_tokens_per_session": self.max_tokens_per_session,
                },
            }

    def _entry(self, key: str) -> BudgetEntry:
        if key not in self._entries:
            self._entries[key] = BudgetEntry(key=key)
        return self._entries[key]


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    prompt_cost, completion_cost = _PRICING.get(str(model or "").lower(), _PRICING["default"])
    return (max(0, prompt_tokens) / 1000 * prompt_cost) + (max(0, completion_tokens) / 1000 * completion_cost)


def _optional_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
        return parsed if parsed > 0 else None
    except Exception:
        return None


def _optional_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
        return parsed if parsed > 0 else None
    except Exception:
        return None


_budget_governor: Optional[BudgetGovernor] = None


def init_budget_governor(config: Optional[Dict[str, Any]] = None, data_root: Path | str | None = None) -> BudgetGovernor:
    global _budget_governor
    cfg = dict(config or {})
    if data_root:
        cfg["_path"] = str(Path(data_root).resolve() / "usage" / "budget.json")
    _budget_governor = BudgetGovernor(cfg)
    return _budget_governor


def get_budget_governor() -> BudgetGovernor:
    global _budget_governor
    if _budget_governor is None:
        _budget_governor = BudgetGovernor({"enabled": False})
    return _budget_governor
