---
name: provider-integration
description: Modify Blackboard provider integrations while preserving fallback, cooldown, health, and secret handling behavior.
tags: [providers, api, secrets, fallback]
priority: 75
---

# Provider Integration Skill

Use this skill for work under `blackboard/providers/`, provider APIs, role chains, keys, models, and provider UI settings.

## Rules

- Resolve secrets through configured secret IDs or inline Settings overrides, never hardcode API keys.
- Preserve provider fallback behavior and role priority chains.
- Treat rate limits and quota errors as retryable when fallback should continue.
- Do not trigger excessive external health probes from UI refreshes or WebSocket connects.
- Keep offline or missing-key providers visible for configuration, but skip them at runtime when unusable.

## Verification

- Run provider fallback tests after provider registry or API changes.
- Include tests for key persistence, disabled role entries, cooldowns, and health probe throttling when touched.
