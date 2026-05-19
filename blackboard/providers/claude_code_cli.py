"""Claude Code CLI provider — spawns `claude` as a subprocess inside a worktree.

The CLI handles its own auth (Pro/Max subscription, OAuth). We pipe the objective via stdin
and stream stdout back into the card transcript via the bus.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import shutil
import time
from typing import Any, Optional

from blackboard.kernel.bus import get_bus
from blackboard.kernel.logger import describe_error, get_logger
from blackboard.providers.base import (
    AIProvider,
    ExecuteInput,
    ExecuteOutput,
    ProviderCapabilities,
    ProviderHealth,
)

logger = get_logger("providers.claude_code_cli")
_WINDOWS_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


class ClaudeCodeCLIProvider(AIProvider):
    """Treats Claude Code as a `coding_cli` provider type."""

    type = "coding_cli"

    def __init__(
        self,
        provider_id: str,
        *,
        bin: str = "claude",
        capabilities: Optional[ProviderCapabilities] = None,
        extra_args: Optional[list[str]] = None,
    ) -> None:
        self.id = provider_id
        self.name = provider_id
        self.model = "claude-code-cli"
        self.capabilities = capabilities or ProviderCapabilities(
            code_edit=True, repo_aware=True, terminal_aware=True, streaming=True
        )
        self._bin = bin
        self._extra_args = list(extra_args or [])

    def _resolve_bin(self) -> Optional[str]:
        path = shutil.which(self._bin)
        if path:
            return path
        # On Windows the bin may be `claude.cmd` or `claude.exe`
        for ext in (".cmd", ".exe", ".bat"):
            alt = shutil.which(self._bin + ext)
            if alt:
                return alt
        return None

    async def health(self) -> ProviderHealth:
        started = time.monotonic()
        path = self._resolve_bin()
        if not path:
            return ProviderHealth(
                ok=False,
                latency_ms=0,
                error=f"`{self._bin}` not found in PATH",
            )
        kwargs = {"creationflags": _WINDOWS_CREATE_NO_WINDOW} if _WINDOWS_CREATE_NO_WINDOW else {}
        logger.info(
            "[spawn][provider_claude_health] provider=%s cmd=%s creationflags=%s",
            self.id,
            [path, "--version"],
            kwargs.get("creationflags") or 0,
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                path, "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **kwargs,
            )
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
            except asyncio.TimeoutError:
                proc.kill()
                return ProviderHealth(ok=False, latency_ms=10000, error="`claude --version` timed out")
            ok = proc.returncode == 0
            return ProviderHealth(
                ok=ok,
                latency_ms=int((time.monotonic() - started) * 1000),
                error="" if ok else f"exit {proc.returncode}",
                detail={"version": stdout.decode("utf-8", "replace").strip()[:200]},
            )
        except Exception as exc:
            return ProviderHealth(
                ok=False,
                latency_ms=int((time.monotonic() - started) * 1000),
                error=describe_error(exc, "claude --version failed"),
            )

    async def execute(self, input: ExecuteInput) -> ExecuteOutput:
        path = self._resolve_bin()
        if not path:
            return ExecuteOutput(success=False, error=f"`{self._bin}` not found in PATH")
        bus = get_bus()
        cwd = input.cwd or os.getcwd()
        before = self._snapshot_mtimes(cwd)

        # Construct the objective blob (markdown).
        blob_parts = [f"# Objective\n{input.objective}"]
        if input.files:
            blob_parts.append("\n## Priority files\n" + "\n".join(f"- {f}" for f in input.files))
        if input.constraints:
            blob_parts.append("\n## Constraints\n" + "\n".join(f"- {c}" for c in input.constraints))
        if input.verification:
            blob_parts.append("\n## Verification\n" + "\n".join(f"- {v}" for v in input.verification))
        blob = "\n".join(blob_parts)

        args = [path, "--print", *self._extra_args]
        kwargs = {"creationflags": _WINDOWS_CREATE_NO_WINDOW} if _WINDOWS_CREATE_NO_WINDOW else {}
        logger.info(
            "[spawn][provider_claude_execute] provider=%s cwd=%s cmd=%s creationflags=%s timeout=%s",
            self.id,
            cwd,
            args,
            kwargs.get("creationflags") or 0,
            input.timeout_s,
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                cwd=cwd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **kwargs,
            )
        except FileNotFoundError as exc:
            return ExecuteOutput(success=False, error=describe_error(exc, "spawn failed"))

        transcript_chunks: list[str] = []

        async def _pump(stream: asyncio.StreamReader, kind: str) -> None:
            while True:
                chunk = await stream.read(2048)
                if not chunk:
                    return
                text = chunk.decode("utf-8", "replace")
                transcript_chunks.append(text)
                await bus.emit("coding:cli.transcript", {"provider": self.id, "kind": kind, "text": text})

        assert proc.stdin and proc.stdout and proc.stderr
        try:
            proc.stdin.write(blob.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()
            stdout_task = asyncio.create_task(_pump(proc.stdout, "stdout"))
            stderr_task = asyncio.create_task(_pump(proc.stderr, "stderr"))
            try:
                returncode = await asyncio.wait_for(proc.wait(), timeout=input.timeout_s)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                returncode = -1
                transcript_chunks.append("\n[timed out]")
            await stdout_task
            await stderr_task
        except Exception as exc:
            try:
                proc.kill()
            except Exception:
                pass
            return ExecuteOutput(success=False, transcript="".join(transcript_chunks), error=describe_error(exc, "cli error"))

        after = self._snapshot_mtimes(cwd)
        changed = sorted(self._diff_mtimes(before, after))
        success = returncode == 0
        return ExecuteOutput(
            success=success,
            transcript="".join(transcript_chunks),
            changed_files=changed,
            error="" if success else f"claude exited with code {returncode}",
        )

    @staticmethod
    def _snapshot_mtimes(cwd: str, max_files: int = 5000) -> dict[str, float]:
        out: dict[str, float] = {}
        root = os.fspath(cwd)
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in {".git", "node_modules", "__pycache__", ".worktrees"}]
            for name in files:
                full = os.path.join(dirpath, name)
                try:
                    out[full] = os.path.getmtime(full)
                except OSError:
                    continue
                if len(out) >= max_files:
                    return out
        return out

    @staticmethod
    def _diff_mtimes(before: dict[str, float], after: dict[str, float]) -> list[str]:
        changed = set()
        for path, mtime in after.items():
            if path not in before or before[path] != mtime:
                changed.add(path)
        return list(changed)
