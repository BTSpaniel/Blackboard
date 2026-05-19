"""Blackboard FastAPI app entrypoint."""
from __future__ import annotations

import asyncio
import json
import time
import warnings
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List

# Silence the harmless "unclosed transport" / "I/O operation on closed pipe"
# ResourceWarning churn that the Windows Proactor event loop emits when
# short-lived subprocess transports (httpx, playwright, git via asyncio, ...)
# get garbage-collected after the loop has already closed their pipes.
warnings.filterwarnings(
    "ignore",
    category=ResourceWarning,
    module=r"asyncio\..*",
)

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from blackboard.api import (
    approval as approval_api,
    artifacts as artifacts_api,
    audit as audit_api,
    board as board_api,
    chat as chat_api,
    coding as coding_api,
    execution as execution_api,
    files as files_api,
    governors as governors_api,
    projects as projects_api,
    providers as providers_api,
    settings as settings_api,
    skills as skills_api,
    usage as usage_api,
    versioning as versioning_api,
    wiki as wiki_api,
)
from blackboard.execution.preview import get_preview_manager
from blackboard.execution.terminal import get_terminal_manager
from blackboard.coding.budget_allocator import ContextBudgetAllocator
from blackboard.coding.jobs import BackgroundJobManager
from blackboard.coding.reviewer import CodeReviewer
from blackboard.coding.skills import build_skill_index, set_active_index
from blackboard.coding.worker import init_coding_worker
from blackboard.governors.budget import init_budget_governor
from blackboard.governors.capability import init_capability_governor
from blackboard.governors.data_protection import init_data_protection_governor
from blackboard.governors.health import init_health_governor
from blackboard.governors.trust import init_trust_governor
from blackboard.kernel.bus import Bus, get_bus
from blackboard.kernel.config import load_config
from blackboard.kernel.logger import get_logger
from blackboard.kernel.prompts import init_prompts
from blackboard.providers.registry import init_provider_registry
from blackboard.providers.usage import init_usage_tracker
from blackboard.react.approval import init_approval_manager
from blackboard.react.tool_policy import init_tool_policy
from blackboard.react.tools.web_tools import configure_web_search
from blackboard.workspace.audit import AuditLog
from blackboard.workspace.memory import ProjectMemory
from blackboard.workspace.role_overrides import load_overrides, merge_into_config
from blackboard.workspace.key_overrides import load_keys
from blackboard.workspace.model_overrides import load_model_overrides, merge_into_profiles
from blackboard.workspace.redaction import sanitize_text
from blackboard.workspace.coding_settings import load_coding_overrides, merge_coding_config
from blackboard.workspace.temporal_scratchpad import rotate_all_temporal_scratchpads
from blackboard.workspace.version_control import init_version_control
from blackboard.workspace.board import BoardService
from blackboard.workspace.protection_feedback import ProtectionFeedback
from blackboard.workspace.project import ProjectStore
from blackboard.workspace.receipts import ReceiptsService
from blackboard.workspace.remote_share import RemoteShareManager, remote_share_cookie_name, secure_cookie_preferred
from blackboard.workspace.server_access import (
    is_loopback_request,
    load_access_overrides,
    merge_server_config,
    remote_cookie_name,
    request_access_decision,
)
from blackboard.workspace.sync_checkpoint_store import init_sync_checkpoint_store
from blackboard.wiki.manager import WikiManager
from blackboard.react.tools.wiki_tools import set_wiki_manager, set_wiki_provider_registry
from blackboard.react.approval import init_approval_manager
from blackboard.react.tool_policy import init_tool_policy

logger = get_logger("main")

_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_PATH = _ROOT / "config.yaml"
_SHUTDOWN_TIMEOUT_S = 5.0

_SUCCESS_PIPELINE = ["inbox", "designing", "planning", "ready", "executing", "reviewing", "done"]
_FAILURE_PIPELINE = ["inbox", "designing", "planning", "ready", "executing", "blocked"]
_CHECKPOINT_PREFIX = "__checkpoint__:"


def _pending_checkpoint_from_result(result: Any) -> Dict[str, Any]:
    for item in getattr(result, "warnings", []) or []:
        text = str(item or "")
        if not text.startswith(_CHECKPOINT_PREFIX):
            continue
        try:
            payload = json.loads(text[len(_CHECKPOINT_PREFIX):])
        except Exception:
            continue
        if isinstance(payload, dict) and payload.get("questions"):
            return payload
    return {}


def _server_access_context(app: FastAPI) -> Dict[str, Any]:
    return dict(getattr(app.state, "server_config", {}) or {})


