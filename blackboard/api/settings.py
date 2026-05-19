"""Settings endpoints — read/update role assignments for active project."""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from blackboard.providers.registry import get_provider_registry
from blackboard.workspace.coding_settings import coding_snapshot, merge_coding_config, save_coding_overrides
from blackboard.workspace.server_access import (
    access_snapshot,
    delete_remote_token_override,
    is_loopback_request,
    merge_server_config,
    save_access_overrides,
    save_remote_token_override,
)

router = APIRouter(prefix="/api/settings", tags=["settings"])


class RoleAssign(BaseModel):
    role: str
    profile: str


class ServerAccessBody(BaseModel):
    lan_enabled: bool = False
    remote_enabled: bool = False
    public_base_url: str = ""
    trust_forwarded_for: bool = False


class RemoteTokenBody(BaseModel):
    token: str


class RemoteInviteBody(BaseModel):
    name: str = "Remote User"
    expires_hours: int = 24


class CodingSettingsBody(BaseModel):
    max_concurrent: int = 4


def _require_local_admin(request: Request) -> None:
    server_config = merge_server_config(getattr(request.app.state, "server_config", {}) or {})
    access = dict(server_config.get("access") or {})
    if not is_loopback_request(
        str(getattr(request.client, "host", "") or ""),
        dict(request.headers or {}),
        trust_forwarded_for=bool(access.get("trust_forwarded_for")),
    ):
        raise HTTPException(403, "local_admin_required")


@router.get("")
async def get_settings(request: Request) -> Dict[str, Any]:
    registry = get_provider_registry()
    server_config = merge_server_config(getattr(request.app.state, "server_config", {}) or {})
    return {
        "providers": registry.list_profiles(),
        "roles": registry.list_roles(),
        "data_root": str(request.app.state.data_root),
        "server_access": access_snapshot(
            server_config,
            data_root=request.app.state.data_root,
            remote_share_manager=getattr(request.app.state, "remote_share", None),
            protection_feedback_manager=getattr(request.app.state, "protection_feedback", None),
        ),
        "coding": coding_snapshot(
            getattr(request.app.state, "coding_config", {}) or {},
            runtime_max_concurrent=getattr(getattr(request.app.state, "coding_jobs", None), "_max_active_runs", 1),
        ),
    }


@router.get("/coding")
async def get_coding_settings(request: Request) -> Dict[str, Any]:
    return coding_snapshot(
        getattr(request.app.state, "coding_config", {}) or {},
        runtime_max_concurrent=getattr(getattr(request.app.state, "coding_jobs", None), "_max_active_runs", 1),
    )


@router.put("/coding")
async def update_coding_settings(body: CodingSettingsBody, request: Request) -> Dict[str, Any]:
    _require_local_admin(request)
    merged = save_coding_overrides(
        request.app.state.data_root,
        {"max_concurrent": int(body.max_concurrent)},
    )
    request.app.state.coding_config = merge_coding_config(getattr(request.app.state, "coding_config", {}) or {}, merged)
    return coding_snapshot(
        request.app.state.coding_config,
        runtime_max_concurrent=getattr(getattr(request.app.state, "coding_jobs", None), "_max_active_runs", 1),
    )


@router.get("/access")
async def get_server_access(request: Request) -> Dict[str, Any]:
    server_config = merge_server_config(getattr(request.app.state, "server_config", {}) or {})
    return access_snapshot(
        server_config,
        data_root=request.app.state.data_root,
        remote_share_manager=getattr(request.app.state, "remote_share", None),
        protection_feedback_manager=getattr(request.app.state, "protection_feedback", None),
    )


@router.get("/access/protection")
async def get_access_protection(request: Request, limit: int = 20) -> Dict[str, Any]:
    protection_feedback = getattr(request.app.state, "protection_feedback", None)
    if protection_feedback is None:
        return {"window_s": 0.0, "cooldown_s": 0.0, "hard_cooldown_s": 0.0, "active_cooldowns": 0, "revoked_total": 0, "subjects": [], "events": []}
    return protection_feedback.snapshot(limit=max(1, min(int(limit or 20), 100)))


@router.put("/access")
async def update_server_access(body: ServerAccessBody, request: Request) -> Dict[str, Any]:
    _require_local_admin(request)
    current = merge_server_config(getattr(request.app.state, "server_config", {}) or {})
    updated_access = save_access_overrides(
        request.app.state.data_root,
        {
            "lan_enabled": body.lan_enabled,
            "remote_enabled": body.remote_enabled,
            "public_base_url": str(body.public_base_url or "").strip(),
            "trust_forwarded_for": body.trust_forwarded_for,
        },
    )
    merged = merge_server_config({**current, "access": {**dict(current.get("access") or {}), **updated_access}})
    request.app.state.server_config = merged
    remote_share = getattr(request.app.state, "remote_share", None)
    if remote_share is not None and not body.remote_enabled and bool(remote_share.enabled):
        await remote_share.disable()
    return access_snapshot(
        merged,
        data_root=request.app.state.data_root,
        remote_share_manager=remote_share,
        protection_feedback_manager=getattr(request.app.state, "protection_feedback", None),
    )


