"""LSP-style tools — currently a thin pyright wrapper. Optional.

If pyright isn't on PATH, tools return a clean JSON error instead of crashing.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import shutil
from typing import Any, Dict

from blackboard.kernel.logger import get_logger
from blackboard.react.tool_registry import ToolRegistry
from blackboard.react.tools.commands import _cwd_for

logger = get_logger("react.tools.lsp")
_WINDOWS_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _find_pyright() -> str | None:
    override = os.environ.get("PYRIGHT_BIN")
    if override and os.path.exists(override):
        return override
    return shutil.which("pyright") or shutil.which("pyright.cmd")


async def _lsp_diagnostics(args: Dict[str, Any]) -> str:
    path = str(args.get("path") or ".")
    cwd = _cwd_for(args)
    pyright = _find_pyright()
    if not pyright:
        return json.dumps({"error": "pyright not installed", "available": False})
    kwargs: Dict[str, Any] = {}
    if _WINDOWS_CREATE_NO_WINDOW:
        kwargs["creationflags"] = _WINDOWS_CREATE_NO_WINDOW
    logger.info(
        "[spawn][lsp_pyright] cwd=%s cmd=%s creationflags=%s",
        cwd,
        [pyright, "--outputjson", path],
        kwargs.get("creationflags") or 0,
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            pyright, "--outputjson", path,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **kwargs,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60.0)
    except asyncio.TimeoutError:
        return json.dumps({"error": "pyright timed out"})
    except Exception as exc:
        return json.dumps({"error": f"pyright failed: {exc}"})
    text = stdout.decode("utf-8", "replace")
    try:
        data = json.loads(text)
        diags = data.get("generalDiagnostics") or []
        summarized = [
            {
                "file": d.get("file"),
                "range": d.get("range"),
                "severity": d.get("severity"),
                "message": d.get("message"),
            }
            for d in diags[:50]
        ]
        return json.dumps({
            "path": path,
            "diagnostic_count": len(diags),
            "diagnostics": summarized,
        })
    except Exception:
        return json.dumps({"raw": text[:4000], "stderr": stderr.decode("utf-8", "replace")[:2000]})


def register_lsp_tools(registry: ToolRegistry) -> None:
    registry.register_fn(
        "lsp_diagnostics",
        "Run pyright on a path and return JSON diagnostics (if pyright is installed).",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "default": "."},
                "cwd": {"type": "string"},
            },
        },
        _lsp_diagnostics,
        timeout_s=90,
        tags=["lsp", "read"],
    )
