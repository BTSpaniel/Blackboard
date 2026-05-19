from __future__ import annotations

import json
import time
from typing import Any, Dict

from blackboard.react.tool_registry import ToolRegistry
from blackboard.workspace.tool_ledger import ToolStatus, get_tool_ledger


def _tool_status(args: Dict[str, Any]) -> str:
    runtime = dict(args.get("_tool_runtime") or {})
    session_id = str(args.get("session_id") or runtime.get("session_id") or runtime.get("run_id") or "default")
    mode = str(args.get("filter") or "recent").strip().lower()
    try:
        count = int(args.get("count") or 10)
    except Exception:
        count = 10
    count = max(1, min(count, 50))
    ledger = get_tool_ledger()
    entries = ledger.entries(session_id)
    if mode == "stats":
        return json.dumps({"session_id": session_id, **ledger.stats(session_id)}, default=str)
    if mode == "active":
        active = [entry for entry in entries if entry.status == ToolStatus.RUNNING]
        if not active:
            return "No tools currently running."
        lines = [f"Active tools for {session_id} ({len(active)}):"]
        for entry in active[-count:]:
            elapsed_ms = (time.time() - float(entry.started_at or time.time())) * 1000
            lines.append(f"- {entry.tool_name}: running for {elapsed_ms:.0f}ms")
        return "\n".join(lines)
    if mode == "failures":
        failures = [entry for entry in entries if entry.status in {ToolStatus.FAILED, ToolStatus.TIMEOUT}]
        if not failures:
            return "No recent tool failures."
        lines = [f"Recent tool failures for {session_id} ({len(failures[-count:])}):"]
        for entry in failures[-count:]:
            detail = entry.error or entry.output_preview
            lines.append(f"- {entry.tool_name} [{entry.status.value}, {int(entry.elapsed_ms)}ms] {detail[:160]}")
        return "\n".join(lines)
    block = ledger.build_context_block(session_id, recent=count)
    return block or f"No tool calls recorded for {session_id}."


def register_introspection_tools(registry: ToolRegistry) -> None:
    registry.register_fn(
        "tool_status",
        "Inspect recent tool calls, failures, active calls, or aggregate stats for the current run/session.",
        {
            "type": "object",
            "properties": {
                "filter": {"type": "string", "enum": ["recent", "active", "failures", "stats"], "description": "Which tool ledger view to return."},
                "count": {"type": "integer", "description": "Maximum recent entries to show."},
                "session_id": {"type": "string", "description": "Optional session id override; defaults to the current run."},
            },
        },
        _tool_status,
        tags=["introspection", "tools", "ledger", "read"],
        domain="introspection",
    )
