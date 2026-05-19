from __future__ import annotations

import json
import re
import time
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List

from blackboard.coding import project_intelligence as pi
from blackboard.kernel.atomic_files import write_text_atomically
from blackboard.workspace.chat_scratchboard import ChatScratchboardStore
from blackboard.workspace.message_ledger import MessageLedger
from blackboard.workspace.sessions import SessionStore

_STOP_WORDS = {
    "the", "and", "for", "with", "that", "this", "from", "into", "your", "their", "them", "they", "have",
    "what", "when", "where", "which", "will", "would", "could", "should", "about", "make", "build", "create",
    "need", "want", "just", "then", "than", "each", "loop", "chat", "user", "project", "blackboard",
}

_FRAMEWORK_HINTS = {
    "react": ["react", "tsx", "jsx", "vite", "component", "tailwind"],
    "fastapi": ["fastapi", "uvicorn", "pydantic", "router", "endpoint"],
    "python": ["python", "pytest", "asyncio", "pip", "venv"],
    "javascript": ["javascript", "typescript", "node", "npm", "pnpm", "bun"],
    "playwright": ["playwright", "browser", "browse", "dom", "page", "click", "scroll"],
    "research": ["research", "crawl", "search", "web", "fetch", "documentation"],
}

_CAPABILITY_HINTS = [
    {
        "name": "browser_automation",
        "keywords": ["browser", "playwright", "browse", "click", "scroll", "inspect page", "dom"],
        "tool_prefixes": ["browse"],
        "tool_names": ["browse", "browse_search", "browse_extract"],
        "tool_kind": "browser tool",
    },
    {
        "name": "web_research",
        "keywords": ["research", "crawl", "web", "search internet", "docs", "documentation", "google"],
        "tool_prefixes": ["web_", "search_"],
        "tool_names": ["fetch_url", "web_search", "web_research", "search_documentation"],
        "tool_kind": "research tool",
    },
    {
        "name": "code_search",
        "keywords": ["grep", "find in code", "search code", "symbol", "references", "search repo"],
        "tool_prefixes": ["search_"],
        "tool_names": ["search_code", "search_files", "search_multi"],
        "tool_kind": "code search tool",
    },
    {
        "name": "workspace_execution",
        "keywords": ["test", "lint", "command", "shell", "run script", "execute"],
        "tool_prefixes": ["git_"],
        "tool_names": ["run_tests", "lint_check", "git_status", "git_diff"],
        "tool_kind": "execution tool",
    },
]


def adaptive_skill_dir(data_root: Path, project_id: str) -> Path:
    return Path(data_root) / "projects" / str(project_id or "default") / "adaptive_skills"


@lru_cache(maxsize=1)
def default_available_tool_names() -> tuple[str, ...]:
    from blackboard.react.tools.registry_builder import build_default_registry

    return tuple(sorted(build_default_registry().all_names()))


