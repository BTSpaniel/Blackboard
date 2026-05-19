"""Lightweight context health diagnostics for coding prompts."""
from __future__ import annotations

from typing import Any, Dict, List


_DEFAULT_WARN_CHARS = 20_000
_SOURCE_OF_TRUTH_SECTIONS = {"objective", "agents_md", "task_context", "priority_files"}


def build_context_health_block(sections: Dict[str, str], *, warn_chars: int = _DEFAULT_WARN_CHARS) -> str:
    reports = analyze_context_sections(sections, warn_chars=warn_chars)
    if not reports:
        return ""
    lines = ["<context_health>", "Context diagnostics:"]
    for report in reports[:10]:
        lines.append(f"- {report['section']}: {report['message']}")
    lines.append("</context_health>")
    return "\n".join(lines)


def analyze_context_sections(sections: Dict[str, str], *, warn_chars: int = _DEFAULT_WARN_CHARS) -> List[Dict[str, Any]]:
    reports: List[Dict[str, Any]] = []
    seen: Dict[str, str] = {}
    for name, content in sections.items():
        text = content or ""
        if len(text) > warn_chars:
            reports.append({"section": name, "message": f"large section ({len(text)} chars); budget allocator may truncate it"})
        fingerprint = " ".join(text.lower().split())[:1000]
        if fingerprint and fingerprint in seen and name not in _SOURCE_OF_TRUTH_SECTIONS:
            reports.append({"section": name, "message": f"appears redundant with {seen[fingerprint]}"})
        elif fingerprint:
            seen[fingerprint] = name
        if name == "tool_status" and len(text) > 4000:
            reports.append({"section": name, "message": "tool status is long; stale tool results may need folding"})
    return reports
