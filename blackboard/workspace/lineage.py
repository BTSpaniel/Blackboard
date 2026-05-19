"""Lineage — slim cross-card outcome index keyed by project + file + provider.

Append-only JSONL at ``data/projects/<id>/lineage.jsonl``. Used by the planner so it
can avoid proposing duplicate or just-failed work.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from blackboard.kernel.atomic_files import append_text_atomically
from blackboard.kernel.logger import get_logger

logger = get_logger("workspace.lineage")


@dataclass
class LineageRecord:
    trace_id: str = field(default_factory=lambda: f"trace_{uuid.uuid4().hex[:10]}")
    project_id: str = ""
    card_id: str = ""
    job_id: str = ""
    provider: str = ""
    role: str = ""
    files: List[str] = field(default_factory=list)
    success: bool = False
    summary: str = ""
    ts: float = field(default_factory=time.time)


class LineageStore:
    """Append-only lineage log per project."""

    def __init__(self, data_root: Path, project_id: str) -> None:
        self._path = Path(data_root) / "projects" / project_id / "lineage.jsonl"
        self._project_id = project_id

    def record(self, record: LineageRecord) -> None:
        record.project_id = record.project_id or self._project_id
        append_text_atomically(self._path, json.dumps(asdict(record), default=str) + "\n")

    def tail(self, limit: int = 100) -> List[LineageRecord]:
        if not self._path.exists():
            return []
        lines = self._path.read_text(encoding="utf-8").splitlines()
        out: List[LineageRecord] = []
        for line in lines[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                out.append(LineageRecord(**{k: v for k, v in data.items() if k in LineageRecord.__dataclass_fields__}))
            except Exception:
                continue
        return out

    def find_for_file(self, path: str, *, limit: int = 5) -> List[LineageRecord]:
        return [r for r in self.tail(500) if path in (r.files or [])][:limit]
