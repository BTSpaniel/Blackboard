"""Provider registry endpoints."""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from blackboard.kernel.bus import get_bus
from blackboard.providers.registry import get_provider_registry
from blackboard.workspace.role_overrides import save_override, delete_override
from blackboard.workspace.key_overrides import save_key, delete_key
from blackboard.workspace.model_overrides import save_model_override, delete_model_override

router = APIRouter(prefix="/api/providers", tags=["providers"])

_PROVIDER_PROBE_MIN_INTERVAL_S = 30.0
_SNAPSHOT_MIN_INTERVAL_S = 0.25
_last_provider_probe_at = 0.0
_last_snapshot_at = 0.0
_provider_probe_lock = asyncio.Lock()
_snapshot_lock = asyncio.Lock()


def _registry():
    try:
        return get_provider_registry()
    except RuntimeError:
        raise HTTPException(503, "provider registry not initialized")


@router.get("")
async def list_providers() -> Dict[str, Any]:
    reg = _registry()
    return {
        "profiles": reg.list_profiles(),
        "roles": reg.list_roles(),
    }


@router.get("/health")
async def all_health() -> Dict[str, Any]:
    reg = _registry()
    state = await sync_verified_provider_state(reg, probe=True, persist=False)
    return state["health"]


@router.post("/{profile_id}/health")
async def one_health(profile_id: str) -> Dict[str, Any]:
    reg = _registry()
    provider = reg.provider(profile_id)
    if provider is None:
        raise HTTPException(404, f"provider not found: {profile_id}")
    h = await provider.health()
    return {"id": profile_id, "ok": h.ok, "latency_ms": h.latency_ms, "error": h.error, "detail": h.detail}


# ── Role priority editing ────────────────────────────────────────


class RoleAssignmentBody(BaseModel):
    profile: str
    fallbacks: List[str] = []
    disabled: List[str] = []


def _health_payload(results) -> Dict[str, Any]:
    return {
        pid: {"ok": h.ok, "latency_ms": h.latency_ms, "error": h.error}
        for pid, h in results.items()
    }


def public_provider_snapshot(reg) -> Dict[str, Any]:
    profiles: List[Dict[str, Any]] = []
    for profile in reg.list_profiles():
        secret = profile.get("secret_status") if isinstance(profile.get("secret_status"), dict) else {}
        profiles.append({
            "id": profile.get("id") or "",
            "adapter": profile.get("adapter") or "",
            "model": profile.get("model") or "",
            "ok": profile.get("ok"),
            "latency_ms": profile.get("latency_ms"),
            "error": profile.get("error") or "",
            "available": bool(profile.get("available")),
            "secret_status": {
                "required": bool(secret.get("required")),
                "has_value": bool(secret.get("has_value")),
            },
        })
    return {
        "profiles": profiles,
        "roles": reg.list_roles(),
    }


async def _probe_provider_health(reg, *, force: bool = False, min_interval_s: float = _PROVIDER_PROBE_MIN_INTERVAL_S):
    global _last_provider_probe_at
    async with _provider_probe_lock:
        now = time.monotonic()
        if not force and _last_provider_probe_at and (now - _last_provider_probe_at) < min_interval_s:
            return reg.health_snapshot(), False
        results = await reg.health_check_all()
        _last_provider_probe_at = time.monotonic()
        return results, True


async def _broadcast_snapshot(reg) -> None:
    global _last_snapshot_at
    try:
        async with _snapshot_lock:
            now = time.monotonic()
            wait_s = max(0.0, _SNAPSHOT_MIN_INTERVAL_S - (now - _last_snapshot_at))
            if wait_s:
                await asyncio.sleep(wait_s)
            _last_snapshot_at = time.monotonic()
        await get_bus().emit("providers:snapshot", public_provider_snapshot(reg))
    except Exception:
        pass


