"""Tiny asyncio pub/sub bus."""
from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any, Awaitable, Callable, Dict, List

from blackboard.kernel.logger import get_logger

logger = get_logger("kernel.bus")

Handler = Callable[[str, Dict[str, Any]], Awaitable[None] | None]


class Bus:
    """In-process pub/sub. Topics are dotted strings; subscribers are async or sync callables."""

    def __init__(self) -> None:
        self._subs: Dict[str, List[Handler]] = defaultdict(list)
        self._wildcard: List[Handler] = []
        self._lock = asyncio.Lock()

    def subscribe(self, topic: str, handler: Handler) -> Callable[[], None]:
        if topic == "*":
            self._wildcard.append(handler)
            def _unsub_wc() -> None:
                try:
                    self._wildcard.remove(handler)
                except ValueError:
                    pass
            return _unsub_wc
        self._subs[topic].append(handler)
        def _unsub() -> None:
            try:
                self._subs[topic].remove(handler)
            except ValueError:
                pass
        return _unsub

    async def emit(self, topic: str, payload: Dict[str, Any] | None = None) -> None:
        payload = payload or {}
        handlers = list(self._subs.get(topic, [])) + list(self._wildcard)
        if not handlers:
            return
        for handler in handlers:
            try:
                result = handler(topic, payload)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                logger.debug("[bus] handler error on %s: %s", topic, exc)


_bus: Bus | None = None


def get_bus() -> Bus:
    global _bus
    if _bus is None:
        _bus = Bus()
    return _bus


def reset_bus() -> None:
    """Test-only helper."""
    global _bus
    _bus = None
