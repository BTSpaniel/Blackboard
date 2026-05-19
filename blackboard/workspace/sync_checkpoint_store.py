"""Sync checkpoint store — records pre-edit file contents so sync edits can be reverted.

Slim port of luna/state/sync_checkpoint_store.py.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from blackboard.kernel.atomic_files import write_text_atomically
from blackboard.kernel.logger import get_logger

logger = get_logger("workspace.sync_checkpoint_store")


@dataclass
class SyncCheckpointRecord:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    created_at: float = field(default_factory=time.time)
    cwd: str = ""
    objective: str = ""
    project_id: str = ""
    card_id: str = ""
    files_touched: List[str] = field(default_factory=list)
    status: str = "active"
    restored_at: Optional[float] = None
    restore_reason: str = ""
    restored_files: List[str] = field(default_factory=list)
    checkpoint_files: Dict[str, Optional[str]] = field(default_factory=dict, repr=False)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SyncCheckpointRecord":
        return cls(**{k: v for k, v in dict(data or {}).items() if k in cls.__dataclass_fields__})


class SyncCheckpointStore:
    def __init__(self, path: Optional[Path] = None, max_records: int = 200) -> None:
        self._path = Path(path) if path else None
        self._max = max(1, int(max_records))
        self._records: List[SyncCheckpointRecord] = []
        self._lock = threading.RLock()
        self._load()

    # ── Public API ───────────────────────────────────────────────

    def record(
        self,
        *,
        cwd: str,
        objective: str,
        files_touched: List[str],
        checkpoint_files: Dict[str, Optional[str]],
        project_id: str = "",
        card_id: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SyncCheckpointRecord:
        rec = SyncCheckpointRecord(
            cwd=str(cwd or ""),
            objective=str(objective or ""),
            project_id=str(project_id or ""),
            card_id=str(card_id or ""),
            files_touched=[str(x) for x in (files_touched or []) if x],
            checkpoint_files=dict(checkpoint_files or {}),
            metadata=dict(metadata or {}),
        )
        with self._lock:
            self._records.append(rec)
            if len(self._records) > self._max:
                self._records = self._records[-self._max:]
            self._save_locked()
        return rec

    def get(self, checkpoint_id: str) -> Optional[SyncCheckpointRecord]:
        with self._lock:
            for rec in reversed(self._records):
                if rec.id == checkpoint_id:
                    return SyncCheckpointRecord.from_dict(rec.to_dict())
        return None

    def latest(self, *, cwd: str = "", include_restored: bool = False) -> Optional[SyncCheckpointRecord]:
        with self._lock:
            for rec in reversed(self._records):
                if cwd and rec.cwd != cwd:
                    continue
                if not include_restored and rec.status == "restored":
                    continue
                return SyncCheckpointRecord.from_dict(rec.to_dict())
        return None

    def recent(self, *, limit: int = 20, cwd: str = "") -> List[SyncCheckpointRecord]:
        out: List[SyncCheckpointRecord] = []
        with self._lock:
            for rec in reversed(self._records):
                if cwd and rec.cwd != cwd:
                    continue
                out.append(SyncCheckpointRecord.from_dict(rec.to_dict()))
                if len(out) >= limit:
                    break
        return out

    def restore(self, checkpoint_id: str, *, reason: str = "") -> Optional[SyncCheckpointRecord]:
        """Restore files captured in a checkpoint to their pre-edit content."""
        rec = self.get(checkpoint_id)
        if rec is None:
            return None
        return self.restore_files(checkpoint_id, files=list(rec.checkpoint_files.keys()), reason=reason)

    def restore_files(self, checkpoint_id: str, *, files: List[str], reason: str = "") -> Optional[SyncCheckpointRecord]:
        """Restore only the selected files captured in a checkpoint."""
        rec = self.get(checkpoint_id)
        if rec is None:
            return None
        requested = [str(item or "") for item in (files or []) if str(item or "")]
        if not requested:
            return None
        checkpoint_files = dict(rec.checkpoint_files or {})
        selected = [path for path in requested if path in checkpoint_files]
        if not selected:
            return None
        for rel in selected:
            original = checkpoint_files.get(rel)
            path = Path(rel)
            try:
                if original is None:
                    # File did not exist before — remove it if present.
                    if path.exists():
                        path.unlink()
                else:
                    write_text_atomically(path, str(original))
            except Exception as exc:
                logger.warning("[checkpoint] restore failed for %s: %s", rel, exc)
        return self._mark_restored(checkpoint_id, restored_files=selected, reason=reason)

    def _mark_restored(self, checkpoint_id: str, *, restored_files: List[str], reason: str = "") -> Optional[SyncCheckpointRecord]:
        with self._lock:
            for idx in range(len(self._records) - 1, -1, -1):
                rec = self._records[idx]
                if rec.id != checkpoint_id:
                    continue
                merged = list(dict.fromkeys([*(rec.restored_files or []), *[str(item or "") for item in (restored_files or []) if str(item or "")]]))
                rec.restored_files = merged
                total_files = [str(item or "") for item in (rec.files_touched or list((rec.checkpoint_files or {}).keys())) if str(item or "")]
                rec.status = "restored" if total_files and set(merged) >= set(total_files) else "partial"
                rec.restored_at = time.time()
                rec.restore_reason = str(reason or "")
                self._save_locked()
                return SyncCheckpointRecord.from_dict(rec.to_dict())
        return None

    # ── Persistence ──────────────────────────────────────────────

    def _save_locked(self) -> None:
        if not self._path:
            return
        payload = [r.to_dict() for r in self._records]
        write_text_atomically(self._path, json.dumps(payload, indent=2), encoding="utf-8")

    def _load(self) -> None:
        if not self._path or not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                self._records = [SyncCheckpointRecord.from_dict(d) for d in data if isinstance(d, dict)]
                if len(self._records) > self._max:
                    self._records = self._records[-self._max:]
        except Exception as exc:
            logger.error("Failed to load sync checkpoint store: %s", exc)


_store: Optional[SyncCheckpointStore] = None
_store_lock = threading.Lock()


def init_sync_checkpoint_store(path: Path) -> SyncCheckpointStore:
    global _store
    with _store_lock:
        _store = SyncCheckpointStore(path)
    return _store


def get_sync_checkpoint_store() -> Optional[SyncCheckpointStore]:
    return _store
