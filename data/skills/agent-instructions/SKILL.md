---
name: agent-instructions
description: Generate project-local AGENTS.md instructions from verified project facts and user-provided intent.
tags: [agents, instructions, template, docs]
priority: 90
---

# Agent Instructions Skill

Use this skill when creating or updating `AGENTS.md` for a user project.

## Rules

- Generate instructions from the user's current request, existing files, and verified tool output.
- Do not copy Blackboard's own commands or implementation rules into a user project unless explicitly requested.
- Keep unknowns explicit under `Decisions still unknown`.
- Prefer concise, durable project rules over one-off task notes.
- Include only commands that are supported by actual project files.
- Never include secrets or API key values.

## Include

- Project intent and non-goals.
- Source-of-truth template/spec references.
- Existing files or behaviors to preserve.
- Stack and commands that are known to work or are clearly implied.
- Code style, layout, and safety constraints.
- Verification checks before future agents claim completion.
