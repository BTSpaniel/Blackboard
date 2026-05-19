from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from blackboard.providers.base import Message


@dataclass(frozen=True)
class TrimRecommendation:
    message_indices: List[int]
    reason: str
    estimated_chars_saved: int
    priority: str


@dataclass(frozen=True)
class ContextRotReport:
    timestamp: float
    context_health: str
    total_chars: int
    efficiency_score: float
    staleness_score: float
    redundancy_score: float
    trim_recommendations: List[TrimRecommendation] = field(default_factory=list)
    performance_trend: str = "stable"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "health": self.context_health,
            "total_chars": self.total_chars,
            "efficiency": round(self.efficiency_score, 2),
            "staleness": round(self.staleness_score, 2),
            "redundancy": round(self.redundancy_score, 2),
            "recommendations": [
                {
                    "reason": item.reason,
                    "chars_saved": item.estimated_chars_saved,
                    "priority": item.priority,
                }
                for item in self.trim_recommendations
            ],
            "trend": self.performance_trend,
        }


@dataclass(frozen=True)
class ContextRotConfig:
    enabled: bool = True
    max_context_chars: int = 200_000
    stale_threshold_s: float = 600.0
    check_interval_rounds: int = 1


@dataclass
class ContextSnapshot:
    timestamp: float
    total_chars: int
    message_count: int
    tool_result_count: int
    redundancy_score: float
    staleness_score: float
    efficiency_score: float


class ContextRotDetector:
    def __init__(self, config: Optional[ContextRotConfig] = None) -> None:
        self.config = config or ContextRotConfig()
        self.snapshots: deque[ContextSnapshot] = deque(maxlen=100)
        self.quality_history: deque[float] = deque(maxlen=50)
        self.round_counter = 0
        self.last_report: Optional[ContextRotReport] = None

    def check(self, messages: List[Dict[str, Any]], total_chars: int = 0) -> Optional[ContextRotReport]:
        self.round_counter += 1
        if not self.config.enabled or not messages:
            return None
        if self.round_counter % max(1, int(self.config.check_interval_rounds or 1)) != 0:
            return None
        total_chars = int(total_chars or sum(len(str(item.get("content") or "")) for item in messages))
        staleness = self._measure_staleness(messages)
        redundancy = self._measure_redundancy(messages)
        efficiency = self._measure_efficiency(total_chars)
        trims = self._trim_recommendations(messages, staleness, redundancy)
        if staleness > 0.7 or redundancy > 0.7 or efficiency < 0.3:
            health = "critical"
        elif staleness > 0.4 or redundancy > 0.4 or efficiency < 0.5:
            health = "degrading"
        else:
            health = "healthy"
        self.quality_history.append(efficiency)
        report = ContextRotReport(
            timestamp=time.time(),
            context_health=health,
            total_chars=total_chars,
            efficiency_score=efficiency,
            staleness_score=staleness,
            redundancy_score=redundancy,
            trim_recommendations=trims,
            performance_trend=self._trend(),
        )
        self.snapshots.append(ContextSnapshot(
            timestamp=report.timestamp,
            total_chars=total_chars,
            message_count=len(messages),
            tool_result_count=sum(1 for item in messages if str(item.get("role") or "") == "tool"),
            redundancy_score=redundancy,
            staleness_score=staleness,
            efficiency_score=efficiency,
        ))
        self.last_report = report
        return report

    def stats(self) -> Dict[str, Any]:
        recent = list(self.snapshots)[-10:]
        return {
            "total_checks": len(self.snapshots),
            "avg_efficiency": sum(item.efficiency_score for item in recent) / max(len(recent), 1),
            "avg_staleness": sum(item.staleness_score for item in recent) / max(len(recent), 1),
            "avg_redundancy": sum(item.redundancy_score for item in recent) / max(len(recent), 1),
            "trend": self._trend(),
            "last_report": self.last_report.to_dict() if self.last_report else None,
        }

    def _measure_staleness(self, messages: List[Dict[str, Any]]) -> float:
        now = time.time()
        stale = 0.0
        total = 0
        for item in messages:
            role = str(item.get("role") or "")
            if role == "system":
                continue
            content = str(item.get("content") or "")
            if not content:
                continue
            total += 1
            if role == "tool" and len(content) > 600:
                stale += 0.4
            if "```" in content and len(content) > 1200:
                stale += 0.25
            try:
                ts = float(item.get("ts") or item.get("timestamp") or 0)
            except Exception:
                ts = 0.0
            if ts and now - ts > self.config.stale_threshold_s:
                stale += 0.35
        if total <= 0:
            return 0.0
        length_factor = min(1.0, len(messages) / 40)
        return min(1.0, stale / total * 0.75 + length_factor * 0.25)

    @staticmethod
    def _measure_redundancy(messages: List[Dict[str, Any]]) -> float:
        fingerprints: List[str] = []
        for item in messages:
            if str(item.get("role") or "") == "system":
                continue
            content = " ".join(str(item.get("content") or "").lower().split())[:240]
            if content:
                fingerprints.append(content)
        if len(fingerprints) < 4:
            return 0.0
        overlaps = 0
        comparisons = 0
        for index, left in enumerate(fingerprints):
            left_words = set(left.split())
            for right in fingerprints[index + 1:index + 5]:
                right_words = set(right.split())
                if not left_words or not right_words:
                    continue
                comparisons += 1
                overlap = len(left_words & right_words) / max(len(left_words | right_words), 1)
                if overlap > 0.55:
                    overlaps += 1
        return overlaps / max(comparisons, 1)

    def _measure_efficiency(self, total_chars: int) -> float:
        ratio = max(0.0, total_chars / max(1, self.config.max_context_chars))
        if ratio < 0.35:
            return 0.9
        if ratio < 0.65:
            return 0.7
        if ratio < 0.85:
            return 0.5
        if ratio < 1.0:
            return 0.3
        return 0.1

    @staticmethod
    def _trim_recommendations(messages: List[Dict[str, Any]], staleness: float, redundancy: float) -> List[TrimRecommendation]:
        recs: List[TrimRecommendation] = []
        tool_indices = [
            index for index, item in enumerate(messages)
            if str(item.get("role") or "") == "tool" and index < len(messages) - 6
        ]
        if tool_indices and staleness > 0.35:
            recs.append(TrimRecommendation(tool_indices[:8], "Old tool observations can be compacted", len(tool_indices[:8]) * 400, "high"))
        repeated_indices: List[int] = []
        seen: Dict[str, int] = {}
        for index, item in enumerate(messages):
            content = " ".join(str(item.get("content") or "").lower().split())[:180]
            if not content:
                continue
            if content in seen and index < len(messages) - 4:
                repeated_indices.append(index)
            seen[content] = index
        if repeated_indices and redundancy > 0.35:
            recs.append(TrimRecommendation(repeated_indices[:6], "Redundant older turns can be compacted", len(repeated_indices[:6]) * 300, "medium"))
        return recs

    def _trend(self) -> str:
        values = list(self.quality_history)
        if len(values) < 5:
            return "stable"
        midpoint = len(values) // 2
        early = sum(values[:midpoint]) / max(midpoint, 1)
        late = sum(values[midpoint:]) / max(len(values) - midpoint, 1)
        if late - early > 0.1:
            return "improving"
        if late - early < -0.1:
            return "degrading"
        return "stable"


