---
status: complete
---

# Implementation Plan: Startup SQL

## Phases

- [ ] Phase 1: Core. `seahaven/setup_sql.py` (`run_setup_sql`, `split_statements`,
  `SetupAuthorizer`, `_explain`), `SETUP_STREAM`, the `setup_sql` parameter on `World.instance`,
  `InstanceManager.create` and `Instance` (stored as `inst.setup_sql`), the pre-copy type check, and
  the pytest marker signature. Tests in `tests/test_setup_sql.py` through `world.instance(...)`:
  behaviour, composed worlds, FTS5, startup hooks seeing setup rows, seeding, change log, every
  refusal and error message, rollback leaving no directory, and the unit tests.
- [ ] Phase 2: Entry points and state document. `setup_sql` in the state document envelope and
  `SeahavenState`; OpenEnv `reset`, `SeahavenResetRequest` and `RESET_ORDER`; the stale
  `cli/mcp.py` comment; console mock schema and smoke order; every pinned test and
  `tests/state_v1.schema.json`. Tests for OpenEnv (in process and over the WebSocket), HTTP `PUT`,
  `seahaven mcp --reset-options`, and the projecttracker test (SQL inserts a user, `startup` names
  them, `create_issue` is attributed to them).
- [ ] Phase 3: Docs. Updates to six existing pages, no new page: `serving_and_openenv.md`, `db_schema_and_fixtures.md` (new short section with
  a runnable example), `state.md`, `composition.md`, `reference/api.md`, `reference/cli.md`, per
  the AGENTS.md docs style.
