"""Coding endpoints — sync execute + background jobs + merge."""
from __future__ import annotations

import time
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from blackboard.api.files import ensure_workspace_dir
from blackboard.coding import project_intelligence
from blackboard.coding.agents_md import load_agents_md
from blackboard.coding.jobs import BackgroundJobManager
from blackboard.coding.models import CodingTask, JobStatus
from blackboard.coding.worker import CodingWorker
from blackboard.kernel.json_schema import build_response_format, parse_json_payload, validate_payload
from blackboard.kernel.logger import get_logger
from blackboard.providers.base import Message
from blackboard.workspace.board import BoardService

router = APIRouter(prefix="/api/coding", tags=["coding"])

_REPO_ROOT = Path(__file__).resolve().parents[2]
logger = get_logger("api.coding")
_MAX_ORCHESTRATED_CHILDREN = 6
_MAX_ORCHESTRATION_LINE_ITEMS = 8
_MAX_ORCHESTRATION_TEXT_CHARS = 240
_JOB_ORCHESTRATION_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "should_decompose": {"type": "boolean"},
        "reason": {"type": "string", "maxLength": 400},
        "design_summary": {"type": "string", "maxLength": 2000},
        "planning_summary": {"type": "string", "maxLength": 2000},
        "tasks": {
            "type": "array",
            "maxItems": _MAX_ORCHESTRATED_CHILDREN,
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "minLength": 3, "maxLength": 140},
                    "objective": {"type": "string", "minLength": 3, "maxLength": 1200},
                    "files": {
                        "type": "array",
                        "maxItems": _MAX_ORCHESTRATION_LINE_ITEMS,
                        "items": {"type": "string", "maxLength": _MAX_ORCHESTRATION_TEXT_CHARS},
                    },
                    "constraints": {
                        "type": "array",
                        "maxItems": _MAX_ORCHESTRATION_LINE_ITEMS,
                        "items": {"type": "string", "maxLength": _MAX_ORCHESTRATION_TEXT_CHARS},
                    },
                    "verification": {
                        "type": "array",
                        "maxItems": _MAX_ORCHESTRATION_LINE_ITEMS,
                        "items": {"type": "string", "maxLength": _MAX_ORCHESTRATION_TEXT_CHARS},
                    },
                },
                "required": ["title", "objective"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["should_decompose", "tasks"],
    "additionalProperties": False,
}
_JOB_ORCHESTRATION_TEMPLATE = (
    "You are Blackboard's design and planning orchestrator. "
    "Decide whether the request should be split into smaller execution jobs before coding begins. "
    "If the work is already small and digestible, set should_decompose=false and return an empty tasks array. "
    "If decomposition is needed, return a concise design_summary, a concise planning_summary, and 2-6 executable child tasks. "
    "Each child task must be independently completable by the coding worker. "
    "Do not include design-only or planning-only child tasks; summarize those passes in the summaries and emit only execution-ready child tasks.\n\n"
    "Parent card title:\n{title}\n\n"
    "Parent card objective:\n{objective}\n\n"
    "Parent card body:\n{body}\n\n"
    "Files:\n{files}\n\n"
    "Constraints:\n{constraints}\n\n"
    "Verification:\n{verification}\n\n"
    "Context:\n{context}\n\n"
    "Project intelligence:\n{project_intelligence}\n\n"
    "AGENTS.md guidance:\n{agents_md}\n\n"
    "Return child tasks with explicit file ownership whenever possible. If a child must change multiple files, list all affected files instead of collapsing the scope to one file."
)


class ExecuteRequest(BaseModel):
    objective: str
    cwd: str = "."
    files: List[str] = []
    constraints: List[str] = []
    verification: List[str] = []
    context: str = ""
    agents_md_path: str = ""
    project_id: str = ""
    card_id: str = ""


class JobSubmitRequest(ExecuteRequest):
    base_branch: str = "main"
    max_retries: int = 2


class MergeRequest(BaseModel):
    confirm: bool = False
    message: str = ""


def _board(request: Request, project_id: str) -> BoardService:
    project = str(project_id or "").strip()
    if not project:
        raise HTTPException(422, "project_id is required")
    boards = getattr(request.app.state, "boards", None)
    if boards is None:
        boards = {}
        request.app.state.boards = boards
    board = boards.get(project)
    if board is not None:
        return board
    project_store = getattr(request.app.state, "project_store", None)
    if project_store is not None and project_store.get(project) is None:
        raise HTTPException(404, "project not found")
    board = BoardService(
        request.app.state.data_root,
        project,
        bus=getattr(request.app.state, "bus", None),
        on_done=getattr(request.app.state, "on_card_done", None),
    )
    boards[project] = board
    return board


