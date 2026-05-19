from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter
from pydantic import BaseModel

from blackboard.react.approval import get_approval_manager

router = APIRouter(prefix="/api/approval", tags=["approval"])


class ApprovalResponseRequest(BaseModel):
    approved: bool
    session_id: str = ""
    tool_name: str = ""
    always_allow_tool: str = ""


class ApprovalSettingsRequest(BaseModel):
    mode: Optional[str] = None
    min_trust_for_trusted: Optional[int] = None
    grant_step_up_in_automation: Optional[bool] = None
    always_allow_tools: Optional[List[str]] = None
    deny_tools: Optional[List[str]] = None


class AcceptAllRequest(BaseModel):
    session_id: str = ""


class LearnedApprovalRequest(BaseModel):
    tool_name: str
    reason: str = ""
    min_trust: Optional[int] = None


@router.get("/settings")
async def approval_settings() -> Dict[str, Any]:
    return get_approval_manager().status()


@router.patch("/settings")
async def update_approval_settings(body: ApprovalSettingsRequest) -> Dict[str, Any]:
    return get_approval_manager().configure(
        mode=body.mode,
        min_trust_for_trusted=body.min_trust_for_trusted,
        grant_step_up_in_automation=body.grant_step_up_in_automation,
        always_allow_tools=body.always_allow_tools,
        deny_tools=body.deny_tools,
    )


@router.post("/respond")
async def respond_approval(body: ApprovalResponseRequest) -> Dict[str, Any]:
    decision = get_approval_manager().respond(
        session_id=body.session_id,
        approved=body.approved,
        tool_name=body.tool_name,
        always_allow_tool=body.always_allow_tool,
    )
    return {"decision": decision.to_dict(), "settings": get_approval_manager().status()}


@router.post("/accept-all")
async def accept_all(body: AcceptAllRequest) -> Dict[str, Any]:
    return get_approval_manager().accept_all(session_id=body.session_id)


@router.get("/learned")
async def learned_approval_patterns() -> Dict[str, Any]:
    return get_approval_manager().learned_patterns()


@router.post("/learned")
async def learn_approval_pattern(body: LearnedApprovalRequest) -> Dict[str, Any]:
    return get_approval_manager().learn_pattern(tool_name=body.tool_name, reason=body.reason, min_trust=body.min_trust)


@router.delete("/learned/{tool_name:path}")
async def forget_approval_pattern(tool_name: str) -> Dict[str, Any]:
    return get_approval_manager().forget_pattern(tool_name)


@router.delete("/sessions/{session_id}")
async def revoke_session_approval(session_id: str) -> Dict[str, Any]:
    return get_approval_manager().revoke_session(session_id)
