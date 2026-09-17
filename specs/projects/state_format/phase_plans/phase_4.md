---
status: complete
---

# Phase 4: Remove the old surface

## Overview

Phases 1-3 built the change log, the state document and the OpenEnv surface beside the old
changeset API. This phase takes the old one away: `Instance.changes()`, the `Change` class,
`changes.render()`, the long-lived session that fed them, and the `controller_changes` control
tool. `controller_run_sql` survives and is marked deprecated (ARCH §8, FS §11).

Two consequences reach further than the deletions. The fold test loses its oracle —
`instance.changes()` *was* the oracle — so the oracle moves into test code as a session the test
opens for itself (ARCH §14, FS §3.6). And every test and doc example that read `changes()` has to
be rewritten to the log, minimally: the docs test executes every example, so a missed one fails
CI. The docs' *rewrite* is phase 5; what this phase owes them is accuracy and a green suite.

Finally the performance work ARCH §16 asks for: a `bench/` probe measuring what the per-call
session costs a call, and the `state()` bound FS §13 names. The measured number is recorded at the
bottom of this plan.

## Steps

1. **`seahaven/changes.py`.** Delete `Change` and `render()`; trim `__all__` to `LogRecord`,
   `open_session`, `render_log`, `tracked_tables`. Rewrite the module docstring: there is one
   reading of the mechanism now, not two. The no-primary-key refusal says "the change log could
   not record its writes".

2. **`seahaven/instances.py`.** Delete `Instance.changes()`, the `session` constructor parameter,
   `self._session`, the `open_session(...)` call in `InstanceManager.create` and the
   `self._session.close()` in `_close`. Drop the now-unused `apsw` import and the `Change`/`render`
   imports. Update the module docstring (an instance is no longer "a changeset session") and
   `_close`'s.

3. **`seahaven/control.py`.** Delete `controller_changes`, its `Tool` and its entry in `TOOLS`.
   Add a module-level `DEPRECATED = frozenset({"controller_run_sql"})` and, in `dispatch`, before
   validating the arguments:

   ```python
   if call.tool.name in DEPRECATED:
       warnings.warn(
           f"{call.tool.name} is deprecated: read inst.state() instead",
           DeprecationWarning,
           stacklevel=2,
       )
   ```

   `controller_run_sql`'s docstring opens with `Deprecated: read inst.state() instead.`, which is
   also the tool's description in the registry.

4. **`seahaven/world.py`.** `CONTROL_TOOL_NAMES = frozenset({"controller_run_sql"})`, so a world
   may now register a tool named `controller_changes`.

5. **`seahaven/__init__.py`.** Drop `Change` from the imports and from `__all__`.

6. **Comments naming the old surface**: `sandbox.py`'s note on the session's `table_xinfo` pragma,
   `tool.py`'s `control` flag, `openenv/__init__.py`'s `include_control_tools` docstring.

7. **`tests/support/oracle.py`** (new). SQLite's own net diff of a whole episode, which is what
   `instance.changes()` used to supply:

   ```python
   @contextmanager
   def recording(instance: Instance) -> Iterator[Oracle]: ...

   class Oracle:
       def net_diff(self) -> list[NetChange]: ...
   ```

   It opens `apsw.Session` on `instance.db.conn` through `changes.open_session`, attached to
   `instance._tracked`, for the length of the block — after creation, so the startup hooks stay
   out of it, exactly as the removed long-lived session did. `net_diff()` renders the changeset
   with `render_log` and maps the records to `NetChange`.

8. **`tests/support/fold.py`.** Delete `net_of_changes` (its `Change` input no longer exists) and
   the `Change` import; `NetChange` and `fold` are unchanged.

9. **`tests/test_fold.py`.** `net_of_changes(instance.changes())` becomes the oracle block:

   ```python
   with started.instance("start") as instance, recording(instance) as sqlite:
       episode(instance)
       assert fold(instance.change_log()) == sqlite.net_diff()
   ```