def _normalized_lines(values: Any, *, max_items: int = _MAX_ORCHESTRATION_LINE_ITEMS, max_chars: int = _MAX_ORCHESTRATION_TEXT_CHARS) -> List[str]:
    out: List[str] = []
    for raw in list(values or []):
        item = str(raw or "").strip()
        if not item:
            continue
        out.append(item[:max_chars])
        if len(out) >= max_items:
            break
    return out


def _orchestration_metadata(card: Any) -> Dict[str, Any]:
    metadata = dict(getattr(card, "metadata", {}) or {}) if card is not None else {}
    orchestration = metadata.get("orchestration")
    return dict(orchestration or {}) if isinstance(orchestration, dict) else {}


def _trim_context_block(text: str, *, max_chars: int = 2400) -> str:
    value = str(text or "").strip()
    if not value:
        return "(none)"
    return value[:max_chars]


def _planner_repo_context(request: Request, task: CodingTask) -> Dict[str, str]:
    cwd_abs = str(Path(task.cwd or ".").resolve())
    project_intel_dir = Path(getattr(request.app.state, "data_root", Path("."))) / "project_intelligence"
    project_summary = project_intelligence.ensure_project_intelligence(
        project_intel_dir,
        cwd=cwd_abs,
        task_files=task.files,
        objective=task.objective,
    )
    project_block = project_intelligence.build_project_context_block(project_summary)
    agents_md_text = load_agents_md(cwd=cwd_abs, explicit_path=task.agents_md_path)
    return {
        "project_intelligence": _trim_context_block(project_block),
        "agents_md": _trim_context_block(agents_md_text),
    }


def _normalized_child_specs(child_specs: List[Dict[str, Any]], task: CodingTask) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    seen: set[tuple[str, str, tuple[str, ...]]] = set()
    for raw in list(child_specs or [])[:_MAX_ORCHESTRATED_CHILDREN]:
        title = str((raw or {}).get("title") or "").strip()[:140]
        objective = str((raw or {}).get("objective") or "").strip()[:1200]
        if not title or not objective:
            continue
        files = _normalized_lines((raw or {}).get("files") or task.files)
        constraints = _normalized_lines((raw or {}).get("constraints") or task.constraints)
        verification = _normalized_lines((raw or {}).get("verification") or task.verification)
        signature = (
            title.lower(),
            objective.lower(),
            tuple(item.lower() for item in files),
        )
        if signature in seen:
            continue
        seen.add(signature)
        normalized.append({
            "title": title,
            "objective": objective,
            "files": files,
            "constraints": constraints,
            "verification": verification,
        })
    return normalized


