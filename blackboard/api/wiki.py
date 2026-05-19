"""Wiki endpoints."""
from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter(prefix="/api/wiki", tags=["wiki"])


class WikiWriteRequest(BaseModel):
    page: str
    content: str
    source: str = "api"


class WikiSearchRequest(BaseModel):
    query: str
    max_results: int = 6


class WikiIngestRequest(BaseModel):
    source_text: str
    source_name: str
    save_raw: bool = True


class WikiQueryRequest(BaseModel):
    question: str
    file_answer: bool = False


def _wiki(request: Request):
    manager = getattr(request.app.state, "wiki_manager", None)
    if manager is None:
        raise HTTPException(503, "wiki manager not available")
    return manager


async def _llm_call(request: Request, prompt: str, *, max_tokens: int = 1500, temperature: float = 0.1, **provider_kwargs: Any) -> str:
    from blackboard.providers.base import Message

    registry = getattr(request.app.state, "provider_registry", None)
    if registry is None:
        raise HTTPException(503, "provider registry not available")

    async def _call(provider):
        kwargs = dict(provider_kwargs)
        if not getattr(getattr(provider, "capabilities", None), "structured_output", False):
            kwargs.pop("response_format", None)
        return await provider.complete(
            [Message(role="user", content=prompt)],
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        )

    response = await registry.call_with_fallback(
        "coder",
        _call,
    )
    return response.content


@router.get("/stats")
async def wiki_stats(request: Request) -> Dict[str, Any]:
    return _wiki(request).stats()


@router.get("/health")
async def wiki_health(request: Request) -> Dict[str, Any]:
    return _wiki(request).health()


@router.get("/pages")
async def wiki_pages(request: Request) -> List[Dict[str, Any]]:
    wiki = _wiki(request)
    return [page.to_dict(wiki.root) for page in wiki.list_pages()]


@router.post("/search")
async def wiki_search(body: WikiSearchRequest, request: Request) -> Dict[str, Any]:
    wiki = _wiki(request)
    hits = wiki.search(body.query, max_results=body.max_results)
    return {"query": body.query, "results": [page.to_dict(wiki.root) for page in hits]}


@router.get("/page/{page:path}")
async def wiki_read(page: str, request: Request) -> Dict[str, Any]:
    wiki = _wiki(request)
    content = wiki.read_page(page)
    if content is None:
        raise HTTPException(404, "wiki page not found")
    path = wiki._page_path(page)  # noqa: SLF001
    return {"page": page, "content": content, "path": str(path.relative_to(wiki.root)).replace("\\", "/")}


@router.post("/page")
async def wiki_write(body: WikiWriteRequest, request: Request) -> Dict[str, Any]:
    wiki = _wiki(request)
    try:
        page = wiki.write_page(body.page, body.content, source=body.source)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return page.to_dict(wiki.root, include_content=True)


@router.post("/ingest")
async def wiki_ingest(body: WikiIngestRequest, request: Request) -> Dict[str, Any]:
    wiki = _wiki(request)
    return await wiki.ingest(
        body.source_text,
        body.source_name,
        lambda prompt, **kwargs: _llm_call(request, prompt, **kwargs),
        save_raw=body.save_raw,
    )


@router.post("/query")
async def wiki_query(body: WikiQueryRequest, request: Request) -> Dict[str, Any]:
    wiki = _wiki(request)
    answer = await wiki.query(
        body.question,
        lambda prompt, **kwargs: _llm_call(request, prompt, **kwargs),
        file_answer=body.file_answer,
    )
    return {"answer": answer}


@router.post("/lint")
async def wiki_lint(request: Request) -> Dict[str, Any]:
    wiki = _wiki(request)
    report = await wiki.lint(lambda prompt, **kwargs: _llm_call(request, prompt, **kwargs))
    return {"report": report}
