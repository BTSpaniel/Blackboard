---
name: debugging
description: Debug failures by finding root cause, adding evidence, and validating targeted fixes.
tags: [debugging, tests, root-cause]
priority: 80
---

# Debugging Skill

Use this skill for broken tests, runtime errors, regressions, or unexpected behavior.

## Procedure

1. Reproduce or inspect the exact failure path.
2. Identify the smallest authoritative code path responsible.
3. Prefer root-cause fixes over symptom handling.
4. Add or update focused tests when practical.
5. Run the narrowest useful validation first, then a broader regression slice if shared code changed.

## Guardrails

- Do not rewrite unrelated code.
- Do not delete existing behavior without explicit approval.
- Do not claim success unless the relevant verification ran or the reason it could not run is documented.
