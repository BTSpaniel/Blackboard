"""Provider interface — every adapter implements this.

The workspace owns truth. Providers only supply intelligence or execution.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Literal, Optional


ProviderType = Literal["llm_api", "coding_cli", "local_model", "router"]


class ProviderError(Exception):
    """Raised when a provider call fails. Carries an optional ``retryable`` flag."""

    def __init__(self, message: str, *, retryable: bool = False, rate_limited: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.rate_limited = rate_limited


# ── Capabilities ──────────────────────────────────────────────────


@dataclass
class ProviderCapabilities:
    chat: bool = False
    code_edit: bool = False
    tool_calling: bool = False
    vision: bool = False
    long_context: bool = False
    streaming: bool = False
    structured_output: bool = False
    repo_aware: bool = False
    terminal_aware: bool = False

    @classmethod
    def from_list(cls, items: List[str]) -> "ProviderCapabilities":
        bag = {str(item).strip().lower() for item in items or []}
        return cls(
            chat="chat" in bag,
            code_edit="code_edit" in bag or "tools" in bag,
            tool_calling="tools" in bag or "tool_calling" in bag,
            vision="vision" in bag,
            long_context="long_context" in bag,
            streaming="streaming" in bag,
            structured_output="structured_output" in bag,
            repo_aware="repo_aware" in bag,
            terminal_aware="terminal_aware" in bag,
        )


@dataclass
class ProviderHealth:
    ok: bool
    latency_ms: int = 0
    error: str = ""
    detail: Dict[str, Any] = field(default_factory=dict)


# ── Shared message + IO contracts ─────────────────────────────────


@dataclass
class Message:
    role: str  # "system" | "user" | "assistant" | "tool"
    content: str
    name: Optional[str] = None
    tool_call_id: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None


@dataclass
class PlanInput:
    objective: str
    project_root: str
    context_blocks: List[str] = field(default_factory=list)


@dataclass
class PlanOutput:
    cards: List[Dict[str, Any]]  # [{title, body, files, verification, constraints, deps}]
    raw: str = ""
    tokens_used: int = 0
    model: str = ""


@dataclass
class TaskInput:
    objective: str
    files: List[str]
    constraints: List[str]
    verification: List[str]
    context: str = ""


@dataclass
class TaskOutput:
    cards: List[Dict[str, Any]]
    raw: str = ""


@dataclass
class ReviewInput:
    objective: str
    diff: str
    summary: str
    constraints: List[str] = field(default_factory=list)


@dataclass
class ReviewOutput:
    overall: str  # "pass" | "fail" | "needs_revision"
    issues: List[str] = field(default_factory=list)
    suggestions: List[str] = field(default_factory=list)
    verdict_reason: str = ""


@dataclass
class ExecuteInput:
    """For coding_cli providers (Claude Code, etc.)."""
    objective: str
    cwd: str
    files: List[str] = field(default_factory=list)
    constraints: List[str] = field(default_factory=list)
    verification: List[str] = field(default_factory=list)
    timeout_s: float = 600.0


@dataclass
class ExecuteOutput:
    success: bool
    transcript: str = ""
    changed_files: List[str] = field(default_factory=list)
    error: str = ""


# ── LLM-call contracts (for adapters that act as a brain) ─────────


@dataclass
class LLMResponse:
    content: str
    model: str = ""
    tokens_prompt: int = 0
    tokens_completion: int = 0
    finish_reason: str = "stop"
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)


# ── Abstract base ─────────────────────────────────────────────────


class AIProvider(abc.ABC):
    """Every provider exposes this interface. Optional methods raise NotImplementedError."""

    id: str = ""
    name: str = ""
    type: ProviderType = "llm_api"
    capabilities: ProviderCapabilities = ProviderCapabilities()
    model: str = ""

    @abc.abstractmethod
    async def health(self) -> ProviderHealth: ...

    async def complete(
        self,
        messages: List[Message],
        *,
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
        cache_stable_prefix: bool = False,
        previous_response_id: Optional[str] = None,
        **kwargs: Any,
    ) -> LLMResponse:
        raise NotImplementedError(f"{self.id} does not support chat completion")

    async def stream(
        self,
        messages: List[Message],
        *,
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        raise NotImplementedError(f"{self.id} does not support streaming")
        if False:  # pragma: no cover  — make this a generator for type checkers
            yield ""

    async def list_models(self) -> List[str]:
        return []

    async def execute(self, input: ExecuteInput) -> ExecuteOutput:
        raise NotImplementedError(f"{self.id} is not a coding_cli provider")

    async def close(self) -> None:
        return None
