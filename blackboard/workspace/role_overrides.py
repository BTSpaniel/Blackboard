"""Persistence for provider role priority overrides.

Stored at ``<data_root>/providers/role_overrides.json``. When present, this file's
contents are merged ON TOP of the role assignments defined in ``config.yaml``,
giving the operator a way to reorder primary/fallbacks at runtime without
editing config.

Schema:
    {
      "coder": {
        "profile": "openai-main",
        "fallbacks": ["anthropic-main", "claude-code"],
        "disabled": ["claude-code"]
      },
      "reviewer": {"profile": "anthropic-main", "fallbacks": ["openai-main"]}
    }

``disabled`` is an optional list of profile ids in the chain that the runtime should
skip entirely (kept on disk for visibility — the UI greys them out).

Roles not present in the file fall back to whatever ``config.yaml`` declared.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from blackboard.kernel.atomic_files import write_text_atomically as write_text_atomic
from blackboard.kernel.logger import get_logger

logger = get_logger("workspace.role_overrides")


def _path(data_root: Path) -> Path:
    return Path(data_root) / "providers" / "role_overrides.json"


def load_overrides(data_root: Path) -> Dict[str, Dict[str, Any]]:
    """Return the merged-friendly ``{role: {profile, fallbacks}}`` dict from disk, or {}."""
    p = _path(data_root)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        out: Dict[str, Dict[str, Any]] = {}
        for role, spec in data.items():
            if not isinstance(spec, dict):
                continue
            profile = str(spec.get("profile") or "").strip()
            fallbacks = [str(f).strip() for f in (spec.get("fallbacks") or []) if isinstance(f, str) and f.strip()]
            disabled = [str(d).strip() for d in (spec.get("disabled") or []) if isinstance(d, str) and d.strip()]
            if profile:
                out[str(role)] = {"profile": profile, "fallbacks": fallbacks, "disabled": disabled}
        return out
    except Exception as exc:
        logger.warning("[role_overrides] failed to load %s: %s", p, exc)
        return {}


def save_override(
    data_root: Path,
    role: str,
    profile: str,
    fallbacks: List[str],
    *,
    disabled: List[str] | None = None,
) -> None:
    """Persist a single role override; merges with any existing entries on disk."""
    p = _path(data_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    current = load_overrides(data_root)
    current[role] = {
        "profile": profile,
        "fallbacks": list(fallbacks or []),
        "disabled": list(disabled or []),
    }
    write_text_atomic(p, json.dumps(current, indent=2, sort_keys=True))
    # Auto-commit a snapshot to data/.git so the change is recoverable.
    try:
        from blackboard.workspace.version_control import commit_safely
        commit_safely(
            f"role: update {role} → {profile} (+{len(fallbacks or [])} fb, {len(disabled or [])} off)",
            kind="vcs.role_override",
            paths=[str(p)],
        )
    except Exception:
        pass


def delete_override(data_root: Path, role: str) -> bool:
    """Remove the override for ``role`` (so it falls back to config.yaml)."""
    p = _path(data_root)
    if not p.exists():
        return False
    current = load_overrides(data_root)
    if role not in current:
        return False
    del current[role]
    if current:
        write_text_atomic(p, json.dumps(current, indent=2, sort_keys=True))
    else:
        p.unlink(missing_ok=True)
    try:
        from blackboard.workspace.version_control import commit_safely
        commit_safely(f"role: drop override for {role}", kind="vcs.role_override_delete", paths=[str(p)])
    except Exception:
        pass
    return True


def merge_into_config(config_roles: Dict[str, Any], overrides: Dict[str, Any]) -> Dict[str, Any]:
    """Return a fresh dict of role assignments with overrides applied on top of ``config_roles``."""
    out = {role: dict(spec) for role, spec in (config_roles or {}).items()}
    for role, spec in (overrides or {}).items():
        out[role] = {
            "profile": spec.get("profile"),
            "fallbacks": list(spec.get("fallbacks") or []),
            "disabled": list(spec.get("disabled") or []),
        }
    return out
