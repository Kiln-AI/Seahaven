---
status: complete
---

# Risk Report: One SQL Reading per Statement

An assessment of the per-statement SQL reading (architecture §3): `register_clock_functions`
registers a `trace_v2` callback on `SQLITE_TRACE_STMT` that drops a connection's cached reading
when a top-level statement starts, and ignores events APSW marks `trigger`.

## Summary

The design is sound and should stay. It depends on two behaviours that SQLite and APSW implement
but do not document in full, and tests now pin both. There are two confirmed defects. In each,
a statement ends up with a reading that is not its own. Neither defect affects `fixed`. Under
`tick`, both appear only on the `inspect()` and control handles.

| # | Risk | Verdict | Action |
|---|---|---|---|
| R1 | SQLite's guarantees for `SQLITE_TRACE_STMT` | Safe at the pinned version | Test added |
| R2 | Nested statements a user function runs | Safe | Test added |
| R3 | Virtual tables reported as trigger activity | Safe | Test added |
| R4 | A statement whose text starts with `-- ` | **Defect**, low-medium | `xfail` tests; F1, F2 |
| R5 | Statements stepped partly, reset and re-run | Safe | Test added |
| R6 | Interleaved statements on one connection | **Defect**, low | `xfail` tests; F2 |
| R7 | APSW versions and the `id` keyword | Safe | None |
| R8 | The sandbox and other tracers | Safe | Test added |
| R9 | Threads and the `inspect()` handle | Safe apart from R6 | None |
| R10 | The trace removed, or never firing | Silent failure, accepted | Existing test pins it |
| R11 | Cost | Acceptable | None |

**Recommendation.** F2 fixes R4 and R6 in every mode, at the cost of the re-prepare case in its
caveats. It costs 4% on a read call and 17% on a write-heavy call of the reference world, and
whether that is worth paying is a maintainer's decision. F1 is the cheap alternative. Its cost
cannot be measured, but it fixes only R4 under `tick`. If F1 is taken instead of F2,
`reference/api.md` needs one sentence naming the exceptions to "one SQL statement takes one
reading".

## Method

All findings were measured on APSW 3.53.4.0 and the SQLite 3.53.4 it bundles. That is the
version in the lock and the floor in `pyproject.toml`. The Python was CPython 3.14.7 with the
GIL. The SQLite source was read at tag `version-3.53.4` (`src/vdbe.c` `OP_Init`,
`src/vdbeapi.c` `sqlite3_step` and `checkProfileCallback`). The clock was stubbed the way the
suite stubs it, so each reading of a `running` clock differs from the one before.

## R1. SQLite's guarantees for `SQLITE_TRACE_STMT`

The documentation says only this: the callback is invoked "when a prepared statement first
begins running and possibly at other times during the execution of the prepared statement (such
as at the start of each trigger subprogram)". Its text argument is the statement's SQL, or "an
SQL comment that indicates the invocation of a trigger".

In 3.53.4, the event comes from one opcode pair. `OP_Init` is instruction 0 of every program,
so it reports each run of a statement once. `OP_Trace` starts each trigger subprogram, with the
text `-- TRIGGER name`. "Possibly at other times" means trigger subprograms and nothing else.
Nothing fires at prepare time, per row, or when a statement ends.

The design also relies on two behaviours that are in the source but not in the documentation:

- A statement that starts while another statement is inside `sqlite3_step`
  (`db->nVdbeExec > 1`) is reported with its text prefixed `-- `. APSW sets `trigger` for any
  text with that prefix and strips it. This is what R2 and R3 depend on, and what R4 breaks.
- When SQLite re-prepares a statement after `SQLITE_SCHEMA`, the retried run does not report
  again (`tag-20220401a`). Its profile event does fire twice. Verified with a cached statement
  that reads a table and is re-run after a second connection changes the schema.

**Verdict:** safe at the pinned version. The existing trigger test and the tests for R2 and R3 pin
the `-- ` behaviour, so a SQLite or APSW upgrade that changes it fails the suite. A new test pins
that a re-prepared statement still takes a fresh reading. That test cannot see whether the retry
reports a second time: SQLite checks the schema before any date function runs, so a second report
would drop a reading that has not been taken yet.

## R2. Nested statements a user function runs

