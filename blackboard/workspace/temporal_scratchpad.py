from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict

from blackboard.kernel.atomic_files import write_text_atomically
from blackboard.workspace.redaction import sanitize_mapping, sanitize_text

_DEFAULT_MAX_CHARS = 18_000
_LOCK = threading.RLock()


class TemporalScratchpadStore:
    def __init__(self, data_root: Path, project_id: str, max_chars: int = _DEFAULT_MAX_CHARS) -> None:
        self._project_id = str(project_id or "default")
        self._root = Path(data_root) / "projects" / self._project_id / "temporal_scratchpad"
        self._root.mkdir(parents=True, exist_ok=True)
        self._max_chars = max(2_000, int(max_chars or _DEFAULT_MAX_CHARS))

    def _path(self, session_id: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(session_id or "main")) or "main"
        return self._root / f"{safe}.md"

    def read(self, session_id: str) -> str:
        path = self._path(session_id)
        if not path.exists():
            return ""
        try:
            return path.read_text(encoding="utf-8")
        except Exception:
            return ""

    def append(self, session_id: str, section: str, content: str, metadata: Dict[str, Any] | None = None) -> Path:
        path = self._path(session_id)
        text = sanitize_text(str(content or "").strip(), max_chars=1_600)
        if not text:
            return path
        section_name = re.sub(r"[^A-Za-z0-9_.:-]+", "-", str(section or "entry").strip().lower()).strip("-") or "entry"
        meta = sanitize_mapping(dict(metadata or {}))
        ts = time.time()
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
        lines = [f"## {section_name}", f"- ts: {stamp}"]
        if meta:
            lines.append(f"- metadata: `{json.dumps(meta, ensure_ascii=False, default=str)[:800]}`")
        lines.extend(["", text, ""])
        chunk = "\n".join(lines)
        with _LOCK:
            existing = self.read(session_id).strip()
            combined = f"{existing}\n\n{chunk}".strip() if existing else chunk
            write_text_atomically(path, self._compact(combined), encoding="utf-8")
        return path

    def append_orchestration_state(self, session_id: str, phase: str, state: Dict[str, Any]) -> Path:
        return self.append(
            session_id,
            f"orchestration.{phase}",
            f"Phase: {str(phase or 'unknown').strip() or 'unknown'}",
            {"phase": str(phase or ""), "state": sanitize_mapping(dict(state or {})), "type": "orchestration"},
        )

    def append_plan_state(self, session_id: str, plan_data: Dict[str, Any]) -> Path:
        plan = sanitize_mapping(dict(plan_data or {}))
        return self.append(
            session_id,
            "plan",
            f"Plan: {str(plan.get('title') or 'Untitled').strip() or 'Untitled'}",
            {"plan": plan, "type": "plan"},
        )

    def append_execution_metrics(self, session_id: str, metrics: Dict[str, Any]) -> Path:
        payload = sanitize_mapping(dict(metrics or {}))
        status = str(payload.get("status") or payload.get("outcome") or payload.get("phase") or "execution")
        return self.append(
            session_id,
            "metrics",
            f"Execution metrics: {status}",
            {"metrics": payload, "type": "metrics"},
        )

    def append_last_used(self, session_id: str, tool_name: str, context: Dict[str, Any]) -> Path:
        payload = sanitize_mapping(dict(context or {}))
        clean_tool = re.sub(r"[^A-Za-z0-9_.:-]+", "-", str(tool_name or "tool").strip()).strip("-") or "tool"
        return self.append(
            session_id,
            "last_used",
            f"Last used: {clean_tool}",
            {"tool": clean_tool, "context": payload, "type": "last_used"},
        )

    def context_block(self, session_id: str, *, max_chars: int = 4_000) -> str:
        content = self.read(session_id).strip()
        if not content:
            return ""
        body = self._tail(content, max_chars=max(500, int(max_chars or 4_000)))
        return f"<temporal_scratchpad>\n{body}\n</temporal_scratchpad>"

    def rotate_on_boot(self) -> int:
        rotated = 0
        ts = int(time.time())
        try:
            for path in self._root.glob("*.md"):
                archive = path.with_name(f"{path.stem}.boot-{ts}.bak")
                try:
                    path.rename(archive)
                    rotated += 1
                except Exception:
                    continue
        except Exception:
            return rotated
        return rotated

    def _compact(self, content: str) -> str:
        value = str(content or "")
        if len(value) <= self._max_chars:
            return value
        return self._tail(value, max_chars=max(1_000, self._max_chars // 2))

    @staticmethod
    def _tail(content: str, *, max_chars: int) -> str:
        value = str(content or "")
        if len(value) <= max_chars:
            return value
        keep = value[-max_chars:]
        marker = keep.find("\n## ")
        if marker > 0:
            keep = keep[marker + 1:]
        return keep.lstrip()


def rotate_all_temporal_scratchpads(data_root: Path) -> int:
    total = 0
    projects_root = Path(data_root) / "projects"
    if not projects_root.exists():
        return 0
    for project_dir in projects_root.iterdir():
        if not project_dir.is_dir():
            continue
        total += TemporalScratchpadStore(Path(data_root), project_dir.name).rotate_on_boot()
    return total


def coding_temporal_session_id(project_id: str, *, card_id: str = "", task_id: str = "", cwd: str = "") -> str:
    for candidate in (card_id, task_id):
        value = str(candidate or "").strip()
        if value:
            return value
    fallback = str(project_id or "").strip() or Path(str(cwd or ".")).resolve().name or "coding"
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", fallback) or "coding"
    return f"coding-{safe}"


def coding_temporal_context_block(
    data_root: Path,
    project_id: str,
    *,
    card_id: str = "",
    task_id: str = "",
    cwd: str = "",
    max_chars: int = 4_000,
) -> str:
    session_id = coding_temporal_session_id(project_id, card_id=card_id, task_id=task_id, cwd=cwd)
    return TemporalScratchpadStore(Path(data_root), project_id).context_block(session_id, max_chars=max_chars)
