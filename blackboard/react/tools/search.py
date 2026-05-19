"""Search tools — grep code, find files."""
from __future__ import annotations

import asyncio
import fnmatch
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from blackboard.kernel.logger import get_logger
from blackboard.react.tool_registry import ToolRegistry
from blackboard.react.tools.file_ops import _resolve_path  # reuse policy

_SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".pytest_cache", ".worktrees", ".venv", "venv"}
_SKIP_SUFFIXES = {".pyc", ".pyo", ".so", ".dll", ".exe", ".bin", ".db", ".sqlite", ".png", ".jpg", ".gif", ".pdf", ".zip"}
_MAX_RESULTS = 200
_MAX_FILE_BYTES = 1_500_000
_MAX_MULTI_QUERIES = 8
_WINDOWS_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

logger = get_logger("react.tools.search")


def _normalize_limit(value: Any, default: int, maximum: int) -> int:
    try:
        parsed = int(value or default)
    except Exception:
        parsed = default
    return max(1, min(parsed, maximum))


def _as_globs(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    return [text] if text else []


def _relative_path(full: Path, root: Path) -> str:
    try:
        return str(full.relative_to(root)).replace("\\", "/")
    except ValueError:
        return full.name if root.is_file() else str(full).replace("\\", "/")


def _glob_candidates(full: Path, root: Path) -> List[str]:
    rel = _relative_path(full, root if root.is_dir() else root.parent)
    return [full.name, rel]


def _matches_globs(candidates: List[str], globs: Optional[List[str]]) -> bool:
    if not globs:
        return True
    for glob in globs:
        if any(fnmatch.fnmatch(candidate, glob) for candidate in candidates):
            return True
    return False


def _path_allowed(full: Path, root: Path, *, include_globs: Optional[List[str]] = None, exclude_globs: Optional[List[str]] = None) -> bool:
    candidates = _glob_candidates(full, root if root.is_dir() else root.parent)
    if include_globs and not _matches_globs(candidates, include_globs):
        return False
    if exclude_globs and _matches_globs(candidates, exclude_globs):
        return False
    return True


def _walk_files(root: Path, *, include_globs: Optional[List[str]] = None, exclude_globs: Optional[List[str]] = None):
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        for name in files:
            if name.startswith("."):
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext in _SKIP_SUFFIXES:
                continue
            full = Path(dirpath) / name
            if not _path_allowed(full, root, include_globs=include_globs, exclude_globs=exclude_globs):
                continue
            yield full


def _iter_candidate_files(root: Path, *, include_globs: Optional[List[str]] = None, exclude_globs: Optional[List[str]] = None):
    if root.is_file():
        if root.name.startswith("."):
            return
        if root.suffix.lower() in _SKIP_SUFFIXES:
            return
        if _path_allowed(root, root.parent, include_globs=include_globs, exclude_globs=exclude_globs):
            yield root
        return
    yield from _walk_files(root, include_globs=include_globs, exclude_globs=exclude_globs)


def _search_root(path_text: str, args: Dict[str, Any]) -> Tuple[Optional[Path], Optional[Dict[str, Any]]]:
    try:
        root = _resolve_path(path_text, args)
    except PermissionError as exc:
        return None, {"error": str(exc), "path": path_text}
    if not root.exists():
        return None, {"error": f"not found: {path_text}", "path": path_text}
    return root, None


def _available_backend() -> Tuple[str, Optional[str]]:
    binary = shutil.which("rg")
    if binary:
        return "ripgrep", binary
    return "python", None


def _search_target(root: Path) -> Tuple[Path, str]:
    if root.is_file():
        return root.parent, root.name
    return root, "."


def _literal_match(line: str, pattern: str, *, case_sensitive: bool) -> bool:
    if case_sensitive:
        return pattern in line
    return pattern.lower() in line.lower()


def _search_files_payload(args: Dict[str, Any]) -> Dict[str, Any]:
    pattern = str(args.get("pattern") or "").strip()
    root_str = str(args.get("path") or ".")
    limit = _normalize_limit(args.get("limit"), 100, _MAX_RESULTS)
    exclude_globs = _as_globs(args.get("exclude"))
    if not pattern:
        return {"error": "pattern required"}
    root, error = _search_root(root_str, args)
    if error:
        return error
    backend, binary = _available_backend()
    if binary:
        try:
            cwd, target = _search_target(root)
            cmd = [binary, "--files"]
            if target != ".":
                cmd.append(target)
            kwargs: Dict[str, Any] = {}
            if _WINDOWS_CREATE_NO_WINDOW:
                kwargs["creationflags"] = _WINDOWS_CREATE_NO_WINDOW
            logger.info(
                "[spawn][search_files] cwd=%s cmd=%s creationflags=%s",
                str(cwd),
                cmd,
                kwargs.get("creationflags") or 0,
            )
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                cwd=str(cwd),
                **kwargs,
            )
            if result.returncode == 0:
                matches: List[Dict[str, Any]] = []
                truncated = False
                for line in result.stdout.splitlines():
                    rel = str(line or "").strip().replace("\\", "/")
                    if not rel:
                        continue
                    full = (cwd / rel).resolve()
                    if not full.is_file():
                        continue
                    if not _path_allowed(full, cwd, include_globs=[pattern], exclude_globs=exclude_globs):
                        continue
                    matches.append({"path": _relative_path(full, cwd), "size": full.stat().st_size})
                    if len(matches) >= limit:
                        truncated = True
                        break
                return {"root": str(root), "pattern": pattern, "backend": backend, "matches": matches, "truncated": truncated}
        except Exception:
            pass
    matches = []
    truncated = False
    for full in _iter_candidate_files(root, include_globs=[pattern], exclude_globs=exclude_globs):
        matches.append({"path": _relative_path(full, root if root.is_dir() else root.parent), "size": full.stat().st_size})
        if len(matches) >= limit:
            truncated = True
            break
    return {"root": str(root), "pattern": pattern, "backend": "python", "matches": matches, "truncated": truncated}


