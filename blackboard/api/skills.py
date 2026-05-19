"""Skills endpoints."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from blackboard.coding.skill_promotion import SkillPromotionGate
from blackboard.coding.skills import build_skill_index, load_skill_body, set_active_index
from blackboard.kernel.atomic_files import write_text_atomically

router = APIRouter(prefix="/api/skills", tags=["skills"])


class SkillCreateRequest(BaseModel):
    name: str
    content: str


class SkillSuggestRequest(BaseModel):
    query: str
    limit: int = 5


def _skill_dir(request: Request) -> Path:
    data_root = Path(request.app.state.data_root)
    path = data_root / "skills"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _index(request: Request):
    index = build_skill_index(global_dir=_skill_dir(request))
    request.app.state.skill_index = index
    set_active_index(index)
    return index


def _promotion_gate(request: Request, project_id: str) -> SkillPromotionGate:
    project_store = getattr(request.app.state, "project_store", None)
    if project_store is not None and project_store.get(project_id) is None:
        raise HTTPException(404, "project not found")
    return SkillPromotionGate(Path(request.app.state.data_root), project_id)


@router.get("/list")
async def list_skills(request: Request) -> Dict[str, List[Dict[str, Any]]]:
    index = _index(request)
    return {"skills": [entry.__dict__ for entry in index.list()]}


@router.get("/promotions/{project_id}")
async def promotion_candidates(project_id: str, request: Request) -> Dict[str, Any]:
    gate = _promotion_gate(request, project_id)
    return {"project_id": project_id, "stats": gate.stats(), "candidates": gate.get_candidates()}


@router.get("/promotions/{project_id}/stats")
async def promotion_stats(project_id: str, request: Request) -> Dict[str, Any]:
    gate = _promotion_gate(request, project_id)
    return gate.stats()


@router.get("/detail/{skill_name}")
async def skill_detail(skill_name: str, request: Request) -> Dict[str, Any]:
    index = _index(request)
    entry = index.get(skill_name)
    if entry is None:
        raise HTTPException(404, "skill not found")
    body = load_skill_body(index, skill_name)
    return {"skill": {**entry.__dict__, "body": body}}


@router.post("/suggest")
async def suggest_skills(body: SkillSuggestRequest, request: Request) -> Dict[str, List[Dict[str, Any]]]:
    index = _index(request)
    terms = [term.lower() for term in body.query.split() if term.strip()]
    scored = []
    for entry in index.list():
        haystack = f"{entry.name} {entry.description}".lower()
        score = sum(1 for term in terms if term in haystack)
        if score:
            scored.append((score, entry))
    scored.sort(key=lambda item: (-item[0], item[1].name))
    return {"suggestions": [entry.__dict__ for _, entry in scored[: max(1, body.limit)]]}


@router.post("/create")
async def create_skill(body: SkillCreateRequest, request: Request) -> Dict[str, Any]:
    safe_name = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in body.name.strip()).strip("-").lower()
    if not safe_name:
        raise HTTPException(400, "skill name required")
    path = _skill_dir(request) / safe_name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise HTTPException(409, "skill already exists")
    write_text_atomically(path, body.content, encoding="utf-8")
    _index(request)
    return {"name": safe_name, "created": True}


@router.delete("/{skill_name}")
async def delete_skill(skill_name: str, request: Request) -> Dict[str, Any]:
    index = _index(request)
    entry = index.get(skill_name)
    if entry is None:
        raise HTTPException(404, "skill not found")
    path = Path(entry.path)
    if path.exists():
        path.unlink()
        try:
            if path.parent.exists() and not any(path.parent.iterdir()):
                path.parent.rmdir()
        except Exception:
            pass
    _index(request)
    return {"name": skill_name, "deleted": True}
