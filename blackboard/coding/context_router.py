"""Task-aware context routing for Blackboard coding runs."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Set


@dataclass(frozen=True)
class ContextProfile:
    name: str
    sections_to_load: Set[str]
    sections_to_skip: Set[str]


_DEFAULT_SECTIONS = {
    "role_meta",
    "objective",
    "agents_md",
    "project_intel",
    "card_memory",
    "project_mailbox",
    "temporal_scratchpad",
    "task_context",
    "skills",
    "wiki_context",
    "priority_files",
    "verification",
    "constraints",
    "prior_feedback",
    "retry_requirements",
    "tool_status",
    "context_health",
    "greenfield_mode",
}


class ContextRouter:
    def route(self, *, objective: str = "", task_context: str = "", files: list[str] | None = None, greenfield: bool = False) -> ContextProfile:
        text = f"{objective}\n{task_context}\n{' '.join(files or [])}".lower()
        if greenfield:
            return ContextProfile("greenfield_project", set(_DEFAULT_SECTIONS), set())
        if any(term in text for term in ("provider", "openai", "anthropic", "fireworks", "api key", "fallback")):
            return ContextProfile("provider_work", set(_DEFAULT_SECTIONS), set())
        if any(term in text for term in ("ui", "frontend", "settings", "button", "css", "javascript")):
            return ContextProfile("ui_work", set(_DEFAULT_SECTIONS), set())
        if any(term in text for term in ("readme", "docs", "documentation", "agents.md")):
            return ContextProfile("docs_only", set(_DEFAULT_SECTIONS), {"tool_status"})
        if any(term in text for term in ("bug", "fix", "traceback", "failing", "error", "debug")):
            return ContextProfile("bugfix", set(_DEFAULT_SECTIONS), set())
        if task_context.strip() and not files:
            return ContextProfile("unknown_project", set(_DEFAULT_SECTIONS), set())
        return ContextProfile("general_coding", set(_DEFAULT_SECTIONS), set())


def filter_sections(profile: ContextProfile, sections: dict[str, str]) -> dict[str, str]:
    return {
        name: content
        for name, content in sections.items()
        if name in profile.sections_to_load and name not in profile.sections_to_skip
    }
