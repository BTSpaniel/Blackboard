---
name: vanilla-js-ui
description: Work on Blackboard's vanilla JavaScript UI without introducing a frontend build step.
tags: [ui, javascript, frontend]
priority: 70
---

# Vanilla JS UI Skill

Use this skill for Blackboard UI changes.

## Rules

- Blackboard's own UI is vanilla browser JavaScript served from `ui/`.
- Do not introduce React, Vue, npm, bundlers, or build steps unless explicitly requested.
- Preserve custom element patterns and existing dark theme tokens.
- Keep API calls aligned with backend routes.
- Update UI state after WebSocket events and direct API mutations where appropriate.

## Verification

- Prefer browser/manual smoke checks for UI behavior.
- Run Python/API tests when backend contracts changed.