async def sync_verified_provider_state(
    reg,
    data_root=None,
    *,
    probe: bool = True,
    persist: bool = True,
    force_probe: bool = False,
    min_probe_interval_s: float = _PROVIDER_PROBE_MIN_INTERVAL_S,
) -> Dict[str, Any]:
    health_payload: Dict[str, Any] = {}
    if probe:
        try:
            results, _did_probe = await _probe_provider_health(
                reg,
                force=force_probe,
                min_interval_s=min_probe_interval_s,
            )
            health_payload = _health_payload(results)
            await get_bus().emit("providers:health", health_payload)
        except Exception:
            pass
    try:
        role_results = reg.auto_fill_all_roles()
    except Exception as exc:
        role_results = {"_sync": {"error": str(exc)}}
    if data_root is not None and persist:
        for role, result in list(role_results.items()):
            if "error" in result:
                continue
            try:
                save_override(
                    data_root,
                    role,
                    result["profile"],
                    result["fallbacks"],
                    disabled=result.get("disabled") or [],
                )
            except Exception as exc:
                role_results[role] = {"error": f"failed to persist: {exc}"}
    await _broadcast_snapshot(reg)
    return {"health": health_payload, "results": role_results}


@router.put("/roles/{role}")
async def update_role(role: str, body: RoleAssignmentBody, request: Request) -> Dict[str, Any]:
    """Reorder a role's primary + fallback chain (and optional disabled list).
    Persisted to disk; auto-broadcast over WS."""
    reg = _registry()
    try:
        updated = reg.update_role(role, body.profile, body.fallbacks, disabled=body.disabled)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    try:
        save_override(
            request.app.state.data_root, role,
            updated["profile"], updated["fallbacks"],
            disabled=updated.get("disabled") or [],
        )
    except Exception as exc:
        raise HTTPException(500, f"failed to persist override: {exc}")
    await sync_verified_provider_state(reg, request.app.state.data_root, probe=False)
    return updated


@router.post("/roles/{role}/auto-fill")
async def auto_fill_role(role: str, request: Request) -> Dict[str, Any]:
    """Rebuild this role's chain from **verified** providers (have API key AND
    most-recent health probe says reachable).

    A fresh health probe runs first so the decision uses current state rather
    than a stale cache. Profiles that fail either check stay in the chain but
    are pushed into ``disabled`` so they remain visible — runtime skips them.
    Persists to disk and broadcasts the new snapshot.
    """
    reg = _registry()
    try:
        await _probe_provider_health(reg)
    except Exception:
        pass
    try:
        updated = reg.auto_fill_role(role)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    try:
        save_override(
            request.app.state.data_root, role,
            updated["profile"], updated["fallbacks"],
            disabled=updated.get("disabled") or [],
        )
    except Exception as exc:
        raise HTTPException(500, f"failed to persist auto-fill: {exc}")
    await sync_verified_provider_state(reg, request.app.state.data_root, probe=False)
    return updated


@router.post("/roles/auto-fill-all")
async def auto_fill_all_roles(request: Request) -> Dict[str, Any]:
    """Run :func:`auto_fill_role` across every configured role in one trip.

    Useful right after the user sets an API key and wants every role chain
    re-derived from "what actually works". Returns a per-role result map; if
    a role can't be filled (no verified providers anywhere) it gets an
    ``{"error": "..."}`` entry instead of failing the whole request.
    """
    reg = _registry()
    try:
        await _probe_provider_health(reg)
    except Exception:
        pass
    results = reg.auto_fill_all_roles()
    persisted: Dict[str, Any] = {}
    for role, result in results.items():
        if "error" in result:
            persisted[role] = result
            continue
        try:
            save_override(
                request.app.state.data_root, role,
                result["profile"], result["fallbacks"],
                disabled=result.get("disabled") or [],
            )
            persisted[role] = result
        except Exception as exc:
            persisted[role] = {"error": f"failed to persist: {exc}"}
    await sync_verified_provider_state(reg, request.app.state.data_root, probe=False)
    return {"results": persisted}


# ── API key set/clear (per-profile, no YAML edit needed) ────────


class SetKeyBody(BaseModel):
    value: str


class SetModelBody(BaseModel):
    model: str
    models: List[str] = []


