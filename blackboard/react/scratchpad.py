"""Scratchpad — per-request Thought/Action/Observation trace.

Ported from luna/workers/react/scratchpad.py.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class StepKind(str, Enum):
    THOUGHT = "thought"
    ACTION = "action"
    OBSERVATION = "observation"
    FINAL = "final"
    ERROR = "error"


@dataclass
class ScratchpadStep:
    kind: StepKind
    content: str
    tool_name: Optional[str] = None
    tool_args: Optional[Dict[str, Any]] = None
    success: Optional[bool] = None
    duration_ms: Optional[float] = None
    timestamp: float = field(default_factory=time.time)

    def to_text(self) -> str:
        if self.kind == StepKind.THOUGHT:
            return f"Thought: {self.content}"
        if self.kind == StepKind.ACTION:
            args_str = f"({self.tool_args})" if self.tool_args else ""
            return f"Action: {self.tool_name}{args_str}"
        if self.kind == StepKind.OBSERVATION:
            status = "" if self.success is None else (" ✓" if self.success else " ✗")
            return f"Observation{status}: {self.content}"
        if self.kind == StepKind.FINAL:
            return f"Final Answer: {self.content}"
        return f"Error: {self.content}"


class Scratchpad:
    """Accumulates ReAct steps for one request."""

    def __init__(self, request_id: str = "") -> None:
        self.request_id = request_id
        self.steps: List[ScratchpadStep] = []
        self._started_at = time.monotonic()

    def thought(self, content: str) -> None:
        self.steps.append(ScratchpadStep(kind=StepKind.THOUGHT, content=content))

    def action(self, tool_name: str, args: Dict[str, Any]) -> None:
        self.steps.append(ScratchpadStep(
            kind=StepKind.ACTION,
            content=f"Call {tool_name}",
            tool_name=tool_name,
            tool_args=args,
        ))

    def observation(self, content: str, success: bool = True, duration_ms: float = 0.0) -> None:
        self.steps.append(ScratchpadStep(
            kind=StepKind.OBSERVATION,
            content=content,
            success=success,
            duration_ms=duration_ms,
        ))

    def final(self, content: str) -> None:
        self.steps.append(ScratchpadStep(kind=StepKind.FINAL, content=content))

    def error(self, content: str) -> None:
        self.steps.append(ScratchpadStep(kind=StepKind.ERROR, content=content))

    def to_text(self) -> str:
        return "\n".join(s.to_text() for s in self.steps)

    def fold_to_summary(self) -> str:
        if not self.steps:
            return "[FOLDED] No steps recorded."
        tools_used = list(dict.fromkeys(
            s.tool_name for s in self.steps if s.kind == StepKind.ACTION and s.tool_name
        ))
        error_count = sum(
            1 for s in self.steps
            if s.kind == StepKind.ERROR or (s.kind == StepKind.OBSERVATION and s.success is False)
        )
        final_step = next((s for s in reversed(self.steps) if s.kind == StepKind.FINAL), None)
        last_obs = next((s for s in reversed(self.steps) if s.kind == StepKind.OBSERVATION), None)
        outcome_text = ""
        if final_step:
            outcome_text = final_step.content[:200]
        elif last_obs:
            prefix = "failed: " if last_obs.success is False else ""
            outcome_text = prefix + last_obs.content[:200]
        elapsed = round(time.monotonic() - self._started_at, 1)
        tools_str = ", ".join(tools_used) if tools_used else "none"
        lines = [f"[FOLDED] {len(self.steps)} steps | {elapsed}s | tools=[{tools_str}] | errors={error_count}"]
        if outcome_text:
            lines.append(f"  key_outcome: {outcome_text}")
        return "\n".join(lines)

    def summary(self) -> Dict[str, Any]:
        return {
            "request_id": self.request_id,
            "steps": len(self.steps),
            "thoughts": sum(1 for s in self.steps if s.kind == StepKind.THOUGHT),
            "tool_calls": sum(1 for s in self.steps if s.kind == StepKind.ACTION),
            "errors": sum(1 for s in self.steps if s.kind == StepKind.ERROR),
            "elapsed_s": round(time.monotonic() - self._started_at, 3),
        }
