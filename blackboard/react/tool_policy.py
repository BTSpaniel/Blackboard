"""Tool policy gates for Blackboard ReAct tools."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import json
from pathlib import Path
from typing import Dict, List, Optional

from blackboard.governors.trust import TrustLevel


class ToolPermission(str, Enum):
    ALLOW = "allow"
    CONFIRM = "confirm"
    DENY = "deny"


class ToolSecurityLevel(str, Enum):
    READ = "read"
    PRIVATE = "private"
    NETWORK = "network"
    MUTATE = "mutate"
    ADMIN = "admin"


@dataclass(frozen=True)
class ToolPolicyResult:
    tool_name: str
    allowed: bool
    reason: str = ""
    security_level: ToolSecurityLevel = ToolSecurityLevel.READ
    requires_confirmation: bool = False
    min_trust: TrustLevel = TrustLevel.LOW
    requires_step_up: bool = False


@dataclass(frozen=True)
class ToolPolicyEntry:
    permission: ToolPermission = ToolPermission.ALLOW
    security_level: ToolSecurityLevel = ToolSecurityLevel.READ
    reason: str = ""
    min_trust: TrustLevel = TrustLevel.LOW
    requires_step_up: bool = False
    requires_context: List[str] = field(default_factory=list)


class ToolPolicy:
    def __init__(self, strict: bool = False, path: Path | None = None) -> None:
        self._strict = strict
        self._entries: Dict[str, ToolPolicyEntry] = {}
        self._path = Path(path).resolve() if path else None
        self._suspend_persist = True
        self.deny("git_force_push", "Force push is denied", ToolSecurityLevel.ADMIN)
        self.deny("git_push", "Remote pushes are denied from autonomous ReAct execution", ToolSecurityLevel.MUTATE)
        self.confirm("delete_file", "File deletion requires confirmation", ToolSecurityLevel.MUTATE, min_trust=TrustLevel.HIGH, requires_step_up=True)
        self._default_entries = dict(self._entries)
        self._load()
        self._suspend_persist = False

    @staticmethod
    def _serialize_entry(entry: ToolPolicyEntry) -> Dict[str, object]:
        return {
            "permission": entry.permission.value,
            "security_level": entry.security_level.value,
            "reason": entry.reason,
            "min_trust": int(entry.min_trust),
            "requires_step_up": entry.requires_step_up,
            "requires_context": list(entry.requires_context),
        }

    @staticmethod
    def _deserialize_entry(payload: Dict[str, object]) -> ToolPolicyEntry:
        permission = ToolPermission(str(payload.get("permission") or ToolPermission.ALLOW.value))
        security_level = ToolSecurityLevel(str(payload.get("security_level") or ToolSecurityLevel.READ.value))
        min_trust = ToolPolicy._coerce_trust_level(payload.get("min_trust", TrustLevel.LOW))
        if permission == ToolPermission.CONFIRM:
            return ToolPolicyEntry(
                permission,
                security_level,
                str(payload.get("reason") or ""),
                min_trust=min_trust,
                requires_step_up=bool(payload.get("requires_step_up", False)),
                requires_context=[str(item) for item in list(payload.get("requires_context") or [])],
            )
        return ToolPolicyEntry(
            permission,
            security_level,
            str(payload.get("reason") or ""),
            min_trust=min_trust,
            requires_step_up=bool(payload.get("requires_step_up", False)),
            requires_context=[str(item) for item in list(payload.get("requires_context") or [])],
        )

    def _load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            return
        entries = payload.get("entries") or {}
        if not isinstance(entries, dict):
            return
        for tool_name, raw in entries.items():
            try:
                self._entries[str(tool_name)] = self._deserialize_entry(dict(raw or {}))
            except Exception:
                continue

    def _save(self) -> None:
        if self._path is None or self._suspend_persist:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            entries = {
                name: self._serialize_entry(entry)
                for name, entry in self._entries.items()
                if self._default_entries.get(name) != entry
            }
            self._path.write_text(json.dumps({"entries": entries}, indent=2, sort_keys=True), encoding="utf-8")
        except Exception:
            return

    @staticmethod
    def _coerce_trust_level(value: object) -> TrustLevel:
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

    def allow(self, tool_name: str, security_level: ToolSecurityLevel = ToolSecurityLevel.READ, *, min_trust: TrustLevel = TrustLevel.LOW) -> None:
        self._entries[tool_name] = ToolPolicyEntry(ToolPermission.ALLOW, security_level, min_trust=min_trust)
        self._save()

    def deny(self, tool_name: str, reason: str = "", security_level: ToolSecurityLevel = ToolSecurityLevel.READ) -> None:
        self._entries[tool_name] = ToolPolicyEntry(ToolPermission.DENY, security_level, reason)
        self._save()

    def confirm(
        self,
        tool_name: str,
        reason: str = "",
        security_level: ToolSecurityLevel = ToolSecurityLevel.MUTATE,
        *,
        min_trust: TrustLevel = TrustLevel.LOW,
        requires_step_up: bool = False,
        requires_context: Optional[List[str]] = None,
    ) -> None:
        self._entries[tool_name] = ToolPolicyEntry(
            ToolPermission.CONFIRM,
            security_level,
            reason,
            min_trust=min_trust,
            requires_step_up=requires_step_up,
            requires_context=list(requires_context or []),
        )
        self._save()

    def check(self, tool_name: str, context: Optional[Dict[str, object]] = None) -> ToolPolicyResult:
        entry = self._entries.get(tool_name)
        ctx = dict(context or {})
        trust_level = self._coerce_trust_level(ctx.get("trust_level", TrustLevel.LOW))
        step_up_verified = bool(ctx.get("step_up_verified", False))
        confirmation_granted = bool(ctx.get("confirmation_granted", False))
        if entry is None:
            if self._strict:
                return ToolPolicyResult(tool_name=tool_name, allowed=False, reason="strict policy: tool not explicitly allowed")
            return ToolPolicyResult(tool_name=tool_name, allowed=True)
        missing_context = [key for key in entry.requires_context if key not in ctx or ctx.get(key) in (None, "")]
        if missing_context:
            return ToolPolicyResult(
                tool_name=tool_name,
                allowed=False,
                reason=f"missing required context: {', '.join(missing_context)}",
                security_level=entry.security_level,
                min_trust=entry.min_trust,
                requires_step_up=entry.requires_step_up,
            )
        if trust_level < entry.min_trust:
            return ToolPolicyResult(
                tool_name=tool_name,
                allowed=False,
                reason=entry.reason or f"tool requires trust level {entry.min_trust.name.lower()}",
                security_level=entry.security_level,
                min_trust=entry.min_trust,
                requires_step_up=entry.requires_step_up,
            )
        if entry.requires_step_up and not step_up_verified:
            return ToolPolicyResult(
                tool_name=tool_name,
                allowed=False,
                reason=entry.reason or "tool requires step-up verification",
                security_level=entry.security_level,
                requires_confirmation=True,
                min_trust=entry.min_trust,
                requires_step_up=True,
            )
        if entry.permission == ToolPermission.CONFIRM and not confirmation_granted:
            return ToolPolicyResult(
                tool_name=tool_name,
                allowed=False,
                reason=entry.reason or "tool requires confirmation",
                security_level=entry.security_level,
                requires_confirmation=True,
                min_trust=entry.min_trust,
                requires_step_up=entry.requires_step_up,
            )
        return ToolPolicyResult(
            tool_name=tool_name,
            allowed=entry.permission != ToolPermission.DENY,
            reason=entry.reason,
            security_level=entry.security_level,
            min_trust=entry.min_trust,
            requires_step_up=entry.requires_step_up,
        )

    def status(self) -> Dict[str, object]:
        return {
            "strict": self._strict,
            "entries": {
                name: {
                    "permission": entry.permission.value,
                    "security_level": entry.security_level.value,
                    "reason": entry.reason,
                    "min_trust": int(entry.min_trust),
                    "min_trust_name": entry.min_trust.name.lower(),
                    "requires_step_up": entry.requires_step_up,
                    "requires_context": list(entry.requires_context),
                }
                for name, entry in self._entries.items()
            },
        }


_tool_policy: Optional[ToolPolicy] = None


def init_tool_policy(strict: bool = False, data_root: Path | str | None = None) -> ToolPolicy:
    global _tool_policy
    path = Path(data_root).resolve() / "governors" / "tool_policy.json" if data_root else None
    _tool_policy = ToolPolicy(strict=strict, path=path)
    return _tool_policy


def get_tool_policy() -> ToolPolicy:
    global _tool_policy
    if _tool_policy is None:
        _tool_policy = ToolPolicy()
    return _tool_policy
