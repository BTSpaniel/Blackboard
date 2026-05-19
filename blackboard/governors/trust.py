"""Source trust governor for Blackboard tool and API safety gates."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Any, Dict, List, Optional


class TrustLevel(IntEnum):
    UNTRUSTED = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    OWNER = 4


@dataclass
class TrustRecord:
    source_id: str
    level: TrustLevel = TrustLevel.LOW
    notes: str = ""
    granted_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_id": self.source_id,
            "level": int(self.level),
            "level_name": self.level.name.lower(),
            "notes": self.notes,
            "granted_at": self.granted_at,
            "last_seen": self.last_seen,
        }


class TrustGovernor:
    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        cfg = dict(config or {})
        self._default = self._coerce_level(cfg.get("default_level", TrustLevel.LOW))
        self._records: Dict[str, TrustRecord] = {}
        self._aliases: Dict[str, str] = {}
        self._step_up_hashes: Dict[str, str] = {}
        self._elevations: Dict[str, float] = {}
        self._revoked: set[str] = set()
        self._path = Path(cfg["_path"]).resolve() if cfg.get("_path") else None
        self._suspend_persist = True
        owner = str(cfg.get("owner_principal") or "").strip()
        if owner:
            self.grant(owner, TrustLevel.OWNER, notes="configured owner principal")
        for source in list(cfg.get("trusted_sources", []) or []):
            self.grant(str(source), TrustLevel.HIGH, notes="configured trusted source")
        for source in list(cfg.get("owner_sources", []) or []):
            value = str(source)
            if owner:
                self.link(value, owner)
                self.grant(owner, TrustLevel.OWNER, notes="configured owner source")
            else:
                self.grant(value, TrustLevel.OWNER, notes="configured owner source")
        for alias, principal in dict(cfg.get("identity_links", {}) or {}).items():
            self.link(str(alias), str(principal))
        for principal, secret in dict(cfg.get("step_up_secrets", {}) or {}).items():
            self.set_step_up_secret(str(principal), str(secret))
        self._load()
        self._suspend_persist = False

    def _load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            return
        records = payload.get("records") or []
        if isinstance(records, list):
            for raw in records:
                item = dict(raw or {})
                source_id = self._normalize(item.get("source_id") or "")
                if not source_id:
                    continue
                self._records[source_id] = TrustRecord(
                    source_id=source_id,
                    level=self._coerce_level(item.get("level", TrustLevel.LOW)),
                    notes=str(item.get("notes") or ""),
                    granted_at=float(item.get("granted_at") or time.time()),
                    last_seen=float(item.get("last_seen") or time.time()),
                )
        aliases = payload.get("aliases") or {}
        if isinstance(aliases, dict):
            self._aliases.update({self._normalize(key): self._normalize(value) for key, value in aliases.items() if self._normalize(key) and self._normalize(value)})
        step_up_hashes = payload.get("step_up_hashes") or {}
        if isinstance(step_up_hashes, dict):
            self._step_up_hashes.update({self._normalize(key): str(value or "") for key, value in step_up_hashes.items() if self._normalize(key) and str(value or "")})
        revoked = payload.get("revoked") or []
        if isinstance(revoked, list):
            self._revoked.update(self._normalize(item) for item in revoked if self._normalize(item))
            for source_id in list(self._revoked):
                self._records.pop(source_id, None)

    def _save(self) -> None:
        if self._path is None or self._suspend_persist:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "records": [record.to_dict() for record in self._records.values()],
                "aliases": dict(self._aliases),
                "step_up_hashes": dict(self._step_up_hashes),
                "revoked": sorted(self._revoked),
            }
            self._path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        except Exception:
            return

    @staticmethod
    def _normalize(value: str) -> str:
        return str(value or "").strip()

    @staticmethod
    def _coerce_level(value: object) -> TrustLevel:
        if isinstance(value, TrustLevel):
            return value
        if isinstance(value, str):
            name = value.strip().upper()
            if name in TrustLevel.__members__:
                return TrustLevel[name]
        try:
            return TrustLevel(int(value))
        except Exception:
            return TrustLevel.LOW

    @staticmethod
    def _hash_secret(secret: str) -> str:
        return hashlib.sha256(str(secret or "").encode("utf-8")).hexdigest()

    def canonical(self, source_id: str) -> str:
        current = self._normalize(source_id)
        seen = set()
        while current and current in self._aliases and current not in seen:
            seen.add(current)
            current = self._normalize(self._aliases.get(current, ""))
        return current or self._normalize(source_id)

    def resolve_source_id(
        self,
        *,
        principal_id: str = "",
        source: str = "",
        user_id: str = "",
        session_id: str = "",
    ) -> str:
        explicit = self._normalize(principal_id)
        if explicit:
            return self.canonical(explicit)
        source_norm = self._normalize(source).lower() or "react"
        user_norm = self._normalize(user_id)
        if user_norm:
            return self.canonical(f"{source_norm}:user:{user_norm}")
        session_norm = self._normalize(session_id)
        if session_norm:
            return self.canonical(f"{source_norm}:session:{session_norm}")
        return self.canonical(source_norm)

    def link(self, source_id: str, principal_id: str) -> str:
        source_norm = self._normalize(source_id)
        principal_norm = self._normalize(principal_id)
        if not source_norm or not principal_norm:
            return principal_norm or source_norm
        canonical_principal = self.canonical(principal_norm)
        self._aliases[source_norm] = canonical_principal
        if source_norm in self._records and canonical_principal not in self._records:
            record = self._records[source_norm]
            self._records[canonical_principal] = TrustRecord(
                source_id=canonical_principal,
                level=record.level,
                notes=record.notes,
                granted_at=record.granted_at,
                last_seen=record.last_seen,
            )
        self._save()
        return canonical_principal

    def grant(self, source_id: str, level: TrustLevel | int | str, notes: str = "") -> TrustRecord:
        canonical_id = self.canonical(source_id)
        trust_level = self._coerce_level(level)
        self._revoked.discard(canonical_id)
        if canonical_id in self._records:
            self._records[canonical_id].level = trust_level
            self._records[canonical_id].notes = notes
            self._save()
            return self._records[canonical_id]
        record = TrustRecord(source_id=canonical_id, level=trust_level, notes=notes)
        self._records[canonical_id] = record
        self._save()
        return record

    def revoke(self, source_id: str) -> bool:
        canonical_id = self.canonical(source_id)
        if canonical_id:
            self._revoked.add(canonical_id)
        if canonical_id in self._records:
            del self._records[canonical_id]
            self._save()
            return True
        self._save()
        return bool(canonical_id)

    def level(self, source_id: str) -> TrustLevel:
        canonical_id = self.canonical(source_id)
        record = self._records.get(canonical_id)
        if record:
            record.last_seen = time.time()
            return record.level
        return self._default

    def check(self, source_id: str, min_level: TrustLevel | int | str = TrustLevel.LOW) -> Dict[str, Any]:
        actual = self.level(source_id)
        required = self._coerce_level(min_level)
        allowed = actual >= required
        return {
            "allowed": allowed,
            "source_id": self.canonical(source_id),
            "level": int(actual),
            "level_name": actual.name.lower(),
            "required_level": int(required),
            "required_level_name": required.name.lower(),
            "reason": "" if allowed else f"trust level {actual.name.lower()} is below required {required.name.lower()}",
        }

    def set_step_up_secret(self, principal_id: str, secret: str) -> None:
        canonical_id = self.canonical(principal_id)
        if canonical_id and str(secret or ""):
            self._step_up_hashes[canonical_id] = self._hash_secret(secret)
            self._save()

    def _elevation_key(self, principal_id: str, scope: str = "") -> str:
        canonical_id = self.canonical(principal_id)
        scope_norm = self._normalize(scope)
        return f"{canonical_id}::{scope_norm}" if scope_norm else canonical_id

    def has_step_up(self, principal_id: str, scope: str = "") -> bool:
        now = time.time()
        for key in (self._elevation_key(principal_id, scope), self._elevation_key(principal_id, "")):
            expires_at = float(self._elevations.get(key, 0) or 0)
            if expires_at > now:
                return True
            if expires_at:
                self._elevations.pop(key, None)
        return False

    def verify_step_up(self, principal_id: str, secret: str, *, scope: str = "", ttl_s: float = 600.0) -> bool:
        canonical_id = self.canonical(principal_id)
        expected = self._step_up_hashes.get(canonical_id)
        if not expected or not str(secret or ""):
            return False
        if not hmac.compare_digest(expected, self._hash_secret(secret)):
            return False
        self._elevations[self._elevation_key(canonical_id, scope)] = time.time() + max(30.0, float(ttl_s or 600.0))
        return True

    def status(self) -> Dict[str, Any]:
        return {
            "default_level": int(self._default),
            "default_level_name": self._default.name.lower(),
            "records": [record.to_dict() for record in sorted(self._records.values(), key=lambda item: int(item.level), reverse=True)],
            "aliases": dict(self._aliases),
            "step_up_principals": sorted(self._step_up_hashes),
            "active_elevations": sorted(key for key, expires_at in self._elevations.items() if expires_at > time.time()),
        }


_trust_governor: Optional[TrustGovernor] = None


def init_trust_governor(config: Optional[Dict[str, Any]] = None, data_root: Path | str | None = None) -> TrustGovernor:
    global _trust_governor
    cfg = dict(config or {})
    if data_root:
        cfg["_path"] = str(Path(data_root).resolve() / "governors" / "trust.json")
    _trust_governor = TrustGovernor(cfg)
    return _trust_governor


def get_trust_governor() -> TrustGovernor:
    global _trust_governor
    if _trust_governor is None:
        _trust_governor = TrustGovernor()
    return _trust_governor
