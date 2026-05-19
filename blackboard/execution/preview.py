"""Preview runner — spawn a dev server in a project, allocate a port, expose iframe URL.

Built-in runners: ``python`` (``python -m http.server <port>``), ``node`` (``npm run dev`` or
``npx vite``), ``static`` (just allocates a port and runs python http.server as fallback).
Each PreviewSession owns one child process plus a small rolling log buffer.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from typing import Any, Deque, Dict, List, Optional

from blackboard.kernel.logger import describe_error, get_logger

logger = get_logger("execution.preview")


_PORT_RANGE = (5101, 9000)
_LOG_BUFFER = 400
_STARTUP_GRACE_S = 1.0
_READINESS_TIMEOUT_S = 8.0
_STOP_TIMEOUT_S = 5.0


def _find_free_port() -> int:
    for _ in range(50):
        with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if _PORT_RANGE[0] <= port <= _PORT_RANGE[1]:
            return port
    # Fallback: any free port the OS gives us.
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _runner_args(runner: str, port: int) -> List[str] | None:
    runner = (runner or "static").lower()
    if runner in ("python", "static"):
        return [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"]
    if runner == "node":
        return ["npm", "run", "dev", "--", "--port", str(port)]
    if runner == "vite":
        return ["npx", "vite", "--port", str(port), "--host", "127.0.0.1"]
    if runner == "next":
        return ["npx", "next", "dev", "-p", str(port)]
    return None


async def _can_connect(port: int) -> bool:
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.close()
        await writer.wait_closed()
        return True
    except Exception:
        return False


def _format_command(args: List[str]) -> str:
    return " ".join(f'"{part}"' if " " in part else part for part in args)


class PreviewSession:
    def __init__(self, session_id: str, *, project_id: str, cwd: str, runner: str, port: int) -> None:
        self.id = session_id
        self.project_id = project_id
        self.cwd = cwd
        self.runner = runner
        self.port = port
        self.url = f"http://127.0.0.1:{port}"
        self.created_at = time.time()
        self.proc: Optional[Any] = None
        self._async_proc = False
        self._pump_task: Optional[asyncio.Task] = None
        self.log: Deque[str] = deque(maxlen=_LOG_BUFFER)
        self.closed = False

    def _returncode(self) -> Optional[int]:
        if self.proc is None:
            return None
        poll = getattr(self.proc, "poll", None)
        if callable(poll):
            return poll()
        return self.proc.returncode

    def _alive(self) -> bool:
        return bool(self.proc and self._returncode() is None)

    def _start_popen(self, args: List[str]) -> None:
        try:
            startupinfo = None
            creationflags = 0
            if os.name == "nt":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = 0
                if hasattr(subprocess, "CREATE_NO_WINDOW"):
                    creationflags |= subprocess.CREATE_NO_WINDOW
            self.proc = subprocess.Popen(
                args,
                cwd=self.cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                startupinfo=startupinfo,
                creationflags=creationflags,
            )
            self._async_proc = False
        except FileNotFoundError as exc:
            self.closed = True
            raise RuntimeError(f"runner not found: {args[0]} ({describe_error(exc)})") from exc
        except OSError as exc:
            self.closed = True
            raise RuntimeError(f"failed to start preview runner {_format_command(args)} in {self.cwd}: {describe_error(exc)}") from exc

    async def start(self) -> None:
        args = _runner_args(self.runner, self.port)
        if not args:
            raise ValueError(f"unknown preview runner: {self.runner}")
        if not os.path.isdir(self.cwd):
            raise RuntimeError(f"preview working directory does not exist: {self.cwd}")
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *args,
                cwd=self.cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            self._async_proc = True
        except FileNotFoundError as exc:
            self.closed = True
            raise RuntimeError(f"runner not found: {args[0]} ({describe_error(exc)})") from exc
        except NotImplementedError:
            self._start_popen(args)
        except OSError as exc:
            self.closed = True
            raise RuntimeError(f"failed to start preview runner {_format_command(args)} in {self.cwd}: {describe_error(exc)}") from exc

        async def _pump_async() -> None:
            assert self.proc and self.proc.stdout
            while not self.closed:
                chunk = await self.proc.stdout.readline()
                if not chunk:
                    return
                self.log.append(chunk.decode("utf-8", "replace").rstrip())

        def _pump_thread() -> None:
            if not self.proc or not self.proc.stdout:
                return
            while not self.closed:
                chunk = self.proc.stdout.readline()
                if not chunk:
                    return
                self.log.append(str(chunk).rstrip())

        if self._async_proc:
            self._pump_task = asyncio.create_task(_pump_async())
        else:
            threading.Thread(target=_pump_thread, daemon=True, name=f"preview-{self.id}-logs").start()
        await asyncio.sleep(_STARTUP_GRACE_S)
        if self._returncode() is not None:
            self.closed = True
            tail = "\n".join(self.log)
            detail = f"preview runner exited with code {self._returncode()}: {_format_command(args)}"
            if tail:
                detail = f"{detail}\n{tail}"
            raise RuntimeError(detail)
        deadline = time.monotonic() + _READINESS_TIMEOUT_S
        while time.monotonic() < deadline:
            if await _can_connect(self.port):
                return
            if self._returncode() is not None:
                self.closed = True
                tail = "\n".join(self.log)
                detail = f"preview runner exited with code {self._returncode()}: {_format_command(args)}"
                if tail:
                    detail = f"{detail}\n{tail}"
                raise RuntimeError(detail)
            await asyncio.sleep(0.15)
        self.log.append(f"[preview] runner still starting; no response on {self.url} after {_READINESS_TIMEOUT_S:.0f}s")

    async def stop(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self._pump_task is not None and not self._pump_task.done():
            self._pump_task.cancel()
            try:
                await asyncio.wait_for(self._pump_task, timeout=1.0)
            except (asyncio.CancelledError, Exception):
                pass
        if self.proc is None or self._returncode() is not None:
            self.proc = None
            return
        try:
            self.proc.terminate()
            if self._async_proc:
                try:
                    await asyncio.wait_for(self.proc.wait(), timeout=_STOP_TIMEOUT_S)
                except asyncio.TimeoutError:
                    self.proc.kill()
                    await asyncio.wait_for(self.proc.wait(), timeout=2.0)
            else:
                try:
                    await asyncio.wait_for(asyncio.to_thread(self.proc.wait, _STOP_TIMEOUT_S), timeout=_STOP_TIMEOUT_S + 0.5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    await asyncio.wait_for(asyncio.to_thread(self.proc.wait), timeout=2.0)
        except Exception as exc:
            logger.debug("[preview] stop failed: %s", exc)
        finally:
            self.proc = None

    def snapshot(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "cwd": self.cwd,
            "runner": self.runner,
            "port": self.port,
            "url": self.url,
            "created_at": self.created_at,
            "closed": self.closed,
            "log_tail": list(self.log)[-30:],
            "alive": self._alive(),
        }


class PreviewManager:
    """One preview per project (overwrites previous on re-start)."""

    def __init__(self) -> None:
        self._sessions: Dict[str, PreviewSession] = {}
        self._lock = asyncio.Lock()

    async def start(self, *, project_id: str, cwd: str, runner: str = "python") -> PreviewSession:
        async with self._lock:
            existing = self._sessions.get(project_id)
            if existing is not None:
                await existing.stop()
            port = _find_free_port()
            sid = f"prv_{uuid.uuid4().hex[:8]}"
            session = PreviewSession(sid, project_id=project_id, cwd=cwd, runner=runner, port=port)
            await session.start()
            self._sessions[project_id] = session
            return session

    async def stop(self, project_id: str) -> bool:
        session = self._sessions.pop(project_id, None)
        if session is None:
            return False
        try:
            await asyncio.wait_for(session.stop(), timeout=_STOP_TIMEOUT_S)
        except (asyncio.CancelledError, Exception):
            pass
        return True

    def get(self, project_id: str) -> Optional[PreviewSession]:
        return self._sessions.get(project_id)

    def list(self) -> List[Dict[str, Any]]:
        return [s.snapshot() for s in self._sessions.values()]

    async def stop_all(self) -> None:
        sessions = list(self._sessions.items())
        self._sessions = {}
        if not sessions:
            return
        await asyncio.gather(
            *(asyncio.wait_for(session.stop(), timeout=_STOP_TIMEOUT_S) for _, session in sessions),
            return_exceptions=True,
        )


_manager: Optional[PreviewManager] = None


def get_preview_manager() -> PreviewManager:
    global _manager
    if _manager is None:
        _manager = PreviewManager()
    return _manager
