"""Capability governor for feature and source-level access gates."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Set


class CapabilityGovernor:
    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        cfg = dict(config or {})
        self._disabled: Set[str] = {str(item) for item in cfg.get("disabled", [])}
        self._source_restrictions: Dict[str, List[str]] = {
            str(key): [str(item) for item in value]
            for key, value in dict(cfg.get("source_restrictions", {}) or {}).items()
        }
        self._overrides: Dict[str, bool] = {}
        self._path = Path(cfg["_path"]).resolve() if cfg.get("_path") else None
        self._load()

    def _load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            return
        overrides = payload.get("overrides") or {}
        if isinstance(overrides, dict):
            self._overrides = {str(key): bool(value) for key, value in overrides.items()}

    def _save(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps({"overrides": self._overrides}, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        except Exception:
            return

    def check(self, capability: str, *, source: str = "", user_id: str = "") -> Dict[str, Any]:
        cap = str(capability or "")
        if cap in self._overrides:
            allowed = bool(self._overrides[cap])
            return {"allowed": allowed, "reason": "override" if not allowed else ""}
        if cap in self._disabled:
            return {"allowed": False, "reason": f"'{cap}' is globally disabled"}
        allowed_sources = self._source_restrictions.get(cap) or []
        if allowed_sources and source and source not in allowed_sources:
            return {"allowed": False, "reason": f"source '{source}' not allowed for '{cap}'"}
        return {"allowed": True}

    def enable(self, capability: str) -> Dict[str, Any]:
        cap = str(capability or "")
        self._overrides[cap] = True
        self._disabled.discard(cap)
        self._save()
        return {"enabled": True, "capability": cap}

    def disable(self, capability: str) -> Dict[str, Any]:
        cap = str(capability or "")
        self._overrides[cap] = False
        self._save()
        return {"disabled": True, "capability": cap}

    def status(self) -> Dict[str, Any]:
        return {
            "disabled": sorted(self._disabled),
            "overrides": dict(self._overrides),
            "source_restrictions": dict(self._source_restrictions),
        }


_capability_governor: Optional[CapabilityGovernor] = None


def init_capability_governor(config: Optional[Dict[str, Any]] = None, data_root: Path | str | None = None) -> CapabilityGovernor:
    global _capability_governor
    cfg = dict(config or {})
    if data_root:
        cfg["_path"] = str(Path(data_root).resolve() / "governors" / "capability.json")
    _capability_governor = CapabilityGovernor(cfg)
    return _capability_governor


def get_capability_governor() -> CapabilityGovernor:
    global _capability_governor
    if _capability_governor is None:
        _capability_governor = CapabilityGovernor()
    return _capability_governor
