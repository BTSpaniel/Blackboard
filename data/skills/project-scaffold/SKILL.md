---
name: project-scaffold
description: Create or update a project scaffold from the user's live template/spec without assuming a stack.
tags: [project, scaffold, template, greenfield]
priority: 100
---

# Project Scaffold Skill

Use this skill when creating a new project, restructuring a near-empty project, or turning a user-provided template into files.

## Rules

- Treat the user's live request, pasted template, card context, and task context as the source of truth.
- Search existing files before creating structure.
- Do not assume framework, package manager, license, deployment target, or folder layout.
- Ask one focused question if the project type or stack is genuinely unclear.
- Create the smallest complete structure that satisfies the request.
- Include only dependencies actually used by generated code.
- Generate setup, run, test, and build commands from the actual files created.
- If a command is unknown, record it as unknown instead of inventing it.

## Required output for new projects

- A project-local `AGENTS.md` built from known facts.
- A README that describes the actual project and commands.
- Runnable source files matching the requested template.
- Tests or verification instructions appropriate to the generated stack.

## Project-local AGENTS.md shape

```markdown
# [Project Name] — Agent Instructions

## Project intent

- What this project is for:
- Primary user goal:
- Explicit non-goals:

## Source of truth

- User-provided template/spec:
- Existing files to preserve:
- Decisions still unknown:

## Stack and commands

- Runtime:
- Package/dependency command:
- Test command:
- Run/dev command:
- Build command:

## Code conventions

- Language/style:
- File layout:
- Error handling/logging:
- UI/design requirements:

## Safety and constraints

- Secrets/config:
- Files or behavior not to change:
- External services/APIs:

## Verification

- Required checks before done:
- Manual acceptance criteria:
```
