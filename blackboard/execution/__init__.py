"""Execution layer — terminal sessions, dev-server preview runner, Playwright screenshots."""
from blackboard.execution.terminal import TerminalManager, TerminalSession, get_terminal_manager
from blackboard.execution.preview import PreviewManager, PreviewSession, get_preview_manager
from blackboard.execution.playwright_runner import capture_screenshot

__all__ = [
    "TerminalManager",
    "TerminalSession",
    "get_terminal_manager",
    "PreviewManager",
    "PreviewSession",
    "get_preview_manager",
    "capture_screenshot",
]
