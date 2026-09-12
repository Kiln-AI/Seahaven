# ProjectTracker

A Seahaven world: a fictional issue tracker for a fictional company, and the reference world the
framework is developed against. Nothing here mimics a real product's names, schema or error text.

**This is the placeholder slice.** It is a real, buildable, servable package — a schema, a tool, the
error shapes, the error wrapper and one fixture — and it is deliberately not the whole tracker. The
full world (`specs/projects/seahaven_framework/components/projecttracker.md`) arrives in Phase 10 of
the implementation plan and extends what is here rather than replacing it.

## What it has today

| | |
|---|---|
| Schema | `users(id, email, name, role, created_at)` |
| Tools | `ping(message="pong")` — the message back, the tracker's time, and the number of users |
| Errors | `NOT_FOUND`, `INVALID_INPUT`, `CONFLICT`, `INTERNAL` (`src/projecttracker/errors.py`) |
| Fixtures | `empty` — the schema with no rows, at `2026-06-01T09:00:00.000Z` |

## Using it

```python
import projecttracker

with projecttracker.world.instance("empty") as tracker:
    tracker.tools()  # the tool list, with JSON schemas
    tracker.call("ping")  # {"message": "pong", "now": ..., "users": 0}
```

Tests: `uv run pytest worlds/projecttracker` from the repository root, or `uv run pytest` from here.

## Conventions

- **Timestamps** are canonical UTC text with milliseconds and a trailing `Z`
  (`2026-06-01T09:00:00.000Z`), written from `ctx.clock.iso()` and never from the wall clock.
- **Ids** come from `ctx.ids.uuid()`, so a seed replays a run exactly.
- **Error codes** are this product's, in `SCREAMING_SNAKE`; the framework's own codes never reach an
  agent, which is the error handler's job.
