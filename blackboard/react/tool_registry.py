"""Tool Registry — registers and executes tools for the ReAct loop.

Slim port of luna/workers/react/tool_registry.py (no ToolPolicy/TrustSystem).
"""
from __future__ import annotations

import asyncio
import inspect
import json
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union

from blackboard.kernel.logger import describe_error, get_logger
from blackboard.kernel.json_schema import normalize_schema_node, validate_payload
from blackboard.governors.capability import get_capability_governor
from blackboard.governors.health import get_health_governor
from blackboard.governors.trust import get_trust_governor
from blackboard.providers.usage import get_usage_tracker
from blackboard.react.approval import get_approval_manager
from blackboard.react.tool_policy import get_tool_policy
from blackboard.workspace.tool_ledger import get_tool_ledger

logger = get_logger("react.tool_registry")
DISCOVERY_TOOL_NAME = "tool_search"
SCHEMA_TOOL_NAME = "get_tool_schema"


ToolHandler = Callable[[Dict[str, Any]], Union[str, Awaitable[str]]]


@dataclass
class ToolDefinition:
    name: str
    description: str
    parameters: Dict[str, Any]
    handler: ToolHandler
    timeout_s: float = 30.0
    tags: List[str] = field(default_factory=list)
    mutation_mode: str = "none"  # "none" | "verified" | "unverified" | "conditional"
    aliases: List[str] = field(default_factory=list)
    domain: str = ""


@dataclass
class ToolResult:
    tool_name: str
    success: bool
    output: str
    error: Optional[str] = None
    duration_ms: float = 0.0
    mutated_workspace: bool = False
    mutation_verified: bool = False
    mutation_paths: List[str] = field(default_factory=list)
    mutation_details: str = ""