def synthesize_adaptive_skills(
    *,
    data_root: Path,
    project_id: str,
    cwd: str,
    query: str,
    session_id: str = "",
    available_tools: List[str] | None = None,
) -> Dict[str, Any]:
    root = adaptive_skill_dir(data_root, project_id)
    root.mkdir(parents=True, exist_ok=True)
    summary = pi.ensure_project_intelligence(Path(data_root) / "project_intelligence", cwd=cwd, objective=query)
    scratchboard = ChatScratchboardStore(Path(data_root), project_id).load(session_id or "main")
    sessions = SessionStore(Path(data_root), project_id)
    session_tail = sessions.tail(session_id or "main", limit=16)
    relevant_hits = sessions.search_messages(query=query, session_id=session_id or "main", limit=8)
    board_cards = _load_board_cards(Path(data_root), project_id)
    receipts = MessageLedger(Path(data_root), project_id).recent(limit=12)
    tools = sorted({str(name or "").strip() for name in (available_tools or list(default_available_tool_names())) if str(name or "").strip()})
    profile_facts = _profile_facts(query, scratchboard, session_tail, relevant_hits)
    build_signals = _build_signals(query, summary, board_cards, receipts)
    missing_capabilities = _missing_capabilities(query, tools)
    skills = [
        {
            "slug": "adaptive-user-profile",
            "name": "adaptive-user-profile",
            "description": _limit(f"Durable user/project preferences inferred from Blackboard history for {project_id}.", 140),
            "priority": 95,
            "tags": ["adaptive", "generated", "history", "preferences"],
            "composes": [],
            "when_to_use": "Use when deciding defaults, conventions, and preferred implementation style for this user/project.",
            "generated": True,
            "allowed_tools": ["skill_invoke"],
            "body": _render_profile_body(profile_facts),
        },
        {
            "slug": "adaptive-current-build",
            "name": "adaptive-current-build",
            "description": _limit(f"Current build direction inferred from the active request, board, and project hotspots.", 140),
            "priority": 110,
            "tags": ["adaptive", "generated", "current", "build"],
            "composes": ["adaptive-user-profile"],
            "when_to_use": "Use when planning or implementing the current request so work stays aligned with what the user is trying to build now.",
            "generated": True,
            "allowed_tools": ["skill_invoke"],
            "body": _render_build_body(query, build_signals),
        },
        {
            "slug": "adaptive-tool-builder",
            "name": "adaptive-tool-builder",
            "description": _limit("Blackboard-native guidance for extending or composing tools when current capabilities are insufficient.", 140),
            "priority": 90 if missing_capabilities else 70,
            "tags": ["adaptive", "generated", "tools", "extensibility"],
            "composes": ["adaptive-current-build"],
            "when_to_use": "Use when the task implies a capability Blackboard does not clearly expose through existing tools.",
            "generated": True,
            "allowed_tools": ["tool_search", "get_tool_schema", "skill_invoke"],
            "body": _render_tool_builder_body(tools, missing_capabilities),
        },
        {
            "slug": "adaptive-skill-graph",
            "name": "adaptive-skill-graph",
            "description": _limit("Composition map for the synthesized adaptive skills active on this loop.", 140),
            "priority": 120,
            "tags": ["adaptive", "generated", "composition"],
            "composes": ["adaptive-user-profile", "adaptive-current-build", "adaptive-tool-builder"],
            "when_to_use": "Use first when you need the shortest path through the available adaptive skills for the current request.",
            "generated": True,
            "allowed_tools": ["skill_invoke"],
            "body": _render_graph_body(query, missing_capabilities),
        },
    ]
    manifest: Dict[str, Any] = {"updated_at": time.time(), "project_id": project_id, "cwd": str(cwd), "skills": []}
    for spec in skills:
        skill_path = root / spec["slug"] / "SKILL.md"
        skill_path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomically(skill_path, _render_skill_file(spec))
        manifest["skills"].append({
            "name": spec["name"],
            "path": str(skill_path),
            "priority": int(spec["priority"]),
            "generated": True,
            "composes": list(spec.get("composes") or []),
        })
    write_text_atomically(root / "manifest.json", json.dumps(manifest, indent=2, default=str))
    return {
        "dir": str(root),
        "skills": skills,
        "context_block": _render_context_block(skills),
        "missing_capabilities": missing_capabilities,
    }


def _render_skill_file(spec: Dict[str, Any]) -> str:
    frontmatter = {
        "name": spec["name"],
        "description": spec["description"],
        "priority": int(spec.get("priority") or 0),
        "tags": list(spec.get("tags") or []),
        "when_to_use": spec.get("when_to_use") or "",
        "composes": list(spec.get("composes") or []),
        "generated": bool(spec.get("generated", False)),
        "allowed_tools": list(spec.get("allowed_tools") or []),
    }
    extra_frontmatter = dict(spec.get("frontmatter") or {})
    if extra_frontmatter:
        frontmatter.update(extra_frontmatter)
    return "---\n" + json.dumps(frontmatter, indent=2) + "\n---\n\n" + str(spec.get("body") or "").strip() + "\n"


def _render_context_block(skills: List[Dict[str, Any]]) -> str:
    lines = ["<adaptive_skills>", "Synthesized Blackboard skills for this loop:"]
    for spec in sorted(skills, key=lambda item: (-int(item.get("priority") or 0), str(item.get("name") or ""))):
        composes = list(spec.get("composes") or [])
        compose_text = f" composes={','.join(composes[:4])}" if composes else ""
        lines.append(f"- {spec.get('name')} priority={int(spec.get('priority') or 0)}{compose_text}: {_limit(str(spec.get('description') or ''), 160)}")
    lines.append("</adaptive_skills>")
    return "\n".join(lines)


