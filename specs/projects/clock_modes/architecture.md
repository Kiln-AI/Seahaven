---
status: complete
---

# Architecture: Clock Modes

Single document: the change is small in files touched and has one hard problem (section 3).

## 1. Shape of the change

One `Clock` object per instance stays the single source of truth. It already reaches every
reader by reference: `Instance.clock`, every node's `Ctx.clock` (per-call contexts are shallow
copies), and every connection through `register_clock_functions`. What changes:

| Where | Change |
|---|---|
| `clock.py` | `Clock` gains a mode and computes each reading on demand. The SQL overrides read the clock per statement instead of capturing an instant at registration. |
| `world.py` | `World(default_clock_mode=...)`; `world.instance(clock_mode=...)`. |
| `instances.py` | Resolve and validate the mode; make the clock before building or copying; advance a `tick` clock when a call takes its ordinal. |
| `state.py` | Envelope field `clock_mode`. |
| `openenv/env.py`, `openenv/client.py` | Reset option, schema, observation, `State` model field. |
| `cli/mcp.py` | `--clock-mode`, `SEAHAVEN_CLOCK_MODE`. |
| `pytest_plugin.py` | Marker signature text. |
| `__init__.py` | Export `ClockMode`. |
| projecttracker | Generator pins `clock_mode="fixed"`. |

No new dependency. No change to fixtures, the sidecar, lints or the sandbox.

## 2. `clock.py`

### 2.1 Mode type and constants

```py
type ClockMode = Literal["fixed", "tick", "running", "wall"]
CLOCK_MODES: tuple[ClockMode, ...] = ("fixed", "tick", "running", "wall")
DEFAULT_CLOCK_MODE: ClockMode = "running"
TICK = timedelta(seconds=1)

def check_clock_mode(value: object) -> ClockMode:
    """`value` if it names a mode, else `WorldBug`."""
```

The refusal text, used everywhere a mode is accepted:
`unknown clock mode 'tik'; the clock modes are 'fixed', 'tick', 'running' and 'wall'`.
Callers prefix their own context where they have one (`World` prefixes `world 'x': `).

A `Literal` rather than an `Enum`: every door takes the string (a reset over the wire, an
environment variable), `state_format` is a plain string today, and `ty` checks a `Literal` at
Python call sites. `ClockMode` is exported from `seahaven` and listed in `reference/api.md`;
`CLOCK_MODES` and `check_clock_mode` are importable from `seahaven.clock`.

### 2.2 Time sources

```py
def _wall_now() -> datetime: return datetime.now(UTC)
_monotonic_ns = time.monotonic_ns
```

Module-level names so tests replace them with `monkeypatch.setattr(seahaven.clock, ...)`. Nothing
else in `clock.py` reads real time. `Clock.wall()` switches to `_wall_now()`.

### 2.3 `Clock`

```py
class Clock:
    def __init__(self, now: datetime, mode: ClockMode = "fixed") -> None
    @classmethod
    def from_iso(cls, text: str, mode: ClockMode = "fixed") -> Self
    @classmethod
    def wall(cls) -> Self                 # unchanged meaning: a fixed clock at the wall time
    @property
    def mode(self) -> ClockMode
    def now(self) -> datetime             # the current reading
    def iso(self) -> str                  # the current reading, canonical text
    def _call_started(self) -> None       # framework-internal; see 4.3
    def __repr__(self) -> str             # "Clock(2024-03-05T12:00:00.123Z, fixed)"
```

State: `_start` (S, aware UTC, truncated to milliseconds exactly as today), `_mode`
(checked with `check_clock_mode`), `_calls: int = 0`, `_origin_ns = _monotonic_ns()` taken in
`__init__` for every mode (cheap, and it keeps construction one path).

`now()`:

| Mode | Reading |
|---|---|
| `fixed` | `_start` |
| `tick` | `_start + _calls * TICK` |
| `running` | `_start + timedelta(milliseconds=(_monotonic_ns() - _origin_ns) // 1_000_000)` |
| `wall` | `_wall_now()` truncated to milliseconds, through the same truncation `__init__` uses |

`iso()` renders one `now()` call, so one `iso()` is one reading. The existing rendering code is
kept.

