"""Atomic file writes with Windows-aware retries.

Ported verbatim from luna/kernel/atomic_files.py.
"""
from __future__ import annotations

import errno
import os
import time
from pathlib import Path


_ATOMIC_REPLACE_RETRIES = 5
_ATOMIC_REPLACE_BASE_DELAY_S = 0.02


def _is_transient_replace_error(exc: BaseException) -> bool:
    if not isinstance(exc, PermissionError):
        return False
    winerror = getattr(exc, "winerror", None)
    err_no = getattr(exc, "errno", None)
    return winerror in {5, 32} or err_no in {errno.EACCES, errno.EPERM}


def write_text_atomically(path: Path, content: str, encoding: str = "utf-8") -> None:
    """Write ``content`` to ``path`` atomically using a tmp file + os.replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.bb_tmp_{os.getpid()}_{time.time_ns()}")
    try:
        tmp_path.write_text(content, encoding=encoding)
        for attempt in range(_ATOMIC_REPLACE_RETRIES + 1):
            try:
                os.replace(tmp_path, path)
                break
            except OSError as exc:
                if attempt >= _ATOMIC_REPLACE_RETRIES or not _is_transient_replace_error(exc):
                    raise
                time.sleep(_ATOMIC_REPLACE_BASE_DELAY_S * (attempt + 1))
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass


def write_bytes_atomically(path: Path, content: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.bb_tmp_{os.getpid()}_{time.time_ns()}")
    try:
        tmp_path.write_bytes(content)
        for attempt in range(_ATOMIC_REPLACE_RETRIES + 1):
            try:
                os.replace(tmp_path, path)
                break
            except OSError as exc:
                if attempt >= _ATOMIC_REPLACE_RETRIES or not _is_transient_replace_error(exc):
                    raise
                time.sleep(_ATOMIC_REPLACE_BASE_DELAY_S * (attempt + 1))
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass


def append_text_atomically(path: Path, content: str, encoding: str = "utf-8") -> None:
    """Append ``content`` to ``path`` atomically (read + concat + atomic write)."""
    path = Path(path)
    existing = path.read_text(encoding=encoding) if path.exists() else ""
    write_text_atomically(path, existing + content, encoding=encoding)
