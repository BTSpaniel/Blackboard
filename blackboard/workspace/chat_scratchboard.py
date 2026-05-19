from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List

from blackboard.kernel.atomic_files import write_text_atomically
from blackboard.workspace.redaction import sanitize_text


class ChatScratchboardStore:
    def __init__(self, data_root: Path, project_id: str) -> None:
        self._project_id = str(project_id or "default")
        self._root = Path(data_root) / "projects" / self._project_id / "scratchboards"
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, session_id: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(session_id or "main")) or "main"
        return self._root / f"{safe}.json"

    def load(self, session_id: str) -> Dict[str, Any]:
        path = self._path(session_id)
        if not path.exists():
            return self._empty(session_id)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                base = self._empty(session_id)
                base.update(data)
                base["facts"] = list(base.get("facts") or [])[-24:]
                return base
        except Exception:
            pass
        return self._empty(session_id)

    def update(self, session_id: str, *, role: str, content: str) -> Dict[str, Any]:
        state = self.load(session_id)
        text = sanitize_text(str(content or ""), max_chars=700)
        role_value = str(role or "").strip().lower()
        if role_value == "user":
            state["last_user_message"] = text[:360]
            for fact in self._extract_facts(text):
                self._add_fact(state, fact)
        elif role_value == "assistant":
            state["last_assistant_reply"] = text[:360]
        elif role_value == "system":
            state["last_system_note"] = text[:240]
        state["updated_at"] = time.time()
        self.save(session_id, state)
        return state

    def save(self, session_id: str, state: Dict[str, Any]) -> None:
        write_text_atomically(self._path(session_id), json.dumps(state, indent=2, default=str))

    def context_block(self, session_id: str) -> str:
        state = self.load(session_id)
        lines: List[str] = ["<session_scratchboard>"]
        facts = list(state.get("facts") or [])[-12:]
        if facts:
            lines.append("durable_facts:")
            for fact in facts:
                lines.append(f"- {fact}")
        if state.get("last_user_message"):
            lines.append(f"last_user_message: {state.get('last_user_message')}")
        if state.get("last_assistant_reply"):
            lines.append(f"last_assistant_reply: {state.get('last_assistant_reply')}")
        if len(lines) == 1:
            lines.append("empty")
        lines.append("</session_scratchboard>")
        return "\n".join(lines)

    def _empty(self, session_id: str) -> Dict[str, Any]:
        return {
            "project_id": self._project_id,
            "session_id": str(session_id or "main"),
            "facts": [],
            "last_user_message": "",
            "last_assistant_reply": "",
            "last_system_note": "",
            "updated_at": time.time(),
        }

    @staticmethod
    def _add_fact(state: Dict[str, Any], fact: str) -> None:
        value = sanitize_text(str(fact or "").strip(), max_chars=320)
        if not value:
            return
        facts = [str(item or "") for item in list(state.get("facts") or []) if str(item or "").strip()]
        lowered = {item.lower() for item in facts}
        if value.lower() not in lowered:
            facts.append(value)
        state["facts"] = facts[-24:]

    @staticmethod
    def _extract_facts(text: str) -> List[str]:
        value = " ".join(str(text or "").split())
        if not value:
            return []
        facts: List[str] = []
        patterns = [
            r"\bwe\s+(?:build|write|store|keep|run|work)\s+([^.!?]{8,180})",
            r"\b(?:remember|note)\s+(?:that\s+)?([^.!?]{8,180})",
            r"\b(?:use|prefer)\s+([^.!?]{8,180})",
        ]
        for pattern in patterns:
            for match in re.finditer(pattern, value, re.IGNORECASE):
                fact = match.group(0).strip()
                if len(fact) >= 8:
                    facts.append(fact)
        if not facts and any(token in value.lower() for token in ("we want", "we need", "our context", "scratchboard", "workspace")):
            facts.append(value[:220])
        return facts[:6]
