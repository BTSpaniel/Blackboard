from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set


class ApprovalMode(str, Enum):
    MANUAL = "manual"
    TRUSTED = "trusted"
    AUTOMATION = "automation"


@dataclass
class ApprovalDecision:
    tool_name: str
    approved: bool
    reason: str = ""
    mode: ApprovalMode = ApprovalMode.MANUAL
    timestamp: float = field(default_factory=time.time)
    session_id: str = ""
    principal: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "approved": self.approved,
            "reason": self.reason,
            "mode": self.mode.value,
            "timestamp": self.timestamp,
            "session_id": self.session_id,
            "principal": self.principal,
        }


class ApprovalManager:
    def __init__(self, *, mode: str = "manual", min_trust_for_trusted: int = 2, grant_step_up_in_automation: bool = False) -> None:
        self._mode = self._coerce_mode(mode)
        self._min_trust_for_trusted = int(min_trust_for_trusted)
        self._grant_step_up_in_automation = bool(grant_step_up_in_automation)
        self._always_allow_tools: Set[str] = set()
        self._deny_tools: Set[str] = set()
        self._session_auto_accept: Set[str] = set()
        self._learned_patterns: Dict[str, Dict[str, Any]] = {}
        self._pending_counts: Dict[str, int] = {}
        self._decisions: List[ApprovalDecision] = []

    @staticmethod
    def _coerce_mode(value: object) -> ApprovalMode:
        try:
            return ApprovalMode(str(value or "manual").strip().lower())
        except Exception:
            return ApprovalMode.MANUAL

    def configure(self, *, mode: Optional[str] = None, min_trust_for_trusted: Optional[int] = None, grant_step_up_in_automation: Optional[bool] = None, always_allow_tools: Optional[List[str]] = None, deny_tools: Optional[List[str]] = None) -> Dict[str, Any]:
        if mode is not None:
            self._mode = self._coerce_mode(mode)
        if min_trust_for_trusted is not None:
            self._min_trust_for_trusted = int(min_trust_for_trusted)
        if grant_step_up_in_automation is not None:
            self._grant_step_up_in_automation = bool(grant_step_up_in_automation)
        if always_allow_tools is not None:
            self._always_allow_tools = {str(item or "").strip() for item in always_allow_tools if str(item or "").strip()}
        if deny_tools is not None:
            self._deny_tools = {str(item or "").strip() for item in deny_tools if str(item or "").strip()}
        return self.status()

    def respond(self, *, session_id: str = "", approved: bool, tool_name: str = "", always_allow_tool: str = "") -> ApprovalDecision:
        session = str(session_id or "").strip()
        tool = str(tool_name or always_allow_tool or "").strip()
        if approved and session:
            self._session_auto_accept.add(session)
        if approved and always_allow_tool:
            self._always_allow_tools.add(str(always_allow_tool).strip())
        if not approved and tool:
            self._deny_tools.add(tool)
        decision = ApprovalDecision(tool_name=tool, approved=bool(approved), reason="manual approval response", mode=self._mode, session_id=session)
        self._record(decision)
        return decision

    def accept_all(self, session_id: str = "") -> Dict[str, Any]:
        session = str(session_id or "").strip()
        if session:
            self._session_auto_accept.add(session)
        else:
            self._mode = ApprovalMode.AUTOMATION
        return self.status()

    def revoke_session(self, session_id: str) -> Dict[str, Any]:
        self._session_auto_accept.discard(str(session_id or "").strip())
        return self.status()

    def learn_pattern(self, *, tool_name: str, reason: str = "", min_trust: Optional[int] = None) -> Dict[str, Any]:
        tool = str(tool_name or "").strip()
        if not tool:
            return self.status()
        self._learned_patterns[tool] = {
            "tool_name": tool,
            "reason": str(reason or "learned approval pattern"),
            "min_trust": int(min_trust if min_trust is not None else self._min_trust_for_trusted),
            "created_at": time.time(),
            "uses": int((self._learned_patterns.get(tool) or {}).get("uses") or 0),
        }
        return self.status()

    def forget_pattern(self, tool_name: str) -> Dict[str, Any]:
        self._learned_patterns.pop(str(tool_name or "").strip(), None)
        return self.status()

    def learned_patterns(self) -> Dict[str, Any]:
        return {
            "learned_patterns": [dict(item) for item in self._learned_patterns.values()],
            "pending_counts": dict(self._pending_counts),
            "always_allow_tools": sorted(self._always_allow_tools),
        }

    def maybe_approve(self, tool_name: str, context: Dict[str, Any]) -> bool:
        tool = str(tool_name or "").strip()
        ctx = dict(context or {})
        session = str(ctx.get("session_id") or ctx.get("run_id") or "").strip()
        principal = str(ctx.get("principal") or ctx.get("principal_id") or "").strip()
        trust_level = int(ctx.get("trust_level") or 0)
        if not tool or tool in self._deny_tools:
            self._record(ApprovalDecision(tool, False, "tool denied by approval manager", self._mode, session_id=session, principal=principal))
            return False
        if bool(ctx.get("confirmation_granted")):
            return True
        approved = False
        reason = "manual approval required"
        if tool in self._always_allow_tools:
            approved = True
            reason = "tool is always allowed"
        elif tool in self._learned_patterns and trust_level >= int(self._learned_patterns[tool].get("min_trust") or self._min_trust_for_trusted):
            approved = True
            reason = str(self._learned_patterns[tool].get("reason") or "learned approval pattern")
            self._learned_patterns[tool]["uses"] = int(self._learned_patterns[tool].get("uses") or 0) + 1
            self._learned_patterns[tool]["last_used_at"] = time.time()
        elif session and session in self._session_auto_accept:
            approved = True
            reason = "session auto-approval enabled"
        elif self._mode == ApprovalMode.AUTOMATION:
            approved = True
            reason = "automation mode enabled"
        elif self._mode == ApprovalMode.TRUSTED and trust_level >= self._min_trust_for_trusted:
            approved = True
            reason = "trusted mode approved principal"
        self._record(ApprovalDecision(tool, approved, reason, self._mode, session_id=session, principal=principal))
        if not approved:
            self._pending_counts[tool] = int(self._pending_counts.get(tool) or 0) + 1
        return approved

    def grants_step_up(self, context: Dict[str, Any]) -> bool:
        ctx = dict(context or {})
        if bool(ctx.get("step_up_verified")):
            return True
        if not self._grant_step_up_in_automation:
            return False
        session = str(ctx.get("session_id") or ctx.get("run_id") or "").strip()
        trust_level = int(ctx.get("trust_level") or 0)
        return self._mode == ApprovalMode.AUTOMATION or bool(session and session in self._session_auto_accept) or (self._mode == ApprovalMode.TRUSTED and trust_level >= self._min_trust_for_trusted)

    def status(self) -> Dict[str, Any]:
        return {
            "mode": self._mode.value,
            "min_trust_for_trusted": self._min_trust_for_trusted,
            "grant_step_up_in_automation": self._grant_step_up_in_automation,
            "always_allow_tools": sorted(self._always_allow_tools),
            "deny_tools": sorted(self._deny_tools),
            "session_auto_accept": sorted(self._session_auto_accept),
            "learned_patterns": [dict(item) for item in self._learned_patterns.values()],
            "pending_counts": dict(self._pending_counts),
            "recent_decisions": [item.to_dict() for item in self._decisions[-20:]],
        }

    def _record(self, decision: ApprovalDecision) -> None:
        self._decisions.append(decision)
        if len(self._decisions) > 500:
            self._decisions = self._decisions[-500:]


_manager: Optional[ApprovalManager] = None


def init_approval_manager(config: Optional[Dict[str, Any]] = None) -> ApprovalManager:
    global _manager
    cfg = dict(config or {})
    _manager = ApprovalManager(
        mode=str(cfg.get("mode") or "manual"),
        min_trust_for_trusted=int(cfg.get("min_trust_for_trusted") or 2),
        grant_step_up_in_automation=bool(cfg.get("grant_step_up_in_automation", False)),
    )
    _manager.configure(
        always_allow_tools=list(cfg.get("always_allow_tools") or []),
        deny_tools=list(cfg.get("deny_tools") or []),
    )
    for item in list(cfg.get("learned_patterns") or []):
        if isinstance(item, dict):
            _manager.learn_pattern(tool_name=str(item.get("tool_name") or item.get("tool") or ""), reason=str(item.get("reason") or ""), min_trust=item.get("min_trust"))
        else:
            _manager.learn_pattern(tool_name=str(item or ""))
    return _manager


def get_approval_manager() -> ApprovalManager:
    global _manager
    if _manager is None:
        _manager = ApprovalManager()
    return _manager
