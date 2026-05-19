"""YAML-backed config with dotted-key lookups."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import yaml

from blackboard.kernel.logger import get_logger, set_level

logger = get_logger("kernel.config")


class Config:
    """Read-only dotted-key config."""

    def __init__(self, data: Dict[str, Any]) -> None:
        self._data = data or {}

    def get(self, key: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in key.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return default
        return node

    def section(self, key: str) -> Dict[str, Any]:
        value = self.get(key, {})
        return dict(value) if isinstance(value, dict) else {}

    def as_dict(self) -> Dict[str, Any]:
        return dict(self._data)

    def __contains__(self, key: str) -> bool:
        return self.get(key, None) is not None


def load_config(path: str | Path) -> Config:
    p = Path(path)
    if not p.exists():
        logger.warning("Config file not found: %s — using empty config", p)
        return Config({})
    raw = p.read_text(encoding="utf-8")
    data = yaml.safe_load(raw) or {}
    cfg = Config(data)
    level = cfg.get("logging.level")
    if level:
        set_level(level)
    return cfg
