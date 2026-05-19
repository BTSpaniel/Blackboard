"""Build the default tool registry — coding-focused, no chat/discord/etc."""
from __future__ import annotations

from typing import List, Optional

from blackboard.react.tool_registry import ToolRegistry
from blackboard.react.tools.agents_md_tool import register_agents_md_tools
from blackboard.react.tools.browser_tools import register_browser_tools
from blackboard.react.tools.commands import register_command_tools
from blackboard.react.tools.file_ops import register_file_tools
from blackboard.react.tools.git_tools import register_git_tools
from blackboard.react.tools.introspection import register_introspection_tools
from blackboard.react.tools.lsp_tools import register_lsp_tools
from blackboard.react.tools.search import register_search_tools
from blackboard.react.tools.web_tools import register_web_tools
from blackboard.react.tools.wiki_tools import register_wiki_tools
from blackboard.coding.skills import register_skill_tools

_ALL_GROUPS = ["file_ops", "search", "web", "browser", "commands", "git", "lsp", "agents_md", "skills", "wiki", "introspection"]


def build_default_registry(
    enabled: Optional[List[str]] = None,
    registry: Optional[ToolRegistry] = None,
) -> ToolRegistry:
    if registry is None:
        registry = ToolRegistry()
    groups = set(enabled) if enabled is not None else set(_ALL_GROUPS)
    if "file_ops" in groups:
        register_file_tools(registry)
    if "search" in groups:
        register_search_tools(registry)
    if "web" in groups:
        register_web_tools(registry)
    if "browser" in groups:
        register_browser_tools(registry)
    if "commands" in groups:
        register_command_tools(registry)
    if "git" in groups:
        register_git_tools(registry)
    if "lsp" in groups:
        register_lsp_tools(registry)
    if "agents_md" in groups:
        register_agents_md_tools(registry)
    if "skills" in groups:
        register_skill_tools(registry)
    if "wiki" in groups:
        register_wiki_tools(registry)
    if "introspection" in groups:
        register_introspection_tools(registry)
    return registry