def _orchestrated_response(*, record_payload: Dict[str, Any], parent_id: str, orchestration: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(record_payload or {})
    payload.update({
        "orchestrated": True,
        "parent_card_id": parent_id,
        "child_card_ids": [str(item or "") for item in list(orchestration.get("child_card_ids") or []) if str(item or "").strip()],
        "child_job_ids": [str(item or "") for item in list(orchestration.get("child_job_ids") or []) if str(item or "").strip()],
        "design_summary": str(orchestration.get("design_summary") or ""),
        "planning_summary": str(orchestration.get("planning_summary") or ""),
        "orchestration_error": str(orchestration.get("error") or ""),
    })
    return payload


def _task_from_child_card(card: Any, *, base_task: CodingTask, parent_id: str, root_id: str) -> CodingTask:
    metadata = dict(getattr(card, "metadata", {}) or {})
    return CodingTask(
        objective=str(metadata.get("execution_objective") or getattr(card, "body", "") or getattr(card, "title", "") or ""),
        files=list(getattr(card, "files", []) or []),
        constraints=list(getattr(card, "constraints", []) or []),
        verification=list(getattr(card, "verification", []) or []),
        cwd=base_task.cwd,
        base_branch=base_task.base_branch,
        context=base_task.context,
        agents_md_path=base_task.agents_md_path,
        project_id=base_task.project_id,
        card_id=str(getattr(card, "id", "") or ""),
        parent_card_id=parent_id,
        root_card_id=root_id,
        orchestration_stage="execution",
    )


async def _reuse_existing_orchestration(body: JobSubmitRequest, request: Request, task: CodingTask, board: BoardService, card: Any) -> Optional[Dict[str, Any]]:
    orchestration = _orchestration_metadata(card)
    child_card_ids = [str(item or "") for item in list(orchestration.get("child_card_ids") or []) if str(item or "").strip()]
    if not child_card_ids:
        return None
    jobs = _jobs(request)
    parent_id = str(getattr(card, "id", "") or task.card_id or "").strip()
    root_id = str(orchestration.get("root_card_id") or parent_id)
    child_job_ids: List[str] = [str(item or "") for item in list(orchestration.get("child_job_ids") or []) if str(item or "").strip()]
    records_by_job_id: Dict[str, Dict[str, Any]] = {}
    for job_id in child_job_ids:
        record = await jobs.get(job_id)
        if record is not None:
            records_by_job_id[job_id] = record.to_dict()
    for child_id in child_card_ids:
        active = await jobs.active_for_card(task.project_id, child_id)
        if active is not None:
            records_by_job_id[active.job_id] = active.to_dict()
            continue
        child_card = board.get(child_id)
        if child_card is None:
            continue
        child_status = str(getattr(child_card, "status", "") or "").strip().lower()
        if child_status in {"done", "reviewing"}:
            continue
        resubmitted = await jobs.submit(_task_from_child_card(child_card, base_task=task, parent_id=parent_id, root_id=root_id), max_retries=body.max_retries)
        records_by_job_id[resubmitted.job_id] = resubmitted.to_dict()
    if not records_by_job_id:
        return None
    ordered_records = sorted(
        records_by_job_id.values(),
        key=lambda item: ({"running": 4, "merging": 3, "pending": 2, "paused": 1}.get(str(item.get("status") or ""), 0), float(item.get("created_at") or 0.0)),
        reverse=True,
    )
    selected = ordered_records[0]
    next_orchestration = dict(orchestration)
    next_orchestration["child_job_ids"] = [str(item.get("job_id") or "") for item in ordered_records if str(item.get("job_id") or "").strip()]
    next_orchestration["updated_at"] = time.time()
    parent_metadata = dict(getattr(card, "metadata", {}) or {})
    parent_metadata["orchestration"] = next_orchestration
    await board.update_card(parent_id, metadata=parent_metadata)
    return _orchestrated_response(record_payload=selected, parent_id=parent_id, orchestration=next_orchestration)


def _should_try_orchestration(body: JobSubmitRequest, card: Optional[Any]) -> bool:
    if not str(body.project_id or "").strip() or not str(body.card_id or "").strip():
        return False
    orchestration = _orchestration_metadata(card)
    if orchestration.get("parent_card_id"):
        return False
    if list(orchestration.get("child_card_ids") or []):
        return False
    objective = "\n".join([
        str(body.objective or ""),
        str(getattr(card, "title", "") or ""),
        str(getattr(card, "body", "") or ""),
    ]).strip()
    signals = 0
    if len(objective) >= 220:
        signals += 1
    if objective.count("\n") >= 2:
        signals += 1
    if len(list(body.files or []) or list(getattr(card, "files", []) or [])) >= 4:
        signals += 1
    if len(list(body.constraints or [])) + len(list(body.verification or [])) >= 5:
        signals += 1
    if len(re.findall(r"\b(and|then|also|plus|after|before|while|across|multiple)\b", objective.lower())) >= 3:
        signals += 1
    return signals >= 2


async def _plan_job_orchestration(request: Request, task: CodingTask, card: Any) -> Dict[str, Any]:
    registry = getattr(request.app.state, "provider_registry", None)
    if registry is None:
        return {}
    repo_context = _planner_repo_context(request, task)
    prompt = _JOB_ORCHESTRATION_TEMPLATE.format(
        title=str(getattr(card, "title", "") or task.objective or "(untitled card)"),
        objective=str(task.objective or ""),
        body=str(getattr(card, "body", "") or ""),
        files="\n".join(f"- {item}" for item in _normalized_lines(getattr(card, "files", None) or task.files)) or "- none specified",
        constraints="\n".join(f"- {item}" for item in _normalized_lines(getattr(card, "constraints", None) or task.constraints)) or "- none specified",
        verification="\n".join(f"- {item}" for item in _normalized_lines(getattr(card, "verification", None) or task.verification)) or "- none specified",
        context=str(task.context or "")[:2000] or "(no extra context)",
        project_intelligence=repo_context["project_intelligence"],
        agents_md=repo_context["agents_md"],
    )

    async def _call(provider):
        kwargs: Dict[str, Any] = {}
        if getattr(getattr(provider, "capabilities", None), "structured_output", False):
            kwargs["response_format"] = build_response_format(_JOB_ORCHESTRATION_SCHEMA, "job_orchestration")
        return await provider.complete([Message(role="user", content=prompt)], temperature=0.1, **kwargs)

    try:
        response = await registry.call_with_fallback("planner", _call)
    except Exception as exc:
        logger.debug("[coding] orchestration planner failed: %s", exc)
        return {}
    parsed, parse_error = parse_json_payload(str(getattr(response, "content", "") or "").strip())
    if parse_error:
        return {}
    parsed, validation_error = validate_payload(parsed, _JOB_ORCHESTRATION_SCHEMA, path="job_orchestration")
    if validation_error:
        logger.debug("[coding] orchestration payload invalid: %s", validation_error)
        return {}
    return dict(parsed or {})


async def _submit_orchestrated_jobs(body: JobSubmitRequest, request: Request, task: CodingTask, board: BoardService, card: Any) -> Optional[Dict[str, Any]]:
    if not _should_try_orchestration(body, card):
        return None
    plan = await _plan_job_orchestration(request, task, card)
    child_specs = _normalized_child_specs(list(plan.get("tasks") or []), task)
    if not plan.get("should_decompose") or not child_specs:
        return None
    jobs = _jobs(request)
    parent_id = str(getattr(card, "id", "") or task.card_id or "").strip()
    if not parent_id:
        return None
    parent_metadata = dict(getattr(card, "metadata", {}) or {})
    root_id = str(_orchestration_metadata(card).get("root_card_id") or parent_id)
    design_summary = str(plan.get("design_summary") or "").strip()
    planning_summary = str(plan.get("planning_summary") or "").strip()
    child_cards: List[Dict[str, Any]] = []
    child_jobs: List[Dict[str, Any]] = []
    context_parts = [str(task.context or "").strip(), f"Parent objective:\n{str(task.objective or '').strip()}".strip()]
    if design_summary:
        context_parts.append(f"Design summary:\n{design_summary}")
    if planning_summary:
        context_parts.append(f"Planning summary:\n{planning_summary}")
    child_context = "\n\n".join(part for part in context_parts if part).strip()
    try:
        previous_child_id = ""
        for index, raw in enumerate(child_specs[:_MAX_ORCHESTRATED_CHILDREN], start=1):
            title = str(raw.get("title") or "").strip()[:140]
            objective = str(raw.get("objective") or "").strip()[:1200]
            files = list(raw.get("files") or [])
            constraints = list(raw.get("constraints") or [])
            verification = list(raw.get("verification") or [])
            child_metadata = {
                "execution_objective": objective,
                "orchestration": {
                    "role": "child",
                    "parent_card_id": parent_id,
                    "root_card_id": root_id,
                    "sequence": index,
                    "depends_on_card_id": previous_child_id,
                    "auto_generated": True,
                    "generated_at": time.time(),
                },
            }
            child = await board.create_card(
                title=title,
                body=objective,
                status="ready",
                files=files,
                constraints=constraints,
                verification=verification,
                deps=[previous_child_id] if previous_child_id else [],
                provider_role="coder",
                tags=["orchestrated", "orchestration-child"],
                metadata=child_metadata,
            )
            child_dict = child.to_dict()
            child_cards.append(child_dict)
            record = await jobs.submit(CodingTask(
                objective=objective,
                files=files,
                constraints=constraints,
                verification=verification,
                cwd=task.cwd,
                base_branch=task.base_branch,
                context=child_context,
                agents_md_path=task.agents_md_path,
                project_id=task.project_id,
                card_id=child.id,
                parent_card_id=parent_id,
                root_card_id=root_id,
                orchestration_stage="execution",
            ), max_retries=body.max_retries)
            child_jobs.append(record.to_dict())
            previous_child_id = str(child.id or "")
    except Exception as exc:
        submitted_child_ids = {
            str(((item or {}).get("task") or {}).get("card_id") or "")
            for item in child_jobs
            if str((((item or {}).get("task") or {}).get("card_id") or "")).strip()
        }
        for child_payload in child_cards:
            child_id = str(child_payload.get("id") or "").strip()
            if not child_id or child_id in submitted_child_ids:
                continue
            child_card = board.get(child_id)
            if child_card is None:
                continue
            child_metadata = dict(getattr(child_card, "metadata", {}) or {})
            child_orchestration = dict(child_metadata.get("orchestration") or {})
            child_orchestration["error"] = f"orchestration_partial_failure: {exc}"
            child_orchestration["updated_at"] = time.time()
            child_metadata["orchestration"] = child_orchestration
            child_metadata["status_note"] = f"Waiting for orchestration recovery after submit failure: {exc}"
            await board.update_card(child_id, status="blocked", metadata=child_metadata, body=child_metadata["status_note"], progress=max(int(getattr(child_card, "progress", 0) or 0), 5))
        partial_orchestration = dict(parent_metadata.get("orchestration") or {})
        partial_orchestration.update({
            "role": "parent",
            "root_card_id": root_id,
            "reason": str(plan.get("reason") or "").strip()[:400],
            "design_summary": design_summary,
            "planning_summary": planning_summary,
            "child_card_ids": [str(item.get("id") or "") for item in child_cards],
            "child_job_ids": [str(item.get("job_id") or "") for item in child_jobs],
            "child_count": len(child_cards),
            "auto_generated": True,
            "error": f"orchestration_partial_failure: {exc}",
            "updated_at": time.time(),
        })
        parent_metadata["orchestration"] = partial_orchestration
        parent_metadata["execution_objective"] = str(task.objective or parent_metadata.get("execution_objective") or "")
        message = f"Orchestration partially failed after creating {len(child_cards)} child task{'s' if len(child_cards) != 1 else ''}: {exc}"
        await board.update_card(parent_id, status="blocked", progress=max(int(getattr(card, "progress", 0) or 0), 10), metadata=parent_metadata, body=message)
        raise HTTPException(422, message) from exc
    if not child_cards or not child_jobs:
        return None
    orchestration = dict(parent_metadata.get("orchestration") or {})
    orchestration.update({
        "role": "parent",
        "root_card_id": root_id,
        "reason": str(plan.get("reason") or "").strip()[:400],
        "design_summary": design_summary,
        "planning_summary": planning_summary,
        "child_card_ids": [str(item.get("id") or "") for item in child_cards],
        "child_job_ids": [str(item.get("job_id") or "") for item in child_jobs],
        "child_count": len(child_cards),
        "auto_generated": True,
        "updated_at": time.time(),
    })
    parent_metadata["orchestration"] = orchestration
    parent_metadata["execution_objective"] = str(task.objective or parent_metadata.get("execution_objective") or "")
    status_note = planning_summary or design_summary or f"Orchestrated into {len(child_cards)} child execution cards."
    await board.update_card(parent_id, status="executing", progress=max(int(getattr(card, "progress", 0) or 0), 12), metadata=parent_metadata, body=status_note)
    return _orchestrated_response(record_payload=child_jobs[0], parent_id=parent_id, orchestration=orchestration)


def _worker(request: Request) -> CodingWorker:
    worker: Optional[CodingWorker] = getattr(request.app.state, "coding_worker", None)
    if worker is None:
        raise HTTPException(503, "coding worker not available")
    return worker


def _jobs(request: Request) -> BackgroundJobManager:
    jobs: Optional[BackgroundJobManager] = getattr(request.app.state, "coding_jobs", None)
    if jobs is None:
        raise HTTPException(503, "background jobs not available")
    return jobs


def _default_cwd(request: Request, cwd: str) -> str:
    value = str(cwd or "").strip()
    if value and value not in (".", "./"):
        return value
    workspace = ensure_workspace_dir(Path(request.app.state.data_root))
    project_store = getattr(request.app.state, "project_store", None)
    active_project_id = project_store.get_active() if project_store is not None else None
    project = project_store.get(active_project_id) if project_store is not None and active_project_id else None
    project_root = str(getattr(project, "root", "") or "").strip() if project is not None else ""
    if project_root and project_root not in (".", "./"):
        resolved_project_root = Path(project_root).resolve()
        if resolved_project_root != _REPO_ROOT:
            resolved_project_root.mkdir(parents=True, exist_ok=True)
            return str(resolved_project_root)
    return str(workspace)


def _normalize_workspace_path(value: str, cwd: str) -> str:
    text = str(value or "")
    try:
        root = Path(cwd).resolve()
        is_workspace = root.name.lower() == "workspace"
    except Exception:
        root = None
        is_workspace = False
    if not is_workspace:
        return text
    result = text
    if root is not None:
        root_value = str(root)
        root_posix = root_value.replace("\\", "/")
        for prefix in {f"{root_value}\\", f"{root_value}/", f"{root_posix}/"}:
            result = result.replace(prefix, "")
    return re.sub(r"(^|[`'\"]|\s)blackboard[/\\]workspace[/\\]", r"\1", result, flags=re.IGNORECASE)


def _normalize_task_paths(body: ExecuteRequest, cwd: str) -> Dict[str, Any]:
    return {
        "objective": _normalize_workspace_path(body.objective, cwd),
        "files": [_normalize_workspace_path(item, cwd) for item in list(body.files or [])],
        "constraints": [_normalize_workspace_path(item, cwd) for item in list(body.constraints or [])],
        "verification": [_normalize_workspace_path(item, cwd) for item in list(body.verification or [])],
    }


@router.get("/context")
async def context_status(request: Request) -> Dict[str, Any]:
    return _worker(request).context_stats()


@router.post("/execute")
async def execute_sync(body: ExecuteRequest, request: Request) -> Dict[str, Any]:
    worker = _worker(request)
    cwd = _default_cwd(request, body.cwd)
    normalized = _normalize_task_paths(body, cwd)
    task = CodingTask(
        objective=normalized["objective"],
        files=normalized["files"],
        constraints=normalized["constraints"],
        verification=normalized["verification"],
        cwd=cwd,
        context=body.context,
        agents_md_path=body.agents_md_path,
        project_id=body.project_id,
        card_id=body.card_id,
    )
    result = await worker.execute(task)
    return result.to_dict()


@router.post("/jobs")
async def submit_job(body: JobSubmitRequest, request: Request) -> Dict[str, Any]:
    cwd = _default_cwd(request, body.cwd)
    normalized = _normalize_task_paths(body, cwd)
    task = CodingTask(
        objective=normalized["objective"],
        files=normalized["files"],
        constraints=normalized["constraints"],
        verification=normalized["verification"],
        cwd=cwd,
        base_branch=body.base_branch,
        context=body.context,
        agents_md_path=body.agents_md_path,
        project_id=body.project_id,
        card_id=body.card_id,
    )
    if str(body.project_id or "").strip() and str(body.card_id or "").strip():
        try:
            board = _board(request, body.project_id)
            card = board.get(body.card_id)
            if card is not None:
                existing = await _reuse_existing_orchestration(body, request, task, board, card)
                if existing is not None:
                    return existing
                orchestrated = await _submit_orchestrated_jobs(body, request, task, board, card)
                if orchestrated is not None:
                    return orchestrated
        except HTTPException:
            raise
        except Exception as exc:
            logger.debug("[coding] orchestration submit fell back to direct job: %s", exc)
    jobs = _jobs(request)
    record = await jobs.submit(task, max_retries=body.max_retries)
    return record.to_dict()


@router.get("/jobs")
async def list_jobs(request: Request, status: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
    jobs = _jobs(request)
    status_enum = JobStatus(status) if status else None
    records = await jobs.list_jobs(status=status_enum, limit=max(1, min(int(limit or 50), 500)))
    return [r.to_dict() for r in records]


@router.get("/jobs/{job_id}")
async def get_job(job_id: str, request: Request) -> Dict[str, Any]:
    jobs = _jobs(request)
    record = await jobs.get(job_id)
    if record is None:
        raise HTTPException(404, "job not found")
    return record.to_dict()


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, request: Request) -> Dict[str, Any]:
    jobs = _jobs(request)
    ok = await jobs.cancel(job_id)
    return {"job_id": job_id, "cancelled": ok}


@router.post("/jobs/{job_id}/resume")
async def resume_job(job_id: str, request: Request) -> Dict[str, Any]:
    jobs = _jobs(request)
    try:
        record = await jobs.resume(job_id)
    except KeyError:
        raise HTTPException(404, "job not found") from None
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return record.to_dict()


@router.post("/jobs/{job_id}/merge")
async def merge_job(job_id: str, body: MergeRequest, request: Request) -> Dict[str, Any]:
    jobs = _jobs(request)
    result = await jobs.approve_merge(job_id, confirm=body.confirm, message=body.message)
    if not result.get("success"):
        raise HTTPException(422, result.get("message", "merge failed"))
    return result
