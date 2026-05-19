"""ReAct loop — Thought / Action / Observation / Final.

Slim port of luna/workers/react/loop.py keeping:
  - dynamic iteration budget
  - stagnation detection (exploration-only loops)
  - mutation verification gate
  - safe-rewrite of unsupported "done" claims
"""
from __future__ import annotations

import ast
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from blackboard.kernel.logger import describe_error, get_logger
from blackboard.providers.base import AIProvider, Message, ProviderError
from blackboard.coding.context_rot import ContextCompressor
from blackboard.react.scratchpad import Scratchpad
from blackboard.react.tool_registry import DISCOVERY_TOOL_NAME, SCHEMA_TOOL_NAME, ToolRegistry, ToolResult
from blackboard.workspace.redaction import guard_untrusted_text

logger = get_logger("react.loop")


_DEFAULT_MAX_ITER = 10
_DYNAMIC_PROGRESS_EXTENSION_STEP = 2
_DYNAMIC_PROGRESS_EXTENSION_MAX = 2
_PROGRESSIVE_DISCOVERY_MAX_VISIBLE = 64
_PROGRESSIVE_DISCOVERY_EXPAND_TOOLS = 12
_GROQ_TOOL_MAX_VISIBLE = 10
_GROQ_TOOL_EXPAND_TOOLS = 6
_GROQ_TOOL_CALL_TEMPERATURE = 0.0
_TOOL_CALL_FENCE_RE = re.compile(r"```tool_call\s*(\{[\s\S]*?\})\s*```", re.IGNORECASE)
_TOOL_CALL_TAG_RE = re.compile(r"<tool_call>\s*(\{[\s\S]*?\})\s*</tool_call>", re.IGNORECASE)
_EXPLORATION_TOOLS = {
    "read_file", "list_dir", "search_code", "search_files", "agents_md_read",
    "git_status", "git_diff", "lsp_outline", "lsp_diagnostics", "lsp_definition",
    "lsp_references", "lsp_hover", DISCOVERY_TOOL_NAME, SCHEMA_TOOL_NAME,
}
_MUTATION_CLAIM_PATTERNS = [
    "i created", "i wrote", "i added", "i fixed", "i updated", "i implemented",
    "done!", "done.", "created the", "fully complete",
]
_CARDS_FENCE_RE = re.compile(r"```cards\b", re.IGNORECASE)


def _looks_like_unsupported_claim(text: str) -> bool:
    lowered = (text or "").strip().lower()
    if not lowered:
        return False
    return any(p in lowered for p in _MUTATION_CLAIM_PATTERNS)


def _safe_no_edit_response() -> str:
    return (
        "I inspected the project and gathered evidence, but I did not actually edit or "
        "create any files yet. I should use a write/patch tool before claiming the change is done."
    )


def _contains_cards_fence(text: str) -> bool:
    return bool(_CARDS_FENCE_RE.search(str(text or "")))


def _is_groq_provider(provider: AIProvider) -> bool:
    provider_id = str(getattr(provider, "id", "") or "").strip().lower()
    return provider_id.startswith("groq")


@dataclass
class ReActResult:
    content: str
    scratchpad: Scratchpad
    tool_calls: int = 0
    iterations: int = 0
    stopped_reason: str = "final_answer"
    mutated_paths: List[str] = field(default_factory=list)