`__eq__` and `__hash__` are removed; a `Clock` compares by identity. A clock whose reading moves
cannot hash by its reading, and nothing in `src/` compares clocks. The `test_clock.py` cases that
compare two fixed clocks compare `.iso()` or `.now()` instead.

The class docstring states: a `Clock` is an instance's clock, which world code reads through
`ctx.clock`; the framework makes it when it creates an instance, in the instance's mode;
`Clock(datetime)` built directly is a `fixed` clock at that instant, which is also the way to
render a datetime as canonical text; building a `Clock` does not choose or change any instance's
mode, which is the `clock_mode` reset option and `World(default_clock_mode=...)`.

## 3. The hard problem: one reading per SQL statement

Today each override closes over `instant = clock.iso()`, fixed at registration. Reading the clock
on each call instead is not enough under `running` and `wall`. Measured on the project's APSW
(3.53.4): an `INSERT` into a table with `DEFAULT (CURRENT_TIMESTAMP)` on two columns calls the
function once per column, so the two columns would get two different readings, and a trigger's
body calls it again. The spec requires one reading per statement, as SQLite gives its own
functions.

### 3.1 Design

`register_clock_functions(conn, clock)` makes one `_StatementReading` per connection:

```py
class _StatementReading:
    def __init__(self, clock: Clock) -> None: self._clock = clock; self._iso: str | None = None
    def iso(self) -> str:
        if self._iso is None: self._iso = self._clock.iso()
        return self._iso
    def statement_started(self, event: dict[str, Any]) -> None:
        if not event["trigger"]: self._iso = None
```

and registers `conn.trace_v2(apsw.SQLITE_TRACE_STMT, reading.statement_started, id=TRACE_ID)`,
with `TRACE_ID = "seahaven.clock"`. Every override (`_override`, `_constant`) calls
`reading.iso()` where it used the captured `instant`. The reading is taken lazily, at the first
date function a statement evaluates, and dropped when the next top-level statement starts.

Why `trace_v2` and not the alternatives, each checked against APSW 3.53.4:

- **`SQLITE_TRACE_STMT` fires once per statement execution**, at its start: once per statement in
  a multi-statement string, once per binding set in `executemany`, once for a `SELECT` however many
  rows it steps through. It fires again for each trigger program, with `trigger: True`, which is
  why those events are ignored: a trigger runs inside its statement and shares its reading.
- **It is connection-level and not displaced by a cursor's `exec_trace`.** The sandbox sets
  `cursor.exec_trace` for every agent statement (`sandbox.py`), and APSW uses a cursor's exec
  tracer *instead of* the connection's, so `Connection.exec_trace` would miss every sandboxed
  statement. `trace_v2` still fires for them.
- **APSW keeps several `trace_v2` callbacks per connection**, keyed by `id`, so a world or a test
  adding its own trace does not remove the clock's.
- **Cost:** about 1.4 µs per statement, measured. Installed for every mode, `fixed` included, so
  there is one code path; under `fixed` the reset is harmless.

`Db.conn`'s docstring adds the one thing world code must not do: remove a trace registered under
`seahaven.clock`.

The constants (`CURRENT_TIMESTAMP`, `CURRENT_DATE`, `CURRENT_TIME`) are evaluated on the helper
connection per call, like the functions, with a one-entry cache keyed by the reading's text so a
statement that uses one on every row runs the helper query once.

The functions keep `SQLITE_INNOCUOUS | SQLITE_DETERMINISTIC`. Deterministic lets SQLite evaluate a
call with constant arguments once per statement execution, never at prepare time and never across
executions (verified: a cached prepared statement re-run sees a new reading). That agrees with the
per-statement reading. The remaining risk of the flag, `'now'` in an index expression, generated
column or `CHECK`, is what `SH103` already refuses.

### 3.2 Tick and fixed

`tick` changes only when a call starts (4.3), so within a statement its reading is constant
anyway; the per-statement reading also covers a cursor on the `inspect()` handle that is still
being stepped when a call starts on another thread.

## 4. Instances

### 4.1 `World`

`World.__init__` gains `default_clock_mode: ClockMode = DEFAULT_CLOCK_MODE` (keyword-only, after
`state_format`), stored as `self.default_clock_mode` after `check_clock_mode`, whose `WorldBug` is
re-raised with the prefix `world {name!r}: `. `__copy__` needs nothing: it copies attributes.

