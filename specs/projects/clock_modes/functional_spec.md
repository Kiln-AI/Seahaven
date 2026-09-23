---
status: draft
---

# Functional Spec: Clock Modes

An instance's clock gets a **mode**, chosen per instance at reset, with a per-world default. Four
modes:

| Mode | What the clock reads | Deterministic |
|---|---|---|
| `fixed` | The start instant, for the instance's whole life. Today's behaviour. | Yes |
| `tick` | The start instant plus one second per tool call dispatched so far. | Yes |
| `running` | The start instant plus the real time elapsed since the instance was created. | No |
| `wall` | The host's real wall-clock time, on every reading. The start instant is not used. | No |

The default for every world is `running` unless the world sets another. Seahaven is pre-v1, so
changing the default away from today's frozen clock is accepted (see `project_overview.md`).

## 1. Terms

- **Start instant (S).** Where an instance's clock begins: the fixture's `now` for an instance
  forked from a fixture; `now=` for a blank instance given one; the wall clock at creation for a
  blank instance without one. This is unchanged from today. `fixed`, `tick` and `running` count
  from S; `wall` ignores it.
- **Reading.** Any time something asks the clock what time it is: `ctx.clock.now()`,
  `ctx.clock.iso()`, `inst.clock.now()` / `.iso()`, and every SQL date and time function resolving
  `'now'` (`CURRENT_TIMESTAMP`, `CURRENT_DATE`, `CURRENT_TIME`, `datetime()`, `datetime('now')`,
  `strftime(..., 'now')`, `julianday('now')`, `unixepoch()`, `timediff('now', ...)` and the rest)
  on any connection the instance opens: the tool-call connection of every node, the `inspect()`
  handle, the control tool's handle, and the connection that builds a blank instance.
- **Call.** A tool call counted by `inst.call_count`, with its ordinal `i` (0-based). That is every
  tool call that reached the world, including one that raised a `ToolError` or was short-circuited
  by middleware. It excludes a name the world refused, a control tool, a nested call through a
  `ctx.worlds` handle, and `tools()`. This definition already exists and is reused unchanged.

A reading never changes the clock in any mode. Only calls (in `tick`) and the passage of real time
(in `running` and `wall`) move it.

## 2. The modes

### 2.1 `fixed`

Every reading is S. Behaviour is exactly today's.

### 2.2 `tick`

- Before the first call, every reading is S. That covers building a blank instance (a schema file
  that stamps seed rows), startup hooks, and any `inspect()` or state document read before the
  first call.
- During call `i`, every reading is `S + (i + 1)` seconds, in Python and in SQL alike. Every reading
  inside one call is the same instant, however many there are. A nested call through a `ctx.worlds`
  handle is part of the outer call and sees the outer call's instant.
- After call `i` returns (or raises) and until call `i + 1` starts, every reading is
  `S + (i + 1)` seconds. Equivalently, outside a call the clock reads `S + call_count` seconds.
- The first call's writes are therefore strictly later than every fixture row and every row a
  startup hook wrote.
- Two calls never share an instant, so ordering rows by timestamp orders them by call. Rows written
  within one call still share an instant.

### 2.3 `running`

- When an instance is created, the framework records S and the moment of creation on a monotonic
  clock. Creation is the moment the clock is made, before a blank instance's schema is built and
  before startup hooks run.
- Every reading is `S + (monotonic now − monotonic at creation)`, truncated to milliseconds like
  every clock value today. Readings are live: two readings in one call can differ, exactly as two
  reads of the real clock in a real system can. There is no per-call snapshot.
- Within one SQL statement, every `'now'` resolves to one instant, as SQLite itself guarantees for
  its own date functions. A `CURRENT_TIMESTAMP` in two columns of one `INSERT`, or in every row of a
  multi-row `INSERT`, is the same value.
- Readings never decrease. Elapsed time is measured on a monotonic clock, so a change to the host's
  system clock during a run does not move the world's clock.
- The clock keeps running while the instance is idle between calls.
- Two readings close together may be equal, because of millisecond truncation.

### 2.4 `wall`

- Every reading is the host's current UTC wall-clock time, truncated to milliseconds. S is not
  used: an instance forked from a fixture dated 2026-06-01 reads today's date from its first
  reading, and the fixture's rows keep their own dates.
- Readings are live and follow the same SQL rule as `running`: one instant per SQL statement.
- Readings follow the host's clock, so they can jump forwards or backwards if the host's clock is
  changed during a run. That is the difference from `running`.
- `now=` together with `clock_mode="wall"` is refused, because `wall` would ignore it. The error
  names both options. A fixture together with `wall` is accepted.

## 3. Choosing the mode

### 3.1 The world's default

`World(..., default_clock_mode=...)` sets the mode an instance of the world gets when the reset
does not name one.

- Optional. When omitted, the world's default is `running`.
- Validated when the `World` is created. An unknown value is refused with an error naming the value
  and listing `fixed`, `tick`, `running` and `wall`.
- Readable as `world.default_clock_mode`.

### 3.2 Per instance: the `clock_mode` reset option

`clock_mode` is a new Seahaven reset option, beside `fixture`, `seed`, `now` and `state_format`.

- Optional. When omitted or null, the instance uses its world's default.
- An unknown value is refused, naming the value and listing the four modes. The refusal happens
  before anything is created, the same way an unknown `state_format` is refused today.
- Combines with every other reset option, including `now=` and a fixture, except `now=` with
  `wall` (section 2.4). The same refusal applies when `wall` comes from the world's default.
- The mode is fixed for the instance's life. There is no way to change it after creation.

The option is available at every place that makes an instance for an eval or a test:

