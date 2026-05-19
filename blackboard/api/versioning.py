"""Version-control API: history, diff, rollback, manual checkpoint, tags.

Backed by ``blackboard/workspace/version_control.py``. Every successful action
fires an audit-friendly ``vcs.<action>`` event on the bus so the audit panel
and any open UI receive live updates.
"""
from __future__ import annotations

from dataclasses import asdict
import re
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from blackboard.kernel.bus import get_bus
from blackboard.workspace.sync_checkpoint_store import get_sync_checkpoint_store, SyncCheckpointRecord
from blackboard.workspace.version_control import get_existing_repo_version_control, get_version_control

router = APIRouter(prefix="/api/versioning", tags=["versioning"])

_SHA_RE = re.compile(r"\b[0-9a-fA-F]{7,40}\b")


def _vcs():
    vcs = get_version_control()
    if vcs is None or not vcs.ensure_initialized():
        raise HTTPException(503, "version control unavailable (git not installed?)")
    return vcs


def _project_repo_vcs(request: Request, project_id: str = ""):
    store = request.app.state.project_store
    resolved_project_id = str(project_id or "").strip() or str(store.get_active() or "").strip()
    if not resolved_project_id:
        raise HTTPException(404, "no active project")
    project = store.get(resolved_project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    vcs = get_existing_repo_version_control(project.root)
    if vcs is None:
        raise HTTPException(404, "project is not a git repository")
    return resolved_project_id, project, vcs


def _scoped_vcs(request: Request, *, scope: str = "data", project_id: str = ""):
    scope_value = str(scope or "data").strip().lower()
    if scope_value == "data":
        return scope_value, "", None, _vcs()
    if scope_value == "project":
        resolved_project_id, project, vcs = _project_repo_vcs(request, project_id)
        return scope_value, resolved_project_id, project, vcs
    raise HTTPException(400, "scope must be 'data' or 'project'")


def _normalize_sha(value: str) -> str:
    text = str(value or "").strip()
    matches = _SHA_RE.findall(text)
    return matches[-1] if matches else text


def _show_diff(request: Request, sha: str, *, scope: str = "data", project_id: str = "") -> Dict[str, Any]:
    scope_value, resolved_project_id, project, vcs = _scoped_vcs(request, scope=scope, project_id=project_id)
    normalized = _normalize_sha(sha)
    if not normalized:
        raise HTTPException(400, "sha required")
    payload = vcs.show_diff(normalized)
    payload.update({
        "scope": scope_value,
        "project_id": resolved_project_id,
        "repo_root": str(getattr(vcs, "data_root", getattr(project, "root", "")) or ""),
    })
    return payload


def _serialize_checkpoint(record: SyncCheckpointRecord) -> Dict[str, Any]:
    payload = record.to_dict()
    payload.pop("checkpoint_files", None)
    files = [str(item or "") for item in (payload.get("files_touched") or []) if str(item or "")]
    payload["files_touched"] = files
    payload["file_count"] = len(files)
    payload["entry_type"] = "sync_checkpoint"
    payload["kind"] = "coding.sync_checkpoint"
    payload["subject"] = str(payload.get("objective") or "Coding action")[:200]
    return payload


def _checkpoint_store():
    store = get_sync_checkpoint_store()
    if store is None:
        raise HTTPException(503, "sync checkpoint store unavailable")
    return store


@router.get("/status")
async def status(request: Request, scope: str = "data", project_id: str = "") -> Dict[str, Any]:
    scope_value = str(scope or "data").strip().lower()
    if scope_value == "data":
        vcs = get_version_control()
        if vcs is None:
            return {"available": False, "scope": "data", "project_id": "", "repo_root": ""}
        payload = dict(vcs.status())
        payload.update({"scope": "data", "project_id": "", "repo_root": str(getattr(vcs, "data_root", "") or "")})
        return payload
    if scope_value == "project":
        store = request.app.state.project_store
        resolved_project_id = str(project_id or "").strip() or str(store.get_active() or "").strip()
        if not resolved_project_id:
            return {"available": False, "scope": "project", "project_id": "", "repo_root": ""}
        project = store.get(resolved_project_id)
        if project is None:
            raise HTTPException(404, "project not found")
        vcs = get_existing_repo_version_control(project.root)
        if vcs is None:
            return {"available": False, "scope": "project", "project_id": resolved_project_id, "repo_root": str(project.root or "")}
        payload = dict(vcs.status())
        payload.update({"scope": "project", "project_id": resolved_project_id, "repo_root": str(getattr(vcs, "data_root", project.root) or "")})
        return payload
    raise HTTPException(400, "scope must be 'data' or 'project'")


@router.get("/history")
async def history(request: Request, path: Optional[str] = None, limit: int = 100, scope: str = "data", project_id: str = "") -> Dict[str, Any]:
    scope_value, resolved_project_id, project, vcs = _scoped_vcs(request, scope=scope, project_id=project_id)
    commits = vcs.history(path=path, limit=limit)
    return {
        "commits": [asdict(c) for c in commits],
        "head": vcs.head(),
        "scope": scope_value,
        "project_id": resolved_project_id,
        "repo_root": str(getattr(vcs, "data_root", getattr(project, "root", "")) or ""),
    }


@router.get("/diff")
async def diff_query(request: Request, sha: str, scope: str = "data", project_id: str = "") -> Dict[str, Any]:
    return _show_diff(request, sha, scope=scope, project_id=project_id)


@router.get("/diff/{sha:path}")
async def diff(request: Request, sha: str, scope: str = "data", project_id: str = "") -> Dict[str, Any]:
    return _show_diff(request, sha, scope=scope, project_id=project_id)


@router.get("/sync-checkpoints")
async def sync_checkpoints(limit: int = 100, project_id: str = "", card_id: str = "", cwd: str = "") -> Dict[str, Any]:
    store = _checkpoint_store()
    records = store.recent(limit=max(1, min(int(limit or 100), 500)), cwd=str(cwd or ""))
    if project_id:
        project_value = str(project_id or "")
        records = [record for record in records if str(record.project_id or "") == project_value]
    if card_id:
        card_value = str(card_id or "")
        records = [record for record in records if str(record.card_id or "") == card_value]
    return {"checkpoints": [_serialize_checkpoint(record) for record in records]}


class SyncCheckpointRestoreBody(BaseModel):
    files: List[str] = []
    reason: str = ""


@router.post("/sync-checkpoints/{checkpoint_id}/restore")
async def restore_sync_checkpoint(checkpoint_id: str, body: SyncCheckpointRestoreBody) -> Dict[str, Any]:
    store = _checkpoint_store()
    checkpoint = store.get(checkpoint_id)
    if checkpoint is None:
        raise HTTPException(404, "checkpoint not found")
    requested = [str(item or "") for item in (body.files or []) if str(item or "")]
    restored = store.restore_files(checkpoint_id, files=requested, reason=body.reason) if requested else store.restore(checkpoint_id, reason=body.reason)
    if restored is None:
        raise HTTPException(400, "restore failed")
    try:
        await get_bus().emit("sync_checkpoint.restored", {
            "checkpoint_id": checkpoint_id,
            "files": requested or list(restored.files_touched or []),
            "status": restored.status,
            "card_id": restored.card_id,
            "project_id": restored.project_id,
        })
    except Exception:
        pass
    return _serialize_checkpoint(restored)


class RollbackBody(BaseModel):
    sha: str
    mode: str = "revert"  # "revert" (safe) or "hard" (destructive)
    scope: str = "data"
    project_id: str = ""


@router.post("/rollback")
async def rollback(body: RollbackBody, request: Request) -> Dict[str, Any]:
    scope_value, resolved_project_id, project, vcs = _scoped_vcs(request, scope=body.scope, project_id=body.project_id)
    if body.mode not in ("revert", "hard"):
        raise HTTPException(400, "mode must be 'revert' or 'hard'")
    result = vcs.rollback(body.sha, mode=body.mode)
    if not result.get("ok"):
        raise HTTPException(400, result.get("error") or "rollback failed")
    try:
        await get_bus().emit("vcs.rollback", {
            "mode": body.mode,
            "target": body.sha,
            "new_sha": result.get("new_sha", ""),
            "scope": scope_value,
            "project_id": resolved_project_id,
        })
    except Exception:
        pass
    result.update({
        "scope": scope_value,
        "project_id": resolved_project_id,
        "repo_root": str(getattr(vcs, "data_root", getattr(project, "root", "")) or ""),
    })
    return result


class CheckpointBody(BaseModel):
    message: str
    paths: List[str] = []
    scope: str = "data"
    project_id: str = ""


@router.post("/checkpoint")
async def checkpoint(body: CheckpointBody, request: Request) -> Dict[str, Any]:
    """Manual user-initiated commit. Useful before risky changes."""
    scope_value, resolved_project_id, project, vcs = _scoped_vcs(request, scope=body.scope, project_id=body.project_id)
    msg = (body.message or "").strip() or "Manual checkpoint"
    sha = vcs.commit(msg, kind="vcs.checkpoint", paths=body.paths or None)
    if not sha:
        raise HTTPException(400, "nothing to commit")
    try:
        await get_bus().emit("vcs.checkpoint", {"sha": sha, "message": msg, "scope": scope_value, "project_id": resolved_project_id})
    except Exception:
        pass
    return {
        "sha": sha,
        "short_sha": sha[:8],
        "message": msg,
        "scope": scope_value,
        "project_id": resolved_project_id,
        "repo_root": str(getattr(vcs, "data_root", getattr(project, "root", "")) or ""),
    }


class TagBody(BaseModel):
    name: str
    sha: Optional[str] = None
    message: str = ""
    scope: str = "data"
    project_id: str = ""


@router.post("/tag")
async def tag(body: TagBody, request: Request) -> Dict[str, Any]:
    scope_value, resolved_project_id, project, vcs = _scoped_vcs(request, scope=body.scope, project_id=body.project_id)
    result = vcs.tag(body.name, sha=body.sha, message=body.message)
    if not result.get("ok"):
        raise HTTPException(400, result.get("error") or "tag failed")
    result.update({
        "scope": scope_value,
        "project_id": resolved_project_id,
        "repo_root": str(getattr(vcs, "data_root", getattr(project, "root", "")) or ""),
    })
    return result


@router.get("/tags")
async def list_tags(request: Request, scope: str = "data", project_id: str = "") -> List[Dict[str, Any]]:
    _, _, _, vcs = _scoped_vcs(request, scope=scope, project_id=project_id)
    return vcs.list_tags()
