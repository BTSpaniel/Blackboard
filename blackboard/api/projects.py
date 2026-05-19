"""Project CRUD endpoints."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from blackboard.api.files import ensure_workspace_dir
from blackboard.coding.agents_md import inspect_agents_md
from blackboard.kernel.atomic_files import write_text_atomically
from blackboard.workspace.project import _slugify

router = APIRouter(prefix="/api/projects", tags=["projects"])


class ProjectCreate(BaseModel):
    name: str
    root: str = ""
    active_branch: str = "main"
    providers: Dict[str, str] = {}


def _project_root(request: Request, name: str, root: str) -> str:
    value = str(root or "").strip()
    if value and value not in (".", "./"):
        return value
    workspace = ensure_workspace_dir(request.app.state.data_root)
    return str((workspace / _slugify(name)).resolve())


def _agents_index_path(request: Request, project_id: str) -> Path:
    return Path(request.app.state.data_root) / "projects" / project_id / "agents_index.json"


def _persist_agents_index(request: Request, project_id: str, payload: Dict[str, Any]) -> str:
    path = _agents_index_path(request, project_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    stored = dict(payload or {})
    stored["stored_at"] = time.time()
    write_text_atomically(path, json.dumps(stored, indent=2))
    return str(path)


@router.get("")
async def list_projects(request: Request) -> List[Dict[str, Any]]:
    store = request.app.state.project_store
    return [p.to_dict() for p in store.list()]


@router.get("/active")
async def active(request: Request) -> Dict[str, Any]:
    store = request.app.state.project_store
    active_id = store.get_active()
    if not active_id:
        return {"project_id": None}
    project = store.get(active_id)
    return {"project_id": active_id, "project": project.to_dict() if project else None}


@router.post("")
async def create_project(body: ProjectCreate, request: Request) -> Dict[str, Any]:
    store = request.app.state.project_store
    root = _project_root(request, body.name, body.root)
    project = store.create(
        name=body.name,
        root=root,
        active_branch=body.active_branch,
        providers=body.providers,
    )
    audit = getattr(request.app.state, "audit_logs", {}).get(project.project_id)
    if audit is not None:
        audit.record("project.created", {"project_id": project.project_id, "name": project.name})
    return project.to_dict()


@router.get("/{project_id}")
async def get_project(project_id: str, request: Request) -> Dict[str, Any]:
    store = request.app.state.project_store
    project = store.get(project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    return project.to_dict()


@router.get("/{project_id}/agents")
async def get_project_agents(
    project_id: str,
    request: Request,
    include_content: bool = Query(default=False),
) -> Dict[str, Any]:
    store = request.app.state.project_store
    project = store.get(project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    inspection = inspect_agents_md(cwd=project.root, include_content=include_content)
    manifest = {
        "project_id": project_id,
        "project_root": str(project.root),
        "cwd": inspection.get("cwd") or str(project.root),
        "explicit_path": inspection.get("explicit_path") or "",
        "found": bool(inspection.get("found")),
        "chain": [
            {
                "path": item.get("path") or "",
                "scope": item.get("scope") or "",
                "exists": bool(item.get("exists")),
                "size": int(item.get("size") or 0),
                "mtime": float(item.get("mtime") or 0.0),
            }
            for item in list(inspection.get("chain") or [])
        ],
        "generated_at": float(inspection.get("generated_at") or time.time()),
    }
    index_path = _persist_agents_index(request, project_id, manifest)
    response = dict(inspection)
    response.update({
        "project_id": project_id,
        "project_root": str(project.root),
        "index_path": index_path,
    })
    return response


@router.post("/{project_id}/switch")
async def switch_project(project_id: str, request: Request) -> Dict[str, Any]:
    store = request.app.state.project_store
    project = store.get(project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    store.set_active(project_id)
    return {"project_id": project_id}


@router.delete("/{project_id}")
async def delete_project(project_id: str, request: Request) -> Dict[str, Any]:
    store = request.app.state.project_store
    ok = store.delete(project_id)
    if not ok:
        raise HTTPException(404, "project not found")
    return {"project_id": project_id, "deleted": True}
