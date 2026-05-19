"""Receipts service — auto-triggered when a card transitions to Done.

Calls the `summarizer` provider (with `presenter` fallback) to produce a one-paragraph
markdown receipt and persists it via ``ProjectMemory.write_receipt``.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from blackboard.kernel.logger import get_logger
from blackboard.providers.base import AIProvider, Message, ProviderError
from blackboard.providers.registry import ProviderRegistry
from blackboard.workspace.memory import ProjectMemory

logger = get_logger("workspace.receipts")


class ReceiptsService:
    """One per app. Owns the summarizer call + receipt write."""

    def __init__(
        self,
        registry: ProviderRegistry,
        *,
        memory_factory,
    ) -> None:
        self._registry = registry
        self._memory_factory = memory_factory  # callable(project_id) -> ProjectMemory

    async def write_for_card(
        self,
        *,
        project_id: str,
        card: Dict[str, Any],
        job_summary: str = "",
        diff_summary: str = "",
    ) -> Optional[str]:
        """Generate and store a markdown receipt for a Done card. Returns the body, or None on failure."""
        memory: ProjectMemory = self._memory_factory(project_id)
        prompt = self._build_prompt(card=card, job_summary=job_summary, diff_summary=diff_summary)

        async def _call(provider: AIProvider):
            return await provider.complete(
                [Message(role="user", content=prompt)],
                temperature=0.2,
                max_tokens=600,
            )

        try:
            response = await self._registry.call_with_fallback("summarizer", _call)
        except ProviderError as exc:
            logger.debug("[receipts] summarizer unavailable: %s — falling back to template", exc)
            body = self._fallback_receipt(card, job_summary, diff_summary)
        else:
            body = (response.content or "").strip() or self._fallback_receipt(card, job_summary, diff_summary)

        markdown = self._format_markdown(card, body)
        try:
            memory.write_receipt(card["id"], markdown)
            return markdown
        except Exception as exc:
            logger.warning("[receipts] write failed: %s", exc)
            return None

    # ── Prompt + fallback ────────────────────────────────────────

    @staticmethod
    def _build_prompt(*, card: Dict[str, Any], job_summary: str, diff_summary: str) -> str:
        files = ", ".join(card.get("files") or []) or "(none listed)"
        verification = ", ".join(card.get("verification") or []) or "(none listed)"
        return (
            "Write a concise, factual receipt for a completed coding card. "
            "One paragraph, plain markdown. Include: what changed, which files, how it was verified, "
            "and any follow-up the user should know.\n\n"
            f"Title: {card.get('title', '')}\n"
            f"Body: {card.get('body', '')}\n"
            f"Files: {files}\n"
            f"Verification: {verification}\n"
            f"Job summary: {job_summary or '(none)'}\n"
            f"Diff summary: {diff_summary or '(none)'}\n"
        )

    @staticmethod
    def _fallback_receipt(card: Dict[str, Any], job_summary: str, diff_summary: str) -> str:
        files = card.get("files") or []
        files_str = ", ".join(files) if files else "no files listed"
        return (
            f"Completed: **{card.get('title', '(untitled)')}**. "
            f"Touched: {files_str}. "
            f"Verification: {', '.join(card.get('verification') or []) or '(none)'}. "
            f"{job_summary or diff_summary or 'No job output captured.'}"
        )

    @staticmethod
    def _format_markdown(card: Dict[str, Any], body: str) -> str:
        return (
            f"# Receipt — {card.get('title', '(untitled)')}\n\n"
            f"**Card:** `{card.get('id', '')}` · **Status:** done\n\n"
            f"{body}\n"
        )