A Python function that runs a statement on the same connection does so inside the caller's
step. SQLite prefixes that statement's text with `-- `, APSW marks it `trigger`, and the
statement shares the caller's reading. Verified with an `INSERT` that has two
`CURRENT_TIMESTAMP` defaults and a function that runs `SELECT CURRENT_TIMESTAMP`: all three
values are one reading. A nested call through `ctx.worlds` does not use this path, because it
runs its statements from Python between the outer call's statements.

**Verdict:** safe. **Test:** `test_a_nested_statement_shares_its_callers_reading`.

## R3. Virtual tables reported as trigger activity

FTS5 runs statements of its own on the connection while it updates or queries an index. They
include `PRAGMA data_version`, writes to the `_data`, `_docsize` and `_idx` tables, and a read of
`_config`. Each one is nested, so APSW reports it as trigger activity and it does not drop the
reading. Verified with an external-content FTS5 table kept in sync by a trigger that then stamps
the row: the `DEFAULT` and the trigger's stamp are one reading. A virtual table implemented in
Python through APSW runs its queries inside the step in the same way, so it follows the same
rule.

A virtual table can also query at prepare time, for example when it connects. That query runs
outside any statement and so drops the reading. It always happens before the statement that
caused it starts, so it is harmless.

**Verdict:** safe. **Test:** `test_a_virtual_tables_own_queries_share_the_statements_reading`.

## R4. A statement whose text starts with `-- `

APSW decides `trigger` from the `-- ` prefix alone. A top-level statement whose own text starts
with a line comment therefore looks like a nested statement, and it keeps the reading of the
connection's previous statement. `--x` (no space) and ` -- x` (leading space) are not affected.
In a string of several statements, APSW skips the whitespace after each `;`. A line comment
that follows a `;` therefore starts the next statement's text, so every statement after the
first that is preceded by a comment is affected.

What it costs depends on the connection:

- **A tool call's connection.** A call, and each nested call through `ctx.worlds`, opens its
  node's transaction with `BEGIN DEFERRED`. That is a statement, and it drops the reading, so the
  first statement of a call that reads the clock always takes a fresh reading. A comment-led
  statement reuses a reading only from an earlier statement in the same call. Under `tick` that
  reading is exact. Under `running` and `wall` it is stale by the time since that earlier
  statement, which is usually milliseconds.
- **The `inspect()` and control handles.** Nothing runs between the caller's statements, so the
  reused reading can be from any time before. Under `tick`, a grader's comment-led query can read
  an instant from before any number of calls. The answer is wrong but repeats exactly on replay.
  Under `running` and `wall` the reading can be arbitrarily old.
- **`fixed`.** No effect.

