"""Chat endpoint — routes user messages to the planner provider and emits cards."""
from __future__ import annotations

import json
import re
import time
import asyncio
from types import SimpleNamespace
from typing import Any, AsyncGenerator, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from blackboard.coding.adaptive_skills import synthesize_adaptive_skills
from blackboard.coding.skill_promotion import SkillPromotionGate
from blackboard.kernel.json_schema import build_response_format, parse_json_payload, validate_payload
from blackboard.kernel.logger import get_logger
from blackboard.kernel.prompts import get_prompts
from blackboard.providers.base import AIProvider, LLMResponse, Message
from blackboard.providers.registry import get_provider_registry
from blackboard.providers.usage import call_and_record_provider
from blackboard.react.loop import ReActLoop
from blackboard.react.tool_registry import ToolRegistry
from blackboard.react.tools.registry_builder import build_default_registry
from blackboard.react.tools.web_tools import _distill_search_query
from blackboard.workspace.board import BoardService, COLUMNS
from blackboard.workspace.chat_attachments import persist_chat_images, register_chat_images, resolve_chat_attachment_path
from blackboard.workspace.chat_context import ChatContextOrganizer
from blackboard.workspace.chat_intent import detect_chat_intent
from blackboard.workspace.chat_scratchboard import ChatScratchboardStore
from blackboard.workspace.message_ledger import MessageLedger
from blackboard.workspace.redaction import sanitize_text
from blackboard.workspace.sessions import SessionMessage, SessionStore
from blackboard.workspace.temporal_scratchpad import TemporalScratchpadStore
from blackboard.workspace.turn_state import TurnStateStore

logger = get_logger("api.chat")
router = APIRouter(prefix="/api/chat", tags=["chat"])
_STREAM_HEARTBEAT_SECONDS = 8.0

_CARD_SCHEMA_BLOCK = (
    "```cards\n"
    "{{\"create\":[{{\"title\":\"...\",\"body\":\"short board note for why this card belongs in the current status\",\"objective\":\"full execution objective for the coding worker\",\"status\":\"inbox\",\"files\":[],\"verification\":[],\"constraints\":[]}}],"
    "\"update\":[{{\"card_id\":\"card_123\",\"target_title\":\"\",\"title\":\"...\",\"body\":\"short board note for why this card belongs in the current status\",\"objective\":\"full execution objective for the coding worker\",\"status\":\"ready\",\"files\":[],\"verification\":[],\"constraints\":[],\"deps\":[],\"tags\":[]}}],"
    "\"delete\":[{{\"card_id\":\"card_123\",\"target_title\":\"...\"}}]}}\n"
    "```"
)

_CARD_RULES_BLOCK = (
    "Use board mutations when the user asks to add, change, move, rename, cancel, delete, "
    "or otherwise manage cards/tasks. For new work, put cards in `create`. For modifying an "
    "existing card, put entries in `update`. For removing/canceling cards, put entries in `delete`. "
    "For a small implementation request, create ONE complete card unless the user asks for a breakdown. "
    "Do not split one small website into separate HTML/CSS/JS cards. When mutating existing cards, prefer "
    "`card_id` from context; otherwise use exact `target_title`. Wrap board mutations in a fenced ```cards``` "
    "JSON block with this exact shape and include a short conversational reply BEFORE the block explaining "
    "what you're changing:\n"
    f"{_CARD_SCHEMA_BLOCK}\n"
    "Never guess which existing card to mutate. If multiple cards could match, or the target is unclear, "
    "ask a clarifying question in plain text and do not emit a mutation block. When the user refers to `that`, "
    "`those`, or recent cards, use the recent board target context and prefer exact `card_id`. "
    "New card bodies must be specific enough to run as coding jobs. Include target files "
    "relative to the selected job working directory and verification steps. Use `body` for a short human-readable "
    "board note explaining why the card is in its current status, and use `objective` for the full execution goal. Do not prefix "
    "file paths with blackboard/workspace when the workspace itself is the target root."
)

_ARTIFACT_RULES_BLOCK = (
    "When the user asks for a small artifact, demo, prototype, mini app, HTML page, landing-page mock, "
    "or something they can save/open immediately in chat, return a short conversational intro followed by exactly one fenced ```html block. "
    "That HTML must be self-contained with inline CSS and inline JavaScript only, with no external dependencies. "
    "Keep scripts scoped to the document itself, avoid top-level navigation/popups, and size the artifact so it previews well in a small embedded frame. "
    "Do not wrap the HTML in JSON. Do not emit multiple competing code blocks when one self-contained HTML artifact will do."
)

_IMAGE_RULES_BLOCK = (
    "If you are returning an image that should appear directly in chat, emit it in a chat-renderable form: "
    "use Markdown image syntax like ![alt text](https://...) or a direct data:image/... URL when appropriate. "
    "Do not wrap chat images in JSON. Avoid raw HTML unless you must include a plain <img src=...> tag. "
    "Include a brief conversational sentence before or after the image when useful."
)

_TOOL_CALL_ROBUSTNESS_BLOCK = (
    "When you decide to use a tool, emit exactly one valid tool call at a time with a real tool name and arguments that match the schema. "
    "Arguments must be a JSON object, not prose, not pseudo-code, not Python, not key=value text, and not wrapped in markdown fences. "
    "Do not invent fields that are not in the tool schema. Prefer the smallest valid argument object that can succeed. "
    "If native function calling is unavailable or starts failing, output a fallback fenced ```tool_call block containing JSON with this exact shape: "
    '{"name":"tool_name","arguments":{},"reason":"optional short why","confidence":0.0,"retry_hint":"optional short hint"}' 
    "Do not include explanation text inside that block. Use only one fallback tool_call block per assistant turn."
)

_PLANNER_TEMPLATE = (
    "{system}\n\n"
    "You are Blackboard's project assistant. Your default behaviour is to chat "
    "conversationally — answer questions, discuss the project, give advice. "
    "You DO NOT need to create cards for every message.\n\n"
    f"{_CARD_RULES_BLOCK}\n\n"
    f"{_ARTIFACT_RULES_BLOCK}\n\n"
    f"{_IMAGE_RULES_BLOCK}\n\n"
    "If the user is just chatting (asking questions, exploring ideas, debugging), "
    "reply with plain text only — no cards, no JSON.\n\n"
    "User request:\n{message}\n\n"
    "Existing project context:\n{context}\n\n"
    "{live_web_context_block}"
)

_CARDS_REPAIR_TEMPLATE = (
    "{system}\n\n"
    "The user explicitly asked for board changes, tasks, or implementation work, "
    "but the previous reply did not include the required fenced ```cards``` JSON block. "
    "Return ONLY a fenced ```cards``` JSON block using create/update/delete entries with this exact shape and no prose "
    "before or after:\n"
    f"{_CARD_SCHEMA_BLOCK}\n\n"
    "User request:\n{message}\n\n"
    "Previous assistant reply:\n{reply}\n\n"
    "Existing project context:\n{context}"
)

_CARDS_APPLY_REPAIR_TEMPLATE = (
    "{system}\n\n"
    "The previous fenced ```cards``` JSON block parsed, but Blackboard could not safely apply it. "
    "Repair the board mutation by using the error details and current board context. "
    "Return ONLY a fenced ```cards``` JSON block using create/update/delete entries with this exact shape and no prose "
    "before or after:\n"
    f"{_CARD_SCHEMA_BLOCK}\n\n"
    "User request:\n{message}\n\n"
    "Visible assistant reply:\n{reply}\n\n"
    "Apply error JSON:\n{error_json}\n\n"
    "Current board context:\n{board_context}\n\n"
    "Existing project context:\n{context}"
)


_CARDS_FENCE_RE = re.compile(r"```cards\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)
_HTML_FENCE_RE = re.compile(r"```html[^\S\r\n]*\r?\n([\s\S]*?)```", re.IGNORECASE)
_PLAIN_CHAT_RE = re.compile(
    r"^\s*(hi|hello|hey|yo|sup|thanks|thank you|ok|okay|cool|nice|great|gm|gn|good morning|good night)[\s!.?]*$",
    re.IGNORECASE,
)
_LIVE_WEB_QUERY_RE = re.compile(
    r"\b(latest|current|today|tonight|right now|recent|recently|news|online|internet|web|browse|search|look up|lookup|check|stream|release|episode)\b",
    re.IGNORECASE,
)
_LIVE_WEB_PROVENANCE_RE = re.compile(r"^Web provenance JSON:\s*(\{.*\})$", re.MULTILINE)
_MAX_GENERATED_CARDS = 12
_MAX_CARD_TITLE_CHARS = 140
_MAX_CARD_BODY_CHARS = 1600
_MAX_CARD_OBJECTIVE_CHARS = 2000
_MAX_CARD_LIST_ITEMS = 12
_MAX_CARD_LIST_ITEM_CHARS = 240
_MAX_MUTATION_TARGET_CHARS = 140
_CHAT_REACT_TOOL_GROUPS = ["file_ops", "search", "web", "browser", "wiki", "introspection"]
_CHAT_REACT_ALLOWED_TOOLS = [
    "read_file",
    "list_dir",
    "search_files",
    "search_code",
    "search_multi",
    "fetch_url",
    "web_search",
    "web_news",
    "web_research",
    "search_github",
    "search_stackoverflow",
    "search_documentation",
    "search_tutorials",
    "browse",
    "browse_search",
    "browse_inspect",
    "browse_click",
    "browse_type",
    "browse_scroll",
    "browse_read",
    "browse_links",
    "browse_extract",
    "wait_for_element",
    "get_page_html",
    "check_console_errors",
    "check_network_errors",
    "check_page_health",
    "eval_js",
    "wiki_search",
    "wiki_read",
    "wiki_stats",
    "wiki_health",
    "tool_status",
]
_CHAT_REACT_STREAM_CHUNK_CHARS = 280

_CARD_STATUS_ENUM = sorted(str(column or "").strip() for column in COLUMNS if str(column or "").strip())
_CARD_LIST_SCHEMA: Dict[str, Any] = {
    "type": "array",
    "items": {"type": "string", "maxLength": _MAX_CARD_LIST_ITEM_CHARS},
    "maxItems": _MAX_CARD_LIST_ITEMS,
}
_CARD_CREATE_ENTRY_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "maxLength": _MAX_CARD_TITLE_CHARS},
        "body": {"type": "string", "maxLength": _MAX_CARD_BODY_CHARS},
        "objective": {"type": "string", "maxLength": _MAX_CARD_OBJECTIVE_CHARS},
        "status": {"type": "string", "enum": _CARD_STATUS_ENUM},
        "files": _CARD_LIST_SCHEMA,
        "verification": _CARD_LIST_SCHEMA,
        "constraints": _CARD_LIST_SCHEMA,
        "deps": _CARD_LIST_SCHEMA,
        "tags": _CARD_LIST_SCHEMA,
    },
    "additionalProperties": False,
}
_CARD_UPDATE_ENTRY_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "card_id": {"type": "string", "maxLength": _MAX_MUTATION_TARGET_CHARS},
        "target_title": {"type": "string", "maxLength": _MAX_MUTATION_TARGET_CHARS},
        "current_title": {"type": "string", "maxLength": _MAX_MUTATION_TARGET_CHARS},
        "existing_title": {"type": "string", "maxLength": _MAX_MUTATION_TARGET_CHARS},
        "title": {"type": "string", "maxLength": _MAX_CARD_TITLE_CHARS},
        "body": {"type": "string", "maxLength": _MAX_CARD_BODY_CHARS},
        "objective": {"type": "string", "maxLength": _MAX_CARD_OBJECTIVE_CHARS},
        "job_id": {"type": "string", "maxLength": _MAX_MUTATION_TARGET_CHARS},
        "status": {"type": "string", "enum": _CARD_STATUS_ENUM},
        "progress": {"type": "integer", "minimum": 0, "maximum": 100},
        "files": _CARD_LIST_SCHEMA,
        "verification": _CARD_LIST_SCHEMA,
        "constraints": _CARD_LIST_SCHEMA,
        "deps": _CARD_LIST_SCHEMA,
        "tags": _CARD_LIST_SCHEMA,
    },
    "additionalProperties": False,
}
_CARD_DELETE_ENTRY_SCHEMA: Dict[str, Any] = {
    "anyOf": [
        {"type": "string", "maxLength": _MAX_MUTATION_TARGET_CHARS},
        {
            "type": "object",
            "properties": {
                "card_id": {"type": "string", "maxLength": _MAX_MUTATION_TARGET_CHARS},
                "target_title": {"type": "string", "maxLength": _MAX_MUTATION_TARGET_CHARS},
                "title": {"type": "string", "maxLength": _MAX_MUTATION_TARGET_CHARS},
                "current_title": {"type": "string", "maxLength": _MAX_MUTATION_TARGET_CHARS},
            },
            "additionalProperties": False,
        },
    ],
}
_CARD_MUTATIONS_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "cards": {"type": "array", "items": _CARD_CREATE_ENTRY_SCHEMA, "maxItems": _MAX_GENERATED_CARDS},
        "create": {"type": "array", "items": _CARD_CREATE_ENTRY_SCHEMA, "maxItems": _MAX_GENERATED_CARDS},
        "update": {"type": "array", "items": _CARD_UPDATE_ENTRY_SCHEMA, "maxItems": _MAX_GENERATED_CARDS},
        "delete": {"type": "array", "items": _CARD_DELETE_ENTRY_SCHEMA, "maxItems": _MAX_GENERATED_CARDS},
    },
    "additionalProperties": False,
}


def _message_requests_live_web_context(message: str) -> bool:
    value = str(message or "").strip()
    if not value:
        return False
    if not _LIVE_WEB_QUERY_RE.search(value):
        return False
    return bool(re.search(r"\b(check|find|look up|lookup|browse|search|latest|current|today|recent|news|online|internet|episode|stream|release|what(?:'s| is)|when|where)\b", value, re.IGNORECASE))


