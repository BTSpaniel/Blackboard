from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from blackboard.kernel.atomic_files import write_text_atomically as write_text_atomic
from blackboard.kernel.logger import get_logger

logger = get_logger("workspace.coding_settings")

_ALLOWED_OVERRIDE_KEYS = {
    "max_concurrent",
}


def _path(data_root: Path) -> Path:
    return Path(data_root) / "server" / "coding_overrides.json"


def normalize_coding(coding: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    current = dict(coding or {})
    raw_max_concurrent = current.get("max_concurrent", 4)
    try:
        max_concurrent = int(raw_max_concurrent)
    except Exception:
        max_concurrent = 4
    return {
        "max_concurrent": max(1, min(max_concurrent, 32)),
    }


def merge_coding_config(coding: Optional[Mapping[str, Any]], overrides: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    current = normalize_coding(coding)
    if overrides:
        current = normalize_coding({**current, **dict(overrides or {})})
    return current


def load_coding_overrides(data_root: Path) -> Dict[str, Any]:
    path = _path(data_root)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("[coding_settings] failed to load %s: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        return {}
    return normalize_coding({key: data.get(key) for key in _ALLOWED_OVERRIDE_KEYS if key in data})


def save_coding_overrides(data_root: Path, coding: Mapping[str, Any]) -> Dict[str, Any]:
    path = _path(data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    current = load_coding_overrides(data_root)
    merged = normalize_coding({**current, **{key: coding.get(key) for key in _ALLOWED_OVERRIDE_KEYS if key in coding}})
    payload = {key: merged[key] for key in sorted(_ALLOWED_OVERRIDE_KEYS)}
    write_text_atomic(path, json.dumps(payload, indent=2, sort_keys=True))
    try:
        from blackboard.workspace.version_control import commit_safely
        commit_safely("server: update coding settings", kind="vcs.server_coding", paths=[str(path)])
    except Exception:
        pass
    return merged


def coding_snapshot(coding: Mapping[str, Any], *, runtime_max_concurrent: int) -> Dict[str, Any]:
    current = normalize_coding(coding)
    runtime_value = max(1, int(runtime_max_concurrent or 1))
    configured = int(current.get("max_concurrent") or 1)
    return {
        "max_concurrent": configured,
        "runtime_max_concurrent": runtime_value,
        "restart_required": configured != runtime_value,
    }
