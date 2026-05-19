"""Provider usage endpoint."""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter

from blackboard.governors.budget import get_budget_governor
from blackboard.governors.health import get_health_governor
from blackboard.providers.usage import get_usage_tracker

router = APIRouter(prefix="/api/usage", tags=["usage"])


@router.get("")
async def usage() -> Dict[str, Any]:
    snapshot = get_usage_tracker().snapshot()
    return {
        "providers": snapshot.get("providers") or {},
        "tools": snapshot.get("tools") or {},
        "totals": snapshot.get("totals") or {},
        "budget": get_budget_governor().status(),
        "health": get_health_governor().status(),
    }


@router.post("/reset")
async def reset() -> Dict[str, Any]:
    get_usage_tracker().reset()
    return {"ok": True}
