"""Audit log endpoints."""
from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Request

from blackboard.workspace.audit import AuditLog

router = APIRouter(prefix="/api/audit", tags=["audit"])


def _ensure_audit(request: Request, project_id: str, *, require_project: bool = False) -> AuditLog | None:
    audit_logs = request.app.state.audit_logs
    if project_id in audit_logs:
        return audit_logs[project_id]
    project = request.app.state.project_store.get(project_id)
    if project is None:
        if require_project:
            raise HTTPException(404, "project not found")
        return None
    audit = AuditLog(request.app.state.data_root, project_id)
    audit_logs[project_id] = audit
    return audit


@router.get("/{project_id:path}")
async def tail(project_id: str, request: Request, limit: int = 100) -> List[Dict[str, Any]]:
    """Return audit tail. Gracefully returns [] for unknown projects so the UI can show an empty panel."""
    audit = _ensure_audit(request, project_id, require_project=False)
    if audit is None:
        return []
    return audit.tail(limit=max(1, min(int(limit or 100), 1000)))


@router.post("/{project_id:path}/record")
async def record(project_id: str, request: Request, body: Dict[str, Any]) -> Dict[str, Any]:
    """Allow the UI to drop manual audit entries (testing / annotations)."""
    audit = _ensure_audit(request, project_id, require_project=True)
    assert audit is not None  # require_project=True raises otherwise
    entry = audit.record(
        str(body.get("kind") or body.get("event_type") or "manual"),
        {k: v for k, v in body.items() if k not in {"kind", "event_type", "actor", "session_id", "outcome", "duration_ms"}},
        actor=str(body.get("actor") or "ui"),
        session_id=str(body.get("session_id") or ""),
        outcome=str(body.get("outcome") or "ok"),
        duration_ms=body.get("duration_ms"),
    )
    return {"ok": True, "id": entry["id"]}
