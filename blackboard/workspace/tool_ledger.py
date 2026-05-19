"""Tool execution ledger — records every tool call with status + preview.

Generates the `<tool_status>` block injected into the coder's context each step.
Slim port of luna/state/tool_ledger.py.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Deque, Dict, List, Optional

from blackboard.workspace.redaction import sanitize_mapping, sanitize_text


class ToolStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"
    SKIPPED = "skipped"
    CACHED = "cached"


@dataclass
class ToolEntry:
    tool_name: str
    arguments: Dict[str, Any] = field(default_factory=dict)
    status: ToolStatus = ToolStatus.QUEUED
    output_preview: str = ""
    error: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    elapsed_ms: float = 0.0
    session_id: str = ""
    run_id: str = ""
    principal: str = ""
    source: str = ""
    trust_level: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "arguments": self.arguments,
            "status": self.status.value,
            "output_preview": self.output_preview,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_ms": self.elapsed_ms,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "principal": self.principal,
            "source": self.source,
            "trust_level": self.trust_level,
            "metadata": self.metadata,
        }


class ToolExecutionLedger:
    """Rolling ledger of tool calls per session. Thread-safe."""

    def __init__(self, max_entries_per_session: int = 200) -> None:
        self._max = int(max_entries_per_session)
        self._sessions: Dict[str, Deque[ToolEntry]] = {}
        self._lock = threading.RLock()

    def _bucket(self, session_id: str) -> Deque[ToolEntry]:
        with self._lock:
            if session_id not in self._sessions:
                self._sessions[session_id] = deque(maxlen=self._max)
            return self._sessions[session_id]

    def record_start(
        self,
        *,
        session_id: str,
        tool_name: str,
        arguments: Dict[str, Any],
        run_id: str = "",
        principal: str = "",
        source: str = "",
        trust_level: int = 0,
        metadata: Dict[str, Any] | None = None,
    ) -> ToolEntry:
        entry = ToolEntry(
            tool_name=tool_name,
            arguments=sanitize_mapping(arguments or {}),
            status=ToolStatus.RUNNING,
            started_at=time.time(),
            session_id=session_id,
            run_id=run_id,
            principal=principal,
            source=source,
            trust_level=int(trust_level or 0),
            metadata=sanitize_mapping(metadata or {}),
        )
        self._bucket(session_id).append(entry)
        return entry

    def record_finish(
        self,
        *,
        session_id: str,
        tool_name: str,
        success: bool,
        output: str = "",
        error: str = "",
        elapsed_ms: float = 0.0,
    ) -> None:
        entry = self._last_running(session_id, tool_name) or ToolEntry(tool_name=tool_name, session_id=session_id)
        if entry not in self._bucket(session_id):
            self._bucket(session_id).append(entry)
        entry.status = ToolStatus.SUCCESS if success else ToolStatus.FAILED
        entry.finished_at = time.time()
        entry.elapsed_ms = float(elapsed_ms or (entry.finished_at - entry.started_at) * 1000)
        entry.output_preview = sanitize_text(output, max_chars=200)
        entry.error = sanitize_text(error, max_chars=200)

    def record_timeout(self, *, session_id: str, tool_name: str, elapsed_ms: float = 0.0) -> None:
        entry = self._last_running(session_id, tool_name) or ToolEntry(tool_name=tool_name, session_id=session_id)
        entry.status = ToolStatus.TIMEOUT
        entry.finished_at = time.time()
        entry.elapsed_ms = float(elapsed_ms or 0)
        self._bucket(session_id).append(entry)

    def _last_running(self, session_id: str, tool_name: str) -> Optional[ToolEntry]:
        bucket = self._bucket(session_id)
        for entry in reversed(bucket):
            if entry.tool_name == tool_name and entry.status == ToolStatus.RUNNING:
                return entry
        return None

    def entries(self, session_id: str) -> List[ToolEntry]:
        return list(self._bucket(session_id))

    def stats(self, session_id: str) -> Dict[str, Any]:
        bucket = self._bucket(session_id)
        success = sum(1 for e in bucket if e.status == ToolStatus.SUCCESS)
        failed = sum(1 for e in bucket if e.status == ToolStatus.FAILED)
        timeout = sum(1 for e in bucket if e.status == ToolStatus.TIMEOUT)
        return {
            "total": len(bucket),
            "success": success,
            "failed": failed,
            "timeout": timeout,
            "success_rate": round(success / max(1, len(bucket)) * 100, 1),
            "total_elapsed_ms": round(sum(e.elapsed_ms for e in bucket), 1),
        }

    def build_context_block(self, session_id: str, *, recent: int = 6) -> str:
        bucket = self._bucket(session_id)
        if not bucket:
            return ""
        stats = self.stats(session_id)
        lines = ["<tool_status>"]
        lines.append(
            f"calls: {stats['total']} | "
            f"success: {stats['success']} | failed: {stats['failed']} | timeout: {stats['timeout']} | "
            f"rate: {stats['success_rate']}% | total_time: {stats['total_elapsed_ms']}ms"
        )
        recent_entries = list(bucket)[-recent:]
        if recent_entries:
            lines.append("recent:")
            for entry in recent_entries:
                mark = {
                    ToolStatus.SUCCESS: "✓",
                    ToolStatus.FAILED: "✗",
                    ToolStatus.TIMEOUT: "⏱",
                    ToolStatus.RUNNING: "▶",
                    ToolStatus.CACHED: "⚡",
                }.get(entry.status, "·")
                detail = entry.output_preview if entry.status == ToolStatus.SUCCESS else entry.error
                lines.append(f"  {mark} {entry.tool_name} [{int(entry.elapsed_ms)}ms] {detail[:120]}")
        lines.append("</tool_status>")
        return "\n".join(lines)


_ledger: Optional[ToolExecutionLedger] = None


def get_tool_ledger() -> ToolExecutionLedger:
    global _ledger
    if _ledger is None:
        _ledger = ToolExecutionLedger()
    return _ledger
