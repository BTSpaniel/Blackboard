from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class ChatIntentDetection:
    intent: str
    confidence: float
    should_create_cards: bool
    default_card_status: str
    reasoning: str
    scores: Dict[str, float] = field(default_factory=dict)
    slots: Dict[str, Any] = field(default_factory=dict)


class ChatIntentDetector:
    def detect(self, message: str) -> ChatIntentDetection:
        text = str(message or "").strip()
        lower = text.lower()
        slots = self._extract_slots(text)
        scores = {
            "chat": self._chat_score(lower, len(text)),
            "card": self._card_score(lower),
            "implementation": self._implementation_score(lower, slots),
            "coding": self._coding_score(lower, slots),
            "tool": self._tool_score(lower, slots),
        }
        if not text:
            return ChatIntentDetection(
                intent="empty",
                confidence=1.0,
                should_create_cards=False,
                default_card_status="inbox",
                reasoning="Empty message",
                scores=scores,
                slots=slots,
            )

        dominant = max(scores, key=scores.get)
        confidence = max(0.0, min(1.0, scores[dominant]))
        explicit_card = scores["card"] >= 0.55
        implementation = scores["implementation"] >= 0.55
        should_create = explicit_card or implementation
        if explicit_card:
            dominant = "card"
            confidence = max(confidence, scores["card"])
        if scores["chat"] >= 0.55 and not should_create:
            dominant = "chat"
            confidence = max(confidence, scores["chat"])
        status = "inbox"
        reasoning = self._reasoning(dominant, scores, slots, should_create)
        return ChatIntentDetection(
            intent=dominant,
            confidence=confidence,
            should_create_cards=should_create,
            default_card_status=status,
            reasoning=reasoning,
            scores=scores,
            slots=slots,
        )

    @staticmethod
    def _extract_slots(message: str) -> Dict[str, Any]:
        slots: Dict[str, Any] = {}
        files = re.findall(r"[\w./\\-]+\.(?:txt|py|js|json|yaml|yml|md|html|css|ts|tsx|jsx|java|go|rs|cpp|c|h)", message, re.IGNORECASE)
        if files:
            slots["file_paths"] = files
        urls = re.findall(r"https?://\S+", message, re.IGNORECASE)
        if urls:
            slots["urls"] = urls
        artifacts = re.findall(r"\b(website|site|web\s*page|page|app|application|demo|prototype|landing\s*page|html|css|javascript|js|file|files)\b", message, re.IGNORECASE)
        if artifacts:
            slots["artifacts"] = sorted({item.lower() for item in artifacts})
        if re.search(r"```[\s\S]*?```", message):
            slots["has_code"] = True
        tech_terms = re.findall(r"\b(python|javascript|typescript|rust|go|java|api|database|query|algorithm|html|css|react|vue|svelte|node)\b", message, re.IGNORECASE)
        if tech_terms:
            slots["tech_terms"] = sorted({item.lower() for item in tech_terms})
        return slots

    def _chat_score(self, message: str, length: int) -> float:
        score = 0.0
        if length < 50:
            score += 0.3
        elif length < 100:
            score += 0.15
        chat_patterns = [
            r"^(hello|hi|hey|yo|good morning|good evening|good night)[\s!,.?]*$",
            r"(how are you|what'?s up|sup)[\s!?]*$",
            r"(thanks|thank you|thx)[\s!,.]*$",
            r"^(yeah|yep|nope|maybe|sure|ok|okay|cool|nice|great)[\s!,.]*$",
            r"\b(lol|haha|omg|wow|awesome)\b",
        ]
        if any(re.search(pattern, message, re.IGNORECASE) for pattern in chat_patterns):
            score += 0.35
        if re.search(r"\?$", message) and not self._has_technical_content(message):
            score += 0.15
        if any(indicator in message for indicator in ("i think", "i feel", "i believe", "i guess", "i wonder")):
            score += 0.1
        if self._has_technical_content(message):
            score -= 0.25
        if re.search(r"\b(can you|please|help me|need to|want to|make|build|create|implement)\b", message):
            score -= 0.15
        return max(0.0, min(1.0, score))

    @staticmethod
    def _card_score(message: str) -> float:
        score = 0.0
        if re.search(r"\b(add|create|make|generate|capture|turn|split|break\s+down|plan|outline|queue|populate)\b.{0,100}\b(card|cards|task|tasks|todo|todos|work item|work items|plan|steps|backlog)\b", message, re.IGNORECASE):
            score += 0.7
        if re.search(r"\bbreak\b.{0,100}\binto\b.{0,100}\b(card|cards|task|tasks|todo|todos|work item|work items|steps)\b", message, re.IGNORECASE):
            score += 0.7
        if re.search(r"\b(card|cards|task|tasks|todo|todos|backlog)\b.{0,100}\b(add|create|make|generate|capture|plan|queue|populate)\b", message, re.IGNORECASE):
            score += 0.7
        if re.search(r"\b(first|second|third|next|then|finally|phase \d+|step \d+)\b", message, re.IGNORECASE):
            score += 0.15
        return max(0.0, min(1.0, score))

    @staticmethod
    def _implementation_score(message: str, slots: Dict[str, Any]) -> float:
        score = 0.0
        explanatory = bool(re.search(r"\b(explain|what is|what are|how does|how do|why|before we|tell me about)\b", message, re.IGNORECASE))
        action = re.search(r"\b(build|create|make|implement|scaffold|add|generate|write)\b", message, re.IGNORECASE)
        artifact = bool(slots.get("artifacts"))
        file_or_code = bool(slots.get("file_paths") or slots.get("has_code"))
        if explanatory and re.search(r"\b(before we|before i|what is|what are|explain)\b", message, re.IGNORECASE):
            return 0.0
        if action and artifact:
            score += 0.75
        elif action and file_or_code:
            score += 0.55
        elif action and re.search(r"\b(function|class|method|script|component|endpoint|route)\b", message, re.IGNORECASE):
            score += 0.55
        if re.search(r"\b(ready-to-run|working|small|simple|demo|prototype)\b", message, re.IGNORECASE) and artifact:
            score += 0.15
        if explanatory:
            score -= 0.25
        return max(0.0, min(1.0, score))

    def _coding_score(self, message: str, slots: Dict[str, Any]) -> float:
        score = 0.0
        if len(message) > 200:
            score += 0.15
        if re.search(r"\b(def |class |function |import |from |return |if |else |for |while |var |let |const |async |await)\b", message):
            score += 0.2
        if re.search(r"(write|create|implement|build|fix|debug)\s+(a\s+)?(function|class|method|code|script|component)", message, re.IGNORECASE):
            score += 0.3
        if re.search(r"(error|bug|issue|problem)\s+(with|in)", message, re.IGNORECASE):
            score += 0.3
        if slots.get("file_paths"):
            score += 0.25
        if slots.get("tech_terms"):
            score += 0.15
        if self._has_technical_content(message):
            score += 0.1
        return max(0.0, min(1.0, score))

    def _tool_score(self, message: str, slots: Dict[str, Any]) -> float:
        score = 0.0
        if re.search(r"\b(search|find|look up|check|read|write|create|delete|modify|edit|update|run|execute|browse|open|save|load)\b", message, re.IGNORECASE):
            score += 0.25
        if slots.get("file_paths") and re.search(r"\b(read|write|open|save|check|delete|modify|edit)\b", message, re.IGNORECASE):
            score += 0.35
        if slots.get("urls") and re.search(r"\b(open|browse|visit|check|read)\b", message, re.IGNORECASE):
            score += 0.3
        if re.search(r"\b(and then|after that|next|finally|first|second|step \d+|phase \d+)\b", message, re.IGNORECASE):
            score += 0.15
        if re.search(r"\btool\b", message, re.IGNORECASE) and not re.search(r"\b(use|call|run|execute|with|using)\b", message, re.IGNORECASE):
            score -= 0.2
        return max(0.0, min(1.0, score))

    @staticmethod
    def _has_technical_content(message: str) -> bool:
        technical_indicators = [
            r"\b(code|function|class|api|endpoint|database|query|component|route)\b",
            r"\b(python|javascript|typescript|rust|go|java|html|css|react|node)\b",
            r"\b(algorithm|data structure|debug|implement|refactor)\b",
            r"[{}\[\];:=]",
        ]
        return any(re.search(pattern, message, re.IGNORECASE) for pattern in technical_indicators)

    @staticmethod
    def _reasoning(dominant: str, scores: Dict[str, float], slots: Dict[str, Any], should_create: bool) -> str:
        reasons = [f"dominant={dominant}"]
        if should_create:
            reasons.append("card creation allowed by structured intent score")
        if slots:
            reasons.append("slots=" + ",".join(sorted(slots.keys())))
        reasons.append("scores=" + ",".join(f"{key}:{value:.2f}" for key, value in sorted(scores.items())))
        return "; ".join(reasons)


_detector = ChatIntentDetector()


def detect_chat_intent(message: str) -> ChatIntentDetection:
    return _detector.detect(message)
