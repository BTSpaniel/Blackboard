"""Board endpoints — list, create, move, update, delete cards."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from blackboard.workspace.board import BoardService

router = APIRouter(prefix="/api/board", tags=["board"])


class CardCreate(BaseModel):
    title: str
    body: str = ""
    status: str = "inbox"
    provider_role: str = "coder"
    files: List[str] = []
    verification: List[str] = []
    constraints: List[str] = []
    deps: List[str] = []
    tags: List[str] = []


class CardUpdate(BaseModel):
    title: Optional[str] = None
    body: Optional[str] = None
    status: Optional[str] = None
    progress: Optional[int] = None
    job_id: Optional[str] = None
    files: Optional[List[str]] = None
    verification: Optional[List[str]] = None
    constraints: Optional[List[str]] = None
    deps: Optional[List[str]] = None
    tags: Optional[List[str]] = None
    metadata: Optional[Dict[str, Any]] = None


def _board(request: Request, project_id: str) -> BoardService:
    boards: Dict[str, BoardService] = request.app.state.boards
    if project_id in boards:
        return boards[project_id]
    project = request.app.state.project_store.get(project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    board = BoardService(
        request.app.state.data_root,
        project_id,
        bus=request.app.state.bus,
        on_done=getattr(request.app.state, "on_card_done", None),
    )
    boards[project_id] = board
    return board


@router.get("/{project_id}")
async def get_board(project_id: str, request: Request) -> Dict[str, Any]:
    return _board(request, project_id).snapshot()


@router.get("/{project_id}/cards")
async def list_or_search_cards(
    project_id: str,
    request: Request,
    query: str = "",
    status: str = "",
    limit: int = Query(default=20, ge=1, le=200),
) -> Dict[str, Any]:
    board = _board(request, project_id)
    cards = board.search_cards(query=query, status=status, limit=limit)
    return {
        "project_id": project_id,
        "query": query,
        "status": status,
        "limit": limit,
        "cards": cards,
    }


@router.get("/{project_id}/cards/{card_id}")
async def get_card(project_id: str, card_id: str, request: Request) -> Dict[str, Any]:
    board = _board(request, project_id)
    card = board.get(card_id)
    if card is None:
        raise HTTPException(404, "card not found")
    return card.to_dict()


@router.post("/{project_id}/cards")
async def create_card(project_id: str, body: CardCreate, request: Request) -> Dict[str, Any]:
    board = _board(request, project_id)
    card = await board.create_card(**body.model_dump())
    return card.to_dict()


@router.put("/{project_id}/cards/{card_id}")
async def update_card(project_id: str, card_id: str, body: CardUpdate, request: Request) -> Dict[str, Any]:
    board = _board(request, project_id)
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    try:
        card = await board.update_card(card_id, **updates)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    if card is None:
        raise HTTPException(404, "card not found")
    return card.to_dict()


@router.delete("/{project_id}/cards/{card_id}")
async def delete_card(project_id: str, card_id: str, request: Request) -> Dict[str, Any]:
    board = _board(request, project_id)
    ok = await board.delete_card(card_id)
    if not ok:
        raise HTTPException(404, "card not found")
    return {"card_id": card_id, "deleted": True}