def _live_web_provenance_from_payload(parsed: Dict[str, Any], *, tool_name: str, attempted_tools: Optional[List[str]] = None, success: bool = True) -> Dict[str, Any]:
    provenance: Dict[str, Any] = {
        "tool": str(tool_name or "").strip(),
        "success": bool(success),
    }
    attempts = [str(item or "").strip() for item in (attempted_tools or []) if str(item or "").strip()]
    if attempts:
        provenance["attempted_tools"] = attempts[:6]
    query = sanitize_text(str(parsed.get("query") or ""), max_chars=240).strip()
    if query:
        provenance["query"] = query
    search_backend = sanitize_text(str(parsed.get("search_backend") or parsed.get("backend") or ""), max_chars=80).strip()
    if search_backend:
        provenance["search_backend"] = search_backend
    fetch_backends: List[str] = []
    source_urls: List[str] = []
    sources_compact: List[Dict[str, str]] = []
    source_items = list(parsed.get("sources") or parsed.get("results") or [])
    if not source_items and parsed.get("url"):
        source_items = [{
            "title": str(parsed.get("url") or ""),
            "url": str(parsed.get("url") or ""),
            "backend": str(parsed.get("backend") or ""),
        }]
    seen_urls = set()
    for item in source_items[:6]:
        if not isinstance(item, dict):
            continue
        title = sanitize_text(str(item.get("title") or item.get("url") or "source"), max_chars=120).strip()
        url = sanitize_text(str(item.get("url") or ""), max_chars=320).strip()
        backend = sanitize_text(str(item.get("backend") or ""), max_chars=80).strip()
        if backend and backend not in fetch_backends:
            fetch_backends.append(backend)
        if url and url not in seen_urls:
            seen_urls.add(url)
            source_urls.append(url)
        compact: Dict[str, str] = {}
        if title:
            compact["title"] = title
        if url:
            compact["url"] = url
        if backend:
            compact["backend"] = backend
        if compact:
            sources_compact.append(compact)
    if fetch_backends:
        provenance["fetch_backends"] = fetch_backends[:6]
    if source_urls:
        provenance["source_urls"] = source_urls[:6]
    if sources_compact:
        provenance["sources"] = sources_compact[:4]
    return provenance


def _extract_live_web_provenance_from_block(block: str) -> Dict[str, Any]:
    match = _LIVE_WEB_PROVENANCE_RE.search(str(block or ""))
    if not match:
        return {}
    try:
        parsed = json.loads(match.group(1))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _append_live_web_provenance_reply(reply: str, provenance: Dict[str, Any]) -> str:
    if not isinstance(provenance, dict) or not provenance or not provenance.get("success", False):
        return str(reply or "").strip()
    tool = sanitize_text(str(provenance.get("tool") or ""), max_chars=80).strip()
    search_backend = sanitize_text(str(provenance.get("search_backend") or ""), max_chars=80).strip()
    urls = [sanitize_text(str(item or ""), max_chars=320).strip() for item in list(provenance.get("source_urls") or [])]
    urls = [item for item in urls if item][:3]
    if not tool and not search_backend and not urls:
        return str(reply or "").strip()
    lines = [str(reply or "").strip(), "", "Live lookup provenance:"]
    if tool:
        lines.append(f"- Tool: {tool}")
    if search_backend:
        lines.append(f"- Search backend: {search_backend}")
    for url in urls:
        lines.append(f"- Source: {url}")
    return "\n".join(line for line in lines if line).strip()


def _format_live_web_context(raw: str, *, tool_name: str = "", attempted_tools: Optional[List[str]] = None, success: bool = True) -> str:
    try:
        parsed = json.loads(raw or "{}")
    except Exception:
        return ""
    if not isinstance(parsed, dict) or parsed.get("error"):
        return ""
    provenance = _live_web_provenance_from_payload(parsed, tool_name=tool_name, attempted_tools=attempted_tools, success=success)
    query = sanitize_text(str(parsed.get("query") or ""), max_chars=240).strip()
    summary = sanitize_text(str(parsed.get("summary") or ""), max_chars=2400).strip()
    sources = list(parsed.get("sources") or parsed.get("results") or [])
    lines: List[str] = []
    if provenance:
        lines.append(f"Web provenance JSON: {json.dumps(provenance, ensure_ascii=False, separators=(',', ':'))}")
    if query:
        lines.append(f"Query: {query}")
    if summary:
        lines.extend(["Summary:", summary])
    if sources:
        lines.append("Sources:")
        for item in sources[:4]:
            if not isinstance(item, dict):
                continue
            title = sanitize_text(str(item.get("title") or item.get("url") or "source"), max_chars=160).strip()
            url = sanitize_text(str(item.get("url") or ""), max_chars=320).strip()
            snippet = sanitize_text(str(item.get("snippet") or item.get("excerpt") or ""), max_chars=220).strip()
            if not title and not url:
                continue
            line = f"- {title}"
            if url:
                line += f" — {url}"
            lines.append(line)
            if snippet:
                lines.append(f"  {snippet}")
    return "\n".join(line for line in lines if line).strip()


async def _chat_live_web_context_block(message: str, *, project_id: str, session_id: str) -> str:
    if not _message_requests_live_web_context(message):
        return ""
    tool_registry = build_default_registry(enabled=["web"])
    tool_context = {
        "source": "react",
        "project_id": str(project_id or ""),
        "session_id": str(session_id or "chat"),
        "run_id": f"chat-web:{project_id or 'project'}:{session_id or 'session'}",
        "intent_text": str(message or "")[:400],
    }
    search_query = _distill_search_query(message)
    freshness_hint = bool(re.search(r"\b(latest|current|today|tonight|recent|recently|news|right now|newest)\b", str(message or ""), re.IGNORECASE))
    attempts = []
    if freshness_hint:
        attempts.append(("web_news", {"query": search_query, "timelimit": "w", "max_results": 5}))
    attempts.extend([
        ("web_research", {"query": search_query, "max_sources": 3, "max_chars": 5000}),
        ("web_search", {"query": search_query, "max_results": 5}),
    ])
    attempted_tools: List[str] = []
    for tool_name, args in attempts:
        attempted_tools.append(tool_name)
        result = await tool_registry.execute(tool_name, args, tool_context=tool_context)
        if not result.success:
            continue
        formatted = _format_live_web_context(result.output, tool_name=tool_name, attempted_tools=attempted_tools, success=True)
        if not formatted:
            continue
        return (
            "LIVE WEB RESEARCH\n"
            "This context was collected just now from the web for the current request. Use it for current facts and do not say you cannot browse when this block is present. Cite the source URLs or domains you rely on when practical.\n\n"
            f"{formatted}"
        )
    failure_provenance = {
        "tool": "",
        "success": False,
        "attempted_tools": attempted_tools[:6],
        "query": sanitize_text(str(message or ""), max_chars=240).strip(),
    }
    return (
        "LIVE WEB RESEARCH\n"
        "A fresh web lookup was attempted for this request but did not return usable results for this turn. If you mention this limitation, describe it as a temporary lookup failure instead of claiming you cannot browse in general.\n\n"
        f"Web provenance JSON: {json.dumps(failure_provenance, ensure_ascii=False, separators=(',', ':'))}"
    )


def _provider_supports_complete(provider: AIProvider) -> bool:
    impl = getattr(type(provider), "complete", None)
    return callable(getattr(provider, "complete", None)) and impl is not AIProvider.complete


def _provider_supports_stream(provider: AIProvider) -> bool:
    impl = getattr(type(provider), "stream", None)
    return callable(getattr(provider, "stream", None)) and impl is not AIProvider.stream


def _coerce_llm_response(response: Any) -> LLMResponse:
    if isinstance(response, LLMResponse):
        return response
    raw = getattr(response, "raw", {})
    if not isinstance(raw, dict):
        raw = {}
    tool_calls = getattr(response, "tool_calls", []) or []
    if not isinstance(tool_calls, list):
        tool_calls = []
    return LLMResponse(
        content=str(getattr(response, "content", "") or ""),
        model=str(getattr(response, "model", "") or ""),
        tokens_prompt=int(getattr(response, "tokens_prompt", 0) or 0),
        tokens_completion=int(getattr(response, "tokens_completion", 0) or 0),
        finish_reason=str(getattr(response, "finish_reason", "stop") or "stop"),
        tool_calls=tool_calls,
        raw=raw,
    )


def _chat_react_system_prompt(system_prompt: str) -> str:
    return "\n\n".join(part for part in [
        str(system_prompt or "").strip(),
        "You are Blackboard's project assistant. Default behaviour is conversational help grounded in the project and available tools.",
        _CARD_RULES_BLOCK,
        _ARTIFACT_RULES_BLOCK,
        _IMAGE_RULES_BLOCK,
        _TOOL_CALL_ROBUSTNESS_BLOCK,
        "You have tools for workspace inspection, durable wiki lookup, and live web browsing. When a question needs current or web information, use those tools instead of saying you cannot browse.",
        "Use tools when they materially improve accuracy. Prefer read-only investigation and grounded answers. Do not invent tool results.",
        "For web lookups, prefer `web_search`, `web_news`, `web_research`, and `fetch_url` first. Use `browse*` tools only when search results are insufficient or a page clearly requires interactive/browser rendering.",
        "When interactive browsing is needed, prefer `browse_research_path` for multi-step website exploration, on-page sub-searching, and result opening with retries. Use the manual progression `browse`/`browse_search` -> `browse_type_search` -> `browse_open_result` or `browse_click` -> `browse_read`/`browse_links`/`browse_extract` when you need more control. Use `browse_inspect` when you must target a specific element.",
        "When a `LIVE WEB RESEARCH` block is present, ground current claims in it and cite the specific source URL or source domain you relied on when practical.",
        "If the user is just chatting, exploring, or debugging, reply with plain text only. Only emit a fenced ```cards``` block when the user actually wants board mutations.",
    ] if str(part or "").strip())


def _chat_react_extra_context(context: str, live_web_context_block: str) -> str:
    sections = [f"Existing project context:\n{context or '(no project intelligence yet)'}"]
    if str(live_web_context_block or "").strip():
        sections.append(str(live_web_context_block).strip())
    return "\n\n".join(sections)


def _chat_request_id(body: "ChatRequest") -> str:
    suffix = str(body.client_message_id or body.session_id or f"turn-{int(time.time() * 1000)}").strip() or f"turn-{int(time.time() * 1000)}"
    return f"chat:{body.project_id}:{suffix}"


def _chat_previous_response_store(request: Request) -> Dict[str, str]:
    store = getattr(request.app.state, "chat_previous_response_ids", None)
    if store is None:
        store = {}
        request.app.state.chat_previous_response_ids = store
    return store


def _chat_previous_response_key(body: "ChatRequest") -> str:
    return f"{body.project_id}:{body.session_id}"


def _provider_fields(provider_id: Any = "", provider_model: Any = "") -> Dict[str, str]:
    payload: Dict[str, str] = {}
    provider_id_text = str(provider_id or "").strip()
    provider_model_text = str(provider_model or "").strip()
    if provider_id_text:
        payload["provider_id"] = provider_id_text
    if provider_model_text:
        payload["provider_model"] = provider_model_text
    return payload


def _chat_tool_registry() -> ToolRegistry:
    return build_default_registry(enabled=list(_CHAT_REACT_TOOL_GROUPS))


def _chunk_stream_text(text: str, *, max_chars: int = _CHAT_REACT_STREAM_CHUNK_CHARS) -> List[str]:
    remaining = str(text or "")
    chunks: List[str] = []
    while remaining:
        if len(remaining) <= max_chars:
            chunks.append(remaining)
            break
        cut = remaining.rfind("\n", 0, max_chars + 1)
        if cut < max_chars // 2:
            cut = remaining.rfind(" ", 0, max_chars + 1)
        if cut < max_chars // 2:
            cut = max_chars
        chunk = remaining[:cut]
        chunks.append(chunk)
        remaining = remaining[cut:].lstrip(" \n")
    return [chunk for chunk in chunks if chunk]


async def _run_chat_react_with_provider(
    provider: AIProvider,
    *,
    request: Request,
    body: "ChatRequest",
    project_root: str,
    context: str,
    system_prompt: str,
    live_web_context_block: str,
    step_callback=None,
) -> Dict[str, Any]:
    request_id = _chat_request_id(body)

    async def _complete(messages, **kwargs):
        return _coerce_llm_response(await call_and_record_provider(
            provider,
            role="planner",
            session_id=request_id,
            call=lambda: provider.complete(messages, **kwargs),
            record_health=False,
        ))

    loop_provider = SimpleNamespace(
        complete=_complete,
        id=str(getattr(provider, "id", "") or ""),
        model=str(getattr(provider, "model", "") or ""),
    )
    loop = ReActLoop(
        loop_provider,
        _chat_tool_registry(),
        max_iterations=int(request.app.state.config.get("react.chat_max_iterations", 8)),
        system_prompt=_chat_react_system_prompt(system_prompt),
    )
    loop._cache_stable_prefix = True
    coding_worker = getattr(request.app.state, "coding_worker", None)
    if coding_worker is not None and getattr(coding_worker, "_context_compressor", None) is not None:
        loop._context_compressor = coding_worker._context_compressor
    previous_store = _chat_previous_response_store(request)
    previous_key = _chat_previous_response_key(body)
    previous_response_id = previous_store.get(previous_key)
    if previous_response_id:
        loop._previous_response_id = previous_response_id
    result = await loop.run(
        body.message,
        extra_context=_chat_react_extra_context(context, live_web_context_block),
        allowed_tools=list(_CHAT_REACT_ALLOWED_TOOLS),
        tool_context={
            "workspace_root": str(project_root or ""),
            "execution_root": str(project_root or ""),
            "project_id": body.project_id,
            "session_id": body.session_id,
            "run_id": request_id,
            "source": "chat",
            "intent_text": body.message,
        },
        request_id=request_id,
        step_callback=step_callback,
    )
    if loop.last_response_id:
        previous_store[previous_key] = loop.last_response_id
    return {
        "content": str(result.content or "").strip(),
        "stopped_reason": result.stopped_reason,
        "tool_calls": result.tool_calls,
        "iterations": result.iterations,
        "request_id": request_id,
        "model": str(getattr(provider, "model", "") or ""),
        **_provider_fields(getattr(provider, "id", ""), getattr(provider, "model", "")),
    }