class ContextCompressor:
    def __init__(self, *, max_context_chars: int = 200_000, reserve_chars: int = 20_000, recent_turns_full: int = 6, detector: Optional[ContextRotDetector] = None) -> None:
        self.max_context_chars = int(max_context_chars)
        self.reserve_chars = int(reserve_chars)
        self.recent_turns_full = int(recent_turns_full)
        self.detector = detector or ContextRotDetector(ContextRotConfig(max_context_chars=max_context_chars))
        self.last_health = "healthy"
        self.total_original_chars = 0
        self.total_compressed_chars = 0

    @property
    def budget_chars(self) -> int:
        return max(1000, self.max_context_chars - self.reserve_chars)

    def compress_sections(self, sections: Dict[str, str]) -> Dict[str, str]:
        if not sections:
            return {}
        total = sum(len(str(value or "")) for value in sections.values())
        if total > self.budget_chars:
            compressed_sections: Dict[str, str] = {}
            stable_names = {"role_meta", "objective", "agents_md", "project_intel"}
            stable_total = sum(len(str(value or "")) for key, value in sections.items() if key in stable_names)
            volatile = [key for key in sections if key not in stable_names and sections.get(key)]
            volatile_budget = max(1000, self.budget_chars - stable_total)
            per_volatile_cap = max(300, volatile_budget // max(len(volatile), 1))
            for key, value in sections.items():
                text = str(value or "")
                if key in stable_names or len(text) <= per_volatile_cap:
                    compressed_sections[key] = text
                else:
                    compressed_sections[key] = self._compact(text, per_volatile_cap, prefix="[COMPRESSED SECTION]")
            sections = compressed_sections
        messages = [
            {"role": "system" if key in {"role_meta", "objective", "agents_md", "project_intel"} else "assistant", "content": value, "name": key, "ts": time.time()}
            for key, value in sections.items()
            if value
        ]
        compressed = self._compress_dicts(messages, budget_chars=self.budget_chars, allow_drop=True)
        by_name = {str(item.get("name") or ""): str(item.get("content") or "") for item in compressed}
        return {key: by_name.get(key, value) for key, value in sections.items()}

    def compress_messages(self, messages: List[Message]) -> List[Message]:
        dicts = [
            {
                "role": msg.role,
                "content": msg.content,
                "name": msg.name or "",
                "tool_call_id": msg.tool_call_id or "",
                "tool_calls": msg.tool_calls,
                "ts": time.time(),
            }
            for msg in messages
        ]
        compressed = self._compress_dicts(dicts, budget_chars=self.budget_chars, allow_drop=False)
        return [
            Message(
                role=str(item.get("role") or "user"),
                content=str(item.get("content") or ""),
                name=str(item.get("name") or "") or None,
                tool_call_id=str(item.get("tool_call_id") or "") or None,
                tool_calls=item.get("tool_calls"),
            )
            for item in compressed
        ]

    def _compress_dicts(self, messages: List[Dict[str, Any]], *, budget_chars: int, allow_drop: bool = True) -> List[Dict[str, Any]]:
        if not messages:
            return []
        original = sum(len(str(item.get("content") or "")) for item in messages)
        self.total_original_chars += original
        report = self.detector.check(messages, original)
        if report is not None:
            self.last_health = report.context_health
        if original <= budget_chars and self.last_health == "healthy":
            self.total_compressed_chars += original
            return list(messages)
        recent_window = max(4, self.recent_turns_full)
        if self.last_health == "degrading":
            recent_window = max(3, int(recent_window * 0.75))
        elif self.last_health == "critical":
            recent_window = max(2, int(recent_window * 0.5))
        stale_indices: set[int] = set()
        if report:
            for rec in report.trim_recommendations:
                stale_indices.update(rec.message_indices)
        result: List[Dict[str, Any]] = []
        total_count = len(messages)
        for index, item in enumerate(messages):
            age = total_count - index - 1
            content = str(item.get("content") or "")
            if index in stale_indices and age >= recent_window:
                content = self._compact(content, 260, prefix="[COMPACTED STALE CONTEXT]")
            elif age >= recent_window * 4:
                content = self._compact(content, 420, prefix="[COMPRESSED OLDER CONTEXT]")
            elif age >= recent_window * 2:
                content = self._compact(content, 900, prefix="[COMPRESSED CONTEXT]")
            result.append({**item, "content": content})
        new_total = sum(len(str(item.get("content") or "")) for item in result)
        if not allow_drop and new_total > budget_chars:
            for index, item in enumerate(result):
                age = len(result) - index - 1
                if age < recent_window or str(item.get("role") or "") == "system":
                    continue
                content = str(item.get("content") or "")
                compacted = self._compact(content, 220, prefix="[COMPACTED REACT CONTEXT]")
                if compacted != content:
                    result[index] = {**item, "content": compacted}
            new_total = sum(len(str(item.get("content") or "")) for item in result)
        while allow_drop and new_total > budget_chars and len(result) > recent_window + 1:
            drop_index = next((idx for idx, item in enumerate(result[:-recent_window]) if str(item.get("role") or "") != "system"), 0)
            dropped = result.pop(drop_index)
            new_total -= len(str(dropped.get("content") or ""))
        self.total_compressed_chars += new_total
        return result

    @staticmethod
    def _compact(content: str, cap: int, *, prefix: str) -> str:
        text = str(content or "")
        if len(text) <= cap:
            return text
        head = max(40, cap // 2)
        tail = max(40, cap - head - len(prefix) - 40)
        return f"{prefix}\n{text[:head]}\n...\n{text[-tail:]}"

    def stats(self) -> Dict[str, Any]:
        return {
            "max_context_chars": self.max_context_chars,
            "budget_chars": self.budget_chars,
            "last_health": self.last_health,
            "total_original_chars": self.total_original_chars,
            "total_compressed_chars": self.total_compressed_chars,
            "effective_ratio": round(self.total_compressed_chars / max(self.total_original_chars, 1), 3),
            "rot": self.detector.stats(),
        }