def _search_code_payload(args: Dict[str, Any]) -> Dict[str, Any]:
    pattern = str(args.get("pattern") or "").strip()
    root_str = str(args.get("path") or ".")
    include_globs = _as_globs(args.get("include"))
    exclude_globs = _as_globs(args.get("exclude"))
    limit = _normalize_limit(args.get("limit"), _MAX_RESULTS, _MAX_RESULTS)
    case_sensitive = bool(args.get("case_sensitive") or False)
    context_lines = max(0, int(args.get("context_lines") or 0))
    files_only = bool(args.get("files_only") or False)
    mode = str(args.get("mode") or "").strip().lower()
    is_regex = bool(args.get("regex") or False) or mode == "regex"
    if not pattern:
        return {"error": "pattern required"}
    root, error = _search_root(root_str, args)
    if error:
        return error

    if is_regex:
        try:
            regex = re.compile(pattern, 0 if case_sensitive else re.IGNORECASE)
        except re.error as exc:
            return {"error": f"bad regex: {exc}", "pattern": pattern}
    else:
        regex = None

    backend, binary = _available_backend()
    if binary and context_lines == 0:
        try:
            cwd, target = _search_target(root)
            cmd = [binary, "--line-number", "--no-heading", "--color=never"]
            if files_only:
                cmd.append("--files-with-matches")
            if case_sensitive:
                cmd.append("--case-sensitive")
            else:
                cmd.append("--ignore-case")
            if not is_regex:
                cmd.append("--fixed-strings")
            for glob in include_globs:
                cmd.extend(["--glob", glob])
            for glob in exclude_globs:
                cmd.extend(["--glob", f"!{glob}"])
            cmd.extend(["--", pattern, target])
            kwargs: Dict[str, Any] = {}
            if _WINDOWS_CREATE_NO_WINDOW:
                kwargs["creationflags"] = _WINDOWS_CREATE_NO_WINDOW
            logger.info(
                "[spawn][search_code] cwd=%s cmd=%s creationflags=%s",
                str(cwd),
                cmd,
                kwargs.get("creationflags") or 0,
            )
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                cwd=str(cwd),
                **kwargs,
            )
            if result.returncode in (0, 1):
                output_lines = [line for line in result.stdout.splitlines() if str(line).strip()]
                truncated = len(output_lines) > limit
                if files_only:
                    files = []
                    for rel in output_lines[:limit]:
                        files.append({"path": str(rel).replace("\\", "/")})
                    return {
                        "root": str(root),
                        "pattern": pattern,
                        "backend": backend,
                        "mode": "regex" if is_regex else "literal",
                        "results": files,
                        "truncated": truncated,
                        "files_only": True,
                    }
                results: List[Dict[str, Any]] = []
                for line in output_lines[:limit]:
                    match = re.match(r"^(.*?):(\d+):(.*)$", line)
                    if not match:
                        continue
                    results.append({
                        "path": str(match.group(1)).replace("\\", "/"),
                        "line": int(match.group(2)),
                        "match": match.group(3).strip()[:240],
                    })
                return {
                    "root": str(root),
                    "pattern": pattern,
                    "backend": backend,
                    "mode": "regex" if is_regex else "literal",
                    "results": results,
                    "truncated": truncated,
                    "files_only": False,
                }
        except Exception:
            pass

    results: List[Dict[str, Any]] = []
    truncated = False
    display_root = root if root.is_dir() else root.parent
    for full in _iter_candidate_files(root, include_globs=include_globs, exclude_globs=exclude_globs):
        if full.stat().st_size > _MAX_FILE_BYTES:
            continue
        try:
            lines = full.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            continue
        rel = _relative_path(full, display_root)
        matched_file = False
        for lineno, line in enumerate(lines, 1):
            matched = regex.search(line) if regex is not None else _literal_match(line, pattern, case_sensitive=case_sensitive)
            if not matched:
                continue
            matched_file = True
            if files_only:
                results.append({"path": rel})
                break
            entry: Dict[str, Any] = {"path": rel, "line": lineno, "match": line.strip()[:240]}
            if context_lines:
                start = max(0, lineno - context_lines - 1)
                end = min(len(lines), lineno + context_lines)
                entry["before"] = [value.rstrip() for value in lines[start: lineno - 1]]
                entry["after"] = [value.rstrip() for value in lines[lineno: end]]
            results.append(entry)
            if len(results) >= limit:
                truncated = True
                break
        if truncated:
            break
        if files_only and matched_file and len(results) >= limit:
            truncated = True
            break
    return {
        "root": str(root),
        "pattern": pattern,
        "backend": "python",
        "mode": "regex" if is_regex else "literal",
        "results": results,
        "truncated": truncated,
        "files_only": files_only,
    }


