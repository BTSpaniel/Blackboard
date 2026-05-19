"""File operation tools — read, write, patch, list, search-in-file.

Slim port of luna/workers/react/tools/file_ops.py with workspace policy enforcement.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from blackboard.kernel.atomic_files import write_text_atomically
from blackboard.react.tool_registry import ToolRegistry

_workspace_root: Optional[Path] = None
_full_access: bool = False


def set_workspace_policy(root: Optional[Path], full_access: bool = False) -> None:
    global _workspace_root, _full_access
    _workspace_root = root.resolve() if root else None
    _full_access = bool(full_access)


def _runtime_root(args: Optional[Dict[str, Any]] = None) -> Optional[Path]:
    runtime = (args or {}).get("_tool_runtime") or {}
    root = runtime.get("workspace_root") or runtime.get("execution_root")
    if root:
        try:
            return Path(root).resolve()
        except Exception:
            return None
    return None


def _normalize_relative_path(p: str, root: Optional[Path]) -> str:
    value = str(p or "").replace("\\", "/").lstrip("./")
    if root is not None and root.name.lower() == "workspace":
        for prefix in ("blackboard/workspace/", "workspace/"):
            if value.lower().startswith(prefix):
                return value[len(prefix):]
    return str(p or "")


def _resolve_path(p: str, args: Optional[Dict[str, Any]] = None) -> Path:
    runtime_root = _runtime_root(args)
    root = runtime_root or _workspace_root
    path = Path(_normalize_relative_path(p, root)).expanduser()
    if path.is_absolute():
        target = path.resolve()
    elif root:
        target = (root / path).resolve()
    else:
        target = path.resolve()
    if not _full_access and root is not None:
        try:
            target.relative_to(root)
        except ValueError:
            raise PermissionError(f"path outside workspace root: {p}")
    return target


def _read_file(args: Dict[str, Any]) -> str:
    path_str = str(args.get("path") or "")
    if not path_str:
        return json.dumps({"error": "path required"})
    path = _resolve_path(path_str, args)
    try:
        if not path.exists():
            return json.dumps({"error": f"not found: {path_str}", "path": path_str})
        if path.is_dir():
            return json.dumps({"error": "is a directory", "path": path_str})
        text = path.read_text(encoding="utf-8", errors="replace")
        return json.dumps({"path": str(path), "content": text, "bytes": len(text)})
    except PermissionError as exc:
        return json.dumps({"error": str(exc)})


def _list_dir(args: Dict[str, Any]) -> str:
    path_str = str(args.get("path") or ".")
    path = _resolve_path(path_str, args)
    if not path.exists():
        return json.dumps({"error": f"not found: {path_str}"})
    if not path.is_dir():
        return json.dumps({"error": "not a directory"})
    entries: List[Dict[str, Any]] = []
    for entry in sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        entries.append({
            "name": entry.name,
            "type": "dir" if entry.is_dir() else "file",
            "size": entry.stat().st_size if entry.is_file() else None,
        })
    return json.dumps({"path": str(path), "entries": entries})


def _write_file(args: Dict[str, Any]) -> str:
    path_str = str(args.get("path") or "")
    content = args.get("content")
    if not path_str:
        return json.dumps({"error": "path required"})
    if content is None:
        return json.dumps({"error": "content required"})
    if isinstance(content, (dict, list)):
        content = json.dumps(content, indent=2)
    path = _resolve_path(path_str, args)
    before_exists = path.exists()
    before_bytes = path.stat().st_size if before_exists else 0
    try:
        write_text_atomically(path, str(content))
    except Exception as exc:
        return json.dumps({"error": str(exc)})
    # Verify
    try:
        written = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return json.dumps({"error": f"verification read failed: {exc}"})
    if written != str(content):
        return json.dumps({"error": "verification mismatch after write"})
    return json.dumps({
        "path": str(path),
        "created": not before_exists,
        "before_bytes": before_bytes,
        "after_bytes": len(written),
        "verified": True,
    })


def _patch_file(args: Dict[str, Any]) -> str:
    """Find-and-replace in an existing file. old_string must match exactly once."""
    path_str = str(args.get("path") or "")
    old = args.get("old_string")
    new = args.get("new_string")
    if not path_str or old is None or new is None:
        return json.dumps({"error": "path, old_string, new_string required"})
    path = _resolve_path(path_str, args)
    if not path.exists() or not path.is_file():
        return json.dumps({"error": f"not a file: {path_str}"})
    text = path.read_text(encoding="utf-8", errors="replace")
    occurrences = text.count(str(old))
    if occurrences == 0:
        return json.dumps({"error": "old_string not found"})
    if occurrences > 1:
        return json.dumps({"error": f"old_string matches {occurrences} times; please include more context"})
    new_text = text.replace(str(old), str(new), 1)
    write_text_atomically(path, new_text)
    verified = path.read_text(encoding="utf-8", errors="replace")
    if verified != new_text:
        return json.dumps({"error": "verification mismatch after patch"})
    return json.dumps({"path": str(path), "before_bytes": len(text), "after_bytes": len(verified), "verified": True})


def _replace_lines(args: Dict[str, Any]) -> str:
    """Replace a 1-indexed inclusive line range with new content."""
    path_str = str(args.get("path") or "")
    start = int(args.get("start_line") or 0)
    end = int(args.get("end_line") or 0)
    new_content = args.get("content") or ""
    if not path_str or start <= 0 or end < start:
        return json.dumps({"error": "path, start_line, end_line required (1-indexed)"})
    path = _resolve_path(path_str, args)
    if not path.exists():
        return json.dumps({"error": f"not found: {path_str}"})
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if end > len(lines):
        return json.dumps({"error": f"end_line {end} > file length {len(lines)}"})
    new_lines = lines[: start - 1] + str(new_content).splitlines() + lines[end:]
    new_text = "\n".join(new_lines)
    if path.read_text(encoding="utf-8", errors="replace").endswith("\n"):
        new_text += "\n"
    write_text_atomically(path, new_text)
    return json.dumps({"path": str(path), "replaced_lines": end - start + 1, "verified": True})


def _delete_file(args: Dict[str, Any]) -> str:
    path_str = str(args.get("path") or "")
    if not path_str:
        return json.dumps({"error": "path required"})
    path = _resolve_path(path_str, args)
    if not path.exists():
        return json.dumps({"error": f"not found: {path_str}"})
    if path.is_dir():
        return json.dumps({"error": "is a directory; use a directory tool"})
    path.unlink()
    return json.dumps({"path": str(path), "deleted": not path.exists(), "verified": not path.exists()})


def register_file_tools(registry: ToolRegistry) -> None:
    registry.register_fn(
        "read_file",
        "Read a text file and return its content. Resolves relative paths against the workspace root.",
        {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Relative or absolute path"}},
            "required": ["path"],
        },
        _read_file,
        tags=["fs", "read"],
    )
    registry.register_fn(
        "list_dir",
        "List entries in a directory.",
        {
            "type": "object",
            "properties": {"path": {"type": "string", "default": "."}},
        },
        _list_dir,
        tags=["fs", "read"],
    )
    registry.register_fn(
        "write_file",
        "Create or overwrite a file with the given content. Atomic + verified on disk.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
        _write_file,
        tags=["fs", "write"],
        mutation_mode="verified",
    )
    registry.register_fn(
        "patch_file",
        "Replace an exact unique substring inside an existing file. Atomic + verified.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
            },
            "required": ["path", "old_string", "new_string"],
        },
        _patch_file,
        tags=["fs", "write"],
        mutation_mode="verified",
    )
    registry.register_fn(
        "replace_lines",
        "Replace a 1-indexed inclusive line range with new content.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer"},
                "end_line": {"type": "integer"},
                "content": {"type": "string"},
            },
            "required": ["path", "start_line", "end_line", "content"],
        },
        _replace_lines,
        tags=["fs", "write"],
        mutation_mode="verified",
    )
    registry.register_fn(
        "delete_file",
        "Delete a file.",
        {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        _delete_file,
        tags=["fs", "write"],
        mutation_mode="verified",
    )