def _request_access_payload(app: FastAPI, *, client_host: str, headers: Dict[str, str], query_params: Dict[str, Any], cookies: Dict[str, Any], path: str, method: str, url_scheme: str) -> Dict[str, Any]:
    return request_access_decision(
        _server_access_context(app),
        data_root=getattr(app.state, "data_root", None),
        remote_share_manager=getattr(app.state, "remote_share", None),
        protection_feedback_manager=getattr(app.state, "protection_feedback", None),
        client_host=client_host,
        headers=headers,
        query_params=query_params,
        cookies=cookies,
        path=path,
        method=method,
        url_scheme=url_scheme,
    )


def _secure_cookie_mode(server_config: Dict[str, Any], headers: Dict[str, str], url_scheme: str) -> bool:
    access = dict((server_config or {}).get("access") or {})
    public_base_url = str(access.get("public_base_url") or "")
    forwarded_proto = str(headers.get("x-forwarded-proto") or "").strip().lower()
    if forwarded_proto == "https":
        return True
    if str(url_scheme or "").lower() == "https":
        return True
    return secure_cookie_preferred(public_base_url)


def _card_note_subject(card: Any, objective: str) -> str:
    subject = sanitize_text(str(getattr(card, "title", "") or objective or "this work"), max_chars=120).strip()
    return subject or "this work"


def _status_note_for_transition(status: str, reason: str, card: Any, objective: str) -> str:
    subject = _card_note_subject(card, objective)
    clean_reason = sanitize_text(str(reason or ""), max_chars=220).strip()
    status_key = str(status or "").strip().lower()
    if status_key == "executing":
        return f"Executing now because work is actively running on: {subject}." if not clean_reason or clean_reason == "job started" else f"Executing now because {clean_reason}."
    if status_key == "reviewing":
        return f"Reviewing now because implementation changes were produced and need verification for: {subject}." if not clean_reason or clean_reason == "worker produced changes; review started" else f"Reviewing now because {clean_reason}."
    if status_key == "blocked":
        return f"Blocked because something is preventing forward progress on: {subject}." if not clean_reason else f"Blocked because {clean_reason}."
    if status_key == "done":
        return f"Done because the requested outcome has been completed for: {subject}." if not clean_reason or clean_reason == "job succeeded and review passed" else f"Done because {clean_reason}."
    if status_key == "planning":
        return clean_reason or f"Planning because the implementation needs clearer scope and checks for: {subject}."
    if status_key == "ready":
        return clean_reason or f"Ready because the work is scoped and prepared for execution: {subject}."
    if status_key == "designing":
        return clean_reason or f"Designing because the approach still needs to be shaped before implementation: {subject}."
    return clean_reason or f"Inbox because the work still needs to be triaged and shaped: {subject}."


async def _advance_card_pipeline(board: BoardService, card_id: str, target_status: str, pipeline: List[str] | None = None) -> None:
    sequence = pipeline or _SUCCESS_PIPELINE
    card = board.get(card_id)
    if card is None:
        return
    if card.status not in sequence or target_status not in sequence:
        return
    current_idx = sequence.index(card.status)
    target_idx = sequence.index(target_status)
    if current_idx >= target_idx:
        return
    for status in sequence[current_idx + 1:target_idx]:
        await board.update_card(card_id, status=status)


async def _sync_orchestration_parent(board: BoardService, card: Any) -> None:
    metadata = dict(getattr(card, "metadata", {}) or {})
    orchestration = dict(metadata.get("orchestration") or {})
    parent_id = str(orchestration.get("parent_card_id") or "").strip()
    if not parent_id:
        return
    parent = board.get(parent_id)
    if parent is None:
        return
    children = []
    for entry in board.all_cards():
        child_metadata = dict((entry or {}).get("metadata") or {})
        child_orchestration = dict(child_metadata.get("orchestration") or {})
        if str(child_orchestration.get("parent_card_id") or "").strip() == parent_id:
            children.append(dict(entry or {}))
    if not children:
        return
    counts: Dict[str, int] = {}
    for child in children:
        status = str(child.get("status") or "inbox").strip().lower() or "inbox"
        counts[status] = counts.get(status, 0) + 1
    total = len(children)
    blocked = counts.get("blocked", 0)
    done = counts.get("done", 0)
    reviewing = counts.get("reviewing", 0)
    executing = counts.get("executing", 0)
    queued = counts.get("ready", 0) + counts.get("planning", 0) + counts.get("designing", 0) + counts.get("inbox", 0)
    completed = done + reviewing
    if blocked:
        target_status = "blocked"
        progress = max(int(getattr(parent, "progress", 0) or 0), 90)
        transition_reason = f"{blocked} orchestrated child task{'s' if blocked != 1 else ''} blocked"
    elif completed >= total:
        target_status = "reviewing"
        progress = 95
        transition_reason = f"All {total} orchestrated child task{'s' if total != 1 else ''} completed and are awaiting final review"
    else:
        target_status = "executing"
        progress = max(int(getattr(parent, "progress", 0) or 0), min(84, 15 + int((completed / max(1, total)) * 65)))
        if executing:
            transition_reason = f"{completed}/{total} orchestrated child task{'s' if total != 1 else ''} completed; {executing} running"
        else:
            transition_reason = f"{completed}/{total} orchestrated child task{'s' if total != 1 else ''} completed; {queued} queued"
    parent_metadata = dict(parent.metadata or {})
    parent_orchestration = dict(parent_metadata.get("orchestration") or {})
    parent_orchestration["child_counts"] = counts
    parent_orchestration["updated_at"] = time.time()
    parent_metadata["orchestration"] = parent_orchestration
    status_note = _status_note_for_transition(target_status, transition_reason, parent, parent_metadata.get("execution_objective") or "")
    parent_metadata["status_note"] = status_note
    updates: Dict[str, Any] = {
        "metadata": parent_metadata,
        "body": status_note,
        "progress": progress,
    }
    if str(getattr(parent, "status", "") or "") != target_status:
        updates["status"] = target_status
    await board.update_card(parent_id, **updates)


