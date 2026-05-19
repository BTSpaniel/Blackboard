"""Reviewer — runs lint + tests + (optional) LLM semantic review.

Slim port of luna/workers/coding/reviewer.py.
"""
from __future__ import annotations

import contextlib
import functools
import http.server
import json
import os
import py_compile
import re
import socketserver
import threading
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import quote

from blackboard.coding.models import CodingResult, CodingTask, ReviewVerdict
from blackboard.execution.playwright_runner import smoke_check
from blackboard.kernel.json_schema import build_response_format, parse_json_payload, validate_payload
from blackboard.kernel.logger import get_logger
from blackboard.providers.base import AIProvider, Message
from blackboard.providers.registry import ProviderRegistry
from blackboard.react.tools.commands import _lint_check, _run_tests

logger = get_logger("coding.reviewer")

_SEMANTIC_REVIEW_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "overall": {"type": "string", "enum": ["pass", "fail", "needs_revision"]},
        "issues": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
        "suggestions": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
        "verdict_reason": {"type": "string", "maxLength": 600},
    },
    "required": ["overall", "issues", "suggestions", "verdict_reason"],
    "additionalProperties": False,
}


class CodeReviewer:
    """Structural review (lint + pytest) + optional LLM semantic review via the `reviewer` role."""

    def __init__(self, registry: Optional[ProviderRegistry] = None) -> None:
        self._registry = registry

    async def review(
        self,
        task: CodingTask,
        result: CodingResult,
        *,
        cwd: str,
        diff: str = "",
        run_tests: bool = True,
        run_lint: bool = True,
        semantic: bool = False,
    ) -> ReviewVerdict:
        verdict = ReviewVerdict()
        verdict.diff_summary = self.summarize_diff(diff)
        changed_files = self._changed_files(task, result)

        if run_lint:
            await self._run_lint(verdict, cwd=cwd)
        else:
            verdict.lint_clean = True

        await self._run_compile_checks(verdict, changed_files=changed_files, cwd=cwd)

        if run_tests:
            pytest_target = self._pytest_target(task.verification or [])
            if pytest_target is None:
                verdict.tests_passed = True
                verdict.raw_tests = "skipped: no pytest verification step"
            else:
                await self._run_tests(verdict, pytest_target, cwd=cwd)
        else:
            verdict.tests_passed = True

        browser_target = self._browser_target(task.verification or [], changed_files=changed_files, cwd=cwd)
        if browser_target:
            await self._run_browser_check(verdict, target=browser_target, cwd=cwd)

        if semantic and self._registry is not None and diff:
            await self._semantic(verdict, task, result, diff)

        verdict.passed = verdict.lint_clean and verdict.compile_passed and verdict.tests_passed and verdict.runtime_passed
        logger.info(
            "[reviewer] passed=%s lint=%s compile=%s tests=%s runtime=%s",
            verdict.passed, verdict.lint_clean, verdict.compile_passed, verdict.tests_passed, verdict.runtime_passed,
        )
        return verdict

    async def _run_lint(self, verdict: ReviewVerdict, *, cwd: str) -> None:
        try:
            raw = await _lint_check({"path": ".", "cwd": cwd})
            data = json.loads(raw)
            if "error" in data and "no linter available" in str(data.get("error", "")).lower():
                # Treat absence of linter as clean to avoid spurious failures.
                verdict.lint_clean = True
                verdict.raw_lint = "no linter available"
                return
            violations = data.get("violations") or []
            verdict.lint_violations = len(violations)
            verdict.lint_clean = bool(data.get("clean", not violations))
            verdict.raw_lint = (raw or "")[:2000]
            for v in violations[:5]:
                verdict.suggestions.append(f"Lint: {v}"[:400])
        except Exception as exc:
            logger.debug("[reviewer] lint failed: %s", exc)
            verdict.lint_clean = True

    async def _run_compile_checks(self, verdict: ReviewVerdict, *, changed_files: list[str], cwd: str) -> None:
        python_targets = []
        root = Path(cwd).resolve()
        for item in changed_files:
            path = Path(str(item or "").strip())
            if path.suffix.lower() != ".py":
                continue
            resolved = path if path.is_absolute() else (root / path)
            if not resolved.exists() or not resolved.is_file():
                continue
            python_targets.append(resolved)
        if not python_targets:
            verdict.compile_passed = True
            verdict.raw_compile = "skipped: no python files changed"
            return
        failures: list[str] = []
        for target in python_targets:
            try:
                py_compile.compile(str(target), doraise=True)
            except py_compile.PyCompileError as exc:
                failures.append(str(exc)[:500])
            except Exception as exc:
                failures.append(f"{target}: {exc}"[:500])
        verdict.compile_failures = len(failures)
        verdict.compile_passed = not failures
        verdict.raw_compile = "\n".join(failures[:8]) if failures else f"py_compile clean across {len(python_targets)} file(s)"
        for failure in failures[:5]:
            verdict.suggestions.append(f"Compile: {failure}"[:400])

    async def _run_tests(self, verdict: ReviewVerdict, pytest_target: tuple[str, str], *, cwd: str) -> None:
        test_path, extra_args = pytest_target
        try:
            raw = await _run_tests({"path": test_path, "args": extra_args, "cwd": cwd})
            data = json.loads(raw)
            failed = int(data.get("failed", 0) or 0)
            verdict.test_failures = failed
            verdict.tests_passed = failed == 0 and data.get("exit_code", 0) == 0
            verdict.raw_tests = (raw or "")[:2000]
        except Exception as exc:
            logger.debug("[reviewer] tests failed: %s", exc)
            verdict.tests_passed = True

    @staticmethod
    def _pytest_target(verification: list[str]) -> Optional[tuple[str, str]]:
        for step in verification:
            s = str(step or "").strip()
            if "pytest" not in s:
                continue
            parts = s.replace("pytest", "", 1).strip().split(None, 1)
            if parts:
                return parts[0], (parts[1] if len(parts) > 1 else "")
            return ".", ""
        return None

    @classmethod
    def _browser_target(cls, verification: list[str], *, changed_files: list[str], cwd: str) -> str:
        for step in verification:
            s = str(step or "").strip()
            if not s:
                continue
            match = re.match(r"^(?:playwright|browser|browser_smoke|demo)\s+(.+)$", s, flags=re.IGNORECASE)
            if match:
                return match.group(1).strip()
        return cls._infer_browser_target(changed_files=changed_files, cwd=cwd)

    @staticmethod
    def _infer_browser_target(*, changed_files: list[str], cwd: str) -> str:
        root = Path(cwd).resolve()
        if not root.exists():
            return ""
        html_targets: list[str] = []
        web_suffixes = {".html", ".htm", ".css", ".js", ".mjs", ".cjs"}
        saw_web_asset = False
        for item in changed_files:
            raw = str(item or "").strip()
            if not raw:
                continue
            path = Path(raw)
            suffix = path.suffix.lower()
            if suffix not in web_suffixes:
                continue
            saw_web_asset = True
            resolved = path if path.is_absolute() else (root / path)
            resolved = resolved.resolve()
            if suffix in {".html", ".htm"} and resolved.exists() and resolved.is_file():
                try:
                    html_targets.append(resolved.relative_to(root).as_posix())
                except ValueError:
                    html_targets.append(str(resolved))
        if not saw_web_asset or not html_targets:
            return ""
        html_targets.sort(key=lambda item: (0 if Path(item).name.lower() in {"index.html", "demo.html"} else 1, len(item), item.lower()))
        return html_targets[0]

    @staticmethod
    def _changed_files(task: CodingTask, result: CodingResult) -> list[str]:
        changed = [str(p.file or "").strip() for p in list(result.patches or [])]
        changed.extend(str(f.file or "").strip() for f in list(result.new_files or []))
        if not any(changed):
            changed = [str(item or "").strip() for item in list(task.files or [])]
        out: list[str] = []
        seen: set[str] = set()
        for item in changed:
            if not item or item in seen:
                continue
            seen.add(item)
            out.append(item)
        return out

    async def _run_browser_check(self, verdict: ReviewVerdict, *, target: str, cwd: str) -> None:
        try:
            with self._browser_target_url(target=target, cwd=cwd) as url:
                out_dir = Path(cwd) / ".blackboard_review"
                data = await smoke_check(url=url, out_dir=out_dir)
        except Exception as exc:
            logger.debug("[reviewer] browser runtime failed: %s", exc)
            verdict.runtime_passed = True
            verdict.raw_runtime = f"skipped: browser runtime setup failed ({exc})"
            return
        problems = []
        if str(data.get("error") or "").strip():
            problems.append(str(data.get("error") or "").strip())
        problems.extend(str(item or "").strip() for item in list(data.get("console_errors") or []) if str(item or "").strip())
        problems.extend(str(item or "").strip() for item in list(data.get("page_errors") or []) if str(item or "").strip())
        problems.extend(str(item or "").strip() for item in list(data.get("request_failures") or []) if str(item or "").strip())
        verdict.runtime_failures = len(problems)
        verdict.runtime_passed = not problems
        parts = [
            f"target={target}",
            f"url={str(data.get('url') or '')}",
            f"screenshot={str(data.get('path') or '')}",
            f"console_count={int(data.get('console_count') or 0)}",
        ]
        parts.extend(problems[:12])
        verdict.raw_runtime = "\n".join(parts)[:4000]
        for failure in problems[:5]:
            verdict.suggestions.append(f"Runtime: {failure}"[:400])

    @contextlib.contextmanager
    def _browser_target_url(self, *, target: str, cwd: str):
        value = str(target or "").strip()
        if re.match(r"^https?://", value, flags=re.IGNORECASE):
            yield value
            return
        root = Path(cwd).resolve()
        file_path = Path(value)
        resolved = file_path if file_path.is_absolute() else (root / file_path)
        resolved = resolved.resolve()
        if not resolved.exists():
            raise FileNotFoundError(f"browser target not found: {resolved}")
        rel = resolved.relative_to(root).as_posix()
        handler = functools.partial(_ReviewStaticHandler, directory=str(root))
        with socketserver.TCPServer(("127.0.0.1", 0), handler) as server:
            server.allow_reuse_address = True
            port = int(server.server_address[1])
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                yield f"http://127.0.0.1:{port}/{quote(rel)}"
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2.0)

    async def _semantic(self, verdict: ReviewVerdict, task: CodingTask, result: CodingResult, diff: str) -> None:
        if self._registry is None:
            return
        prompt = (
            f"Task objective: {task.objective}\n\n"
            f"Implementation summary: {result.summary}\n\n"
            f"Constraints: {', '.join(task.constraints) or '(none)'}\n\n"
            f"Diff (truncated):\n```diff\n{diff[:4000]}\n```\n\n"
            "Return JSON {\"overall\": \"pass|fail|needs_revision\", \"issues\": [...], "
            "\"suggestions\": [...], \"verdict_reason\": \"...\"}."
        )
        try:
            async def _call(provider: AIProvider):
                kwargs: Dict[str, Any] = {}
                if getattr(getattr(provider, "capabilities", None), "structured_output", False):
                    kwargs["response_format"] = build_response_format(_SEMANTIC_REVIEW_SCHEMA, "semantic_review")
                return await provider.complete(
                    [Message(role="user", content=prompt)],
                    temperature=0.1,
                    max_tokens=600,
                    **kwargs,
                )
            response = await self._registry.call_with_fallback("reviewer", _call)
            data, parse_error = parse_json_payload(response.content)
            if parse_error:
                raise ValueError(parse_error)
            data, validation_error = validate_payload(data, _SEMANTIC_REVIEW_SCHEMA, path="semantic_review")
            if validation_error:
                raise ValueError(validation_error)
            if data.get("overall") == "fail":
                verdict.passed = False
            verdict.suggestions.extend(list(data.get("issues") or []))
            verdict.suggestions.extend(list(data.get("suggestions") or []))
        except Exception as exc:
            logger.debug("[reviewer] semantic review failed: %s", exc)

    @staticmethod
    def summarize_diff(diff: str) -> str:
        if not diff:
            return "No diff available."
        lines = diff.splitlines()
        added = sum(1 for l in lines if l.startswith("+") and not l.startswith("+++"))
        removed = sum(1 for l in lines if l.startswith("-") and not l.startswith("---"))
        files = [l[6:] for l in lines if l.startswith("+++ b/")]
        out = f"+{added} -{removed} lines across {len(files)} file(s)"
        if files:
            out += ": " + ", ".join(files[:5])
        return out


class _ReviewStaticHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return
