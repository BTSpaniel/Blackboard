"""Blackboard coding context compiler."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

from blackboard.coding.context_health import build_context_health_block
from blackboard.coding.context_envelope import (
    ContextEnvelope,
    agents_md_block,
    constraints_block,
    greenfield_mode_block,
    objective_block,
    priority_files_block,
    retry_requirements_block,
    role_meta_block,
    skills_block,
    task_context_block,
    verification_block,
    wiki_context_block,
)
from blackboard.coding.context_router import ContextProfile, ContextRouter, filter_sections
from blackboard.coding.mailbox_context import build_project_mailbox_block
from blackboard.coding.models import CodingTask
from blackboard.coding.skills import SkillIndex
from blackboard.workspace import card_workspace as cw
from blackboard.workspace.temporal_scratchpad import coding_temporal_context_block
from blackboard.workspace.tool_ledger import get_tool_ledger


@dataclass
class CompiledContext:
    envelope: ContextEnvelope
    profile: ContextProfile


class CodingContextCompiler:
    def __init__(self, *, data_dir, registry, router: ContextRouter | None = None, wiki_manager=None) -> None:
        self._data_dir = data_dir
        self._registry = registry
        self._router = router or ContextRouter()
        self._wiki_manager = wiki_manager

    def compile(
        self,
        task: CodingTask,
        *,
        cwd_abs: str,
        agents_md: str,
        project_block: str,
        skill_index: SkillIndex,
        priority_files: Dict[str, str],
        attempt: int,
        tool_ledger_session: str,
        attempt_notes: List[str],
        greenfield: bool,
        max_attempts: int = 1,
    ) -> CompiledContext:
        profile = self._router.route(
            objective=task.objective,
            task_context=task.context,
            files=task.files,
            greenfield=greenfield,
        )
        coder_profile = self._registry.role_profile_id("coder")
        provider = self._registry.provider(coder_profile)
        sections = {
            "role_meta": role_meta_block(
                role="coder",
                provider=coder_profile,
                model=provider.model if provider else "?",
                attempt=attempt + 1,
                max_attempts=max_attempts,
            ),
            "objective": objective_block(task.objective),
            "agents_md": agents_md_block(agents_md),
            "project_intel": project_block,
            "card_memory": cw.build_card_memory_block(self._data_dir, task.project_id or "default", task.card_id or "sync") if task.card_id else "",
            "project_mailbox": build_project_mailbox_block(
                self._data_dir,
                task.project_id or "default",
                query="\n".join(part for part in (task.objective, task.context, " ".join(task.files or [])) if part),
                card_id=task.card_id or "",
            ),
            "temporal_scratchpad": coding_temporal_context_block(
                self._data_dir,
                task.project_id or "default",
                card_id=task.card_id or "",
                task_id=task.task_id or "",
                cwd=cwd_abs,
            ),
            "task_context": task_context_block(task.context),
            "skills": skills_block(skill_index.as_context_block()),
            "wiki_context": wiki_context_block(self._wiki_context(task)),
            "priority_files": priority_files_block(priority_files),
            "verification": verification_block(task.verification),
            "constraints": constraints_block(task.constraints),
            "prior_feedback": cw.build_prior_feedback_block(self._data_dir, task.project_id or "default", task.card_id or "sync") if task.card_id else "",
            "retry_requirements": retry_requirements_block(attempt, task.files[0] if task.files else "the highest priority relevant file"),
            "tool_status": get_tool_ledger().build_context_block(tool_ledger_session),
            "greenfield_mode": greenfield_mode_block(greenfield),
        }
        sections["context_health"] = build_context_health_block(sections)
        sections = filter_sections(profile, sections)
        envelope = ContextEnvelope(**sections)
        if attempt_notes:
            tail = "\n".join(f"- {note}" for note in attempt_notes[-3:])
            envelope.retry_requirements = f"{envelope.retry_requirements}\n<previous_attempt_notes>\n{tail}\n</previous_attempt_notes>"
        return CompiledContext(envelope=envelope, profile=profile)

    def _wiki_context(self, task: CodingTask) -> str:
        if self._wiki_manager is None:
            return ""
        query = "\n".join(part for part in (task.objective, task.context, " ".join(task.files or [])) if part)
        if not query.strip():
            return ""
        try:
            return self._wiki_manager.context_block(query, max_results=3)
        except Exception:
            return ""