class ToolRegistry:
    """Register tools and execute them by name."""

    def __init__(self) -> None:
        self._tools: Dict[str, ToolDefinition] = {}
        self.workspace_root: Optional[str] = None
        self.full_access: bool = False

    # ── Registration ─────────────────────────────────────────────

    def register(self, tool: ToolDefinition) -> None:
        if tool.name in self._tools:
            logger.warning("Tool '%s' already registered — overwriting", tool.name)
        tool.parameters = self._normalize_schema_node(tool.parameters)
        self._tools[tool.name] = tool

    def register_fn(
        self,
        name: str,
        description: str,
        parameters: Dict[str, Any],
        handler: ToolHandler,
        *,
        timeout_s: float = 30.0,
        tags: Optional[List[str]] = None,
        mutation_mode: str = "none",
        aliases: Optional[List[str]] = None,
        domain: str = "",
    ) -> None:
        self.register(ToolDefinition(
            name=name,
            description=description,
            parameters=parameters,
            handler=handler,
            timeout_s=timeout_s,
            tags=tags or [],
            mutation_mode=mutation_mode,
            aliases=aliases or [],
            domain=domain,
        ))

    def get(self, name: str) -> Optional[ToolDefinition]:
        if name in self._tools:
            return self._tools[name]
        for tool in self._tools.values():
            if name in tool.aliases:
                return tool
        return None

    def all_names(self) -> List[str]:
        return list(self._tools.keys())

    def _allowed_tool_names(self, allowed_tools: Optional[List[str]] = None) -> Optional[set[str]]:
        if allowed_tools is None:
            return None
        allowed = {str(name or "").strip() for name in allowed_tools if str(name or "").strip()}
        return allowed or set()

    def _allowed_in_run(self, requested_name: str, tool: ToolDefinition, allowed_tools: Optional[List[str]] = None) -> bool:
        allowed = self._allowed_tool_names(allowed_tools)
        if allowed is None:
            return True
        return tool.name in allowed or requested_name in allowed or any(alias in allowed for alias in tool.aliases)

    # ── Schema export ────────────────────────────────────────────

    def _security_context(self, tool_context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        context = dict(tool_context or {})
        trust = get_trust_governor()
        principal = trust.resolve_source_id(
            principal_id=str(context.get("principal_id") or context.get("principal") or ""),
            source=str(context.get("source") or "react"),
            user_id=str(context.get("user_id") or ""),
            session_id=str(context.get("session_id") or context.get("run_id") or ""),
        )
        scope = str(context.get("session_id") or context.get("run_id") or "")
        safe_word = str(context.get("safe_word") or context.get("step_up_code") or "").strip()
        if safe_word:
            step_up_verified = trust.verify_step_up(principal, safe_word, scope=scope)
        else:
            step_up_verified = trust.has_step_up(principal, scope=scope)
        context["principal"] = principal
        context["trust_level"] = int(trust.level(principal))
        context["step_up_verified"] = bool(step_up_verified)
        if get_approval_manager().grants_step_up(context):
            context["step_up_verified"] = True
        return context

    def _is_visible(self, tool: ToolDefinition, *, tool_context: Optional[Dict[str, Any]] = None) -> bool:
        context = self._security_context(tool_context)
        policy = get_tool_policy().check(tool.name, context=context)
        if not policy.allowed or policy.requires_confirmation:
            return False
        source = str(context.get("source") or "react")
        cap_result = get_capability_governor().check(f"tool.{tool.name}", source=source)
        if not cap_result.get("allowed", False):
            return False
        return f"tool:{tool.name}" not in set(get_health_governor().status().get("open_circuits") or [])

    def _visible_tools(self, *, allowed_tools: Optional[List[str]] = None, tool_context: Optional[Dict[str, Any]] = None) -> List[ToolDefinition]:
        tools: List[ToolDefinition] = []
        for tool in self._tools.values():
            if not self._allowed_in_run(tool.name, tool, allowed_tools):
                continue
            if not self._is_visible(tool, tool_context=tool_context):
                continue
            tools.append(tool)
        return tools

    @staticmethod
    def _tool_schema(tool: ToolDefinition) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters or {"type": "object", "properties": {}},
            },
        }

    @classmethod
    def _normalize_schema_node(cls, schema: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        return normalize_schema_node(schema, close_objects=True)

    @classmethod
    def _validate_schema_value(cls, value: Any, schema: Dict[str, Any], *, path: str) -> tuple[Any, str]:
        return validate_payload(value, schema, path=path, close_objects=True)

    @classmethod
    def _validate_tool_arguments(cls, tool: ToolDefinition, args: Dict[str, Any]) -> tuple[Dict[str, Any], str]:
        schema = cls._normalize_schema_node(tool.parameters or {"type": "object", "properties": {}})
        validated, error = validate_payload(args, schema, path="arguments", close_objects=True)
        if error:
            return {}, error
        return dict(validated or {}), ""

    @staticmethod
    def _query_terms(query: str) -> List[str]:
        return [term for term in re.findall(r"[a-z0-9_]{2,}", str(query or "").lower()) if term]

    @staticmethod
    def _tool_categories(tool: ToolDefinition) -> List[str]:
        categories: List[str] = []
        domain = str(tool.domain or "").strip().lower()
        if domain:
            categories.append(domain)
        for tag in tool.tags or []:
            value = str(tag or "").strip().lower()
            if value and value not in categories:
                categories.append(value)
        return categories

    def _tool_relevance_score(self, tool: ToolDefinition, query_terms: List[str]) -> int:
        if not query_terms:
            return 0
        name_terms = set(re.findall(r"[a-z0-9_]{2,}", tool.name.lower()))
        desc_terms = set(re.findall(r"[a-z0-9_]{2,}", tool.description.lower()))
        tag_terms = {str(tag or "").lower() for tag in (tool.tags or [])}
        alias_terms = {str(alias or "").lower() for alias in (tool.aliases or [])}
        categories = set(self._tool_categories(tool))
        score = 0
        for term in query_terms:
            if term == tool.name.lower():
                score += 10
            if term in alias_terms:
                score += 8
            if term in name_terms:
                score += 6
            if term in tag_terms or term in categories:
                score += 4
            if term in desc_terms:
                score += 2
        return score

    @staticmethod
    def _matches_prefix_suffix(tool: ToolDefinition, *, prefix: str = "", suffix: str = "") -> bool:
        prefix_value = str(prefix or "").strip().lower()
        suffix_value = str(suffix or "").strip().lower()
        if not prefix_value and not suffix_value:
            return True
        candidates = [tool.name, *(tool.aliases or [])]
        for candidate in candidates:
            value = str(candidate or "").strip().lower()
            if not value:
                continue
            if prefix_value and not value.startswith(prefix_value):
                continue
            if suffix_value and not value.endswith(suffix_value):
                continue
            return True
        return False

    def schema_names(self, *, allowed_tools: Optional[List[str]] = None, tool_context: Optional[Dict[str, Any]] = None) -> List[str]:
        return [tool.name for tool in self._visible_tools(allowed_tools=allowed_tools, tool_context=tool_context)]

    def tool_category_summary(self, *, allowed_tools: Optional[List[str]] = None, tool_context: Optional[Dict[str, Any]] = None) -> List[tuple[str, int]]:
        counts: Counter[str] = Counter()
        for tool in self._visible_tools(allowed_tools=allowed_tools, tool_context=tool_context):
            for category in self._tool_categories(tool) or ["uncategorized"]:
                counts[category] += 1
        return sorted(counts.items(), key=lambda item: (-item[1], item[0]))

    def _resolve_tool_reference(self, name: str, *, allowed_tools: Optional[List[str]] = None, tool_context: Optional[Dict[str, Any]] = None) -> Optional[ToolDefinition]:
        needle = str(name or "").strip().lower()
        if not needle:
            return None
        visible = self._visible_tools(allowed_tools=allowed_tools, tool_context=tool_context)
        exact = next((tool for tool in visible if tool.name.lower() == needle), None)
        if exact is not None:
            return exact
        alias_match = next(
            (tool for tool in visible if needle in {str(alias or "").strip().lower() for alias in (tool.aliases or [])}),
            None,
        )
        if alias_match is not None:
            return alias_match
        return next(
            (
                tool for tool in visible
                if tool.name.lower().startswith(needle)
                or needle in tool.name.lower()
                or any(needle in str(alias or "").strip().lower() for alias in (tool.aliases or []))
            ),
            None,
        )

    def tool_schema_summary(self, name: str, *, allowed_tools: Optional[List[str]] = None, tool_context: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        tool = self._resolve_tool_reference(name, allowed_tools=allowed_tools, tool_context=tool_context)
        if tool is None:
            return None
        return {
            "name": tool.name,
            "description": tool.description,
            "categories": self._tool_categories(tool) or ["uncategorized"],
            "parameters": tool.parameters or {"type": "object", "properties": {}},
        }

    def discover_tools(
        self,
        query: str,
        *,
        allowed_tools: Optional[List[str]] = None,
        tool_context: Optional[Dict[str, Any]] = None,
        category: str = "",
        prefix: str = "",
        suffix: str = "",
        max_results: int = 8,
    ) -> List[ToolDefinition]:
        visible = self._visible_tools(allowed_tools=allowed_tools, tool_context=tool_context)
        category_value = str(category or "").strip().lower()
        if category_value:
            visible = [tool for tool in visible if category_value in self._tool_categories(tool)]
        if prefix or suffix:
            visible = [tool for tool in visible if self._matches_prefix_suffix(tool, prefix=prefix, suffix=suffix)]
        query_terms = self._query_terms(query)
        ranked = sorted(visible, key=lambda tool: (-self._tool_relevance_score(tool, query_terms), tool.name))
        return ranked[: max(1, min(int(max_results or 8), 12))]

    def discovery_tool_schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": DISCOVERY_TOOL_NAME,
                "description": "Search the currently available tool catalog and reveal matching tools before calling them.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Capability or action to find."},
                        "category": {"type": "string", "description": "Optional category/tag filter."},
                        "prefix": {"type": "string", "description": "Optional tool-name prefix filter."},
                        "suffix": {"type": "string", "description": "Optional tool-name suffix filter."},
                        "max_results": {"type": "integer", "description": "Maximum matches to reveal.", "minimum": 1, "maximum": 12},
                    },
                },
            },
        }

    def schema_description_tool_schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": SCHEMA_TOOL_NAME,
                "description": "Load the exact parameter schema for a discovered visible tool before calling it.",
                "parameters": {
                    "type": "object",
                    "properties": {"name": {"type": "string", "description": "Tool name or alias to describe."}},
                    "required": ["name"],
                },
            },
        }

    def schema_list_filtered(
        self,
        query: str = "",
        *,
        allowed_tools: Optional[List[str]] = None,
        tool_context: Optional[Dict[str, Any]] = None,
        max_tools: int = 24,
    ) -> List[Dict[str, Any]]:
        visible = self._visible_tools(allowed_tools=allowed_tools, tool_context=tool_context)
        limit = max(1, int(max_tools or 24))
        if len(visible) <= limit:
            return [self._tool_schema(tool) for tool in visible]
        query_terms = self._query_terms(query)
        ranked = sorted(visible, key=lambda tool: (-self._tool_relevance_score(tool, query_terms), tool.name))
        return [self._tool_schema(tool) for tool in ranked[:limit]]

    def schemas(
        self,
        *,
        allowed_tools: Optional[List[str]] = None,
        tool_context: Optional[Dict[str, Any]] = None,
        query: str = "",
        max_tools: int = 0,
    ) -> List[Dict[str, Any]]:
        if max_tools:
            return self.schema_list_filtered(query, allowed_tools=allowed_tools, tool_context=tool_context, max_tools=max_tools)
        out: List[Dict[str, Any]] = []
        for tool in self._visible_tools(allowed_tools=allowed_tools, tool_context=tool_context):
            out.append(self._tool_schema(tool))
        return out

    # ── Execution ────────────────────────────────────────────────

    async def execute(
        self,
        name: str,
        args: Optional[Dict[str, Any]] = None,
        *,
        allowed_tools: Optional[List[str]] = None,
        tool_context: Optional[Dict[str, Any]] = None,
    ) -> ToolResult:
        if name == DISCOVERY_TOOL_NAME:
            if allowed_tools is not None and name not in set(allowed_tools):
                return ToolResult(tool_name=name, success=False, output="", error=f"tool '{name}' is not allowed in this run")
            payload = dict(args or {})
            query = str(payload.get("query") or "").strip()
            category = str(payload.get("category") or "").strip().lower()
            prefix = str(payload.get("prefix") or "").strip()
            suffix = str(payload.get("suffix") or "").strip()
            try:
                max_results = int(payload.get("max_results") or 8)
            except Exception:
                max_results = 8
            max_results = max(1, min(max_results, 12))
            if not any((query, category, prefix, suffix)):
                lines = ["Available tool categories:"]
                for category_name, count in self.tool_category_summary(allowed_tools=allowed_tools, tool_context=tool_context)[:12]:
                    lines.append(f"- {category_name}: {count} tool(s)")
                lines.append("Search again with query, category, prefix, or suffix to reveal specific tools.")
                return ToolResult(tool_name=name, success=True, output="\n".join(lines), duration_ms=0.0)
            matches = self.discover_tools(query, allowed_tools=allowed_tools, tool_context=tool_context, category=category, prefix=prefix, suffix=suffix, max_results=max_results)
            filter_parts = []
            if query:
                filter_parts.append(f"query={query!r}")
            if category:
                filter_parts.append(f"category={category!r}")
            if prefix:
                filter_parts.append(f"prefix={prefix!r}")
            if suffix:
                filter_parts.append(f"suffix={suffix!r}")
            lines = [f"Found {len(matches)} tool(s) for {', '.join(filter_parts)}:"]
            for tool in matches:
                categories = ", ".join(self._tool_categories(tool) or ["uncategorized"])
                lines.append(f"- {tool.name}: {tool.description} | categories: {categories}")
            return ToolResult(tool_name=name, success=True, output="\n".join(lines), duration_ms=0.0)
        if name == SCHEMA_TOOL_NAME:
            if allowed_tools is not None and name not in set(allowed_tools):
                return ToolResult(tool_name=name, success=False, output="", error=f"tool '{name}' is not allowed in this run")
            tool_name = str((args or {}).get("name") or "").strip()
            summary = self.tool_schema_summary(tool_name, allowed_tools=allowed_tools, tool_context=tool_context)
            if summary is None:
                return ToolResult(tool_name=name, success=False, output="", error=f"no visible tool matched {tool_name!r}", duration_ms=0.0)
            return ToolResult(tool_name=name, success=True, output=json.dumps(summary, default=str), duration_ms=0.0)
        tool = self.get(name)
        if tool is None:
            return ToolResult(tool_name=name, success=False, output="", error=f"unknown tool: {name}")
        if not self._allowed_in_run(name, tool, allowed_tools):
            return ToolResult(tool_name=tool.name, success=False, output="", error=f"tool '{tool.name}' is not allowed in this run")
        tool_context = self._security_context(tool_context)
        if get_approval_manager().maybe_approve(tool.name, tool_context):
            tool_context["confirmation_granted"] = True
        policy = get_tool_policy().check(tool.name, context=tool_context)
        if not policy.allowed:
            if policy.requires_confirmation:
                return ToolResult(tool_name=tool.name, success=False, output="", error=f"tool policy requires confirmation for '{tool.name}': {policy.reason}")
            return ToolResult(tool_name=tool.name, success=False, output="", error=f"tool policy denied '{tool.name}': {policy.reason}")
        source = str(tool_context.get("source") or "react")
        capability = f"tool.{tool.name}"
        cap_result = get_capability_governor().check(capability, source=source)
        if not cap_result.get("allowed", False):
            return ToolResult(tool_name=tool.name, success=False, output="", error=f"capability denied '{capability}': {cap_result.get('reason', '')}")
        health_name = f"tool:{tool.name}"
        if not get_health_governor().allow_call(health_name):
            return ToolResult(tool_name=tool.name, success=False, output="", error=f"tool circuit open: {tool.name}")
        args = dict(args or {})
        args, validation_error = self._validate_tool_arguments(tool, args)
        if validation_error:
            return ToolResult(tool_name=tool.name, success=False, output="", error=f"invalid arguments for '{tool.name}': {validation_error}")
        ledger_args = dict(args)
        session_id = str(tool_context.get("session_id") or tool_context.get("run_id") or "default")
        run_id = str(tool_context.get("run_id") or session_id)
        get_tool_ledger().record_start(
            session_id=session_id,
            tool_name=tool.name,
            arguments=ledger_args,
            run_id=run_id,
            principal=str(tool_context.get("principal") or ""),
            source=str(tool_context.get("source") or ""),
            trust_level=int(tool_context.get("trust_level") or 0),
            metadata={
                "project_id": str(tool_context.get("project_id") or ""),
                "card_id": str(tool_context.get("card_id") or ""),
                "task_id": str(tool_context.get("task_id") or ""),
                "intent_text": str(tool_context.get("intent_text") or "")[:240],
                "target_files": list(tool_context.get("target_files") or [])[:8],
            },
        )
        if tool_context:
            args["_tool_runtime"] = tool_context
        started = time.monotonic()
        try:
            handler = tool.handler
            result = handler(args)
            if inspect.isawaitable(result):
                result = await asyncio.wait_for(result, timeout=tool.timeout_s)
            output = result if isinstance(result, str) else str(result)
            duration_ms = (time.monotonic() - started) * 1000
            mutated, verified, paths, details = self._mutation_state_for_result(tool, args, output)
            get_health_governor().record_call(health_name, success=True, duration_ms=duration_ms)
            get_usage_tracker().record_tool_call(tool_name=tool.name, success=True, elapsed_ms=duration_ms)
            get_tool_ledger().record_finish(
                session_id=session_id,
                tool_name=tool.name,
                success=True,
                output=output,
                elapsed_ms=duration_ms,
            )
            return ToolResult(
                tool_name=tool.name,
                success=True,
                output=output,
                duration_ms=duration_ms,
                mutated_workspace=mutated,
                mutation_verified=verified,
                mutation_paths=paths,
                mutation_details=details,
            )
        except asyncio.TimeoutError:
            duration_ms = (time.monotonic() - started) * 1000
            get_health_governor().record_call(health_name, success=False, duration_ms=duration_ms)
            get_usage_tracker().record_tool_call(tool_name=tool.name, success=False, elapsed_ms=duration_ms, timed_out=True)
            get_tool_ledger().record_timeout(session_id=session_id, tool_name=tool.name, elapsed_ms=duration_ms)
            return ToolResult(
                tool_name=tool.name,
                success=False,
                output="",
                error=f"tool '{tool.name}' timed out after {tool.timeout_s}s",
                duration_ms=duration_ms,
            )
        except Exception as exc:
            duration_ms = (time.monotonic() - started) * 1000
            get_health_governor().record_call(health_name, success=False, duration_ms=duration_ms)
            get_usage_tracker().record_tool_call(tool_name=tool.name, success=False, elapsed_ms=duration_ms)
            err = describe_error(exc, "tool error")
            get_tool_ledger().record_finish(
                session_id=session_id,
                tool_name=tool.name,
                success=False,
                error=err,
                elapsed_ms=duration_ms,
            )
            return ToolResult(
                tool_name=tool.name,
                success=False,
                output="",
                error=err,
                duration_ms=duration_ms,
            )

    def _mutation_state_for_result(self, tool: ToolDefinition, args: Dict[str, Any], output: str) -> tuple[bool, bool, List[str], str]:
        mode = str(tool.mutation_mode or "none").strip().lower()
        if mode == "none":
            return False, False, [], ""
        payload = dict(args or {})
        path = str(payload.get("path") or payload.get("cwd") or "").strip()
        paths = [path] if path else []
        try:
            parsed = json.loads(output or "{}")
        except Exception:
            return True, False, paths, "Tool mutated workspace but returned non-JSON output."
        if not isinstance(parsed, dict):
            return True, False, paths, "Tool mutated workspace but returned non-object JSON output."
        if parsed.get("path") and not paths:
            paths = [str(parsed.get("path"))]
        if parsed.get("error"):
            return True, False, paths, str(parsed.get("error") or "")
        verified = bool(parsed.get("verified") or parsed.get("deleted"))
        details = "Verified by tool output." if verified else "Tool mutated workspace but did not report verified postconditions."
        return True, verified, paths, details