async def _chat_react_event_stream(
    provider: AIProvider,
    *,
    request: Request,
    body: "ChatRequest",
    project_root: str,
    context: str,
    system_prompt: str,
    live_web_context_block: str,
    stream_meta: Dict[str, Any],
) -> AsyncGenerator[Any, None]:
    queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue()
    started_at = time.monotonic()
    thinking_turns = 0
    reply_chunks = 0

    yield {"skip_usage_accounting": True, "skip_health_accounting": True}

    async def _step_callback(event: Dict[str, Any]) -> None:
        await queue.put(dict(event or {}))

    run_task = asyncio.create_task(_run_chat_react_with_provider(
        provider,
        request=request,
        body=body,
        project_root=project_root,
        context=context,
        system_prompt=system_prompt,
        live_web_context_block=live_web_context_block,
        step_callback=_step_callback,
    ))

    while True:
        if run_task.done() and queue.empty():
            break
        try:
            event = await asyncio.wait_for(queue.get(), timeout=_STREAM_HEARTBEAT_SECONDS)
        except asyncio.TimeoutError:
            yield {
                "type": "progress",
                "payload": {
                    **stream_meta,
                    "phase": "thinking" if not reply_chunks else "bleeping",
                    "detail": "Still working...",
                    "heartbeat": True,
                    "elapsed_seconds": round(time.monotonic() - started_at, 1),
                    "thinking_turns": thinking_turns,
                    "reply_chunks": reply_chunks,
                    "remaining": "until assistant finishes",
                    "indeterminate": True,
                    "ts": time.time(),
                },
            }
            continue
        kind = str(event.get("kind") or "").strip().lower()
        elapsed_seconds = round(time.monotonic() - started_at, 1)
        if kind == "thought":
            content = sanitize_text(str(event.get("content") or ""), max_chars=1600).strip()
            if content:
                thinking_turns += 1
                yield {"type": "thinking", "content": content, "thinking_turns": thinking_turns}
            continue
        if kind == "error":
            content = sanitize_text(str(event.get("content") or ""), max_chars=1600).strip()
            if content:
                thinking_turns += 1
                yield {"type": "thinking", "content": content, "thinking_turns": thinking_turns}
            continue
        if kind == "action":
            tool = sanitize_text(str(event.get("tool") or "tool"), max_chars=80).strip() or "tool"
            yield {
                "type": "progress",
                "payload": {
                    **stream_meta,
                    "phase": "thinking",
                    "detail": f"Using {tool}...",
                    "elapsed_seconds": elapsed_seconds,
                    "thinking_turns": thinking_turns,
                    "reply_chunks": reply_chunks,
                    "remaining": "until tools and model finish",
                    "indeterminate": True,
                    "ts": time.time(),
                },
            }
            continue
        if kind == "observation":
            tool = sanitize_text(str(event.get("tool") or "tool"), max_chars=80).strip() or "tool"
            success = bool(event.get("success", False))
            yield {
                "type": "progress",
                "payload": {
                    **stream_meta,
                    "phase": "thinking",
                    "detail": f"{tool} {'finished' if success else 'failed'}.",
                    "elapsed_seconds": elapsed_seconds,
                    "thinking_turns": thinking_turns,
                    "reply_chunks": reply_chunks,
                    "remaining": "until assistant finishes",
                    "indeterminate": True,
                    "ts": time.time(),
                },
            }
            continue
        if kind == "budget":
            remaining = max(0, int(event.get("remaining_iterations") or 0))
            yield {
                "type": "progress",
                "payload": {
                    **stream_meta,
                    "phase": "thinking",
                    "detail": f"{remaining} tool-loop step{'s' if remaining != 1 else ''} remaining.",
                    "elapsed_seconds": elapsed_seconds,
                    "thinking_turns": thinking_turns,
                    "reply_chunks": reply_chunks,
                    "remaining": "until assistant finishes",
                    "indeterminate": True,
                    "ts": time.time(),
                },
            }
            continue
        if kind == "budget_extended":
            added = max(0, int(event.get("added_iterations") or 0))
            yield {
                "type": "progress",
                "payload": {
                    **stream_meta,
                    "phase": "thinking",
                    "detail": f"Extended the tool loop by {added} step{'s' if added != 1 else ''}.",
                    "elapsed_seconds": elapsed_seconds,
                    "thinking_turns": thinking_turns,
                    "reply_chunks": reply_chunks,
                    "remaining": "until assistant finishes",
                    "indeterminate": True,
                    "ts": time.time(),
                },
            }

    react_result = await run_task
    yield {
        "type": "progress",
        "payload": {
            **stream_meta,
            "phase": "bleeping",
            "detail": "Composing final response...",
            "elapsed_seconds": round(time.monotonic() - started_at, 1),
            "thinking_turns": thinking_turns,
            "reply_chunks": reply_chunks,
            "remaining": "finishing up",
            "indeterminate": False,
            "ts": time.time(),
        },
    }
    for chunk in _chunk_stream_text(str(react_result.get("content") or "")):
        reply_chunks += 1
        yield chunk
    yield {"type": "react_meta", "meta": react_result}


def _extract_cards_block(text: str) -> Dict[str, Any]:
    """Extract a cards definition only when wrapped in a ``\`\`\`cards``
    fenced block. Returns ``{}`` for plain conversational replies — those don't
    create any cards. This is the ONLY way the planner can request cards now."""
    if not text:
        return {}
    match = _CARDS_FENCE_RE.search(text)
    if not match:
        return {}
    try:
        parsed = json.loads(match.group(1))
        if not isinstance(parsed, dict):
            return {}
        return dict(parsed)
    except Exception:
        return {}


