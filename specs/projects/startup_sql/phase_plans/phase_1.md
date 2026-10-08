---
status: complete
---

# Phase 1: Core

## Overview

`setup_sql: str | None` becomes a parameter of `world.instance(...)`. Instance creation runs it on
one writable connection to the root's file with every added node attached, after the files are in
place and before any node connection is opened, in one transaction, under an authorizer that allows
only row reads and row writes. Startup hooks then see the committed rows. Any failure is a
`WorldBug` naming the statement, and creation leaves no directory behind.

The entry points (OpenEnv, the state document) and the docs are phases 2 and 3. HTTP and MCP derive
their keys from the `World.instance` signature, so they take the key from this phase on; the one
test that pins those keys is updated here so the suite stays green.

## Steps

1. `src/seahaven/ids.py`: add `SETUP_STREAM = b"setup"` beside the other streams, to `__all__`, and
   one sentence in the stream comment saying it is the setup connection's door.
2. New `src/seahaven/setup_sql.py`:

   ```py
   __all__ = ["SetupAuthorizer", "run_setup_sql", "split_statements"]

   def run_setup_sql(
       sql: str, *, root: Path, attachments: Sequence[tuple[str, Path]], clock: Clock, seed: bytes
   ) -> None: ...

   def split_statements(sql: str) -> list[str]: ...

   @dataclass(frozen=True)
   class Refused:
       action: int
       third: str | None
       fourth: str | None
       description: str      # the base class's refusal text

   class SetupAuthorizer(Authorizer):
       refused: Refused | None
       def reset(self) -> None: ...
       def __call__(self, action, third, fourth, database, trigger, /) -> int: ...

   def _explain(refused: Refused | None, error: apsw.Error, schemas: Sequence[str]) -> tuple[str, str]
   def _every_table(conn: apsw.Connection) -> list[str]
   def _failure(index, count, statement, refused, error, schemas) -> WorldBug
   ```

   - `split_statements`: split on `;`, join pieces until `apsw.complete` is true, drop
     whitespace-only pieces, keep a trailing fragment as the last statement.
   - `run_setup_sql`: per architecture — open, `_harden`, `synchronous OFF`, busy timeout 0, random
     functions on `SETUP_STREAM`, clock functions, `ATTACH ... AS ?` with bound parameters, list every
     table (including FTS5 shadow tables), `BEGIN`, install the authorizer, run each statement on
     one cursor and drain it, remove the authorizer, `COMMIT`; on an `apsw.Error` roll back and raise
     the `WorldBug` from it; always close the connection and the clock helper.
   - `SetupAuthorizer`: `read_only=False`, `ALLOWED_FUNCTIONS`; additionally allows the read-only
     pragmas `db._READ_ONLY_PRAGMAS` (the list the inspection connection uses), so the error
     message "Only read-only pragmas such as table_info are allowed" is true. Records the first
     refusal of a statement; `reset()` clears it.
   - `_explain`: the reason and fix per the architecture's error table; message shape
     `setup_sql statement {i} of {n} {was refused|failed}: {reason} {fix}\n  {statement}` with the
     statement whitespace-collapsed and cut at 200 characters with `...`.
3. `src/seahaven/instances.py`:
   - `_attachment_pairs(nodes: Iterable[Node], directory: Path) -> list[tuple[str, Path]]`, used by
     `Instance._attachments()` and by `create`.
   - `_check_setup_sql(setup_sql: object) -> None`: `WorldBug("setup_sql takes a string of SQL
     statements separated by ';', not {type}")` unless `str` or `None`; called beside
     `_check_startup_keywords`, before any directory exists.
   - `create(..., setup_sql: str | None = None)`: after the copy or blank build and before the
     `_open_node` loop, `run_setup_sql(...)` when `setup_sql is not None`, seeded with
     `node_seed(base, ROOT_PATH)`. Docstring line.
   - `Instance.__init__(..., setup_sql: str | None)`: stored as `self.setup_sql`.
4. `src/seahaven/world.py` `World.instance`: `setup_sql: str | None = None` after `startup`,
   docstring sentence, forwarded.
5. `src/seahaven/pytest_plugin.py` `_MARKER_SIGNATURE`: add `setup_sql=None` after `startup=None`.
6. Pins the new parameter breaks: `tests/test_cli_mcp.py` key list; `tests/test_world.py`
   framework-parameter list; `SETUP_STREAM` added to the door lists in `tests/test_ids.py` and
   `tests/test_composite_instance.py`.

## Tests

New `tests/test_setup_sql.py`, every behaviour test through `world.instance(...)`, on small inline
worlds (a leaf `notes`-style world, a host adding `payments` which adds `tax`, and an FTS5 world).

Behaviour:
- `test_rows_written_by_setup_sql_are_there_for_the_first_call`: inserted row read by the first
  tool call and by `inst.inspect()`.
- `test_statements_run_in_the_order_written`: a later `UPDATE` sees an earlier `INSERT`'s row.
- `test_a_semicolon_inside_a_literal_does_not_split`.
- `test_unqualified_names_write_the_root_and_qualified_names_write_a_node`: `payments.charges` and
  `payments__tax.rates`.
- `test_one_statement_reads_one_node_and_writes_another`: `INSERT INTO payments.charges ...
  SELECT ... FROM <root table>`.
- `test_setup_sql_runs_on_a_copied_fixture`: a frozen fixture plus setup SQL.
- `test_an_update_through_setup_sql_is_found_by_fts5`: `UPDATE` then a `MATCH` in the first call;
  the old term no longer matches.
- `test_a_startup_hook_sees_the_rows_setup_sql_wrote`: SQL inserts a user, `startup={"user_id":
  ...}` names them, the hook reads the row.
- `test_the_same_seed_and_setup_sql_give_the_same_random_rows`, and
  `test_setup_sql_draws_from_a_stream_of_its_own`: an instance's own `random()` in a tool call is
  the same with and without setup SQL that drew from `random()`; and different seeds differ.
- `test_the_clock_functions_read_the_instances_clock`: `datetime('now')` is the instance's `now`.
- `test_setup_sql_rows_are_not_in_the_change_log`: `change_log()` empty and `call_count == 0`.
- `test_setup_sql_is_kept_on_the_instance`: `inst.setup_sql` is the string as given, `None` when
  omitted.
- `test_an_empty_string_whitespace_or_a_comment_changes_nothing` (parametrized).
- `test_a_select_runs_and_changes_nothing`, and `test_read_only_pragmas_and_fts5_commands_are_allowed`.

Refusals and errors (each asserts the whole `WorldBug` message shape: statement number of count,
"was refused"/"failed", reason, fix):
- `test_a_refused_statement_names_what_was_refused` parametrized over every row of the
  architecture's Refusals table.
- `test_a_refusal_rolls_back_earlier_statements_and_leaves_no_directory`.
- `test_a_syntax_error_names_the_statement_and_sqlites_message`,
  `test_a_constraint_failure_fails_the_instance`,
  `test_a_missing_table_in_a_composed_world_names_the_schemas`, and the same in a leaf world with no
  hint.
- `test_a_non_string_setup_sql_is_refused_before_anything_is_copied` (no directory created).
- `test_a_long_statement_is_cut_in_the_message`.

Unit tests:
- `split_statements` cases: literal `;`, quoted identifier, comment, trigger body, trailing fragment,
  empty and whitespace, comment-only piece.
- `_explain` per row of the error table, including the ATTACH fix with and without added nodes.
- `SetupAuthorizer.reset` clears the recorded refusal and records only the first.
