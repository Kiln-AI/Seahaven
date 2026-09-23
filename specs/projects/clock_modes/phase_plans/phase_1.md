---
status: complete
---

# Phase 1: Clock core

## Overview

Give an instance's clock a mode (`fixed`, `tick`, `running`, `wall`), chosen per instance with a
per-world default of `running`. The `Clock` computes each reading on demand from its mode; the SQL
date and time overrides take one reading per top-level statement through a `trace_v2` statement
hook; `tick` advances when a call takes its ordinal. The state envelope carries the mode. The
projecttracker generator pins `fixed`, and every existing test that asserts an exact timestamp
through an instance pins `fixed` at its call site. The doors (OpenEnv, MCP, pytest plugin) and the
docs are later phases.

## Steps

1. `src/seahaven/clock.py`
   - Module docstring: an instance's clock and its modes, and the SQL overrides that read it.
   - `type ClockMode = Literal["fixed", "tick", "running", "wall"]`,
     `CLOCK_MODES: tuple[ClockMode, ...]`, `DEFAULT_CLOCK_MODE: ClockMode = "running"`,
     `TICK = timedelta(seconds=1)`, `TRACE_ID = "seahaven.clock"`.
   - `check_clock_mode(value: object) -> ClockMode`, refusing with `WorldBug`:
     `unknown clock mode 'tik'; the clock modes are 'fixed', 'tick', 'running' and 'wall'`.
   - Time sources `_wall_now() -> datetime` and `_monotonic_ns = time.monotonic_ns`, the only
     real-time reads in the module.
   - `Clock(now, mode="fixed")`: `_start` (aware, UTC, truncated to ms by a shared `_truncated`
     helper), `_mode`, `_calls = 0`, `_origin_ns = _monotonic_ns()`. `from_iso(text, mode="fixed")`,
     `wall()` (fixed, at `_wall_now()`), `mode` property, `now()` per the architecture's table,
     `iso()` rendering one `now()`, `_call_started()`, `__repr__` as
     `Clock(2024-03-05T12:00:00.123Z, fixed)`. Remove `__eq__` and `__hash__`. Class docstring as
     architecture §2.3 describes.
   - `_StatementReading(clock)` with `iso()` (lazy, cached until the next top-level statement) and
     `statement_started(event)` (drops the cache when `event["trigger"]` is false).
   - `register_clock_functions(conn, clock)`: one `_StatementReading` per connection, registered with
     `conn.trace_v2(apsw.SQLITE_TRACE_STMT, reading.statement_started, id=TRACE_ID)`. `_override`
     takes the reading and calls `reading.iso()` per evaluation. `_constant` evaluates its expression
     on the helper per call with a one-entry cache keyed by the reading's text.
2. `src/seahaven/db.py`: `Db.conn` docstring adds that world code must not remove the trace
   registered under `seahaven.clock`.
3. `src/seahaven/world.py`
   - `World.__init__(..., state_format=..., default_clock_mode: ClockMode = DEFAULT_CLOCK_MODE, ...)`,
     stored as `self.default_clock_mode` after `check_clock_mode`, re-raised with the prefix
     `world {name!r}: `.
   - `World.instance(..., now=..., clock_mode: ClockMode | None = None, ...)`, forwarded to
     `create`; docstring sentence on the mode.
