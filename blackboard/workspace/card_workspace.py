"""Per-card workspace directory.

Layout under ``data/projects/<project_id>/cards/<card_id>/``:
    summary.json              — current_state, next_action, lineage, intervention
    scratchpad/working.md     — current attempt's scratch
    scratchpad/folded.md      — folded history of prior attempts
    scratchpad/notes.jsonl    — append-only thought/observation log
    scratchpad/tool_runs.jsonl — append-only ledger entries
    episodes/history.jsonl    — attempt records (success/failure)
    episodes/milestones.jsonl — verified mutations / passing tests
    episodes/blockers.jsonl   — failures the next attempt should not repeat
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from blackboard.kernel.atomic_files import append_text_atomically, write_text_atomically
from blackboard.kernel.logger import get_logger

logger = get_logger("workspace.card_workspace")

_LOCK = threading.RLock()


def _project_root(data_root: Path, project_id: str) -> Path:
    return Path(data_root) / "projects" / project_id


def card_dir(data_root: Path, project_id: str, card_id: str) -> Path:
    d = _project_root(data_root, project_id) / "cards" / card_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _append_jsonl(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    append_text_atomically(path, json.dumps(payload, default=str) + "\n", encoding="utf-8")


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    out: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


# ── Working scratchpad ──────────────────────────────────────────


def save_working_scratchpad(data_root: Path, project_id: str, card_id: str, content: str) -> None:
    with _LOCK:
        target = card_dir(data_root, project_id, card_id) / "scratchpad" / "working.md"
        write_text_atomically(target, content or "")


def load_working_scratchpad(data_root: Path, project_id: str, card_id: str) -> str:
    target = card_dir(data_root, project_id, card_id) / "scratchpad" / "working.md"
    return target.read_text(encoding="utf-8") if target.exists() else ""


def append_folded_scratchpad(data_root: Path, project_id: str, card_id: str, fold: str) -> None:
    with _LOCK:
        target = card_dir(data_root, project_id, card_id) / "scratchpad" / "folded.md"
        existing = target.read_text(encoding="utf-8") if target.exists() else ""
        sep = "\n\n---\n\n" if existing else ""
        write_text_atomically(target, existing + sep + fold)


def load_folded_scratchpad(data_root: Path, project_id: str, card_id: str) -> str:
    target = card_dir(data_root, project_id, card_id) / "scratchpad" / "folded.md"
    return target.read_text(encoding="utf-8") if target.exists() else ""


# ── Notes + tool ledger mirrors ─────────────────────────────────


def append_note(data_root: Path, project_id: str, card_id: str, payload: Dict[str, Any]) -> None:
    target = card_dir(data_root, project_id, card_id) / "scratchpad" / "notes.jsonl"
    _append_jsonl(target, {"ts": time.time(), **payload})


def append_tool_run(data_root: Path, project_id: str, card_id: str, payload: Dict[str, Any]) -> None:
    target = card_dir(data_root, project_id, card_id) / "scratchpad" / "tool_runs.jsonl"
    _append_jsonl(target, {"ts": time.time(), **payload})


# ── Episodes ─────────────────────────────────────────────────────


def append_history(data_root: Path, project_id: str, card_id: str, payload: Dict[str, Any]) -> None:
    target = card_dir(data_root, project_id, card_id) / "episodes" / "history.jsonl"
    _append_jsonl(target, {"ts": time.time(), **payload})


def append_milestone(data_root: Path, project_id: str, card_id: str, payload: Dict[str, Any]) -> None:
    target = card_dir(data_root, project_id, card_id) / "episodes" / "milestones.jsonl"
    _append_jsonl(target, {"ts": time.time(), **payload})


def append_blocker(data_root: Path, project_id: str, card_id: str, payload: Dict[str, Any]) -> None:
    target = card_dir(data_root, project_id, card_id) / "episodes" / "blockers.jsonl"
    _append_jsonl(target, {"ts": time.time(), **payload})


def recent_history(data_root: Path, project_id: str, card_id: str, limit: int = 3) -> List[Dict[str, Any]]:
    target = card_dir(data_root, project_id, card_id) / "episodes" / "history.jsonl"
    return _read_jsonl(target)[-limit:]


# ── Summary ──────────────────────────────────────────────────────


def save_summary(data_root: Path, project_id: str, card_id: str, summary: Dict[str, Any]) -> None:
    target = card_dir(data_root, project_id, card_id) / "summary.json"
    write_text_atomically(target, json.dumps(summary, indent=2, default=str))


def load_summary(data_root: Path, project_id: str, card_id: str) -> Dict[str, Any]:
    target = card_dir(data_root, project_id, card_id) / "summary.json"
    return _read_json(target, {})


def build_card_memory_block(data_root: Path, project_id: str, card_id: str) -> str:
    """Build the `<card_memory>` context block from summary + folded scratchpad."""
    summary = load_summary(data_root, project_id, card_id)
    folded = load_folded_scratchpad(data_root, project_id, card_id)
    if not summary and not folded:
        return ""
    lines = ["<card_memory>"]
    if summary:
        cs = str(summary.get("current_state") or "").strip()
        nx = str(summary.get("next_action") or "").strip()
        if cs:
            lines.append(f"current_state: {cs[:400]}")
        if nx:
            lines.append(f"next_action: {nx[:400]}")
    if folded:
        lines.append("folded_history:")
        lines.append(folded[:6000])
    lines.append("</card_memory>")
    return "\n".join(lines)


def build_prior_feedback_block(data_root: Path, project_id: str, card_id: str, *, limit: int = 3) -> str:
    history = recent_history(data_root, project_id, card_id, limit=limit)
    if not history:
        return ""
    lines = ["<prior_feedback>"]
    for entry in history:
        mark = "✓" if entry.get("success") else "✗"
        lines.append(
            f"  {mark} attempt={entry.get('attempt', '?')} "
            f"stopped={entry.get('stopped_reason', '?')} "
            f"files={entry.get('files_changed', [])[:5]} "
            f"err={(entry.get('error') or '')[:160]}"
        )
    lines.append("</prior_feedback>")
    return "\n".join(lines)
