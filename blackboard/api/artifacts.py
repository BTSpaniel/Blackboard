"""Render-on-demand HTML artifact export (§11b.3).

POST /api/artifacts/{project_id}/render
{ "source_markdown": "...", "preset": "audit-deliverable|plan-with-subtasks|...", "name": "card_42" }

→ saves <name>.rendered.html alongside the source markdown, returns URL.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from blackboard.kernel.atomic_files import write_text_atomically
from blackboard.kernel.prompts import get_prompts
from blackboard.providers.base import Message
from blackboard.providers.registry import get_provider_registry
from blackboard.workspace.artifact_library import (
    create_artifact_project_folder,
    delete_artifact,
    list_artifact_project_files,
    list_artifacts,
    load_artifact,
    read_artifact_project_file,
    remember_artifact,
    resolve_reusable_artifact,
    resolve_artifact_preview_file,
    update_artifact,
    write_artifact_project_file,
)

router = APIRouter(prefix="/api/artifacts", tags=["artifacts"])

_PRESETS = {
    "audit-deliverable": "presenter.suffix.audit-deliverable",
    "plan-with-subtasks": "presenter.suffix.plan-with-subtasks",
    "comparison-grid": "presenter.suffix.comparison-grid",
    "pricing-calculator": "presenter.suffix.pricing-calculator",
    "live-dashboard": "presenter.suffix.live-dashboard",
}


class RenderRequest(BaseModel):
    name: str
    source_markdown: str
    preset: str = "plan-with-subtasks"


class ArtifactCreateRequest(BaseModel):
    title: str = "Artifact"
    type: str = "html"
    source: str = ""


class ArtifactUpdateRequest(BaseModel):
    title: str | None = None
    source: str | None = None


class ArtifactResolveRequest(BaseModel):
    title: str = ""
    type: str = "html"
    source: str = ""


class ArtifactFileWriteRequest(BaseModel):
    path: str
    content: str = ""


class ArtifactFolderCreateRequest(BaseModel):
    path: str


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_\-]+", "_", name)[:80] or "artifact"


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        parts = text.split("```")
        if len(parts) >= 2:
            text = parts[1]
            if text.lstrip().lower().startswith("html"):
                text = text.split("\n", 1)[1] if "\n" in text else ""
    return text.strip()


def _data_root(request: Request) -> Path:
    return Path(request.app.state.data_root).resolve()


def _require_project(request: Request, project_id: str) -> None:
    project = request.app.state.project_store.get(project_id)
    if project is None:
        raise HTTPException(404, "project not found")


@router.post("/{project_id}/render")
async def render_html(project_id: str, body: RenderRequest, request: Request) -> Dict[str, Any]:
    _require_project(request, project_id)
    preset_key = _PRESETS.get(body.preset, "presenter.suffix.plan-with-subtasks")
    prompts = get_prompts()
    system = prompts.get("presenter.system")
    suffix = prompts.get(preset_key)
    user = f"{body.source_markdown.strip()}\n\n---\n{suffix}"

    registry = get_provider_registry()

    async def _call(provider):
        return await provider.complete(
            [Message(role="system", content=system), Message(role="user", content=user)],
            temperature=0.2,
            max_tokens=8000,
        )

    response = await registry.call_with_fallback("presenter", _call)
    html = _strip_code_fences(response.content)
    if "<html" not in html.lower():
        html = f"<!doctype html><html><head><meta charset='utf-8'><title>{body.name}</title></head><body>{html}</body></html>"
    artifacts_dir = Path(request.app.state.data_root) / "projects" / project_id / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    name = _safe_name(body.name)
    out_path = artifacts_dir / f"{name}.rendered.html"
    write_text_atomically(out_path, html)
    audit = request.app.state.audit_logs.get(project_id)
    if audit is not None:
        audit.record("artifact.rendered", {"name": name, "preset": body.preset, "path": str(out_path)})
    return {
        "name": name,
        "preset": body.preset,
        "url": f"/api/artifacts/{project_id}/{name}.html",
        "path": str(out_path),
        "bytes": len(html),
    }


@router.post("/{project_id}/library")
async def create_artifact(project_id: str, body: ArtifactCreateRequest, request: Request) -> Dict[str, Any]:
    _require_project(request, project_id)
    artifact = remember_artifact(
        _data_root(request),
        project_id=project_id,
        title=body.title,
        kind=body.type,
        source=body.source,
    )
    return artifact


@router.post("/{project_id}/library/resolve")
async def resolve_artifact(project_id: str, body: ArtifactResolveRequest, request: Request) -> Dict[str, Any]:
    _require_project(request, project_id)
    resolved = resolve_reusable_artifact(
        _data_root(request),
        project_id=project_id,
        title=body.title,
        kind=body.type,
        source=body.source,
    )
    artifact = resolved.get("artifact") if isinstance(resolved, dict) else None
    return {
        "match": str((resolved or {}).get("match") or ""),
        "artifact": artifact or None,
    }


@router.get("/{project_id}/library")
async def list_project_artifacts(project_id: str, request: Request) -> Dict[str, Any]:
    _require_project(request, project_id)
    return {"artifacts": list_artifacts(_data_root(request), project_id=project_id)}


@router.get("/{project_id}/library/{artifact_id}")
async def get_artifact(project_id: str, artifact_id: str, request: Request) -> Dict[str, Any]:
    _require_project(request, project_id)
    artifact = load_artifact(_data_root(request), artifact_id)
    if not artifact or artifact.get("project_id") != project_id:
        raise HTTPException(404, "artifact not found")
    return artifact


@router.put("/{project_id}/library/{artifact_id}")
async def put_artifact(project_id: str, artifact_id: str, body: ArtifactUpdateRequest, request: Request) -> Dict[str, Any]:
    _require_project(request, project_id)
    artifact = update_artifact(_data_root(request), artifact_id, title=body.title, source=body.source)
    if not artifact or artifact.get("project_id") != project_id:
        raise HTTPException(404, "artifact not found")
    return artifact


@router.get("/{project_id}/library/{artifact_id}/files")
async def list_artifact_files(project_id: str, artifact_id: str, request: Request) -> Dict[str, Any]:
    _require_project(request, project_id)
    artifact = load_artifact(_data_root(request), artifact_id)
    if not artifact or artifact.get("project_id") != project_id:
        raise HTTPException(404, "artifact not found")
    return {
        "artifact_id": artifact_id,
        "entry_file": artifact.get("entry_file", ""),
        "project_root": artifact.get("project_root", ""),
        "files": list_artifact_project_files(_data_root(request), artifact_id),
    }


@router.get("/{project_id}/library/{artifact_id}/files/content")
async def get_artifact_file_content(project_id: str, artifact_id: str, request: Request, path: str = "") -> Dict[str, Any]:
    _require_project(request, project_id)
    artifact = load_artifact(_data_root(request), artifact_id)
    if not artifact or artifact.get("project_id") != project_id:
        raise HTTPException(404, "artifact not found")
    payload = read_artifact_project_file(_data_root(request), artifact_id, path)
    if not payload:
        raise HTTPException(404, "artifact file not found")
    return payload


@router.put("/{project_id}/library/{artifact_id}/files/content")
async def put_artifact_file_content(project_id: str, artifact_id: str, body: ArtifactFileWriteRequest, request: Request) -> Dict[str, Any]:
    _require_project(request, project_id)
    artifact = load_artifact(_data_root(request), artifact_id)
    if not artifact or artifact.get("project_id") != project_id:
        raise HTTPException(404, "artifact not found")
    updated = write_artifact_project_file(_data_root(request), artifact_id, body.path, body.content)
    if not updated or updated.get("project_id") != project_id:
        raise HTTPException(404, "artifact not found")
    return updated


@router.post("/{project_id}/library/{artifact_id}/folders")
async def post_artifact_folder(project_id: str, artifact_id: str, body: ArtifactFolderCreateRequest, request: Request) -> Dict[str, Any]:
    _require_project(request, project_id)
    artifact = load_artifact(_data_root(request), artifact_id)
    if not artifact or artifact.get("project_id") != project_id:
        raise HTTPException(404, "artifact not found")
    updated = create_artifact_project_folder(_data_root(request), artifact_id, body.path)
    if not updated or updated.get("project_id") != project_id:
        raise HTTPException(404, "artifact not found")
    return updated


@router.delete("/{project_id}/library/{artifact_id}")
async def remove_artifact(project_id: str, artifact_id: str, request: Request) -> Dict[str, Any]:
    _require_project(request, project_id)
    artifact = load_artifact(_data_root(request), artifact_id)
    if not artifact or artifact.get("project_id") != project_id:
        raise HTTPException(404, "artifact not found")
    deleted = delete_artifact(_data_root(request), artifact_id)
    if not deleted:
        raise HTTPException(404, "artifact not found")
    return {"artifact_id": artifact_id, "deleted": True}


@router.get("/{project_id}/{name}.html", response_class=HTMLResponse)
async def serve_html(project_id: str, name: str, request: Request) -> HTMLResponse:
    _require_project(request, project_id)
    path = Path(request.app.state.data_root) / "projects" / project_id / "artifacts" / f"{_safe_name(name)}.rendered.html"
    if not path.exists():
        raise HTTPException(404, "artifact not found")
    return HTMLResponse(path.read_text(encoding="utf-8"))


@router.get("/{project_id}/library/{artifact_id}/preview")
async def serve_artifact_preview_root(project_id: str, artifact_id: str, request: Request) -> FileResponse:
    _require_project(request, project_id)
    artifact = load_artifact(_data_root(request), artifact_id)
    if not artifact or artifact.get("project_id") != project_id:
        raise HTTPException(404, "artifact not found")
    path = resolve_artifact_preview_file(_data_root(request), artifact_id)
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "artifact preview file not found")
    return FileResponse(path)


@router.get("/{project_id}/library/{artifact_id}/preview/{relative_path:path}")
async def serve_artifact_preview_file(project_id: str, artifact_id: str, relative_path: str, request: Request) -> FileResponse:
    _require_project(request, project_id)
    artifact = load_artifact(_data_root(request), artifact_id)
    if not artifact or artifact.get("project_id") != project_id:
        raise HTTPException(404, "artifact not found")
    path = resolve_artifact_preview_file(_data_root(request), artifact_id, relative_path)
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "artifact preview file not found")
    return FileResponse(path)