@router.post("/{profile_id}/key")
async def set_profile_key(profile_id: str, body: SetKeyBody, request: Request) -> Dict[str, Any]:
    """Set an inline API key for ``profile_id``. Persists to ``data/providers/key_overrides.json``
    and applies it to the live provider in-memory immediately. Re-broadcasts the snapshot."""
    reg = _registry()
    provider = reg.provider(profile_id)
    if profile_id not in reg._profiles:  # noqa: SLF001
        raise HTTPException(404, f"provider not found: {profile_id}")
    value = (body.value or "").strip()
    if not value:
        raise HTTPException(400, "value is required (use DELETE to clear)")
    # Persist to disk first.
    try:
        save_key(request.app.state.data_root, profile_id, value)
    except Exception as exc:
        raise HTTPException(500, f"failed to persist key: {exc}")
    # Apply in-memory.
    setter = getattr(provider, "set_api_key", None)
    if callable(setter):
        setter(value)
    # Mutate the registry's profile dict so secret_status reflects "inline".
    if profile_id in reg._profiles:  # noqa: SLF001
        reg._profiles[profile_id]["api_key"] = value  # noqa: SLF001
    await sync_verified_provider_state(reg, request.app.state.data_root, probe=True)
    # Return the updated secret_status only — never echo the key value.
    new_status = next(
        (p["secret_status"] for p in reg.list_profiles() if p["id"] == profile_id),
        None,
    )
    return {"id": profile_id, "secret_status": new_status}


@router.post("/{profile_id}/model")
async def set_profile_model(profile_id: str, body: SetModelBody, request: Request) -> Dict[str, Any]:
    reg = _registry()
    model = (body.model or "").strip()
    if not model:
        raise HTTPException(400, "model is required")
    try:
        updated = reg.update_profile_model(profile_id, model, models=body.models or None)
        save_model_override(
            request.app.state.data_root,
            profile_id,
            model=model,
            models=updated.get("models") or body.models or [model],
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(500, f"failed to set model: {exc}")
    await sync_verified_provider_state(reg, request.app.state.data_root, probe=True)
    return updated


@router.post("/{profile_id}/models/refresh")
async def refresh_profile_models(profile_id: str, request: Request) -> Dict[str, Any]:
    reg = _registry()
    try:
        result = await reg.refresh_profile_models(profile_id)
        save_model_override(
            request.app.state.data_root,
            profile_id,
            model=result.get("model") or "",
            models=result.get("models") or [],
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(424, f"failed to load models: {exc}")
    await sync_verified_provider_state(reg, request.app.state.data_root, probe=True)
    return result


@router.delete("/{profile_id}/model")
async def clear_profile_model(profile_id: str, request: Request) -> Dict[str, Any]:
    reg = _registry()
    provider = reg.provider(profile_id)
    if provider is None:
        raise HTTPException(404, f"provider not found: {profile_id}")
    removed = delete_model_override(request.app.state.data_root, profile_id)
    await sync_verified_provider_state(reg, request.app.state.data_root, probe=True)
    return {"id": profile_id, "removed": removed}


@router.delete("/{profile_id}/key")
async def clear_profile_key(profile_id: str, request: Request) -> Dict[str, Any]:
    """Clear the inline API key for ``profile_id`` (falls back to env/keyring)."""
    reg = _registry()
    provider = reg.provider(profile_id)
    if profile_id not in reg._profiles:  # noqa: SLF001
        raise HTTPException(404, f"provider not found: {profile_id}")
    delete_key(request.app.state.data_root, profile_id)
    setter = getattr(provider, "set_api_key", None)
    if callable(setter):
        setter("")
    if profile_id in reg._profiles:  # noqa: SLF001
        reg._profiles[profile_id]["api_key"] = ""  # noqa: SLF001
    await sync_verified_provider_state(reg, request.app.state.data_root, probe=True)
    new_status = next(
        (p["secret_status"] for p in reg.list_profiles() if p["id"] == profile_id),
        None,
    )
    return {"id": profile_id, "secret_status": new_status}


@router.delete("/roles/{role}/override")
async def reset_role_override(role: str, request: Request) -> Dict[str, Any]:
    """Drop the on-disk override so this role goes back to whatever config.yaml declared.
    NOTE: only the file is removed; the in-memory registry keeps its current value
    until next server restart (since the original config.yaml value would need re-read)."""
    removed = delete_override(request.app.state.data_root, role)
    return {"role": role, "removed": removed}