def _mutation_entries(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    if isinstance(value, str):
        return [value]
    return []


def _extract_card_mutations_block(text: str) -> Dict[str, List[Dict[str, Any]]]:
    parsed = _extract_cards_block(text)
    return {
        "create": _mutation_entries(parsed.get("create") or parsed.get("cards")),
        "update": _mutation_entries(parsed.get("update")),
        "delete": _mutation_entries(parsed.get("delete")),
    }


def _strip_cards_block(text: str) -> str:
    """Remove the ``\`\`\`cards`` block from the assistant's reply so the
    chat shows only the conversational prose surrounding it."""
    value = _CARDS_FENCE_RE.sub("", text or "")
    partial = re.search(r"```cards\b.*$", value, re.DOTALL | re.IGNORECASE)
    if partial:
        value = value[:partial.start()]
    return value.strip()


def _safe_visible_chat_reply(text: str) -> str:
    return _normalize_html_artifacts(_strip_cards_block(str(text or ""))).strip()


def _safe_chat_raw(text: str) -> str:
    original = str(text or "").strip()
    visible = _strip_cards_block(original)
    if visible != original:
        return visible
    if _HTML_FENCE_RE.search(original):
        return original
    return ""


def _html_artifact_tag(html: str, *, title: str = "preview") -> str:
    payload = json.dumps({"html": html})
    return f'<blackboard-artifact type="html" title="{title}">{payload}</blackboard-artifact>'


def _normalize_html_artifacts(text: str) -> str:
    value = str(text or "")
    if not value:
        return ""
    return _HTML_FENCE_RE.sub(lambda match: _html_artifact_tag(match.group(1).strip()), value).strip()


def _message_requests_card_creation(message: str) -> bool:
    value = str(message or "").strip()
    if not value:
        return False
    return detect_chat_intent(value).should_create_cards


def _message_requests_card_mutation(message: str) -> bool:
    value = str(message or "").strip().replace("’", "'")
    if not value:
        return False
    if _message_requests_card_creation(value):
        return True
    patterns = [
        r"\b(cancel(?:ing|led)?|delete(?:d|ing)?|remove(?:d|ing)?|drop(?:ped|ping)?|archive(?:d|ing)?|close(?:d|ing)?|rename(?:d|ing)?|change(?:d|ing)?|update(?:d|ing)?|move(?:d|ing)?|edit(?:ed|ing)?|tear(?:ing)?\s+up)\b.{0,80}\b(card|cards|task|tasks|todo|todos|work item|work items)\b",
        r"\b(cancel(?:ing|led)?|delete(?:d|ing)?|remove(?:d|ing)?|drop(?:ped|ping)?|archive(?:d|ing)?|close(?:d|ing)?|rename(?:d|ing)?|change(?:d|ing)?|update(?:d|ing)?|move(?:d|ing)?|edit(?:ed|ing)?|tear(?:ing)?\s+up)\b.{0,40}\b(those|them|it|that)\b",
    ]
    return any(re.search(pattern, value, re.IGNORECASE) for pattern in patterns)


def _default_card_status(message: str) -> str:
    value = str(message or "").strip()
    return detect_chat_intent(value).default_card_status


def _blocked_card_reply(message: str, reply_text: str) -> str:
    value = str(message or "").strip().lower()
    if _PLAIN_CHAT_RE.match(value):
        return "Hi! How can I help?"
    if reply_text and "```" not in reply_text and "created " not in reply_text.lower():
        return reply_text
    return "I can talk through that first. If you want board cards, ask me to add cards or break the work into tasks."


def _reply_commits_to_cards(reply_text: str) -> bool:
    value = str(reply_text or "").strip().replace("’", "'")
    if not value:
        return False
    patterns = [
        r"\b(here are|here is|i'm|i am|i'll|i will|adding|creating|created|queued|prepared|generated|captured|updating|updated|changing|changed|editing|edited|moving|moved|renaming|renamed|deleting|deleted|removing|removed)\b.{0,120}\b(card|cards|task|tasks|work item|work items|todo|todos)\b",
        r"\b(card|cards|task|tasks|work item|work items|todo|todos)\b.{0,120}\b(added|created|generated|queued|prepared|updated|changed|edited|moved|renamed|deleted|removed)\b",
        r"\b(one|two|three|four|five|six|seven|eight|nine|ten|\d+)\b.{0,40}\b(card|cards|task|tasks|work item|work items|todo|todos)\b.{0,120}\b(inspect|audit|review|cover|target|feed|queue|map|catalog|check)\b",
        r"\b(delete(?:d|ing)?|remove(?:d|ing)?|cancel(?:ing|led)?|drop(?:ped|ping)?|archive(?:d|ing)?|close(?:d|ing)?|rename(?:d|ing)?|change(?:d|ing)?|update(?:d|ing)?|move(?:d|ing)?|edit(?:ed|ing)?|tear(?:ing)?\s+up)\b.{0,120}\b(card|cards|task|tasks|work item|work items|todo|todos)\b",
    ]
    return any(re.search(pattern, value, re.IGNORECASE) for pattern in patterns)


def _allow_card_creation(message: str, reply_text: str) -> bool:
    if _message_requests_card_mutation(message):
        return True
    value = str(message or "").strip().lower()
    if not value or _PLAIN_CHAT_RE.match(value):
        return False
    return _reply_commits_to_cards(reply_text)


def _normalize_card_list(values: Any) -> List[str]:
    if not isinstance(values, list):
        return []
    out: List[str] = []
    seen: set[str] = set()
    for item in values:
        text = sanitize_text(str(item or ""), max_chars=_MAX_CARD_LIST_ITEM_CHARS).strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= _MAX_CARD_LIST_ITEMS:
            break
    return out


def _normalize_card_target(value: Any) -> str:
    return sanitize_text(str(value or ""), max_chars=_MAX_MUTATION_TARGET_CHARS).strip()


def _normalize_card_objective(value: Any) -> str:
    return sanitize_text(str(value or ""), max_chars=_MAX_CARD_OBJECTIVE_CHARS).strip()


def _body_repeats_title_or_objective(body: str, title: str, objective: str) -> bool:
    body_key = re.sub(r"\s+", " ", str(body or "").strip().lower())
    title_key = re.sub(r"\s+", " ", str(title or "").strip().lower())
    objective_key = re.sub(r"\s+", " ", str(objective or "").strip().lower())
    if not body_key:
        return True
    if title_key and body_key == title_key:
        return True
    if objective_key and body_key == objective_key:
        return True
    if title_key and body_key.startswith(title_key):
        return True
    return False


def _default_status_note(status: str, title: str, objective: str) -> str:
    subject = sanitize_text(str(title or objective or "this work"), max_chars=96).strip() or "this work"
    templates = {
        "inbox": f"In inbox so the work can be triaged and shaped: {subject}.",
        "designing": f"In designing because the approach still needs to be shaped before implementation: {subject}.",
        "planning": f"In planning because the implementation needs clearer scope, files, and checks: {subject}.",
        "ready": f"In ready because the work is scoped and prepared for execution: {subject}.",
        "executing": f"In executing because a coding run is actively working on it: {subject}.",
        "reviewing": f"In reviewing because changes were produced and need verification: {subject}.",
        "blocked": f"In blocked because something is preventing forward progress right now: {subject}.",
        "done": f"In done because the requested outcome has been completed: {subject}.",
    }
    note = templates.get(str(status or "").strip().lower(), templates["inbox"])
    return sanitize_text(note, max_chars=_MAX_CARD_BODY_CHARS).strip()


def _prepare_card_mutation_payload(entry: Dict[str, Any], *, default_status: str, existing: Optional[Dict[str, Any]] = None, force_default_status: bool = False) -> Dict[str, Any]:
    payload = dict(entry or {})
    payload_status = str(payload.get("status") or "").strip().lower()
    status = str(payload_status or (existing or {}).get("status") or default_status or "inbox").strip().lower()
    if status not in COLUMNS:
        status = str(default_status or "inbox").strip().lower() or "inbox"
    if existing is None and force_default_status:
        forced_default = str(default_status or "").strip().lower()
        if forced_default in COLUMNS:
            status = forced_default
    title = sanitize_text(str(payload.get("title") or (existing or {}).get("title") or ""), max_chars=_MAX_CARD_TITLE_CHARS).strip()
    body = sanitize_text(str(payload.get("body") or ""), max_chars=_MAX_CARD_BODY_CHARS).strip()
    existing_meta = dict((existing or {}).get("metadata") or {})
    existing_status = str((existing or {}).get("status") or "").strip().lower()
    existing_note = sanitize_text(str((existing or {}).get("body") or existing_meta.get("status_note") or ""), max_chars=_MAX_CARD_BODY_CHARS).strip()
    objective = _normalize_card_objective(payload.get("objective") or existing_meta.get("execution_objective") or "")
    if not objective:
        objective = _normalize_card_objective(body or title)
    note = body or existing_note
    should_refresh_note = existing is None or (status != existing_status and not body)
    if should_refresh_note and _body_repeats_title_or_objective(body or existing_note, title, objective):
        note = _default_status_note(status, title, objective)
    metadata = dict(existing_meta)
    incoming_metadata = payload.get("metadata")
    if isinstance(incoming_metadata, dict):
        metadata.update({str(k): v for k, v in incoming_metadata.items() if str(k).strip()})
    if objective:
        metadata["execution_objective"] = objective
    if note:
        metadata["status_note"] = note
    prepared = {k: v for k, v in payload.items() if k not in {"objective", "metadata"}}
    prepared["status"] = status
    prepared["body"] = note
    prepared["metadata"] = metadata
    return prepared


def _normalize_cards_data(cards_data: Any) -> List[Dict[str, Any]]:
    if not isinstance(cards_data, list):
        return []
    out: List[Dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for entry in cards_data:
        if not isinstance(entry, dict):
            continue
        title = sanitize_text(str(entry.get("title") or ""), max_chars=_MAX_CARD_TITLE_CHARS).strip()
        body = sanitize_text(str(entry.get("body") or ""), max_chars=_MAX_CARD_BODY_CHARS).strip()
        objective = _normalize_card_objective(entry.get("objective"))
        status = str(entry.get("status") or "").strip().lower()
        if not title:
            fallback = body.splitlines()[0].strip() if body else ""
            title = sanitize_text(fallback, max_chars=_MAX_CARD_TITLE_CHARS).strip()
        if not title:
            continue
        key = (title.casefold(), body.casefold())
        if key in seen:
            continue
        seen.add(key)
        normalized = {
            "title": title,
            "body": body,
            **({"objective": objective} if objective else {}),
            **({"status": status} if status in COLUMNS else {}),
            "files": _normalize_card_list(entry.get("files")),
            "verification": _normalize_card_list(entry.get("verification")),
            "constraints": _normalize_card_list(entry.get("constraints")),
            "deps": _normalize_card_list(entry.get("deps")),
            "tags": _normalize_card_list(entry.get("tags")),
        }
        if isinstance(entry.get("metadata"), dict):
            normalized["metadata"] = dict(entry.get("metadata") or {})
        out.append(normalized)
        if len(out) >= _MAX_GENERATED_CARDS:
            break
    return out


def _normalize_card_updates_data(values: Any) -> List[Dict[str, Any]]:
    if not isinstance(values, list):
        return []
    out: List[Dict[str, Any]] = []
    for entry in values:
        if not isinstance(entry, dict):
            continue
        card_id = _normalize_card_target(entry.get("card_id"))
        target_title = _normalize_card_target(entry.get("target_title") or entry.get("current_title") or entry.get("existing_title"))
        if not card_id and not target_title:
            continue
        normalized: Dict[str, Any] = {"card_id": card_id, "target_title": target_title}
        title = sanitize_text(str(entry.get("title") or ""), max_chars=_MAX_CARD_TITLE_CHARS).strip()
        body = sanitize_text(str(entry.get("body") or ""), max_chars=_MAX_CARD_BODY_CHARS).strip()
        objective = _normalize_card_objective(entry.get("objective"))
        job_id = _normalize_card_target(entry.get("job_id"))
        status = str(entry.get("status") or "").strip().lower()
        progress = entry.get("progress")
        if title:
            normalized["title"] = title
        if body:
            normalized["body"] = body
        if objective:
            normalized["objective"] = objective
        if job_id:
            normalized["job_id"] = job_id
        if status in COLUMNS:
            normalized["status"] = status
        if isinstance(progress, (int, float)):
            normalized["progress"] = max(0, min(100, int(progress)))
        for key in ("files", "verification", "constraints", "deps", "tags"):
            value = _normalize_card_list(entry.get(key))
            if value:
                normalized[key] = value
        if isinstance(entry.get("metadata"), dict):
            normalized["metadata"] = dict(entry.get("metadata") or {})
        if len(normalized) > 2:
            out.append(normalized)
    return out[:_MAX_GENERATED_CARDS]


def _normalize_card_deletes_data(values: Any) -> List[Dict[str, Any]]:
    if not isinstance(values, list):
        return []
    out: List[Dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for entry in values:
        if isinstance(entry, str):
            entry = {"target_title": entry}
        if not isinstance(entry, dict):
            continue
        card_id = _normalize_card_target(entry.get("card_id"))
        target_title = _normalize_card_target(entry.get("target_title") or entry.get("title") or entry.get("current_title"))
        if not card_id and not target_title:
            continue
        key = (card_id.casefold(), target_title.casefold())
        if key in seen:
            continue
        seen.add(key)
        out.append({"card_id": card_id, "target_title": target_title})
        if len(out) >= _MAX_GENERATED_CARDS:
            break
    return out


def _card_generation_failed_reply(reply_text: str) -> str:
    base = _safe_visible_chat_reply(reply_text)
    notice = "I couldn't safely apply the requested board changes from the planner output. Please retry if you still want me to change them."
    if not base:
        return notice
    if notice.lower() in base.lower():
        return base
    return f"{base}\n\n{notice}"


def _card_targeting_failed_reply(reply_text: str, unresolved: List[Dict[str, Any]]) -> str:
    base = _safe_visible_chat_reply(reply_text)
    ambiguous = sum(1 for item in unresolved if str(item.get("reason") or "") == "ambiguous")
    missing = sum(1 for item in unresolved if str(item.get("reason") or "") == "not_found")
    pieces: List[str] = []
    if ambiguous:
        pieces.append(f"{ambiguous} target{'s were' if ambiguous != 1 else ' was'} ambiguous")
    if missing:
        pieces.append(f"{missing} target{'s were' if missing != 1 else ' was'} not found")
    detail = ", ".join(pieces) if pieces else "the requested targets couldn't be resolved safely"
    notice = f"I didn't change any cards because {detail}. Please name the exact card or card id you want me to change."
    if not base:
        return notice
    if notice.lower() in base.lower():
        return base
    return f"{base}\n\n{notice}"


def _board_cards_context_block(board: BoardService, *, limit: int = 40) -> str:
    snapshot = board.snapshot()
    cards = [card for items in snapshot.get("cards_by_column", {}).values() for card in items]
    if not cards:
        return ""
    cards = cards[-max(1, int(limit or 40)):]
    lines = ["<board_cards>"]
    for card in cards:
        metadata = dict(card.get("metadata") or {})
        note = sanitize_text(str(card.get("body") or metadata.get("status_note") or ""), max_chars=180)
        objective = sanitize_text(str(metadata.get("execution_objective") or ""), max_chars=220)
        lines.append(
            f"- id={card.get('id') or ''} status={card.get('status') or ''} title={sanitize_text(str(card.get('title') or ''), max_chars=_MAX_CARD_TITLE_CHARS)} note={note} objective={objective}"
        )
    lines.append("</board_cards>")
    return "\n".join(lines)


def _recent_board_targets_block(
    sessions: SessionStore,
    session_id: str,
    board: BoardService,
    *,
    current_message_id: str = "",
    message_limit: int = 10,
    card_limit: int = 12,
) -> str:
    tail = [
        message for message in sessions.tail(session_id, limit=max(1, int(message_limit or 10)) + 2)
        if message.id != current_message_id
    ]
    if not tail:
        return ""
    lines = ["<recent_board_targets>"]
    seen: set[str] = set()
    count = 0
    for message in reversed(tail):
        metadata = dict(message.metadata or {})
        for card_id in list(metadata.get("card_ids") or []):
            value = str(card_id or "").strip()
            if not value or value in seen:
                continue
            card = board.get(value)
            if card is None:
                continue
            seen.add(value)
            count += 1
            objective = sanitize_text(str((card.metadata or {}).get("execution_objective") or ""), max_chars=220)
            lines.append(f"- recent id={card.id} status={card.status} title={sanitize_text(card.title, max_chars=_MAX_CARD_TITLE_CHARS)} objective={objective}")
            if count >= max(1, int(card_limit or 12)):
                lines.append("</recent_board_targets>")
                return "\n".join(lines)
    lines.append("</recent_board_targets>")
    return "\n".join(lines) if count else ""


def _relevant_board_cards_block(board: BoardService, query: str, *, limit: int = 8) -> str:
    query_text = str(query or "").strip()
    if not query_text:
        return ""
    matches = board.search_cards(query=query_text, limit=max(1, int(limit or 8)))
    if not matches:
        return ""
    lines = ["<relevant_board_cards>"]
    for card in matches:
        title = sanitize_text(str(card.get("title") or ""), max_chars=_MAX_CARD_TITLE_CHARS)
        body = sanitize_text(str(card.get("body") or ""), max_chars=220)
        objective = sanitize_text(str((card.get("metadata") or {}).get("execution_objective") or ""), max_chars=260)
        lines.append(
            f"- id={card.get('id') or ''} status={card.get('status') or ''} title={title} body={body} objective={objective}"
        )
    lines.append("</relevant_board_cards>")
    return "\n".join(lines)


def _resolve_board_card(board: BoardService, *, card_id: str = "", target_title: str = "") -> tuple[Optional[Dict[str, Any]], str]:
    if card_id:
        card = board.get(card_id)
        if card is not None:
            return card.to_dict(), ""
        return None, "not_found"
    title = str(target_title or "").strip()
    if not title:
        return None, "not_found"
    normalized = title.casefold()
    snapshot = board.snapshot()
    matches = [
        card
        for items in snapshot.get("cards_by_column", {}).values()
        for card in items
        if str(card.get("title") or "").strip().casefold() == normalized
    ]
    if len(matches) == 1:
        return matches[0], ""
    if len(matches) > 1:
        return None, "ambiguous"
    return None, "not_found"


async def _apply_card_mutations(
    board: BoardService,
    *,
    create: List[Dict[str, Any]],
    update: List[Dict[str, Any]],
    delete: List[Dict[str, Any]],
    default_status: str,
    force_create_status: bool = False,
) -> Dict[str, List[Dict[str, Any]]]:
    created_cards = []
    for entry in create:
        prepared = _prepare_card_mutation_payload(entry, default_status=default_status, force_default_status=force_create_status)
        created_cards.append(await board.create_card(**prepared))
    updated_cards: List[Dict[str, Any]] = []
    deleted_cards: List[Dict[str, Any]] = []
    unresolved: List[Dict[str, Any]] = []
    for entry in update:
        match, reason = _resolve_board_card(board, card_id=entry.get("card_id") or "", target_title=entry.get("target_title") or "")
        if not match:
            unresolved.append({"kind": "update", "reason": reason or "not_found", "card_id": entry.get("card_id") or "", "target_title": entry.get("target_title") or ""})
            continue
        updates = {k: v for k, v in entry.items() if k not in {"card_id", "target_title"}}
        if not updates:
            continue
        prepared = _prepare_card_mutation_payload(updates, default_status=str(match.get("status") or default_status), existing=match)
        card = await board.update_card(str(match.get("id") or ""), **prepared)
        if card is not None:
            updated_cards.append(card.to_dict())
    for entry in delete:
        match, reason = _resolve_board_card(board, card_id=entry.get("card_id") or "", target_title=entry.get("target_title") or "")
        if not match:
            unresolved.append({"kind": "delete", "reason": reason or "not_found", "card_id": entry.get("card_id") or "", "target_title": entry.get("target_title") or ""})
            continue
        card_id = str(match.get("id") or "")
        if card_id and await board.delete_card(card_id):
            deleted_cards.append({"id": card_id, "title": str(match.get("title") or "")})
    return {
        "created": [card.to_dict() for card in created_cards],
        "updated": updated_cards,
        "deleted": deleted_cards,
        "unresolved": unresolved,
    }


def _mutation_summary(card_operations: Dict[str, List[Dict[str, Any]]]) -> str:
    lines: List[str] = []
    for key, label in (("created", "Created"), ("updated", "Updated"), ("deleted", "Deleted")):
        items = list(card_operations.get(key) or [])
        if not items:
            continue
        titles = ", ".join(str(item.get("title") or item.get("id") or "card") for item in items[:5])
        line = f"{label} {len(items)} card{'s' if len(items) != 1 else ''}: {titles}"
        if len(items) > 5:
            line += f", +{len(items) - 5} more"
        lines.append(line)
    return "\n".join(lines)


def _chunk_text(text: str, size: int = 96) -> List[str]:
    value = str(text or "")
    return [value[i:i + size] for i in range(0, len(value), size)] or [""]


def _persist_chat_reply_images(
    request: Request,
    body: "ChatRequest",
    *,
    message_id: str,
    project_root: str,
    content: str,
    raw: Optional[Dict[str, Any]] = None,
) -> tuple[str, List[Dict[str, Any]]]:
    try:
        return persist_chat_images(
            data_root=request.app.state.data_root,
            project_id=body.project_id,
            session_id=body.session_id,
            message_id=message_id,
            project_root=project_root,
            content=content,
            raw=raw if isinstance(raw, dict) else {},
        )
    except Exception as exc:
        logger.debug("[chat] image attachment persistence skipped: %s", exc)
        return str(content or ""), []


async def _repair_cards_block(
    *,
    registry: Any,
    system_prompt: str,
    message: str,
    context: str,
    reply: str,
) -> tuple[Dict[str, List[Dict[str, Any]]], str]:
    repair_prompt = _CARDS_REPAIR_TEMPLATE.format(
        system=system_prompt,
        message=message,
        reply=reply or "(empty reply)",
        context=context or "(no project intelligence yet)",
    )

    async def _call(provider):
        kwargs: Dict[str, Any] = {}
        if getattr(getattr(provider, "capabilities", None), "structured_output", False):
            kwargs["response_format"] = build_response_format(_CARD_MUTATIONS_SCHEMA, "card_mutations")
        return await provider.complete(
            [Message(role="user", content=repair_prompt)],
            temperature=0.0,
            **kwargs,
        )

    response = await registry.call_with_fallback("planner", _call)
    repair_raw = str(response.content or "").strip()
    parsed, parse_error = parse_json_payload(repair_raw)
    if not parse_error:
        parsed, validation_error = validate_payload(parsed, _CARD_MUTATIONS_SCHEMA, path="card_mutations")
        if not validation_error:
            normalized_raw = repair_raw if "```cards" in repair_raw.lower() else f"```cards\n{json.dumps(parsed, default=str)}\n```"
            return _extract_card_mutations_block(normalized_raw), normalized_raw
    return _extract_card_mutations_block(repair_raw), repair_raw


async def _repair_card_apply_block(
    *,
    registry: Any,
    system_prompt: str,
    message: str,
    context: str,
    reply: str,
    board_context: str,
    error_payload: Dict[str, Any],
) -> tuple[Dict[str, List[Dict[str, Any]]], str]:
    repair_prompt = _CARDS_APPLY_REPAIR_TEMPLATE.format(
        system=system_prompt,
        message=message,
        reply=_safe_visible_chat_reply(reply) or "(empty reply)",
        error_json=json.dumps(error_payload, ensure_ascii=False, default=str),
        board_context=board_context or "(no board cards)",
        context=context or "(no project intelligence yet)",
    )

    async def _call(provider):
        kwargs: Dict[str, Any] = {}
        if getattr(getattr(provider, "capabilities", None), "structured_output", False):
            kwargs["response_format"] = build_response_format(_CARD_MUTATIONS_SCHEMA, "card_mutations")
        return await provider.complete(
            [Message(role="user", content=repair_prompt)],
            temperature=0.0,
            **kwargs,
        )

    response = await registry.call_with_fallback("planner", _call)
    repair_raw = str(response.content or "").strip()
    parsed, parse_error = parse_json_payload(repair_raw)
    if not parse_error:
        parsed, validation_error = validate_payload(parsed, _CARD_MUTATIONS_SCHEMA, path="card_mutations")
        if not validation_error:
            normalized_raw = repair_raw if "```cards" in repair_raw.lower() else f"```cards\n{json.dumps(parsed, default=str)}\n```"
            return _extract_card_mutations_block(normalized_raw), normalized_raw
    return _extract_card_mutations_block(repair_raw), repair_raw


def _mark_autohealed_mutations(mutations: Dict[str, List[Dict[str, Any]]], *, error_payload: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    marked: Dict[str, List[Dict[str, Any]]] = {"create": [], "update": [], "delete": []}
    metadata = {
        "autoheal": {
            "applied": True,
            "reason": "initial_card_mutation_unresolved",
            "source": "chat_card_apply_repair",
            "error": error_payload,
        }
    }
    for key in ("create", "update"):
        for entry in mutations.get(key) or []:
            if not isinstance(entry, dict):
                continue
            next_entry = dict(entry)
            tags = _normalize_card_list(list(next_entry.get("tags") or []) + ["autohealed", "card-repair"])
            if tags:
                next_entry["tags"] = tags
            next_entry["metadata"] = metadata
            marked[key].append(next_entry)
    marked["delete"] = [dict(entry) for entry in mutations.get("delete") or [] if isinstance(entry, dict)]
    return marked


def _card_apply_error_payload(
    *,
    unresolved: List[Dict[str, Any]],
    create_count: int,
    update_count: int,
    delete_count: int,
) -> Dict[str, Any]:
    reasons = sorted({str(item.get("reason") or "unknown") for item in unresolved})
    return {
        "stage": "apply_card_mutations",
        "unresolved": unresolved[:_MAX_GENERATED_CARDS],
        "attempted": {
            "create": int(create_count),
            "update": int(update_count),
            "delete": int(delete_count),
        },
        "reasons": reasons,
        "confidence": 0.65 if unresolved else 0.0,
        "retry_policy": "one_safe_repair_attempt_when_no_board_changes_were_applied",
    }


def _normalize_repaired_mutations(
    mutations: Dict[str, List[Dict[str, Any]]],
    *,
    error_payload: Dict[str, Any],
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    marked = _mark_autohealed_mutations(mutations, error_payload=error_payload)
    return (
        _normalize_cards_data(marked.get("create") or []),
        _normalize_card_updates_data(marked.get("update") or []),
        _normalize_card_deletes_data(marked.get("delete") or []),
    )


class _ThinkStreamFilter:
    def __init__(self) -> None:
        self.inside = False
        self.pending = ""

    def feed(self, text: str) -> tuple[str, str]:
        value = self.pending + str(text or "")
        self.pending = ""
        visible: List[str] = []
        hidden: List[str] = []
        i = 0
        while i < len(value):
            lower_value = value.lower()
            if not self.inside:
                start = lower_value.find("<think>", i)
                if start == -1:
                    tail = value[i:]
                    marker = tail.lower().rfind("<thi")
                    if marker != -1 and marker + i + len(tail[marker:]) == len(value):
                        visible.append(tail[:marker])
                        self.pending = tail[marker:]
                    else:
                        visible.append(tail)
                    break
                visible.append(value[i:start])
                self.inside = True
                i = start + len("<think>")
            else:
                end = lower_value.find("</think>", i)
                if end == -1:
                    hidden.append(value[i:])
                    break
                hidden.append(value[i:end])
                self.inside = False
                i = end + len("</think>")
        return "".join(visible), "".join(hidden)

    def flush(self) -> tuple[str, str]:
        if not self.pending:
            return "", ""
        value = self.pending
        self.pending = ""
        if self.inside:
            return "", value
        return value, ""


class _CardsStreamFilter:
    marker = "```cards"

    def __init__(self) -> None:
        self.inside = False
        self.pending = ""

    def feed(self, text: str) -> str:
        value = self.pending + str(text or "")
        self.pending = ""
        visible: List[str] = []
        i = 0
        while i < len(value):
            lower_value = value.lower()
            if not self.inside:
                start = lower_value.find(self.marker, i)
                if start == -1:
                    tail = value[i:]
                    hold = self._marker_suffix_len(tail)
                    if hold:
                        visible.append(tail[:-hold])
                        self.pending = tail[-hold:]
                    else:
                        visible.append(tail)
                    break
                visible.append(value[i:start])
                self.inside = True
                i = start + len(self.marker)
            else:
                end = lower_value.find("```", i)
                if end == -1:
                    tail = value[i:]
                    if tail.endswith("``"):
                        self.pending = "``"
                    elif tail.endswith("`"):
                        self.pending = "`"
                    break
                self.inside = False
                i = end + len("```")
        return "".join(visible)

    def flush(self) -> str:
        if self.inside:
            self.pending = ""
            return ""
        value = self.pending
        self.pending = ""
        return value

    @classmethod
    def _marker_suffix_len(cls, text: str) -> int:
        lower = text.lower()
        for size in range(len(cls.marker) - 1, 0, -1):
            if lower.endswith(cls.marker[:size]):
                return size
        return 0


def _sse_event(event: str, payload: Dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, default=str)}\n\n"


class ChatRequest(BaseModel):
    project_id: str
    session_id: str = "main"
    message: str
    client_message_id: str = ""


class ChatAttachmentItemRequest(BaseModel):
    source: str = ""
    content_base64: str = ""
    content_type: str = ""
    filename: str = ""
    alt: str = ""
    title: str = ""


class ChatAttachmentRegisterRequest(BaseModel):
    attachments: List[ChatAttachmentItemRequest] = []
    project_root: str = ""


class MessageReceiptSearchRequest(BaseModel):
    query: str = ""
    session_id: str = ""
    role: str = ""
    status: str = ""
    event_kind: str = ""
    correlation_id: str = ""
    limit: int = 20


def _get_message_ledger(request: Request, project_id: str) -> MessageLedger:
    ledgers = getattr(request.app.state, "message_ledgers", None)
    if ledgers is None:
        ledgers = {}
        request.app.state.message_ledgers = ledgers
    if project_id not in ledgers:
        ledgers[project_id] = MessageLedger(request.app.state.data_root, project_id)
    return ledgers[project_id]


def _receipt_dicts(receipts) -> List[Dict[str, Any]]:
    return [receipt.to_dict() for receipt in receipts]


def _record_message_receipt(
    ledger: MessageLedger,
    *,
    body: ChatRequest,
    message: SessionMessage,
    direction: str,
    status: str = "recorded",
    event_kind: str = "message",
    reply_to: str = "",
    correlation_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    receipt_metadata = dict(metadata or {})
    if body.client_message_id:
        receipt_metadata.setdefault("client_message_id", body.client_message_id)
    ledger.record(
        message_id=message.id,
        session_id=body.session_id,
        event_kind=event_kind,
        role=message.role,
        direction=direction,
        status=status,
        source="chat",
        correlation_id=correlation_id or body.client_message_id or message.id,
        reply_to=reply_to,
        run_id=body.client_message_id or message.id,
        content=message.content,
        metadata=receipt_metadata,
    )


def _turn_state_store(request: Request, project_id: str) -> TurnStateStore:
    return TurnStateStore(request.app.state.data_root, project_id)


def _chat_scratchboard_store(request: Request, project_id: str) -> ChatScratchboardStore:
    return ChatScratchboardStore(request.app.state.data_root, project_id)


def _temporal_scratchpad_store(request: Request, project_id: str) -> TemporalScratchpadStore:
    return TemporalScratchpadStore(request.app.state.data_root, project_id)


def _organized_chat_context(
    *,
    project_intelligence: str = "",
    conversation_state: str = "",
    session_scratchboard: str = "",
    temporal_scratchpad: str = "",
    adaptive_skills: str = "",
    relevant_session_memory: str = "",
    recent_session_history: str = "",
    board_cards: str = "",
    relevant_board_cards: str = "",
    recent_board_targets: str = "",
) -> str:
    organizer = ChatContextOrganizer()
    return organizer.organize({
        "project_intelligence": project_intelligence,
        "conversation_state": conversation_state,
        "session_scratchboard": session_scratchboard,
        "temporal_scratchpad": temporal_scratchpad,
        "adaptive_skills": adaptive_skills,
        "relevant_session_memory": relevant_session_memory,
        "recent_session_history": recent_session_history,
        "board_cards": board_cards,
        "relevant_board_cards": relevant_board_cards,
        "recent_board_targets": recent_board_targets,
    })


def _session_history_block(sessions: SessionStore, session_id: str, *, current_message_id: str = "", limit: int = 12) -> str:
    messages = [
        message for message in sessions.tail(session_id, limit=max(1, int(limit or 12)) + 4)
        if message.id != current_message_id and str(message.content or "").strip()
    ][-max(1, int(limit or 12)):]
    if not messages:
        return ""
    lines = ["<recent_session_history>"]
    for message in messages:
        role = str(message.role or "user").strip().lower()
        if role not in {"user", "assistant", "system"}:
            continue
        content = sanitize_text(str(message.content or ""), max_chars=500)
        if content:
            lines.append(f"{role}: {content}")
    lines.append("</recent_session_history>")
    return "\n".join(lines) if len(lines) > 2 else ""


def _relevant_session_memory_block(sessions: SessionStore, query: str, session_id: str, *, current_message_id: str = "", limit: int = 8) -> str:
    hits = [
        hit for hit in sessions.search_messages(query=query, session_id=session_id, limit=max(1, int(limit or 8)))
        if str(hit.get("message_id") or "") != current_message_id
    ]
    if not hits:
        return ""
    lines = ["<relevant_session_memory>"]
    for hit in hits:
        role = str(hit.get("role") or "user").strip().lower()
        if role not in {"user", "assistant", "system"}:
            continue
        content = sanitize_text(str(hit.get("content") or ""), max_chars=420)
        if content:
            lines.append(f"- session={hit.get('session_id')} role={role} score={round(float(hit.get('score') or 0.0), 2)}: {content}")
    lines.append("</relevant_session_memory>")
    return "\n".join(lines) if len(lines) > 2 else ""


def _extract_json(text: str) -> Dict[str, Any]:
    payload, error = parse_json_payload(text)
    return payload if not error and isinstance(payload, dict) else {}


def _observe_chat_skill_promotion(
    request: Request,
    body: "ChatRequest",
    user_message: SessionMessage,
    assistant_message: SessionMessage,
    *,
    created_count: int = 0,
    updated_count: int = 0,
    deleted_count: int = 0,
    unresolved_count: int = 0,
) -> None:
    try:
        SkillPromotionGate(request.app.state.data_root, body.project_id).observe_chat_workflow(
            session_id=body.session_id,
            user_message=user_message.content,
            assistant_message=assistant_message.content,
            observation_id=f"chat:{assistant_message.id}",
            created_count=created_count,
            updated_count=updated_count,
            deleted_count=deleted_count,
            unresolved_count=unresolved_count,
        )
    except Exception as exc:
        logger.debug("[chat] skill promotion observe skipped: %s", exc)


@router.post("")
async def chat(body: ChatRequest, request: Request) -> Dict[str, Any]:
    project_store = request.app.state.project_store
    project = project_store.get(body.project_id)
    if project is None:
        raise HTTPException(404, "project not found")

    boards: Dict[str, BoardService] = request.app.state.boards
    if body.project_id not in boards:
        boards[body.project_id] = BoardService(
            request.app.state.data_root,
            body.project_id,
            bus=request.app.state.bus,
            on_done=getattr(request.app.state, "on_card_done", None),
        )
    board = boards[body.project_id]

    sessions = SessionStore(request.app.state.data_root, body.project_id)
    ledger = _get_message_ledger(request, body.project_id)
    turn_state = _turn_state_store(request, body.project_id)
    scratchboard = _chat_scratchboard_store(request, body.project_id)
    temporal_scratchpad = _temporal_scratchpad_store(request, body.project_id)
    user_message = SessionMessage(role="user", content=body.message)
    sessions.append(body.session_id, user_message)
    turn_state.update(body.session_id, role="user", content=body.message)
    scratchboard.update(body.session_id, role="user", content=body.message)
    temporal_scratchpad.append(body.session_id, "user", body.message, {"phase": "request"})
    _record_message_receipt(ledger, body=body, message=user_message, direction="inbound")
    bus = getattr(request.app.state, "bus", None)
    stream_meta = {
        "project_id": body.project_id,
        "session_id": body.session_id,
        "client_message_id": body.client_message_id,
        "ts": time.time(),
    }
    if bus is not None:
        await bus.emit("chat.started", stream_meta)

    # Build planner context from project memory + recent session tail.
    memory = request.app.state.project_memories.get(body.project_id)
    if memory is None:
        from blackboard.workspace.memory import ProjectMemory
        memory = ProjectMemory(request.app.state.data_root, body.project_id, project_intel_dir=request.app.state.project_intel_dir)
        request.app.state.project_memories[body.project_id] = memory
    adaptive = synthesize_adaptive_skills(
        data_root=request.app.state.data_root,
        project_id=body.project_id,
        cwd=project.root,
        query=body.message,
        session_id=body.session_id,
    )

    context = _organized_chat_context(
        project_intelligence=memory.context_block(project.root),
        conversation_state=turn_state.context_block(body.session_id),
        session_scratchboard=scratchboard.context_block(body.session_id),
        temporal_scratchpad=temporal_scratchpad.context_block(body.session_id),
        adaptive_skills=adaptive.get("context_block", ""),
        relevant_session_memory=_relevant_session_memory_block(sessions, body.message, body.session_id, current_message_id=user_message.id),
        recent_session_history=_session_history_block(sessions, body.session_id, current_message_id=user_message.id),
        board_cards=_board_cards_context_block(board),
        relevant_board_cards=_relevant_board_cards_block(board, body.message),
        recent_board_targets=_recent_board_targets_block(sessions, body.session_id, board, current_message_id=user_message.id),
    )

    registry = get_provider_registry()
    system_prompt = get_prompts().get("planner.system")
    live_web_context_block = await _chat_live_web_context_block(
        body.message,
        project_id=body.project_id,
        session_id=body.session_id,
    )
    live_web_provenance = _extract_live_web_provenance_from_block(live_web_context_block)
    user_prompt = _PLANNER_TEMPLATE.format(
        system=system_prompt,
        message=body.message,
        context=context or "(no project intelligence yet)",
        live_web_context_block=live_web_context_block,
    )
    selected_provider: Dict[str, str] = {}

    async def _call(provider):
        selected_provider.update(_provider_fields(getattr(provider, "id", ""), getattr(provider, "model", "")))
        if _provider_supports_complete(provider):
            react_payload = await _run_chat_react_with_provider(
                provider,
                request=request,
                body=body,
                project_root=project.root,
                context=context,
                system_prompt=system_prompt,
                live_web_context_block=live_web_context_block,
            )
            selected_provider.update(_provider_fields(react_payload.get("provider_id"), react_payload.get("provider_model") or react_payload.get("model")))
            return SimpleNamespace(
                content=react_payload.get("content") or "",
                tokens_prompt=0,
                tokens_completion=0,
                model=react_payload.get("model") or "",
                provider_id=react_payload.get("provider_id") or selected_provider.get("provider_id") or "",
                provider_model=react_payload.get("provider_model") or react_payload.get("model") or selected_provider.get("provider_model") or "",
                tool_calls=[],
                raw={},
                _skip_usage_accounting=True,
                _skip_health_accounting=True,
            )
        result = await provider.complete(
            [Message(role="user", content=user_prompt)],
            temperature=0.2,
        )
        selected_provider.update(_provider_fields(getattr(provider, "id", ""), getattr(result, "model", "") or getattr(provider, "model", "")))
        return result

    try:
        response = await registry.call_with_fallback("planner", _call)
    except Exception as exc:
        # No verified provider available — record the user turn so it isn't lost
        # and surface a chat error. We do NOT auto-create a card here; that was
        # the old behavior and made every chat message litter the board.
        warning = f"planner unavailable: {exc}"
        system_message = SessionMessage(
            role="system",
            content=warning,
            metadata={"error": True},
        )
        sessions.append(body.session_id, system_message)
        turn_state.update(body.session_id, role="system", content=warning)
        scratchboard.update(body.session_id, role="system", content=warning)
        temporal_scratchpad.append(body.session_id, "system", warning, {"phase": "planner_error"})
        _record_message_receipt(ledger, body=body, message=system_message, direction="internal", status="error", event_kind="planner_error", reply_to=user_message.id)
        if bus is not None:
            await bus.emit("chat.error", {**stream_meta, "error": warning})
        return {"cards": [], "warning": warning, "reply": warning}

    provider_fields = _provider_fields(
        getattr(response, "provider_id", "") or selected_provider.get("provider_id") or "",
        getattr(response, "provider_model", "") or getattr(response, "model", "") or selected_provider.get("provider_model") or "",
    )
    raw = response.content.strip()

    # Cards are opt-in — only created when the planner returns a fenced
    # ```cards``` JSON block (which it does only when the user explicitly asked
    # to add/plan tasks, per the system prompt). Plain conversational replies
    # are saved as text and create zero cards.
    parsed = _extract_card_mutations_block(raw)
    create_data = _normalize_cards_data(parsed.get("create") or [])
    update_data = _normalize_card_updates_data(parsed.get("update") or [])
    delete_data = _normalize_card_deletes_data(parsed.get("delete") or [])
    stripped_reply = _strip_cards_block(raw)
    reply_text = _normalize_html_artifacts(stripped_reply)
    plain_reply_raw = _safe_chat_raw(raw)
    plain_chat_message = _PLAIN_CHAT_RE.match(str(body.message or "").strip().lower()) is not None
    requested_cards = _allow_card_creation(body.message, reply_text or raw) or (
        bool(create_data or update_data or delete_data) and not plain_chat_message
    )
    intent_detection = detect_chat_intent(body.message)
    if requested_cards and not (create_data or update_data or delete_data):
        try:
            repaired_mutations, repair_raw = await _repair_cards_block(
                registry=registry,
                system_prompt=system_prompt,
                message=body.message,
                context=context,
                reply=reply_text or raw,
            )
            if repaired_mutations:
                create_data = _normalize_cards_data(repaired_mutations.get("create") or [])
                update_data = _normalize_card_updates_data(repaired_mutations.get("update") or [])
                delete_data = _normalize_card_deletes_data(repaired_mutations.get("delete") or [])
                raw = (raw + "\n\n" + repair_raw).strip() if repair_raw else raw
        except Exception:
            pass
    blocked_card_count = 0
    if (create_data or update_data or delete_data) and not requested_cards:
        blocked_card_count = len(create_data) + len(update_data) + len(delete_data)
        create_data = []
        update_data = []
        delete_data = []
        reply_text = _blocked_card_reply(body.message, reply_text)

    audit = request.app.state.audit_logs.get(body.project_id)
    if audit is not None:
        audit.record("chat.planner", {
            "session_id": body.session_id,
            "tokens": response.tokens_prompt + response.tokens_completion,
            "model": response.model,
            "provider_id": provider_fields.get("provider_id") or "",
            "card_count": len(create_data) + len(update_data) + len(delete_data),
            "had_cards_block": bool(create_data or update_data or delete_data),
            "blocked_card_count": blocked_card_count,
        })

    if not (create_data or update_data or delete_data):
        # Conversational reply — no cards, just save the assistant's prose.
        assistant_message = SessionMessage(role="assistant")
        assistant_content = _card_generation_failed_reply(reply_text) if requested_cards else (reply_text or _safe_visible_chat_reply(raw) or "(empty reply)")
        assistant_content, assistant_images = _persist_chat_reply_images(
            request,
            body,
            message_id=assistant_message.id,
            project_root=project.root,
            content=assistant_content,
            raw=getattr(response, "raw", {}),
        )
        assistant_content = _append_live_web_provenance_reply(assistant_content, live_web_provenance)
        assistant_message.content = assistant_content
        assistant_message.metadata = {
            **({"raw": plain_reply_raw} if plain_reply_raw else {}),
            **({"images": assistant_images} if assistant_images else {}),
            **({"web_provenance": live_web_provenance} if live_web_provenance else {}),
            **provider_fields,
        }
        sessions.append(body.session_id, assistant_message)
        turn_state.update(body.session_id, role="assistant", content=assistant_content)
        scratchboard.update(body.session_id, role="assistant", content=assistant_content)
        temporal_scratchpad.append(body.session_id, "assistant", assistant_content, {"cards": 0})
        _record_message_receipt(ledger, body=body, message=assistant_message, direction="outbound", reply_to=user_message.id, metadata={"cards": 0, **({"raw": plain_reply_raw} if plain_reply_raw else {}), **({"images": assistant_images} if assistant_images else {}), **({"web_provenance": live_web_provenance} if live_web_provenance else {}), **provider_fields})
        _observe_chat_skill_promotion(request, body, user_message, assistant_message)
        if bus is not None:
            await bus.emit("chat.done", {**stream_meta, "reply": assistant_content, "cards": [], "raw": plain_reply_raw, "warning": "", **({"images": assistant_images} if assistant_images else {}), **({"web_provenance": live_web_provenance} if live_web_provenance else {}), **provider_fields})
        return {"cards": [], "reply": assistant_content, **({"raw": plain_reply_raw} if plain_reply_raw else {}), **({"images": assistant_images} if assistant_images else {}), **({"web_provenance": live_web_provenance} if live_web_provenance else {}), **provider_fields}

    card_operations = await _apply_card_mutations(
        board,
        create=create_data,
        update=update_data,
        delete=delete_data,
        default_status=_default_card_status(body.message),
        force_create_status=intent_detection.intent == "implementation",
    )
    autoheal_metadata: Dict[str, Any] = {}
    initial_unresolved = list(card_operations.get("unresolved", []))
    initial_summary = _mutation_summary(card_operations)
    if initial_unresolved and not initial_summary:
        error_payload = _card_apply_error_payload(
            unresolved=initial_unresolved,
            create_count=len(create_data),
            update_count=len(update_data),
            delete_count=len(delete_data),
        )
        autoheal_metadata = {"attempted": True, "error": error_payload, "applied": False}
        try:
            repaired_mutations, repair_raw = await _repair_card_apply_block(
                registry=registry,
                system_prompt=system_prompt,
                message=body.message,
                context=context,
                reply=reply_text,
                board_context=_board_cards_context_block(board),
                error_payload=error_payload,
            )
            repaired_create, repaired_update, repaired_delete = _normalize_repaired_mutations(repaired_mutations, error_payload=error_payload)
            if repaired_create or repaired_update or repaired_delete:
                repaired_operations = await _apply_card_mutations(
                    board,
                    create=repaired_create,
                    update=repaired_update,
                    delete=repaired_delete,
                    default_status=_default_card_status(body.message),
                    force_create_status=False,
                )
                repaired_summary = _mutation_summary(repaired_operations)
                if repaired_summary:
                    card_operations = repaired_operations
                    raw = (raw + "\n\n" + repair_raw).strip() if repair_raw else raw
                    autoheal_metadata = {"attempted": True, "error": error_payload, "applied": True}
        except Exception as exc:
            autoheal_metadata = {"attempted": True, "error": error_payload, "applied": False, "exception": sanitize_text(str(exc), max_chars=240)}
    unresolved_targets = list(card_operations.get("unresolved", []))
    summary = _mutation_summary(card_operations)
    assistant_content = (reply_text + "\n\n" + summary).strip() if reply_text and summary else (reply_text or summary)
    if unresolved_targets and not summary:
        assistant_content = _card_targeting_failed_reply(reply_text, unresolved_targets)
    elif unresolved_targets:
        assistant_content = (assistant_content + "\n\n" + _card_targeting_failed_reply("", unresolved_targets)).strip()
    assistant_message = SessionMessage(role="assistant")
    assistant_content, assistant_images = _persist_chat_reply_images(
        request,
        body,
        message_id=assistant_message.id,
        project_root=project.root,
        content=assistant_content,
        raw=getattr(response, "raw", {}),
    )
    assistant_message.content = assistant_content
    assistant_message.metadata = {
        **({"raw": _safe_chat_raw(raw)} if _safe_chat_raw(raw) else {}),
        "card_ids": [item.get("id") for item in card_operations.get("created", []) + card_operations.get("updated", []) if item.get("id")],
        "deleted_card_ids": [item.get("id") for item in card_operations.get("deleted", []) if item.get("id")],
        "unresolved_targets": unresolved_targets,
        **({"card_autoheal": autoheal_metadata} if autoheal_metadata else {}),
        **({"images": assistant_images} if assistant_images else {}),
        **({"web_provenance": live_web_provenance} if live_web_provenance else {}),
        **provider_fields,
    }
    sessions.append(body.session_id, assistant_message)
    turn_state.update(body.session_id, role="assistant", content=assistant_content)
    scratchboard.update(body.session_id, role="assistant", content=assistant_content)
    temporal_scratchpad.append(body.session_id, "assistant", assistant_content, {"cards": len(list(card_operations.get("created", [])) + list(card_operations.get("updated", [])) )})
    _record_message_receipt(ledger, body=body, message=assistant_message, direction="outbound", reply_to=user_message.id, metadata={
        "card_ids": [item.get("id") for item in card_operations.get("created", []) + card_operations.get("updated", []) if item.get("id")],
        "deleted_card_ids": [item.get("id") for item in card_operations.get("deleted", []) if item.get("id")],
        "unresolved_targets": unresolved_targets,
        **({"card_autoheal": autoheal_metadata} if autoheal_metadata else {}),
        **({"images": assistant_images} if assistant_images else {}),
        **({"web_provenance": live_web_provenance} if live_web_provenance else {}),
        **provider_fields,
    })
    _observe_chat_skill_promotion(
        request,
        body,
        user_message,
        assistant_message,
        created_count=len(list(card_operations.get("created", []))),
        updated_count=len(list(card_operations.get("updated", []))),
        deleted_count=len(list(card_operations.get("deleted", []))),
        unresolved_count=len(unresolved_targets),
    )
    if bus is not None:
        await bus.emit("chat.done", {
            **stream_meta,
            "reply": assistant_content,
            "cards": list(card_operations.get("created", [])) + list(card_operations.get("updated", [])),
            "deleted_cards": list(card_operations.get("deleted", [])),
            "unresolved_targets": unresolved_targets,
            **({"card_autoheal": autoheal_metadata} if autoheal_metadata else {}),
            "raw": _safe_chat_raw(raw),
            "warning": "",
            **({"images": assistant_images} if assistant_images else {}),
            **({"web_provenance": live_web_provenance} if live_web_provenance else {}),
            **provider_fields,
        })
    return {
        "cards": list(card_operations.get("created", [])) + list(card_operations.get("updated", [])),
        "deleted_cards": list(card_operations.get("deleted", [])),
        "unresolved_targets": unresolved_targets,
        **({"card_autoheal": autoheal_metadata} if autoheal_metadata else {}),
        "reply": assistant_content,
        **({"raw": _safe_chat_raw(raw)} if _safe_chat_raw(raw) else {}),
        **({"images": assistant_images} if assistant_images else {}),
        "web_provenance": live_web_provenance,
        **provider_fields,
    }


@router.post("/stream")
async def chat_stream(body: ChatRequest, request: Request) -> StreamingResponse:
    async def _generate() -> AsyncGenerator[str, None]:
        stream: Any = None
        pending_chunk: Optional[asyncio.Task] = None
        heartbeat: Optional[asyncio.Task] = None
        project_store = request.app.state.project_store
        project = project_store.get(body.project_id)
        if project is None:
            yield _sse_event("error", {"error": "project not found"})
            return

        boards: Dict[str, BoardService] = request.app.state.boards
        if body.project_id not in boards:
            boards[body.project_id] = BoardService(
                request.app.state.data_root,
                body.project_id,
                bus=request.app.state.bus,
                on_done=getattr(request.app.state, "on_card_done", None),
            )
        board = boards[body.project_id]

        sessions = SessionStore(request.app.state.data_root, body.project_id)
        ledger = _get_message_ledger(request, body.project_id)
        turn_state = _turn_state_store(request, body.project_id)
        scratchboard = _chat_scratchboard_store(request, body.project_id)
        temporal_scratchpad = _temporal_scratchpad_store(request, body.project_id)
        user_message = SessionMessage(role="user", content=body.message)
        sessions.append(body.session_id, user_message)
        user_turn_state = turn_state.update(body.session_id, role="user", content=body.message)
        scratchboard.update(body.session_id, role="user", content=body.message)
        temporal_scratchpad.append(body.session_id, "user", body.message, {"phase": "request", "streamed": True})
        _record_message_receipt(ledger, body=body, message=user_message, direction="inbound", metadata={"streamed": True})
        bus = getattr(request.app.state, "bus", None)
        stream_meta = {
            "project_id": body.project_id,
            "session_id": body.session_id,
            "client_message_id": body.client_message_id,
            "turn_count": user_turn_state.turn_count,
            "user_turn_count": user_turn_state.user_turn_count,
            "assistant_turn_count": user_turn_state.assistant_turn_count,
            "ts": time.time(),
        }
        if bus is not None:
            await bus.emit("chat.started", stream_meta)
        yield _sse_event("started", stream_meta)
        progress_payload = {
            **stream_meta,
            "phase": "thinking",
            "detail": "Thinking through the request...",
            "elapsed_seconds": 0,
            "thinking_turns": 0,
            "reply_chunks": 0,
            "remaining": "until model finishes",
            "indeterminate": True,
        }
        if bus is not None:
            await bus.emit("chat.progress", progress_payload)
        yield _sse_event("progress", progress_payload)

        memory = request.app.state.project_memories.get(body.project_id)
        if memory is None:
            from blackboard.workspace.memory import ProjectMemory
            memory = ProjectMemory(request.app.state.data_root, body.project_id, project_intel_dir=request.app.state.project_intel_dir)
            request.app.state.project_memories[body.project_id] = memory
        adaptive = synthesize_adaptive_skills(
            data_root=request.app.state.data_root,
            project_id=body.project_id,
            cwd=project.root,
            query=body.message,
            session_id=body.session_id,
        )

        context = _organized_chat_context(
            project_intelligence=memory.context_block(project.root),
            conversation_state=turn_state.context_block(body.session_id),
            session_scratchboard=scratchboard.context_block(body.session_id),
            temporal_scratchpad=temporal_scratchpad.context_block(body.session_id),
            adaptive_skills=adaptive.get("context_block", ""),
            relevant_session_memory=_relevant_session_memory_block(sessions, body.message, body.session_id, current_message_id=user_message.id),
            recent_session_history=_session_history_block(sessions, body.session_id, current_message_id=user_message.id),
            board_cards=_board_cards_context_block(board),
            relevant_board_cards=_relevant_board_cards_block(board, body.message),
            recent_board_targets=_recent_board_targets_block(sessions, body.session_id, board, current_message_id=user_message.id),
        )

        registry = get_provider_registry()
        system_prompt = get_prompts().get("planner.system")
        live_web_context_block = await _chat_live_web_context_block(
            body.message,
            project_id=body.project_id,
            session_id=body.session_id,
        )
        live_web_provenance = _extract_live_web_provenance_from_block(live_web_context_block)
        user_prompt = _PLANNER_TEMPLATE.format(
            system=system_prompt,
            message=body.message,
            context=context or "(no project intelligence yet)",
            live_web_context_block=live_web_context_block,
        )
        selected_provider: Dict[str, str] = {}

        async def _stream_call(provider):
            selected_provider.update(_provider_fields(getattr(provider, "id", ""), getattr(provider, "model", "")))
            if _provider_supports_complete(provider):
                async for event in _chat_react_event_stream(
                    provider,
                    request=request,
                    body=body,
                    project_root=project.root,
                    context=context,
                    system_prompt=system_prompt,
                    live_web_context_block=live_web_context_block,
                    stream_meta=stream_meta,
                ):
                    yield event
                return
            if not _provider_supports_stream(provider):
                raise RuntimeError("planner provider does not support chat streaming or ReAct completion")
            async for token in provider.stream(
                [Message(role="user", content=user_prompt)],
                temperature=0.2,
            ):
                yield token

        try:
            raw_parts: List[str] = []
            visible_parts: List[str] = []
            filter_state = _ThinkStreamFilter()
            cards_filter = _CardsStreamFilter()
            started_at = time.monotonic()
            thinking_turns = 0
            reply_chunks = 0
            stream_run_meta: Dict[str, Any] = {}
            stream = registry.stream_with_fallback("planner", _stream_call)
            pending_chunk = asyncio.create_task(stream.__anext__())
            while True:
                heartbeat = asyncio.create_task(asyncio.sleep(_STREAM_HEARTBEAT_SECONDS))
                done, _ = await asyncio.wait({pending_chunk, heartbeat}, return_when=asyncio.FIRST_COMPLETED)
                if heartbeat in done and pending_chunk not in done:
                    heartbeat_payload = {
                        **stream_meta,
                        "phase": "thinking" if not reply_chunks else "bleeping",
                        "detail": "Still working...",
                        "heartbeat": True,
                        "elapsed_seconds": round(time.monotonic() - started_at, 1),
                        "thinking_turns": thinking_turns,
                        "reply_chunks": reply_chunks,
                        "remaining": "until model finishes",
                        "indeterminate": True,
                        "ts": time.time(),
                    }
                    if bus is not None:
                        await bus.emit("chat.progress", heartbeat_payload)
                    yield _sse_event("progress", heartbeat_payload)
                    heartbeat = None
                    continue
                if heartbeat is not None:
                    heartbeat.cancel()
                    try:
                        await heartbeat
                    except (asyncio.CancelledError, Exception):
                        pass
                    heartbeat = None
                try:
                    token = pending_chunk.result()
                except StopAsyncIteration:
                    pending_chunk = None
                    break
                pending_chunk = asyncio.create_task(stream.__anext__())
                if isinstance(token, dict):
                    if token.get("type") == "thinking":
                        thinking = str(token.get("content") or "")
                        if thinking:
                            thinking_turns += 1
                            payload = {**stream_meta, "content": thinking, "thinking_turns": thinking_turns}
                            if bus is not None:
                                await bus.emit("chat.thinking", payload)
                            yield _sse_event("thinking", payload)
                    elif token.get("type") == "progress":
                        payload = dict(token.get("payload") or {})
                        if payload:
                            thinking_turns = max(thinking_turns, int(payload.get("thinking_turns") or 0))
                            reply_chunks = max(reply_chunks, int(payload.get("reply_chunks") or 0))
                            if bus is not None:
                                await bus.emit("chat.progress", payload)
                            yield _sse_event("progress", payload)
                    elif token.get("type") == "react_meta":
                        stream_run_meta = dict(token.get("meta") or {})
                    continue
                token_text = str(token or "")
                raw_parts.append(token_text)
                visible, hidden = filter_state.feed(token_text)
                if hidden:
                    thinking_turns += 1
                    payload = {**stream_meta, "content": hidden, "thinking_turns": thinking_turns}
                    if bus is not None:
                        await bus.emit("chat.thinking", payload)
                    yield _sse_event("thinking", payload)
                if visible:
                    visible_without_cards = cards_filter.feed(visible)
                    if visible_without_cards:
                        reply_chunks += 1
                        visible_parts.append(visible_without_cards)
                        payload = {**stream_meta, "token": visible_without_cards, "reply_chunks": reply_chunks}
                        if bus is not None:
                            await bus.emit("chat.token", payload)
                        yield _sse_event("token", payload)
            tail_visible, tail_hidden = filter_state.flush()
            if tail_hidden:
                thinking_turns += 1
                payload = {**stream_meta, "content": tail_hidden, "thinking_turns": thinking_turns}
                if bus is not None:
                    await bus.emit("chat.thinking", payload)
                yield _sse_event("thinking", payload)
            if tail_visible:
                visible_without_cards = cards_filter.feed(tail_visible)
                if visible_without_cards:
                    reply_chunks += 1
                    visible_parts.append(visible_without_cards)
                    payload = {**stream_meta, "token": visible_without_cards, "reply_chunks": reply_chunks}
                    if bus is not None:
                        await bus.emit("chat.token", payload)
                    yield _sse_event("token", payload)
            tail_without_cards = cards_filter.flush()
            if tail_without_cards:
                reply_chunks += 1
                visible_parts.append(tail_without_cards)
                payload = {**stream_meta, "token": tail_without_cards, "reply_chunks": reply_chunks}
                if bus is not None:
                    await bus.emit("chat.token", payload)
                yield _sse_event("token", payload)
            raw = "".join(raw_parts).strip()
            visible_raw = "".join(visible_parts).strip()
        except Exception as exc:
            warning = f"planner unavailable: {exc}"
            system_message = SessionMessage(
                role="system",
                content=warning,
                metadata={"error": True},
            )
            sessions.append(body.session_id, system_message)
            turn_state.update(body.session_id, role="system", content=warning)
            scratchboard.update(body.session_id, role="system", content=warning)
            temporal_scratchpad.append(body.session_id, "system", warning, {"phase": "planner_error", "streamed": True})
            _record_message_receipt(ledger, body=body, message=system_message, direction="internal", status="error", event_kind="planner_error", reply_to=user_message.id, metadata={"streamed": True})
            payload = {**stream_meta, "error": warning}
            if bus is not None:
                await bus.emit("chat.error", payload)
            yield _sse_event("error", payload)
            return
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                try:
                    await heartbeat
                except (asyncio.CancelledError, Exception):
                    pass
            if pending_chunk is not None:
                pending_chunk.cancel()
                try:
                    await pending_chunk
                except (StopAsyncIteration, asyncio.CancelledError, Exception):
                    pass
            if stream is not None:
                try:
                    await asyncio.shield(stream.aclose())
                except RuntimeError:
                    pass
                except (asyncio.CancelledError, Exception):
                    pass

        provider_fields = _provider_fields(
            stream_run_meta.get("provider_id") or selected_provider.get("provider_id") or "",
            stream_run_meta.get("provider_model") or stream_run_meta.get("model") or selected_provider.get("provider_model") or "",
        )
        parsed = _extract_card_mutations_block(raw)
        create_data = _normalize_cards_data(parsed.get("create") or [])
        update_data = _normalize_card_updates_data(parsed.get("update") or [])
        delete_data = _normalize_card_deletes_data(parsed.get("delete") or [])
        stripped_reply = _strip_cards_block(visible_raw or raw)
        reply_text = _normalize_html_artifacts(stripped_reply)
        plain_reply_raw = _safe_chat_raw(raw)
        requested_cards = _allow_card_creation(body.message, reply_text or raw)
        intent_detection = detect_chat_intent(body.message)
        if requested_cards and not (create_data or update_data or delete_data):
            try:
                repaired_mutations, repair_raw = await _repair_cards_block(
                    registry=registry,
                    system_prompt=system_prompt,
                    message=body.message,
                    context=context,
                    reply=reply_text or raw,
                )
                if repaired_mutations:
                    create_data = _normalize_cards_data(repaired_mutations.get("create") or [])
                    update_data = _normalize_card_updates_data(repaired_mutations.get("update") or [])
                    delete_data = _normalize_card_deletes_data(repaired_mutations.get("delete") or [])
                    raw = (raw + "\n\n" + repair_raw).strip() if repair_raw else raw
            except Exception:
                pass
        blocked_card_count = 0
        if (create_data or update_data or delete_data) and not requested_cards:
            blocked_card_count = len(create_data) + len(update_data) + len(delete_data)
            create_data = []
            update_data = []
            delete_data = []
            reply_text = _blocked_card_reply(body.message, reply_text)

        audit = request.app.state.audit_logs.get(body.project_id)
        if audit is not None:
            audit.record("chat.planner", {
                "session_id": body.session_id,
                "tokens": 0,
                "model": str(stream_run_meta.get("model") or ""),
                "provider_id": provider_fields.get("provider_id") or "",
                "card_count": len(create_data) + len(update_data) + len(delete_data),
                "had_cards_block": bool(create_data or update_data or delete_data),
                "blocked_card_count": blocked_card_count,
                "streamed": True,
                "tool_calls": int(stream_run_meta.get("tool_calls") or 0),
                "iterations": int(stream_run_meta.get("iterations") or 0),
            })

        if not (create_data or update_data or delete_data):
            assistant_message = SessionMessage(role="assistant")
            assistant_content = _card_generation_failed_reply(reply_text) if requested_cards else (reply_text or _safe_visible_chat_reply(raw) or "(empty reply)")
            assistant_content, assistant_images = _persist_chat_reply_images(
                request,
                body,
                message_id=assistant_message.id,
                project_root=project.root,
                content=assistant_content,
                raw={},
            )
            assistant_content = _append_live_web_provenance_reply(assistant_content, live_web_provenance)
            assistant_message.content = assistant_content
            assistant_message.metadata = {
                **({"raw": plain_reply_raw} if plain_reply_raw else {}),
                **({"images": assistant_images} if assistant_images else {}),
                **({"web_provenance": live_web_provenance} if live_web_provenance else {}),
                **provider_fields,
            }
            sessions.append(body.session_id, assistant_message)
            turn_state.update(body.session_id, role="assistant", content=assistant_content)
            scratchboard.update(body.session_id, role="assistant", content=assistant_content)
            temporal_scratchpad.append(body.session_id, "assistant", assistant_content, {"cards": 0, "streamed": True})
            _record_message_receipt(ledger, body=body, message=assistant_message, direction="outbound", reply_to=user_message.id, metadata={"cards": 0, "streamed": True, **({"raw": plain_reply_raw} if plain_reply_raw else {}), **({"images": assistant_images} if assistant_images else {}), **({"web_provenance": live_web_provenance} if live_web_provenance else {}), **provider_fields})
            _observe_chat_skill_promotion(request, body, user_message, assistant_message)
            cards = []
            deleted_cards = []
            unresolved_targets = []
            autoheal_metadata = {}
            raw_out = plain_reply_raw
        else:
            card_operations = await _apply_card_mutations(
                board,
                create=create_data,
                update=update_data,
                delete=delete_data,
                default_status=_default_card_status(body.message),
                force_create_status=intent_detection.intent == "implementation",
            )
            autoheal_metadata: Dict[str, Any] = {}
            initial_unresolved = list(card_operations.get("unresolved", []))
            initial_summary = _mutation_summary(card_operations)
            if initial_unresolved and not initial_summary:
                error_payload = _card_apply_error_payload(
                    unresolved=initial_unresolved,
                    create_count=len(create_data),
                    update_count=len(update_data),
                    delete_count=len(delete_data),
                )
                autoheal_metadata = {"attempted": True, "error": error_payload, "applied": False}
                try:
                    repaired_mutations, repair_raw = await _repair_card_apply_block(
                        registry=registry,
                        system_prompt=system_prompt,
                        message=body.message,
                        context=context,
                        reply=reply_text,
                        board_context=_board_cards_context_block(board),
                        error_payload=error_payload,
                    )
                    repaired_create, repaired_update, repaired_delete = _normalize_repaired_mutations(repaired_mutations, error_payload=error_payload)
                    if repaired_create or repaired_update or repaired_delete:
                        repaired_operations = await _apply_card_mutations(
                            board,
                            create=repaired_create,
                            update=repaired_update,
                            delete=repaired_delete,
                            default_status=_default_card_status(body.message),
                            force_create_status=False,
                        )
                        repaired_summary = _mutation_summary(repaired_operations)
                        if repaired_summary:
                            card_operations = repaired_operations
                            raw = (raw + "\n\n" + repair_raw).strip() if repair_raw else raw
                            autoheal_metadata = {"attempted": True, "error": error_payload, "applied": True}
                except Exception as exc:
                    autoheal_metadata = {"attempted": True, "error": error_payload, "applied": False, "exception": sanitize_text(str(exc), max_chars=240)}
            unresolved_targets = list(card_operations.get("unresolved", []))
            summary = _mutation_summary(card_operations)
            assistant_content = (reply_text + "\n\n" + summary).strip() if reply_text and summary else (reply_text or summary)
            if unresolved_targets and not summary:
                assistant_content = _card_targeting_failed_reply(reply_text, unresolved_targets)
            elif unresolved_targets:
                assistant_content = (assistant_content + "\n\n" + _card_targeting_failed_reply("", unresolved_targets)).strip()
            assistant_message = SessionMessage(role="assistant")
            assistant_content, assistant_images = _persist_chat_reply_images(
                request,
                body,
                message_id=assistant_message.id,
                project_root=project.root,
                content=assistant_content,
                raw={},
            )
            assistant_message.content = assistant_content
            assistant_message.metadata = {
                **({"raw": _safe_chat_raw(raw)} if _safe_chat_raw(raw) else {}),
                "card_ids": [item.get("id") for item in card_operations.get("created", []) + card_operations.get("updated", []) if item.get("id")],
                "deleted_card_ids": [item.get("id") for item in card_operations.get("deleted", []) if item.get("id")],
                "unresolved_targets": unresolved_targets,
                **({"card_autoheal": autoheal_metadata} if autoheal_metadata else {}),
                **({"images": assistant_images} if assistant_images else {}),
                **({"web_provenance": live_web_provenance} if live_web_provenance else {}),
                **provider_fields,
            }
            sessions.append(body.session_id, assistant_message)
            turn_state.update(body.session_id, role="assistant", content=assistant_content)
            scratchboard.update(body.session_id, role="assistant", content=assistant_content)
            temporal_scratchpad.append(body.session_id, "assistant", assistant_content, {"cards": len(list(card_operations.get("created", [])) + list(card_operations.get("updated", []))), "streamed": True})
            _record_message_receipt(ledger, body=body, message=assistant_message, direction="outbound", reply_to=user_message.id, metadata={
                "card_ids": [item.get("id") for item in card_operations.get("created", []) + card_operations.get("updated", []) if item.get("id")],
                "deleted_card_ids": [item.get("id") for item in card_operations.get("deleted", []) if item.get("id")],
                "unresolved_targets": unresolved_targets,
                "streamed": True,
                **({"card_autoheal": autoheal_metadata} if autoheal_metadata else {}),
                **({"images": assistant_images} if assistant_images else {}),
                **({"web_provenance": live_web_provenance} if live_web_provenance else {}),
                **provider_fields,
            })
            _observe_chat_skill_promotion(
                request,
                body,
                user_message,
                assistant_message,
                created_count=len(list(card_operations.get("created", []))),
                updated_count=len(list(card_operations.get("updated", []))),
                deleted_count=len(list(card_operations.get("deleted", []))),
                unresolved_count=len(unresolved_targets),
            )
            cards = list(card_operations.get("created", [])) + list(card_operations.get("updated", []))
            deleted_cards = list(card_operations.get("deleted", []))
            raw_out = _safe_chat_raw(raw)

        done_payload = {**stream_meta, "reply": assistant_content, "cards": cards, "deleted_cards": deleted_cards, "unresolved_targets": unresolved_targets, **({"card_autoheal": autoheal_metadata} if autoheal_metadata else {}), "raw": raw_out, "warning": "", **({"images": assistant_images} if assistant_images else {}), **({"web_provenance": live_web_provenance} if live_web_provenance else {}), **provider_fields}
        if bus is not None:
            await bus.emit("chat.done", done_payload)
        yield _sse_event("done", done_payload)

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── Session reload / list / delete ─────────────────────────────────


@router.get("/{project_id}/sessions")
async def list_chat_sessions(project_id: str, request: Request) -> Dict[str, Any]:
    """List the chat sessions that exist on disk for a project. Newest first."""
    sessions = SessionStore(request.app.state.data_root, project_id)
    return {"sessions": sessions.list_summaries()}


@router.get("/{project_id}/sessions/{session_id}/history")
async def get_chat_history(
    project_id: str,
    session_id: str,
    request: Request,
    limit: int = 200,
) -> Dict[str, Any]:
    """Return up to ``limit`` recent messages for the session (oldest first)."""
    if limit < 1 or limit > 1000:
        raise HTTPException(400, "limit must be between 1 and 1000")
    sessions = SessionStore(request.app.state.data_root, project_id)
    msgs = sessions.tail(session_id, limit=limit)
    return {
        "session_id": session_id,
        "turn_state": TurnStateStore(request.app.state.data_root, project_id).load(session_id).to_dict(),
        "history": [
            {
                "id": m.id,
                "role": m.role,
                "content": m.content,
                "ts": m.ts,
                "metadata": m.metadata,
            }
            for m in msgs
        ],
        "count": len(msgs),
    }


@router.post("/{project_id}/sessions/{session_id}/attachments/{message_id}")
async def register_chat_attachments(
    project_id: str,
    session_id: str,
    message_id: str,
    body: ChatAttachmentRegisterRequest,
    request: Request,
) -> Dict[str, Any]:
    project = request.app.state.project_store.get(project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    attachments = [item.model_dump() for item in list(body.attachments or []) if item is not None]
    if not attachments:
        raise HTTPException(400, "attachments are required")
    images = register_chat_images(
        data_root=request.app.state.data_root,
        project_id=project_id,
        session_id=session_id,
        message_id=message_id,
        project_root=str(body.project_root or project.root or ""),
        attachments=attachments,
    )
    if not images:
        raise HTTPException(422, "no valid image attachments were registered")
    return {
        "project_id": project_id,
        "session_id": session_id,
        "message_id": message_id,
        "images": images,
        "count": len(images),
    }


@router.get("/{project_id}/sessions/{session_id}/attachments/{message_id}/{filename}")
async def get_chat_attachment(
    project_id: str,
    session_id: str,
    message_id: str,
    filename: str,
    request: Request,
) -> FileResponse:
    project = request.app.state.project_store.get(project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    try:
        path = resolve_chat_attachment_path(request.app.state.data_root, project_id, session_id, message_id, filename)
    except ValueError:
        raise HTTPException(404, "attachment not found")
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "attachment not found")
    return FileResponse(path)


@router.get("/{project_id}/receipts/recent")
async def recent_message_receipts(project_id: str, request: Request, limit: int = 50) -> Dict[str, Any]:
    ledger = _get_message_ledger(request, project_id)
    receipts = ledger.recent(limit=max(1, min(int(limit or 50), 200)))
    return {"receipts": _receipt_dicts(receipts), "count": len(receipts)}


@router.post("/{project_id}/receipts/search")
async def search_message_receipts(project_id: str, body: MessageReceiptSearchRequest, request: Request) -> Dict[str, Any]:
    ledger = _get_message_ledger(request, project_id)
    receipts = ledger.search(
        query=body.query,
        session_id=body.session_id,
        role=body.role,
        status=body.status,
        event_kind=body.event_kind,
        correlation_id=body.correlation_id,
        limit=max(1, min(int(body.limit or 20), 100)),
    )
    return {"receipts": _receipt_dicts(receipts), "count": len(receipts)}


@router.get("/{project_id}/receipts/trace/{correlation_id}")
async def trace_message_receipts(project_id: str, correlation_id: str, request: Request, limit: int = 100) -> Dict[str, Any]:
    ledger = _get_message_ledger(request, project_id)
    receipts = ledger.trace_correlation(correlation_id, limit=max(1, min(int(limit or 100), 200)))
    return {"correlation_id": correlation_id, "receipts": _receipt_dicts(receipts), "count": len(receipts)}


@router.delete("/{project_id}/sessions/{session_id}", status_code=204)
async def delete_chat_session(project_id: str, session_id: str, request: Request) -> None:
    """Remove the entire session file."""
    sessions = SessionStore(request.app.state.data_root, project_id)
    if not sessions.delete(session_id):
        raise HTTPException(404, "session not found")


@router.delete("/{project_id}/sessions/{session_id}/history", status_code=204)
async def clear_chat_history(project_id: str, session_id: str, request: Request) -> None:
    """Wipe the session's messages but keep the session id valid."""
    sessions = SessionStore(request.app.state.data_root, project_id)
    if not sessions.clear(session_id):
        raise HTTPException(404, "session not found")
