"""Kernel layer — primitives with no external dependencies."""
from blackboard.kernel.atomic_files import (
    append_text_atomically,
    write_text_atomically,
)
from blackboard.kernel.bus import Bus, get_bus
from blackboard.kernel.config import Config, load_config
from blackboard.kernel.logger import describe_error, get_logger

__all__ = [
    "append_text_atomically",
    "write_text_atomically",
    "Bus",
    "get_bus",
    "Config",
    "load_config",
    "describe_error",
    "get_logger",
]