def _profile_facts(query: str, scratchboard: Dict[str, Any], session_tail: List[Any], relevant_hits: List[Dict[str, Any]]) -> List[str]:
    facts = [str(item or "").strip() for item in list(scratchboard.get("facts") or []) if str(item or "").strip()]
    corpus = "\n".join([query, *(str(getattr(item, "content", "") or "") for item in session_tail), *(str(hit.get("content") or "") for hit in relevant_hits)])
    frameworks = _detect_frameworks(corpus)
    if frameworks:
        facts.append(f"Likely active stack: {', '.join(frameworks[:4])}")
    top_terms = _top_terms(corpus, limit=6)
    if top_terms:
        facts.append(f"Repeated working vocabulary: {', '.join(top_terms)}")
    deduped: List[str] = []
    seen: set[str] = set()
    for fact in facts:
        key = fact.casefold()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(_limit(fact, 220))
    return deduped[:8]


def _build_signals(query: str, summary: Dict[str, Any], board_cards: List[Dict[str, Any]], receipts: List[Any]) -> Dict[str, List[str]]:
    signals: Dict[str, List[str]] = {
        "focus": [],
        "files": [],
        "board": [],
        "outcomes": [],
    }
    signals["focus"] = _top_terms("\n".join([query, str(summary.get("last_objective") or "")]), limit=8)
    for hotspot in list(summary.get("hotspots") or [])[:6]:
        path = str(hotspot.get("path") or "").strip()
        if path:
            signals["files"].append(path)
    for card in board_cards[:6]:
        title = str(card.get("title") or "").strip()
        if title:
            signals["board"].append(_limit(title, 140))
    for outcome in list(summary.get("recent_outcomes") or [])[:4]:
        objective = str(outcome.get("objective") or "").strip()
        if objective:
            signals["outcomes"].append(_limit(objective, 180))
    if not signals["board"]:
        for receipt in receipts[:4]:
            preview = str(getattr(receipt, "content_preview", "") or "").strip()
            if preview:
                signals["board"].append(_limit(preview, 140))
    return signals


def _missing_capabilities(query: str, available_tools: List[str]) -> List[Dict[str, Any]]:
    query_lc = str(query or "").lower()
    tools = {str(name or "").strip().lower() for name in available_tools}
    missing: List[Dict[str, Any]] = []
    for hint in _CAPABILITY_HINTS:
        if not any(keyword in query_lc for keyword in hint["keywords"]):
            continue
        present = any(any(name.startswith(prefix) for prefix in hint["tool_prefixes"]) for name in tools)
        present = present or any(name in tools for name in hint["tool_names"])
        if present:
            continue
        missing.append({
            "name": hint["name"],
            "tool_kind": hint["tool_kind"],
            "keywords": list(hint["keywords"][:5]),
        })
    return missing


def _render_profile_body(facts: List[str]) -> str:
    lines = ["# Adaptive user profile", "", "Use these durable preferences and repeated project signals before choosing defaults.", ""]
    if facts:
        lines.append("## Active preferences and habits")
        for fact in facts:
            lines.append(f"- {fact}")
    else:
        lines.append("## Active preferences and habits")
        lines.append("- No durable preferences are strong enough yet. Prefer local repository conventions and recent project patterns.")
    lines.append("")
    lines.append("## Application")
    lines.append("- Prefer matching repeated user conventions before introducing a new pattern.")
    lines.append("- If the task conflicts with older history, favor the current request and most recent project state.")
    lines.append("- Keep outputs composable with `adaptive-current-build` and `adaptive-tool-builder`.")
    return "\n".join(lines)


