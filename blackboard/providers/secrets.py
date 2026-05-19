"""Secret resolution — env first, OS keyring second. Values never logged."""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

from blackboard.kernel.logger import get_logger

logger = get_logger("providers.secrets")

try:
    import keyring as _keyring
except Exception:
    _keyring = None  # type: ignore[assignment]


_KEYRING_SERVICE = "blackboard"
_registry: Dict[str, Dict[str, Any]] = {}


def configure(registry: Dict[str, Any]) -> None:
    """Register the secret_id -> source mapping from config.yaml `providers.secrets`."""
    global _registry
    _registry = {str(k): dict(v or {}) for k, v in (registry or {}).items()}


def resolve(secret_id: str) -> str:
    """Return the resolved secret string, or "" if not configured/found.

    Never logs the resolved value.
    """
    if not secret_id:
        return ""
    record = _registry.get(secret_id, {})
    env_name = str(record.get("env") or "").strip()
    if env_name:
        value = os.environ.get(env_name, "")
        if value:
            return value
    keyring_name = str(record.get("keyring") or "").strip()
    if keyring_name and _keyring is not None:
        try:
            value = _keyring.get_password(_KEYRING_SERVICE, keyring_name) or ""
            if value:
                return value
        except Exception as exc:
            logger.debug("[secrets] keyring lookup failed for %s: %s", secret_id, exc)
    fallback_env = f"BLACKBOARD_{secret_id.upper()}"
    return os.environ.get(fallback_env, "")


def store_keyring(secret_id: str, value: str) -> bool:
    """Store a secret in the OS keyring under the configured keyring name. Returns success."""
    if _keyring is None:
        return False
    record = _registry.get(secret_id, {})
    keyring_name = str(record.get("keyring") or secret_id).strip()
    try:
        _keyring.set_password(_KEYRING_SERVICE, keyring_name, value or "")
        return True
    except Exception as exc:
        logger.debug("[secrets] keyring set failed for %s: %s", secret_id, exc)
        return False


def known_ids() -> list[str]:
    return list(_registry.keys())
