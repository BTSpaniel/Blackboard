"""Provider registry — resolves roles to providers with fallback + health tracking."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional

from blackboard.kernel.logger import describe_error, get_logger
from blackboard.governors.budget import get_budget_governor
from blackboard.governors.health import get_health_governor
from blackboard.providers.anthropic_provider import AnthropicProvider
from blackboard.providers.base import (
    AIProvider,
    ProviderCapabilities,
    ProviderError,
    ProviderHealth,
)
from blackboard.providers.claude_code_cli import ClaudeCodeCLIProvider
from blackboard.providers.llama_cpp_python_provider import LlamaCppPythonProvider
from blackboard.providers.openai_codex_cli import OpenAICodexCLIProvider
from blackboard.providers.openai_compat import OpenAICompatProvider
from blackboard.providers.openai_provider import OpenAIProvider
from blackboard.providers.secrets import configure as configure_secrets, resolve as resolve_secret
from blackboard.providers.usage import record_provider_usage
import os

logger = get_logger("providers.registry")

_CLOSE_TIMEOUT_S = 3.0


def _describe_secret(profile: Dict[str, Any]) -> Dict[str, Any]:
    """Report key-detection status for a profile WITHOUT exposing the value.

    Resolution order matches the runtime path:
      1. inline ``api_key:`` literal in config.yaml (Luna-style)
      2. env var named in ``providers.secrets[secret_id].env``
      3. OS keyring under ``providers.secrets[secret_id].keyring``
      4. ``BLACKBOARD_<SECRET_ID>`` fallback env var

    Returns ``{required, secret_id, env, env_set, has_value, source}``.
    """
    secret_id = str(profile.get("api_key_secret") or "").strip()
    inline_key = str(profile.get("api_key") or "").strip()

    if not secret_id and not inline_key:
        # Local model / no key required.
        return {"required": False, "secret_id": "", "env": "", "env_set": False, "has_value": True, "source": "none"}

    # Inline literal beats everything else — Luna pattern.
    if inline_key:
        return {
            "required": True,
            "secret_id": secret_id,
            "env": "",
            "env_set": False,
            "has_value": True,
            "source": "inline",
        }

    # Look up the configured env var name from the secrets registry without resolving.
    from blackboard.providers.secrets import _registry as _secrets_registry
    record = _secrets_registry.get(secret_id, {}) if isinstance(_secrets_registry, dict) else {}
    env_name = str(record.get("env") or "").strip()
    keyring_name = str(record.get("keyring") or "").strip()

    env_set = bool(env_name and os.environ.get(env_name))
    fallback_env = f"BLACKBOARD_{secret_id.upper()}"
    fallback_set = bool(os.environ.get(fallback_env))
    # `resolve()` checks env then keyring then BLACKBOARD_* fallback. Boolean only.
    has_value = bool(resolve_secret(secret_id))
    source = (
        "env" if env_set else
        "keyring" if (keyring_name and has_value and not env_set and not fallback_set) else
        "fallback_env" if fallback_set else
        "missing"
    )
    return {
        "required": True,
        "secret_id": secret_id,
        "env": env_name,
        "env_set": env_set,
        "has_value": has_value,
        "source": source,
    }


def _profile_models(profile: Dict[str, Any]) -> List[str]:
    raw = profile.get("models") or []
    if isinstance(raw, str):
        raw = [raw]
    models = [str(model).strip() for model in raw if str(model).strip()]
    selected = str(profile.get("model") or "").strip()
    if selected and selected not in models:
        models.insert(0, selected)
    return models


def _profile_enabled(profile: Dict[str, Any]) -> bool:
    value = profile.get("enabled")
    if value is None:
        return True
    return bool(value)


@dataclass
class RoleAssignment:
    role: str
    profile: str
    fallbacks: List[str] = field(default_factory=list)
    disabled: List[str] = field(default_factory=list)  # profile ids skipped at runtime even if listed


def _instantiate(provider_id: str, profile: Dict[str, Any]) -> AIProvider:
    adapter = str(profile.get("adapter") or "openai_compat").strip().lower()
    capabilities = ProviderCapabilities.from_list(list(profile.get("capabilities") or []))
    common = {
        "provider_id": provider_id,
        "endpoint": str(profile.get("endpoint") or "https://api.openai.com/v1"),
        "model": str(profile.get("model") or ""),
        "api_key_secret": str(profile.get("api_key_secret") or ""),
        "api_key": str(profile.get("api_key") or ""),  # Luna-style inline literal key
        "timeout": float(profile.get("timeout") or 120.0),
        "max_retries": int(profile.get("max_retries") or 3),
        "capabilities": capabilities,
    }
    if adapter == "openai":
        return OpenAIProvider(
            **common,
            use_responses_api=bool(profile.get("use_responses_api") or False),
        )
    if adapter == "anthropic":
        return AnthropicProvider(**common)
    if adapter == "claude_code_cli":
        return ClaudeCodeCLIProvider(
            provider_id=provider_id,
            bin=str(profile.get("bin") or "claude"),
            capabilities=capabilities,
            extra_args=list(profile.get("extra_args") or []),
        )
    if adapter == "openai_codex_cli":
        return OpenAICodexCLIProvider(
            provider_id=provider_id,
            bin=str(profile.get("bin") or "codex"),
            model=str(profile.get("model") or "gpt-5.5"),
            capabilities=capabilities,
            extra_args=list(profile.get("extra_args") or []),
            sandbox=str(profile.get("sandbox") or "workspace-write"),
            ephemeral=bool(profile.get("ephemeral", True)),
            skip_git_repo_check=bool(profile.get("skip_git_repo_check") or False),
            approval_policy=str(profile.get("approval_policy") or ""),
        )
    if adapter == "llama_cpp_python":
        return LlamaCppPythonProvider(
            provider_id=provider_id,
            model=str(profile.get("model") or provider_id),
            model_path=str(profile.get("model_path") or ""),
            model_path_env=str(profile.get("model_path_env") or ""),
            timeout=float(profile.get("timeout") or 120.0),
            max_retries=int(profile.get("max_retries") or 1),
            capabilities=capabilities,
            n_ctx=int(profile.get("n_ctx") or 4096),
            n_threads=int(profile.get("n_threads") or 4),
            n_gpu_layers=int(profile.get("n_gpu_layers") or 0),
            chat_format=str(profile.get("chat_format") or ""),
            verbose=bool(profile.get("verbose") or False),
        )
    # default — openai_compat
    return OpenAICompatProvider(**common)


class ProviderRegistry:
    def __init__(
        self,
        profiles: Dict[str, Dict[str, Any]],
        roles: Dict[str, Dict[str, Any]],
        *,
        audit_hook: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        self._profiles = dict(profiles or {})
        self._roles: Dict[str, RoleAssignment] = {}
        for role, spec in (roles or {}).items():
            self._roles[role] = RoleAssignment(
                role=role,
                profile=str((spec or {}).get("profile") or ""),
                fallbacks=list((spec or {}).get("fallbacks") or []),
                disabled=list((spec or {}).get("disabled") or []),
            )
        self._providers: Dict[str, AIProvider] = {}
        self._health: Dict[str, ProviderHealth] = {}
        self._unhealthy_until: Dict[str, float] = {}
        self._audit = audit_hook
        for profile_id, profile in self._profiles.items():
            if not _profile_enabled(profile):
                continue
            try:
                self._providers[profile_id] = _instantiate(profile_id, profile)
            except Exception as exc:
                logger.warning("Failed to instantiate provider %s: %s", profile_id, exc)

    # ── Introspection ────────────────────────────────────────────

    def provider(self, profile_id: str) -> Optional[AIProvider]:
        return self._providers.get(profile_id)

    def list_profiles(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for pid, profile in self._profiles.items():
            provider = self._providers.get(pid)
            health = self._health.get(pid)
            out.append({
                "id": pid,
                "type": (profile.get("type") or "llm_api"),
                "adapter": profile.get("adapter") or "",
                "model": profile.get("model") or "",
                "models": _profile_models(profile),
                "endpoint": profile.get("endpoint") or "",
                "capabilities": list(profile.get("capabilities") or []),
                "enabled": _profile_enabled(profile),
                "ok": bool(health.ok) if health else None,
                "latency_ms": int(health.latency_ms) if health else None,
                "error": str(health.error) if health and health.error else "",
                "available": provider is not None,
                "secret_status": _describe_secret(profile),
            })
        return out

    def health_snapshot(self) -> Dict[str, ProviderHealth]:
        return dict(self._health)

    def update_profile_model(self, profile_id: str, model: str, models: Optional[List[str]] = None) -> Dict[str, Any]:
        profile_id = str(profile_id or "").strip()
        model = str(model or "").strip()
        if not profile_id:
            raise ValueError("profile id is required")
        if profile_id not in self._profiles:
            raise ValueError(f"unknown profile id: {profile_id}")
        if not model:
            raise ValueError("model is required")
        profile = self._profiles[profile_id]
        if models is not None:
            clean_models = [str(item).strip() for item in models if str(item).strip()]
            if model not in clean_models:
                clean_models.insert(0, model)
            profile["models"] = clean_models
        elif model not in _profile_models(profile):
            profile["models"] = [model, *_profile_models(profile)]
        profile["model"] = model
        provider = self._providers.get(profile_id)
        setter = getattr(provider, "set_model", None)
        if callable(setter):
            setter(model)
        elif provider is not None:
            provider.model = model
        return next((p for p in self.list_profiles() if p["id"] == profile_id), {})

    async def refresh_profile_models(self, profile_id: str) -> Dict[str, Any]:
        profile_id = str(profile_id or "").strip()
        if profile_id not in self._profiles:
            raise ValueError(f"unknown profile id: {profile_id}")
        provider = self._providers.get(profile_id)
        if provider is None:
            raise ValueError(f"provider unavailable: {profile_id}")
        try:
            models = await provider.list_models()
        except Exception as exc:
            logger.warning("Failed to refresh models for %s, using configured models: %s", profile_id, describe_error(exc))
            models = _profile_models(self._profiles[profile_id])
        if not models:
            models = _profile_models(self._profiles[profile_id])
        current = str(self._profiles[profile_id].get("model") or getattr(provider, "model", "") or "").strip()
        if current and current not in models:
            models.insert(0, current)
        self._profiles[profile_id]["models"] = models
        return {"id": profile_id, "model": current, "models": models}

    def list_roles(self) -> Dict[str, Dict[str, Any]]:
        return {
            role: {
                "profile": assignment.profile,
                "fallbacks": list(assignment.fallbacks),
                "disabled": list(assignment.disabled),
            }
            for role, assignment in self._roles.items()
        }

    def update_role(
        self,
        role: str,
        profile: str,
        fallbacks: List[str],
        *,
        disabled: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Reorder a role's primary + fallback chain.

        ``disabled`` is an optional list of profile ids in the chain that should be
        skipped at runtime resolution (kept in the chain for visibility, but no calls).

        Validates that every referenced profile exists. Raises ``ValueError`` on bad input.
        Returns the new assignment dict ``{role, profile, fallbacks, disabled}``.
        """
        role = str(role or "").strip()
        profile = str(profile or "").strip()
        if not role:
            raise ValueError("role is required")
        if not profile:
            raise ValueError("primary profile is required")
        cleaned_fallbacks: List[str] = []
        seen = {profile}
        for f in (fallbacks or []):
            f = str(f or "").strip()
            if not f or f in seen:
                continue
            cleaned_fallbacks.append(f)
            seen.add(f)
        # Validate that every referenced profile actually exists.
        unknown = [pid for pid in [profile, *cleaned_fallbacks] if pid not in self._profiles]
        if unknown:
            raise ValueError(f"unknown profile id(s): {', '.join(unknown)}")
        chain_set = {profile, *cleaned_fallbacks}
        cleaned_disabled = [d for d in (disabled or []) if d in chain_set]
        self._roles[role] = RoleAssignment(
            role=role,
            profile=profile,
            fallbacks=cleaned_fallbacks,
            disabled=cleaned_disabled,
        )
        return {
            "role": role,
            "profile": profile,
            "fallbacks": cleaned_fallbacks,
            "disabled": cleaned_disabled,
        }

    def auto_fill_role(self, role: str) -> Dict[str, Any]:
        """Rebuild the role's chain from currently-detected providers (those with API keys).

        Preserves the relative order of any detected providers already in the chain;
        appends any newly-detected providers not yet present; drops profiles with no key
        or failing health from the role chain.
        """
        role = str(role or "").strip()
        if not role:
            raise ValueError("role is required")
        existing = self._roles.get(role)
        existing_chain: List[str] = []
        if existing:
            existing_chain = [existing.profile, *existing.fallbacks] if existing.profile else []

        # A provider is "verified" only when BOTH (a) we have a usable API key
        # for it AND (b) the most recent health probe says it's reachable.
        # Anything else (missing key, never probed, last probe failed) gets
        # parked in `disabled` so it stays visible in the UI but the runtime
        # never tries to call it.
        verified: List[str] = []
        unverified: List[str] = []
        for pid, profile in self._profiles.items():
            status = _describe_secret(profile)
            has_key = bool(status.get("has_value"))
            health = self._health.get(pid)
            healthy = bool(health and health.ok)
            if has_key and healthy:
                verified.append(pid)
            else:
                unverified.append(pid)

        # Preserve user's existing order for verified providers; append any
        # newly-detected ones to the end so explicit reordering is respected.
        ordered_verified = [pid for pid in existing_chain if pid in verified]
        for pid in verified:
            if pid not in ordered_verified:
                ordered_verified.append(pid)
        if not ordered_verified:
            raise ValueError(
                "no verified providers available — set an API key and ensure the health "
                "probe succeeds, then try auto-fill again"
            )

        disabled = [pid for pid in existing_chain if pid in unverified]
        new_chain = [*ordered_verified]
        for pid in disabled:
            if pid not in new_chain:
                new_chain.append(pid)
        primary, *fallbacks = new_chain
        return self.update_role(role, primary, fallbacks, disabled=disabled)

    def auto_fill_all_roles(self) -> Dict[str, Dict[str, Any]]:
        """Run :meth:`auto_fill_role` across every configured role.

        Returns ``{role: result_or_error}`` so the UI can show per-role status
        in one trip. Errors are reported as ``{"error": "..."}`` for that role
        rather than raising — so a single missing-everything role doesn't block
        the rest from being reset.
        """
        out: Dict[str, Dict[str, Any]] = {}
        for role in list(self._roles.keys()):
            try:
                out[role] = self.auto_fill_role(role)
            except ValueError as exc:
                out[role] = {"error": str(exc)}
        return out

    def role_profile_id(self, role: str) -> str:
        return self._roles.get(role, RoleAssignment(role=role, profile="")).profile

    # ── Role resolution ─────────────────────────────────────────

    def get(self, role: str) -> AIProvider:
        """Return the highest-priority *currently healthy* provider for ``role``.

        Falls back through ``fallbacks`` if the primary is unhealthy or unavailable.
        Raises ``ProviderError`` if nothing is reachable.
        """
        chain = self._role_chain(role)
        if not chain:
            raise ProviderError(f"No providers configured for role '{role}'", retryable=False)
        now = time.monotonic()
        for profile_id in chain:
            provider = self._providers.get(profile_id)
            if provider is None:
                continue
            penalty_until = self._unhealthy_until.get(profile_id, 0.0)
            if penalty_until > now:
                continue
            return provider
        # All on cooldown — pick the first available and let the caller retry.
        for profile_id in chain:
            provider = self._providers.get(profile_id)
            if provider is not None:
                return provider
        raise ProviderError(f"No providers available for role '{role}'", retryable=False)

    def _role_chain(self, role: str, *, include_disabled: bool = False) -> List[str]:
        """Return the active fallback chain for ``role``.

        Skips:
          - profiles flagged as ``disabled`` for this role
          - profiles whose API key is required but not detected (auto-skip)

        Set ``include_disabled=True`` to get the raw declared chain (used by
        ``list_roles`` for UI display, not by runtime calls).
        """
        assignment = self._roles.get(role)
        if not assignment:
            return []
        raw: List[str] = []
        if assignment.profile:
            raw.append(assignment.profile)
        for fb in assignment.fallbacks:
            if fb and fb not in raw:
                raw.append(fb)
        if include_disabled:
            return raw
        disabled_set = set(assignment.disabled or [])
        out: List[str] = []
        for pid in raw:
            if pid in disabled_set:
                continue
            profile = self._profiles.get(pid)
            if profile is None:
                continue
            if not _profile_enabled(profile):
                continue
            # Skip profiles that require a key but don't have one detected.
            status = _describe_secret(profile)
            if status.get("required") and not status.get("has_value"):
                continue
            out.append(pid)
        return out

    # ── Fallback executor ────────────────────────────────────────

    async def call_with_fallback(
        self,
        role: str,
        fn: Callable[[AIProvider], Awaitable[Any]],
    ) -> Any:
        """Call ``fn(provider)`` walking the role's fallback chain on retryable failures.

        Records audit events on each swap, and tracks token usage on the responding provider.
        """
        chain = self._role_chain(role)
        if not chain:
            raise ProviderError(f"No providers configured for role '{role}'", retryable=False)
        last_error: Optional[Exception] = None
        now = time.monotonic()
        for idx, profile_id in enumerate(chain):
            provider = self._providers.get(profile_id)
            if provider is None:
                continue
            if self._unhealthy_until.get(profile_id, 0.0) > now:
                continue
            try:
                session_id = f"{role}:{profile_id}"
                get_budget_governor().enforce(user_id="default", session_id=session_id)
                call_started = time.monotonic()
                result = await fn(provider)
                call_duration_ms = (time.monotonic() - call_started) * 1000
                skip_health_accounting = bool(
                    result.get("_skip_health_accounting") if isinstance(result, dict) else getattr(result, "_skip_health_accounting", False)
                )
                should_record_usage = (
                    not bool(getattr(result, "_skip_usage_accounting", False))
                    and any(hasattr(result, attr) for attr in ("tokens_prompt", "tokens_completion", "model", "raw"))
                )
                if should_record_usage:
                    prompt = int(getattr(result, "tokens_prompt", 0) or 0)
                    completion = int(getattr(result, "tokens_completion", 0) or 0)
                    record_provider_usage(
                        profile_id=profile_id,
                        role=role,
                        prompt_tokens=prompt,
                        completion_tokens=completion,
                        model=str(getattr(result, "model", "") or provider.model),
                        user_id="default",
                        session_id=session_id,
                    )
                if not skip_health_accounting:
                    get_health_governor().record_call(f"provider:{profile_id}", success=True, duration_ms=call_duration_ms)
                if idx > 0:
                    self._audit_event({
                        "kind": "provider.fallback_recovered",
                        "role": role,
                        "from": chain[0],
                        "to": profile_id,
                    })
                return result
            except ProviderError as exc:
                last_error = exc
                get_health_governor().record_call(f"provider:{profile_id}", success=False)
                if exc.rate_limited and not exc.retryable:
                    raise
                if not exc.retryable and not exc.rate_limited:
                    raise
                self._mark_unhealthy(profile_id, retryable=True, rate_limited=exc.rate_limited)
                self._audit_event({
                    "kind": "provider.fallback_swap",
                    "role": role,
                    "from": profile_id,
                    "reason": describe_error(exc),
                })
            except Exception as exc:
                last_error = exc
                get_health_governor().record_call(f"provider:{profile_id}", success=False)
                self._mark_unhealthy(profile_id, retryable=True, rate_limited=False)
                self._audit_event({
                    "kind": "provider.fallback_swap",
                    "role": role,
                    "from": profile_id,
                    "reason": describe_error(exc),
                })
        if last_error:
            raise last_error
        raise ProviderError(f"All providers exhausted for role '{role}'", retryable=False)

    async def stream_with_fallback(
        self,
        role: str,
        fn: Callable[[AIProvider], AsyncIterator[Any]],
    ) -> AsyncIterator[Any]:
        chain = self._role_chain(role)
        if not chain:
            raise ProviderError(f"No providers configured for role '{role}'", retryable=False)
        last_error: Optional[Exception] = None
        now = time.monotonic()
        for idx, profile_id in enumerate(chain):
            provider = self._providers.get(profile_id)
            if provider is None:
                continue
            if self._unhealthy_until.get(profile_id, 0.0) > now:
                continue
            committed = False
            prompt_tokens = 0
            completion_tokens = 0
            stream_model = str(getattr(provider, "model", "") or "default")
            skip_usage_accounting = False
            skip_health_accounting = False
            try:
                session_id = f"{role}:{profile_id}"
                get_budget_governor().enforce(user_id="default", session_id=session_id)
                call_started = time.monotonic()
                async for chunk in fn(provider):
                    if isinstance(chunk, dict) and bool(chunk.get("skip_usage_accounting")):
                        skip_usage_accounting = True
                        continue
                    if isinstance(chunk, dict) and bool(chunk.get("skip_health_accounting")):
                        skip_health_accounting = True
                        continue
                    if isinstance(chunk, dict) and str(chunk.get("type") or "") == "usage":
                        prompt_tokens = int(chunk.get("prompt_tokens") or prompt_tokens or 0)
                        completion_tokens = int(chunk.get("completion_tokens") or completion_tokens or 0)
                        stream_model = str(chunk.get("model") or stream_model or "default")
                        continue
                    if not isinstance(chunk, dict):
                        committed = True
                    yield chunk
                call_duration_ms = (time.monotonic() - call_started) * 1000
                if not skip_usage_accounting:
                    record_provider_usage(
                        profile_id=profile_id,
                        role=role,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        model=stream_model,
                        user_id="default",
                        session_id=session_id,
                    )
                if not skip_health_accounting:
                    get_health_governor().record_call(f"provider:{profile_id}", success=True, duration_ms=call_duration_ms)
                if idx > 0:
                    self._audit_event({
                        "kind": "provider.fallback_recovered",
                        "role": role,
                        "from": chain[0],
                        "to": profile_id,
                    })
                return
            except ProviderError as exc:
                last_error = exc
                get_health_governor().record_call(f"provider:{profile_id}", success=False)
                if committed or (not exc.retryable and not exc.rate_limited):
                    raise
                self._mark_unhealthy(profile_id, retryable=True, rate_limited=exc.rate_limited)
                self._audit_event({
                    "kind": "provider.fallback_swap",
                    "role": role,
                    "from": profile_id,
                    "reason": describe_error(exc),
                })
            except Exception as exc:
                last_error = exc
                get_health_governor().record_call(f"provider:{profile_id}", success=False)
                if committed:
                    raise
                self._mark_unhealthy(profile_id, retryable=True, rate_limited=False)
                self._audit_event({
                    "kind": "provider.fallback_swap",
                    "role": role,
                    "from": profile_id,
                    "reason": describe_error(exc),
                })
        if last_error:
            raise last_error
        raise ProviderError(f"All providers exhausted for role '{role}'", retryable=False)

    def _mark_unhealthy(self, profile_id: str, *, retryable: bool, rate_limited: bool) -> None:
        cooldown = 30.0 if rate_limited else 8.0
        self._unhealthy_until[profile_id] = time.monotonic() + cooldown

    def _audit_event(self, payload: Dict[str, Any]) -> None:
        if self._audit:
            try:
                self._audit(payload)
            except Exception:
                pass

    # ── Health pings ─────────────────────────────────────────────

    async def health_check_all(self) -> Dict[str, ProviderHealth]:
        results: Dict[str, ProviderHealth] = {}
        for pid, provider in self._providers.items():
            try:
                results[pid] = await provider.health()
            except Exception as exc:
                results[pid] = ProviderHealth(ok=False, error=describe_error(exc, "health failed"))
            self._health[pid] = results[pid]
            if results[pid].ok:
                penalty_until = self._unhealthy_until.get(pid, 0.0)
                if penalty_until <= time.monotonic():
                    self._unhealthy_until.pop(pid, None)
        return results

    async def close(self) -> None:
        for provider in self._providers.values():
            try:
                await asyncio.wait_for(provider.close(), timeout=_CLOSE_TIMEOUT_S)
            except Exception:
                pass
        self._health.clear()
        self._unhealthy_until.clear()


# ── Singleton helpers ────────────────────────────────────────────


_registry: Optional[ProviderRegistry] = None


def init_provider_registry(
    config: Dict[str, Any],
    *,
    audit_hook: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> ProviderRegistry:
    """Build the registry from the ``providers`` section of config.yaml."""
    global _registry
    section = dict(config or {})
    configure_secrets(section.get("secrets") or {})
    _registry = ProviderRegistry(
        profiles=section.get("profiles") or {},
        roles=section.get("roles") or {},
        audit_hook=audit_hook,
    )
    return _registry


def get_provider_registry() -> ProviderRegistry:
    if _registry is None:
        raise RuntimeError("provider registry not initialized")
    return _registry