def _search_files(args: Dict[str, Any]) -> str:
    return json.dumps(_search_files_payload(args))


def _search_code(args: Dict[str, Any]) -> str:
    return json.dumps(_search_code_payload(args))


async def _search_multi(args: Dict[str, Any]) -> str:
    raw_queries = args.get("queries") or []
    if not isinstance(raw_queries, list) or not raw_queries:
        return json.dumps({"error": "queries required"})
    runtime = dict(args.get("_tool_runtime") or {})
    queries = raw_queries[:_MAX_MULTI_QUERIES]

    async def run_one(index: int, query: Any) -> Dict[str, Any]:
        payload = dict(query or {})
        label = str(payload.pop("label", "") or f"query_{index + 1}")
        kind = str(payload.pop("kind", "code") or "code").strip().lower()
        if runtime and "_tool_runtime" not in payload:
            payload["_tool_runtime"] = runtime
        if kind in {"code", "text", "search_code", "search_text"}:
            result = await asyncio.to_thread(_search_code_payload, payload)
        elif kind in {"files", "file", "search_files"}:
            result = await asyncio.to_thread(_search_files_payload, payload)
        else:
            result = {"error": f"unsupported search kind: {kind}"}
        return {"index": index, "label": label, "kind": kind, **result}

    query_results = await asyncio.gather(*(run_one(index, query) for index, query in enumerate(queries)))
    merged_paths: List[str] = []
    seen_paths = set()
    error_count = 0
    total_matches = 0
    for result in query_results:
        if result.get("error"):
            error_count += 1
        matches = result.get("matches") or result.get("results") or []
        total_matches += len(matches)
        for item in matches:
            path = str((item or {}).get("path") or "").strip()
            if not path or path in seen_paths:
                continue
            seen_paths.add(path)
            merged_paths.append(path)
    return json.dumps({
        "requested_queries": len(raw_queries),
        "executed_queries": len(queries),
        "truncated_queries": len(raw_queries) > len(queries),
        "query_results": query_results,
        "merged_paths": merged_paths,
        "total_matches": total_matches,
        "error_count": error_count,
    })


def register_search_tools(registry: ToolRegistry) -> None:
    registry.register_fn(
        "search_files",
        "Find files by glob. Prefers ripgrep when installed and falls back to the built-in Python scanner.",
        {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern for filenames or relative paths."},
                "path": {"type": "string", "default": "."},
                "exclude": {"type": "array", "items": {"type": "string"}, "description": "Optional globs to exclude."},
                "limit": {"type": "integer", "default": 100},
            },
            "required": ["pattern"],
        },
        _search_files,
        tags=["fs", "read", "search"],
        aliases=["find_files"],
        domain="search",
    )
    registry.register_fn(
        "search_code",
        "Search file contents using literal or regex mode. Prefers ripgrep when installed and falls back to the built-in Python scanner.",
        {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string", "default": "."},
                "include": {"type": "array", "items": {"type": "string"}, "description": "Optional filename or relative-path globs to include."},
                "exclude": {"type": "array", "items": {"type": "string"}, "description": "Optional filename or relative-path globs to exclude."},
                "regex": {"type": "boolean", "default": False},
                "mode": {"type": "string", "enum": ["literal", "regex"]},
                "case_sensitive": {"type": "boolean", "default": False},
                "context_lines": {"type": "integer", "default": 0},
                "files_only": {"type": "boolean", "default": False},
                "limit": {"type": "integer", "default": 200},
            },
            "required": ["pattern"],
        },
        _search_code,
        tags=["fs", "read", "search"],
        aliases=["search_text", "grep_code"],
        domain="search",
    )
    registry.register_fn(
        "search_multi",
        "Run multiple read-only search queries in parallel and merge the discovered paths.",
        {
            "type": "object",
            "properties": {
                "queries": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string"},
                            "kind": {"type": "string", "enum": ["code", "text", "files"]},
                            "pattern": {"type": "string"},
                            "path": {"type": "string"},
                            "include": {"type": "array", "items": {"type": "string"}},
                            "exclude": {"type": "array", "items": {"type": "string"}},
                            "regex": {"type": "boolean"},
                            "mode": {"type": "string", "enum": ["literal", "regex"]},
                            "case_sensitive": {"type": "boolean"},
                            "context_lines": {"type": "integer"},
                            "files_only": {"type": "boolean"},
                            "limit": {"type": "integer"}
                        },
                        "required": ["pattern"]
                    }
                }
            },
            "required": ["queries"],
        },
        _search_multi,
        timeout_s=45.0,
        tags=["fs", "read", "search"],
        aliases=["parallel_search", "multi_search"],
        domain="search",
    )