**Verdict:** defect, low to medium. It breaks functional spec §2.2 ("outside a call the clock
reads `S + call_count` seconds") and §2.3 (live readings) for SQL on the read-only handles.

**Tests:**

- `test_a_statement_led_by_a_line_comment_takes_a_fresh_reading`: strict `xfail`, `running`.
- `test_tick_a_comment_led_read_through_inspect_reads_the_current_instant`: strict `xfail`,
  through `world.instance(...)`.
- `test_tick_a_comment_led_statement_in_a_call_reads_its_calls_instant`: passes. It pins the
  call-path behaviour described above.

**Fix:** F1 fixes the `tick` case. F2 fixes every mode.

## R5. Statements stepped partly, reset and re-run

Each run of a statement starts at `OP_Init`, so each run takes its own reading. This holds when
a cached prepared statement is run again, when a cursor is re-executed, and for each binding of
an `executemany`. A statement abandoned after its first row reports nothing more, and the next
statement drops its reading when it starts. A statement that fails does the same.

**Verdict:** safe. **Tests:** new:
`test_a_statement_abandoned_after_its_first_row_takes_a_new_reading_when_run_again`. Existing:
`test_the_same_statement_run_again_takes_a_later_reading`,
`test_each_executemany_binding_takes_its_own_reading` and
`test_a_failed_statements_reading_is_not_reused`.

## R6. Interleaved statements on one connection

Say statement A is paused at a row, and a second top-level statement B starts on the same
connection. B is not nested, so it drops A's reading. A's remaining rows then take a later
reading, and one statement has several readings, which breaks functional spec §2.3. The same
happens when B runs on another thread between A's rows (R9).

Two SQLite behaviours make the exposure narrow:

- A date function whose arguments are all constant is evaluated once per run, at the start. This
  was verified for `CURRENT_TIMESTAMP`, `datetime('now')`, and the same function inside `CASE`,
  `coalesce` and a scalar subquery. Such a function is never evaluated again for a later row.
- `INSERT`, `UPDATE` and `DELETE` make every change in their first step, `RETURNING` included.
  A write cannot be split by a statement that runs between its rows.

So the only expressions exposed are per-row date functions in a read, whose arguments include a
column, such as `timediff('now', due_at)` or `datetime('now', offset)`. How often they are exposed
depends on the mode:

- **`running` and `wall`:** any statement B exposes them.
- **`tick`:** a fresh reading differs from the old one only after a call has started. So B
  exposes them only when a call also starts between A's rows. That happens on the `inspect()`
  and control handles, where a caller can hold a cursor across a call. Verified with a
  three-row read on `inspect()`: a call and a second statement between its rows gave the later
  rows the new call's instant.
- **`fixed`:** nothing changes.

Readings still never decrease.

**Verdict:** defect, low. **Tests:**

- `test_a_statement_keeps_its_reading_while_another_runs_between_its_rows`: strict `xfail`,
  `running`.
- `test_tick_a_read_through_inspect_keeps_its_reading_across_a_call`: strict `xfail`, through
  `world.instance(...)`.

**Fix:** F2.

## R7. APSW versions and the `id` keyword

`pyproject.toml` requires `apsw>=3.53.4` with no ceiling, and the lock pins 3.53.4.0. The `id`
keyword and several traces per connection exist at the floor. The event dict's shape and the
`trigger` rule are APSW's. If a later APSW renames the key, the callback raises `KeyError`.
APSW documents that an exception in a trace callback is raised when Python code resumes, so
every statement would fail loudly. If a later APSW changes the `trigger` rule, the tests for R2,
R3 and triggers fail. The bundled SQLite has tracing compiled in: `SQLITE_OMIT_TRACE` is not in
`apsw.compile_options`.

**Verdict:** safe. No test needed beyond R1 to R3.

## R8. The sandbox and other tracers

The sandbox sets a cursor's `exec_trace`, which does not affect `trace_v2`. The existing test
`test_a_sandboxed_statement_takes_a_fresh_reading` covers it. None of the other ways to trace a
connection displaces the clock's trace: `set_profile`, a `trace_v2` without `id`,
`trace_v2(0, None)` without `id`, `apsw.ext.Trace` and `apsw.ext.ShowResourceUsage`. Only
`trace_v2` with the id `seahaven.clock` removes it (R10).

**Verdict:** safe. **Tests:** `test_other_trace_apis_leave_the_clocks_trace_in_place` and
`test_apsws_own_tracer_leaves_the_clocks_trace_in_place`, beside the existing
`test_another_trace_does_not_displace_the_clocks`.

## R9. Threads and the `inspect()` handle

The bundled SQLite is built `THREADSAFE=1` (serialized). A statement's callbacks run while the
connection's mutex is held, so two threads never touch one connection's reading at the same
time. When a second thread used a connection that was inside a step, the statement either waited
or raised `ThreadingViolationError` ("the Connection is busy in another thread"). Between rows,
another thread can run a statement on the same handle, and that is R6. Each connection has its
own reading, so a statement on `inspect()` and a statement in a call on another thread take
separate readings, as two statements should. `tick`'s call count is incremented under the
instance lock and read without it. The read is a single attribute read.

**Verdict:** safe apart from R6. No test added: the effect is R6's, and R6's test is the direct
one.

## R10. The trace removed, or never firing

If world code removes the trace (`trace_v2(..., id="seahaven.clock")`), that connection keeps its
first reading for the rest of its life. SQL on it stops moving while `ctx.clock` keeps moving.
Nothing reports an error. `reference/api.md` forbids the removal, and the existing test
`test_the_clock_trace_is_registered_under_its_id` pins what happens. In the pinned build, no
path was found in which the trace does not fire for a statement that starts running. The
re-prepare retry in R1 does not report, but its first run already has.

**Verdict:** silent failure, accepted. The removal is a documented prohibition and needs a
deliberate call. F1 does not help here, because it also runs inside the trace.

## R11. Cost

Best of repeated runs of 200,000 statements on an in-memory connection:

| Statement | No trace | With the clock's trace |
|---|---|---|
| `SELECT 1` | 0.73 µs | 1.93 µs |

The trace costs about 1.2 µs per event, including trigger events. The architecture's estimate was
1.4 µs. A statement that reads the clock also pays for a fresh reading, about 5.5 µs, where
removing the trace would reuse one stale reading. In the reference world, a `list_issues` call
runs 6 statements and a `create_issue` call runs 42 (23 top-level, plus trigger and FTS5 work).
Against calls of about 1 ms, the trace's share is about 1% and 5%.

**Verdict:** acceptable.

## Proposed fixes

### F1. Drop a reading that is older than the last call

Record the call count when a reading is taken, and also drop the reading on a trigger event if
the clock has counted a call since then:

```py
def iso(self) -> str:
    if self._iso is None:
        self._iso = self._clock.iso()
        self._taken_at = self._clock._calls
    return self._iso

def statement_started(self, event: dict[str, Any]) -> None:
    if not event["trigger"] or self._taken_at != self._clock._calls:
        self._iso = None
```

This fixes R4 under `tick`: a comment-led statement on `inspect()` after a call reads that call's
instant. Under `running` and `wall` it only limits R4: a reused reading is at most as old as the
last call start, which helps during an episode but not afterwards. F1 does not fix R6. R6 comes
from the drop on a non-trigger event, which F1 leaves as it is.

The check lives in the trace callback, not in `iso()`. A statement on `inspect()` that is running
when a call starts on another thread therefore keeps one reading, as long as it emits no
trigger-marked event after the call starts. A statement that does emit one, such as an FTS5 query
or a call to a Python function that runs a nested query, has its reading dropped at that event,
and a per-row date function in it can take two readings. This is reasoned from the code, not
reproduced.

The cost is not measurable: 2.08 µs against 2.13 µs per `SELECT 1`. With F1 in place, the full
framework suite passes, and of the four `xfail` tests only the `tick` `inspect()` test of R4
passes.

### F2. Count the statements that are running

Register `SQLITE_TRACE_STMT | SQLITE_TRACE_PROFILE`. The profile event fires when a statement
finishes, fails or is reset. Keep the set of top-level statements that are running. A statement
that starts when none is running is top-level whatever its text, which fixes R4. A statement that
starts while another is running shares that statement's reading, which fixes R6, the `tick` case
included.

```py
def traced(self, event: dict[str, Any]) -> None:
    if event["code"] == apsw.SQLITE_TRACE_PROFILE:
        self._active.discard(event["id"])
    elif not self._active:
        self._iso = None
        self._active.add(event["id"])
    elif not event["trigger"]:
        self._active.add(event["id"])
```

With this prototype in place, the full framework suite passes and all four `xfail` tests pass.

**Cost.** APSW builds a dict of statement counters for each profile event. The measured cost is:

| | Current | F2 |
|---|---|---|
| `SELECT 1` | 1.93 µs | 4.90 µs |
| projecttracker `list_issues` (6 statements) | 1168 µs | 1211 µs (+4%) |
| projecttracker `create_issue` (42 statement and 40 profile events) | 974 µs | 1140 µs (+17%) |

**Caveats.**

- A cursor that is paused and never closed makes every later statement on its connection share
  its reading until the cursor is closed, across calls too. Under `tick`, statements on that
  connection then read an old call's instant. CPython closes an abandoned cursor as soon as its
  last reference goes, so only a cursor that is kept alive causes this. Adding F1's call-count
  drop would limit this to one call. But it would also split a statement whose rows span a call
  start, which is the `tick` case of R6, so F2 as written leaves it out.
- After `SQLITE_SCHEMA`, a re-prepared statement fires its profile event before the retried
  run, and the retry does not report a start. During the retry the statement is not in the set,
  so a trigger program or a nested statement inside it looks top-level and drops the reading. A
  re-prepared write whose trigger stamps a row then takes two readings, where the current code
  takes one. This needs a schema change on another connection during an episode, so it is rare.

## Follow-ups

1. Decide between F2 and F1.
   - F2: remove all four `xfail` markers.
   - F1: remove the `xfail` from
     `test_tick_a_comment_led_read_through_inspect_reads_the_current_instant`. Then add one
     sentence to the `Clock` section of `reference/api.md`: a statement whose text starts with
     `-- ` can take an earlier statement's reading under `running` and `wall`. The same happens
     to a read with a per-row date function when another statement runs between its rows. Under
     `tick`, that read is affected only if a call also starts between the rows.