`World.instance(...)` gains `clock_mode: ClockMode | None = None` after `now`, passed to
`_instances().create`. The docstring gains one sentence: the mode the instance's clock runs in,
`None` for the world's default.

### 4.2 `InstanceManager.create`

1. In the refusal block, before any directory exists and next to the `state_format` resolution:
   `mode = check_clock_mode(clock_mode) if clock_mode is not None else world.default_clock_mode`.
   `world` is the root, so a composite's added worlds' defaults are never read.
2. The start instant, then the clock, before either branch builds or copies:
   ```py
   if fixture is not None:
       clock = Clock.from_iso(fixture.now, mode)
   elif now is not None:
       clock = _clock_from(now, mode)
   else:
       clock = Clock(Clock.wall().now(), mode)
   ```
   `_clock_from` gains the `mode` parameter. The blank branch passes `clock` to `build_blank` as
   today; the fixture branch no longer makes its own after copying, so a `running` clock starts
   before the copy, as the spec's "creation" requires.
3. `now=` with a fixture stays refused in `_fixture` as today. `now=` with `wall` is accepted.

### 4.3 Advancing `tick`

`Instance._next_ordinal` is the one place a call takes its ordinal, under the instance lock, before
the chain runs. It calls `self.clock._call_started()` after incrementing `_call_count`, so during
call `i` the clock has counted `i + 1` calls, and outside a call it has counted `call_count`.
`_call_started` increments `_calls` in every mode; only `tick` reads it. Refused names, control
tools, nested handle calls and `tools()` never reach `_next_ordinal`, which is exactly the spec's
set.

### 4.4 Readers that need nothing new

`Instance.freeze` writes `now=instance.clock.iso()`: the reading at the freeze, as specified.
`created_at` keeps `Clock.wall().iso()`. `inspect()`, the control handle and `build_blank` all go
through `register_clock_functions` and follow the clock.

## 5. Doors

### 5.1 State document (`state.py`)

`envelope()` adds `"clock_mode": instance.clock.mode if instance is not None else None` directly
after `"now"`. The OpenEnv `State` model in `openenv/env.py` gains
`clock_mode: ClockMode | None` after `now`, described as "How the instance's clock moves: fixed,
tick, running or wall; null before the first reset." The `now` description drops "frozen".

### 5.2 OpenEnv (`openenv/env.py`, `openenv/client.py`)

- `SeahavenResetRequest` gains
  `clock_mode: ClockMode | None = Field(default=None, description="How the instance's clock moves: fixed, tick, running or wall. Null uses the world's default.")`.
  The `Literal` publishes as a JSON Schema `enum`; `for_world` needs no change for it.
- `RESET_ORDER = ("fixture", "startup", "now", "clock_mode", "state_format")`.
- `SeahavenEnv.reset` gains `clock_mode: str | None = None` (keyword-only, after `now`) and passes
  it to `create`; validation is `create`'s `WorldBug`, surfaced as every reset refusal is today.
- The reset observation's `metadata` gains `"clock_mode": instance.clock.mode`.
- `client.py`: docstrings that list the observation keys add `clock_mode`; the client forwards
  reset keywords as it does today.
- The console renders the reset form from the schema; an `enum` field renders as a choice, as
  `state_format` already does. No console change is expected; a test confirms the schema.

### 5.3 MCP (`cli/mcp.py`)

- `CONVENIENCE` gains `"clock_mode": "SEAHAVEN_CLOCK_MODE"`.
- A `--clock-mode MODE` argument, a string like `--now` (no argparse `choices`, so a bad value is
  the one-line `WorldBug` refusal and exit 1 like every other refusal here). Help:
  `how the instance's clock moves: fixed, tick, running or wall ($SEAHAVEN_CLOCK_MODE); the
  default is the world's`.
- `--reset-options` help names `--clock-mode` among the flags it cannot be combined with; the
  existing conflict check is driven by `CONVENIENCE` and needs no other change.
- `RESET_OPTION_KEYS` is read off `World.instance`'s signature, so it picks up `clock_mode`
  itself; `tests/test_cli_mcp.py`'s pin of the names goes from five to six.
- The `_options` docstring sentence about the clock is updated: the start is the fixture's `now`
  or wall time for a blank instance, and the mode is the world's unless given.

