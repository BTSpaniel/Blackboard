from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple


_CHAT_CONTEXT_BUDGET_CHARS = 24_000
_CHAT_SECTION_BUDGETS: Dict[str, int] = {
    "project_intelligence": 8_000,
    "conversation_state": 1_500,
    "session_scratchboard": 4_000,
    "temporal_scratchpad": 4_000,
    "adaptive_skills": 4_000,
    "relevant_session_memory": 5_000,
    "recent_session_history": 5_000,
}
_CHAT_SECTION_ORDER = [
    "project_intelligence",
    "conversation_state",
    "session_scratchboard",
    "temporal_scratchpad",
    "adaptive_skills",
    "relevant_session_memory",
    "recent_session_history",
]


@dataclass
class ChatContextStats:
    section_count: int
    original_chars: int
    organized_chars: int
    truncated_sections: List[str]


class ChatContextOrganizer:
    def __init__(self, *, total_budget: int = _CHAT_CONTEXT_BUDGET_CHARS, section_budgets: Dict[str, int] | None = None) -> None:
        self._total_budget = max(1, int(total_budget or _CHAT_CONTEXT_BUDGET_CHARS))
        self._section_budgets = dict(_CHAT_SECTION_BUDGETS)
        if section_budgets:
            self._section_budgets.update({str(key): max(200, int(value)) for key, value in section_budgets.items()})
        self._last_stats = ChatContextStats(section_count=0, original_chars=0, organized_chars=0, truncated_sections=[])

    def organize(self, sections: Dict[str, str]) -> str:
        cleaned = {str(key): self._compress_whitespace(str(value or "")) for key, value in dict(sections or {}).items() if str(value or "").strip()}
        ordered: List[Tuple[str, str]] = []
        for name in _CHAT_SECTION_ORDER:
            if cleaned.get(name):
                ordered.append((name, cleaned.pop(name)))
        for name in sorted(cleaned):
            ordered.append((name, cleaned[name]))

        original_chars = sum(len(value) for _, value in ordered)
        truncated_sections: List[str] = []
        capped: List[Tuple[str, str]] = []
        for name, value in ordered:
            cap = self._section_budgets.get(name, 2_000)
            if len(value) > cap:
                truncated_sections.append(name)
                value = self._truncate(value, cap, name)
            capped.append((name, value))

        total = sum(len(value) for _, value in capped)
        if total > self._total_budget and total > 0:
            ratio = self._total_budget / total
            shrunk: List[Tuple[str, str]] = []
            for name, value in capped:
                new_len = max(200, int(len(value) * ratio))
                if len(value) > new_len:
                    if name not in truncated_sections:
                        truncated_sections.append(name)
                    value = self._truncate(value, new_len, name)
                shrunk.append((name, value))
            capped = shrunk

        organized = "\n\n".join(value for _, value in capped if value.strip())
        self._last_stats = ChatContextStats(
            section_count=len(capped),
            original_chars=original_chars,
            organized_chars=len(organized),
            truncated_sections=truncated_sections,
        )
        return organized

    def stats(self) -> Dict[str, object]:
        return {
            "section_count": self._last_stats.section_count,
            "original_chars": self._last_stats.original_chars,
            "organized_chars": self._last_stats.organized_chars,
            "truncated_sections": list(self._last_stats.truncated_sections),
        }

    @staticmethod
    def _compress_whitespace(content: str) -> str:
        lines: List[str] = []
        previous_empty = False
        for line in str(content or "").splitlines():
            stripped = line.strip()
            if not stripped:
                if not previous_empty:
                    lines.append("")
                previous_empty = True
                continue
            lines.append(stripped)
            previous_empty = False
        return "\n".join(lines).strip()

    @staticmethod
    def _truncate(content: str, max_chars: int, section_name: str) -> str:
        value = str(content or "")
        limit = max(80, int(max_chars or 80))
        if len(value) <= limit:
            return value
        suffix = f"\n[...truncated {section_name} to fit chat context budget]"
        return value[: max(1, limit - len(suffix))].rstrip() + suffix