@router.post("/access/remote-token")
async def set_remote_token_override(body: RemoteTokenBody, request: Request) -> Dict[str, Any]:
    _require_local_admin(request)
    token = str(body.token or "").strip()
    if not token:
        raise HTTPException(400, "token is required")
    save_remote_token_override(request.app.state.data_root, token)
    server_config = merge_server_config(getattr(request.app.state, "server_config", {}) or {})
    return access_snapshot(
        server_config,
        data_root=request.app.state.data_root,
        remote_share_manager=getattr(request.app.state, "remote_share", None),
        protection_feedback_manager=getattr(request.app.state, "protection_feedback", None),
    )


@router.delete("/access/remote-token")
async def clear_remote_token_override(request: Request) -> Dict[str, Any]:
    _require_local_admin(request)
    delete_remote_token_override(request.app.state.data_root)
    server_config = merge_server_config(getattr(request.app.state, "server_config", {}) or {})
    return access_snapshot(
        server_config,
        data_root=request.app.state.data_root,
        remote_share_manager=getattr(request.app.state, "remote_share", None),
        protection_feedback_manager=getattr(request.app.state, "protection_feedback", None),
    )


@router.post("/access/share/enable")
async def enable_remote_share(request: Request) -> Dict[str, Any]:
    _require_local_admin(request)
    server_config = merge_server_config(getattr(request.app.state, "server_config", {}) or {})
    access = dict(server_config.get("access") or {})
    if not access.get("remote_enabled"):
        raise HTTPException(400, "remote access must be enabled first")
    remote_share = getattr(request.app.state, "remote_share", None)
    if remote_share is None:
        raise HTTPException(500, "remote share unavailable")
    await remote_share.enable(public_base_url=str(access.get("public_base_url") or ""))
    return access_snapshot(
        server_config,
        data_root=request.app.state.data_root,
        remote_share_manager=remote_share,
        protection_feedback_manager=getattr(request.app.state, "protection_feedback", None),
    )


@router.post("/access/share/disable")
async def disable_remote_share(request: Request) -> Dict[str, Any]:
    _require_local_admin(request)
    server_config = merge_server_config(getattr(request.app.state, "server_config", {}) or {})
    remote_share = getattr(request.app.state, "remote_share", None)
    if remote_share is None:
        raise HTTPException(500, "remote share unavailable")
    await remote_share.disable()
    return access_snapshot(
        server_config,
        data_root=request.app.state.data_root,
        remote_share_manager=remote_share,
        protection_feedback_manager=getattr(request.app.state, "protection_feedback", None),
    )


@router.get("/access/share/invites")
async def list_remote_share_invites(request: Request) -> Dict[str, Any]:
    _require_local_admin(request)
    server_config = merge_server_config(getattr(request.app.state, "server_config", {}) or {})
    access = dict(server_config.get("access") or {})
    remote_share = getattr(request.app.state, "remote_share", None)
    if remote_share is None:
        raise HTTPException(500, "remote share unavailable")
    return {"invites": remote_share.list_invites(public_base_url=str(access.get("public_base_url") or ""))}


@router.get("/access/share/audit")
async def list_remote_share_audit(request: Request, limit: int = 50) -> Dict[str, Any]:
    _require_local_admin(request)
    remote_share = getattr(request.app.state, "remote_share", None)
    if remote_share is None:
        raise HTTPException(500, "remote share unavailable")
    return {"events": remote_share.audit_events(limit=max(1, min(int(limit or 50), 200)))}


@router.post("/access/share/invites")
async def create_remote_share_invite(body: RemoteInviteBody, request: Request) -> Dict[str, Any]:
    _require_local_admin(request)
    server_config = merge_server_config(getattr(request.app.state, "server_config", {}) or {})
    access = dict(server_config.get("access") or {})
    remote_share = getattr(request.app.state, "remote_share", None)
    if remote_share is None:
        raise HTTPException(500, "remote share unavailable")
    invite = remote_share.create_invite(
        name=str(body.name or "Remote User").strip() or "Remote User",
        hours=max(1, int(body.expires_hours or 24)),
        public_base_url=str(access.get("public_base_url") or ""),
    )
    return {"invite": invite}


@router.delete("/access/share/invites/{token_id}")
async def revoke_remote_share_invite(token_id: str, request: Request) -> Dict[str, Any]:
    _require_local_admin(request)
    remote_share = getattr(request.app.state, "remote_share", None)
    if remote_share is None:
        raise HTTPException(500, "remote share unavailable")
    if not remote_share.revoke_invite(token_id):
        raise HTTPException(404, "invite not found")
    return {"ok": True, "token_id": token_id}
