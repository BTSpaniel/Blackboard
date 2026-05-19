"""Governor status and control endpoints."""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter
from pydantic import BaseModel

from blackboard.governors.budget import get_budget_governor
from blackboard.governors.capability import get_capability_governor
from blackboard.governors.data_protection import get_data_protection_governor
from blackboard.governors.health import get_health_governor
from blackboard.governors.trust import get_trust_governor
from blackboard.react.tool_policy import get_tool_policy

router = APIRouter(prefix="/api/governors", tags=["governors"])


class CapabilityRequest(BaseModel):
    capability: str


class DataProtectionRequest(BaseModel):
    text: str
    operation: str = "inspect"


class TrustGrantRequest(BaseModel):
    source_id: str
    level: int | str
    notes: str = ""


class TrustSourceRequest(BaseModel):
    source_id: str


@router.get("/budget")
async def budget_status() -> Dict[str, Any]:
    return get_budget_governor().status()


@router.get("/health")
async def health_status() -> Dict[str, Any]:
    return get_health_governor().status()


@router.get("/capabilities")
async def capability_status() -> Dict[str, Any]:
    return get_capability_governor().status()


@router.post("/capabilities/enable")
async def capability_enable(body: CapabilityRequest) -> Dict[str, Any]:
    return get_capability_governor().enable(body.capability)


@router.post("/capabilities/disable")
async def capability_disable(body: CapabilityRequest) -> Dict[str, Any]:
    return get_capability_governor().disable(body.capability)


@router.get("/tool-policy")
async def tool_policy_status() -> Dict[str, Any]:
    return get_tool_policy().status()


@router.get("/trust")
async def trust_status() -> Dict[str, Any]:
    return get_trust_governor().status()


@router.post("/trust/grant")
async def trust_grant(body: TrustGrantRequest) -> Dict[str, Any]:
    record = get_trust_governor().grant(body.source_id, body.level, notes=body.notes)
    return record.to_dict()


@router.post("/trust/revoke")
async def trust_revoke(body: TrustSourceRequest) -> Dict[str, Any]:
    return {"revoked": get_trust_governor().revoke(body.source_id), "source_id": body.source_id}


@router.post("/data-protection/inspect")
async def data_protection_inspect(body: DataProtectionRequest) -> Dict[str, Any]:
    result = get_data_protection_governor().protect_text(body.text, operation=body.operation)
    return {**result.metadata(), "protected": result.protected}
