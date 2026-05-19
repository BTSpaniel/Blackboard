"""Trello-style 8-column board.

Columns: Inbox, Designing, Planning, Ready, Executing, Reviewing, Blocked, Done.
Persisted as a single board.json per project (small data, atomic writes), with
``board:card.*`` events emitted on every mutation.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from blackboard.kernel.atomic_files import write_text_atomically
from blackboard.kernel.bus import Bus, get_bus
from blackboard.kernel.logger import get_logger

logger = get_logger("workspace.board")

COLUMNS: List[str] = [
    "inbox",
    "designing",
    "planning",
    "ready",
    "executing",
    "reviewing",
    "blocked",
    "done",
]


@dataclass
class Card:
    id: str
    title: str
    body: str = ""
    status: str = "inbox"
    provider_role: str = "coder"
    files: List[str] = field(default_factory=list)
    verification: List[str] = field(default_factory=list)
    constraints: List[str] = field(default_factory=list)
    deps: List[str] = field(default_factory=list)
    progress: int = 0  # 0..100
    job_id: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class BoardService:
    """One board per project, kept in-memory + persisted to data/projects/<id>/board.json."""

    def __init__(
        self,
        data_root: Path,
        project_id: str,
        *,
        bus: Optional[Bus] = None,
        on_done: Optional[Any] = None,
    ) -> None:
        self._root = Path(data_root) / "projects" / project_id
        self._root.mkdir(parents=True, exist_ok=True)
        self._path = self._root / "board.json"
        self._project_id = project_id
        self._bus = bus or get_bus()
        self._cards: Dict[str, Card] = {}
        self._on_done = on_done  # async callable(project_id, card_dict) -> None
        self._load()

    def set_on_done(self, handler: Any) -> None:
        self._on_done = handler

    # ── Persistence ──────────────────────────────────────────────

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            for entry in data.get("cards") or []:
                card = Card(**{k: v for k, v in entry.items() if k in Card.__dataclass_fields__})
                self._cards[card.id] = card
        except Exception as exc:
            logger.warning("Board load failed: %s", exc)

    def _save(self, *, change_kind: str = "board.update", change_summary: str = "") -> None:
        payload = {"project_id": self._project_id, "cards": [c.to_dict() for c in self._cards.values()]}
        write_text_atomically(self._path, json.dumps(payload, indent=2))
        try:
            from blackboard.workspace.version_control import commit_safely
            msg = change_summary or f"board: update {self._project_id}"
            commit_safely(msg, kind=change_kind, paths=[str(self._path)])
        except Exception:
            pass

    # ── Queries ──────────────────────────────────────────────────

    def snapshot(self) -> Dict[str, Any]:
        cards_by_col: Dict[str, List[Dict[str, Any]]] = {col: [] for col in COLUMNS}
        for card in self._cards.values():
            cards_by_col.setdefault(card.status, []).append(card.to_dict())
        for col, items in cards_by_col.items():
            items.sort(key=lambda c: c.get("created_at", 0))
        return {"project_id": self._project_id, "columns": COLUMNS, "cards_by_column": cards_by_col}

    def all_cards(self) -> List[Dict[str, Any]]:
        cards = [card.to_dict() for card in self._cards.values()]
        cards.sort(key=lambda card: (float(card.get("updated_at") or 0), float(card.get("created_at") or 0)), reverse=True)
        return cards

    def get(self, card_id: str) -> Optional[Card]:
        return self._cards.get(card_id)

    def search_cards(self, *, query: str = "", limit: int = 20, status: str = "") -> List[Dict[str, Any]]:
        query_norm = str(query or "").strip().lower()
        status_norm = str(status or "").strip().lower()
        terms = [term for term in re.findall(r"[\w#@./:-]+", query_norm) if len(term) >= 2]
        cards = self.all_cards()
        if status_norm:
            cards = [card for card in cards if str(card.get("status") or "").strip().lower() == status_norm]
        if not query_norm:
            return cards[:max(1, int(limit or 20))]
        scored: List[tuple[float, Dict[str, Any]]] = []
        for card in cards:
            title = str(card.get("title") or "")
            body = str(card.get("body") or "")
            metadata = dict(card.get("metadata") or {})
            execution_objective = str(metadata.get("execution_objective") or "")
            fields = [
                str(card.get("id") or ""),
                str(card.get("status") or ""),
                title,
                body,
                execution_objective,
                " ".join(str(item or "") for item in card.get("files") or []),
                " ".join(str(item or "") for item in card.get("verification") or []),
                " ".join(str(item or "") for item in card.get("constraints") or []),
                " ".join(str(item or "") for item in card.get("deps") or []),
                " ".join(str(item or "") for item in card.get("tags") or []),
            ]
            haystack = "\n".join(fields).lower()
            if query_norm not in haystack and not any(term in haystack for term in terms):
                continue
            score = 0.0
            if query_norm and query_norm in title.lower():
                score += 10.0
            if query_norm and query_norm in body.lower():
                score += 4.0
            if query_norm and query_norm in execution_objective.lower():
                score += 5.0
            if query_norm and query_norm in haystack:
                score += 3.0
            for term in terms:
                if term in title.lower():
                    score += 3.0
                elif term in haystack:
                    score += 1.0
            score += min(float(card.get("updated_at") or 0), float(card.get("created_at") or 0)) / 1_000_000_000_000
            scored.append((score, card))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [card for _, card in scored[:max(1, int(limit or 20))]]

    # ── Mutations ────────────────────────────────────────────────

    async def create_card(self, **kwargs: Any) -> Card:
        card_id = kwargs.pop("id", None) or f"card_{uuid.uuid4().hex[:10]}"
        status = kwargs.pop("status", "inbox")
        if status not in COLUMNS:
            status = "inbox"
        card = Card(id=card_id, status=status, **{k: v for k, v in kwargs.items() if k in Card.__dataclass_fields__})
        self._cards[card_id] = card
        self._save(
            change_kind="board.card.created",
            change_summary=f"board[{self._project_id}]: create card '{(card.title or '')[:60]}' ({card_id})",
        )
        await self._bus.emit("board:card.created", {"project_id": self._project_id, "card": card.to_dict()})
        return card

    async def update_card(self, card_id: str, **updates: Any) -> Optional[Card]:
        card = self._cards.get(card_id)
        if card is None:
            return None
        previous_status = card.status
        requested_status = str(updates.get("status") or card.status or "").strip().lower()
        if requested_status == "done" and previous_status not in {"reviewing", "done"}:
            raise ValueError("card can only move to done from reviewing")
        if requested_status == "done" and previous_status == "reviewing":
            metadata = dict(card.metadata or {})
            autonomy = dict(metadata.get("autonomy") or {})
            autonomy["awaiting_human_review"] = False
            autonomy["last_review_decision"] = "approved_done"
            autonomy["last_review_decision_at"] = time.time()
            metadata["autonomy"] = autonomy
            metadata["human_review"] = {
                "decision": "approved_done",
                "approved_at": time.time(),
            }
            updates["metadata"] = metadata
        for key, value in updates.items():
            if key in Card.__dataclass_fields__ and key not in ("id", "created_at"):
                setattr(card, key, value)
        card.updated_at = time.time()
        if previous_status != card.status:
            summary = f"board[{self._project_id}]: move {card_id} {previous_status} → {card.status}"
            kind = "board.card.moved"
        else:
            changed = sorted(k for k in updates if k in Card.__dataclass_fields__ and k != "updated_at")
            summary = f"board[{self._project_id}]: update {card_id} ({', '.join(changed) or 'fields'})"
            kind = "board.card.updated"
        self._save(change_kind=kind, change_summary=summary)
        await self._bus.emit("board:card.updated", {"project_id": self._project_id, "card": card.to_dict()})
        # Fire the Done hook exactly once per transition.
        if previous_status != "done" and card.status == "done" and self._on_done:
            try:
                result = self._on_done(self._project_id, card.to_dict())
                if hasattr(result, "__await__"):
                    await result
            except Exception as exc:
                logger.debug("[board] on_done handler failed: %s", exc)
        return card

    async def move_card(self, card_id: str, status: str) -> Optional[Card]:
        if status not in COLUMNS:
            return None
        return await self.update_card(card_id, status=status)

    async def delete_card(self, card_id: str) -> bool:
        if card_id not in self._cards:
            return False
        del self._cards[card_id]
        self._save(
            change_kind="board.card.deleted",
            change_summary=f"board[{self._project_id}]: delete card {card_id}",
        )
        await self._bus.emit("board:card.deleted", {"project_id": self._project_id, "card_id": card_id})
        return True

    async def bulk_create(self, cards: List[Dict[str, Any]], *, status: str = "designing") -> List[Card]:
        created: List[Card] = []
        for entry in cards:
            params = dict(entry)
            params["status"] = status
            card = await self.create_card(**params)
            created.append(card)
        return created
