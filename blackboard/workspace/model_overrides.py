from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from blackboard.kernel.atomic_files import write_text_atomically as write_text_atomic
from blackboard.kernel.logger import get_logger

logger = get_logger("workspace.model_overrides")


def _path(data_root: Path) -> Path:
    return Path(data_root) / "providers" / "model_overrides.json"


def load_model_overrides(data_root: Path) -> Dict[str, Dict[str, Any]]:
    p = _path(data_root)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        out: Dict[str, Dict[str, Any]] = {}
        for profile_id, spec in data.items():
            if not isinstance(spec, dict):
                continue
            model = str(spec.get("model") or "").strip()
            models = [str(m).strip() for m in (spec.get("models") or []) if str(m).strip()]
            clean: Dict[str, Any] = {}
            if model:
                clean["model"] = model
            if models:
                clean["models"] = models
            if clean:
                out[str(profile_id)] = clean
        return out
    except Exception as exc:
        logger.warning("[model_overrides] failed to load %s: %s", p, exc)
        return {}


def save_model_override(data_root: Path, profile_id: str, *, model: str = "", models: List[str] | None = None) -> Dict[str, Any]:
    p = _path(data_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    current = load_model_overrides(data_root)
    entry = dict(current.get(profile_id) or {})
    if model:
        entry["model"] = str(model).strip()
    if models is not None:
        entry["models"] = [str(m).strip() for m in models if str(m).strip()]
    if not entry.get("model") and not entry.get("models"):
        current.pop(profile_id, None)
    else:
        current[profile_id] = entry
    if current:
        write_text_atomic(p, json.dumps(current, indent=2, sort_keys=True))
    else:
        p.unlink(missing_ok=True)
    try:
        from blackboard.workspace.version_control import commit_safely
        commit_safely(
            f"model: update provider model override for {profile_id}",
            kind="vcs.model_override",
            paths=[str(p)],
        )
    except Exception:
        pass
    return dict(current.get(profile_id) or {})


def delete_model_override(data_root: Path, profile_id: str) -> bool:
    p = _path(data_root)
    if not p.exists():
        return False
    current = load_model_overrides(data_root)
    if profile_id not in current:
        return False
    del current[profile_id]
    if current:
        write_text_atomic(p, json.dumps(current, indent=2, sort_keys=True))
    else:
        p.unlink(missing_ok=True)
    try:
        from blackboard.workspace.version_control import commit_safely
        commit_safely(f"model: clear provider model override for {profile_id}", kind="vcs.model_override_delete", paths=[str(p)])
    except Exception:
        pass
    return True


def merge_into_profiles(profiles: Dict[str, Any], overrides: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    out = {profile_id: dict(profile or {}) for profile_id, profile in (profiles or {}).items()}
    for profile_id, spec in (overrides or {}).items():
        if profile_id not in out:
            continue
        if spec.get("model"):
            out[profile_id]["model"] = spec["model"]
        if spec.get("models"):
            out[profile_id]["models"] = list(spec["models"] or [])
    return out