### 5.4 pytest plugin

`_MARKER_SIGNATURE` adds `clock_mode=None` after `now=None`. The plugin already forwards every
marker keyword but `fixture`, so nothing else changes.

### 5.5 Fixture CLI

No change. `seahaven fixture freeze` and `fork` build their instance with `world.instance(...)`
and so get the world's default.

## 6. Reference world

`worlds/projecttracker/fixtures_src/generate.py` `freeze()` becomes
`into.instance(None, now=NOW, clock_mode="fixed")`, with its docstring saying why: byte-identical
regeneration. Its world keeps the default. Projecttracker tests that assert exact timestamps pin
`clock_mode="fixed"` (or `tick` where call order is what they check).

## 7. Error handling

One refusal, `WorldBug` from `check_clock_mode`, at `World(...)` and at instance creation. Instance
creation refuses before a directory exists, so nothing is left behind. Over OpenEnv and MCP it
travels the way an unknown `state_format` travels today. The per-statement trace callback cannot
raise: it reads one dict key and assigns `None`.

## 8. Testing

Real time is never slept on. `running` and `wall` tests replace `seahaven.clock._monotonic_ns`
and `seahaven.clock._wall_now` with `monkeypatch`, usually with a fake that advances a fixed step
per read, so "each reading differs" and "one reading per statement" are both observable.

**Adapting the existing suites.** Changing the default moves every test that asserts an exact
timestamp through a default-mode instance. Each is fixed at its call site with
`clock_mode="fixed"` (in `world.instance(...)`, a pytest marker, or a reset), never by giving a
test world a non-default `default_clock_mode`, so the default stays exercised. Tests that
construct a `Clock` directly are unaffected except for equality (2.3) and `repr`.

**New tests**, by file:

- `tests/test_clock.py`
  - each mode's reading from stubbed sources; `running` truncates elapsed time to milliseconds and
    never decreases; `wall` ignores the start; `tick` counts `_call_started`
  - an unknown mode is refused with the four names; `Clock(dt)` is `fixed`; `repr`
  - SQL under a stepping `running` clock: two `DEFAULT (CURRENT_TIMESTAMP)` columns get one value;
    a trigger's `CURRENT_TIMESTAMP` equals its statement's; every row of a multi-row `INSERT`
    shares it; the next statement gets a later one; each `executemany` binding gets its own;
    `datetime('now')`, `strftime('%s','now')`, `julianday()`, `unixepoch()` follow the clock
  - a statement run through the sandbox (cursor `exec_trace` set) still gets a fresh reading
  - a second `trace_v2` registered on the connection does not stop the clock's
- `tests/test_world.py`: `default_clock_mode` defaults to `running`, accepts each mode, refuses an
  unknown one with the world's name
- `tests/test_instances.py`, all through `world.instance(...)`:
  - no `clock_mode` gives the world's default; the argument overrides it; an unknown one is
    refused and leaves no instance directory
  - `tick`: a startup hook and a schema-seeded row see S; call `i` sees `S + (i+1)s` in Python and
    in SQL, and they agree; a `ToolError` call ticks; an unknown tool name and
    `controller_run_sql` do not; a nested `ctx.worlds` call sees the outer call's instant;
    `inspect()` and `inst.state()` read without ticking
  - `running`: readings advance with the stubbed monotonic clock; freeze records the reading;
    an instance forked from that fixture starts at it
  - `wall`: reads the stubbed wall clock; `now=` and a fixture are both accepted
  - a composite instance takes the root's default and ignores an added world's; every node's SQL
    and the added world's tools read one clock
- `tests/test_state.py`: `clock_mode` follows `now` in the envelope; null before a reset
- `tests/test_env.py`: the reset parameter pin gains `clock_mode`; the reset schema publishes the
  four-value enum in `RESET_ORDER` order; the observation and `State` carry the mode
- `tests/test_cli_mcp.py`: six reset keys; `--clock-mode` and `SEAHAVEN_CLOCK_MODE` reach the
  instance; combining either with `--reset-options` is refused
- `tests/test_pytest_plugin.py`: `clock_mode=` on a marker reaches the instance
- projecttracker: the generator regenerates a byte-identical fixture with the default now
  `running`

The docs examples run under the suite; examples that show a moving clock use `tick`, whose
readings are exact.
