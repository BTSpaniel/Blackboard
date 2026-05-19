from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List

from blackboard.workspace.redaction import sanitize_inline_text, sanitize_mapping


@dataclass(slots=True)
class ProtectionSubject:
    client_ip: str
    strikes: float = 0.0
    cooldown_until: float = 0.0
    revoked_count: int = 0
    last_reason: str = ""
    last_path: str = ""
    last_seen: float = field(default_factory=time.time)


class ProtectionFeedback:
    def __init__(self, *, window_s: float = 900.0, cooldown_s: float = 60.0, hard_cooldown_s: float = 300.0, max_events: int = 200) -> None:
        self._window_s = float(window_s)
        self._cooldown_s = float(cooldown_s)
        self._hard_cooldown_s = float(hard_cooldown_s)
        self._max_events = max(20, int(max_events))
        self._subjects: Dict[str, ProtectionSubject] = {}
        self._events: List[Dict[str, Any]] = []

    def _normalize_ip(self, client_ip: str) -> str:
        return sanitize_inline_text(str(client_ip or ""), max_chars=120)

    def _normalize_reason(self, reason: str) -> str:
        return sanitize_inline_text(str(reason or ""), max_chars=80)

    def _normalize_path(self, path: str) -> str:
        return sanitize_inline_text(str(path or ""), max_chars=160)

    def _subject(self, client_ip: str) -> ProtectionSubject | None:
        key = self._normalize_ip(client_ip)
        if not key:
            return None
        subject = self._subjects.get(key)
        if subject is None:
            subject = ProtectionSubject(client_ip=key)
            self._subjects[key] = subject
        return subject

    def _prune(self) -> None:
        now = time.time()
        keep: Dict[str, ProtectionSubject] = {}
        for key, subject in self._subjects.items():
            if subject.cooldown_until > now or (now - float(subject.last_seen or 0.0)) <= self._window_s:
                keep[key] = subject
        self._subjects = keep
        self._events = [item for item in self._events[-self._max_events :] if (now - float(item.get("ts") or 0.0)) <= (self._window_s * 2)]

    def _record_event(self, kind: str, *, client_ip: str = "", reason: str = "", path: str = "", outcome: str = "", payload: Dict[str, Any] | None = None) -> Dict[str, Any]:
        event = {
            "ts": time.time(),
            "kind": sanitize_inline_text(str(kind or "protection.event"), max_chars=80),
            "client_ip": self._normalize_ip(client_ip),
            "reason": self._normalize_reason(reason),
            "path": self._normalize_path(path),
            "outcome": sanitize_inline_text(str(outcome or "ok"), max_chars=40),
            "payload": sanitize_mapping(dict(payload or {}), max_chars=240),
        }
        self._events.append(event)
        if len(self._events) > self._max_events:
            self._events = self._events[-self._max_events :]
        return event

    def evaluate(self, client_ip: str) -> Dict[str, Any]:
        self._prune()
        subject = self._subject(client_ip)
        if subject is None:
            return {"allowed": True, "cooldown_until": 0.0, "cooldown_remaining_s": 0.0, "strikes": 0.0}
        now = time.time()
        remaining = max(0.0, float(subject.cooldown_until or 0.0) - now)
        return {
            "allowed": remaining <= 0.0,
            "cooldown_until": float(subject.cooldown_until or 0.0),
            "cooldown_remaining_s": remaining,
            "strikes": float(subject.strikes or 0.0),
            "revoked_count": int(subject.revoked_count or 0),
            "last_reason": subject.last_reason,
        }

    def record_denial(self, *, client_ip: str, reason: str, path: str = "", weight: float = 1.0) -> Dict[str, Any]:
        self._prune()
        subject = self._subject(client_ip)
        if subject is None:
            return self.evaluate(client_ip)
        now = time.time()
        subject.last_seen = now
        subject.last_reason = self._normalize_reason(reason)
        subject.last_path = self._normalize_path(path)
        subject.strikes = max(0.0, float(subject.strikes or 0.0)) + max(0.25, float(weight or 1.0))
        if subject.strikes >= 5.0:
            subject.cooldown_until = max(float(subject.cooldown_until or 0.0), now + self._hard_cooldown_s)
        elif subject.strikes >= 3.0:
            subject.cooldown_until = max(float(subject.cooldown_until or 0.0), now + self._cooldown_s)
        self._record_event(
            "protection.denial",
            client_ip=subject.client_ip,
            reason=subject.last_reason,
            path=subject.last_path,
            outcome="denied",
            payload={"strikes": subject.strikes, "cooldown_until": subject.cooldown_until},
        )
        return self.evaluate(subject.client_ip)

    def record_allow(self, *, client_ip: str, reason: str, path: str = "") -> None:
        subject = self._subject(client_ip)
        if subject is None:
            return
        subject.last_seen = time.time()
        if float(subject.strikes or 0.0) > 0.0:
            subject.strikes = max(0.0, float(subject.strikes or 0.0) - 0.25)
        self._record_event("protection.allow", client_ip=subject.client_ip, reason=reason, path=path, outcome="allowed")

    def observe_remote_share_event(self, event: Dict[str, Any]) -> None:
        payload = dict(event.get("payload") or {})
        client_ip = self._normalize_ip(str(payload.get("remote_ip") or ""))
        kind = sanitize_inline_text(str(event.get("event") or event.get("kind") or "remote_share.event"), max_chars=80)
        outcome = sanitize_inline_text(str(event.get("outcome") or "ok"), max_chars=40)
        if not client_ip:
            self._record_event(kind, reason=str(payload.get("reason") or ""), outcome=outcome, payload=payload)
            return
        subject = self._subject(client_ip)
        if subject is None:
            return
        subject.last_seen = time.time()
        if kind == "remote_share.auto_revoked":
            subject.revoked_count = int(subject.revoked_count or 0) + 1
            subject.last_reason = self._normalize_reason(str(payload.get("reason") or "remote_share_auto_revoked"))
            subject.cooldown_until = max(float(subject.cooldown_until or 0.0), time.time() + self._cooldown_s)
        self._record_event(kind, client_ip=client_ip, reason=str(payload.get("reason") or ""), outcome=outcome, payload=payload)

    def snapshot(self, *, limit: int = 20) -> Dict[str, Any]:
        self._prune()
        now = time.time()
        subjects = sorted(self._subjects.values(), key=lambda item: (float(item.cooldown_until or 0.0), float(item.last_seen or 0.0)), reverse=True)
        return {
            "window_s": self._window_s,
            "cooldown_s": self._cooldown_s,
            "hard_cooldown_s": self._hard_cooldown_s,
            "active_cooldowns": sum(1 for item in subjects if float(item.cooldown_until or 0.0) > now),
            "revoked_total": sum(int(item.revoked_count or 0) for item in subjects),
            "subjects": [
                {
                    "client_ip": item.client_ip,
                    "strikes": float(item.strikes or 0.0),
                    "cooldown_until": float(item.cooldown_until or 0.0),
                    "cooldown_remaining_s": max(0.0, float(item.cooldown_until or 0.0) - now),
                    "revoked_count": int(item.revoked_count or 0),
                    "last_reason": item.last_reason,
                    "last_path": item.last_path,
                    "last_seen": float(item.last_seen or 0.0),
                }
                for item in subjects[: max(1, int(limit or 20))]
            ],
            "events": list(self._events[-max(1, int(limit or 20)) :]),
        }
