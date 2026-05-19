"""Append-only audit log per project. Never stores resolved secrets."""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List

from blackboard.kernel.atomic_files import append_text_atomically
from blackboard.kernel.logger import get_logger
from blackboard.workspace.redaction import sanitize_mapping

logger = get_logger("workspace.audit")


class AuditLog:
    """Single JSONL file per project at data/projects/<id>/audit.jsonl."""

    def __init__(self, data_root: Path, project_id: str) -> None:
        self._root = Path(data_root) / "projects" / project_id
        self._root.mkdir(parents=True, exist_ok=True)
        self._path = self._root / "audit.jsonl"
        self._lock = threading.Lock()

    def record(
        self,
        kind: str,
        payload: Dict[str, Any] | None = None,
        *,
        actor: str = "system",
        session_id: str = "",
        outcome: str = "ok",
        duration_ms: float | None = None,
    ) -> Dict[str, Any]:
        entry = {
            "id": uuid.uuid4().hex[:16],
            "ts": time.time(),
            "kind": kind,
            "event_type": kind,
            "actor": actor,
            "session_id": session_id,
            "payload": sanitize_mapping(payload or {}),
            "outcome": outcome,
            "duration_ms": duration_ms,
        }
        with self._lock:
            append_text_atomically(self._path, json.dumps(entry, default=str) + "\n")
        return entry

    def tail(self, limit: int = 100) -> List[Dict[str, Any]]:
        if not self._path.exists():
            return []
        lines = self._path.read_text(encoding="utf-8").splitlines()
        out: List[Dict[str, Any]] = []
        for line in lines[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        return out
