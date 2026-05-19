"""Coding worker — turns a CodingTask into verified file mutations.

Flow:
  1. Snapshot workspace files (text).
  2. Build a context envelope (XML-tagged sections, markdown bodies, budget-allocated).
  3. Drive a ReActLoop against the `coder` provider with file/search/commands/git tools.
  4. Snapshot again, diff before/after → patches + new_files.
  5. Update project intelligence + card workspace + sync checkpoint.
  6. On no-mutation, retry once with a sharper retry-requirements block.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from blackboard.coding.agents_md import load_agents_md
from blackboard.coding.adaptive_skills import synthesize_adaptive_skills
from blackboard.coding.budget_allocator import ContextBudgetAllocator
from blackboard.coding.context_compiler import CodingContextCompiler
from blackboard.coding.context_envelope import ContextEnvelope
from blackboard.coding.context_rot import ContextCompressor, ContextRotDetector
from blackboard.coding.models import CodingResult, CodingTask, FilePatch, NewFile
from blackboard.coding import project_intelligence
from blackboard.coding.skill_promotion import SkillPromotionGate, promoted_skill_dir
from blackboard.coding.scope_guard import ScopeGuard
from blackboard.coding.skills import SkillIndex, build_skill_index, set_active_index
from blackboard.kernel.bus import get_bus
from blackboard.kernel.json_schema import parse_json_payload, validate_payload
from blackboard.kernel.logger import get_logger
from blackboard.kernel.prompts import get_prompts
from blackboard.providers.registry import ProviderRegistry
from blackboard.providers.usage import call_and_record_provider
from blackboard.react.loop import ReActLoop
from blackboard.react.tool_registry import ToolRegistry
from blackboard.react.tools.file_ops import set_workspace_policy
from blackboard.react.tools.registry_builder import build_default_registry
from blackboard.workspace import card_workspace as cw
from blackboard.workspace.redaction import sanitize_text
from blackboard.workspace.sync_checkpoint_store import get_sync_checkpoint_store
from blackboard.workspace.temporal_scratchpad import TemporalScratchpadStore, coding_temporal_session_id
from blackboard.workspace.tool_ledger import get_tool_ledger

logger = get_logger("coding.worker")

_SNAPSHOT_SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".pytest_cache", ".worktrees", ".venv", "venv"}
_SNAPSHOT_SKIP_SUFFIXES = {".pyc", ".pyo", ".so", ".dll", ".exe", ".bin", ".db", ".sqlite", ".png", ".jpg", ".gif", ".pdf", ".zip", ".gz"}
_SNAPSHOT_MAX_FILES = 2000
_SNAPSHOT_MAX_FILE_BYTES = 400_000

_PROGRESS_PATH_RE = re.compile(r"(?:[A-Za-z]:[\\/]|\.{0,2}[\\/])?(?:[\w.-]+[\\/])*\w[\w.-]*\.[A-Za-z0-9]{1,8}")

_FILE_READ_LIMIT = 6000
_MAX_FILES_INJECT = 8
_CHECKPOINT_PREFIX = "__checkpoint__:"
_CHECKPOINT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "reason": {"type": "string", "maxLength": 500},
        "questions": {
            "type": "array",
            "maxItems": 4,
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "maxLength": 80},
                    "prompt": {"type": "string", "maxLength": 500},
                    "options": {
                        "type": "array",
                        "minItems": 2,
                        "maxItems": 4,
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string", "maxLength": 80},
                                "label": {"type": "string", "maxLength": 160},
                                "value": {"type": "string", "maxLength": 500},
                            },
                            "required": ["label"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["prompt", "options"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["reason", "questions"],
    "additionalProperties": False,
}


class CodingWorker:
    """Synchronous coder. Drives ReActLoop against the configured `coder` provider."""

    def __init__(
        self,
        registry: ProviderRegistry,
        *,
        data_dir: Path,
        max_iterations: int = 12,
        max_retries: int = 1,
        project_intel_dir: Optional[Path] = None,
        budget_allocator: Optional[ContextBudgetAllocator] = None,
        wiki_manager=None,
    ) -> None:
        self._registry = registry
        self._data_dir = Path(data_dir)
        self._max_iter = int(max_iterations)
        self._max_retries = int(max_retries)
        self._project_intel_dir = Path(project_intel_dir) if project_intel_dir else self._data_dir / "project_intelligence"
        self._allocator = budget_allocator or ContextBudgetAllocator()
        self._wiki_manager = wiki_manager
        self._context_rot_detector = ContextRotDetector()
        self._context_compressor = ContextCompressor(detector=self._context_rot_detector)
        # Per-card prompt-cache hints — OpenAI `previous_response_id`, used to chain retries
        # against the same prompt-cache key on the provider side.
        self._previous_response_ids: Dict[str, str] = {}

    # ── Main entry ───────────────────────────────────────────────

    async def execute(self, task: CodingTask) -> CodingResult:
        started = time.monotonic()
        cwd_abs = str(Path(task.cwd).resolve())
        set_workspace_policy(Path(cwd_abs), full_access=True)
        tool_registry = build_default_registry()

        # Project intelligence (load or bootstrap).
        project_summary = project_intelligence.ensure_project_intelligence(
            self._project_intel_dir,
            cwd=cwd_abs,
            task_files=task.files,
            objective=task.objective,
        )
        project_block = project_intelligence.build_project_context_block(project_summary)

        agents_md_text = load_agents_md(cwd=cwd_abs, explicit_path=task.agents_md_path)
        skill_index = self._build_skill_index(task, cwd_abs, tool_registry.all_names())
        set_active_index(skill_index)

        before = self._snapshot(cwd_abs)
        attempt_notes: List[str] = []

        temporal_project_id = task.project_id or "default"
        temporal_session_id = coding_temporal_session_id(
            temporal_project_id,
            card_id=task.card_id or "",
            task_id=task.task_id or "",
            cwd=cwd_abs,
        )
        temporal_store = TemporalScratchpadStore(self._data_dir, temporal_project_id)
        request_text = task.objective.strip()
        if task.context.strip():
            request_text = (request_text + "\n\n" + task.context.strip())[:1600]
        temporal_store.append(
            temporal_session_id,
            "coding-request",
            request_text,
            {"card_id": task.card_id or "", "task_id": task.task_id or "", "files": list(task.files or [])[:8]},
        )
        temporal_store.append_orchestration_state(
            temporal_session_id,
            "coding.execute.start",
            {
                "card_id": task.card_id or "",
                "task_id": task.task_id or "",
                "cwd": cwd_abs,
                "files": list(task.files or [])[:8],
            },
        )
        temporal_store.append_plan_state(
            temporal_session_id,
            {
                "title": task.objective[:160],
                "steps": list(task.verification or [])[:6],
                "status": "created",
                "card_id": task.card_id or "",
                "task_id": task.task_id or "",
            },
        )

        # Checkpoint pre-edit state for sync restore safety.
        checkpoint_store = get_sync_checkpoint_store()
        checkpoint_files: Dict[str, Optional[str]] = {}

        last_result: Optional[CodingResult] = None
        last_request_id = ""
        last_stopped_reason = ""
        for attempt in range(self._max_retries + 1):
            session_id = f"{task.card_id or task.task_id or 'sync'}_a{attempt + 1}"
            envelope = self._build_envelope(
                task,
                cwd_abs=cwd_abs,
                agents_md=agents_md_text,
                project_block=project_block,
                skill_index=skill_index,
                attempt=attempt,
                tool_ledger_session=session_id,
                attempt_notes=attempt_notes,
            )
            try:
                response = await self._registry.call_with_fallback(
                    "coder",
                    lambda provider, env=envelope, tr=tool_registry, si=skill_index: self._run_loop(provider, tr, task, env, attempt, si),
                )
            except Exception as exc:
                last_result = CodingResult(
                    success=False,
                    error=str(exc),
                    elapsed_s=time.monotonic() - started,
                )
                self._record_attempt(task, attempt, success=False, error=str(exc))
                temporal_store.append(
                    temporal_session_id,
                    "coding-attempt",
                    f"Attempt {attempt + 1} failed before completion.\n\n{str(exc)[:1200]}",
                    {"attempt": attempt + 1, "success": False, "stopped_reason": "provider_error"},
                )
                temporal_store.append_execution_metrics(
                    temporal_session_id,
                    {
                        "phase": "coding_attempt",
                        "attempt": attempt + 1,
                        "request_id": session_id,
                        "status": "provider_error",
                        "stopped_reason": "provider_error",
                        "error": str(exc)[:240],
                        "card_id": task.card_id or "",
                        "task_id": task.task_id or "",
                    },
                )
                temporal_store.append_orchestration_state(
                    temporal_session_id,
                    "coding.execute.finish",
                    {
                        "status": "failed",
                        "request_id": session_id,
                        "stopped_reason": "provider_error",
                        "files": [],
                    },
                )
                break

            last_request_id = str(response.get("request_id") or session_id)
            last_stopped_reason = str(response.get("stopped_reason") or "")
            after = self._snapshot(cwd_abs)
            result = self._build_result(task, before=before, after=after, response=response, started_at=started)
            tool_sequence = self._tool_sequence_for_session(last_request_id)
            tool_stats = get_tool_ledger().stats(last_request_id) if last_request_id else {}
            # Stash original contents of changed files for the checkpoint store.
            for patch in result.patches:
                checkpoint_files.setdefault(patch.file, patch.old_string)
            for new_file in result.new_files:
                checkpoint_files.setdefault(new_file.file, None)

            self._record_attempt(
                task,
                attempt,
                success=result.success,
                error=result.error,
                stopped_reason=response.get("stopped_reason", ""),
                files_changed=[p.file for p in result.patches] + [n.file for n in result.new_files],
            )
            changed_files = [p.file for p in result.patches] + [n.file for n in result.new_files]
            outcome_lines = [f"Attempt {attempt + 1} success={str(result.success).lower()} stopped={last_stopped_reason or 'final_answer'}"]
            if result.summary:
                outcome_lines.append(result.summary[:600])
            elif result.error:
                outcome_lines.append(result.error[:600])
            if changed_files:
                outcome_lines.append("Files: " + ", ".join(changed_files[:8]))
            temporal_store.append(
                temporal_session_id,
                "coding-attempt",
                "\n\n".join(part for part in outcome_lines if part),
                {"attempt": attempt + 1, "success": result.success, "stopped_reason": last_stopped_reason, "files": changed_files[:8]},
            )
            temporal_store.append_execution_metrics(
                temporal_session_id,
                {
                    "phase": "coding_attempt",
                    "attempt": attempt + 1,
                    "request_id": last_request_id,
                    "status": "success" if result.success else "failed",
                    "stopped_reason": last_stopped_reason,
                    "tool_calls": int(response.get("tool_calls") or 0),
                    "iterations": int(response.get("iterations") or 0),
                    "tool_sequence": tool_sequence[:12],
                    "tool_success": int(tool_stats.get("success") or 0),
                    "tool_failed": int(tool_stats.get("failed") or 0),
                    "tool_timeout": int(tool_stats.get("timeout") or 0),
                    "tool_total_elapsed_ms": float(tool_stats.get("total_elapsed_ms") or 0.0),
                    "files": changed_files[:8],
                    "card_id": task.card_id or "",
                    "task_id": task.task_id or "",
                },
            )
            temporal_store.append_orchestration_state(
                temporal_session_id,
                "coding.execute.finish",
                {
                    "status": "success" if result.success else "failed",
                    "request_id": last_request_id or temporal_session_id,
                    "stopped_reason": last_stopped_reason,
                    "files": changed_files[:8],
                },
            )
            if tool_sequence:
                temporal_store.append_last_used(
                    temporal_session_id,
                    tool_sequence[-1],
                    {
                        "request_id": last_request_id,
                        "attempt": attempt + 1,
                        "status": "success" if result.success else "failed",
                        "files": changed_files[:8],
                    },
                )

            last_result = result
            if result.success:
                break
            attempt_notes.append(self._note_for_retry(result, response.get("stopped_reason", "")))
            # Refresh `before` for next attempt so we re-diff from current disk state.
            before = self._snapshot(cwd_abs)

        result = last_result or CodingResult(success=False, error="no attempts executed", elapsed_s=time.monotonic() - started)

        # Update project intelligence + checkpoint store.
        try:
            project_intelligence.update_project_intelligence(
                self._project_intel_dir,
                cwd=cwd_abs,
                objective=task.objective,
                task_files=list(task.files or []),
                changed_files=[p.file for p in result.patches] + [n.file for n in result.new_files],
                success=result.success,
                summary_text=result.summary,
                error_text=result.error,
                stopped_reason=last_stopped_reason,
                wiki_manager=self._wiki_manager,
            )
        except Exception as exc:
            logger.debug("[worker] project intelligence update skipped: %s", exc)

        if checkpoint_store is not None and checkpoint_files:
            try:
                checkpoint = checkpoint_store.record(
                    cwd=cwd_abs,
                    objective=task.objective,
                    files_touched=list(checkpoint_files.keys()),
                    checkpoint_files=checkpoint_files,
                    project_id=task.project_id,
                    card_id=task.card_id,
                )
                await get_bus().emit("sync_checkpoint.recorded", {
                    "checkpoint_id": checkpoint.id,
                    "project_id": task.project_id,
                    "card_id": task.card_id,
                    "cwd": cwd_abs,
                    "objective": task.objective[:200],
                    "files": list(checkpoint.files_touched or [])[:24],
                    "status": checkpoint.status,
                })
            except Exception as exc:
                logger.debug("[worker] checkpoint record failed: %s", exc)

        changed_files = [p.file for p in result.patches] + [n.file for n in result.new_files]
        tool_sequence = self._tool_sequence_for_session(last_request_id)
        tool_stats = get_tool_ledger().stats(last_request_id) if last_request_id else {}
        try:
            gate = SkillPromotionGate(self._data_dir, task.project_id or "default")
            gate.observe_coding_workflow(
                objective=task.objective,
                summary_text=result.summary or result.error,
                success=result.success,
                observation_id=f"coding:{last_request_id or temporal_session_id}:{task.card_id or task.task_id or task.objective[:80]}",
                session_id=last_request_id or temporal_session_id,
                card_id=task.card_id or "",
                files=changed_files,
                tool_sequence=tool_sequence,
                stopped_reason=last_stopped_reason,
                error_text=result.error,
            )
            if tool_sequence:
                gate.observe_tool_workflow(
                    intent_text=task.objective,
                    tool_sequence=tool_sequence,
                    success=result.success,
                    observation_id=f"tool:{last_request_id or temporal_session_id}:{task.card_id or task.task_id or task.objective[:80]}",
                    session_id=last_request_id or temporal_session_id,
                    card_id=task.card_id or "",
                    summary_text=result.summary or result.error,
                    files=changed_files,
                    metadata={
                        "stopped_reason": last_stopped_reason,
                        "tool_calls": int(tool_stats.get("total") or 0),
                        "tool_success": int(tool_stats.get("success") or 0),
                        "tool_failed": int(tool_stats.get("failed") or 0),
                        "tool_timeout": int(tool_stats.get("timeout") or 0),
                    },
                )
        except Exception as exc:
            logger.debug("[worker] skill promotion observe skipped: %s", exc)

        result.elapsed_s = time.monotonic() - started
        return result

    # ── Envelope construction ───────────────────────────────────

    def context_stats(self) -> Dict[str, Any]:
        return self._context_compressor.stats()

    def _build_envelope(
        self,
        task: CodingTask,
        *,
        cwd_abs: str,
        agents_md: str,
        project_block: str,
        skill_index: SkillIndex,
        attempt: int,
        tool_ledger_session: str,
        attempt_notes: List[str],
    ) -> ContextEnvelope:
        file_contents = self._read_priority_files(cwd_abs, task.files)
        compiler = CodingContextCompiler(
            data_dir=self._data_dir,
            registry=self._registry,
            wiki_manager=self._wiki_manager,
        )
        compiled = compiler.compile(
            task,
            cwd_abs=cwd_abs,
            agents_md=agents_md,
            project_block=project_block,
            skill_index=skill_index,
            priority_files=file_contents,
            attempt=attempt,
            tool_ledger_session=tool_ledger_session,
            attempt_notes=attempt_notes,
            greenfield=self._is_greenfield(cwd_abs, task),
            max_attempts=self._max_retries + 1,
        )
        return compiled.envelope

    def _build_skill_index(self, task: CodingTask, cwd_abs: str, available_tools: List[str]) -> SkillIndex:
        project_skills = Path(cwd_abs) / ".skills"
        adaptive = synthesize_adaptive_skills(
            data_root=self._data_dir,
            project_id=task.project_id or "default",
            cwd=cwd_abs,
            query="\n".join(part for part in (task.objective, task.context, " ".join(task.files or [])) if part),
            session_id=task.card_id or "coding",
            available_tools=available_tools,
        )
        return build_skill_index(
            global_dir=self._data_dir / "skills",
            project_dirs=[project_skills, Path(adaptive["dir"]), promoted_skill_dir(self._data_dir, task.project_id or "default")],
        )

    async def _run_loop(self, provider, tool_registry: ToolRegistry, task: CodingTask, env: ContextEnvelope, attempt: int, skill_index: SkillIndex) -> Dict[str, Any]:
        system_prompt = get_prompts().get("coder.system")
        # Tell the OpenAI provider to enable prompt-cache stable-prefix hinting +
        # carry forward the previous_response_id for the same card across retries.
        cache_key = task.card_id or task.task_id or "sync"
        prev_id = self._previous_response_ids.get(cache_key)
        request_id = f"{task.card_id or task.task_id or 'sync'}_a{attempt + 1}"

        async def _complete(messages, **kwargs):
            return await call_and_record_provider(
                provider,
                role="coder",
                session_id=request_id,
                call=lambda: provider.complete(messages, **kwargs),
                record_health=False,
            )

        loop_provider = SimpleNamespace(
            complete=_complete,
            id=str(getattr(provider, "id", "") or ""),
            model=str(getattr(provider, "model", "") or ""),
        )
        loop = ReActLoop(loop_provider, tool_registry, max_iterations=self._max_iter, system_prompt=system_prompt)
        loop._cache_stable_prefix = True  # noqa: SLF001 — runtime hint
        loop._context_compressor = self._context_compressor  # noqa: SLF001 — shared rot-aware compressor
        if prev_id:
            loop._previous_response_id = prev_id  # noqa: SLF001
        rendered = env.render(self._allocator, compressor=self._context_compressor)
        # Per-step mirror callback writes Thought/Action/Observation into the card workspace
        # so persisted scratchpad/notes/tool_runs reflect the live run.
        callback = self._make_step_callback(task, attempt=attempt, request_id=request_id) if task.card_id else None
        result = await loop.run(
            task.objective,
            extra_context=rendered,
            tool_context={
                "workspace_root": task.cwd,
                "execution_root": task.cwd,
                "skill_index": skill_index,
                "project_id": task.project_id or "default",
                "card_id": task.card_id or "",
                "task_id": task.task_id or "",
                "intent_text": task.objective,
                "target_files": list(task.files or [])[:8],
            },
            request_id=request_id,
            step_callback=callback,
        )
        # Stash the last provider response id for the next retry on this card.
        if loop.last_response_id:
            self._previous_response_ids[cache_key] = loop.last_response_id
        # Final scratchpad write + fold on attempt end.
        if task.card_id:
            project_id = task.project_id or "default"
            try:
                cw.save_working_scratchpad(self._data_dir, project_id, task.card_id, result.scratchpad.to_text())
                if result.stopped_reason != "final_answer":
                    cw.append_folded_scratchpad(self._data_dir, project_id, task.card_id, result.scratchpad.fold_to_summary())
            except Exception as exc:
                logger.debug("[worker] scratchpad persist failed: %s", exc)
        return {
            "content": result.content,
            "stopped_reason": result.stopped_reason,
            "tool_calls": result.tool_calls,
            "iterations": result.iterations,
            "scratchpad_text": result.scratchpad.to_text(),
            "folded": result.scratchpad.fold_to_summary(),
            "request_id": request_id,
            "_skip_health_accounting": True,
        }

    def _make_step_callback(self, task: CodingTask, *, attempt: int, request_id: str):
        project_id = task.project_id or "default"
        card_id = task.card_id
        job_id = str(task.task_id or "").strip()
        cwd = str(task.cwd or "").strip()
        planned_files = list(task.files or [])[:24]

        async def _cb(event: Dict[str, Any]) -> None:
            kind = event.get("kind", "")
            try:
                if kind in ("thought", "final", "error"):
                    cw.append_note(self._data_dir, project_id, card_id, {
                        "kind": kind,
                        "attempt": attempt + 1,
                        "request_id": request_id,
                        "content": (event.get("content") or "")[:1200],
                    })
                elif kind == "action":
                    cw.append_tool_run(self._data_dir, project_id, card_id, {
                        "phase": "started",
                        "attempt": attempt + 1,
                        "request_id": request_id,
                        "tool": event.get("tool", ""),
                        "args_preview": str(event.get("args", ""))[:200],
                    })
                elif kind == "observation":
                    cw.append_tool_run(self._data_dir, project_id, card_id, {
                        "phase": "finished",
                        "attempt": attempt + 1,
                        "request_id": request_id,
                        "tool": event.get("tool", ""),
                        "success": bool(event.get("success", False)),
                        "duration_ms": event.get("duration_ms", 0),
                        "output_preview": sanitize_text(event.get("output") or "", max_chars=200),
                        "error_preview": sanitize_text(event.get("error") or "", max_chars=200),
                    })
                if kind in ("action", "observation", "thought"):
                    note = self._progress_note_for_event(kind, event)
                    current_file = self._current_file_for_event(event, planned_files=planned_files)
                    preview_text = self._preview_text_for_event(kind, event)
                    if note or current_file:
                        await get_bus().emit("coding:job.progress", {
                            "job_id": job_id,
                            "project_id": project_id,
                            "card_id": card_id,
                            "cwd": cwd,
                            "request_id": request_id,
                            "attempt": attempt + 1,
                            "kind": kind,
                            "note": note,
                            "current_file": current_file,
                            "preview_text": preview_text,
                            "files": planned_files,
                        })
            except Exception as exc:
                logger.debug("[worker] step mirror failed (%s): %s", kind, exc)

        return _cb

    @staticmethod
    def _progress_note_for_event(kind: str, event: Dict[str, Any]) -> str:
        if kind == "action":
            tool = str(event.get("tool") or "").strip()
            args = sanitize_text(str(event.get("args") or ""), max_chars=160)
            if tool and args:
                return f"{tool} {args}"[:220]
            if tool:
                return f"{tool} started"[:220]
        if kind == "observation":
            tool = str(event.get("tool") or "").strip()
            if tool:
                return (f"{tool} finished" if bool(event.get("success", False)) else f"{tool} failed")[:220]
        if kind == "thought":
            return sanitize_text(str(event.get("content") or ""), max_chars=220)
        return ""

    @staticmethod
    def _preview_text_for_event(kind: str, event: Dict[str, Any]) -> str:
        if kind != "action":
            return ""
        tool = str(event.get("tool") or "").strip().lower()
        args = event.get("args")
        if not isinstance(args, dict):
            return ""
        for key in ("content", "new_source", "old_string", "new_string"):
            value = str(args.get(key) or "").strip()
            if value:
                return sanitize_text(value, max_chars=280)
        if tool == "apply_patch":
            value = str(args.get("patch") or args.get("input") or "").strip()
            if value:
                return sanitize_text(value, max_chars=280)
        return ""

    @classmethod
    def _current_file_for_event(cls, event: Dict[str, Any], *, planned_files: List[str]) -> str:
        candidates: List[str] = []
        args = event.get("args")
        if isinstance(args, dict):
            for key in ("path", "file", "target", "target_file", "source", "destination"):
                value = str(args.get(key) or "").strip()
                if value:
                    candidates.append(value)
        for value in (event.get("output"), event.get("content"), event.get("error")):
            text = str(value or "")
            if not text:
                continue
            match = cls._PROGRESS_PATH_RE.search(text)
            if match:
                candidates.append(match.group(0))
        for candidate in candidates:
            normalized = str(candidate or "").strip().replace("\\", "/")
            if normalized:
                return normalized
        return str((planned_files or [""])[0] or "")

    def _tool_sequence_for_session(self, session_id: str) -> List[str]:
        if not session_id:
            return []
        sequence: List[str] = []
        for entry in get_tool_ledger().entries(session_id):
            if str(getattr(getattr(entry, "status", ""), "value", getattr(entry, "status", ""))) != "success":
                continue
            tool_name = str(getattr(entry, "tool_name", "") or "").strip()
            if tool_name:
                sequence.append(tool_name)
        return sequence[:24]

    # ── Snapshot + diff helpers ─────────────────────────────────

    def _snapshot(self, cwd: str) -> Dict[str, str]:
        root = Path(cwd).resolve()
        out: Dict[str, str] = {}
        if not root.exists():
            return out
        for current, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in _SNAPSHOT_SKIP_DIRS]
            for name in files:
                if Path(name).suffix.lower() in _SNAPSHOT_SKIP_SUFFIXES:
                    continue
                path = Path(current) / name
                try:
                    if path.stat().st_size > _SNAPSHOT_MAX_FILE_BYTES:
                        continue
                    rel = str(path.relative_to(root)).replace("\\", "/")
                    out[rel] = path.read_text(encoding="utf-8")
                except Exception:
                    continue
                if len(out) >= _SNAPSHOT_MAX_FILES:
                    return out
        return out

    def _read_priority_files(self, cwd: str, files: List[str]) -> Dict[str, str]:
        out: Dict[str, str] = {}
        root = Path(cwd).resolve()
        for name in (files or [])[:_MAX_FILES_INJECT]:
            try:
                path = (root / name).resolve()
                path.relative_to(root)  # ensure inside workspace
                if path.exists() and path.is_file():
                    out[name] = path.read_text(encoding="utf-8", errors="replace")[:_FILE_READ_LIMIT]
                else:
                    out[name] = ""  # signal: does not exist (greenfield hint)
            except Exception:
                continue
        return out

    def _build_result(
        self,
        task: CodingTask,
        *,
        before: Dict[str, str],
        after: Dict[str, str],
        response: Dict[str, Any],
        started_at: float,
    ) -> CodingResult:
        patches: List[FilePatch] = []
        new_files: List[NewFile] = []
        warnings: List[str] = []
        for rel, content in after.items():
            if rel not in before:
                new_files.append(NewFile(file=rel, content=content, description="created by coder"))
            elif before[rel] != content:
                patches.append(FilePatch(file=rel, old_string=before[rel], new_string=content, description="updated by coder"))
        for rel in before:
            if rel not in after:
                warnings.append(f"File deleted during run: {rel}")

        modified_files = [patch.file for patch in patches] + [new_file.file for new_file in new_files]
        guard = ScopeGuard(task.cwd)
        scope_result = guard.check_file_scope(modified_files, list(task.files or []), allow_new_files=True)
        for violation in scope_result.violations:
            warnings.append(f"Scope warning: {violation.description}")
        for patch in patches:
            import_result = guard.check_import_safety(patch.file, patch.old_string, patch.new_string)
            for violation in import_result.violations:
                warnings.append(f"Scope warning: {violation.description}")
        for new_file in new_files:
            import_result = guard.check_import_safety(new_file.file, "", new_file.content)
            for violation in import_result.violations:
                warnings.append(f"Scope warning: {violation.description}")

        stopped_reason = str(response.get("stopped_reason") or "")
        success = bool(patches or new_files)
        summary = str(response.get("content") or "")[:4000]
        checkpoint = self._extract_checkpoint(summary)
        if checkpoint:
            warnings.append(_CHECKPOINT_PREFIX + json.dumps(checkpoint, separators=(",", ":")))
        error = ""
        if not success:
            if checkpoint:
                error = f"User input required: {checkpoint.get('reason') or 'checkpoint questions pending'}"
            elif summary.lower().startswith("react run failed:") or "http 429" in summary.lower():
                error = summary
            else:
                error = "Coder produced no file changes."
            if stopped_reason == "max_iterations":
                error += " (max iterations reached)"
            elif stopped_reason == "stagnation_detected":
                error += " (stagnation detected)"
        return CodingResult(
            success=success,
            plan=[task.objective],
            patches=patches,
            new_files=new_files,
            summary=summary,
            test_hint=(task.verification[0] if task.verification else ""),
            warnings=warnings,
            elapsed_s=time.monotonic() - started_at,
            error=error,
        )

    @staticmethod
    def _extract_checkpoint(text: str) -> Dict[str, Any]:
        match = re.search(r"```checkpoint\s*(\{.*?\})\s*```", str(text or ""), re.DOTALL | re.IGNORECASE)
        if not match:
            return {}
        payload, parse_error = parse_json_payload(match.group(1))
        if parse_error:
            return {}
        payload, validation_error = validate_payload(payload, _CHECKPOINT_SCHEMA, path="checkpoint")
        if validation_error or not isinstance(payload, dict):
            return {}
        questions = []
        for idx, question in enumerate(payload.get("questions") or []):
            if not isinstance(question, dict):
                continue
            options = []
            for opt_idx, option in enumerate(question.get("options") or []):
                if not isinstance(option, dict):
                    continue
                label = str(option.get("label") or option.get("value") or "").strip()
                if not label:
                    continue
                options.append({
                    "id": str(option.get("id") or f"o{opt_idx + 1}"),
                    "label": label[:160],
                    "value": str(option.get("value") or label)[:500],
                })
            if len(options) < 2:
                continue
            questions.append({
                "id": str(question.get("id") or f"q{idx + 1}"),
                "prompt": str(question.get("prompt") or "").strip()[:500],
                "options": options[:4],
            })
        if not questions:
            return {}
        return {
            "reason": str(payload.get("reason") or "User input required").strip()[:500],
            "questions": questions[:4],
        }

    @staticmethod
    def _is_greenfield(cwd: str, task: CodingTask) -> bool:
        root = Path(cwd).resolve()
        if task.files:
            for name in task.files[:6]:
                if (root / name).exists():
                    return False
            return True
        try:
            visible = [p for p in root.iterdir() if p.name not in {".git", ".worktrees", "__pycache__", ".pytest_cache"}]
            return len(visible) == 0
        except Exception:
            return False

    @staticmethod
    def _note_for_retry(result: CodingResult, stopped_reason: str) -> str:
        if stopped_reason == "stagnation_detected":
            return "Previous attempt stagnated in exploration. Move to a concrete edit immediately."
        if stopped_reason == "max_iterations":
            return "Previous attempt hit max iterations. Reduce inspection, mutate the priority file first."
        if not result.has_file_changes:
            return "Previous attempt produced no file changes. Write or patch a file this turn."
        if result.error:
            return f"Previous attempt failed: {result.error[:160]}"
        return "Previous attempt did not succeed. Adjust approach and try again."

    def _record_attempt(
        self,
        task: CodingTask,
        attempt: int,
        *,
        success: bool,
        error: str = "",
        stopped_reason: str = "",
        files_changed: Optional[List[str]] = None,
    ) -> None:
        if not task.card_id:
            return
        project_id = task.project_id or "default"
        payload = {
            "attempt": attempt + 1,
            "success": success,
            "stopped_reason": stopped_reason,
            "files_changed": files_changed or [],
            "error": error,
        }
        cw.append_history(self._data_dir, project_id, task.card_id, payload)
        if success:
            cw.append_milestone(self._data_dir, project_id, task.card_id, payload)
        elif error:
            cw.append_blocker(self._data_dir, project_id, task.card_id, payload)


_worker: Optional[CodingWorker] = None


def init_coding_worker(
    registry: ProviderRegistry,
    *,
    data_dir: Path,
    max_iterations: int = 12,
    max_retries: int = 1,
    wiki_manager=None,
) -> CodingWorker:
    global _worker
    _worker = CodingWorker(
        registry,
        data_dir=data_dir,
        max_iterations=max_iterations,
        max_retries=max_retries,
        wiki_manager=wiki_manager,
    )
    return _worker


def get_coding_worker() -> Optional[CodingWorker]:
    return _worker