def _render_build_body(query: str, signals: Dict[str, List[str]]) -> str:
    lines = ["# Adaptive current build", "", "Use this skill to stay aligned with the specific thing the user is trying to build right now.", "", "## Current request", f"- {_limit(query, 320)}", ""]
    if signals.get("focus"):
        lines.append("## Likely focus terms")
        for term in signals["focus"]:
            lines.append(f"- {term}")
        lines.append("")
    if signals.get("files"):
        lines.append("## Hot files and paths")
        for item in signals["files"]:
            lines.append(f"- {item}")
        lines.append("")
    if signals.get("board"):
        lines.append("## Related board and conversation signals")
        for item in signals["board"]:
            lines.append(f"- {item}")
        lines.append("")
    if signals.get("outcomes"):
        lines.append("## Recent outcomes to reuse or avoid")
        for item in signals["outcomes"]:
            lines.append(f"- {item}")
        lines.append("")
    lines.append("## Application")
    lines.append("- Use the most recent objective and active board work as the primary frame for planning.")
    lines.append("- Reuse hotspots and recent success paths before creating parallel patterns.")
    lines.append("- Compose this with `adaptive-user-profile` before making high-impact defaults.")
    return "\n".join(lines)


def _render_tool_builder_body(available_tools: List[str], missing_capabilities: List[Dict[str, Any]]) -> str:
    lines = ["# Adaptive tool builder", "", "Use this skill when the task implies a capability Blackboard may not already expose cleanly.", "", "## Existing surface", f"- Available tool count: {len(available_tools)}"]
    if available_tools:
        lines.append(f"- Relevant tool names snapshot: {', '.join(available_tools[:18])}")
    lines.append("")
    lines.append("## Blackboard-native tool extension flow")
    lines.append("1. Search the existing tool surface first through `tool_search`, `get_tool_schema`, and current tool modules.")
    lines.append("2. Extend an existing module in `blackboard/react/tools/` before creating a new one.")
    lines.append("3. If a new tool is required, keep it narrow, composable, and easy to register in `blackboard/react/tools/registry_builder.py`.")
    lines.append("4. Add focused tests in `tests/` for registration and behavior before relying on the tool.")
    lines.append("5. If the workflow will recur, keep the tool small and move the reusable procedure into a skill.")
    lines.append("")
    lines.append("## Current inferred gaps")
    if missing_capabilities:
        for gap in missing_capabilities:
            lines.append(f"- Missing {gap['tool_kind']} capability for {gap['name']} triggered by keywords: {', '.join(gap['keywords'])}")
    else:
        lines.append("- No obvious tool gap is inferred from the current request. Prefer composition of existing tools.")
    return "\n".join(lines)


def _render_graph_body(query: str, missing_capabilities: List[Dict[str, Any]]) -> str:
    lines = ["# Adaptive skill graph", "", f"Active request: {_limit(query, 320)}", "", "## Composition order", "1. Load `adaptive-user-profile` for durable preferences and stack hints.", "2. Load `adaptive-current-build` for the current task frame.", "3. Load `adaptive-tool-builder` if the task needs new or extended tools."]
    if missing_capabilities:
        lines.append("")
        lines.append("## Tool-build trigger")
        lines.append("- The current request implies missing capabilities. Building or extending a tool is allowed if existing tools cannot satisfy the task.")
    return "\n".join(lines)


def _load_board_cards(data_root: Path, project_id: str) -> List[Dict[str, Any]]:
    path = Path(data_root) / "projects" / str(project_id or "default") / "board.json"
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    cards = list(payload.get("cards") or [])
    cards.sort(key=lambda item: float(item.get("updated_at") or item.get("created_at") or 0), reverse=True)
    return cards[:12]


def _detect_frameworks(text: str) -> List[str]:
    haystack = str(text or "").lower()
    found: List[str] = []
    for name, hints in _FRAMEWORK_HINTS.items():
        if any(hint in haystack for hint in hints):
            found.append(name)
    return found


def _top_terms(text: str, limit: int = 8) -> List[str]:
    counts: Counter[str] = Counter()
    for term in re.findall(r"[a-z0-9_./-]{3,}", str(text or "").lower()):
        if term in _STOP_WORDS or term.isdigit():
            continue
        counts[term] += 1
    return [term for term, _ in counts.most_common(max(1, int(limit or 8)))]


def _limit(text: str, max_chars: int) -> str:
    value = str(text or "").strip()
    if len(value) <= max_chars:
        return value
    return value[: max(1, int(max_chars or 1) - 1)].rstrip() + "…"
