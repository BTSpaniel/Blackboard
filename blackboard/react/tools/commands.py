"""Command execution tools — run_command, run_tests, lint_check."""
from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from blackboard.kernel.logger import get_logger
from blackboard.react.tool_registry import ToolRegistry
from blackboard.react.tools.file_ops import _resolve_path

_BLOCKED = ["rm -rf /", "mkfs", "dd if=/dev/zero", ":(){:|:&};:", "shutdown", "reboot", "halt", "format c:"]
_DEFAULT_TIMEOUT = 60.0
_MAX_OUTPUT = 8000
_WINDOWS_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

logger = get_logger("react.tools.commands")


def _is_blocked(cmd: str) -> bool:
    lowered = cmd.lower()
    return any(b in lowered for b in _BLOCKED)


def _cwd_for(args: Dict[str, Any]) -> str:
    cwd_arg = args.get("cwd")
    if cwd_arg:
        return str(_resolve_path(str(cwd_arg), args))
    runtime = (args or {}).get("_tool_runtime") or {}
    root = runtime.get("workspace_root") or runtime.get("execution_root")
    if root:
        return str(root)
    return os.getcwd()


async def _spawn(cmd_args: List[str], cwd: str, timeout: float) -> Dict[str, Any]:
    kwargs: Dict[str, Any] = {}
    if _WINDOWS_CREATE_NO_WINDOW:
        kwargs["creationflags"] = _WINDOWS_CREATE_NO_WINDOW
    logger.info(
        "[spawn][tool_command] cwd=%s cmd=%s timeout=%s creationflags=%s",
        cwd,
        cmd_args,
        timeout,
        kwargs.get("creationflags") or 0,
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd_args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **kwargs,
        )
    except FileNotFoundError as exc:
        return {"error": f"binary not found: {cmd_args[0]} ({exc})", "exit_code": -1}
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return {"error": f"timed out after {timeout}s", "exit_code": -1, "stdout": "", "stderr": ""}
    return {
        "exit_code": proc.returncode,
        "stdout": stdout.decode("utf-8", "replace")[-_MAX_OUTPUT:],
        "stderr": stderr.decode("utf-8", "replace")[-_MAX_OUTPUT:],
    }


async def _run_command(args: Dict[str, Any]) -> str:
    command = str(args.get("command") or "")
    if not command:
        return json.dumps({"error": "command required"})
    if _is_blocked(command):
        return json.dumps({"error": "command blocked by safety policy"})
    cwd = _cwd_for(args)
    timeout = float(args.get("timeout") or _DEFAULT_TIMEOUT)
    cmd_args = shlex.split(command, posix=os.name != "nt")
    result = await _spawn(cmd_args, cwd=cwd, timeout=timeout)
    result["command"] = command
    result["cwd"] = cwd
    return json.dumps(result)


async def _run_tests(args: Dict[str, Any]) -> str:
    path = str(args.get("path") or ".")
    extra = str(args.get("args") or "")
    timeout = float(args.get("timeout") or 180.0)
    cwd = _cwd_for(args)
    cmd_args = ["pytest", path, "--tb=short", "-q"]
    if extra:
        cmd_args.extend(shlex.split(extra, posix=os.name != "nt"))
    result = await _spawn(cmd_args, cwd=cwd, timeout=timeout)
    combined = (result.get("stdout", "") + "\n" + result.get("stderr", "")).strip()
    # Cheap heuristics for pass/fail count.
    failed = 0
    passed = 0
    for line in combined.splitlines():
        if "passed" in line and "failed" in line:
            for token in line.replace(",", " ").split():
                if token.isdigit():
                    continue
        if " failed" in line:
            for token in line.split():
                if token.isdigit():
                    failed = max(failed, int(token))
        if " passed" in line:
            for token in line.split():
                if token.isdigit():
                    passed = max(passed, int(token))
    result.update({"passed": passed, "failed": failed})
    return json.dumps(result)


async def _lint_check(args: Dict[str, Any]) -> str:
    path = str(args.get("path") or ".")
    cwd = _cwd_for(args)
    timeout = float(args.get("timeout") or 60.0)
    # Try ruff -> flake8 -> python -m pyflakes.
    for cmd_args in (["ruff", "check", path], ["flake8", path], ["python", "-m", "pyflakes", path]):
        result = await _spawn(cmd_args, cwd=cwd, timeout=timeout)
        if result.get("exit_code") == -1 and "binary not found" in (result.get("error") or ""):
            continue
        combined = (result.get("stdout", "") + "\n" + result.get("stderr", "")).strip()
        if cmd_args[:3] == ["python", "-m", "pyflakes"] and "no module named pyflakes" in combined.lower():
            continue
        violations = []
        for line in combined.splitlines():
            if line.strip():
                violations.append(line)
        return json.dumps({
            "linter": cmd_args[0],
            "violations": violations,
            "clean": result.get("exit_code") == 0,
            "exit_code": result.get("exit_code"),
        })
    return json.dumps({"error": "no linter available (tried ruff, flake8, pyflakes)"})


def register_command_tools(registry: ToolRegistry) -> None:
    registry.register_fn(
        "run_command",
        "Run a shell command in the workspace. Blocks destructive patterns. Returns exit_code/stdout/stderr.",
        {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "cwd": {"type": "string"},
                "timeout": {"type": "number", "default": 60},
            },
            "required": ["command"],
        },
        _run_command,
        timeout_s=180,
        tags=["exec"],
        mutation_mode="unverified",
    )
    registry.register_fn(
        "run_tests",
        "Run pytest in the workspace and return a summary.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "default": "."},
                "args": {"type": "string"},
                "cwd": {"type": "string"},
            },
        },
        _run_tests,
        timeout_s=300,
        tags=["exec", "test"],
    )
    registry.register_fn(
        "lint_check",
        "Run ruff/flake8/pyflakes (whichever is available) and return violations.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "default": "."},
                "cwd": {"type": "string"},
            },
        },
        _lint_check,
        timeout_s=120,
        tags=["exec", "lint"],
    )
