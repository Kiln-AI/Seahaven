---
status: complete
---

# Phase 4: Risk report

## Overview

Phase 1 made every SQL date function read the clock once per top-level statement, using a
`trace_v2` callback on `SQLITE_TRACE_STMT` (architecture §3). This phase assesses whether that
design is safe and robust. It writes `specs/projects/clock_modes/risk_report.md`, with a verdict
for each risk the implementation plan names, and adds tests to `tests/test_clock.py` and
`tests/test_instances.py`. Each test either pins a behaviour the design depends on or records a
confirmed defect as a strict `xfail`. The report proposes fixes for the defects. This phase does
not change `src/`.

## Steps

1. Establish the facts, in a scratch directory, against the locked APSW 3.53.4 and the SQLite
   3.53.4 sources (`vdbe.c` `OP_Init`/`OP_Trace`, `vdbeapi.c` `sqlite3_step`,
   `checkProfileCallback`):
   - when `SQLITE_TRACE_STMT` fires, and when SQLite prefixes the text with `-- `
     (`db->nVdbeExec > 1`); the re-prepare suppression (`tag-20220401a`);
   - how APSW derives `trigger` (the `-- ` prefix), including a top-level statement whose text
     starts with a line comment, and later statements of a multi-statement string;
   - nested statements from a Python function; FTS5's internal queries;
   - partly stepped, abandoned, re-run and failed statements; `SQLITE_SCHEMA` re-prepare;
   - interleaved cursors, and which expressions SQLite evaluates per row rather than once;
   - other trace APIs (`set_profile`, a `trace_v2` without `id`, `trace_v2(0, None)`,
     `apsw.ext.Trace`) against the clock's trace;
   - two threads on one connection; `THREADSAFE` in `apsw.compile_options`;
   - cost per statement, with and without the trace, and inside real projecttracker calls;
   - a prototype of the proposed fix, run against the edge cases and the framework suite.
2. Write `specs/projects/clock_modes/risk_report.md`: a summary table (risk, verdict, action),
   one section per risk with evidence and verdict, and a section of proposed fixes with their
   measured cost and caveats.
3. Add the tests below.

## Tests

In `tests/test_clock.py`, on the existing stepping `running` fixture:

- `test_a_nested_statement_shares_its_callers_reading`: a Python function that runs a statement
  on the same connection, called from an `INSERT` with two `CURRENT_TIMESTAMP` defaults; every
  value is one reading.
- `test_a_virtual_tables_own_queries_share_the_statements_reading`: an `INSERT` whose trigger
  writes to an FTS5 external-content table and then stamps the row; both stamps are one reading.
- `test_a_statement_re_prepared_after_a_schema_change_takes_a_fresh_reading`: a second connection
  changes the schema, the cached statement is re-prepared, and its reading is still later than the
  one before.
- `test_other_trace_apis_leave_the_clocks_trace_in_place` (parametrized): `set_profile`, a
  `trace_v2` without `id`, `trace_v2(0, None)` without `id`, and `apsw.ext.Trace`; readings still
  move.
- `test_a_statement_led_by_a_line_comment_takes_a_fresh_reading`: strict `xfail`, the comment
  defect.
- `test_a_statement_keeps_its_reading_while_another_runs_between_its_rows`: strict `xfail`, the
  interleaved-cursor defect.

In `tests/test_instances.py`, through `world.instance(...)`:

- `test_tick_a_comment_led_statement_reads_its_own_calls_instant`: strict `xfail`; under `tick`,
  a statement starting with `-- ` in call 1 reads call 0's instant.
