"""Logger + error description helpers (slim port of luna/kernel/logger.py)."""
from __future__ import annotations

import logging
import sys
from typing import Any


_INITIALIZED = False
_LEVEL = logging.INFO


def _ensure_initialized() -> None:
    global _INITIALIZED
    if _INITIALIZED:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-5s %(name)s %(message)s", datefmt="%H:%M:%S")
    )
    root = logging.getLogger("blackboard")
    root.addHandler(handler)
    root.setLevel(_LEVEL)
    root.propagate = False
    _INITIALIZED = True


def set_level(level: str | int) -> None:
    global _LEVEL
    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)
    _LEVEL = int(level)
    logging.getLogger("blackboard").setLevel(_LEVEL)


def get_logger(name: str) -> logging.Logger:
    _ensure_initialized()
    if not name.startswith("blackboard"):
        name = f"blackboard.{name}"
    return logging.getLogger(name)


def describe_error(error: Any, default: str = "unknown error") -> str:
    """Normalize an exception or error-like value to a human-readable string."""
    if error is None:
        return default
    if isinstance(error, BaseException):
        message = str(error).strip()
        if message:
            return message
        cls = type(error).__name__
        if "timeout" in cls.lower():
            return "Request timed out"
        return cls or default
    text = str(error).strip()
    return text or default