| Place | Spelling |
|---|---|
| Python | `world.instance(fixture, ..., clock_mode="tick")` |
| OpenEnv `reset` | `clock_mode` field in the reset request; the reset schema publishes it as an enum of the four values, with a description; the console form shows it after `now` |
| OpenEnv Python client | the same keyword the client already forwards for the other reset options |
| `seahaven mcp` | `--clock-mode MODE` convenience flag, `SEAHAVEN_CLOCK_MODE` environment variable, and `"clock_mode"` inside `--reset-options` JSON; refused together with `--reset-options` like the other convenience flags |
| pytest plugin | `clock_mode=` on the marker and the fixture factory, like `now=` and `state_format=` |

`seahaven fixture freeze` and `seahaven fixture fork` get no new flag. The instance each builds to
run the author's generator in uses the world's default mode. An author who wants another mode for
generation writes a generator that makes its own instance, as the projecttracker generator does.
A `wall` default together with `freeze --now` is refused like `now=` with `wall` anywhere else.

### 3.3 Composite instances

A composite instance has one clock, as today. Its mode is the reset's `clock_mode`, or else the
**root** world's `default_clock_mode`. The defaults of worlds the root adds are ignored. Every node,
every connection and every added world's tools read the one clock.

## 4. What the mode affects

### 4.1 Reading the mode

- `ctx.clock.mode` and `inst.clock.mode` answer the instance's mode, as its string value.
- `ctx.clock.now()` and `ctx.clock.iso()` answer the current reading as defined in section 2.
- `seahaven.Clock` stays public. `Clock(datetime)` builds a `fixed` clock at that instant, which is
  also a way to render a datetime as canonical timestamp text. Moving clocks are made by the
  framework when it creates an instance; a world author does not build one. The class docstring
  says what a `Clock` is (an instance's clock, read through `ctx.clock`), what constructing one
  directly gives (a `fixed` clock), and what it is not (a way to choose or change an instance's
  mode, which is the `clock_mode` reset option and the world's default).

### 4.2 The state document

- The envelope gains `clock_mode`, placed directly after `now`: the instance's mode as a string, or
  null before the first reset over OpenEnv. The envelope is documented as gaining fields over time,
  so the format names (`seahaven.state/1` and the rest) do not change.
- `now` is the clock's reading at the moment the document is produced. Producing a document is a
  reading and never ticks.

### 4.3 The OpenEnv reset observation

The reset observation's result (today `fixture`, `now`, `tools`) gains `clock_mode`. `now` is the
reading at the end of the reset.

### 4.4 Freezing

- A fixture's `now` is the instance's clock reading at the moment of the freeze, in every mode. A
  `running` instance started at 1998-01-01 and frozen after 40 minutes has a fixture `now` of
  1998-01-01 plus 40 minutes. A `tick` instance frozen after 12 calls has `S + 12s`. A `wall`
  instance has the wall-clock time of the freeze.
- An instance forked from that fixture starts at the fixture's `now`, in whatever mode it is given
  except `wall`, so a `running` or `tick` instance picks up from where the frozen one stopped.
- A fixture does not record a clock mode. The sidecar format does not change.

### 4.5 Reproducibility

- `fixed` and `tick` are deterministic: the same fixture, seed, mode and calls produce the same
  result and the same timestamps, as `fixed` does today.
- `running` and `wall` are not: timestamps depend on how long the agent and the harness took, and
  for `wall` on the date of the run. The docs say so, and point to `tick` for an eval that needs
  replayable timestamps with an order between calls.
- The docs' statement that a world reads the wall clock in exactly two places becomes true of
  `fixed`, `tick` and `running` only.
- The framework does not make fixture generation deterministic. A generator that wants the same
  fixture on every run picks a deterministic mode itself. The projecttracker generator already
  promises byte-identical regeneration, so it passes `clock_mode="fixed"`.

## 5. Schema rules

A moving clock makes `'now'` in an index expression, a generated column, a `CHECK` constraint or a
partial index store a value that is stale by the next reading. The existing rule already forbids
any wall-clock expression anywhere in a world's schema (`SH103`, severity error). It stays as it
is; no new rule is added.

## 6. Reference world and tests

- The projecttracker world keeps the framework default (`running`). Its fixture generator pins
  `clock_mode="fixed"` (section 4.5).
- `wall` is tested against a stubbed wall clock, not by sleeping.
- Framework and projecttracker tests that assert exact timestamps pin `fixed` or `tick`. Tests
  that exercise the new modes pin the mode they test. No test depends on the default except the
  tests of the default itself.
- Each mode is tested through `world.instance(...)` as well as at the unit level, covering the
  Python and SQL readings agreeing, startup hooks, `inspect()`, the state document, freeze and
  fork, composite instances, and every place in section 3.2's table.

## 7. Documentation

The bundled docs change where they describe the clock as frozen or static:

- `concepts.md` "Clock": the four modes, the default, how to choose one; "Reproducibility": which
  modes replay.
- `authoring.md` "Ordering within one run": `tick` orders rows across calls.
- `state.md`: the `clock_mode` envelope field and what `now` means for a moving clock.
- `serving_and_openenv.md`, `reference/api.md`, `reference/cli.md`, `testing.md`: the new option
  and flag at each place.
- `db_schema_and_fixtures.md`: a fixture's `now` is the reading at freeze.

## 8. Out of scope

- Tick sizes other than one second, and a configurable tick.
- Pausing, setting or moving the clock after creation, including a control tool to do so.
- A clock that runs faster or slower than real time.
- Recording the mode in a fixture.
- Changing `SH103` or adding schema rules.
