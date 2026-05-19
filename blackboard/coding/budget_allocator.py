"""Context budget allocator.

Percentage-based per-section char caps with two-phase redistribution + reduction notices.
Slim port of luna/executive/context_compiler.py:ContextBudgetAllocator.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Tuple


_DEFAULT_BUDGET_CHARS = 200_000
_DEFAULT_ALLOCATIONS: Dict[str, int] = {
    "role_meta": 1,
    "objective": 1,
    "agents_md": 6,
    "project_intel": 6,
    "card_memory": 4,
    "task_context": 8,
    "skills": 4,
    "wiki_context": 5,
    "priority_files": 10,
    "verification": 1,
    "constraints": 1,
    "prior_feedback": 3,
    "retry_requirements": 1,
    "tool_status": 1,
    "context_health": 1,
    "messages": 42,
    "buffer": 5,
}


@dataclass
class AllocationResult:
    sections: Dict[str, str] = field(default_factory=dict)
    reduced: List[str] = field(default_factory=list)
    total_chars: int = 0


class ContextBudgetAllocator:
    """Distribute a global char budget across named sections; truncate over-budget ones."""

    def __init__(
        self,
        total_budget: int = _DEFAULT_BUDGET_CHARS,
        allocations: Dict[str, int] | None = None,
    ) -> None:
        self._total = int(total_budget)
        self._pct = dict(allocations or _DEFAULT_ALLOCATIONS)

    def per_section_caps(self) -> Dict[str, int]:
        return {key: max(0, int(self._total * pct / 100)) for key, pct in self._pct.items()}

    def allocate(self, sections: Dict[str, str]) -> AllocationResult:
        caps = self.per_section_caps()
        result = AllocationResult()
        # Phase 1: per-section truncation.
        phase1: Dict[str, Tuple[str, int]] = {}
        for name, text in sections.items():
            cap = caps.get(name, len(text or ""))
            content = text or ""
            if len(content) > cap and cap > 0:
                trimmed = content[: max(0, cap - 80)]
                trimmed += f"\n\n<context_reduction_notice section=\"{name}\">truncated to {cap} chars</context_reduction_notice>"
                result.reduced.append(name)
                phase1[name] = (trimmed, len(trimmed))
            else:
                phase1[name] = (content, len(content))
        # Phase 2: redistribute surplus from under-used sections (best-effort, simple)
        used = sum(length for _, length in phase1.values())
        if used > self._total:
            # Over budget — shrink residual sections proportionally.
            overshoot = used - self._total
            shrinkable = sorted(
                [(name, length) for name, (_, length) in phase1.items() if name in {"messages", "priority_files"}],
                key=lambda item: -item[1],
            )
            for name, length in shrinkable:
                if overshoot <= 0:
                    break
                cut = min(overshoot, max(0, length - 200))
                if cut <= 0:
                    continue
                text, _ = phase1[name]
                trimmed = text[: max(0, length - cut)]
                trimmed += f"\n\n<context_reduction_notice section=\"{name}\">overflow-trimmed -{cut} chars</context_reduction_notice>"
                phase1[name] = (trimmed, len(trimmed))
                if name not in result.reduced:
                    result.reduced.append(name)
                overshoot -= cut
        result.sections = {name: text for name, (text, _) in phase1.items()}
        result.total_chars = sum(length for _, length in phase1.values())
        return result
