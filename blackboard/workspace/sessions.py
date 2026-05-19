"""Per-project chat sessions — append-only JSONL.

Used by the planner to hydrate recent conversation context.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from blackboard.kernel.atomic_files import append_text_atomically
from blackboard.kernel.logger import get_logger

logger = get_logger("workspace.sessions")


@dataclass
class SessionMessage:
    id: str = field(default_factory=lambda: f"msg_{uuid.uuid4().hex[:10]}")
    role: str = "user"      # user | assistant | system
    content: str = ""
    ts: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)


class SessionStore:
    """One session per project. Append-only; tail-load on read."""

    def __init__(self, data_root: Path, project_id: str) -> None:
        self._root = Path(data_root) / "projects" / project_id / "sessions"
        self._root.mkdir(parents=True, exist_ok=True)
        self._project_id = project_id

    def _path(self, session_id: str) -> Path:
        return self._root / f"{session_id}.jsonl"

    def append(self, session_id: str, message: SessionMessage) -> None:
        path = self._path(session_id)
        line = json.dumps({
            "id": message.id,
            "role": message.role,
            "content": message.content,
            "ts": message.ts,
            "metadata": message.metadata,
        }) + "\n"
        append_text_atomically(path, line)

    def tail(self, session_id: str, limit: int = 40) -> List[SessionMessage]:
        path = self._path(session_id)
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()
        out: List[SessionMessage] = []
        for line in lines[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                out.append(SessionMessage(
                    id=str(data.get("id") or ""),
                    role=str(data.get("role") or "user"),
                    content=str(data.get("content") or ""),
                    ts=float(data.get("ts") or 0),
                    metadata=dict(data.get("metadata") or {}),
                ))
            except Exception:
                continue
        return out

    def list_sessions(self) -> List[str]:
        return sorted(p.stem for p in self._root.glob("*.jsonl"))

    def search_messages(
        self,
        *,
        query: str = "",
        session_id: str = "",
        limit: int = 8,
        include_assistant: bool = True,
    ) -> List[Dict[str, Any]]:
        query_norm = str(query or "").strip().lower()
        session_norm = str(session_id or "").strip()
        terms = [term for term in re.findall(r"[\w#@./:-]+", query_norm) if len(term) >= 2]
        if not terms and not session_norm:
            return []
        now = time.time()
        results: List[Dict[str, Any]] = []
        for path in sorted(self._root.glob("*.jsonl")):
            current_session = path.stem
            try:
                lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
            except Exception:
                continue
            for line in lines[-200:]:
                try:
                    data = json.loads(line)
                except Exception:
                    continue
                role = str(data.get("role") or "user")
                if not include_assistant and role == "assistant":
                    continue
                content = str(data.get("content") or "")
                if not content.strip():
                    continue
                haystack = content.lower()
                score = 0.0
                if terms:
                    hits = sum(1 for term in terms if term in haystack)
                    if hits <= 0:
                        continue
                    score += hits * 10.0
                    if query_norm and query_norm in haystack:
                        score += 8.0
                try:
                    ts = float(data.get("ts") or 0)
                except Exception:
                    ts = 0.0
                age_s = max(0.0, now - ts) if ts else 0.0
                score += max(0.0, 4.0 - min(age_s / 86_400.0, 4.0))
                if role == "user":
                    score += 2.0
                if session_norm and current_session == session_norm:
                    score += 3.0
                results.append({
                    "score": score,
                    "session_id": current_session,
                    "message_id": str(data.get("id") or ""),
                    "role": role,
                    "content": content,
                    "ts": ts,
                    "metadata": dict(data.get("metadata") or {}),
                })
        results.sort(key=lambda row: (float(row.get("score") or 0.0), float(row.get("ts") or 0.0)), reverse=True)
        return results[: max(1, int(limit or 8))]

    def list_summaries(self) -> List[Dict[str, Any]]:
        """Return one summary record per session — id, message_count, last activity,
        first user message (used as a default title in the UI)."""
        out: List[Dict[str, Any]] = []
        for path in sorted(self._root.glob("*.jsonl")):
            sid = path.stem
            try:
                lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
            except Exception:
                continue
            count = len(lines)
            first_user = ""
            last_ts = 0.0
            for raw in lines:
                try:
                    obj = json.loads(raw)
                except Exception:
                    continue
                if not first_user and obj.get("role") == "user":
                    first_user = str(obj.get("content") or "")[:60].strip()
                last_ts = max(last_ts, float(obj.get("ts") or 0))
            out.append({
                "session_id": sid,
                "message_count": count,
                "last_ts": last_ts,
                "title": first_user or sid,
            })
        # Newest activity first.
        out.sort(key=lambda r: r["last_ts"], reverse=True)
        return out

    def clear(self, session_id: str) -> bool:
        """Wipe a session's messages but keep the file (so the id stays valid)."""
        path = self._path(session_id)
        if not path.exists():
            return False
        path.write_text("", encoding="utf-8")
        return True

    def delete(self, session_id: str) -> bool:
        """Remove the session entirely."""
        path = self._path(session_id)
        if not path.exists():
            return False
        try:
            path.unlink()
            return True
        except Exception as exc:
            logger.warning("[sessions] delete failed for %s: %s", session_id, exc)
            return False
