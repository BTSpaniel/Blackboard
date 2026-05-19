from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List

from blackboard.workspace.message_ledger import MessageLedger
from blackboard.workspace.sessions import SessionStore


def build_project_mailbox_block(data_root: Path, project_id: str, *, query: str = "", card_id: str = "", limit: int = 6) -> str:
    project = str(project_id or "default")
    terms = _terms(query)
    lines: List[str] = ["<project_mailbox>", "Project-level situational awareness for this coding job."]
    board_lines = _board_lines(Path(data_root), project, terms, card_id=card_id, limit=limit)
    receipt_lines = _receipt_lines(Path(data_root), project, query=query, limit=limit)
    session_lines = _session_lines(Path(data_root), project, query=query, limit=limit)
    if board_lines:
        lines.append("<board_state>")
        lines.extend(board_lines)
        lines.append("</board_state>")
    if receipt_lines:
        lines.append("<message_receipts>")
        lines.extend(receipt_lines)
        lines.append("</message_receipts>")
    if session_lines:
        lines.append("<recent_messages>")
        lines.extend(session_lines)
        lines.append("</recent_messages>")
    if len(lines) == 2:
        lines.append("No project mailbox messages, board signals, or receipts matched this job yet.")
    lines.append("</project_mailbox>")
    return "\n".join(lines)


def _board_lines(data_root: Path, project_id: str, terms: List[str], *, card_id: str, limit: int) -> List[str]:
    path = data_root / "projects" / project_id / "board.json"
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        if not isinstance(payload, dict):
            return []
        cards = list((payload.get("cards") or []))
    except Exception:
        return []
    ranked: List[tuple[float, Dict[str, Any]]] = []
    for card in cards:
        haystack = " ".join([
            str(card.get("id") or ""),
            str(card.get("title") or ""),
            str(card.get("body") or ""),
            " ".join(str(item) for item in (card.get("files") or [])),
            " ".join(str(item) for item in (card.get("tags") or [])),
            json.dumps(card.get("metadata") or {}, default=str),
        ]).lower()
        score = 1.0
        if str(card.get("id") or "") == str(card_id or ""):
            score += 25.0
        if terms:
            hits = sum(1 for term in terms if term in haystack)
            if hits <= 0 and str(card.get("id") or "") != str(card_id or ""):
                continue
            score += hits * 6.0
        metadata = dict(card.get("metadata") or {})
        if metadata.get("coordination"):
            score += 5.0
        if metadata.get("merge_recommendation"):
            score += 4.0
        if metadata.get("pending_questions"):
            score += 4.0
        try:
            score += min(float(card.get("updated_at") or 0.0) / 1_000_000_000.0, 2.0)
        except Exception:
            pass
        ranked.append((score, card))
    ranked.sort(key=lambda item: item[0], reverse=True)
    lines: List[str] = []
    for _, card in ranked[: max(1, int(limit or 6))]:
        metadata = dict(card.get("metadata") or {})
        signals = []
        if metadata.get("coordination"):
            signals.append("coordination paused")
        if metadata.get("merge_recommendation"):
            best = dict(metadata.get("merge_recommendation") or {}).get("best") or {}
            signals.append(f"merge suggested with {best.get('job_id') or 'related job'}")
        if metadata.get("pending_questions"):
            signals.append("checkpoint pending")
        suffix = f" signals={'; '.join(signals)}" if signals else ""
        files = ", ".join(str(item) for item in (card.get("files") or [])[:4])
        lines.append(f"- card={card.get('id')} status={card.get('status')} title={_clip(card.get('title'), 90)} files={files}{suffix}")
    return lines


def _receipt_lines(data_root: Path, project_id: str, *, query: str, limit: int) -> List[str]:
    try:
        ledger = MessageLedger(data_root, project_id)
        receipts = ledger.search(query=query, limit=limit) if str(query or "").strip() else ledger.recent(limit)
    except Exception:
        return []
    lines = []
    for receipt in receipts[: max(1, int(limit or 6))]:
        lines.append(f"- {receipt.event_kind}/{receipt.direction}/{receipt.status} session={receipt.session_id} role={receipt.role}: {_clip(receipt.content_preview, 160)}")
    return lines


def _session_lines(data_root: Path, project_id: str, *, query: str, limit: int) -> List[str]:
    try:
        store = SessionStore(data_root, project_id)
        matches = store.search_messages(query=query, limit=limit) if str(query or "").strip() else []
        if not matches:
            sessions = store.list_sessions()[-3:]
            for session_id in sessions:
                for message in store.tail(session_id, limit=2):
                    matches.append({"session_id": session_id, "role": message.role, "content": message.content})
    except Exception:
        return []
    lines = []
    for item in matches[: max(1, int(limit or 6))]:
        lines.append(f"- session={item.get('session_id')} role={item.get('role')}: {_clip(item.get('content'), 180)}")
    return lines


def _terms(text: str) -> List[str]:
    return [term for term in re.findall(r"[a-z0-9_./:-]{3,}", str(text or "").lower()) if term not in {"the", "and", "for", "with", "this", "that"}]


def _clip(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"
