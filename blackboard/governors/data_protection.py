"""Data protection governor for durable Blackboard persistence."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from blackboard.workspace.redaction import sanitize_text


class DataClassification(str, Enum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


_ORDER = {
    DataClassification.PUBLIC: 0,
    DataClassification.INTERNAL: 1,
    DataClassification.CONFIDENTIAL: 2,
    DataClassification.RESTRICTED: 3,
}


@dataclass(frozen=True)
class DataProtectionResult:
    original: str
    protected: str
    classification: DataClassification
    operation: str
    categories: List[str] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)
    modified: bool = False
    blocked: bool = False

    def metadata(self) -> Dict[str, Any]:
        return {
            "data_classification": self.classification.value,
            "data_protection_operation": self.operation,
            "data_protection_categories": list(self.categories),
            "data_protection_reasons": list(self.reasons),
            "data_protection_modified": self.modified,
            "data_protection_blocked": self.blocked,
        }


class DataProtectionGovernor:
    def classify(self, value: object) -> Tuple[DataClassification, List[str], List[str]]:
        text = str(value or "")
        categories: List[str] = []
        reasons: List[str] = []
        lowered = text.lower()
        marker_map = {
            "api_key": ["api_key", "api key", "apikey"],
            "token": ["token", "bearer ", "ghp_", "sk-", "sk-ant-", "fw_"],
            "password": ["password", "passwd"],
            "email": ["@"],
            "private_profile": ["private profile", "personal profile"],
        }
        for category, markers in marker_map.items():
            if any(marker in lowered for marker in markers):
                categories.append(category)
        sanitized = sanitize_text(text, max_chars=max(len(text), 1))
        if sanitized != text and "secret_pattern" not in categories:
            categories.append("secret_pattern")
        if categories:
            reasons.append("sensitive_pattern_detected")
        restricted = {"api_key", "token", "password", "secret_pattern"}
        confidential = {"private_profile"}
        if any(item in restricted for item in categories):
            return DataClassification.RESTRICTED, categories, reasons
        if any(item in confidential for item in categories):
            return DataClassification.CONFIDENTIAL, categories, reasons
        if categories:
            return DataClassification.INTERNAL, categories, reasons
        return DataClassification.PUBLIC, categories, reasons

    def protect_text(self, value: object, *, operation: str, max_internal_chars: int = 5000) -> DataProtectionResult:
        original = str(value or "")
        classification, categories, reasons = self.classify(original)
        protected = original
        blocked = False
        if classification == DataClassification.RESTRICTED:
            protected = sanitize_text(original, max_chars=max(len(original), 1)) or "[REDACTED]"
        elif classification == DataClassification.CONFIDENTIAL:
            protected = "[FILTERED PRIVATE PROFILE]"
        elif classification == DataClassification.INTERNAL and operation.startswith("persist"):
            protected = original[:max_internal_chars]
        return DataProtectionResult(
            original=original,
            protected=protected,
            classification=classification,
            operation=operation,
            categories=categories,
            reasons=reasons,
            modified=protected != original,
            blocked=blocked,
        )

    def protect_value(self, value: Any, *, operation: str) -> Tuple[Any, Dict[str, Any]]:
        if isinstance(value, str):
            result = self.protect_text(value, operation=operation)
            return result.protected, result.metadata()
        if isinstance(value, dict):
            protected: Dict[str, Any] = {}
            classes: List[DataClassification] = []
            categories: List[str] = []
            reasons: List[str] = []
            modified = False
            for key, item in value.items():
                p_item, meta = self.protect_value(item, operation=operation)
                protected[key] = p_item
                classes.append(DataClassification(str(meta.get("data_classification") or DataClassification.PUBLIC.value)))
                categories.extend([cat for cat in list(meta.get("data_protection_categories") or []) if cat not in categories])
                reasons.extend([reason for reason in list(meta.get("data_protection_reasons") or []) if reason not in reasons])
                modified = modified or bool(meta.get("data_protection_modified"))
            return protected, {
                "data_classification": merge_classifications(classes).value,
                "data_protection_operation": operation,
                "data_protection_categories": categories,
                "data_protection_reasons": reasons,
                "data_protection_modified": modified,
                "data_protection_blocked": False,
            }
        if isinstance(value, list):
            protected_items = []
            classes: List[DataClassification] = []
            modified = False
            for item in value:
                p_item, meta = self.protect_value(item, operation=operation)
                protected_items.append(p_item)
                classes.append(DataClassification(str(meta.get("data_classification") or DataClassification.PUBLIC.value)))
                modified = modified or bool(meta.get("data_protection_modified"))
            return protected_items, {
                "data_classification": merge_classifications(classes).value,
                "data_protection_operation": operation,
                "data_protection_categories": [],
                "data_protection_reasons": [],
                "data_protection_modified": modified,
                "data_protection_blocked": False,
            }
        return value, {
            "data_classification": DataClassification.PUBLIC.value,
            "data_protection_operation": operation,
            "data_protection_categories": [],
            "data_protection_reasons": [],
            "data_protection_modified": False,
            "data_protection_blocked": False,
        }


def merge_classifications(values: List[DataClassification]) -> DataClassification:
    if not values:
        return DataClassification.PUBLIC
    return max(values, key=lambda item: _ORDER.get(item, 0))


_data_protection_governor: Optional[DataProtectionGovernor] = None


def init_data_protection_governor() -> DataProtectionGovernor:
    global _data_protection_governor
    _data_protection_governor = DataProtectionGovernor()
    return _data_protection_governor


def get_data_protection_governor() -> DataProtectionGovernor:
    global _data_protection_governor
    if _data_protection_governor is None:
        _data_protection_governor = DataProtectionGovernor()
    return _data_protection_governor