async def sync_board_card_for_job(app: FastAPI, topic: str, payload: Dict[str, Any]) -> None:
    job_id = str((payload or {}).get("job_id") or "").strip()
    if not job_id:
        return
    jobs = getattr(app.state, "coding_jobs", None)
    if jobs is None:
        return

    async def _mark_orphaned_sync(detail: str) -> None:
        if topic not in {"coding:job.completed", "coding:job.failed"}:
            return
        mark_terminal_synced = getattr(jobs, "mark_terminal_synced", None)
        if not callable(mark_terminal_synced):
            return
        try:
            record = await jobs.get(job_id)
            if record is None:
                return
            await mark_terminal_synced(job_id, record.status.value, detail=detail)
        except Exception as exc:
            logger.debug("[jobs] could not mark orphaned terminal sync for %s (%s): %s", job_id, detail, exc)

    try:
        record = await jobs.get(job_id)
    except Exception as exc:
        logger.debug("[jobs] card sync could not load job %s: %s", job_id, exc)
        return
    if record is None or record.task is None:
        return
    project_id = str(record.task.project_id or "").strip()
    card_id = str(record.task.card_id or "").strip()
    if not project_id or not card_id:
        await _mark_orphaned_sync("missing_target")
        return
    try:
        boards = getattr(app.state, "boards", None)
        if boards is None:
            boards = {}
            app.state.boards = boards
        board = boards.get(project_id)
        if board is None:
            project_store = getattr(app.state, "project_store", None)
            if project_store is not None and project_store.get(project_id) is None:
                await _mark_orphaned_sync("missing_project")
                return
            board = BoardService(
                app.state.data_root,
                project_id,
                bus=getattr(app.state, "bus", None),
                on_done=getattr(app.state, "on_card_done", None),
            )
            boards[project_id] = board
        card = board.get(card_id)
        if card is None:
            await _mark_orphaned_sync("missing_card")
            return
        metadata = dict(card.metadata or {})
        autonomy = dict(metadata.get("autonomy") or {})
        metadata["execution_objective"] = str(getattr(record.task, "objective", "") or metadata.get("execution_objective") or "")
        result = record.result
        review = record.review
        pending_checkpoint = _pending_checkpoint_from_result(result)
        review_passed = review is None or bool(getattr(review, "passed", False))
        payload_success = bool((payload or {}).get("success", False))
        result_success = bool(getattr(result, "success", False)) if result is not None else payload_success
        error_text = str(record.error or (payload or {}).get("error") or getattr(result, "error", "") or "")
        merge_candidates = list((payload or {}).get("merge_candidates") or [])[:4]
        transition_reason = ""
        metadata["last_job"] = {
            "job_id": job_id,
            "status": record.status.value,
            "success": result_success,
            "summary": str((payload or {}).get("summary") or ""),
            "branch": str((payload or {}).get("branch") or record.worktree_branch or ""),
            "cwd": str((payload or {}).get("cwd") or getattr(record.task, "cwd", "") or ""),
            "execution_cwd": str((payload or {}).get("execution_cwd") or ""),
            "error": error_text,
            "review_passed": None if review is None else bool(getattr(review, "passed", False)),
            "patch_count": int((payload or {}).get("patch_count") or len(getattr(result, "patches", []) or [])),
            "new_file_count": int((payload or {}).get("new_file_count") or len(getattr(result, "new_files", []) or [])),
            "merge_candidates": merge_candidates,
        }
        updates: Dict[str, Any] = {"job_id": job_id, "metadata": metadata}
        if topic == "coding:job.created":
            autonomy["active_task"] = True
            autonomy["started_once"] = True
            metadata.pop("coordination", None)
            transition_reason = "job resumed and queued" if bool((payload or {}).get("resumed")) else "job queued"
            updates.update({"status": "executing", "progress": max(int(card.progress or 0), 6)})
            metadata["last_job"]["transition_reason"] = transition_reason
        elif topic == "coding:job.started":
            autonomy["active_task"] = True
            autonomy["started_once"] = True
            autonomy["last_job_started_at"] = time.time()
            metadata.pop("coordination", None)
            await _advance_card_pipeline(board, card_id, "executing")
            card = board.get(card_id) or card
            updates.update({"status": "executing", "progress": max(int(card.progress or 0), 10)})
            transition_reason = "job started"
        elif topic == "coding:job.paused":
            autonomy["active_task"] = True
            autonomy["started_once"] = True
            coordination = dict((payload or {}).get("coordination") or {})
            transition_reason = str((payload or {}).get("reason") or record.progress_note or "job paused for coordination")
            metadata["coordination"] = {
                "status": "paused",
                "reason": transition_reason,
                "related_job_id": str((payload or {}).get("related_job_id") or coordination.get("job_id") or ""),
                "related_card_id": str((payload or {}).get("related_card_id") or coordination.get("card_id") or ""),
                "conflicts": list((payload or {}).get("conflicts") or coordination.get("conflicts") or [])[:12],
                "updated_at": time.time(),
            }
            updates.update({"status": "blocked", "progress": max(int(card.progress or 0), 5)})
            metadata["last_job"]["transition_reason"] = transition_reason
        elif topic == "coding:job.reviewing":
            autonomy["active_task"] = True
            autonomy["started_once"] = True
            autonomy["awaiting_human_review"] = True
            metadata.pop("coordination", None)
            await _advance_card_pipeline(board, card_id, "reviewing")
            card = board.get(card_id) or card
            updates.update({"status": "reviewing", "progress": max(int(card.progress or 0), 85)})
            transition_reason = "worker produced changes; review started"
        elif topic == "coding:job.completed":
            autonomy["active_task"] = True
            autonomy["started_once"] = True
            success = result_success and review_passed
            metadata.pop("coordination", None)
            if pending_checkpoint:
                transition_reason = pending_checkpoint.get("reason") or "user input required"
                metadata["pending_questions"] = pending_checkpoint
                metadata["last_job"]["checkpoint_pending"] = True
                autonomy["awaiting_human_review"] = False
                await _advance_card_pipeline(board, card_id, "blocked", _FAILURE_PIPELINE)
                card = board.get(card_id) or card
                updates.update({"status": "blocked", "progress": max(int(card.progress or 0), 90)})
            elif success:
                transition_reason = "job succeeded and review passed; waiting for human finish approval"
                autonomy["awaiting_human_review"] = True
                await _advance_card_pipeline(board, card_id, "reviewing")
                card = board.get(card_id) or card
            elif not result_success:
                transition_reason = error_text or "worker produced no successful file changes"
                autonomy["awaiting_human_review"] = False
                await _advance_card_pipeline(board, card_id, "blocked", _FAILURE_PIPELINE)
                card = board.get(card_id) or card
            else:
                transition_reason = "review did not pass"
                autonomy["awaiting_human_review"] = False
                await _advance_card_pipeline(board, card_id, "blocked", _FAILURE_PIPELINE)
                card = board.get(card_id) or card
            metadata["last_job"]["transition_reason"] = transition_reason
            if merge_candidates:
                metadata["merge_candidates"] = merge_candidates
                metadata["merge_recommendation"] = {
                    "status": "suggested",
                    "job_id": job_id,
                    "best": merge_candidates[0],
                    "updated_at": time.time(),
                }
            else:
                metadata.pop("merge_candidates", None)
                metadata.pop("merge_recommendation", None)
            if not pending_checkpoint:
                updates.update({"status": "reviewing" if success else "blocked", "progress": 95 if success else max(int(card.progress or 0), 90)})
        elif topic == "coding:job.failed":
            autonomy["active_task"] = True
            autonomy["started_once"] = True
            autonomy["awaiting_human_review"] = False
            metadata.pop("coordination", None)
            await _advance_card_pipeline(board, card_id, "blocked", _FAILURE_PIPELINE)
            card = board.get(card_id) or card
            updates.update({"status": "blocked", "progress": max(int(card.progress or 0), 90)})
            transition_reason = error_text or "job failed"
            metadata["last_job"]["transition_reason"] = transition_reason
        else:
            return
        current_status = str(updates.get("status") or getattr(card, "status", "") or "")
        status_note = _status_note_for_transition(current_status, transition_reason, card, metadata.get("execution_objective") or "")
        autonomy["last_status"] = current_status
        autonomy["last_job_id"] = job_id
        metadata["autonomy"] = autonomy
        metadata["status_note"] = status_note
        updates["metadata"] = metadata
        updates["body"] = status_note
        updated = await board.update_card(card_id, **updates)
        if updated is None:
            await _mark_orphaned_sync("missing_card")
            return
        if updated is not None:
            await _sync_orchestration_parent(board, updated)
            if topic in {"coding:job.completed", "coding:job.failed"}:
                mark_terminal_synced = getattr(jobs, "mark_terminal_synced", None)
                if callable(mark_terminal_synced):
                    try:
                        await mark_terminal_synced(job_id, record.status.value)
                    except Exception as exc:
                        logger.debug("[jobs] could not mark terminal sync for %s: %s", job_id, exc)
            await app.state.bus.emit("board:card.job_synced", {
                "project_id": project_id,
                "card_id": card_id,
                "job_id": job_id,
                "topic": topic,
                "card_status": updated.status,
                "job_status": record.status.value,
                "success": result_success,
                "review_passed": None if review is None else bool(getattr(review, "passed", False)),
                "reason": transition_reason,
                "error": error_text,
                "patch_count": metadata["last_job"]["patch_count"],
                "new_file_count": metadata["last_job"]["new_file_count"],
            })
    except Exception as exc:
        logger.debug("[jobs] card sync failed for %s/%s: %s", project_id, card_id, exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = load_config(_CONFIG_PATH)
    # Allow tests / sandboxes to override the data directory via env var so they
    # don't pollute the live project store.
    import os as _os
    override = _os.environ.get("BLACKBOARD_DATA_DIR")
    data_root = Path(override or cfg.get("data_dir", "data")).resolve()
    data_root.mkdir(parents=True, exist_ok=True)
    app.state.data_root = data_root
    app.state.config = cfg
    server_overrides = load_access_overrides(data_root)
    base_server = cfg.section("server")
    base_server_access = dict(base_server.get("access") or {})
    merged_access = {**base_server_access, **server_overrides}
    server_config = merge_server_config({**base_server, "access": merged_access})
    app.state.server_config = server_config
    coding_overrides = load_coding_overrides(data_root)
    app.state.coding_config = merge_coding_config(cfg.section("coding"), coding_overrides)
    protection_feedback = ProtectionFeedback()
    app.state.protection_feedback = protection_feedback
    remote_share = RemoteShareManager(
        data_root,
        server_port=int(server_config.get("port") or 8780),
        event_hook=protection_feedback.observe_remote_share_event,
    )
    await remote_share.restore(public_base_url=str((server_config.get("access") or {}).get("public_base_url") or ""))
    app.state.remote_share = remote_share

    rotated_temporal_scratchpads = rotate_all_temporal_scratchpads(data_root)
    if rotated_temporal_scratchpads:
        logger.info("[temporal_scratchpad] rotated %d hot-layer file(s) on boot", rotated_temporal_scratchpads)

    # Init the data/.git timeline early so subsequent state changes get tracked.
    vcs = init_version_control(data_root)
    app.state.vcs = vcs
    if vcs.ensure_initialized():
        logger.info("[vcs] data version control ready: %s", data_root / ".git")

    # Ensure the default workspace directory exists so the UI picker can land
    # there on first run. The path resolution prefers <repo>/workspace for the
    # development layout and falls back to <data_root>/workspace otherwise.
    try:
        from blackboard.api.files import ensure_workspace_dir
        ws = ensure_workspace_dir(data_root)
        app.state.workspace_dir = ws
        logger.info("[workspace] default workspace dir: %s", ws)
    except Exception as exc:
        logger.warning("[workspace] could not ensure workspace dir: %s", exc)

    bus = get_bus()
    app.state.bus = bus

    # Prompts
    init_prompts(_ROOT / "data" / "prompts.yaml")
    init_usage_tracker(data_root)
    init_budget_governor(cfg.section("governors.budget"), data_root=data_root)
    init_capability_governor(cfg.section("governors.capability"), data_root=data_root)
    init_data_protection_governor()
    init_health_governor(cfg.section("governors.health"), data_root=data_root)
    init_trust_governor(cfg.section("governors.trust"), data_root=data_root)
    init_approval_manager(cfg.section("approval") or {})
    init_tool_policy(strict=bool(cfg.get("governors.tool_policy.strict", False)), data_root=data_root)
    configure_web_search(cfg.section("web_search") or {})

    # Audit hook → write to active project's audit log
    audit_logs: Dict[str, AuditLog] = {}
    app.state.audit_logs = audit_logs

    def audit_hook(event: Dict[str, Any]) -> None:
        store = app.state.project_store
        active = store.get_active() if store else None
        if not active:
            return
        log = audit_logs.get(active)
        if log is None:
            log = AuditLog(data_root, active)
            audit_logs[active] = log
        log.record(event.get("kind", "event"), event)

    # Provider registry — apply persisted role priority + per-profile key overrides on top of config.yaml.
    providers_section = cfg.section("providers")
    role_overrides = load_overrides(data_root)
    if role_overrides:
        merged_roles = merge_into_config(providers_section.get("roles") or {}, role_overrides)
        providers_section = {**providers_section, "roles": merged_roles}
        logger.info("Applied %d role override(s) from %s", len(role_overrides), data_root / "providers" / "role_overrides.json")
    key_overrides = load_keys(data_root)
    if key_overrides:
        merged_profiles = {pid: dict(prof) for pid, prof in (providers_section.get("profiles") or {}).items()}
        for pid, value in key_overrides.items():
            if pid in merged_profiles:
                merged_profiles[pid]["api_key"] = value
        providers_section = {**providers_section, "profiles": merged_profiles}
        logger.info("Applied %d API key override(s) from %s", len(key_overrides), data_root / "providers" / "key_overrides.json")
    model_overrides = load_model_overrides(data_root)
    if model_overrides:
        providers_section = {
            **providers_section,
            "profiles": merge_into_profiles(providers_section.get("profiles") or {}, model_overrides),
        }
        logger.info("Applied %d model override(s) from %s", len(model_overrides), data_root / "providers" / "model_overrides.json")
    registry = init_provider_registry(providers_section, audit_hook=audit_hook)
    app.state.provider_registry = registry
    app.state.data_root = data_root  # ensure routes can resolve override path

    # Project store
    project_store = ProjectStore(data_root)
    app.state.project_store = project_store
    app.state.boards = {}
    app.state.project_memories = {}
    app.state.message_ledgers = {}

    # Project intelligence + checkpoint store
    project_intel_dir = data_root / "project_intelligence"
    project_intel_dir.mkdir(parents=True, exist_ok=True)
    app.state.project_intel_dir = project_intel_dir
    init_sync_checkpoint_store(data_root / "coding" / "checkpoints.json")
    wiki_manager = WikiManager(data_root / "wiki")
    app.state.wiki_manager = wiki_manager
    set_wiki_manager(wiki_manager)
    set_wiki_provider_registry(registry)

    # Coding worker + reviewer + background jobs
    allocator = ContextBudgetAllocator(
        total_budget=int(cfg.get("context.budget", 200_000)),
        allocations=cfg.section("context.budget_allocations") or None,
    )
    worker = init_coding_worker(
        registry,
        data_dir=data_root,
        max_iterations=int(cfg.get("react.coding_max_iterations", 12)),
        max_retries=int(cfg.get("coding.max_job_retries", 2)),
        wiki_manager=wiki_manager,
    )
    worker._allocator = allocator  # noqa: SLF001 — wire in custom allocator
    app.state.coding_worker = worker

    reviewer = CodeReviewer(registry=registry)
    app.state.code_reviewer = reviewer

    coding_jobs = BackgroundJobManager(
        db_path=data_root / "coding" / "jobs.db",
        worker=worker,
        reviewer=reviewer,
        bus=bus,
        worktree_dir=cfg.get("coding.worktree_dir", ".worktrees"),
        base_branch=cfg.get("coding.worktree_base_branch", "main"),
        max_concurrent=int((app.state.coding_config or {}).get("max_concurrent", cfg.get("coding.max_concurrent", 4))),
    )
    await coding_jobs.start()
    app.state.coding_jobs = coding_jobs

    # SKILL.md index
    skills_dir = data_root / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    skill_index = build_skill_index(global_dir=skills_dir)
    set_active_index(skill_index)
    app.state.skill_index = skill_index

    # Receipts service — wired into the board's on_done hook.
    def _memory_factory(project_id: str) -> ProjectMemory:
        existing = app.state.project_memories.get(project_id)
        if existing is not None:
            return existing
        mem = ProjectMemory(data_root, project_id, project_intel_dir=project_intel_dir)
        app.state.project_memories[project_id] = mem
        return mem

    receipts = ReceiptsService(registry, memory_factory=_memory_factory)
    app.state.receipts = receipts

    async def _on_card_done(project_id: str, card: Dict[str, Any]) -> None:
        try:
            await receipts.write_for_card(project_id=project_id, card=card)
            await bus.emit("card:receipt.written", {"project_id": project_id, "card_id": card.get("id")})
        except Exception as exc:
            logger.debug("[lifespan] receipt write failed: %s", exc)

    app.state.on_card_done = _on_card_done

    async def _job_card_sync(topic: str, payload: Dict[str, Any]) -> None:
        await sync_board_card_for_job(app, topic, payload)

    bus_unsubscribers = [
        bus.subscribe("coding:job.created", _job_card_sync),
        bus.subscribe("coding:job.started", _job_card_sync),
        bus.subscribe("coding:job.paused", _job_card_sync),
        bus.subscribe("coding:job.reviewing", _job_card_sync),
        bus.subscribe("coding:job.completed", _job_card_sync),
        bus.subscribe("coding:job.failed", _job_card_sync),
    ]

    replayed_terminal_jobs = await coding_jobs.replay_unsynced_terminal_jobs(limit=500)
    if replayed_terminal_jobs:
        logger.info("[jobs] replayed %d unsynced terminal job(s) after startup", replayed_terminal_jobs)

    # Periodic provider health pings — first ping fires shortly after boot, then at a conservative interval.
    async def _health_loop():
        # Fire one fast initial ping so the UI gets data before the user opens Settings.
        await asyncio.sleep(0.25)
        health_interval = max(30, int(cfg.get("providers.health_interval_s", 30)))
        while True:
            try:
                await providers_api.sync_verified_provider_state(registry, data_root, probe=True)
            except Exception as exc:
                logger.debug("[health] periodic check failed: %s", exc)
            await asyncio.sleep(health_interval)

    health_task = asyncio.create_task(_health_loop())
    app.state.health_task = health_task

    async def _remote_share_loop():
        await asyncio.sleep(5.0)
        while True:
            try:
                await remote_share.renew_if_needed()
            except Exception as exc:
                logger.debug("[remote_share] renew failed: %s", exc)
            await asyncio.sleep(900)

    remote_share_task = asyncio.create_task(_remote_share_loop())
    app.state.remote_share_task = remote_share_task

    # WS broadcast — pipe bus events to all connected clients.
    app.state.ws_clients = set()

    async def _broadcast(topic: str, payload: Dict[str, Any]) -> None:
        if not app.state.ws_clients:
            return
        message = json.dumps({"topic": topic, "payload": payload}, default=str)
        dead = []
        for ws in list(app.state.ws_clients):
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            app.state.ws_clients.discard(ws)

    bus_unsubscribers.append(bus.subscribe("*", _broadcast))

    async def _await_shutdown_step(label: str, awaitable: Any, *, timeout_s: float = _SHUTDOWN_TIMEOUT_S) -> None:
        try:
            await asyncio.wait_for(awaitable, timeout=timeout_s)
        except asyncio.TimeoutError:
            logger.warning("[shutdown] %s timed out after %.1fs", label, timeout_s)
        except Exception as exc:
            logger.debug("[shutdown] %s failed: %s", label, exc)

    logger.info("Blackboard started — data_root=%s", data_root)
    try:
        yield
    finally:
        for unsubscribe in reversed(bus_unsubscribers):
            try:
                unsubscribe()
            except Exception:
                pass
        health_task.cancel()
        try:
            await asyncio.wait_for(health_task, timeout=2.0)
        except (asyncio.CancelledError, Exception):
            pass
        remote_share_task.cancel()
        try:
            await asyncio.wait_for(remote_share_task, timeout=2.0)
        except (asyncio.CancelledError, Exception):
            pass
        await _await_shutdown_step("remote_share.close", remote_share.close())
        await _await_shutdown_step("coding_jobs.stop", coding_jobs.stop(), timeout_s=8.0)
        await _await_shutdown_step("terminal_manager.close_all", get_terminal_manager().close_all())
        await _await_shutdown_step("preview_manager.stop_all", get_preview_manager().stop_all())
        await _await_shutdown_step("provider_registry.close", registry.close())
        logger.info("Blackboard stopped.")


app = FastAPI(title="Blackboard", version="0.1.0", lifespan=lifespan)

@app.middleware("http")
async def enforce_server_access(request: Request, call_next):
    client_host = str(getattr(request.client, "host", "") or "")
    server_config = _server_access_context(request.app)
    secure_cookie_mode = _secure_cookie_mode(server_config, dict(request.headers or {}), request.url.scheme)
    decision = _request_access_payload(
        request.app,
        client_host=client_host,
        headers=dict(request.headers or {}),
        query_params=dict(request.query_params or {}),
        cookies=dict(request.cookies or {}),
        path=request.url.path,
        method=request.method,
        url_scheme=request.url.scheme,
    )
    if not decision.get("allowed"):
        return JSONResponse({"detail": str(decision.get("reason") or "forbidden")}, status_code=403)
    response = await call_next(request)
    replacement_share_cookie = str(decision.get("replacement_cookie") or "").strip()
    if replacement_share_cookie:
        response.set_cookie(
            remote_share_cookie_name(secure=secure_cookie_mode),
            replacement_share_cookie,
            max_age=60 * 60 * 24 * 30,
            httponly=True,
            samesite="lax",
            secure=secure_cookie_mode,
            path="/",
        )
    if decision.get("scope") == "remote" and decision.get("reason") == "remote_token_valid":
        cookie_token = ""
        for cookie_name in (remote_cookie_name(secure=True), remote_cookie_name(secure=False)):
            cookie_token = str(request.cookies.get(cookie_name) or "").strip()
            if cookie_token:
                break
        token = str(request.query_params.get("token") or request.query_params.get("access_token") or request.headers.get("x-blackboard-remote-token") or cookie_token or "").strip()
        auth = str(request.headers.get("authorization") or "").strip()
        if not token and auth.lower().startswith("bearer "):
            token = auth[7:].strip()
        if token:
            response.set_cookie(
                remote_cookie_name(secure=secure_cookie_mode),
                token,
                max_age=60 * 60 * 24 * 30,
                httponly=True,
                samesite="lax",
                secure=secure_cookie_mode,
                path="/",
            )
    return response

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers
app.include_router(providers_api.router)
app.include_router(projects_api.router)
app.include_router(board_api.router)
app.include_router(chat_api.router)
app.include_router(approval_api.router)
app.include_router(coding_api.router)
app.include_router(audit_api.router)
app.include_router(artifacts_api.router)
app.include_router(settings_api.router)
app.include_router(skills_api.router)
app.include_router(execution_api.router)
app.include_router(files_api.router)
app.include_router(usage_api.router)
app.include_router(versioning_api.router)
app.include_router(wiki_api.router)
app.include_router(governors_api.router)


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    client_host = str(getattr(ws.client, "host", "") or "")
    allowed = _request_access_payload(
        app,
        client_host=client_host,
        headers=dict(ws.headers or {}),
        query_params=dict(ws.query_params or {}),
        cookies=dict(ws.cookies or {}),
        path="/ws",
        method="WEBSOCKET",
        url_scheme="https" if str(ws.headers.get("x-forwarded-proto") or "").lower() == "https" else "http",
    )
    if not allowed.get("allowed"):
        await ws.close(code=1008, reason=str(allowed.get("reason") or "forbidden"))
        return
    await ws.accept()
    app.state.ws_clients.add(ws)
    try:
        await ws.send_text(json.dumps({"topic": "ws:hello", "payload": {"version": "0.1.0"}}))
        registry = app.state.provider_registry
        await ws.send_text(json.dumps({
            "topic": "providers:snapshot",
            "payload": providers_api.public_provider_snapshot(registry),
        }, default=str))
        asyncio.create_task(providers_api.sync_verified_provider_state(registry, app.state.data_root, probe=False))
        while True:
            message = await ws.receive()
            if message.get("type") == "websocket.disconnect":
                break
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        app.state.ws_clients.discard(ws)


# Static UI mount + root.
_UI_DIR = _ROOT / "ui"
if _UI_DIR.exists():
    app.mount("/ui", StaticFiles(directory=str(_UI_DIR)), name="ui")
_ASSETS_DIR = _ROOT / "assets"
if _ASSETS_DIR.exists():
    app.mount("/assets", StaticFiles(directory=str(_ASSETS_DIR)), name="assets")


@app.get("/", include_in_schema=False)
async def root() -> Any:
    index = _UI_DIR / "index.html"
    if index.exists():
        return FileResponse(index)
    return JSONResponse({
        "service": "Blackboard",
        "version": "0.1.0",
        "ui": "/ui/index.html",
        "api": "/api/*",
    })


@app.get("/join", include_in_schema=False)
async def join_remote_share(request: Request, t: str = "") -> Any:
    remote_share = getattr(request.app.state, "remote_share", None)
    protection_feedback = getattr(request.app.state, "protection_feedback", None)
    server_config = _server_access_context(request.app)
    access = dict(server_config.get("access") or {})
    secure_cookie_mode = _secure_cookie_mode(server_config, dict(request.headers or {}), request.url.scheme)
    client_ip = str(getattr(request.client, "host", "") or "")
    if not access.get("remote_enabled"):
        raise HTTPException(403, "remote_disabled")
    invite = remote_share.validate_invite(t) if remote_share is not None else None
    if invite is None:
        if protection_feedback is not None:
            protection_feedback.record_denial(client_ip=client_ip, reason="remote_invite_invalid", path="/join", weight=1.5)
        raise HTTPException(401, "invalid_or_expired_invite")
    if protection_feedback is not None:
        protection_feedback.record_allow(client_ip=client_ip, reason="remote_invite_valid", path="/join")
    cookie_value = remote_share.register_join(
        invite,
        remote_ip=client_ip,
        user_agent=str(request.headers.get("user-agent") or ""),
    )
    response = RedirectResponse(url="/", status_code=307)
    response.set_cookie(
        remote_share_cookie_name(secure=secure_cookie_mode),
        cookie_value,
        max_age=60 * 60 * 24 * 30,
        httponly=True,
        samesite="lax",
        secure=secure_cookie_mode,
        path="/",
    )
    return response


@app.get("/healthz", include_in_schema=False)
async def healthz() -> Dict[str, str]:
    return {"status": "ok"}


# Inline SVG favicon — keeps the browser console clean without shipping a binary asset.
_FAVICON_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'>"
    "<rect width='32' height='32' rx='6' fill='#0b0d10'/>"
    "<rect x='4' y='4' width='6' height='24' fill='#6b7280'/>"
    "<rect x='12' y='4' width='6' height='14' fill='#8b5cf6'/>"
    "<rect x='20' y='4' width='6' height='9' fill='#3b82f6'/>"
    "<rect x='12' y='20' width='6' height='8' fill='#22c55e'/>"
    "<rect x='20' y='15' width='6' height='13' fill='#14b8a6'/>"
    "</svg>"
)


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    from fastapi.responses import Response
    return Response(content=_FAVICON_SVG, media_type="image/svg+xml")
