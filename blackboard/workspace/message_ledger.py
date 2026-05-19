from __future__ import annotations

import json
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from blackboard.kernel.atomic_files import append_text_atomically, write_text_atomically
from blackboard.workspace.redaction import sanitize_mapping, sanitize_text


@dataclass
class MessageReceipt:
    id: str = field(default_factory=lambda: f"rcpt_{uuid.uuid4().hex[:12]}")
    message_id: str = ""
    project_id: str = ""
    session_id: str = ""
    timestamp: float = field(default_factory=time.time)
    event_kind: str = "message"
    role: str = ""
    direction: str = ""
    status: str = "recorded"
    source: str = "chat"
    user_id: str = ""
    principal_id: str = ""
    correlation_id: str = ""
    reply_to: str = ""
    run_id: str = ""
    payload_size: int = 0
    content_preview: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MessageReceipt":
        return cls(**{key: value for key, value in dict(data).items() if key in cls.__dataclass_fields__})


class MessageLedger:
    def __init__(self, data_root: Path, project_id: str, buffer_size: int = 5000) -> None:
        self._project_id = str(project_id or "")
        self._path = Path(data_root) / "projects" / self._project_id / "message_receipts.jsonl"
        self._buffer_size = max(1, int(buffer_size or 5000))
        self._receipts: List[MessageReceipt] = []
        self._lock = threading.RLock()
        self._load()

    def record(
        self,
        *,
        message_id: str = "",
        session_id: str = "",
        event_kind: str = "message",
        role: str = "",
        direction: str = "",
        status: str = "recorded",
        source: str = "chat",
        user_id: str = "",
        principal_id: str = "",
        correlation_id: str = "",
        reply_to: str = "",
        run_id: str = "",
        content: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> MessageReceipt:
        receipt = MessageReceipt(
            message_id=str(message_id or ""),
            project_id=self._project_id,
            session_id=str(session_id or ""),
            event_kind=str(event_kind or "message").strip().lower() or "message",
            role=str(role or "").strip().lower(),
            direction=str(direction or "").strip().lower(),
            status=str(status or "recorded").strip().lower() or "recorded",
            source=str(source or "chat").strip().lower() or "chat",
            user_id=str(user_id or "").strip(),
            principal_id=str(principal_id or "").strip(),
            correlation_id=str(correlation_id or "").strip(),
            reply_to=str(reply_to or "").strip(),
            run_id=str(run_id or "").strip(),
            payload_size=len(str(content or "")),
            content_preview=sanitize_text(str(content or ""), max_chars=200),
            metadata=sanitize_mapping(dict(metadata or {})),
        )
        with self._lock:
            self._receipts.append(receipt)
            if len(self._receipts) > self._buffer_size:
                self._receipts = self._receipts[-self._buffer_size:]
                self._save_locked()
            else:
                append_text_atomically(self._path, json.dumps(receipt.to_dict(), default=str) + "\n")
        return receipt

    def recent(self, limit: int = 50) -> List[MessageReceipt]:
        with self._lock:
            receipts = list(reversed(self._receipts))
        return receipts[: max(1, int(limit or 50))]

    def search(
        self,
        *,
        query: str = "",
        session_id: str = "",
        role: str = "",
        status: str = "",
        event_kind: str = "",
        correlation_id: str = "",
        limit: int = 20,
    ) -> List[MessageReceipt]:
        query_norm = str(query or "").strip().lower()
        session_norm = str(session_id or "").strip()
        role_norm = str(role or "").strip().lower()
        status_norm = str(status or "").strip().lower()
        event_norm = str(event_kind or "").strip().lower()
        corr_norm = str(correlation_id or "").strip()
        terms = [term for term in re.findall(r"[\w#@.:/-]+", query_norm) if len(term) >= 2]
        with self._lock:
            receipts = list(reversed(self._receipts))
        results: List[MessageReceipt] = []
        for receipt in receipts:
            if session_norm and receipt.session_id != session_norm:
                continue
            if role_norm and receipt.role != role_norm:
                continue
            if status_norm and receipt.status != status_norm:
                continue
            if event_norm and receipt.event_kind != event_norm:
                continue
            if corr_norm and receipt.correlation_id != corr_norm:
                continue
            if terms:
                haystack = " ".join([
                    receipt.message_id,
                    receipt.session_id,
                    receipt.event_kind,
                    receipt.role,
                    receipt.direction,
                    receipt.status,
                    receipt.source,
                    receipt.correlation_id,
                    receipt.reply_to,
                    receipt.run_id,
                    receipt.content_preview,
                    str(receipt.metadata.get("client_message_id") or ""),
                ]).lower()
                if not all(term in haystack for term in terms):
                    continue
            results.append(receipt)
            if len(results) >= max(1, int(limit or 20)):
                break
        return results

    def trace_correlation(self, correlation_id: str, limit: int = 100) -> List[MessageReceipt]:
        corr = str(correlation_id or "").strip()
        if not corr:
            return []
        with self._lock:
            receipts = [receipt for receipt in self._receipts if receipt.correlation_id == corr]
        receipts.sort(key=lambda receipt: float(receipt.timestamp or 0.0))
        return receipts[: max(1, int(limit or 100))]

    def compact(self, *, max_records: Optional[int] = None) -> Dict[str, int]:
        with self._lock:
            before = len(self._receipts)
            keep = max(1, int(max_records or self._buffer_size or 1))
            self._receipts = self._receipts[-keep:]
            self._save_locked()
            return {"before": before, "after": len(self._receipts), "pruned": max(0, before - len(self._receipts))}

    def _save_locked(self) -> None:
        payload = "".join(json.dumps(receipt.to_dict(), default=str) + "\n" for receipt in self._receipts)
        write_text_atomically(self._path, payload)

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            for line in self._path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    self._receipts.append(MessageReceipt.from_dict(json.loads(line)))
                except Exception:
                    continue
            if len(self._receipts) > self._buffer_size:
                self._receipts = self._receipts[-self._buffer_size:]
        except Exception:
            self._receipts = []
