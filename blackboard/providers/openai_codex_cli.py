from __future__ import annotations

import asyncio
import os
import subprocess
import shutil
import time
from typing import Optional

from blackboard.kernel.bus import get_bus
from blackboard.kernel.logger import describe_error, get_logger
from blackboard.providers.base import (
    AIProvider,
    ExecuteInput,
    ExecuteOutput,
    ProviderCapabilities,
    ProviderHealth,
)

logger = get_logger("providers.openai_codex_cli")
_WINDOWS_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


class OpenAICodexCLIProvider(AIProvider):
    type = "coding_cli"

    def __init__(
        self,
        provider_id: str,
        *,
        bin: str = "codex",
        model: str = "gpt-5.5",
        capabilities: Optional[ProviderCapabilities] = None,
        extra_args: Optional[list[str]] = None,
        sandbox: str = "workspace-write",
        ephemeral: bool = True,
        skip_git_repo_check: bool = False,
        approval_policy: str = "",
    ) -> None:
        self.id = provider_id
        self.name = provider_id
        self.model = str(model or "gpt-5.5")
        self.capabilities = capabilities or ProviderCapabilities(
            code_edit=True, repo_aware=True, terminal_aware=True, streaming=True
        )
        self._bin = bin
        self._extra_args = list(extra_args or [])
        self._sandbox = str(sandbox or "workspace-write")
        self._ephemeral = bool(ephemeral)
        self._skip_git_repo_check = bool(skip_git_repo_check)
        self._approval_policy = str(approval_policy or "").strip()

    def set_model(self, value: str) -> None:
        self.model = str(value or "").strip()

    async def list_models(self) -> list[str]:
        models = ["gpt-5.5", "gpt-5.4-mini", "gpt-5.3-codex-spark"]
        if self.model and self.model not in models:
            models.insert(0, self.model)
        return models

    def _resolve_bin(self) -> Optional[str]:
        path = shutil.which(self._bin)
        if path:
            return path
        for ext in (".cmd", ".exe", ".bat"):
            alt = shutil.which(self._bin + ext)
            if alt:
                return alt
        return None

    async def health(self) -> ProviderHealth:
        started = time.monotonic()
        path = self._resolve_bin()
        if not path:
            return ProviderHealth(ok=False, latency_ms=0, error=f"`{self._bin}` not found in PATH")
        kwargs = {"creationflags": _WINDOWS_CREATE_NO_WINDOW} if _WINDOWS_CREATE_NO_WINDOW else {}
        logger.info(
            "[spawn][provider_codex_health] provider=%s cmd=%s creationflags=%s",
            self.id,
            [path, "--version"],
            kwargs.get("creationflags") or 0,
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                path,
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **kwargs,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)
            except asyncio.TimeoutError:
                proc.kill()
                return ProviderHealth(ok=False, latency_ms=10000, error="`codex --version` timed out")
            version = (stdout + stderr).decode("utf-8", "replace").strip()[:200]
            ok = proc.returncode == 0
            return ProviderHealth(
                ok=ok,
                latency_ms=int((time.monotonic() - started) * 1000),
                error="" if ok else f"exit {proc.returncode}",
                detail={"version": version},
            )
        except Exception as exc:
            return ProviderHealth(
                ok=False,
                latency_ms=int((time.monotonic() - started) * 1000),
                error=describe_error(exc, "codex --version failed"),
            )

    async def execute(self, input: ExecuteInput) -> ExecuteOutput:
        path = self._resolve_bin()
        if not path:
            return ExecuteOutput(success=False, error=f"`{self._bin}` not found in PATH")
        bus = get_bus()
        cwd = input.cwd or os.getcwd()
        before = self._snapshot_mtimes(cwd)
        blob = self._build_prompt(input)
        args = self._build_args(path, cwd)
        kwargs = {"creationflags": _WINDOWS_CREATE_NO_WINDOW} if _WINDOWS_CREATE_NO_WINDOW else {}
        logger.info(
            "[spawn][provider_codex_execute] provider=%s cwd=%s cmd=%s creationflags=%s timeout=%s",
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
            error="" if success else f"codex exited with code {returncode}",
        )

    def _build_args(self, path: str, cwd: str) -> list[str]:
        args = [path, "exec", "--cd", cwd, "--sandbox", self._sandbox, "--color", "never"]
        if self.model:
            args.extend(["--model", self.model])
        if self._ephemeral:
            args.append("--ephemeral")
        if self._skip_git_repo_check:
            args.append("--skip-git-repo-check")
        if self._approval_policy:
            args.extend(["--ask-for-approval", self._approval_policy])
        args.extend(self._extra_args)
        args.append("-")
        return args

    @staticmethod
    def _build_prompt(input: ExecuteInput) -> str:
        blob_parts = [f"# Objective\n{input.objective}"]
        if input.files:
            blob_parts.append("\n## Priority files\n" + "\n".join(f"- {f}" for f in input.files))
        if input.constraints:
            blob_parts.append("\n## Constraints\n" + "\n".join(f"- {c}" for c in input.constraints))
        if input.verification:
            blob_parts.append("\n## Verification\n" + "\n".join(f"- {v}" for v in input.verification))
        return "\n".join(blob_parts)

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