10. **`tests/test_changes.py`.** Delete the `Change` half of the file. The cases it held that the
    `LogRecord` half does not — an untracked table, an FTS5 virtual table, a table with no explicit
    primary key (refused, or untracked instead), and a log that survives a `freeze` — are rewritten
    against `change_log()` and kept. Rewrite the module docstring.

11. **`tests/test_control.py`.** Delete the `controller_changes` tests. Add: every
    `controller_run_sql` call raises `DeprecationWarning` (`pytest.warns`), in process; a
    `controller_changes` call is `UnknownTool`; a world may register a tool of that name and it
    behaves as an ordinary tool.

12. **The remaining suites**, each rewritten to `change_log()`/`state()` with no change of intent:
    `tests/test_change_log.py` (drop the `controller_changes` call from the control-tools test),
    `tests/test_describe_schema.py`, `tests/test_run_sql.py`, `tests/test_env.py`,
    `tests/test_world.py`, `tests/test_bench.py`, `tests/test_fts5.py`,
    `tests/test_docs_examples.py`, `tests/test_server.py`,
    `worlds/projecttracker/tests/{test_package,test_sql_tools,test_determinism,test_errors}.py`,
    `extensions/seahaven-xmlrpc/tests/test_faults.py`.

13. **The docs, minimally** — phase 5 rewrites them. Every executed example that calls `changes()`
    reads the log instead; every `controller_changes` row, list entry and sentence goes;
    `reference/api.md` loses `Change`, `Instance.changes` and `controller_changes`; `README.md`'s
    `grade(world_instance.changes())` becomes `grade(world_instance.state())`.

14. **`bench/recording.py`** (new), ARCH §16's probe. One instance per workload, timed three ways
    with `Instance._recording` shadowed for the two comparison legs: as Seahaven runs it; a session
    opened and attached per call but never read; and one long-lived session for the whole pass,
    which is the shape before this release. Three legs and not two because two would say how much
    and not where.

    ```python
    @dataclass(frozen=True)
    class Recording:
        workload: str
        calls: int
        per_call_seconds: float
        unread_seconds: float
        long_lived_seconds: float
        wrote_rows: bool

        @property
        def overhead(self) -> float: ...    # against the long-lived leg
        @property
        def rendering(self) -> float: ...   # the part of it that is changeset() + render_log
    ```

    Two shares and not three: the report prints the fresh-session remainder as `total - rendering`,
    so that the table and the prose stay consistent under rounding, and a third property would only
    round it differently.

    `recording()` takes the workload by name and both are run, so the read figure and the write
    figure are reproducible from what ships. Each comparison leg yields a `HasRecorded` callable
    over its own session's `is_empty`, which is what lets a test see that the leg is still
    recording and lets the probe report `wrote_rows` — a fact asked of the sessions rather than
    assumed from the workload's name.

    Wired in as a `recording` subcommand in `bench/__main__.py` (and into `all`), a section in
    `bench/report.py`, and toy-size tests in `tests/test_bench.py` that prove every leg of every
    workload ran, that the shares are the differences they claim, and that each comparison leg
    still records, writes no log, and puts the real recorder back.

15. **`tests/test_state.py`.** FS §13's stated bound: `state()` on an instance carrying 1,000 log
    records returns in well under a second. Deliberately loose — it is there to catch a `state()`
    that went back to the database or grew quadratic, not to police a machine.

16. **`BACKLOG.md`.** The one entry that describes a bug found through `Instance.changes()` names
    the change log instead.

## Tests

- `test_a_table_the_world_lists_as_untracked_is_not_logged`: an untracked table's writes are
  absent from the log while a tracked table's are in it.
- `test_writes_to_an_fts5_table_are_not_logged`: a virtual table and its shadow tables never
  reach the log.
- `test_a_table_with_no_explicit_primary_key_is_refused`: `WorldBug` at instance creation,
  naming the table.
- `test_a_table_with_no_primary_key_can_be_untracked_instead`: named in `untracked_tables`, the
  instance is made and its writes are not logged.
- `test_the_log_survives_a_freeze`: records from before and after `freeze()` are both in it.
- `test_controller_run_sql_warns_that_it_is_deprecated`: `pytest.warns(DeprecationWarning)` around
  the call, and the result is still the rows.
