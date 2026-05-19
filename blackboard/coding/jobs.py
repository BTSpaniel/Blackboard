"""Background job manager — SQLite-persisted, async-scheduled coding jobs.

Slim port of luna/workers/coding/jobs.py. Each job runs inside a git worktree
and is never auto-merged into the base branch.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

import aiosqlite

from blackboard.coding.isolation import WorktreeManager, get_file_claims, is_git_repo, make_diff
from blackboard.coding.models import CodingResult, CodingTask, JobRecord, JobStatus, ReviewVerdict
from blackboard.coding.worker import CodingWorker
from blackboard.coding.reviewer import CodeReviewer
from blackboard.kernel.bus import Bus
from blackboard.kernel.logger import describe_error, get_logger

logger = get_logger("coding.jobs")

_STOP_TIMEOUT_S = 5.0
_WINDOWS_JOB_RUNNER_FLAGS = (
    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    | getattr(subprocess, "DETACHED_PROCESS", 0)
    | getattr(subprocess, "CREATE_NO_WINDOW", 0)
) if os.name == "nt" else 0


def _terminal_sync_matches(status: JobStatus, synced_status: str) -> bool:
    expected = str(getattr(status, "value", status) or "")
    actual = str(synced_status or "")
    return bool(expected) and (actual == expected or actual.startswith(f"{expected}:"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id        TEXT PRIMARY KEY,
    status        TEXT NOT NULL,
    created_at    REAL NOT NULL,
    started_at    REAL,
    completed_at  REAL,
    task_json     TEXT,
    result_json   TEXT,
    review_json   TEXT,
    worktree_path TEXT,
    worktree_branch TEXT,
    runner_pid    INTEGER DEFAULT 0,
    retries       INTEGER DEFAULT 0,
    max_retries   INTEGER DEFAULT 2,
    error         TEXT DEFAULT '',
    progress_note TEXT DEFAULT '',
    synced_terminal_status TEXT DEFAULT ''
);
"""


