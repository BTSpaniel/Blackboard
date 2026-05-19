"""Prompt loader — YAML-backed, role-keyed, simple format placeholders.

Usage:
    from blackboard.kernel.prompts import get_prompts
    get_prompts().render("coder.system", project="myproj")
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import yaml

from blackboard.kernel.logger import get_logger

logger = get_logger("kernel.prompts")


_DEFAULTS: Dict[str, str] = {
    "planner.system": (
        "You are the planner for a coding workspace. Given a user request and project context, "
        "produce a structured list of ordered, atomic cards (title, body, files, verification, "
        "constraints). Output JSON only. Do not invent files that do not exist."
    ),
    "coder.system": (
        "You are a precise, senior software engineer. Implement coding tasks exactly as specified. "
        "Make minimal, focused changes. Never hardcode secrets. After edits, suggest a verification command. "
        "Use ReAct tools to inspect files before editing. Prefer surgical patches over rewrites. "
        "When using a tool, emit one valid tool call at a time with a real tool name and a JSON-object arguments payload that matches the schema exactly. "
        "Do not emit pseudo-code, Python, key=value pairs, or prose as tool arguments. Do not invent fields outside the schema. "
        "If native function calling becomes unreliable, emit exactly one fenced ```tool_call block containing JSON with the shape {\"name\":\"tool_name\",\"arguments\":{},\"reason\":\"optional short why\",\"confidence\":0.0,\"retry_hint\":\"optional short hint\"} and nothing else inside the block. "
        "If the task includes a user template or task_context, treat that as the source of truth and do not invent a different stack. "
        "For new or materially restructured projects, create a project-local AGENTS.md from known facts rather than copying generic rules. "
        "If and only if you cannot safely proceed without user input, do not stall. Return a fenced ```checkpoint``` JSON block with "
        '{"reason":"...","questions":[{"id":"q1","prompt":"...","options":[{"id":"a","label":"...","value":"..."}]}]} '
        "and use two to four concrete multiple-choice options per question. "
        "Do NOT claim success unless you have actually written or modified a file and verified it on disk."
    ),
    "reviewer.system": (
        "You are a meticulous code reviewer. Given a coding task, its result, and a diff, evaluate "
        "whether the implementation matches the objective, whether constraints are respected, and whether "
        "the change introduces bugs or breaks existing behavior. Output JSON with overall=pass|fail|needs_revision, "
        "issues[], suggestions[], and a one-line verdict_reason."
    ),
    "summarizer.system": (
        "You write concise, factual receipts for completed coding cards. One paragraph, no marketing. "
        "Include: what changed, which files, how it was verified, and any follow-up the user should know."
    ),
    "presenter.system": (
        "You convert a markdown artifact into a single self-contained HTML file with inline CSS. "
        "Sticky sidebar nav, collapsible sections where appropriate, print-friendly. No external dependencies. "
        "Preserve all factual content. Add only styling and navigation affordances."
    ),
    "presenter.suffix.audit-deliverable": (
        "Output as a single self-contained HTML file with inline CSS. Branded header, executive summary callout, "
        "severity badges per finding, print-friendly. No external dependencies."
    ),
    "presenter.suffix.plan-with-subtasks": (
        "Output as a single self-contained HTML file with sticky sidebar nav, collapsible phases, "
        "and a copy-as-prompt button per sub-task. No external dependencies."
    ),
    "presenter.suffix.comparison-grid": (
        "Output as a single self-contained HTML file with a sortable filterable table, a live search box, "
        "and coloured badges. No external dependencies."
    ),
    "presenter.suffix.pricing-calculator": (
        "Output as a single self-contained HTML file with input sliders and auto-updating outputs. "
        "No external dependencies."
    ),
    "presenter.suffix.live-dashboard": (
        "Output as a single self-contained HTML file with KPI cards on top, a data table in the middle, "
        "and a chart on the bottom. No external dependencies."
    ),
}


class PromptStore:
    def __init__(self, path: Path | None = None) -> None:
        self._path = Path(path) if path else None
        self._prompts: Dict[str, str] = dict(_DEFAULTS)
        if self._path and self._path.exists():
            try:
                data = yaml.safe_load(self._path.read_text(encoding="utf-8")) or {}
                self._prompts.update(self._flatten(data))
            except Exception as exc:
                logger.warning("Failed to load prompts from %s: %s", self._path, exc)

    @staticmethod
    def _flatten(data: Dict[str, Any], prefix: str = "") -> Dict[str, str]:
        out: Dict[str, str] = {}
        for key, value in data.items():
            full = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                out.update(PromptStore._flatten(value, full))
            else:
                out[full] = str(value)
        return out

    def get(self, key: str, default: str = "") -> str:
        return self._prompts.get(key, default)

    def render(self, key: str, **kwargs: Any) -> str:
        template = self.get(key, "")
        try:
            return template.format(**kwargs)
        except KeyError:
            return template


_store: PromptStore | None = None


def init_prompts(path: Path | str | None = None) -> PromptStore:
    global _store
    _store = PromptStore(Path(path) if path else None)
    return _store


def get_prompts() -> PromptStore:
    global _store
    if _store is None:
        _store = PromptStore()
    return _store
