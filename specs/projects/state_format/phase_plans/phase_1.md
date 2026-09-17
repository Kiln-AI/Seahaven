---
status: complete
---

# Phase 1: Capture the change log

## Overview

The change log is the data every later phase formats, serves and documents, so it lands first and
alone. This phase adds the record (`LogRecord`), the renderer (`render_log`), the per-call and
per-bulk SQLite sessions that feed it, the call ordinal that joins it to a harness trace, and the
two new reads on `Instance` (`change_log()`, `call_count`). It also narrows `seed` to `int | None`
(ARCH §7), which is a breaking change with no dependants left in the repo.

Nothing is removed. `Instance.changes()`, `Change`, `render()` and the long-lived cumulative
session stay exactly as they are until Phase 4, so every existing test, doc example and control
tool keeps working, and the fold test of FS §3.6 has `changes()` as its first oracle — SQLite's own
cumulative changeset, which is what the fold is defined against.

## Steps

1. **`src/seahaven/changes.py` — the log record and its renderer.**

   - Add `LogRecord`, a frozen dataclass beside `Change`:

     ```python
     @dataclass(frozen=True)
     class LogRecord:
         i: int | None
         subworld: str | None
         table: str
         op: Literal["insert", "update", "delete"]
         key: dict[str, Any]
         before: dict[str, Any] | None
         after: dict[str, Any] | None

         def to_dict(self) -> dict[str, Any]:  # field order: i, subworld, table, op, key, before, after
     ```

   - Factor `start_session`'s loop into two functions and rewrite `start_session` over them, so the
     existing behaviour (including the no-primary-key refusal) has exactly one implementation:

     ```python
     def tracked_tables(conn: apsw.Connection, world: World) -> tuple[str, ...]
     def open_session(conn: apsw.Connection, tracked: Sequence[str]) -> apsw.Session
     def start_session(conn, world) -> apsw.Session:  # open_session(conn, tracked_tables(conn, world))
     ```

   - Add the renderer:

     ```python
     def render_log(
         changeset: bytes,
         conn: apsw.Connection,
         columns: dict[str, tuple[list[str], list[int]]],
         *,
         i: int | None,
     ) -> list[LogRecord]
     ```

     `columns` is the caller's cache, filled lazily with `_columns(conn, table)`. Per `TableChange`:
     `key` from the key positions of `new` for an insert and of `old` otherwise (as `render` does);
     `before`/`after` per ARCH §3.3 — insert: `None` / whole row; update: the non-key columns whose
     `new` is not `apsw.no_change`, old values / new values; delete: whole row / `None`. `subworld`
     is `None` in this release.

     The records of one changeset are then sorted by `_sort_key(record)`, which is
     `(subworld or "", table, rank of each key value)`, where `_sqlite_rank` maps a value to
     `(0, None)` for NULL, `(1, value)` for a number and `(2, value)` for text — SQLite's own
     cross-type order, so a key column typed ANY cannot raise `TypeError` in the sort. The rank is
     taken over the *rendered* key values, as ARCH §3.3 specifies: the within-call order is part of
     the format and the fold re-sorts by it, and a consumer holds a blob's base64 text and never its
     bytes, so a blob key sorts by that text.

   - Extend `_jsonable`: an infinite `float` becomes `None`, with the one-line comment ARCH §3.4
     dictates. Everything else is unchanged.

   - `__all__` gains `LogRecord`, `open_session`, `render_log`, `tracked_tables`.