4. `src/seahaven/instances.py`
   - `InstanceManager.create(..., clock_mode: ClockMode | None = None, ...)`: resolve the mode in the
     refusal block (`check_clock_mode` or the root world's default), before any directory exists.
   - Make the clock before either branch builds or copies: `Clock.from_iso(fixture.now, mode)`,
     `_clock_from(now, mode)`, or `Clock(Clock.wall().now(), mode)`. The fixture branch no longer
     makes its own clock after copying.
   - `_clock_from(now, mode)`.
   - `Instance._next_ordinal` calls `self.clock._call_started()` after incrementing `_call_count`.
5. `src/seahaven/state.py`: `envelope()` adds `"clock_mode"` directly after `"now"`, the mode or
   `None` without an instance.
6. `src/seahaven/__init__.py`: export `ClockMode`.
7. `worlds/projecttracker/fixtures_src/generate.py`: `build()` makes its instance with
   `clock_mode="fixed"`; docstring says why (byte-identical regeneration).
8. Existing tests: every test that asserts an exact timestamp through a default-mode instance pins
   `clock_mode="fixed"` at its call site (`world.instance(...)`, a pytest marker, a reset option),
   never by changing a test world's default. `tests/state_v1.schema.json` gains `clock_mode`.
   `test_clock.py` cases that compare clocks compare `.iso()` / `.now()`; `repr` gains the mode.

## Tests

- `tests/test_clock.py`
  - `test_fixed_reads_the_start_on_every_reading`: stubbed sources move, `fixed` does not.
  - `test_tick_counts_calls_started`: `S`, then `S + 1s`, `S + 2s` after each `_call_started`.
  - `test_running_adds_elapsed_monotonic_time_truncated_to_milliseconds`.
  - `test_running_ignores_the_wall_clock`: a stubbed wall jump does not move it.
  - `test_wall_reads_the_wall_clock_and_ignores_the_start`, truncated to ms.
  - `test_clock_built_directly_is_fixed`; `test_an_unknown_mode_is_refused_naming_the_four`;
    `test_check_clock_mode_accepts_each_mode`; `test_repr_names_the_reading_and_the_mode`;
    `test_clocks_compare_by_identity`.
  - SQL under a stepping `running` clock (each monotonic read advances 1 s):
    `test_two_default_columns_share_one_reading`, `test_a_trigger_shares_its_statements_reading`,
    `test_every_row_of_a_multi_row_insert_shares_one_reading`,
    `test_the_next_statement_takes_a_later_reading`,
    `test_each_executemany_binding_takes_its_own_reading`,
    `test_every_date_function_follows_a_moving_clock` (parametrised over `datetime('now')`,
    `strftime('%s','now')`, `julianday()`, `unixepoch()`, `date()`, `time()`, `CURRENT_*`),
    `test_a_cached_prepared_statement_rerun_takes_a_new_reading`.
  - `test_a_sandboxed_statement_takes_a_fresh_reading` (cursor `exec_trace` set via `run_sql`).
  - `test_another_trace_does_not_displace_the_clocks`.
- `tests/test_world.py`: `test_default_clock_mode_is_running`,
  `test_default_clock_mode_accepts_each_mode`, `test_an_unknown_default_clock_mode_names_the_world`.
- `tests/test_instances.py`, all through `world.instance(...)`:
  - `test_an_instance_takes_the_worlds_default_clock_mode`,
    `test_clock_mode_overrides_the_worlds_default`,
    `test_an_unknown_clock_mode_is_refused_before_a_directory_exists`.
  - `tick`: `test_tick_startup_hook_and_schema_seed_see_the_start`,
    `test_tick_call_i_reads_start_plus_i_plus_one_in_python_and_sql`,
    `test_tick_a_tool_error_ticks`, `test_tick_unknown_tool_and_control_tool_do_not_tick`,
    `test_tick_nested_call_sees_the_outer_calls_instant`,
    `test_tick_inspect_and_state_read_without_ticking`.
  - `running`: `test_running_readings_advance_with_monotonic_time`,
    `test_running_freeze_records_the_reading_and_a_fork_starts_there`.
  - `wall`: `test_wall_reads_the_wall_clock`, `test_wall_accepts_now_and_a_fixture`.
  - composite: `test_a_composite_takes_the_roots_default_clock_mode`,
    `test_every_node_of_a_composite_reads_one_clock`.
- `tests/test_state.py`: `test_clock_mode_follows_now_in_the_envelope`,
  `test_clock_mode_is_null_without_an_instance`.
- projecttracker: the existing byte-identical regeneration test runs with the default now `running`,
  covering the generator's pin.
