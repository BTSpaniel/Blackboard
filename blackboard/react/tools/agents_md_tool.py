"""ReAct wrapper for the AGENTS.md loader (declared in blackboard.coding.agents_md)."""
from __future__ import annotations

import json
from typing import Any, Dict

from blackboard.react.tool_registry import ToolRegistry


def _agents_md_read(args: Dict[str, Any]) -> str:
    from blackboard.coding.agents_md import load_agents_md  # late import to avoid cycle
    cwd = str(args.get("cwd") or ".")
    explicit = args.get("path")
    text = load_agents_md(cwd=cwd, explicit_path=str(explicit) if explicit else None)
    return json.dumps({"cwd": cwd, "content": text, "found": bool(text)})


def register_agents_md_tools(registry: ToolRegistry) -> None:
    registry.register_fn(
        "agents_md_read",
        "Read layered AGENTS.md files (home -> repo root -> cwd). Returns concatenated markdown.",
        {
            "type": "object",
            "properties": {
                "cwd": {"type": "string", "default": "."},
                "path": {"type": "string", "description": "Optional explicit AGENTS.md path."},
            },
        },
        _agents_md_read,
        tags=["docs", "read"],
    )
