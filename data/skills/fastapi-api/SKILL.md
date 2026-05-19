---
name: fastapi-api
description: Add or modify FastAPI endpoints using Blackboard's existing router and state patterns.
tags: [fastapi, api, backend]
priority: 70
---

# FastAPI API Skill

Use this skill for Blackboard backend API work.

## Patterns

- Add routers under `blackboard/api/` and include them in `blackboard/main.py`.
- Use Pydantic `BaseModel` request bodies for structured inputs.
- Resolve shared services from `request.app.state`.
- Raise `HTTPException` with clear status codes for missing resources.
- Keep endpoint logic thin; put durable state logic in dedicated modules.

## Verification

- Add TestClient or direct unit tests for new routes.
- Run focused API tests and `py_compile` on touched modules.
