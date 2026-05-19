"""Project memory facade — wraps coding.project_intelligence + receipts."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from blackboard.kernel.atomic_files import append_text_atomically, write_text_atomically
from blackboard.coding import project_intelligence as pi
from blackboard.governors.data_protection import get_data_protection_governor


class ProjectMemory:
    """Per-project memory store: project_intelligence summary + receipts."""

    def __init__(self, data_root: Path, project_id: str, *, project_intel_dir: Optional[Path] = None) -> None:
        self._root = Path(data_root)
        self._project_id = project_id
        self._memory_dir = self._root / "projects" / project_id / "memory"
        self._memory_dir.mkdir(parents=True, exist_ok=True)
        self._receipts_dir = self._root / "projects" / project_id / "receipts"
        self._receipts_dir.mkdir(parents=True, exist_ok=True)
        self._intel_dir = Path(project_intel_dir) if project_intel_dir else self._root / "project_intelligence"

    def project_intel_summary(self, cwd: str, *, task_files: Optional[List[str]] = None) -> Dict[str, Any]:
        return pi.ensure_project_intelligence(self._intel_dir, cwd=cwd, task_files=task_files or [])

    def context_block(self, cwd: str) -> str:
        summary = self.project_intel_summary(cwd)
        return pi.build_project_context_block(summary)

    def write_receipt(self, card_id: str, markdown_body: str) -> Path:
        path = self._receipts_dir / f"{card_id}.md"
        protected = get_data_protection_governor().protect_text(markdown_body, operation="persist_receipt")
        write_text_atomically(path, protected.protected)
        append_text_atomically(
            self._memory_dir / "recent_outcomes.jsonl",
            json.dumps({"ts": time.time(), "card_id": card_id, "receipt_path": str(path), **protected.metadata()}) + "\n",
        )
        return path

    def recent_outcomes(self, limit: int = 20) -> List[Dict[str, Any]]:
        path = self._memory_dir / "recent_outcomes.jsonl"
        if not path.exists():
            return []
        out: List[Dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        return out