class BackgroundJobManager:
    """Owns the coding job lifecycle: submit → worktree → execute → review → merge."""

    def __init__(
        self,
        *,
        db_path: Path,
        worker: CodingWorker,
        reviewer: CodeReviewer,
        bus: Optional[Bus] = None,
        worktree_dir: str = ".worktrees",
        base_branch: str = "main",
        max_concurrent: int = 4,
    ) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._worker = worker
        self._reviewer = reviewer
        self._bus = bus
        self._wt_manager = WorktreeManager(worktree_dir=worktree_dir, base_branch=base_branch)
        self._sem = asyncio.Semaphore(max(1, int(max_concurrent)))
        self._tasks: Dict[str, asyncio.Task] = {}
        self._db: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()
        self._submit_lock = asyncio.Lock()
        self._schedule_lock = asyncio.Lock()
        self._monitor_task: Optional[asyncio.Task] = None
        self._known_status: Dict[str, str] = {}
        self._known_progress_note: Dict[str, str] = {}
        self._data_root = self._db_path.parent.parent
        self._repo_root = Path(__file__).resolve().parents[2]
        self._max_active_runs = max(1, int(max_concurrent))

    # ── Lifecycle ────────────────────────────────────────────────

    async def start(self, *, recover_running: bool = True, start_monitor: bool = True) -> None:
        self._db = await aiosqlite.connect(str(self._db_path))
        await self._db.execute(_SCHEMA)
        await self._ensure_column("runner_pid", "INTEGER DEFAULT 0")
        await self._ensure_column("synced_terminal_status", "TEXT DEFAULT ''")
        await self._db.commit()
        if recover_running:
            cursor = await self._db.execute("SELECT job_id, status, runner_pid FROM jobs WHERE status IN (?, ?)", (JobStatus.PENDING.value, JobStatus.RUNNING.value))
            rows = await cursor.fetchall()
            for job_id, status, runner_pid in rows:
                pid = int(runner_pid or 0)
                if pid <= 0 and str(status) == JobStatus.PENDING.value:
                    continue
                if pid > 0 and self._job_process_alive(pid):
                    continue
                await self._update_field(str(job_id), "status", JobStatus.FAILED.value)
                await self._update_field(str(job_id), "runner_pid", 0)
                await self._update_field(str(job_id), "error", f"interrupted: server restart ({status})")
        self._known_status = await self._load_status_map()
        self._known_progress_note = await self._load_progress_note_map()
        if start_monitor:
            self._monitor_task = asyncio.create_task(self._monitor_jobs())
        await self._schedule_pending_jobs()
        logger.info("[jobs] database ready: %s", self._db_path)

    async def stop(self) -> None:
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            try:
                await asyncio.wait_for(self._monitor_task, timeout=_STOP_TIMEOUT_S)
            except (asyncio.CancelledError, Exception):
                pass
            self._monitor_task = None
        pending = [task for task in self._tasks.values() if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            try:
                await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=_STOP_TIMEOUT_S)
            except Exception:
                pass
        self._tasks.clear()
        if self._db is not None:
            try:
                await asyncio.wait_for(self._db.close(), timeout=_STOP_TIMEOUT_S)
            except Exception:
                pass
            self._db = None

    # ── Submission ───────────────────────────────────────────────

    async def submit(self, task: CodingTask, *, max_retries: int = 2) -> JobRecord:
        async with self._submit_lock:
            existing = await self.active_for_card(task.project_id, task.card_id)
            if existing is not None:
                return existing
            conflict = await self._active_file_conflict(task)
            record = JobRecord(task=task, max_retries=int(max_retries))
            if record.task is not None and not str(record.task.task_id or "").strip():
                record.task.task_id = record.job_id
            if conflict is not None:
                record.status = JobStatus.PAUSED
                record.progress_note = self._coordination_note(conflict)
            await self._persist(record)
        if record.status == JobStatus.PAUSED:
            await self._emit("coding:job.paused", {
                "job_id": record.job_id,
                "objective": task.objective,
                "project_id": task.project_id,
                "card_id": task.card_id,
                "cwd": task.cwd,
                "reason": record.progress_note,
                "related_job_id": conflict.get("job_id") if conflict else "",
                "related_card_id": conflict.get("card_id") if conflict else "",
                "conflicts": conflict.get("conflicts") if conflict else [],
                "coordination": conflict or {},
            })
            return record
        await self._emit("coding:job.created", {
            "job_id": record.job_id,
            "objective": task.objective,
            "project_id": task.project_id,
            "card_id": task.card_id,
            "cwd": task.cwd,
            "max_retries": int(max_retries),
        })
        self._known_status[record.job_id] = record.status.value
        await self._schedule_pending_jobs()
        return await self.get(record.job_id) or record

    async def get(self, job_id: str) -> Optional[JobRecord]:
        assert self._db is not None
        cursor = await self._db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,))
        row = await cursor.fetchone()
        cols = [d[0] for d in cursor.description] if cursor.description else []
        if not row:
            return None
        return self._row_to_record(dict(zip(cols, row)))

    async def list_jobs(
        self,
        *,
        status: Optional[JobStatus] = None,
        limit: int = 100,
    ) -> List[JobRecord]:
        assert self._db is not None
        q = "SELECT * FROM jobs"
        params: tuple = ()
        if status:
            q += " WHERE status=?"
            params = (status.value,)
        q += " ORDER BY created_at DESC LIMIT ?"
        params = params + (int(limit),)
        cursor = await self._db.execute(q, params)
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description] if cursor.description else []
        return [self._row_to_record(dict(zip(cols, row))) for row in rows]

    async def active_for_card(self, project_id: str, card_id: str) -> Optional[JobRecord]:
        project = str(project_id or "").strip()
        card = str(card_id or "").strip()
        if not project or not card:
            return None
        active: List[JobRecord] = []
        for status in (JobStatus.PENDING, JobStatus.PAUSED, JobStatus.RUNNING, JobStatus.MERGING):
            active.extend(await self.list_jobs(status=status, limit=500))
        active.sort(key=lambda record: record.created_at, reverse=True)
        for record in active:
            task = record.task
            if task is not None and task.project_id == project and task.card_id == card:
                return record
        return None

    async def _active_file_conflict(self, task: CodingTask) -> Optional[Dict[str, Any]]:
        project = str(task.project_id or "").strip()
        card = str(task.card_id or "").strip()
        cwd = str(Path(task.cwd or ".").resolve())
        target_files = self._coordination_targets(task, cwd)
        orchestration_root = self._orchestration_group(task)
        active = await self._launched_active_jobs()
        for record in active:
            other = record.task
            if other is None:
                continue
            if project and other.project_id and other.project_id != project:
                continue
            if card and other.card_id == card:
                continue
            other_cwd = str(Path(other.cwd or ".").resolve())
            if other_cwd != cwd:
                continue
            other_root = self._orchestration_group(other)
            if orchestration_root and other_root and orchestration_root == other_root:
                return {
                    "job_id": record.job_id,
                    "status": record.status.value,
                    "card_id": other.card_id,
                    "project_id": other.project_id,
                    "cwd": other.cwd,
                    "conflicts": [],
                    "reason": "orchestration_sequence",
                }
            other_files = self._coordination_targets(other, other_cwd)
            conflicts = self._coordination_conflicts(target_files, other_files)
            if conflicts:
                return {
                    "job_id": record.job_id,
                    "status": record.status.value,
                    "card_id": other.card_id,
                    "project_id": other.project_id,
                    "cwd": other.cwd,
                    "conflicts": conflicts[:12],
                    "reason": "file_conflict",
                }
        return None

    @staticmethod
    def _coordination_targets(task: CodingTask, cwd: str) -> set[str]:
        root = Path(cwd).resolve()
        files = [str(item or "").strip() for item in (task.files or []) if str(item or "").strip()]
        if not files:
            return {str(root)}
        targets: set[str] = set()
        for raw in files:
            path = Path(raw)
            target = path.resolve() if path.is_absolute() else (root / path).resolve()
            targets.add(str(target))
        return targets or {str(root)}

    @staticmethod
    def _coordination_conflicts(left: set[str], right: set[str]) -> List[str]:
        conflicts = sorted(left & right)
        if conflicts:
            return conflicts
        for a in left:
            pa = Path(a)
            for b in right:
                pb = Path(b)
                try:
                    pb.relative_to(pa)
                    conflicts.append(str(pb))
                    continue
                except ValueError:
                    pass
                try:
                    pa.relative_to(pb)
                    conflicts.append(str(pa))
                except ValueError:
                    pass
        return sorted(set(conflicts))

    @staticmethod
    def _coordination_note(conflict: Dict[str, Any]) -> str:
        related = str(conflict.get("job_id") or "")
        reason = str(conflict.get("reason") or "").strip().lower()
        if reason == "orchestration_sequence":
            return f"paused until earlier orchestrated job {related} finishes"
        files = conflict.get("conflicts") or []
        suffix = f" overlapping {len(files)} target(s)" if files else ""
        return f"paused for coordination with active job {related}{suffix}"

    @staticmethod
    def _orchestration_group(task: CodingTask) -> str:
        return str(task.root_card_id or task.parent_card_id or "").strip()

    async def cancel(self, job_id: str) -> bool:
        task = self._tasks.get(job_id)
        if task and not task.done():
            task.cancel()
        record = await self.get(job_id)
        if record is not None and int(getattr(record, "runner_pid", 0) or 0) > 0:
            self._terminate_process(int(record.runner_pid))
        await self._update_field(job_id, "status", JobStatus.CANCELLED.value)
        await self._update_field(job_id, "runner_pid", 0)
        self._known_status[job_id] = JobStatus.CANCELLED.value
        await self._schedule_pending_jobs()
        return True

    async def resume(self, job_id: str) -> JobRecord:
        record = await self.get(job_id)
        if record is None:
            raise KeyError(f"job not found: {job_id}")
        if record.status != JobStatus.PAUSED:
            return record
        task = record.task
        if task is None:
            raise ValueError(f"job has no task: {job_id}")
        conflict = await self._active_file_conflict(task)
        if conflict is not None:
            note = self._coordination_note(conflict)
            await self._update_field(job_id, "progress_note", note)
            record.progress_note = note
            await self._emit("coding:job.paused", {
                "job_id": record.job_id,
                "objective": task.objective,
                "project_id": task.project_id,
                "card_id": task.card_id,
                "cwd": task.cwd,
                "reason": note,
                "related_job_id": conflict.get("job_id") if conflict else "",
                "related_card_id": conflict.get("card_id") if conflict else "",
                "conflicts": conflict.get("conflicts") if conflict else [],
                "coordination": conflict or {},
            })
            return record
        await self._update_field(job_id, "status", JobStatus.PENDING.value)
        await self._update_field(job_id, "progress_note", "")
        resumed = await self.get(job_id) or record
        await self._emit("coding:job.created", {
            "job_id": resumed.job_id,
            "objective": task.objective,
            "project_id": task.project_id,
            "card_id": task.card_id,
            "cwd": task.cwd,
            "max_retries": int(resumed.max_retries),
            "resumed": True,
        })
        self._known_status[resumed.job_id] = resumed.status.value
        await self._schedule_pending_jobs()
        return await self.get(resumed.job_id) or resumed

    def _card_snapshot(self, project_id: str, card_id: str) -> Dict[str, Any]:
        project = str(project_id or "").strip()
        card = str(card_id or "").strip()
        if not project or not card:
            return {}
        path = self._data_root / "projects" / project / "board.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        for entry in list(data.get("cards") or []):
            if str((entry or {}).get("id") or "").strip() == card:
                return dict(entry or {})
        return {}

    @staticmethod
    def _priority_rank(card: Dict[str, Any]) -> int:
        metadata = dict((card or {}).get("metadata") or {})
        tags = {str(item or "").strip().lower() for item in list((card or {}).get("tags") or [])}
        value = str(metadata.get("priority") or metadata.get("priority_label") or "").strip().lower()
        if value in {"critical", "urgent", "p0", "highest"} or "priority:critical" in tags or "priority:urgent" in tags:
            return 4
        if value in {"high", "p1"} or "priority:high" in tags:
            return 3
        if value in {"low", "p3"} or "priority:low" in tags:
            return 1
        return 2

    @staticmethod
    def _estimate_rank(card: Dict[str, Any]) -> float:
        metadata = dict((card or {}).get("metadata") or {})
        value = metadata.get("estimate_points", metadata.get("estimate"))
        try:
            parsed = float(value)
            if parsed >= 0:
                return parsed
        except Exception:
            pass
        label = str(value or metadata.get("estimate_size") or metadata.get("size") or "").strip().lower()
        lookup = {
            "xs": 1.0,
            "small": 2.0,
            "s": 2.0,
            "medium": 3.0,
            "m": 3.0,
            "large": 5.0,
            "l": 5.0,
            "xl": 8.0,
            "huge": 13.0,
        }
        return lookup.get(label, 3.0)

    def _pending_sort_key(self, record: JobRecord) -> tuple[float, float, float]:
        task = record.task
        card = self._card_snapshot(task.project_id if task is not None else "", task.card_id if task is not None else "")
        priority = self._priority_rank(card)
        estimate = self._estimate_rank(card)
        updated_at = float((card or {}).get("updated_at") or (card or {}).get("created_at") or 0.0)
        return (-float(priority), float(estimate), -updated_at or float(record.created_at or 0.0))

    async def _launched_active_jobs(self) -> List[JobRecord]:
        active: List[JobRecord] = []
        for status in (JobStatus.PENDING, JobStatus.RUNNING, JobStatus.MERGING):
            active.extend(await self.list_jobs(status=status, limit=500))
        launched: List[JobRecord] = []
        for record in active:
            if record.status == JobStatus.PENDING and int(record.runner_pid or 0) == 0:
                continue
            launched.append(record)
        launched.sort(key=lambda record: (float(record.started_at or 0.0), float(record.created_at or 0.0)), reverse=True)
        return launched

    async def _schedule_pending_jobs(self) -> int:
        async with self._schedule_lock:
            launched = await self._launched_active_jobs()
            available = max(0, int(self._max_active_runs) - len(launched))
            if available <= 0:
                return 0
            pending = [record for record in await self.list_jobs(status=JobStatus.PENDING, limit=500) if int(record.runner_pid or 0) == 0]
            pending.sort(key=self._pending_sort_key)
            launched_count = 0
            for record in pending[:available]:
                await self._launch_job(record.job_id)
                launched_count += 1
            return launched_count

    async def _resume_paused_jobs(self) -> int:
        paused = await self.list_jobs(status=JobStatus.PAUSED, limit=500)
        if not paused:
            return 0
        paused.sort(key=self._pending_sort_key)
        resumed_count = 0
        for record in paused:
            task = record.task
            if task is None:
                continue
            conflict = await self._active_file_conflict(task)
            if conflict is not None:
                note = self._coordination_note(conflict)
                if note != str(record.progress_note or ""):
                    await self._update_field(record.job_id, "progress_note", note)
                    self._known_progress_note[record.job_id] = note
                continue
            await self._update_field(record.job_id, "status", JobStatus.PENDING.value)
            await self._update_field(record.job_id, "progress_note", "")
            resumed = await self.get(record.job_id) or record
            await self._emit("coding:job.created", {
                "job_id": resumed.job_id,
                "objective": task.objective,
                "project_id": task.project_id,
                "card_id": task.card_id,
                "cwd": task.cwd,
                "max_retries": int(resumed.max_retries),
                "resumed": True,
                "auto_resumed": True,
            })
            self._known_status[resumed.job_id] = resumed.status.value
            self._known_progress_note[resumed.job_id] = ""
            resumed_count += 1
        return resumed_count

    async def merge_candidates_for(self, record: JobRecord, *, limit: int = 4) -> List[Dict[str, Any]]:
        if record.task is None:
            return []
        candidates: List[Dict[str, Any]] = []
        recent: List[JobRecord] = []
        for status in (JobStatus.PENDING, JobStatus.PAUSED, JobStatus.RUNNING, JobStatus.SUCCESS):
            recent.extend(await self.list_jobs(status=status, limit=100))
        seen: set[str] = set()
        for other in recent:
            if other.job_id == record.job_id or other.job_id in seen or other.task is None:
                continue
            seen.add(other.job_id)
            score, reasons = self._merge_score(record, other)
            if score < 0.35:
                continue
            candidates.append({
                "job_id": other.job_id,
                "card_id": other.task.card_id,
                "project_id": other.task.project_id,
                "status": other.status.value,
                "score": round(score, 3),
                "reasons": reasons[:5],
                "objective": other.task.objective[:180],
                "files": list(other.task.files or [])[:8],
            })
        candidates.sort(key=lambda item: (-float(item["score"]), str(item["job_id"])))
        return candidates[: max(1, int(limit or 4))]

    @classmethod
    def _merge_score(cls, left: JobRecord, right: JobRecord) -> tuple[float, List[str]]:
        if left.task is None or right.task is None:
            return 0.0, []
        if left.task.project_id and right.task.project_id and left.task.project_id != right.task.project_id:
            return 0.0, []
        if str(Path(left.task.cwd or ".").resolve()) != str(Path(right.task.cwd or ".").resolve()):
            return 0.0, []
        reasons: List[str] = []
        score = 0.0
        left_files = set(left.task.files or [])
        right_files = set(right.task.files or [])
        shared_files = sorted(left_files & right_files)
        if shared_files:
            score += min(0.45, 0.18 + 0.09 * len(shared_files))
            reasons.append(f"shared files: {', '.join(shared_files[:4])}")
        left_changed = cls._changed_files(left)
        right_changed = cls._changed_files(right)
        shared_changed = sorted(left_changed & right_changed)
        if shared_changed:
            score += min(0.35, 0.2 + 0.05 * len(shared_changed))
            reasons.append(f"overlapping changes: {', '.join(shared_changed[:4])}")
        token_overlap = cls._token_overlap(left.task.objective, right.task.objective)
        if token_overlap:
            score += min(0.35, token_overlap * 0.5)
            reasons.append(f"similar objectives: {round(token_overlap, 2)}")
        if left.task.card_id and right.task.card_id and left.task.card_id != right.task.card_id:
            score += 0.05
        if not reasons and score < 0.35:
            return 0.0, []
        return min(score, 1.0), reasons

    @staticmethod
    def _changed_files(record: JobRecord) -> set[str]:
        result = record.result
        if result is None:
            return set()
        return {patch.file for patch in result.patches} | {new_file.file for new_file in result.new_files}

    @staticmethod
    def _token_overlap(left: str, right: str) -> float:
        stop = {"the", "and", "for", "with", "this", "that", "from", "into", "card", "job", "task", "build", "create", "update", "make", "add"}
        a = {token for token in re.findall(r"[a-z0-9_]{3,}", str(left or "").lower()) if token not in stop}
        b = {token for token in re.findall(r"[a-z0-9_]{3,}", str(right or "").lower()) if token not in stop}
        if not a or not b:
            return 0.0
        return len(a & b) / max(1, min(len(a), len(b)))

    async def approve_merge(
        self,
        job_id: str,
        *,
        confirm: bool = False,
        message: str = "",
    ) -> Dict[str, Any]:
        record = await self.get(job_id)
        if record is None:
            return {"success": False, "message": f"job not found: {job_id}"}
        if record.status not in (JobStatus.SUCCESS,):
            return {"success": False, "message": f"job not ready to merge (status={record.status.value})"}
        repo_root = record.task.cwd if record.task else ""
        branch = record.worktree_branch or ""
        if not branch or not is_git_repo(repo_root):
            return {"success": False, "message": "no worktree/branch to merge"}
        if not confirm:
            # Return diff for preview.
            return {
                "success": True,
                "preview": True,
                "branch": branch,
                "review": record.review.to_dict() if record.review else None,
            }
        ok, info = self._wt_manager.merge_into_base(repo_root, branch)
        if not ok:
            return {"success": False, "message": info}
        await self._update_field(job_id, "status", JobStatus.MERGED.value)
        # Clean up worktree on successful merge.
        try:
            self._wt_manager.remove(record.job_id, repo_root)
        except Exception:
            pass
        await self._emit("coding:job.merged", {"job_id": job_id, "branch": branch, "info": info[:200]})
        return {"success": True, "branch": branch, "info": info}

    @staticmethod
    def _diff_from_result(result: Optional[CodingResult]) -> str:
        if result is None:
            return ""
        before: Dict[str, str] = {}
        after: Dict[str, str] = {}
        for patch in list(result.patches or []):
            path = str(getattr(patch, "file", "") or "").strip()
            if not path:
                continue
            before[path] = str(getattr(patch, "old_string", "") or "")
            after[path] = str(getattr(patch, "new_string", "") or "")
        for new_file in list(result.new_files or []):
            path = str(getattr(new_file, "file", "") or "").strip()
            if not path:
                continue
            before.setdefault(path, "")
            after[path] = str(getattr(new_file, "content", "") or "")
        return make_diff(before, after)

    async def _run_job(self, job_id: str) -> None:
        async with self._sem:
            record = await self.get(job_id)
            if record is None or record.status == JobStatus.CANCELLED:
                return
            task = record.task
            if task is None:
                await self._fail(job_id, f"job has no task: {job_id}")
                return

            repo_root = str(Path(task.cwd or ".").resolve())
            exec_cwd = repo_root
            worktree_path = str(record.worktree_path or "")
            worktree_branch = str(record.worktree_branch or "")

            try:
                started_at = time.time()
                await self._update_field(job_id, "status", JobStatus.RUNNING.value)
                await self._update_field(job_id, "started_at", started_at)
                await self._update_field(job_id, "progress_note", "starting job")

                if is_git_repo(repo_root):
                    ok, wt_path, branch = self._wt_manager.create(job_id, repo_root)
                    if ok:
                        worktree_path = str(wt_path or "")
                        worktree_branch = str(branch or "")
                        exec_cwd = worktree_path or repo_root
                        await self._update_field(job_id, "worktree_path", worktree_path)
                        await self._update_field(job_id, "worktree_branch", worktree_branch)
                        await self._update_field(job_id, "progress_note", f"executing in worktree {worktree_path}"[:1000])
                    else:
                        exec_cwd = repo_root
                        await self._update_field(job_id, "progress_note", f"worktree unavailable; executing in place ({branch or wt_path or 'git fallback'})"[:1000])
                else:
                    await self._update_field(job_id, "progress_note", "executing in place (not a git repository)")

                exec_payload = task.to_dict()
                exec_payload["cwd"] = exec_cwd
                exec_task = CodingTask.from_dict(exec_payload)
                result = await self._worker.execute(exec_task)

                if result.success and worktree_path:
                    ok, info = self._wt_manager.commit_changes(
                        job_id,
                        repo_root,
                        worktree_path=worktree_path,
                        message=f"Blackboard coding job {job_id}: {(task.objective or '').strip()[:72]}",
                    )
                    if not ok:
                        result.success = False
                        result.error = str(info or "could not commit worktree changes")
                        result.warnings = [*(result.warnings or []), result.error]
                        await self._update_field(job_id, "progress_note", f"commit failed: {result.error}"[:1000])
                    else:
                        await self._update_field(job_id, "progress_note", f"committed changes to {worktree_branch or 'job branch'}"[:1000])

                review = None
                if result.success and self._reviewer is not None:
                    await self._update_field(job_id, "progress_note", "reviewing changes")
                    try:
                        review = await self._reviewer.review(
                            task,
                            result,
                            cwd=exec_cwd,
                            diff=self._diff_from_result(result),
                        )
                    except Exception as exc:
                        logger.debug("[jobs] review failed for %s: %s", job_id, exc)

                final_status = JobStatus.SUCCESS if result.success else JobStatus.FAILED
                finished = await self.get(job_id) or record
                finished.status = final_status
                finished.started_at = finished.started_at or started_at
                finished.completed_at = time.time()
                finished.task = finished.task or task
                finished.result = result
                finished.review = review
                finished.worktree_path = worktree_path or finished.worktree_path
                finished.worktree_branch = worktree_branch or finished.worktree_branch
                finished.error = str(result.error or "")
                finished.progress_note = "complete" if final_status == JobStatus.SUCCESS else str(result.error or "job failed")[:1000]
                finished.runner_pid = 0
                await self._persist(finished)
            except asyncio.CancelledError:
                cancelled = await self.get(job_id) or record
                cancelled.status = JobStatus.CANCELLED
                cancelled.completed_at = time.time()
                cancelled.task = cancelled.task or task
                cancelled.error = ""
                cancelled.progress_note = "cancelled"
                cancelled.runner_pid = 0
                await self._persist(cancelled)
                raise
            except Exception as exc:
                await self._fail(job_id, describe_error(exc))
            finally:
                await self._update_field(job_id, "runner_pid", 0)

    # ── Job runner ───────────────────────────────────────────────

    async def _launch_job(self, job_id: str) -> None:
        await self._update_field(job_id, "runner_pid", -1)
        if self._worker is None or self._reviewer is None:
            loop = asyncio.get_event_loop()
            self._tasks[job_id] = loop.create_task(self._run_job(job_id))
            return
        pid = self._spawn_detached_job(job_id)
        await self._update_field(job_id, "runner_pid", int(pid))

    def _job_runner_log_path(self, job_id: str) -> Path:
        root = self._data_root / "coding" / "runner_logs"
        root.mkdir(parents=True, exist_ok=True)
        safe_job_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(job_id or "job"))[:120] or "job"
        return root / f"{safe_job_id}.log"

    def _job_runner_log_tail(self, job_id: str, *, max_chars: int = 600) -> str:
        path = self._job_runner_log_path(job_id)
        try:
            if not path.exists():
                return ""
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return ""
        return text[-max(80, int(max_chars or 600)):].strip()

    def _spawn_detached_job(self, job_id: str) -> int:
        cmd = [
            sys.executable,
            "-m",
            "blackboard.coding.job_runner",
            "--job-id",
            str(job_id),
            "--db-path",
            str(self._db_path),
            "--data-root",
            str(self._data_root),
            "--worktree-dir",
            str(self._wt_manager._worktree_dir),
            "--base-branch",
            str(self._wt_manager._base_branch),
        ]
        log_path = self._job_runner_log_path(job_id)
        kwargs: Dict[str, Any] = {
            "cwd": str(self._repo_root),
            "stdin": subprocess.DEVNULL,
            "close_fds": True,
        }
        if os.name == "nt":
            kwargs["creationflags"] = _WINDOWS_JOB_RUNNER_FLAGS
        else:
            kwargs["start_new_session"] = True
        logger.info(
            "[spawn][job_runner] job_id=%s cwd=%s cmd=%s creationflags=%s start_new_session=%s",
            job_id,
            kwargs.get("cwd") or "",
            cmd,
            kwargs.get("creationflags") or 0,
            bool(kwargs.get("start_new_session") or False),
        )
        with log_path.open("ab") as stream:
            kwargs["stdout"] = stream
            kwargs["stderr"] = stream
            proc = subprocess.Popen(cmd, **kwargs)
        return int(proc.pid)

    @staticmethod
    def _job_process_alive(pid: int) -> bool:
        if int(pid or 0) <= 0:
            return False
        try:
            os.kill(int(pid), 0)
        except OSError:
            return False
        except Exception:
            return False
        return True

    @staticmethod
    def _terminate_process(pid: int) -> None:
        if int(pid or 0) <= 0:
            return
        try:
            os.kill(int(pid), signal.SIGTERM)
        except Exception:
            pass

    async def _monitor_jobs(self) -> None:
        while True:
            await asyncio.sleep(0.75)
            try:
                records = await self.list_jobs(limit=500)
            except Exception:
                continue
            for record in records:
                if record.status in (JobStatus.PENDING, JobStatus.RUNNING) and int(record.runner_pid or 0) > 0 and not self._job_process_alive(int(record.runner_pid)):
                    detail = self._job_runner_log_tail(record.job_id)
                    error_text = f"detached runner exited unexpectedly while {record.status.value}"
                    if detail:
                        error_text = f"{error_text}: {detail}"[:1000]
                    await self._update_field(record.job_id, "status", JobStatus.FAILED.value)
                    await self._update_field(record.job_id, "runner_pid", 0)
                    await self._update_field(record.job_id, "error", error_text)
                    record = await self.get(record.job_id) or record
                previous = self._known_status.get(record.job_id)
                current = record.status.value
                if previous != current and previous is not None:
                    await self._emit_status_transition(record, previous)
                self._known_status[record.job_id] = current
                previous_note = self._known_progress_note.get(record.job_id, "")
                current_note = str(record.progress_note or "")
                if previous_note != current_note and previous_note is not None:
                    await self._emit_progress_note_transition(record, previous_note)
                self._known_progress_note[record.job_id] = current_note
                if record.status in (JobStatus.SUCCESS, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.MERGED):
                    await self._schedule_pending_jobs()
            await self._resume_paused_jobs()
            await self._schedule_pending_jobs()

    async def _emit_status_transition(self, record: JobRecord, previous: str) -> None:
        task = record.task
        if task is None:
            return
        result = record.result
        review = record.review
        if record.status == JobStatus.RUNNING:
            await self._emit("coding:job.started", {
                "job_id": record.job_id,
                "objective": task.objective,
                "project_id": task.project_id,
                "card_id": task.card_id,
                "cwd": task.cwd,
            })
            return
        if record.status == JobStatus.FAILED and result is None:
            await self._emit("coding:job.failed", {
                "job_id": record.job_id,
                "error": str(record.error or "job failed")[:200],
                "project_id": task.project_id,
                "card_id": task.card_id,
                "cwd": task.cwd,
            })
            return
        if record.status not in (JobStatus.SUCCESS, JobStatus.FAILED):
            return
        merge_candidates = await self.merge_candidates_for(record, limit=4)
        await self._emit("coding:job.completed", {
            "job_id": record.job_id,
            "status": record.status.value,
            "success": bool(getattr(result, "success", False)),
            "summary": str(getattr(result, "summary", "") or "")[:200],
            "error": str(getattr(result, "error", "") or record.error or ""),
            "branch": record.worktree_branch or "",
            "project_id": task.project_id,
            "card_id": task.card_id,
            "cwd": task.cwd,
            "execution_cwd": record.worktree_path or task.cwd,
            "worktree_path": record.worktree_path or "",
            "files_changed": ([p.file for p in (result.patches or [])] + [f.file for f in (result.new_files or [])]) if result is not None else [],
            "patch_count": len(result.patches) if result is not None else 0,
            "new_file_count": len(result.new_files) if result is not None else 0,
            "merge_candidates": merge_candidates,
            "review_passed": bool(review.passed) if review is not None else None,
            "review": review.to_dict() if review is not None else None,
        })

    async def _emit_progress_note_transition(self, record: JobRecord, previous_note: str) -> None:
        task = record.task
        if task is None or record.status != JobStatus.RUNNING:
            return
        current_note = str(record.progress_note or "")
        previous = str(previous_note or "")
        if current_note == previous:
            return
        if not current_note.lower().startswith("reviewing"):
            return
        await self._emit("coding:job.reviewing", {
            "job_id": record.job_id,
            "objective": task.objective,
            "project_id": task.project_id,
            "card_id": task.card_id,
            "cwd": task.cwd,
            "execution_cwd": record.worktree_path or task.cwd,
            "note": current_note[:200],
        })

    async def replay_unsynced_terminal_jobs(self, *, limit: int = 200) -> int:
        records = await self.list_jobs(limit=max(1, int(limit or 200)))
        replayed = 0
        for record in records:
            if record.status not in (JobStatus.SUCCESS, JobStatus.FAILED):
                continue
            if _terminal_sync_matches(record.status, str(record.synced_terminal_status or "")):
                continue
            if record.task is None:
                await self.mark_terminal_synced(record.job_id, record.status.value, detail="missing_task")
                continue
            await self._emit_status_transition(record, "")
            replayed += 1
        return replayed

    async def mark_terminal_synced(self, job_id: str, status: str, detail: str = "") -> None:
        marker = str(status or "")
        extra = str(detail or "").strip()
        if marker and extra:
            marker = f"{marker}:{extra}"
        await self._update_field(job_id, "synced_terminal_status", marker)

    # ── Persistence helpers ─────────────────────────────────────

    async def _fail(self, job_id: str, message: str) -> None:
        await self._update_field(job_id, "status", JobStatus.FAILED.value)
        await self._update_field(job_id, "error", message)
        await self._update_field(job_id, "completed_at", time.time())
        await self._update_field(job_id, "runner_pid", 0)
        self._known_status[job_id] = JobStatus.FAILED.value
        record = await self.get(job_id)
        task = record.task if record is not None else None
        await self._emit("coding:job.failed", {
            "job_id": job_id,
            "error": message[:200],
            "project_id": task.project_id if task is not None else "",
            "card_id": task.card_id if task is not None else "",
            "cwd": task.cwd if task is not None else "",
        })

    async def _emit(self, topic: str, payload: Dict[str, Any]) -> None:
        if self._bus is None:
            return
        try:
            await self._bus.emit(topic, payload)
        except Exception:
            pass

    async def _persist(self, record: JobRecord) -> None:
        assert self._db is not None
        d = record.to_dict()
        async with self._lock:
            await self._db.execute(
                "INSERT OR REPLACE INTO jobs (job_id,status,created_at,started_at,completed_at,task_json,result_json,review_json,worktree_path,worktree_branch,runner_pid,retries,max_retries,error,progress_note,synced_terminal_status) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    d["job_id"], d["status"], d["created_at"], d["started_at"], d["completed_at"],
                    json.dumps(d["task"]) if d["task"] else "",
                    json.dumps(d["result"]) if d["result"] else "",
                    json.dumps(d["review"]) if d["review"] else "",
                    d["worktree_path"] or "", d["worktree_branch"] or "",
                    int(d.get("runner_pid") or 0),
                    d["retries"], d["max_retries"],
                    d["error"] or "", d["progress_note"] or "", d.get("synced_terminal_status") or "",
                ),
            )
            await self._db.commit()

    async def _update_field(self, job_id: str, field: str, value: Any) -> None:
        if self._db is None:
            return
        async with self._lock:
            await self._db.execute(f"UPDATE jobs SET {field}=? WHERE job_id=?", (value, job_id))
            await self._db.commit()

    async def _ensure_column(self, name: str, ddl: str) -> None:
        assert self._db is not None
        cursor = await self._db.execute("PRAGMA table_info(jobs)")
        rows = await cursor.fetchall()
        existing = {str(row[1]) for row in rows if len(row) > 1}
        if name in existing:
            return
        await self._db.execute(f"ALTER TABLE jobs ADD COLUMN {name} {ddl}")

    async def _load_status_map(self) -> Dict[str, str]:
        assert self._db is not None
        cursor = await self._db.execute("SELECT job_id, status FROM jobs")
        rows = await cursor.fetchall()
        return {str(job_id): str(status) for job_id, status in rows}

    async def _load_progress_note_map(self) -> Dict[str, str]:
        assert self._db is not None
        cursor = await self._db.execute("SELECT job_id, progress_note FROM jobs")
        rows = await cursor.fetchall()
        return {str(job_id): str(progress_note or "") for job_id, progress_note in rows}

    @staticmethod
    def _row_to_record(row: Dict[str, Any]) -> JobRecord:
        task = None
        if row.get("task_json"):
            try:
                payload = json.loads(row["task_json"])
                if isinstance(payload, dict) and payload.get("objective"):
                    task = CodingTask.from_dict(payload)
            except Exception:
                task = None
        result = None
        if row.get("result_json"):
            data = json.loads(row["result_json"])
            from blackboard.coding.models import FilePatch, NewFile
            result = CodingResult(
                success=bool(data.get("success")),
                plan=list(data.get("plan") or []),
                patches=[FilePatch(**p) for p in (data.get("patches") or [])],
                new_files=[NewFile(**n) for n in (data.get("new_files") or [])],
                summary=str(data.get("summary") or ""),
                test_hint=str(data.get("test_hint") or ""),
                lint_hint=str(data.get("lint_hint") or ""),
                warnings=list(data.get("warnings") or []),
                model_used=str(data.get("model_used") or ""),
                tokens_used=int(data.get("tokens_used") or 0),
                elapsed_s=float(data.get("elapsed_s") or 0.0),
                error=str(data.get("error") or ""),
            )
        review = None
        if row.get("review_json"):
            data = json.loads(row["review_json"])
            review = ReviewVerdict(**{k: v for k, v in data.items() if k in ReviewVerdict.__dataclass_fields__})
        return JobRecord(
            job_id=row["job_id"],
            status=JobStatus(row["status"]),
            created_at=row["created_at"],
            started_at=row.get("started_at"),
            completed_at=row.get("completed_at"),
            task=task,
            result=result,
            review=review,
            worktree_path=row.get("worktree_path") or None,
            worktree_branch=row.get("worktree_branch") or None,
            runner_pid=int(row.get("runner_pid") or 0),
            retries=int(row.get("retries") or 0),
            max_retries=int(row.get("max_retries") or 2),
            error=row.get("error") or "",
            progress_note=row.get("progress_note") or "",
            synced_terminal_status=row.get("synced_terminal_status") or "",
        )
