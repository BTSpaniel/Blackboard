"""Project intelligence — JSON-backed repo snapshot the planner/coder both read.

Slim port of luna/state/project_intelligence.py (no Wiki dep).
Stores at ``data/project_intelligence/<project_key>/`` with summary.json, master.md,
events.jsonl, outcomes.jsonl.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from blackboard.governors.data_protection import get_data_protection_governor
from blackboard.kernel.atomic_files import append_text_atomically, write_text_atomically
from blackboard.kernel.logger import get_logger

logger = get_logger("coding.project_intelligence")

_SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".pytest_cache", ".venv", "venv", ".worktrees", "dist", "build"}
_SCAN_LIMIT = 60
_OUTCOME_LIMIT = 6
_BACKLOG_LIMIT = 12
_FOLDER_MAP_REFRESH_INTERVAL_S = 20.0
_WINDOWS_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

# Resolved git executable (None = unavailable). Sentinel "" means "not resolved yet".
_GIT_BIN: Optional[str] = ""
# Memoized canonical-project-root lookups keyed by the input cwd path (resolved).
_CANONICAL_ROOT_CACHE: Dict[str, Path] = {}


def _resolve_git_bin() -> Optional[str]:
    """Locate the git executable once. Uses PATH then common Windows install paths."""
    global _GIT_BIN
    if _GIT_BIN != "":
        return _GIT_BIN
    found = shutil.which("git")
    if not found and os.name == "nt":
        for candidate in (
            r"C:\Program Files\Git\cmd\git.exe",
            r"C:\Program Files (x86)\Git\cmd\git.exe",
            r"C:\Program Files\Git\bin\git.exe",
        ):
            if Path(candidate).exists():
                found = candidate
                break
    _GIT_BIN = found or None
    if _GIT_BIN:
        logger.debug("[project_intel_git] resolved git: %s", _GIT_BIN)
    else:
        logger.debug("[project_intel_git] git not available — repo-root detection disabled")
    return _GIT_BIN


def _git_path(cwd: str, *args: str) -> str:
    git_bin = _resolve_git_bin()
    if not git_bin:
        return ""
    kwargs: Dict[str, Any] = {}
    if _WINDOWS_CREATE_NO_WINDOW:
        kwargs["creationflags"] = _WINDOWS_CREATE_NO_WINDOW
    logger.debug(
        "[spawn][project_intel_git] cwd=%s args=%s",
        str(cwd),
        ["rev-parse", "--path-format=absolute", *args],
    )
    try:
        result = subprocess.run(
            [git_bin, "rev-parse", "--path-format=absolute", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            **kwargs,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        return ""
    return ""


def _canonical_project_root(cwd: str) -> Path:
    base = Path(cwd).resolve()
    cache_key = str(base)
    cached = _CANONICAL_ROOT_CACHE.get(cache_key)
    if cached is not None:
        return cached
    common_dir_text = _git_path(str(base), "--git-common-dir")
    if common_dir_text:
        common_dir = Path(common_dir_text).resolve()
        if common_dir.name == ".git" and common_dir.parent.exists():
            root = common_dir.parent.resolve()
            _CANONICAL_ROOT_CACHE[cache_key] = root
            return root
        if common_dir.parent.name == ".git" and common_dir.parent.parent.exists():
            root = common_dir.parent.parent.resolve()
            _CANONICAL_ROOT_CACHE[cache_key] = root
            return root
    top_level_text = _git_path(str(base), "--show-toplevel")
    if top_level_text:
        root = Path(top_level_text).resolve()
        _CANONICAL_ROOT_CACHE[cache_key] = root
        return root
    _CANONICAL_ROOT_CACHE[cache_key] = base
    return base


def _project_key(cwd: str) -> str:
    """Derive a stable project key from the canonical repo root path."""
    root = str(_canonical_project_root(cwd)).replace("\\", "/").lower()
    return hashlib.sha1(root.encode("utf-8")).hexdigest()[:16]


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomically(path, json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _append_jsonl(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    append_text_atomically(path, json.dumps(payload, default=str) + "\n", encoding="utf-8")


def _scan_folder_map(root: Path) -> List[str]:
    out: List[str] = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        rel_dir = Path(dirpath).relative_to(root)
        if str(rel_dir) == ".":
            continue
        depth = len(rel_dir.parts)
        if depth > 3:
            dirs[:] = []
            continue
        out.append(str(rel_dir).replace("\\", "/") + "/")
        if len(out) >= _SCAN_LIMIT:
            break
    return out


def _refresh_folder_map(summary: Dict[str, Any], *, root: Path, force: bool = False) -> Dict[str, Any]:
    folder_map = list(summary.get("folder_map") or [])
    last_scanned = float(summary.get("folder_map_scanned_at") or 0.0)
    now = time.time()
    if folder_map and not force and now - last_scanned < _FOLDER_MAP_REFRESH_INTERVAL_S:
        return summary
    summary["folder_map"] = _scan_folder_map(root)
    summary["folder_map_scanned_at"] = now
    return summary


def project_dir(base_dir: Path, cwd: str) -> Path:
    return Path(base_dir) / _project_key(cwd)


def ensure_project_intelligence(
    base_dir: Path,
    *,
    cwd: str,
    task_files: Optional[List[str]] = None,
    objective: str = "",
) -> Dict[str, Any]:
    """Load or initialize the project-intel summary for ``cwd``."""
    pdir = project_dir(base_dir, cwd)
    summary_path = pdir / "summary.json"
    summary = _read_json(summary_path, {})
    root = _canonical_project_root(cwd)
    now = time.time()
    if not summary:
        summary = {
            "project_key": _project_key(cwd),
            "root": str(root),
            "created_at": now,
            "updated_at": now,
            "run_count": 0,
            "folder_map": _scan_folder_map(root),
            "folder_map_scanned_at": now,
            "hotspots": [],
            "conventions": [],
            "recent_outcomes": [],
            "upgrade_backlog": [],
            "last_objective": objective,
            "synopsis": "",
        }
        _write_json(summary_path, summary)
    else:
        summary = _refresh_folder_map(summary, root=root)
    return summary


def update_project_intelligence(
    base_dir: Path,
    *,
    cwd: str,
    objective: str = "",
    task_files: Optional[List[str]] = None,
    changed_files: Optional[List[str]] = None,
    success: bool = False,
    summary_text: str = "",
    error_text: str = "",
    stopped_reason: str = "",
    wiki_manager: Any = None,
) -> Dict[str, Any]:
    pdir = project_dir(base_dir, cwd)
    summary_path = pdir / "summary.json"
    summary = _read_json(summary_path, {})
    if not summary:
        summary = ensure_project_intelligence(base_dir, cwd=cwd, task_files=task_files, objective=objective)
    root = _canonical_project_root(cwd)
    summary = _refresh_folder_map(summary, root=root, force=True)

    summary["updated_at"] = time.time()
    summary["run_count"] = int(summary.get("run_count", 0)) + 1
    summary["last_objective"] = objective or summary.get("last_objective", "")

    # Hotspots — file paths touched recently
    hotspots: List[Dict[str, Any]] = list(summary.get("hotspots", []) or [])
    for path in (changed_files or []) + (task_files or []):
        existing = next((h for h in hotspots if h.get("path") == path), None)
        if existing:
            existing["count"] = int(existing.get("count", 0)) + 1
            existing["last_touched"] = time.time()
        else:
            hotspots.append({"path": path, "count": 1, "last_touched": time.time()})
    hotspots.sort(key=lambda h: (-int(h.get("count", 0)), -float(h.get("last_touched", 0))))
    summary["hotspots"] = hotspots[:12]

    # Recent outcomes
    outcomes = list(summary.get("recent_outcomes", []) or [])
    outcomes.insert(0, {
        "ts": time.time(),
        "objective": objective[:240],
        "success": bool(success),
        "summary": (summary_text or "")[:240],
        "error": (error_text or "")[:240],
        "stopped_reason": stopped_reason,
        "files": list(changed_files or [])[:8],
    })
    summary["recent_outcomes"] = outcomes[:_OUTCOME_LIMIT]

    # Backlog inference
    backlog = list(summary.get("upgrade_backlog", []) or [])
    error_lc = (error_text or "").lower() + " " + (stopped_reason or "").lower() + " " + (summary_text or "").lower()
    if not success:
        hints: List[str] = []
        if "max_iter" in error_lc or "stagnation" in error_lc:
            hints.append("Repeated stagnation/max-iter exhaustion; refine context envelope or split task.")
        if "no file changes" in error_lc:
            hints.append("Coder produced no file changes — task too vague or missing priority files.")
        if "review" in error_lc and "fail" in error_lc:
            hints.append("Reviewer rejected the change — add explicit constraints/tests.")
        if "lint" in error_lc and "fail" in error_lc:
            hints.append("Lint failures blocked merge — add lint-clean verification.")
        for hint in hints:
            if hint not in backlog:
                backlog.insert(0, hint)
    summary["upgrade_backlog"] = backlog[:_BACKLOG_LIMIT]

    _write_json(summary_path, summary)
    _append_jsonl(pdir / "outcomes.jsonl", outcomes[0])
    _append_jsonl(pdir / "events.jsonl", {
        "ts": time.time(),
        "kind": "coding.run",
        "success": success,
        "objective": objective[:240],
    })

    # Refresh master.md for human eyeballs.
    protection = get_data_protection_governor().protect_text(build_project_master_md(summary), operation="persist_project_intelligence")
    master_md = protection.protected
    write_text_atomically(pdir / "master.md", master_md, encoding="utf-8")
    if wiki_manager is not None:
        try:
            wiki_manager.write_page(f"Projects/{summary.get('project_key', _project_key(cwd))}/master", master_md, source="project_intelligence")
        except Exception as exc:
            logger.debug("[project_intelligence] wiki bridge skipped: %s", exc)
    return summary


def build_project_context_block(summary: Dict[str, Any]) -> str:
    """Render the project-intelligence summary as an XML-tagged context block."""
    if not summary:
        return ""
    lines: List[str] = ["<project_intelligence>"]
    if summary.get("root"):
        lines.append(f"root: {summary['root']}")
    if summary.get("synopsis"):
        lines.append(f"synopsis: {summary['synopsis']}")
    folder_map = summary.get("folder_map") or []
    if folder_map:
        lines.append("folder_map:")
        for entry in folder_map[:20]:
            lines.append(f"  - {entry}")
    hotspots = summary.get("hotspots") or []
    if hotspots:
        lines.append("hotspots (recently touched):")
        for h in hotspots[:8]:
            lines.append(f"  - {h.get('path')} (x{h.get('count', 0)})")
    outcomes = summary.get("recent_outcomes") or []
    if outcomes:
        lines.append("recent outcomes:")
        for o in outcomes[:5]:
            mark = "✓" if o.get("success") else "✗"
            lines.append(f"  {mark} {o.get('objective', '')[:120]}")
    backlog = summary.get("upgrade_backlog") or []
    if backlog:
        lines.append("upgrade_backlog:")
        for item in backlog[:5]:
            lines.append(f"  - {item}")
    lines.append("</project_intelligence>")
    return "\n".join(lines)


def build_project_master_md(summary: Dict[str, Any]) -> str:
    """Human-readable master page for the project. Plain markdown."""
    if not summary:
        return "# Project Intelligence\n\n(empty)\n"
    lines = ["# Project Intelligence", ""]
    if summary.get("root"):
        lines.append(f"**Root:** `{summary['root']}`")
    lines.append(f"**Runs:** {summary.get('run_count', 0)}")
    lines.append("")
    if summary.get("synopsis"):
        lines.append("## Synopsis")
        lines.append(str(summary["synopsis"]))
        lines.append("")
    if summary.get("folder_map"):
        lines.append("## Folder map")
        for entry in summary["folder_map"][:30]:
            lines.append(f"- `{entry}`")
        lines.append("")
    if summary.get("hotspots"):
        lines.append("## Hotspots")
        for h in summary["hotspots"][:10]:
            lines.append(f"- `{h.get('path')}` (x{h.get('count', 0)})")
        lines.append("")
    if summary.get("recent_outcomes"):
        lines.append("## Recent outcomes")
        for o in summary["recent_outcomes"][:6]:
            mark = "✓" if o.get("success") else "✗"
            lines.append(f"- {mark} {o.get('objective', '')[:140]}")
        lines.append("")
    if summary.get("upgrade_backlog"):
        lines.append("## Upgrade backlog")
        for item in summary["upgrade_backlog"][:10]:
            lines.append(f"- {item}")
        lines.append("")
    return "\n".join(lines)