2. **`src/seahaven/instances.py` — the log store, the ordinal and the sessions.**

   - `Instance.__init__` gains `tracked: tuple[str, ...]` and initialises
     `self._records: list[LogRecord] = []`, `self._call_count = 0`,
     `self._columns: dict[str, tuple[list[str], list[int]]] = {}`. `session` stays (Phase 4 removes
     it with `changes()`).
   - `call_count` — a read-only property over `_call_count`.
   - `change_log()` — `list(self._records)` under `_held()`. No database work: the log is in memory
     and `render_log` ran at call time. The records themselves are handed out rather than copied,
     which the docstring says.
   - `_next_ordinal()` — increments `_call_count` and returns it minus one; called with the lock
     held.
   - `_recording(i)` — a context manager that opens a session over `self._tracked` for the block and,
     in its `finally`, takes the changeset, closes the session and extends `self._records` with
     `render_log(..., i=i)`. The changeset is read after the call's transaction has committed or
     rolled back, which is what makes a record the net of its call.
   - `_call` — the tool lookup stays where it is (the gate's `bypass` reads `tool.control`), but the
     `UnknownTool` raise moves inside `self._held()` and follows `_next_ordinal()`, so a refused
     name consumes an ordinal (FS §7). A world tool runs inside `_recording(self._next_ordinal())`;
     a control tool takes no ordinal and no session.
   - `_bulk` — `self._recording(None)` around the transaction, so authoring writes land with
     `i: null`.
   - `InstanceManager.create` — computes `tracked = tracked_tables(db.conn, world)` once, after the
     startup hooks, and passes it and `open_session(db.conn, tracked)` to `Instance`. `seed` narrows
     to `int | None`.

3. **`src/seahaven/ids.py` — `seed` is `int | None`.** `instance_seed` and `_seed_bytes` drop the
   `bytes` arm, so `bytes` falls into the existing `case _` and raises the existing `WorldBug`, whose
   message becomes "an int or None".

4. **`src/seahaven/world.py` — `World.instance(seed: int | None = None)`.**

5. **`src/seahaven/__init__.py` — export `LogRecord`** beside `Change`.

6. **Tests** (below).

## Tests

`tests/test_changes.py` (additions, `LogRecord` and `render_log` through real calls):

- `test_an_insert_logs_the_whole_row` — `before` is `None`, `after` is every column.
- `test_a_delete_logs_the_whole_row` — `after` is `None`, `before` is every column.
- `test_an_update_logs_only_the_columns_it_changed` — one column changed, key absent from both sides.
- `test_an_update_of_several_columns_logs_them_all`.
- `test_a_column_set_to_null_is_a_change` — `after` holds `None`, not an absent column.
- `test_a_composite_key_is_logged_in_key_order`.
- `test_an_integer_primary_key_is_a_key`.
- `test_a_blob_is_logged_as_base64`.
- `test_an_infinite_float_is_logged_as_null` — and a finite one is not.
- `test_a_log_record_renders_to_a_dict` — the FS §3.2 field order.
- `test_records_of_one_call_are_sorted_by_table_then_key`.
- `test_a_mixed_type_key_sorts_by_type_and_then_by_its_published_value` — a non-STRICT table with an
  ANY key column, so the rank is exercised rather than reasoned about.
- `test_a_blob_key_sorts_by_its_base64_and_not_by_its_bytes` — the one key type whose published text
  orders differently from its raw value, pinned here and again in `test_fold.py` against the fold.

`tests/test_change_log.py` (new):

- `test_the_log_is_empty_before_any_call`.
- `test_a_call_that_changed_nothing_logs_nothing` and `test_a_read_only_call_logs_nothing`.
- `test_a_rolled_back_call_logs_nothing`.
- `test_rows_a_startup_hook_wrote_are_not_in_the_log`.
- `test_an_insert_then_an_update_in_one_call_is_one_insert`.
- `test_an_insert_then_a_delete_in_one_call_logs_nothing`.
- `test_an_update_back_to_the_original_in_one_call_logs_nothing`.
- `test_the_same_row_in_two_calls_is_two_records` — and `test_an_update_back_to_the_original_across_two_calls_is_two_records`.
- `test_records_are_ordered_by_call`.
- `test_bulk_writes_carry_no_ordinal` — `i is None`, ordered where they committed.
- `test_every_dispatched_call_consumes_an_ordinal` — a `ToolError` and an `UnknownTool` each do.
- `test_control_tools_and_tool_listing_are_not_calls` — `call_count` unmoved.
- `test_two_identical_episodes_serialise_byte_for_byte` — `json.dumps` of the two logs.
- `test_change_log_does_no_database_work` — an exec tracer on the instance connection counts zero
  statements across `change_log()` (the ARCH §16 precursor).
- `test_change_log_on_a_destroyed_instance_is_refused`.

`tests/test_fold.py` (new) with `tests/support/fold.py`:

- `fold(log)` implements FS §3.6 — group by `(subworld, table, key)`, compose in log order, drop
  what folded to untouched, sort as §3.3. The test's oracle is `instance.changes()` normalised to
  the same shape (an update's `before` with its key columns removed), which is SQLite's own
  cumulative changeset.
- `test_the_fold_equals_the_cumulative_changeset` — parametrised over the episode shapes
  `test_change_log.py` exercises: insert; insert then update; insert then delete; update then
  update; update then delete; delete then insert with different values; delete then insert with the
  same values; a primary-key rewrite; bulk writes mixed with calls.
- `test_the_log_and_the_fold_sort_blob_keys_the_same_way` — the parametrised test above sorts
  both sides with `fold`, so the one order the two modules could disagree about is pinned apart.

`tests/test_ids.py`:

- `test_a_bytes_seed_is_refused` — `WorldBug`, "an int or None".
- `test_different_caller_seeds_diverge` loses its `bytes` case and the wrong-type test matches the
  new message; `test_caller_seed_forms` becomes `test_no_caller_seed_is_not_seed_zero`, since the
  `bytes` equivalences were what it had to say.

`tests/test_instances.py`:

- the `test_the_instance_seed_is_the_derived_one` parametrisation drops `bytes`.

`tests/test_pytest_plugin.py`:

- `test_a_bytes_seed_in_the_marker_is_refused` — the marker passes `seed=` straight through, so the
  narrowing is what the world's test author meets.
