from __future__ import annotations

import json
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List

from blackboard.coding.adaptive_skills import _render_skill_file
from blackboard.kernel.atomic_files import append_text_atomically, write_text_atomically
from blackboard.workspace.audit import AuditLog

_STOP_WORDS = {
    "the", "and", "for", "with", "that", "this", "from", "into", "your", "their", "them", "they", "have",
    "what", "when", "where", "which", "will", "would", "could", "should", "about", "make", "build", "create",
    "need", "want", "just", "then", "than", "each", "loop", "chat", "user", "project", "blackboard",
    "using", "used", "into", "onto", "after", "before", "through", "over", "under", "while",
}

_DEFAULT_MIN_RUN_COUNT = 2
_DEFAULT_MIN_SUCCESS_RATE = 0.8
_DEFAULT_MAX_CANDIDATES = 200


def promoted_skill_dir(data_root: Path, project_id: str) -> Path:
    return Path(data_root) / "projects" / str(project_id or "default") / "promoted_skills"


class SkillPromotionGate:
    def __init__(
        self,
        data_root: Path,
        project_id: str,
        *,
        threshold: int | None = None,
        min_run_count: int = _DEFAULT_MIN_RUN_COUNT,
        min_success_rate: float = _DEFAULT_MIN_SUCCESS_RATE,
        max_observed_ids: int = 500,
        max_candidates: int = _DEFAULT_MAX_CANDIDATES,
    ) -> None:
        self._data_root = Path(data_root)
        self._project_id = str(project_id or "default")
        if threshold is not None:
            min_run_count = int(threshold or min_run_count)
        self._min_run_count = max(2, int(min_run_count or _DEFAULT_MIN_RUN_COUNT))
        self._min_success_rate = max(0.0, min(float(min_success_rate or _DEFAULT_MIN_SUCCESS_RATE), 1.0))
        self._max_observed_ids = max(50, int(max_observed_ids or 500))
        self._max_candidates = max(20, int(max_candidates or _DEFAULT_MAX_CANDIDATES))
        self._state_dir = self._data_root / "projects" / self._project_id / "skill_promotions"
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._state_path = self._state_dir / "state.json"
        self._observations_path = self._state_dir / "workflows.jsonl"
        self._audit = AuditLog(self._data_root, self._project_id)

    def observe_chat_workflow(
        self,
        *,
        session_id: str,
        user_message: str,
        assistant_message: str,
        observation_id: str = "",
        created_count: int = 0,
        updated_count: int = 0,
        deleted_count: int = 0,
        unresolved_count: int = 0,
    ) -> Dict[str, Any]:
        summary_parts: List[str] = []
        if created_count:
            summary_parts.append(f"created {created_count} card(s)")
        if updated_count:
            summary_parts.append(f"updated {updated_count} card(s)")
        if deleted_count:
            summary_parts.append(f"deleted {deleted_count} card(s)")
        if unresolved_count:
            summary_parts.append(f"unresolved {unresolved_count} target(s)")
        summary = assistant_message.strip()
        if summary_parts:
            summary = (summary + "\n\n" + "; ".join(summary_parts)).strip()
        return self.observe_workflow(
            source="chat",
            intent_text=user_message,
            summary_text=summary,
            success=bool(user_message.strip() and assistant_message.strip()),
            session_id=session_id,
            observation_id=observation_id or f"chat:{session_id}:{self._fingerprint(user_message)}:{self._fingerprint(assistant_message)}",
            metadata={
                "created_count": int(created_count or 0),
                "updated_count": int(updated_count or 0),
                "deleted_count": int(deleted_count or 0),
                "unresolved_count": int(unresolved_count or 0),
            },
        )

    def observe_coding_workflow(
        self,
        *,
        objective: str,
        summary_text: str,
        success: bool,
        observation_id: str = "",
        session_id: str = "",
        card_id: str = "",
        files: List[str] | None = None,
        tool_sequence: List[str] | None = None,
        stopped_reason: str = "",
        error_text: str = "",
    ) -> Dict[str, Any]:
        return self.observe_workflow(
            source="coding",
            intent_text=objective,
            summary_text=summary_text or error_text,
            success=bool(success),
            session_id=session_id,
            card_id=card_id,
            files=list(files or []),
            tool_sequence=list(tool_sequence or []),
            observation_id=observation_id or f"coding:{session_id or card_id}:{self._fingerprint(objective)}:{self._fingerprint(summary_text or error_text)}",
            metadata={
                "stopped_reason": str(stopped_reason or ""),
                "error_text": str(error_text or "")[:240],
            },
        )

    def observe_tool_workflow(
        self,
        *,
        intent_text: str,
        tool_sequence: List[str],
        success: bool,
        observation_id: str = "",
        session_id: str = "",
        card_id: str = "",
        summary_text: str = "",
        files: List[str] | None = None,
        metadata: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        summary = str(summary_text or "").strip() or "Observed repeated tool workflow candidate."
        return self.observe_workflow(
            source="tool",
            intent_text=intent_text,
            summary_text=summary,
            success=bool(success),
            session_id=session_id,
            card_id=card_id,
            files=list(files or []),
            tool_sequence=list(tool_sequence or []),
            observation_id=observation_id or f"tool:{session_id or card_id}:{self._fingerprint(intent_text)}:{' > '.join(list(tool_sequence or [])[:8])}",
            metadata=dict(metadata or {}),
        )

    def observe_workflow(
        self,
        *,
        source: str,
        intent_text: str,
        summary_text: str,
        success: bool,
        session_id: str = "",
        card_id: str = "",
        files: List[str] | None = None,
        tool_sequence: List[str] | None = None,
        observation_id: str = "",
        metadata: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        state = self._load_state()
        observed_ids = list(state.get("observed_ids") or [])
        obs_id = str(observation_id or "").strip()
        if obs_id and obs_id in observed_ids:
            return {"recorded": False, "duplicate": True, "promoted": []}
        pattern = self._normalize_intent(intent_text)
        patterns = dict(state.get("patterns") or {})
        if pattern["key"] not in patterns and len(patterns) >= self._max_candidates:
            oldest_key = min(
                patterns.keys(),
                key=lambda key: float(dict(patterns.get(key) or {}).get("last_seen") or 0.0),
            )
            patterns.pop(oldest_key, None)
        entry = self._hydrate_entry(patterns.get(pattern["key"]), pattern)
        observation = {
            "ts": time.time(),
            "source": str(source or "workflow").strip().lower() or "workflow",
            "intent_text": str(intent_text or "")[:400],
            "intent_key": pattern["key"],
            "intent_label": pattern["label"],
            "terms": list(pattern["terms"]),
            "summary_text": str(summary_text or "")[:600],
            "success": bool(success),
            "session_id": str(session_id or ""),
            "card_id": str(card_id or ""),
            "files": [str(item or "").strip() for item in list(files or []) if str(item or "").strip()][:12],
            "tool_sequence": [str(item or "").strip() for item in list(tool_sequence or []) if str(item or "").strip()][:24],
            "metadata": dict(metadata or {}),
            "observation_id": obs_id,
        }
        append_text_atomically(self._observations_path, json.dumps(observation, default=str) + "\n", encoding="utf-8")
        if obs_id:
            observed_ids.append(obs_id)
            state["observed_ids"] = observed_ids[-self._max_observed_ids :]

        entry["intent_label"] = pattern["label"]
        entry["terms"] = list(pattern["terms"])
        entry["run_count"] = int(entry.get("run_count") or 0) + 1
        entry["last_seen"] = float(observation["ts"])
        sources = dict(entry.get("sources") or {})
        sources[observation["source"]] = int(sources.get(observation["source"], 0)) + 1
        entry["sources"] = sources

        if observation["success"]:
            entry["success_count"] = int(entry.get("success_count") or 0) + 1
            tool_sequences = dict(entry.get("tool_sequences") or {})
            sequence_key = " > ".join(observation["tool_sequence"])
            if sequence_key:
                tool_sequences[sequence_key] = int(tool_sequences.get(sequence_key, 0)) + 1
            entry["tool_sequences"] = tool_sequences
            file_counts = dict(entry.get("files") or {})
            for path in observation["files"]:
                file_counts[path] = int(file_counts.get(path, 0)) + 1
            entry["files"] = file_counts
            examples = list(entry.get("examples") or [])
            examples.insert(0, {
                "source": observation["source"],
                "summary_text": observation["summary_text"],
                "tool_sequence": observation["tool_sequence"],
                "files": observation["files"],
                "session_id": observation["session_id"],
                "card_id": observation["card_id"],
                "ts": observation["ts"],
                "success": True,
            })
            entry["examples"] = examples[:6]

        patterns[pattern["key"]] = entry
        state["patterns"] = patterns
        promoted = self._maybe_promote(pattern["key"], entry, state=state)
        patterns[pattern["key"]] = entry
        state["updated_at"] = time.time()
        self._save_state(state)
        return {
            "recorded": True,
            "duplicate": False,
            "promoted": [promoted] if promoted else [],
            "candidate": self._candidate_summary(entry),
        }

    def get_candidates(self) -> List[Dict[str, Any]]:
        state = self._load_state()
        patterns = [self._candidate_summary(self._hydrate_entry(entry, None)) for entry in list((state.get("patterns") or {}).values())]
        patterns.sort(key=lambda item: (-int(item.get("run_count") or 0), -float(item.get("last_seen") or 0.0), str(item.get("intent_key") or "")))
        return patterns

    def stats(self) -> Dict[str, Any]:
        state = self._load_state()
        patterns = [self._hydrate_entry(entry, None) for entry in list((state.get("patterns") or {}).values())]
        ready = 0
        for entry in patterns:
            if str(entry.get("promoted_skill") or "").strip():
                continue
            if int(entry.get("run_count") or 0) >= self._min_run_count and self._success_rate(entry) >= self._min_success_rate:
                ready += 1
        return {
            "project_id": self._project_id,
            "total_candidates": len(patterns),
            "total_promotions": int(state.get("total_promotions") or 0),
            "ready_to_promote": ready,
            "min_run_count": self._min_run_count,
            "min_success_rate": round(self._min_success_rate, 3),
        }

    def _maybe_promote(self, pattern_key: str, entry: Dict[str, Any], *, state: Dict[str, Any]) -> Dict[str, Any] | None:
        if str(entry.get("promoted_skill") or "").strip() and str(entry.get("promoted_path") or "").strip():
            return None
        run_count = int(entry.get("run_count") or 0)
        success_count = int(entry.get("success_count") or 0)
        success_rate = self._success_rate(entry)
        if run_count < self._min_run_count or success_rate < self._min_success_rate:
            return None
        terms = [str(term or "").strip() for term in list(entry.get("terms") or []) if str(term or "").strip()]
        if len(terms) < 2:
            return None
        top_sequence = ""
        sequence_counts = dict(entry.get("tool_sequences") or {})
        if sequence_counts:
            top_sequence = max(sequence_counts.items(), key=lambda item: (int(item[1]), item[0]))[0]
        file_counts = Counter({str(path): int(count or 0) for path, count in dict(entry.get("files") or {}).items()})
        if not top_sequence and not file_counts:
            return None
        slug = self._slugify(pattern_key)
        name = f"promoted-{slug}"
        body = self._render_body(entry, top_sequence=top_sequence, file_counts=file_counts)
        spec = {
            "name": name,
            "description": self._limit(f"Durable promoted workflow for {' '.join(terms[:4])} based on repeated successful Blackboard runs.", 140),
            "priority": 135,
            "tags": ["promoted", "workflow", *sorted(dict(entry.get("sources") or {}).keys())[:3]],
            "composes": ["adaptive-current-build", "adaptive-user-profile"],
            "when_to_use": self._limit(f"Use when the request matches repeated intent terms: {', '.join(terms[:6])}.", 180),
            "generated": True,
            "allowed_tools": ["skill_invoke"],
            "frontmatter": {
                "promoted": True,
                "promotion_reviewed": True,
                "promotion_reviewed_by": "skill-promotion-gate",
                "promotion_run_count": run_count,
                "promotion_success_count": success_count,
                "promotion_success_rate": round(success_rate, 3),
                "promotion_sources": sorted(dict(entry.get("sources") or {}).keys()),
            },
            "body": body,
        }
        root = promoted_skill_dir(self._data_root, self._project_id)
        skill_path = root / slug / "SKILL.md"
        skill_path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomically(skill_path, _render_skill_file(spec), encoding="utf-8")
        manifest = self._load_manifest(root)
        skills = [item for item in list(manifest.get("skills") or []) if str(item.get("name") or "") != name]
        skills.append({
            "name": name,
            "path": str(skill_path),
            "priority": int(spec["priority"]),
            "generated": True,
            "promoted": True,
            "run_count": run_count,
            "success_count": success_count,
            "success_rate": round(success_rate, 3),
        })
        skills.sort(key=lambda item: (-int(item.get("priority") or 0), str(item.get("name") or "")))
        manifest.update({
            "updated_at": time.time(),
            "project_id": self._project_id,
            "skills": skills,
        })
        write_text_atomically(root / "manifest.json", json.dumps(manifest, indent=2, default=str), encoding="utf-8")
        entry["promoted_skill"] = name
        entry["promoted_path"] = str(skill_path)
        state["total_promotions"] = int(state.get("total_promotions") or 0) + 1
        promotion = {
            "name": name,
            "path": str(skill_path),
            "run_count": run_count,
            "success_count": success_count,
            "success_rate": round(success_rate, 3),
        }
        self._audit.record(
            "SKILL_PROMOTED",
            {
                "skill_name": name,
                "pattern": pattern_key,
                "project_id": self._project_id,
                "run_count": run_count,
                "success_count": success_count,
                "success_rate": round(success_rate, 3),
                "tool_sequence": top_sequence.split(" > ") if top_sequence else [],
                "sources": sorted(dict(entry.get("sources") or {}).keys()),
            },
            actor="skill_promotion_gate",
            session_id=str((list(entry.get("examples") or [{}])[:1] or [{}])[0].get("session_id") or ""),
        )
        return promotion

    def _render_body(self, entry: Dict[str, Any], *, top_sequence: str, file_counts: Counter[str]) -> str:
        lines = [
            "# Promoted workflow",
            "",
            "## Intent pattern",
            f"- {self._limit(str(entry.get('intent_label') or ''), 220)}",
            f"- Runs observed: {int(entry.get('run_count') or 0)}",
            f"- Successful observations: {int(entry.get('success_count') or 0)}",
            f"- Success rate: {round(self._success_rate(entry) * 100, 1)}%",
            f"- Sources: {', '.join(sorted(dict(entry.get('sources') or {}).keys())[:6])}",
            "",
            "## Reuse cues",
        ]
        for term in list(entry.get("terms") or [])[:8]:
            lines.append(f"- {term}")
        lines.append("")
        if top_sequence:
            lines.append("## Preferred tool sequence")
            for idx, tool_name in enumerate([part.strip() for part in top_sequence.split(">") if part.strip()], start=1):
                lines.append(f"{idx}. `{tool_name}`")
            lines.append("")
        if file_counts:
            lines.append("## Repeated files and paths")
            for path, _count in file_counts.most_common(6):
                lines.append(f"- {path}")
            lines.append("")
        lines.append("## Guardrails")
        lines.append("- Reuse this workflow only when the current request matches the intent cues above.")
        lines.append("- Prefer current repository constraints over older successful runs if they conflict.")
        lines.append("- Compose with `adaptive-current-build` before choosing concrete file edits.")
        examples = list(entry.get("examples") or [])
        if examples:
            lines.append("")
            lines.append("## Evidence snapshots")
            for example in examples[:3]:
                summary = self._limit(str(example.get("summary_text") or ""), 220)
                source = str(example.get("source") or "workflow")
                if summary:
                    lines.append(f"- {source}: {summary}")
        return "\n".join(lines)

    def _load_state(self) -> Dict[str, Any]:
        if not self._state_path.exists():
            return {"updated_at": 0.0, "observed_ids": [], "patterns": {}, "total_promotions": 0}
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("observed_ids", [])
                data.setdefault("patterns", {})
                data.setdefault("total_promotions", 0)
                return data
        except Exception:
            pass
        return {"updated_at": 0.0, "observed_ids": [], "patterns": {}, "total_promotions": 0}

    def _save_state(self, state: Dict[str, Any]) -> None:
        write_text_atomically(self._state_path, json.dumps(state, indent=2, default=str), encoding="utf-8")

    @staticmethod
    def _load_manifest(root: Path) -> Dict[str, Any]:
        path = root / "manifest.json"
        if not path.exists():
            return {"updated_at": 0.0, "project_id": "", "skills": []}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("skills", [])
                return data
        except Exception:
            pass
        return {"updated_at": 0.0, "project_id": "", "skills": []}

    def _normalize_intent(self, text: str) -> Dict[str, Any]:
        counts: Counter[str] = Counter()
        ordered: List[str] = []
        for term in re.findall(r"[a-z0-9_./-]{3,}", str(text or "").lower()):
            if term in _STOP_WORDS or term.isdigit():
                continue
            counts[term] += 1
            if term not in ordered:
                ordered.append(term)
        terms = [term for term, _count in counts.most_common(8)]
        if not terms:
            terms = ordered[:6] or ["general", "workflow"]
        key = "-".join(terms[:6])
        label = " ".join(terms[:6])
        return {"key": key, "label": label, "terms": terms[:8]}

    def _hydrate_entry(self, entry: Dict[str, Any] | None, pattern: Dict[str, Any] | None) -> Dict[str, Any]:
        seed = dict(entry or {})
        if pattern is not None:
            seed.setdefault("intent_key", pattern["key"])
            seed.setdefault("intent_label", pattern["label"])
            seed.setdefault("terms", list(pattern["terms"]))
        seed.setdefault("success_count", 0)
        seed.setdefault("run_count", int(seed.get("success_count") or 0))
        seed.setdefault("last_seen", 0.0)
        seed.setdefault("sources", {})
        seed.setdefault("tool_sequences", {})
        seed.setdefault("files", {})
        seed.setdefault("examples", [])
        seed.setdefault("promoted_skill", "")
        seed.setdefault("promoted_path", "")
        return seed

    def _candidate_summary(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "intent_key": str(entry.get("intent_key") or ""),
            "intent_label": str(entry.get("intent_label") or ""),
            "terms": list(entry.get("terms") or []),
            "run_count": int(entry.get("run_count") or 0),
            "success_count": int(entry.get("success_count") or 0),
            "success_rate": round(self._success_rate(entry), 3),
            "last_seen": float(entry.get("last_seen") or 0.0),
            "sources": dict(entry.get("sources") or {}),
            "promoted_skill": str(entry.get("promoted_skill") or ""),
            "promoted_path": str(entry.get("promoted_path") or ""),
            "ready_to_promote": (
                not str(entry.get("promoted_skill") or "").strip()
                and int(entry.get("run_count") or 0) >= self._min_run_count
                and self._success_rate(entry) >= self._min_success_rate
            ),
        }

    @staticmethod
    def _success_rate(entry: Dict[str, Any]) -> float:
        run_count = max(1, int(entry.get("run_count") or 0))
        return float(entry.get("success_count") or 0) / run_count

    @staticmethod
    def _slugify(value: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
        return slug or "workflow"

    @staticmethod
    def _fingerprint(value: str) -> str:
        text = re.sub(r"\s+", " ", str(value or "").strip().lower())
        return text[:80]

    @staticmethod
    def _limit(text: str, max_chars: int) -> str:
        value = str(text or "").strip()
        if len(value) <= max_chars:
            return value
        return value[: max(1, int(max_chars or 1) - 1)].rstrip() + "…"
