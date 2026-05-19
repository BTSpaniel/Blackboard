"""Terminal sessions — async PTY where available, subprocess pipes as fallback.

A TerminalSession owns a child shell (PowerShell on Windows, /bin/bash elsewhere),
exposes ``write(text)`` for input, and pushes output chunks into an asyncio.Queue
so callers (WebSocket handler, tests) can stream them.

pywinpty is preferred on Windows; if it's not importable we fall back to a plain
asyncio subprocess with stdout/stderr pipes. Both code paths share the same
public interface.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
import uuid
from typing import Any, Dict, Optional

from blackboard.kernel.logger import describe_error, get_logger

logger = get_logger("execution.terminal")


_DEFAULT_SHELL_WINDOWS = "powershell.exe"
_DEFAULT_SHELL_POSIX = "/bin/bash"
_CLOSE_TIMEOUT_S = 3.0
_WINDOWS_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


class TerminalSession:
    """One shell process bound to a workspace cwd, with an output queue."""

    def __init__(self, session_id: str, *, cwd: str, shell: str | None = None) -> None:
        self.id = session_id
        self.cwd = str(cwd)
        self.shell = shell or (_DEFAULT_SHELL_WINDOWS if os.name == "nt" else _DEFAULT_SHELL_POSIX)
        self.created_at = time.time()
        self.closed = False
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._pty = None
        self._pty_read_task: Optional[asyncio.Task] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self.output: asyncio.Queue[Dict[str, Any]] = asyncio.Queue(maxsize=2048)

    # ── Lifecycle ────────────────────────────────────────────────

    async def start(self) -> None:
        if self._try_pty_start():
            return
        await self._fallback_subprocess_start()

    def _try_pty_start(self) -> bool:
        """Try pywinpty (Windows) — return True if it spawned successfully."""
        if os.name != "nt":
            return False
        try:
            from winpty import PtyProcess  # type: ignore
        except Exception:
            return False
        try:
            self._pty = PtyProcess.spawn([self.shell], cwd=self.cwd)
        except Exception as exc:
            logger.debug("[terminal] pywinpty spawn failed: %s", exc)
            return False
        logger.info(
            "[spawn][terminal_pty] session_id=%s cwd=%s shell=%s",
            self.id,
            self.cwd,
            self.shell,
        )

        async def _pump():
            loop = asyncio.get_running_loop()
            while not self.closed and self._pty and self._pty.isalive():
                try:
                    chunk = await loop.run_in_executor(None, self._pty.read, 1024)
                except Exception as exc:
                    await self._push({"stream": "stderr", "text": f"[terminal] read error: {describe_error(exc)}"})
                    break
                if not chunk:
                    break
                await self._push({"stream": "stdout", "text": chunk})
            await self._push({"stream": "system", "text": "[terminal] closed", "closed": True})

        self._pty_read_task = asyncio.create_task(_pump())
        return True

    async def _fallback_subprocess_start(self) -> None:
        # Plain subprocess: no real TTY (no interactive prompts), but enough for one-shot commands.
        try:
            kwargs: Dict[str, Any] = {}
            if _WINDOWS_CREATE_NO_WINDOW:
                kwargs["creationflags"] = _WINDOWS_CREATE_NO_WINDOW
            logger.info(
                "[spawn][terminal_fallback] session_id=%s cwd=%s shell=%s creationflags=%s",
                self.id,
                self.cwd,
                self.shell,
                kwargs.get("creationflags") or 0,
            )
            self._proc = await asyncio.create_subprocess_shell(
                self.shell,
                cwd=self.cwd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **kwargs,
            )
        except Exception as exc:
            await self._push({"stream": "stderr", "text": f"[terminal] spawn failed: {describe_error(exc)}", "closed": True})
            self.closed = True
            return

        async def _pump_stream(reader: asyncio.StreamReader, label: str) -> None:
            while not self.closed:
                chunk = await reader.read(1024)
                if not chunk:
                    return
                await self._push({"stream": label, "text": chunk.decode("utf-8", "replace")})

        assert self._proc.stdout and self._proc.stderr
        self._reader_task = asyncio.create_task(_pump_stream(self._proc.stdout, "stdout"))
        self._stderr_task = asyncio.create_task(_pump_stream(self._proc.stderr, "stderr"))

        async def _watch_exit() -> None:
            try:
                rc = await self._proc.wait() if self._proc else None
            except Exception:
                rc = None
            await self._push({"stream": "system", "text": f"[terminal] exited rc={rc}", "closed": True})
            self.closed = True

        asyncio.create_task(_watch_exit())

    async def _push(self, message: Dict[str, Any]) -> None:
        try:
            self.output.put_nowait(message)
        except asyncio.QueueFull:
            try:
                _ = self.output.get_nowait()
            except Exception:
                pass
            try:
                self.output.put_nowait(message)
            except Exception:
                pass

    # ── I/O ──────────────────────────────────────────────────────

    async def write(self, text: str) -> None:
        if self.closed:
            return
        # pywinpty path
        if self._pty is not None:
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self._pty.write, text)
                return
            except Exception as exc:
                logger.debug("[terminal] pty write failed: %s", exc)
        # subprocess path
        if self._proc is not None and self._proc.stdin is not None:
            try:
                self._proc.stdin.write(text.encode("utf-8"))
                await self._proc.stdin.drain()
            except Exception as exc:
                logger.debug("[terminal] subprocess write failed: %s", exc)

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            if self._pty is not None and self._pty.isalive():
                self._pty.terminate(force=True)
        except Exception:
            pass
        pending_tasks = [task for task in (self._pty_read_task, self._reader_task, self._stderr_task) if task and not task.done()]
        for task in pending_tasks:
            if task and not task.done():
                task.cancel()
        if pending_tasks:
            try:
                await asyncio.wait_for(asyncio.gather(*pending_tasks, return_exceptions=True), timeout=_CLOSE_TIMEOUT_S)
            except Exception:
                pass
        if self._proc and self._proc.returncode is None:
            try:
                self._proc.kill()
                await asyncio.wait_for(self._proc.wait(), timeout=_CLOSE_TIMEOUT_S)
            except Exception:
                pass
        self._proc = None
        self._pty = None


class TerminalManager:
    """Owns active terminal sessions keyed by id."""

    def __init__(self) -> None:
        self._sessions: Dict[str, TerminalSession] = {}
        self._lock = asyncio.Lock()

    async def create(self, *, cwd: str, shell: str | None = None) -> TerminalSession:
        async with self._lock:
            sid = f"term_{uuid.uuid4().hex[:10]}"
            session = TerminalSession(sid, cwd=cwd, shell=shell)
            await session.start()
            self._sessions[sid] = session
            return session

    def get(self, session_id: str) -> Optional[TerminalSession]:
        return self._sessions.get(session_id)

    def list(self) -> Dict[str, Dict[str, Any]]:
        return {
            sid: {"id": sid, "cwd": s.cwd, "shell": s.shell, "closed": s.closed, "created_at": s.created_at}
            for sid, s in self._sessions.items()
        }

    async def close(self, session_id: str) -> bool:
        session = self._sessions.pop(session_id, None)
        if session is None:
            return False
        try:
            await asyncio.wait_for(session.close(), timeout=_CLOSE_TIMEOUT_S)
        except Exception:
            pass
        return True

    async def close_all(self) -> None:
        sessions = list(self._sessions.items())
        self._sessions = {}
        if not sessions:
            return
        await asyncio.gather(
            *(asyncio.wait_for(session.close(), timeout=_CLOSE_TIMEOUT_S) for _, session in sessions),
            return_exceptions=True,
        )


_manager: Optional[TerminalManager] = None


def get_terminal_manager() -> TerminalManager:
    global _manager
    if _manager is None:
        _manager = TerminalManager()
    return _manager
