"""Context envelope — XML-tag-delimited sections with markdown bodies.

Per §11a.2 and §11b of the plan. Output goes into the coder provider's system message.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from blackboard.coding.budget_allocator import ContextBudgetAllocator
from blackboard.coding.context_rot import ContextCompressor


# Stable-prefix sections come first (for prompt-cache hits); volatile-suffix last.
_STABLE_PREFIX = ["role_meta", "objective", "agents_md", "project_intel"]
_VOLATILE_SUFFIX = [
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
]


def analyze_context_sections(sections: Dict[str, str]) -> List[Dict[str, object]]:
    reports: List[Dict[str, object]] = []
    seen: Dict[str, str] = {}
    for name, text in sections.items():
        content = str(text or "")
        if len(content) > 4000:
            reports.append({"section": name, "issue": "large", "chars": len(content)})
        fingerprint = " ".join(content.lower().split())[:300]
        if fingerprint:
            if fingerprint in seen:
                reports.append({"section": name, "issue": "redundant", "similar_to": seen[fingerprint]})
            else:
                seen[fingerprint] = name
    return reports


def build_context_health_block(sections: Dict[str, str]) -> str:
    reports = analyze_context_sections(sections)
    if not reports:
        return "<context_health>\nhealthy\n</context_health>"
    lines = ["<context_health>"]
    for report in reports[:12]:
        issue = str(report.get("issue") or "")
        section = str(report.get("section") or "")
        if issue == "large":
            lines.append(f"- {section}: large section ({report.get('chars')} chars)")
        elif issue == "redundant":
            lines.append(f"- {section}: redundant with {report.get('similar_to')}")
        else:
            lines.append(f"- {section}: {issue}")
    lines.append("</context_health>")
    return "\n".join(lines)


@dataclass
class ContextEnvelope:
    role_meta: str = ""
    objective: str = ""
    agents_md: str = ""
    project_intel: str = ""
    card_memory: str = ""
    project_mailbox: str = ""
    temporal_scratchpad: str = ""
    task_context: str = ""
    skills: str = ""
    wiki_context: str = ""
    priority_files: str = ""
    verification: str = ""
    constraints: str = ""
    prior_feedback: str = ""
    retry_requirements: str = ""
    tool_status: str = ""
    context_health: str = ""
    greenfield_mode: str = ""

    def to_sections(self) -> Dict[str, str]:
        return {
            "role_meta": self.role_meta,
            "objective": self.objective,
            "agents_md": self.agents_md,
            "project_intel": self.project_intel,
            "card_memory": self.card_memory,
            "project_mailbox": self.project_mailbox,
            "temporal_scratchpad": self.temporal_scratchpad,
            "task_context": self.task_context,
            "skills": self.skills,
            "wiki_context": self.wiki_context,
            "priority_files": self.priority_files,
            "verification": self.verification,
            "constraints": self.constraints,
            "prior_feedback": self.prior_feedback,
            "retry_requirements": self.retry_requirements,
            "tool_status": self.tool_status,
            "context_health": self.context_health,
            "greenfield_mode": self.greenfield_mode,
        }

    def render(self, allocator: Optional[ContextBudgetAllocator] = None, compressor: Optional[ContextCompressor] = None) -> str:
        sections = self.to_sections()
        if compressor is not None:
            sections = compressor.compress_sections(sections)
        if allocator is not None:
            result = allocator.allocate(sections)
            sections = result.sections
        parts: List[str] = []
        for name in _STABLE_PREFIX + _VOLATILE_SUFFIX:
            text = (sections.get(name) or "").strip()
            if not text:
                continue
            if not text.startswith("<"):
                # Wrap with the matching XML tag if the caller did not.
                parts.append(f"<{name}>\n{text}\n</{name}>")
            else:
                parts.append(text)
        return "\n\n".join(parts)


# ── Section builders (markdown bodies, XML wrapping) ────────────


def role_meta_block(*, role: str, provider: str, model: str, attempt: int, max_attempts: int) -> str:
    return f"<role_meta>role={role} provider={provider} model={model} attempt={attempt}/{max_attempts}</role_meta>"


def objective_block(text: str) -> str:
    return f"<objective>\n{(text or '').strip()}\n</objective>"


def agents_md_block(content: str, *, cap: int = 12000) -> str:
    if not content:
        return ""
    body = content[:cap]
    return f"<agents_md>\n{body}\n</agents_md>"


def task_context_block(content: str, *, cap: int = 12000) -> str:
    if not content:
        return ""
    body = content[:cap]
    return f"<task_context>\n{body}\n</task_context>"


def skills_block(content: str, *, cap: int = 6000) -> str:
    if not content:
        return ""
    body = content[:cap]
    return body if body.lstrip().startswith("<skills>") else f"<skills>\n{body}\n</skills>"


def wiki_context_block(content: str, *, cap: int = 10000) -> str:
    if not content:
        return ""
    body = content[:cap]
    return body if body.lstrip().startswith("<wiki_context>") else f"<wiki_context>\n{body}\n</wiki_context>"


def priority_files_block(files: Dict[str, str], *, per_file_cap: int = 6000, max_files: int = 8) -> str:
    if not files:
        return ""
    lines = ["<priority_files>"]
    for idx, (path, content) in enumerate(files.items()):
        if idx >= max_files:
            break
        body = (content or "")[:per_file_cap]
        if len(content or "") > per_file_cap:
            body += f"\n... [truncated, +{len(content) - per_file_cap} chars]"
        lines.append(f'<file path="{path}">')
        lines.append(body)
        lines.append("</file>")
    lines.append("</priority_files>")
    return "\n".join(lines)


def verification_block(items: List[str]) -> str:
    if not items:
        return ""
    body = "\n".join(f"- {item}" for item in items)
    return f"<verification>\n{body}\n</verification>"


def constraints_block(items: List[str]) -> str:
    if not items:
        return ""
    body = "\n".join(f"- {item}" for item in items)
    return f"<constraints>\n{body}\n</constraints>"


def retry_requirements_block(attempt: int, priority_target: str) -> str:
    if attempt <= 0:
        return ""
    body = (
        f"This is retry attempt {attempt + 1}. The prior run did not produce verified file changes.\n"
        "Do not spend the whole run re-exploring the same directories.\n"
        f"Make at least one concrete edit to {priority_target} unless inspection proves another file is required.\n"
        "If a target file is missing, create it instead of stopping after discovery."
    )
    return f"<retry_requirements>\n{body}\n</retry_requirements>"


def greenfield_mode_block(enabled: bool) -> str:
    if not enabled:
        return ""
    body = (
        "This appears to be an empty or near-empty project area.\n"
        "Create the target scaffold immediately; do not loop on directory listings.\n"
        "Within the first meaningful mutation, create at least one root or priority file."
    )
    return f"<greenfield_mode>\n{body}\n</greenfield_mode>"
