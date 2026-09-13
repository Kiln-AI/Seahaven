# ProjectTracker

A Seahaven world. The framework's own documents are the place to start — `architecture.md` and
`components/projecttracker.md` under `specs/projects/seahaven_framework/` — and this file holds only
what is particular to this world.

Commands: `uv run seahaven check` runs every lint over this world and is what to run before a
commit; `uv run seahaven fixture list` lists its fixtures and `uv run seahaven fixture freeze` mints
one; `uv run seahaven docs` prints the directory the bundled authoring documentation is installed
in. Every page of that layout exists and `reference/lints.md` is complete, but the prose is Phase 12
of the implementation plan, so the specifications under `specs/projects/seahaven_framework/` remain
the place to read until then.

This world lives inside the Seahaven repository rather than beside it, so the repository's own
`AGENTS.md` — Python 3.14, fully typed, `ty` and `ruff` and the tests clean before any commit —
applies here too.

## About this world

- **Scope.** This is the placeholder slice of ProjectTracker, built in Phase 5 of the implementation
  plan: the `users` table, a `ping` tool, the error shapes, the error wrapper and an `empty`
  fixture. The full tracker is Phase 10 and is specified in
  `specs/projects/seahaven_framework/components/projecttracker.md`. Extend what is here in the shape
  that document gives; do not invent a second convention beside it.
- **Timestamps** are canonical UTC text with milliseconds and a trailing `Z`, e.g.
  `2026-06-01T09:00:00.000Z`. Every one of them comes from `ctx.clock.iso()`. There are no
  wall-clock reads in world code and no time defaults in the DDL.
- **Ids** come from `ctx.ids.uuid()` and randomness from `ctx.ids.random`, so one seed replays one
  run.
- **Errors.** Declare shapes in `errors.py` as `seahaven.ToolError` subclasses with
  `SCREAMING_SNAKE` codes, and raise them by name. Nothing else may reach an agent: the error
  handler in `middleware/error_handler.py` restates framework errors in those shapes, and the two
  doors that are allowed to show engine text (`run_sql`, `search_issues`) are named there.
- **Tests** use Seahaven's pytest plugin: `@pytest.mark.seahaven(fixture="empty")` over the
  `instance` fixture, or `fixture=None` with `now=BLANK_NOW` for an instance with no fixture behind
  it. Nothing here builds an instance in a `conftest.py`, and nothing calls a tool function
  directly: a tool is tested through `instance.call`.
- **Fixtures** are frozen from a blank instance at `2026-06-01T09:00:00.000Z` by
  `fixtures_src/generate.py`, which is committed and is the only way a fixture here is made. Never
  edit a fixture in place: fork it, change it, freeze it.
