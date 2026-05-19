"""Lightweight health governor with rolling stats and circuit state."""
from __future__ import annotations

import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Dict, Optional


@dataclass
class HealthEvent:
    timestamp: float
    success: bool
    duration_ms: float


@dataclass
class HealthEntry:
    name: str
    failure_threshold: int = 5
    cooldown_s: float = 30.0
    events: Deque[HealthEvent] = field(default_factory=lambda: deque(maxlen=100))
    consecutive_failures: int = 0
    open_until: float = 0.0

    def allow(self) -> bool:
        return self.open_until <= time.monotonic()

    def record(self, success: bool, duration_ms: float = 0.0) -> None:
        self.events.append(HealthEvent(timestamp=time.time(), success=bool(success), duration_ms=float(duration_ms or 0.0)))
        if success:
            self.consecutive_failures = 0
            if self.open_until <= time.monotonic():
                self.open_until = 0.0
        else:
            self.consecutive_failures += 1
            if self.consecutive_failures >= self.failure_threshold:
                self.open_until = time.monotonic() + self.cooldown_s

    def to_dict(self) -> Dict[str, Any]:
        total = len(self.events)
        successes = sum(1 for event in self.events if event.success)
        return {
            "name": self.name,
            "state": "open" if not self.allow() else "closed",
            "consecutive_failures": self.consecutive_failures,
            "success_rate": round(successes / max(1, total) * 100, 1),
            "total_events": total,
            "open_for_s": round(max(0.0, self.open_until - time.monotonic()), 2),
        }

    def to_payload(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "failure_threshold": self.failure_threshold,
            "cooldown_s": self.cooldown_s,
            "consecutive_failures": self.consecutive_failures,
            "open_until": self.open_until,
            "events": [
                {
                    "timestamp": event.timestamp,
                    "success": event.success,
                    "duration_ms": event.duration_ms,
                }
                for event in self.events
            ],
        }


class HealthGovernor:
    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        cfg = dict(config or {})
        self.failure_threshold = int(cfg.get("failure_threshold") or 5)
        self.cooldown_s = float(cfg.get("cooldown_s") or 30.0)
        self._entries: Dict[str, HealthEntry] = {}
        self._started_at = time.monotonic()
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
            name = str(item.get("name") or "")
            if not name:
                continue
            entry = HealthEntry(
                name=name,
                failure_threshold=int(item.get("failure_threshold") or self.failure_threshold),
                cooldown_s=float(item.get("cooldown_s") or self.cooldown_s),
            )
            entry.consecutive_failures = int(item.get("consecutive_failures") or 0)
            entry.open_until = float(item.get("open_until") or 0.0)
            for raw_event in list(item.get("events") or []):
                event = dict(raw_event or {})
                entry.events.append(HealthEvent(
                    timestamp=float(event.get("timestamp") or 0.0),
                    success=bool(event.get("success", False)),
                    duration_ms=float(event.get("duration_ms") or 0.0),
                ))
            self._entries[name] = entry

    def _save(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "entries": [entry.to_payload() for entry in self._entries.values()],
            }
            self._path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        except Exception:
            return

    def allow_call(self, name: str) -> bool:
        with self._lock:
            return self._entry(name).allow()

    def record_call(self, name: str, *, success: bool, duration_ms: float = 0.0) -> None:
        with self._lock:
            self._entry(name).record(success, duration_ms)
            self._save()

    def status(self) -> Dict[str, Any]:
        with self._lock:
            circuits = {name: entry.to_dict() for name, entry in self._entries.items()}
            open_circuits = [name for name, entry in self._entries.items() if not entry.allow()]
            return {
                "status": "degraded" if open_circuits else "ok",
                "uptime_s": round(time.monotonic() - self._started_at, 2),
                "open_circuits": open_circuits,
                "circuits": circuits,
            }

    def _entry(self, name: str) -> HealthEntry:
        key = str(name or "unknown")
        if key not in self._entries:
            self._entries[key] = HealthEntry(name=key, failure_threshold=self.failure_threshold, cooldown_s=self.cooldown_s)
        return self._entries[key]


_health_governor: Optional[HealthGovernor] = None


def init_health_governor(config: Optional[Dict[str, Any]] = None, data_root: Path | str | None = None) -> HealthGovernor:
    global _health_governor
    cfg = dict(config or {})
    if data_root:
        cfg["_path"] = str(Path(data_root).resolve() / "usage" / "health.json")
    _health_governor = HealthGovernor(cfg)
    return _health_governor


def get_health_governor() -> HealthGovernor:
    global _health_governor
    if _health_governor is None:
        _health_governor = HealthGovernor()
    return _health_governor
