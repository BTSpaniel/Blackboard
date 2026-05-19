from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from blackboard.kernel.atomic_files import write_text_atomically
from blackboard.workspace.redaction import sanitize_text


@dataclass
class TurnState:
    project_id: str
    session_id: str
    turn_count: int = 0
    user_turn_count: int = 0
    assistant_turn_count: int = 0
    phase: str = "opening"
    depth: str = "shallow"
    last_topic: str = ""
    topic_shifts: List[Dict[str, Any]] = field(default_factory=list)
    repair_signals: int = 0
    correction_signals: int = 0
    last_user_preview: str = ""
    last_assistant_preview: str = ""
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class TurnStateStore:
    def __init__(self, data_root: Path, project_id: str) -> None:
        self._project_id = str(project_id or "default")
        self._root = Path(data_root) / "projects" / self._project_id / "turn_state"
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, session_id: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(session_id or "main")) or "main"
        return self._root / f"{safe}.json"

    def load(self, session_id: str) -> TurnState:
        path = self._path(session_id)
        if not path.exists():
            return TurnState(project_id=self._project_id, session_id=str(session_id or "main"))
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            allowed = {key: value for key, value in data.items() if key in TurnState.__dataclass_fields__}
            return TurnState(**allowed)
        except Exception:
            return TurnState(project_id=self._project_id, session_id=str(session_id or "main"))

    def update(self, session_id: str, *, role: str, content: str) -> TurnState:
        state = self.load(session_id)
        value = str(content or "")
        role_value = str(role or "").strip().lower()
        state.turn_count += 1
        if role_value == "user":
            state.user_turn_count += 1
            state.last_user_preview = sanitize_text(value, max_chars=240)
            topic = self._topic(value)
            if topic and state.last_topic and topic != state.last_topic:
                state.topic_shifts.append({"from": state.last_topic, "to": topic, "turn": state.turn_count, "ts": time.time()})
                state.topic_shifts = state.topic_shifts[-20:]
            if topic:
                state.last_topic = topic
            if re.search(r"\b(fix|broken|wrong|error|bug|crash|failed|not working)\b", value, re.IGNORECASE):
                state.repair_signals += 1
            if re.search(r"\b(actually|instead|correction|i meant|not that)\b", value, re.IGNORECASE):
                state.correction_signals += 1
        elif role_value == "assistant":
            state.assistant_turn_count += 1
            state.last_assistant_preview = sanitize_text(value, max_chars=240)
        state.phase = self._phase(state)
        state.depth = self._depth(state)
        state.updated_at = time.time()
        self.save(state)
        return state

    def save(self, state: TurnState) -> None:
        write_text_atomically(self._path(state.session_id), json.dumps(state.to_dict(), indent=2, default=str))

    def context_block(self, session_id: str) -> str:
        state = self.load(session_id)
        lines = ["<conversation_state>"]
        lines.append(f"turn_count: {state.turn_count}")
        lines.append(f"phase: {state.phase}")
        lines.append(f"depth: {state.depth}")
        if state.last_topic:
            lines.append(f"last_topic: {state.last_topic}")
        if state.topic_shifts:
            last_shift = state.topic_shifts[-1]
            lines.append(f"last_topic_shift: {last_shift.get('from')} -> {last_shift.get('to')}")
        if state.repair_signals:
            lines.append(f"repair_signals: {state.repair_signals}")
        if state.correction_signals:
            lines.append(f"correction_signals: {state.correction_signals}")
        lines.append("</conversation_state>")
        return "\n".join(lines)

    @staticmethod
    def _topic(content: str) -> str:
        terms = re.findall(r"[A-Za-z0-9_./-]{3,}", str(content or "").lower())
        stop = {"the", "and", "for", "with", "that", "this", "please", "can", "you", "want", "need", "from", "into", "continue"}
        useful = [term for term in terms if term not in stop]
        return " ".join(useful[:5])

    @staticmethod
    def _phase(state: TurnState) -> str:
        if state.repair_signals or state.correction_signals:
            return "repair"
        if state.user_turn_count <= 1:
            return "opening"
        if state.user_turn_count <= 3:
            return "exploration"
        return "execution"

    @staticmethod
    def _depth(state: TurnState) -> str:
        if state.user_turn_count >= 8:
            return "deep"
        if state.user_turn_count >= 4:
            return "medium"
        return "shallow"
