"""Persistence for per-profile API key overrides set through the UI.

Stored at ``<data_root>/providers/key_overrides.json`` with **0o600** permissions
where the OS supports it. The file holds inline ``api_key`` values per profile,
applied on registry boot — same shape as ``role_overrides.json``.

This is a deliberate fallback for users who don't want to set env vars and can't
use the OS keyring (or don't care about keyring isolation in a single-user dev box).
The file is git-ignored via ``data/`` being in ``.gitignore``.

Schema:
    {
      "fireworks-fast": {"api_key": "fw_..."},
      "anthropic-main": {"api_key": "sk-ant-..."}
    }
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any, Dict

from blackboard.kernel.atomic_files import write_text_atomically as write_text_atomic
from blackboard.kernel.logger import get_logger

logger = get_logger("workspace.key_overrides")


def _path(data_root: Path) -> Path:
    return Path(data_root) / "providers" / "key_overrides.json"


def load_keys(data_root: Path) -> Dict[str, str]:
    """Return ``{profile_id: api_key}`` from disk (empty dict if file missing/bad)."""
    p = _path(data_root)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        out: Dict[str, str] = {}
        for pid, spec in data.items():
            if isinstance(spec, dict):
                value = str(spec.get("api_key") or "").strip()
            elif isinstance(spec, str):
                value = spec.strip()
            else:
                continue
            if value:
                out[str(pid)] = value
        return out
    except Exception as exc:
        logger.warning("[key_overrides] failed to load %s: %s", p, exc)
        return {}


def save_key(data_root: Path, profile_id: str, value: str) -> None:
    p = _path(data_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    current_blob: Dict[str, Any] = {}
    if p.exists():
        try:
            parsed = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                current_blob = parsed
        except Exception:
            pass
    current_blob[profile_id] = {"api_key": (value or "").strip()}
    write_text_atomic(p, json.dumps(current_blob, indent=2, sort_keys=True))
    # Best-effort permission tightening on POSIX.
    try:
        if os.name != "nt":
            os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        pass
    # Auto-commit the override file. The commit message intentionally records
    # ONLY the profile id and length of the value, never the secret itself.
    try:
        from blackboard.workspace.version_control import commit_safely
        commit_safely(
            f"key: set inline api_key for {profile_id} (len={len(value or '')})",
            kind="vcs.key_override",
            paths=[str(p)],
        )
    except Exception:
        pass


def delete_key(data_root: Path, profile_id: str) -> bool:
    p = _path(data_root)
    if not p.exists():
        return False
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or profile_id not in data:
            return False
        del data[profile_id]
        if data:
            write_text_atomic(p, json.dumps(data, indent=2, sort_keys=True))
        else:
            p.unlink(missing_ok=True)
        try:
            from blackboard.workspace.version_control import commit_safely
            commit_safely(f"key: clear inline api_key for {profile_id}", kind="vcs.key_override_delete", paths=[str(p)])
        except Exception:
            pass
        return True
    except Exception as exc:
        logger.warning("[key_overrides] failed to delete %s: %s", profile_id, exc)
        return False