class ReActLoop:
    """Run one ReAct loop end-to-end against a provider + tool registry."""

    def __init__(
        self,
        provider: AIProvider,
        tool_registry: ToolRegistry,
        *,
        max_iterations: int = _DEFAULT_MAX_ITER,
        system_prompt: str = "",
    ) -> None:
        self._provider = provider
        self._tools = tool_registry
        self._max_iter = int(max_iterations)
        self._system_prompt = system_prompt
        # Optional provider-specific shaping hints (set by callers like CodingWorker).
        self._cache_stable_prefix: bool = False
        self._previous_response_id: Optional[str] = None
        self.last_response_id: Optional[str] = None  # populated after each provider.complete
        self._context_compressor: Optional[ContextCompressor] = None

    # ── Main entrypoint ──────────────────────────────────────────

    async def run(
        self,
        message: str,
        *,
        extra_context: str = "",
        allowed_tools: Optional[List[str]] = None,
        tool_context: Optional[Dict[str, Any]] = None,
        max_iterations: Optional[int] = None,
        request_id: str = "",
        step_callback: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
    ) -> ReActResult:
        max_iter = int(max_iterations or self._max_iter)
        effective_max_iter = max_iter
        extension_budget = _DYNAMIC_PROGRESS_EXTENSION_MAX
        pad = Scratchpad(request_id=request_id)

        system_parts: List[str] = []
        if self._system_prompt:
            system_parts.append(self._system_prompt)
        if extra_context:
            system_parts.append(extra_context)
        system_text = "\n\n".join(system_parts).strip()

        messages: List[Message] = []
        if system_text:
            messages.append(Message(role="system", content=system_text))
        messages.append(Message(role="user", content=message))

        effective_tool_context = dict(tool_context or {})
        if request_id:
            effective_tool_context.setdefault("session_id", request_id)
            effective_tool_context.setdefault("run_id", request_id)
        described_tools: List[str] = []
        tool_call_count = 0
        mutation_attempts: List[str] = []
        verified_mutations: List[str] = []
        exploration_streak = 0
        stopped_reason = "final_answer"

        iteration = 0
        while iteration < effective_max_iter:
            iteration += 1
            try:
                remaining_iterations = max(effective_max_iter - iteration + 1, 0)
                if remaining_iterations <= 2 and step_callback:
                    await _maybe_await(step_callback({
                        "kind": "budget",
                        "remaining_iterations": remaining_iterations,
                        "max_iterations": effective_max_iter,
                    }))
                if self._context_compressor is not None:
                    messages = self._context_compressor.compress_messages(messages)
                tool_schemas = self._current_tool_schemas(
                    message=message,
                    allowed_tools=allowed_tools,
                    tool_context=effective_tool_context,
                    described_tools=described_tools,
                )
                response = await self._provider.complete(
                    messages,
                    tools=tool_schemas if tool_schemas else None,
                    temperature=self._tool_call_temperature(bool(tool_schemas)),
                    cache_stable_prefix=self._cache_stable_prefix,
                    previous_response_id=self._previous_response_id,
                )
                # Capture response id from raw payload if the provider returned one
                # (OpenAI Responses API returns {id: "resp_..."}; chat-completions returns {id: "chatcmpl-..."}).
                rid = ""
                if isinstance(response.raw, dict):
                    rid = str(response.raw.get("id") or "")
                if rid:
                    self.last_response_id = rid
                    # Subsequent iterations within the same loop run continue chaining.
                    self._previous_response_id = rid
            except Exception as exc:
                err = describe_error(exc, "LLM error")
                pad.error(err)
                stopped_reason = "error"
                if step_callback:
                    await _maybe_await(step_callback({"kind": "error", "content": err}))
                if isinstance(exc, ProviderError):
                    raise
                return ReActResult(
                    content=f"ReAct run failed: {err}",
                    scratchpad=pad,
                    tool_calls=tool_call_count,
                    iterations=iteration,
                    stopped_reason=stopped_reason,
                )

            # Tool call branch
            fallback_tool_calls = []
            tool_calls = list(response.tool_calls or [])
            if not tool_calls:
                fallback_tool_calls = self._extract_fallback_tool_calls(response.content)
                if fallback_tool_calls:
                    tool_calls = fallback_tool_calls
            if tool_calls:
                assistant_tool_content = self._strip_fallback_tool_call_blocks(response.content) if fallback_tool_calls else response.content
                # Record the model's text-side thought, if any.
                if assistant_tool_content.strip():
                    pad.thought(assistant_tool_content.strip())
                    if step_callback:
                        await _maybe_await(step_callback({"kind": "thought", "content": assistant_tool_content.strip()}))

                # Append assistant message that emitted tool_calls so providers like OpenAI accept it.
                messages.append(Message(role="assistant", content=assistant_tool_content, tool_calls=tool_calls))

                only_exploration_this_turn = True
                for call in tool_calls:
                    tool_call_count += 1
                    fn = call.get("function") or {}
                    name = str(fn.get("name") or "")
                    args_raw = fn.get("arguments") or "{}"
                    args = self._parse_args(args_raw)
                    pad.action(name, args)
                    if step_callback:
                        await _maybe_await(step_callback({
                            "kind": "action",
                            "tool": name,
                            "args": args,
                        }))
                    execution_allowed_tools = self._execution_allowed_tools(
                        name,
                        allowed_tools=allowed_tools,
                        tool_context=effective_tool_context,
                        described_tools=described_tools,
                    )
                    tool_result = await self._tools.execute(name, args, allowed_tools=execution_allowed_tools, tool_context=effective_tool_context)
                    if name == SCHEMA_TOOL_NAME and tool_result.success:
                        described = self._described_tool_name(tool_result.output)
                        if described and described not in described_tools:
                            described_tools.append(described)
                    if not _is_exploration_tool(name):
                        only_exploration_this_turn = False
                    if tool_result.mutated_workspace:
                        mutation_attempts.append(name)
                        if tool_result.mutation_verified:
                            verified_mutations.extend(tool_result.mutation_paths or [name])
                    pad.observation(
                        self._truncate_output(tool_result.output if tool_result.success else (tool_result.error or "")),
                        success=tool_result.success,
                        duration_ms=tool_result.duration_ms,
                    )
                    if step_callback:
                        await _maybe_await(step_callback({
                            "kind": "observation",
                            "tool": name,
                            "success": tool_result.success,
                            "output": tool_result.output,
                            "error": tool_result.error,
                            "duration_ms": tool_result.duration_ms,
                        }))
                    # Feed the observation back to the model.
                    obs_text = tool_result.output if tool_result.success else f"ERROR: {tool_result.error}"
                    obs_text = guard_untrusted_text(obs_text, max_chars=6000)
                    messages.append(Message(
                        role="tool",
                        content=self._truncate_output(obs_text),
                        tool_call_id=call.get("id") or call.get("tool_call_id") or "",
                        name=name,
                    ))

                if only_exploration_this_turn:
                    exploration_streak += 1
                else:
                    exploration_streak = 0

                if exploration_streak >= 4 and not mutation_attempts:
                    pad.error("Stagnation: too many exploration-only turns without mutation attempt.")
                    stopped_reason = "stagnation_detected"
                    return ReActResult(
                        content=(
                            "I kept inspecting the workspace without making concrete edits. "
                            "Next attempt should move from discovery to an actual file mutation."
                        ),
                        scratchpad=pad,
                        tool_calls=tool_call_count,
                        iterations=iteration,
                        stopped_reason=stopped_reason,
                    )
                if iteration >= effective_max_iter and extension_budget > 0 and verified_mutations and exploration_streak == 0:
                    added = min(_DYNAMIC_PROGRESS_EXTENSION_STEP, extension_budget)
                    effective_max_iter += added
                    extension_budget -= added
                    if step_callback:
                        await _maybe_await(step_callback({
                            "kind": "budget_extended",
                            "added_iterations": added,
                            "max_iterations": effective_max_iter,
                        }))
                continue

            # Final-answer branch
            content = response.content.strip()
            if mutation_attempts and not verified_mutations and _looks_like_unsupported_claim(content) and not _contains_cards_fence(content):
                # The model claims success but we have no verified mutation. Rewrite.
                content = _safe_no_edit_response()
                stopped_reason = "rewritten_no_edit"
            elif _looks_like_unsupported_claim(content) and not mutation_attempts and not _contains_cards_fence(content):
                content = _safe_no_edit_response()
                stopped_reason = "rewritten_no_edit"
            pad.final(content)
            if step_callback:
                await _maybe_await(step_callback({"kind": "final", "content": content}))
            return ReActResult(
                content=content,
                scratchpad=pad,
                tool_calls=tool_call_count,
                iterations=iteration,
                stopped_reason=stopped_reason,
                mutated_paths=verified_mutations,
            )

        # Hit max iterations
        last_text = ""
        for step in reversed(pad.steps):
            if step.kind.value in ("thought", "observation"):
                last_text = step.content[:300]
                break
        pad.error(f"Hit max iterations ({effective_max_iter}) without final answer.")
        return ReActResult(
            content=(
                f"I ran out of iterations ({effective_max_iter}) before producing a final answer. "
                f"Last signal: {last_text}"
            ),
            scratchpad=pad,
            tool_calls=tool_call_count,
            iterations=effective_max_iter,
            stopped_reason="max_iterations",
            mutated_paths=verified_mutations,
        )

    # ── Helpers ──────────────────────────────────────────────────

    def _current_tool_schemas(
        self,
        *,
        message: str,
        allowed_tools: Optional[List[str]],
        tool_context: Optional[Dict[str, Any]],
        described_tools: List[str],
    ) -> List[Dict[str, Any]]:
        max_visible_tools = self._max_visible_tools(allowed_tools=allowed_tools)
        expand_tools = self._expand_tools_limit()
        visible_names = self._tools.schema_names(allowed_tools=allowed_tools, tool_context=tool_context)
        if allowed_tools is not None or len(visible_names) <= max_visible_tools:
            return self._tools.schema_list_filtered(
                message,
                allowed_tools=allowed_tools,
                tool_context=tool_context,
                max_tools=max_visible_tools,
            )
        meta_schemas = [
            self._tools.discovery_tool_schema(),
            self._tools.schema_description_tool_schema(),
        ]
        described = list(dict.fromkeys(described_tools))
        if not described:
            return meta_schemas
        described_schemas = self._tools.schema_list_filtered(
            message,
            allowed_tools=described,
            tool_context=tool_context,
            max_tools=expand_tools,
        )
        return [*meta_schemas, *described_schemas]

    def _active_tool_allowlist(
        self,
        *,
        allowed_tools: Optional[List[str]],
        tool_context: Optional[Dict[str, Any]],
        described_tools: List[str],
    ) -> Optional[List[str]]:
        max_visible_tools = self._max_visible_tools(allowed_tools=allowed_tools)
        visible_names = self._tools.schema_names(allowed_tools=allowed_tools, tool_context=tool_context)
        if allowed_tools is not None or len(visible_names) <= max_visible_tools:
            return allowed_tools
        return list(dict.fromkeys([DISCOVERY_TOOL_NAME, SCHEMA_TOOL_NAME, *described_tools]))

    def _tool_call_temperature(self, has_tools: bool) -> float:
        if has_tools and _is_groq_provider(self._provider):
            return _GROQ_TOOL_CALL_TEMPERATURE
        return 0.2

    def _max_visible_tools(self, *, allowed_tools: Optional[List[str]]) -> int:
        if _is_groq_provider(self._provider):
            if allowed_tools is not None:
                return min(_GROQ_TOOL_MAX_VISIBLE, max(1, len(list(allowed_tools or []))))
            return _GROQ_TOOL_MAX_VISIBLE
        return _PROGRESSIVE_DISCOVERY_MAX_VISIBLE

    def _expand_tools_limit(self) -> int:
        if _is_groq_provider(self._provider):
            return _GROQ_TOOL_EXPAND_TOOLS
        return _PROGRESSIVE_DISCOVERY_EXPAND_TOOLS

    def _execution_allowed_tools(
        self,
        tool_name: str,
        *,
        allowed_tools: Optional[List[str]],
        tool_context: Optional[Dict[str, Any]],
        described_tools: List[str],
    ) -> Optional[List[str]]:
        name = str(tool_name or "").strip()
        if name in {DISCOVERY_TOOL_NAME, SCHEMA_TOOL_NAME}:
            return allowed_tools
        return self._active_tool_allowlist(
            allowed_tools=allowed_tools,
            tool_context=tool_context,
            described_tools=described_tools,
        )

    @staticmethod
    def _described_tool_name(output: str) -> str:
        try:
            parsed = json.loads(output or "{}")
        except Exception:
            return ""
        if not isinstance(parsed, dict):
            return ""
        return str(parsed.get("name") or "").strip()

    @staticmethod
    def _parse_args(raw: Any) -> Dict[str, Any]:
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            text = ReActLoop._repair_argument_string(raw)
            try:
                parsed = json.loads(text)
                return parsed if isinstance(parsed, dict) else {"value": parsed}
            except Exception:
                try:
                    parsed = ast.literal_eval(text)
                    return parsed if isinstance(parsed, dict) else {"value": parsed}
                except Exception:
                    return {"_raw": raw}
        return {}

    @staticmethod
    def _repair_argument_string(raw: str) -> str:
        text = str(raw or "").strip()
        if not text:
            return text
        if text.startswith("```"):
            text = re.sub(r"^```(?:json|tool_args|args)?\s*", "", text, flags=re.IGNORECASE)
            text = re.sub(r"\s*```$", "", text)
        text = re.sub(r"^<(?:tool_args|args)>\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*</(?:tool_args|args)>$", "", text, flags=re.IGNORECASE)
        lowered = text.lower()
        for prefix in ("arguments=", "args=", "input="):
            if lowered.startswith(prefix):
                text = text[len(prefix):].strip()
                break
        extracted = ReActLoop._extract_braced_object(text)
        return extracted or text

    @staticmethod
    def _extract_braced_object(text: str) -> str:
        raw = str(text or "")
        start = raw.find("{")
        if start < 0:
            return ""
        depth = 0
        in_string = False
        escape = False
        quote = ""
        for index in range(start, len(raw)):
            ch = raw[index]
            if in_string:
                if escape:
                    escape = False
                    continue
                if ch == "\\":
                    escape = True
                    continue
                if ch == quote:
                    in_string = False
                continue
            if ch in {'"', "'"}:
                in_string = True
                quote = ch
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return raw[start:index + 1]
        return ""

    @staticmethod
    def _extract_fallback_tool_calls(content: str) -> List[Dict[str, Any]]:
        calls: List[Dict[str, Any]] = []
        for pattern in (_TOOL_CALL_FENCE_RE, _TOOL_CALL_TAG_RE):
            for match in pattern.finditer(str(content or "")):
                payload_text = str(match.group(1) or "").strip()
                if not payload_text:
                    continue
                try:
                    payload = json.loads(payload_text)
                except Exception:
                    try:
                        payload = ast.literal_eval(payload_text)
                    except Exception:
                        continue
                if not isinstance(payload, dict):
                    continue
                function = payload.get("function") if isinstance(payload.get("function"), dict) else None
                if function is None:
                    name = str(payload.get("name") or "").strip()
                    if not name:
                        continue
                    function = {"name": name, "arguments": json.dumps(payload.get("arguments") or {}, default=str)}
                else:
                    function = {
                        "name": str(function.get("name") or "").strip(),
                        "arguments": function.get("arguments") if isinstance(function.get("arguments"), str) else json.dumps(function.get("arguments") or {}, default=str),
                    }
                if not function.get("name"):
                    continue
                fallback_meta = {
                    "reason": str(payload.get("reason") or "").strip(),
                    "confidence": payload.get("confidence"),
                    "retry_hint": str(payload.get("retry_hint") or "").strip(),
                }
                calls.append({
                    "id": str(payload.get("id") or f"fallback_{len(calls) + 1}"),
                    "type": "function",
                    "function": function,
                    "fallback_meta": fallback_meta,
                })
        return calls

    @staticmethod
    def _strip_fallback_tool_call_blocks(content: str) -> str:
        text = str(content or "")
        for pattern in (_TOOL_CALL_FENCE_RE, _TOOL_CALL_TAG_RE):
            text = pattern.sub("", text)
        return text.strip()

    @staticmethod
    def _truncate_output(text: str, max_chars: int = 6000) -> str:
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + f"\n... [truncated, {len(text) - max_chars} more chars]"


def _is_exploration_tool(name: str) -> bool:
    return str(name or "").strip().lower() in _EXPLORATION_TOOLS


async def _maybe_await(value: Any) -> None:
    if hasattr(value, "__await__"):
        await value
