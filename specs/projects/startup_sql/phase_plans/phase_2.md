---
status: complete
---

# Phase 2: Entry points and state document

## Overview

Phase 1 made `setup_sql` a `world.instance(...)` parameter. HTTP `PUT` and
`seahaven mcp --reset-options` already take it, because both read their keys off that signature.
This phase puts it on the OpenEnv wire (the `reset` signature and the published reset schema),
reports it in the state document's envelope and in `SeahavenState`, updates the console mock and
smoke test, and updates every test that pins the envelope or the reset keys. Each entry point gets
a test that drives `setup_sql` through it.

## Steps

1. `src/seahaven/state.py` `envelope()`: add
   `"setup_sql": instance.setup_sql if instance is not None else None` directly after `startup`.
2. `src/seahaven/openenv/env.py`:
   - `SeahavenState`: `setup_sql: str | None = Field(default=None, description=...)` after
     `startup`; the field comment's count of null-defaulted fields becomes seven.
   - `RESET_ORDER = ("fixture", "startup", "setup_sql", "now", "clock_mode", "state_format")`.
   - `SeahavenResetRequest`: `setup_sql: str | None = Field(default=None, description=...)`.
     `for_world` is unchanged.
   - `SeahavenEnv.reset`: `setup_sql: str | None = None` after `startup`, before `**unknown`;
     forwarded to `InstanceManager.create`; one docstring clause.
3. `src/seahaven/cli/mcp.py`: the comment over `RESET_OPTION_KEYS` no longer says "five names".
4. Console: `tools/console/mock/server.mjs` reset schema gains `setup_sql` (titled "Setup Sql")
   after `startup`; `tools/console/e2e/smoke.mjs` form order includes "Setup Sql" between the
   startup keywords and "Now", and checks it renders as a `textarea`. No console source change.
5. Pins: `tests/test_env.py` `RESET_PARAMETERS` and `DECLARED_DESCRIPTIONS`;
   `tests/test_reset_schema.py` property order; `tests/state_v1.schema.json` (`required`,
   `properties` as `["string", "null"]`); `tests/test_state.py` key order and no-instance nulls;
   `tests/test_server.py` `EPISODE` and `expected_document`; `tests/test_client.py`
   `DOCUMENT_FRAME` and the pre-reset nulls.

## Tests

- `tests/test_state.py::test_the_setup_sql_is_reported_exactly_as_given`: the string as given,
  `""` stays `""`, `None` when omitted, and the setup row is not in the log.
- `tests/test_state.py::test_a_built_in_document_validates_against_the_published_schema`: now with
  `setup_sql`, so the string branch of the schema is exercised.
- `tests/test_env.py::test_setup_sql_reaches_the_instance_through_reset`: in process, on a fixture;
  rows visible to the first call, `state.setup_sql`, empty log.
- `tests/test_env.py::test_a_failing_setup_sql_leaves_the_session_as_a_fresh_one`: `WorldBug`, no
  instance, old directory gone, `setup_sql` null, another `reset` works.
- `tests/test_server.py::test_setup_sql_reaches_the_instance_over_the_wire`: a refused statement is
  an `EXECUTION_ERROR` frame; the next reset with good SQL has the row and the state field.
- `tests/test_server.py` whole-document tests: `setup_sql` is in `EPISODE`, so the document over the
  WebSocket (typed and stock client) carries it, and its row is not in the log.
- `tests/test_reset_schema.py`: `setup_sql` in the published order, as a nullable string titled
  "Setup Sql".
- `tests/test_http_server.py::test_a_put_body_with_setup_sql_starts_the_instance_with_its_rows`, and
  two new rows in the refused-`PUT` table (non-string, refused statement) answering 400.
- `tests/test_cli_mcp.py::test_setup_sql_is_a_reset_option` and
  `tests/test_mcp_process.py::test_reset_options_carry_setup_sql_to_the_instance` (a real MCP
  process; the first call reads the row).
- `worlds/projecttracker/tests/test_issues.py::test_a_user_setup_sql_inserts_can_be_the_viewer_who_files_an_issue`:
  `agency` plus SQL inserting a user plus `startup={"user_id": ...}`; `create_issue` is attributed
  to that user, and `users` is not in the change log.
