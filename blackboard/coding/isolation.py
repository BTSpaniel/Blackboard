"""Git worktree isolation + file-claim registry for sync jobs.

Slim port of luna/workers/coding/isolation.py — keeps git worktrees,
file claims, merge coordinator. Drops sparse-checkout subtleties for v1
(can be re-added if needed).
"""
from __future__ import annotations

import asyncio
import difflib
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from blackboard.kernel.logger import get_logger

logger = get_logger("coding.isolation")
_WINDOWS_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _git(args: List[str], cwd: str, timeout: int = 30) -> Tuple[int, str, str]:
    kwargs: Dict[str, object] = {}
    if _WINDOWS_CREATE_NO_WINDOW:
        kwargs["creationflags"] = _WINDOWS_CREATE_NO_WINDOW
    logger.info(
        "[spawn][isolation_git] cwd=%s cmd=%s timeout=%s creationflags=%s",
        cwd,
        ["git", *args],
        timeout,
        kwargs.get("creationflags") or 0,
    )
    try:
        r = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout, **kwargs
        )
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return 1, "", f"git command timed out: {' '.join(args)}"
    except FileNotFoundError:
        return 1, "", "git not installed"
    except Exception as exc:
        return 1, "", str(exc)


def is_git_repo(path: str) -> bool:
    code, _, _ = _git(["rev-parse", "--is-inside-work-tree"], cwd=path)
    return code == 0


class WorktreeManager:
    """Creates git worktrees for isolated job execution.

    Each job: branch ``blackboard/job/{job_id}``, path ``{worktree_dir}/bb_{job_id}``.
    """

    def __init__(self, worktree_dir: str = ".worktrees", base_branch: str = "main") -> None:
        self._worktree_dir = worktree_dir
        self._base_branch = base_branch
        self._lock = threading.Lock()

    def _resolve_base(self, repo_root: str) -> str:
        for candidate in (self._base_branch, "main", "master", "HEAD"):
            code, _, _ = _git(["rev-parse", "--verify", candidate], cwd=repo_root)
            if code == 0:
                return candidate
        return "HEAD"

    def create(self, job_id: str, repo_root: str) -> Tuple[bool, str, str]:
        if not is_git_repo(repo_root):
            return False, "", "not a git repository"
        branch = f"blackboard/job/{job_id}"
        wt_path = str(Path(repo_root) / self._worktree_dir / f"bb_{job_id}")
        with self._lock:
            os.makedirs(str(Path(wt_path).parent), exist_ok=True)
            base = self._resolve_base(repo_root)
            code, out, err = _git(["worktree", "add", "-b", branch, wt_path, base], cwd=repo_root, timeout=60)
            if code != 0:
                # Attempt cleanup + retry once.
                err_text = (err or out).lower()
                if "already exists" in err_text or Path(wt_path).exists():
                    _git(["worktree", "remove", "--force", wt_path], cwd=repo_root)
                    _git(["branch", "-D", branch], cwd=repo_root)
                    if Path(wt_path).exists():
                        try:
                            shutil.rmtree(wt_path)
                        except Exception:
                            pass
                    code, out, err = _git(["worktree", "add", "-b", branch, wt_path, base], cwd=repo_root, timeout=60)
                if code != 0:
                    logger.error("[isolation] worktree create failed: %s", err or out)
                    return False, "", err or out
        logger.info("[isolation] worktree created: %s (branch=%s)", wt_path, branch)
        return True, wt_path, branch

    def remove(self, job_id: str, repo_root: str, *, delete_branch: bool = True) -> bool:
        wt_path = str(Path(repo_root) / self._worktree_dir / f"bb_{job_id}")
        branch = f"blackboard/job/{job_id}"
        with self._lock:
            _git(["worktree", "remove", "--force", wt_path], cwd=repo_root)
            if Path(wt_path).exists():
                try:
                    shutil.rmtree(wt_path)
                except Exception:
                    pass
            if delete_branch:
                _git(["branch", "-D", branch], cwd=repo_root)
        return True

    def commit_changes(
        self,
        job_id: str,
        repo_root: str,
        *,
        message: str = "",
        worktree_path: str = "",
    ) -> Tuple[bool, str]:
        wt_path = worktree_path or str(Path(repo_root) / self._worktree_dir / f"bb_{job_id}")
        if not Path(wt_path).exists():
            return False, f"worktree not found: {wt_path}"
        code, _, err = _git(["add", "-A"], cwd=wt_path)
        if code != 0:
            return False, err
        code, out, _ = _git(["status", "--short"], cwd=wt_path)
        if not out.strip():
            return False, "no changes to commit"
        commit_msg = message or f"Blackboard coding job {job_id}"
        code, _, err = _git(
            ["-c", "user.name=Blackboard", "-c", "user.email=blackboard@local",
             "commit", "-m", commit_msg],
            cwd=wt_path, timeout=60,
        )
        if code != 0:
            return False, err
        code, sha, _ = _git(["rev-parse", "HEAD"], cwd=wt_path)
        return True, sha if code == 0 else commit_msg

    def merge_into_base(self, repo_root: str, branch: str, *, no_ff: bool = True) -> Tuple[bool, str]:
        args = ["merge", "--no-ff", branch] if no_ff else ["merge", branch]
        code, out, err = _git(args, cwd=repo_root, timeout=120)
        if code != 0:
            return False, err or out
        return True, out


class FileClaimRegistry:
    """Prevents two sync jobs from editing the same file concurrently."""

    def __init__(self) -> None:
        self._claims: Dict[str, str] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _normalize(path: str) -> str:
        return str(Path(path).resolve())

    def claim(self, files: List[str], job_id: str) -> List[str]:
        conflicts: List[str] = []
        with self._lock:
            for raw in files:
                key = self._normalize(raw)
                owner = self._claims.get(key)
                if owner and owner != job_id:
                    conflicts.append(raw)
            if conflicts:
                return conflicts
            for raw in files:
                self._claims[self._normalize(raw)] = job_id
        return []

    def release(self, job_id: str) -> None:
        with self._lock:
            for key in [k for k, v in self._claims.items() if v == job_id]:
                self._claims.pop(key, None)

    def status(self) -> Dict[str, str]:
        with self._lock:
            return dict(self._claims)


_claim_registry: Optional[FileClaimRegistry] = None


def get_file_claims() -> FileClaimRegistry:
    global _claim_registry
    if _claim_registry is None:
        _claim_registry = FileClaimRegistry()
    return _claim_registry


def make_diff(before: Dict[str, str], after: Dict[str, str]) -> str:
    """Produce a unified diff string from two {path: content} snapshots."""
    out_lines: List[str] = []
    keys = sorted(set(before) | set(after))
    for path in keys:
        old = before.get(path, "")
        new = after.get(path, "")
        if old == new:
            continue
        diff = difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
        out_lines.extend(diff)
    return "".join(out_lines)
