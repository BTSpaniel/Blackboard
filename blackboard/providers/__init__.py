"""Provider layer — pluggable AI brains and coding hands."""
from __future__ import annotations

from blackboard.providers.base import (
    AIProvider,
    ProviderCapabilities,
    ProviderHealth,
    ProviderError,
    PlanInput,
    PlanOutput,
    TaskInput,
    TaskOutput,
    ReviewInput,
    ReviewOutput,
    ExecuteInput,
    ExecuteOutput,
    Message,
)


def __getattr__(name: str):
    if name in {"ProviderRegistry", "init_provider_registry", "get_provider_registry"}:
        from blackboard.providers.registry import ProviderRegistry, get_provider_registry, init_provider_registry

        mapping = {
            "ProviderRegistry": ProviderRegistry,
            "init_provider_registry": init_provider_registry,
            "get_provider_registry": get_provider_registry,
        }
        return mapping[name]
    raise AttributeError(name)

__all__ = [
    "AIProvider",
    "ProviderCapabilities",
    "ProviderHealth",
    "ProviderError",
    "PlanInput",
    "PlanOutput",
    "TaskInput",
    "TaskOutput",
    "ReviewInput",
    "ReviewOutput",
    "ExecuteInput",
    "ExecuteOutput",
    "Message",
    "ProviderRegistry",
    "init_provider_registry",
    "get_provider_registry",
]
