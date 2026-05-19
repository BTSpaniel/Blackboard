"""Git tools — status, diff, branch info."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
from typing import Any, Dict

from blackboard.kernel.logger import get_logger
from blackboard.react.tool_registry import ToolRegistry
from blackboard.react.tools.commands import _cwd_for

logger = get_logger("react.tools.git")
_WINDOWS_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


async def _git(args_list, cwd: str, timeout: float = 30.0) -> Dict[str, Any]:
    kwargs: Dict[str, Any] = {}
    if _WINDOWS_CREATE_NO_WINDOW:
        kwargs["creationflags"] = _WINDOWS_CREATE_NO_WINDOW
    logger.info(
        "[spawn][git_tool] cwd=%s cmd=%s timeout=%s creationflags=%s",
        cwd,
        ["git", *args_list],
        timeout,
        kwargs.get("creationflags") or 0,
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args_list, cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **kwargs,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return {
            "exit_code": proc.returncode,
            "stdout": stdout.decode("utf-8", "replace"),
            "stderr": stderr.decode("utf-8", "replace"),
        }
    except FileNotFoundError:
        return {"exit_code": -1, "error": "git not installed"}
    except asyncio.TimeoutError:
        return {"exit_code": -1, "error": "git timed out"}


async def _git_status(args: Dict[str, Any]) -> str:
    cwd = _cwd_for(args)
    result = await _git(["status", "--short", "--branch"], cwd=cwd)
    return json.dumps({"cwd": cwd, **result})


async def _git_diff(args: Dict[str, Any]) -> str:
    cwd = _cwd_for(args)
    staged = bool(args.get("staged") or False)
    extra = ["--staged"] if staged else []
    paths = args.get("paths")
    if paths:
        extra.extend(paths if isinstance(paths, list) else [str(paths)])
    result = await _git(["diff", *extra], cwd=cwd, timeout=60.0)
    return json.dumps({"cwd": cwd, **result})


async def _git_log(args: Dict[str, Any]) -> str:
    cwd = _cwd_for(args)
    limit = int(args.get("limit") or 20)
    result = await _git(["log", "--oneline", f"-{limit}"], cwd=cwd)
    return json.dumps({"cwd": cwd, **result})


def register_git_tools(registry: ToolRegistry) -> None:
    registry.register_fn(
        "git_status",
        "Run `git status --short --branch` in the workspace.",
        {"type": "object", "properties": {"cwd": {"type": "string"}}},
        _git_status,
        tags=["git", "read"],
    )
    registry.register_fn(
        "git_diff",
        "Run `git diff` (optionally staged) in the workspace.",
        {
            "type": "object",
            "properties": {
                "cwd": {"type": "string"},
                "staged": {"type": "boolean", "default": False},
                "paths": {"type": "array", "items": {"type": "string"}},
            },
        },
        _git_diff,
        tags=["git", "read"],
    )
    registry.register_fn(
        "git_log",
        "Run `git log --oneline -N` in the workspace.",
        {"type": "object", "properties": {"cwd": {"type": "string"}, "limit": {"type": "integer", "default": 20}}},
        _git_log,
        tags=["git", "read"],
    )