- `test_every_controller_run_sql_call_warns`: two calls, two warnings — not once per process.
- `test_controller_changes_is_gone`: `UnknownTool`, in process.
- `test_a_world_may_register_a_tool_named_controller_changes`: it registers, it is listed, and it
  runs as an ordinary tool.
- `test_the_fold_of_the_log_is_the_cumulative_changeset` (rewritten): parametrised over every
  episode shape, now against `tests/support/oracle.py`.
- `test_the_oracle_sees_what_the_instance_saw`: the oracle is not vacuously empty — an episode's
  net diff has the rows the episode left behind.
- `test_state_is_fast_with_a_long_log`: 1,000 records, one `state()`, under the stated bound.
- `test_the_recording_probe_times_every_leg_of_a_workload`, parametrised over both workloads, and
  `test_a_comparison_leg_records_but_writes_no_log_and_is_put_back`, parametrised over both
  comparison legs, in `tests/test_bench.py`.

## Measured performance (ARCH §16)

    uv run python -m bench recording --calls 400 --repeats 5

Nineteen runs of that command in fresh processes, on Linux 6.18 x86_64, 4 CPUs, CPython 3.14.7,
APSW 3.53.4.0 on SQLite 3.53.4. ProjectTracker `agency`, nine tracked tables, 2,000 calls down
each leg. The benchmark is never a gate and nothing asserts these numbers.

**Quote the command with the number.** The figures are flag-sensitive: a bare `python -m bench
recording` takes the sweep's defaults (200 calls, 3 repeats), which is a short enough pass to be
dominated by its own noise — it gave +28% and +34% for the write mix and +12% and +18% for the read
on the same machine in the same session.

| Workload | Leg | Per call | Against one long-lived session |
|---|---|---:|---:|
| `write_mix` | a session per call (shipped) | 0.70-0.76 ms | **about +40%** (+32% to +49%) |
| `write_mix` | a session per call, never read | 0.56-0.60 ms | the remainder below |
| `write_mix` | one long-lived session | 0.50-0.53 ms | -- |
| `read` | a session per call (shipped) | 0.12-0.13 ms | **about +10%** (+3% to +14%) |
| `read` | a session per call, never read | 0.12 ms | noise |
| `read` | one long-lived session | 0.11-0.12 ms | -- |

**ARCH §16 expected this to be within noise; it is not.** A write-heavy call costs about 40% more
than it did under the long-lived session, across every run of the documented command. A read-only call
costs about 10% more, on a call an order of magnitude cheaper, so the framework's headline
one-row-read figure is barely moved.

**Where it goes, and what §16 got wrong.** §16 reasoned that "the per-row recording work is the
same as today's" and that what is new is "one `Session` open and `attach` per tracked table per
call". The probe's middle leg says otherwise, and says it with its own numbers rather than an
aside:

- **`changeset()` and `render_log` are the larger part of the write mix's cost**, roughly 29-34 of
  its ~40 points, and the stable share across every run. §16 counted that work as unchanged; it is
  not. It used to run once an episode, when an eval called `changes()`, and it runs once per call
  now. That is inherent to a per-call record, not a cost to tune away.
- **The rest is everything a *fresh* session costs over a warmed-up one** — opening it, attaching
  its tables, its first sighting of each table it records (the `PRAGMA table_xinfo` `sandbox.py`'s
  authorizer comment documents), and freeing a populated change buffer at `close()`. The probe
  does not separate those four, so neither the report nor this plan attributes the leg to any one
  of them; §16's reduction of it to an open and N attaches is what this phase cannot support.
  Its weight differs by workload: it is the minority of the write mix's total, and it is the
  *whole* of the read's, whose calls write no rows and so have nothing to render and nothing to
  free.

**Read the split loosely.** The fresh-session leg is the noisiest of the three and has been
observed to move by more than its own size between runs, while the rendering share holds. The
totals are solid; the split is an attribution. The report says so in the same words, and for a
workload whose calls gave their sessions no rows — which the probe asks the sessions rather than
assuming from the workload's name — it suppresses the split entirely.
