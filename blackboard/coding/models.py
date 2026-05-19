"""Coding subsystem data models. Slim port of luna/workers/coding/models.py."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class JobStatus(str, Enum):
    PENDING = "pending"
    PAUSED = "paused"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"
    MERGING = "merging"
    MERGED = "merged"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass
class CodingTask:
    objective: str
    files: List[str] = field(default_factory=list)
    constraints: List[str] = field(default_factory=list)
    verification: List[str] = field(default_factory=list)
    risk_level: RiskLevel = RiskLevel.MEDIUM
    cwd: str = "."
    base_branch: str = "main"
    context: str = ""
    agents_md_path: Optional[str] = None
    project_id: str = ""
    card_id: str = ""
    task_id: str = ""
    parent_card_id: str = ""
    root_card_id: str = ""
    orchestration_stage: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "objective": self.objective,
            "files": self.files,
            "constraints": self.constraints,
            "verification": self.verification,
            "risk_level": self.risk_level.value,
            "cwd": self.cwd,
            "base_branch": self.base_branch,
            "context": self.context,
            "agents_md_path": self.agents_md_path,
            "project_id": self.project_id,
            "card_id": self.card_id,
            "task_id": self.task_id,
            "parent_card_id": self.parent_card_id,
            "root_card_id": self.root_card_id,
            "orchestration_stage": self.orchestration_stage,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CodingTask":
        d = dict(d or {})
        if "risk_level" in d:
            try:
                d["risk_level"] = RiskLevel(d["risk_level"])
            except Exception:
                d["risk_level"] = RiskLevel.MEDIUM
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class FilePatch:
    file: str
    old_string: str
    new_string: str
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "file": self.file,
            "old_string": self.old_string,
            "new_string": self.new_string,
            "description": self.description,
        }


@dataclass
class NewFile:
    file: str
    content: str
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"file": self.file, "content": self.content, "description": self.description}


@dataclass
class CodingResult:
    success: bool = False
    plan: List[str] = field(default_factory=list)
    patches: List[FilePatch] = field(default_factory=list)
    new_files: List[NewFile] = field(default_factory=list)
    summary: str = ""
    test_hint: str = ""
    lint_hint: str = ""
    warnings: List[str] = field(default_factory=list)
    model_used: str = ""
    tokens_used: int = 0
    elapsed_s: float = 0.0
    error: str = ""

    @property
    def has_file_changes(self) -> bool:
        return bool(self.patches or self.new_files)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "plan": self.plan,
            "patches": [p.to_dict() for p in self.patches],
            "new_files": [f.to_dict() for f in self.new_files],
            "summary": self.summary,
            "test_hint": self.test_hint,
            "lint_hint": self.lint_hint,
            "warnings": self.warnings,
            "model_used": self.model_used,
            "tokens_used": self.tokens_used,
            "elapsed_s": self.elapsed_s,
            "error": self.error,
        }


@dataclass
class ReviewVerdict:
    passed: bool = False
    lint_clean: bool = False
    compile_passed: bool = True
    tests_passed: bool = False
    runtime_passed: bool = True
    lint_violations: int = 0
    compile_failures: int = 0
    test_failures: int = 0
    runtime_failures: int = 0
    diff_summary: str = ""
    suggestions: List[str] = field(default_factory=list)
    raw_lint: str = ""
    raw_compile: str = ""
    raw_tests: str = ""
    raw_runtime: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "lint_clean": self.lint_clean,
            "compile_passed": self.compile_passed,
            "tests_passed": self.tests_passed,
            "runtime_passed": self.runtime_passed,
            "lint_violations": self.lint_violations,
            "compile_failures": self.compile_failures,
            "test_failures": self.test_failures,
            "runtime_failures": self.runtime_failures,
            "diff_summary": self.diff_summary,
            "suggestions": self.suggestions,
            "raw_lint": self.raw_lint,
            "raw_compile": self.raw_compile,
            "raw_tests": self.raw_tests,
            "raw_runtime": self.raw_runtime,
        }


@dataclass
class JobRecord:
    job_id: str = field(default_factory=lambda: f"job_{uuid.uuid4().hex[:10]}")
    status: JobStatus = JobStatus.PENDING
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    task: Optional[CodingTask] = None
    result: Optional[CodingResult] = None
    review: Optional[ReviewVerdict] = None
    worktree_path: Optional[str] = None
    worktree_branch: Optional[str] = None
    runner_pid: int = 0
    retries: int = 0
    max_retries: int = 2
    error: str = ""
    progress_note: str = ""
    synced_terminal_status: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": self.status.value,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "task": self.task.to_dict() if self.task else None,
            "result": self.result.to_dict() if self.result else None,
            "review": self.review.to_dict() if self.review else None,
            "worktree_path": self.worktree_path,
            "worktree_branch": self.worktree_branch,
            "runner_pid": self.runner_pid,
            "retries": self.retries,
            "max_retries": self.max_retries,
            "error": self.error,
            "progress_note": self.progress_note,
            "synced_terminal_status": self.synced_terminal_status,
        }
