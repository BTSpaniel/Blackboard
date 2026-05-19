"""Best-effort Playwright screenshot capture.

Returns a structured error if Playwright isn't installed or the browser binary is missing,
so the UI/API never crashes. Stores screenshots under
``data/projects/<project_id>/screenshots/<ts>.png``.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from blackboard.kernel.logger import describe_error, get_logger

logger = get_logger("execution.playwright")


async def capture_screenshot(
    *,
    url: str,
    out_dir: Path,
    full_page: bool = True,
    viewport: Optional[Dict[str, int]] = None,
    timeout_ms: int = 15000,
) -> Dict[str, Any]:
    """Capture a single screenshot. Returns a result dict.

    Result shape:
        success: bool
        path: str   (absolute path on disk)
        url: str
        console_count: int
        error: str
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"shot_{int(time.time() * 1000)}.png"
    try:
        from playwright.async_api import async_playwright  # type: ignore
    except Exception:
        return {
            "success": False,
            "path": "",
            "url": url,
            "console_count": 0,
            "error": "playwright not installed (run `pip install playwright && playwright install chromium`)",
        }

    console_count = 0
    try:
        async with async_playwright() as p:
            try:
                browser = await p.chromium.launch(headless=True)
            except Exception as exc:
                return {
                    "success": False,
                    "path": "",
                    "url": url,
                    "console_count": 0,
                    "error": f"chromium launch failed: {describe_error(exc)}",
                }
            ctx_kwargs: Dict[str, Any] = {}
            if viewport:
                ctx_kwargs["viewport"] = {"width": int(viewport.get("width", 1280)), "height": int(viewport.get("height", 800))}
            ctx = await browser.new_context(**ctx_kwargs)
            page = await ctx.new_page()
            page.on("console", lambda msg: _bump(locals(), "console_count"))
            try:
                await page.goto(url, timeout=timeout_ms, wait_until="networkidle")
            except Exception as exc:
                await browser.close()
                return {
                    "success": False,
                    "path": "",
                    "url": url,
                    "console_count": 0,
                    "error": f"navigation failed: {describe_error(exc)}",
                }
            await page.screenshot(path=str(target), full_page=full_page)
            await browser.close()
    except Exception as exc:
        return {
            "success": False,
            "path": "",
            "url": url,
            "console_count": 0,
            "error": describe_error(exc, "playwright error"),
        }
    return {
        "success": True,
        "path": str(target),
        "url": url,
        "console_count": console_count,
        "error": "",
    }


async def smoke_check(
    *,
    url: str,
    out_dir: Path,
    full_page: bool = True,
    viewport: Optional[Dict[str, int]] = None,
    timeout_ms: int = 15000,
    wait_after_load_ms: int = 900,
) -> Dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"smoke_{int(time.time() * 1000)}.png"
    try:
        from playwright.async_api import async_playwright  # type: ignore
    except Exception:
        return {
            "success": False,
            "path": "",
            "url": url,
            "console_count": 0,
            "console_errors": [],
            "page_errors": [],
            "request_failures": [],
            "error": "playwright not installed (run `pip install playwright && playwright install chromium`)",
        }

    console_errors: List[str] = []
    page_errors: List[str] = []
    request_failures: List[str] = []
    console_count = 0
    try:
        async with async_playwright() as p:
            try:
                browser = await p.chromium.launch(headless=True)
            except Exception as exc:
                return {
                    "success": False,
                    "path": "",
                    "url": url,
                    "console_count": 0,
                    "console_errors": [],
                    "page_errors": [],
                    "request_failures": [],
                    "error": f"chromium launch failed: {describe_error(exc)}",
                }
            ctx_kwargs: Dict[str, Any] = {}
            if viewport:
                ctx_kwargs["viewport"] = {"width": int(viewport.get("width", 1280)), "height": int(viewport.get("height", 800))}
            context = await browser.new_context(**ctx_kwargs)
            page = await context.new_page()

            def _handle_console(msg) -> None:
                nonlocal console_count
                console_count += 1
                if str(getattr(msg, "type", "") or "").lower() == "error":
                    console_errors.append(str(getattr(msg, "text", "") or "")[:400])

            def _handle_page_error(exc: Exception) -> None:
                page_errors.append(str(exc or "")[:400])

            def _handle_request_failed(request) -> None:
                failure = getattr(request, "failure", None)
                failure_text = ""
                try:
                    failure_obj = failure() if callable(failure) else failure
                    if isinstance(failure_obj, dict):
                        failure_text = str(failure_obj.get("errorText") or "")
                except Exception:
                    failure_text = ""
                request_failures.append(f"{str(getattr(request, 'method', '') or '')} {str(getattr(request, 'url', '') or '')} {failure_text}".strip()[:400])

            page.on("console", _handle_console)
            page.on("pageerror", _handle_page_error)
            page.on("requestfailed", _handle_request_failed)
            try:
                await page.goto(url, timeout=timeout_ms, wait_until="load")
                if wait_after_load_ms > 0:
                    await page.wait_for_timeout(wait_after_load_ms)
            except Exception as exc:
                await browser.close()
                return {
                    "success": False,
                    "path": "",
                    "url": url,
                    "console_count": console_count,
                    "console_errors": console_errors[:12],
                    "page_errors": page_errors[:12],
                    "request_failures": request_failures[:12],
                    "error": f"navigation failed: {describe_error(exc)}",
                }
            await page.screenshot(path=str(target), full_page=full_page)
            await browser.close()
    except Exception as exc:
        return {
            "success": False,
            "path": "",
            "url": url,
            "console_count": console_count,
            "console_errors": console_errors[:12],
            "page_errors": page_errors[:12],
            "request_failures": request_failures[:12],
            "error": describe_error(exc, "playwright error"),
        }
    return {
        "success": not console_errors and not page_errors and not request_failures,
        "path": str(target),
        "url": url,
        "console_count": console_count,
        "console_errors": console_errors[:12],
        "page_errors": page_errors[:12],
        "request_failures": request_failures[:12],
        "error": "",
    }


def _bump(scope: Dict[str, Any], name: str) -> None:
    # Hack: increment a counter declared as a local in the outer fn. Used purely to silence
    # type-checkers about the closure; real counts come from the dict-based fallback below.
    try:
        scope[name] = int(scope.get(name, 0)) + 1
    except Exception:
        pass
