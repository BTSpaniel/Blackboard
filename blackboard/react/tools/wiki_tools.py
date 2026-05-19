"""ReAct tools for Blackboard's markdown wiki."""
from __future__ import annotations

import json
from typing import Any, Dict

from blackboard.providers.base import Message
from blackboard.react.tool_registry import ToolRegistry

_wiki_manager: Any = None
_provider_registry: Any = None


def set_wiki_manager(manager: Any) -> None:
    global _wiki_manager
    _wiki_manager = manager


def set_wiki_provider_registry(registry: Any) -> None:
    global _provider_registry
    _provider_registry = registry


def get_wiki_manager() -> Any:
    if _wiki_manager is None:
        raise RuntimeError("wiki manager not available")
    return _wiki_manager


def _wiki_search(args: Dict[str, Any]) -> str:
    wiki = get_wiki_manager()
    query = str(args.get("query") or "")
    max_results = int(args.get("max_results") or 6)
    hits = wiki.search(query, max_results=max_results)
    return json.dumps({"query": query, "results": [page.to_dict(wiki.root) for page in hits]}, default=str)


def _wiki_read(args: Dict[str, Any]) -> str:
    wiki = get_wiki_manager()
    page = str(args.get("page") or "")
    content = wiki.read_page(page)
    return json.dumps({"page": page, "found": content is not None, "content": content or ""}, default=str)


def _wiki_write(args: Dict[str, Any]) -> str:
    wiki = get_wiki_manager()
    page = str(args.get("page") or "")
    content = str(args.get("content") or "")
    source = str(args.get("source") or "react")
    written = wiki.write_page(page, content, source=source)
    return json.dumps({"page": written.name, "written": True}, default=str)


def _wiki_stats(args: Dict[str, Any]) -> str:
    return json.dumps(get_wiki_manager().stats(), default=str)


def _wiki_health(args: Dict[str, Any]) -> str:
    return json.dumps(get_wiki_manager().health(), default=str)


async def _llm_call(prompt: str, *, max_tokens: int = 1500, temperature: float = 0.1) -> str:
    if _provider_registry is None:
        raise RuntimeError("provider registry not available for wiki LLM operation")
    response = await _provider_registry.call_with_fallback(
        "coder",
        lambda provider: provider.complete(
            [Message(role="user", content=prompt)],
            temperature=temperature,
            max_tokens=max_tokens,
        ),
    )
    return response.content


async def _wiki_ingest(args: Dict[str, Any]) -> str:
    wiki = get_wiki_manager()
    result = await wiki.ingest(
        str(args.get("source_text") or ""),
        str(args.get("source_name") or "source"),
        _llm_call,
        save_raw=bool(args.get("save_raw", True)),
    )
    return json.dumps(result, default=str)


async def _wiki_query(args: Dict[str, Any]) -> str:
    wiki = get_wiki_manager()
    answer = await wiki.query(
        str(args.get("question") or ""),
        _llm_call,
        file_answer=bool(args.get("file_answer", False)),
    )
    return json.dumps({"answer": answer}, default=str)


async def _wiki_lint(args: Dict[str, Any]) -> str:
    report = await get_wiki_manager().lint(_llm_call)
    return json.dumps({"report": report}, default=str)


def register_wiki_tools(registry: ToolRegistry) -> None:
    registry.register_fn(
        "wiki_search",
        "Search Blackboard's durable markdown wiki for relevant project knowledge.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer", "default": 6},
            },
            "required": ["query"],
        },
        _wiki_search,
        tags=["wiki", "read"],
    )
    registry.register_fn(
        "wiki_read",
        "Read a Blackboard wiki page by name.",
        {
            "type": "object",
            "properties": {"page": {"type": "string"}},
            "required": ["page"],
        },
        _wiki_read,
        tags=["wiki", "read"],
    )
    registry.register_fn(
        "wiki_write",
        "Write or update a Blackboard wiki page with durable project knowledge. This writes Blackboard data, not project source files.",
        {
            "type": "object",
            "properties": {
                "page": {"type": "string"},
                "content": {"type": "string"},
                "source": {"type": "string", "default": "react"},
            },
            "required": ["page", "content"],
        },
        _wiki_write,
        tags=["wiki", "write"],
        mutation_mode="unverified",
    )
    registry.register_fn(
        "wiki_stats",
        "Return Blackboard wiki statistics.",
        {"type": "object", "properties": {}},
        _wiki_stats,
        tags=["wiki", "read"],
    )
    registry.register_fn(
        "wiki_health",
        "Return deterministic Blackboard wiki health: orphan pages, broken links, missing summaries, and duplicate summaries.",
        {"type": "object", "properties": {}},
        _wiki_health,
        tags=["wiki", "read"],
    )
    registry.register_fn(
        "wiki_ingest",
        "Ingest a source document into Blackboard's wiki using the configured coder provider.",
        {
            "type": "object",
            "properties": {
                "source_text": {"type": "string"},
                "source_name": {"type": "string"},
                "save_raw": {"type": "boolean", "default": True},
            },
            "required": ["source_text", "source_name"],
        },
        _wiki_ingest,
        timeout_s=60.0,
        tags=["wiki", "write"],
        mutation_mode="unverified",
    )
    registry.register_fn(
        "wiki_query",
        "Ask a question answered only from Blackboard wiki pages.",
        {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "file_answer": {"type": "boolean", "default": False},
            },
            "required": ["question"],
        },
        _wiki_query,
        timeout_s=60.0,
        tags=["wiki", "read"],
    )
    registry.register_fn(
        "wiki_lint",
        "Create an LLM-assisted Blackboard wiki lint report.",
        {"type": "object", "properties": {}},
        _wiki_lint,
        timeout_s=60.0,
        tags=["wiki", "write"],
        mutation_mode="unverified",
    )
