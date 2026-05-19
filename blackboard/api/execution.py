"""Execution endpoints — terminal spawn + WS stream, preview start/stop, screenshot."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from blackboard.execution.playwright_runner import capture_screenshot
from blackboard.execution.preview import get_preview_manager
from blackboard.execution.terminal import get_terminal_manager
from blackboard.api.files import ensure_workspace_dir
from blackboard.kernel.logger import describe_error
from blackboard.kernel.logger import get_logger

logger = get_logger("api.execution")
router = APIRouter(tags=["execution"])


# ── Terminal ─────────────────────────────────────────────────────


class TerminalCreate(BaseModel):
    cwd: str = "."
    shell: Optional[str] = None


def _default_cwd(request: Request, cwd: str) -> str:
    value = str(cwd or "").strip()
    if value and value not in (".", "./"):
        return value
    return str(ensure_workspace_dir(Path(request.app.state.data_root)))


@router.post("/api/terminal")
async def create_terminal(body: TerminalCreate, request: Request) -> Dict[str, Any]:
    manager = get_terminal_manager()
    session = await manager.create(cwd=_default_cwd(request, body.cwd), shell=body.shell)
    return {"id": session.id, "cwd": session.cwd, "shell": session.shell, "ws": f"/ws/terminal/{session.id}"}


@router.get("/api/terminal")
async def list_terminals() -> Dict[str, Any]:
    return get_terminal_manager().list()


@router.delete("/api/terminal/{session_id}")
async def close_terminal(session_id: str) -> Dict[str, Any]:
    ok = await get_terminal_manager().close(session_id)
    return {"id": session_id, "closed": ok}


@router.websocket("/ws/terminal/{session_id}")
async def terminal_ws(ws: WebSocket, session_id: str) -> None:
    """Bidirectional terminal stream. Client sends raw text input; server sends {stream, text} frames."""
    manager = get_terminal_manager()
    session = manager.get(session_id)
    if session is None:
        await ws.close(code=4004)
        return
    await ws.accept()
    await ws.send_text(json.dumps({"stream": "system", "text": f"[connected to {session.id} cwd={session.cwd}]"}))

    async def _read_input() -> None:
        try:
            while not session.closed:
                msg = await ws.receive_text()
                await session.write(msg)
        except WebSocketDisconnect:
            return
        except Exception as exc:
            logger.debug("[terminal-ws] input loop error: %s", exc)

    async def _push_output() -> None:
        while not session.closed:
            try:
                msg = await asyncio.wait_for(session.output.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            try:
                await ws.send_text(json.dumps(msg))
            except Exception:
                return
            if msg.get("closed"):
                return

    input_task = asyncio.create_task(_read_input())
    output_task = asyncio.create_task(_push_output())
    try:
        await asyncio.wait({input_task, output_task}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        input_task.cancel()
        output_task.cancel()
        try:
            await ws.close()
        except Exception:
            pass


# ── Preview ──────────────────────────────────────────────────────


class PreviewStart(BaseModel):
    cwd: str = "."
    runner: str = "python"


@router.post("/api/preview/{project_id}")
async def start_preview(project_id: str, body: PreviewStart, request: Request) -> Dict[str, Any]:
    project = request.app.state.project_store.get(project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    try:
        session = await get_preview_manager().start(project_id=project_id, cwd=_default_cwd(request, body.cwd), runner=body.runner)
    except RuntimeError as exc:
        raise HTTPException(424, describe_error(exc, "preview runner failed to start"))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return session.snapshot()


@router.get("/api/preview/{project_id}")
async def get_preview(project_id: str) -> Dict[str, Any]:
    session = get_preview_manager().get(project_id)
    if session is None:
        return {"running": False}
    return {"running": True, **session.snapshot()}


@router.delete("/api/preview/{project_id}")
async def stop_preview(project_id: str) -> Dict[str, Any]:
    ok = await get_preview_manager().stop(project_id)
    return {"project_id": project_id, "stopped": ok}


@router.get("/api/preview")
async def list_previews() -> List[Dict[str, Any]]:
    return get_preview_manager().list()


# ── Playwright ──────────────────────────────────────────────────


class ScreenshotRequest(BaseModel):
    project_id: str
    url: str
    full_page: bool = True
    timeout_ms: int = 15000
    viewport_width: int = 1280
    viewport_height: int = 800


@router.post("/api/playwright/screenshot")
async def take_screenshot(body: ScreenshotRequest, request: Request) -> Dict[str, Any]:
    project = request.app.state.project_store.get(body.project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    out_dir = Path(request.app.state.data_root) / "projects" / body.project_id / "screenshots"
    result = await capture_screenshot(
        url=body.url,
        out_dir=out_dir,
        full_page=body.full_page,
        viewport={"width": body.viewport_width, "height": body.viewport_height},
        timeout_ms=body.timeout_ms,
    )
    audit = request.app.state.audit_logs.get(body.project_id)
    if audit is not None:
        audit.record("playwright.screenshot", {
            "url": body.url, "success": result.get("success"), "error": result.get("error") or "",
        })
    return result
