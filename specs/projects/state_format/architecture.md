---
status: draft
---

# Architecture: State Format

How `functional_spec.md` is built into the code as it stands. Single document: the change is
wide (it touches the instance, the world, the OpenEnv layer, the CLI scaffold and the docs) but
every piece is small, and no component has enough internal complexity for a document of its own.
Section numbers in `functional_spec.md` are cited as "FS §n".

## 1. Module map

| Module | Change |
|---|---|
| `seahaven/changes.py` | Gains `LogRecord`, `render_log()`, a per-instance column cache, the JSON value mapping (`_jsonable` handles infinities), and `tracked_tables()`. `Change`, `render()` and `start_session()` keep their behaviour. |
| `seahaven/state.py` | **New.** The formatter protocol, the two built-in formatters, format-name validation, the pre-reset document, and `SEAHAVEN_STATE_V1` / `SEAHAVEN_STATE_LAST_STEP_V1` name constants. |
| `seahaven/instances.py` | `Instance` gains the log store, the call counter, `state()`, `change_log()`, `call_count`, `episode_id`, `caller_seed`, `fixture_sha256`, `startup`, `state_format`; per-call and per-bulk sessions; the formatting guard. `InstanceManager.create` gains `state_format` and `episode_id`, resolves the formatter first, serialises the startup keywords. |
| `seahaven/world.py` | `World(..., state_format: str)` required; `RESET_ARGUMENTS` gains `state_format`; `world.state_format()` registration; `world.resolve_state_format()`; `__copy__` carries the registry. |
| `seahaven/ids.py` | `_seed_bytes` and `instance_seed` narrow to `int | None`. |
| `seahaven/pytest_plugin.py` | The marker's `seed` narrows to `int | None`; `state_format` is passed through if given. |
| `seahaven/openenv/env.py` | `reset(state_format=...)`; `state` answers the document; `SeahavenState` is an open model over the document. |
| `seahaven/openenv/client.py` | Docstring example only; `_parse_state` is unchanged. |
| `seahaven/control.py` | `DeprecationWarning` in `dispatch`; docstrings name `state()`. |
| `seahaven/cli/templates/base/src/PACKAGE/world.py.tmpl` | `state_format="seahaven.state/1"`. |
| `seahaven/__init__.py` | Exports `LogRecord`. |
| Every `World(...)` in the repo | Gains `state_format=`. 38 files; the sweep is one phase. |
| Docs | FS §12. |

## 2. Data model

### 2.1 `LogRecord` (`changes.py`)

```python
@dataclass(frozen=True)
class LogRecord:
    i: int | None
    subworld: str | None            # always None in this release
    table: str
    op: Literal["insert", "update", "delete"]
    key: dict[str, Any]
    before: dict[str, Any] | None
    after: dict[str, Any] | None

    def to_dict(self) -> dict[str, Any]   # FS §3.2 field order: i, subworld, table, op, key, before, after
```

`Change` is unchanged (its update `before` keeps the key columns; `LogRecord`'s does not). The two
are rendered by two functions over the same changeset iterator so neither depends on the other.

### 2.2 The log store (`Instance`)

- `self._log: list[LogRecord]`, appended in commit order, read under the instance lock.
- `self._call_count: int`, incremented per dispatched call (§4).
- `self._columns: dict[str, tuple[list[str], list[int]]]`, the per-table column names and key
  positions, filled lazily by `render_log` and `render`; a world's schema does not change for the
  life of an instance, so it is never invalidated.
- `self._tracked: tuple[str, ...]`, the tables the sessions attach, computed once at creation by
  `changes.tracked_tables(conn, world)` (the loop `start_session` runs today, factored out so the
  cumulative session and every per-call session attach the same list).

### 2.3 The state document

A `dict[str, Any]` built by a formatter (§5). Never a model in process; the OpenEnv layer wraps it
(§9).

## 3. Capturing the change log

### 3.1 One SQLite session per call

The cumulative session (`self._session`, attached at creation after the startup hooks) stays
exactly as it is: it is what `changes()` renders, and `changes()` is the fold's reference (FS
§3.6). Beside it, **every call gets a session of its own**:

```python
def _call(self, name, arguments):
    ...
    with gate(bypass=tool.control), self._held():
        self._refuse_if_formatting()
        if tool.control:
            return control.dispatch(self, ctx)           # not a call: no ordinal, no session
        i = self._next_ordinal()
        with self._recording(i):
            return world.chain(ctx, call)
```

```python
@contextmanager
def _recording(self, i: int | None) -> Iterator[None]:
    session = apsw.Session(self.db.conn, "main")
    for table in self._tracked:
        session.attach(table)
    try:
        yield
    finally:
        # After the call's transaction has committed or rolled back and before
        # any other write: `changeset()` joins what the session recorded against
        # the live table, so a rolled-back row, or one written back to its
        # original values, contributes nothing (FS §3.2 "net per call").
        changeset = session.changeset()
        session.close()
        if changeset:
            self._log.extend(render_log(changeset, self.db.conn, self._columns, i=i))
```

Why not the alternatives:

- Diffing consecutive cumulative changesets (`Changeset.concat(Changeset.invert(prev), cur)`) is
  O(total changes) per call: quadratic inside Seahaven, which is the cost FS §4 exists to avoid on
  the harness side.
- Dropping the cumulative session and folding per-call changesets with `ChangesetBuilder` would
  make `changes()` depend on SQLite's changegroup combination rules, which are not documented to
  discard an update that returns a row to its original values; `changes()` must stay the long
  session's exact answer.

APSW permits several sessions on one connection. The cost is a second preupdate-hook recording
per written row; §16 measures it and states the bound.

The `finally` runs on a `ToolError` too, and on any exception: a refused or failed call still
consumed its ordinal and still gets its (empty) recording. If `session.changeset()` itself
raises, the exception propagates as a `WorldBug`-class failure of the instance; nothing is
appended.

### 3.2 `bulk()`

`_bulk` wraps its transaction in `self._recording(None)`, so authoring writes land in the log with
`i: null` (FS §3.2), appended when the block exits. Startup hooks run before any session exists and
stay out.

### 3.3 Rendering (`render_log`)

```python
def render_log(changeset: bytes, conn, columns: dict[...], *, i: int | None) -> list[LogRecord]
```

For each `TableChange`: `key` from the key positions (as `render` does today); `before`/`after`:

| op | before | after |
|---|---|---|
| insert | `None` | every column (whole row) |
| update | changed non-key columns, old values | changed non-key columns, new values |
| delete | every column (whole row) | `None` |

"Changed" is a column whose `new` value is not `apsw.no_change`; key columns are excluded from
both sides of an update. Then the records of one changeset are **sorted** by
`(subworld, table, key)` with `_sort_key(record)` returning
`(record.subworld or "", record.table, tuple(_sqlite_rank(v) for v in record.key.values()))`,
where `_sqlite_rank` maps a value to `(0, None)` for NULL, `(1, number)` for int/float, `(2, str)`
for text and `(3, bytes)` for blobs, SQLite's own cross-type order, so a key of type ANY cannot
raise a `TypeError` in the sort. STRICT guarantees one type per column, so in practice the rank
never varies.

### 3.4 Values (`_jsonable`)

Extended from today's bytes→base64: a `float` that is infinite becomes `None`. One-line comment at
the conversion: `# JSON has no infinities; SQLite already stores NaN as NULL, so this is its rule
one step further (functional_spec.md §3.4).` Integers and finite floats pass through; text
passes through; `None` stays `None`.

## 4. The call ordinal and `call_count`

`_next_ordinal()` increments and returns `self._call_count - 1`, under the lock. It runs for every
dispatched world tool, before the chain, so a `ToolError` and a middleware short-circuit both
count. `UnknownTool` is raised today before the lock; it moves inside `_held()` so it too consumes
an ordinal (FS §7): the lookup happens under the lock, and the `raise` follows the increment. A
control tool never reaches `_next_ordinal`. `inst.tools()` is not a call. `call_count` is a
read-only property over `_call_count`.

## 5. Formats and formatters (`state.py`)

```python
type Formatter = Callable[["Instance"], dict[str, Any]]

SEAHAVEN_STATE_V1 = "seahaven.state/1"
SEAHAVEN_STATE_LAST_STEP_V1 = "seahaven.state+last_step/1"
BUILTIN_FORMATS: Mapping[str, Formatter]        # the two names above
BUILTIN_PREFIX = "seahaven."
_NAME = re.compile(r"^[A-Za-z0-9_.+-]+/[1-9][0-9]*$")

def check_format_name(name: str) -> None       # syntax; WorldBug naming the rule
def before_reset(world: World, format: str) -> dict[str, Any]   # FS §3.5, the no-instance document
```

### 5.1 The built-ins

`state_v1(instance)` builds FS §3.1 in field order:

```python
{"format": SEAHAVEN_STATE_V1,
 "seahaven_version": seahaven.__version__,
 "world": {"name": w.name, "version": w.version},
 "fixture": {"id": inst.fixture, "file_sha256": inst.fixture_sha256} if inst.fixture else None,
 "episode_id": inst.episode_id, "seed": inst.caller_seed, "now": inst.clock.iso(),
 "startup": inst.startup, "call_count": inst.call_count,
 "db": {"log": [r.to_dict() for r in inst.change_log()]}}
```

`state_last_step_v1(instance)` is `state_v1` with the log filtered to `r.i == call_count - 1`
and `format` replaced; `i: None` records never pass the filter (FS §4).

### 5.2 Registration and resolution (`world.py`)

- `World.__init__(..., *, state_format: str, ...)`: keyword-only, no default. `check_format_name`;
  if the name begins with `seahaven.` it must be in `BUILTIN_FORMATS`, else `WorldBug` listing
  the built-ins. A name outside the prefix is a custom format and is resolved at instance
  creation (§6), because it is registered after the `World(...)` line runs. Stored as
  `self.state_format`.
- `world.state_format(name)` is a decorator/call like `world.tool`: `check_format_name`; refuse
  the `seahaven.` prefix; refuse a duplicate; require a callable; store in
  `self._state_formats: dict[str, Formatter]`. Open for the life of the world.
- `world.resolve_state_format(name) -> Formatter`: built-ins first, then the world's; unknown is
  `WorldBug` naming the built-ins and the world's registered names.
- `__copy__` copies `_state_formats` like the other three registries.
- `RESET_ARGUMENTS = frozenset({"fixture", "now", "seed", "state_format"})`: the existing hook
  check refuses a parameter of that name with the existing message.

### 5.3 Running a formatter (`Instance.state`)

```python
def state(self, format: str | None = None) -> dict[str, Any]:
    with self._held():
        formatter = self._formatter if format is None else self.world.resolve_state_format(format)
        self._formatting = True
        try:
            document = formatter(self)
        finally:
            self._formatting = False
    if not document or next(iter(document)) != "format" or document["format"] != name:
        raise WorldBug(...)      # FS §6: `format` first, with the registered name
    return document
```

`_refuse_if_formatting()` at the top of `_call` and `_bulk` raises `WorldBug("a state formatter
reads an instance and never writes to it")`: a formatter that calls `inst.call` or `inst.bulk` is
caught by the RLock re-entry landing on the guard. Reads through `inspect()`, `change_log()`,
`changes()` and the attributes are what a formatter is for.

## 6. Instance creation (`InstanceManager.create`)

Signature gains `state_format: str | None = None, episode_id: str | None = None`; `seed` narrows
to `int | None`. Order of refusals, all before a directory exists:

1. `_check_startup_kwargs` (unchanged).
2. `startup = serialise(dict(kwargs))` from `call.py`: the document's `startup` object, JSON-able
   or a `WorldBug` now rather than at `state()` time. Hooks still receive the raw values.
3. `formatter = world.resolve_state_format(state_format or world.state_format)`.
4. The fixture checks (unchanged); `fixture.meta.file_sha256` is kept for the instance.

`Instance.__init__` gains `state_format: str`, `formatter: Formatter`, `episode_id: str`
(`episode_id or id`), `caller_seed: int | None`, `fixture_sha256: str | None`,
`startup: dict[str, Any]`, `tracked: tuple[str, ...]`. `world.instance(...)` gains
`state_format` and passes it through; `episode_id` is not on `world.instance` (in process the
instance id is the episode id, FS §3.1).

## 7. Seed narrowing (`ids.py`)

`instance_seed(source, caller_seed: int | None)`; `_seed_bytes` drops the `bytes` arm, so a
`bytes` value falls into the existing `case _` and raises the existing `WorldBug` with the message
updated to "an int or None". `World.instance`, `InstanceManager.create`, `InstanceInfo` docs and
the pytest marker follow. `ctx.instance.seed` (the derived bytes) is unchanged.

## 8. Control tools (`control.py`)

`dispatch` issues `warnings.warn(f"{call.name} is deprecated: read inst.state() instead",
DeprecationWarning, stacklevel=2)` before running the function. Docstrings of both tools open with
the same sentence. Nothing else changes.

## 9. OpenEnv (`openenv/env.py`)

- `reset(self, seed=None, episode_id=None, *, fixture=None, now=None, state_format=None,
  **startup_kwargs)`: computes `episode_id or str(uuid.uuid4())` first and passes both
  `state_format=` and `episode_id=` to `world.instance(...)`. `self._episode_id` is read from the
  instance afterwards.
- `state` property: `SeahavenState(step_count=self._steps, **instance.state())` with an instance;
  without one, `SeahavenState(step_count=0, **state.before_reset(self.world,
  self.world.state_format))`.
- `SeahavenState(State)`: one declared field, `format: str` (with a description, for the reason
  the current fields have them), and the base class's `extra="allow"` carries the rest. A typed
  model cannot serve custom formats, whose documents are arbitrary; the built-in documents are
  validated by tests against a JSON Schema kept in `tests/`, not by the model. `episode_id` is the
  base class's field and is filled from the document.
- `_parse_state` is unchanged. `SeahavenClient.state().model_dump()` is the document plus
  `step_count`.

## 10. Scaffold and sweep

- `world.py.tmpl`: `state_format="seahaven.state/1",` with a two-line comment: pinned at creation;
  changing it changes what every eval of this world saves, so bump `version` with it.
- The sweep adds `state_format="seahaven.state/1"` to every `World(...)` in `worlds/`,
  `extensions/`, `tests/`, `bench/`, `README.md`, `CONTRIBUTING.md` and `src/seahaven/docs/`. The
  docs test executes every example, so a missed one fails CI.

## 11. Lint

FS §12 asks for a rule refusing a nullable primary-key column. **Measured 2026-09-17 (SQLite
3.45.1): a STRICT table refuses `NULL` in any primary-key column with `NOT NULL constraint
failed`, single and composite keys alike, and only non-STRICT tables accept it.** SH101 already
requires STRICT, so the hazard cannot reach a linted world. Proposed: no new rule; SH101's
`lints.md` entry gains one sentence saying STRICT is also what keeps every row visible to the
change log (open item §17.1).

## 12. Documentation

Per FS §12. The coding agent for the docs phase writes `state.md` from FS §3, §4, §5, §6 and
§10, in the three-case order FS §12 gives; rewrites `serving.md`'s control-tools section into a
state section; replaces `testing.md`'s changeset example with `inst.state()["db"]["log"]`;
extends `concepts.md`'s changeset section with the log; updates the README example; removes every
`controller_` mention except `cli.md`'s deprecation line; adds `state.md` to `index.md`; adds
`state_format` to `authoring.md`'s `World(...)` section and reserved-keyword list; updates
`reference/api.md` for `Instance.state`, `change_log`, `call_count`, `World.state_format`,
`World.resolve_state_format`, `LogRecord`. `openenv.md` is edited in the same phase if it has
landed on `main` by then, else the phase records it in `BACKLOG.md` as the one deferred edit.

## 13. Error handling

| Condition | Outcome |
|---|---|
| `World(...)` without `state_format`, or with an invalid name, or a `seahaven.` name that is not built in | `WorldBug` at construction, naming the built-ins |
| `reset`/`instance` with an unknown format | `WorldBug` before any directory exists; over OpenEnv the reset's `EXECUTION_ERROR`, session stays open |
| A hook parameter named `state_format` | `WorldBug` at registration (existing check, extended set) |
| A startup keyword that is not JSON-able | `WorldBug` at creation |
| A formatter whose document lacks `format` first with the right name | `WorldBug` from `state()` |
| A formatter that calls `inst.call` or `inst.bulk` | `WorldBug` from the guard |
| `state()` on a destroyed instance | `WorldBug`, as every other method |
| `seed` that is `bytes` | `WorldBug` from `_seed_bytes` |
| A control tool call | `DeprecationWarning`, then the normal result |

Nothing new is logged at `INFO`; the per-call log line is unchanged.

## 14. Testing strategy

Unit tests live beside the module they pin; every FS §13 item maps to one below.

- `tests/test_changes.py` gains the `LogRecord`/`render_log` cases: the three ops, update with
  one and several changed columns and a column set to `NULL`, composite key order, integer key,
  blob, infinity → `null`, key columns absent from an update's sides, the sort including a
  mixed-type key on a column of type ANY.
- `tests/test_change_log.py` (new): net per call and not across calls (the FS §13 second bullet,
  case by case), ordering across calls, `i: null` for `bulk()`, no records for a rolled-back call,
  a read-only call, and startup rows; byte-identical `json.dumps` of two identical episodes; the
  formatting guard; `state()` after `destroy()`.
- `tests/test_fold.py` (new) with `tests/support/fold.py`: the FS §3.6 fold as test code;
  `fold(log) == [c.to_dict() for c in inst.changes()]` on every `test_change_log.py` episode plus a
  primary-key rewrite. The support module is the seed of a later helper and is deliberately not
  importable from `seahaven`.
- `tests/test_state.py` (new): `state_v1` field by field against a fixture instance and a blank
  one; `startup` given and empty; `state_last_step_v1` per step, concatenation equals the full
  log, empty before any call; a custom formatter registered, selected by `instance(state_format=)`,
  refused for a `seahaven.` name, a duplicate, a non-callable, a bad name, a wrong first key;
  `World(...)` without the argument; unknown names at `World`, `instance`; the world's pin naming
  a custom format registered later; `before_reset`; the built-in documents validate against
  `tests/support/state_v1.schema.json`.
- `tests/test_world.py`: `RESET_ARGUMENTS` refusal for `state_format`; `__copy__` carries formats.
- `tests/test_ids.py`: `bytes` seed refused; the pytest marker test in `tests/test_pytest_plugin.py`
  follows.
- `tests/test_env.py`: `state` before `reset` is `before_reset`; after `reset` the document with
  `step_count`; `reset(state_format=)` selects; a second `reset` starts a new log; the two existing
  state tests are rewritten to the document.
- `tests/test_server.py`: over the WebSocket, `env.state().model_dump()` carries every FS §3.1
  field with the values the in-process document has (this is FS §9's confirmation step);
  `reset(state_format="seahaven.state+last_step/1")` selects it; the stock `GenericEnvClient`
  sees the same keys.
- `tests/test_control.py`: each control tool warns (`pytest.warns(DeprecationWarning)`), in
  process, which is where the server's warning is raised too.
- `tests/test_new_cli.py` (existing scaffold tests): the generated `world.py` carries the pin and
  `seahaven check` passes on it.
- `bench/`: a probe for §16.
- Docs tests run every example, unchanged.

## 15. Determinism

Two identical episodes produce byte-identical `json.dumps(inst.state())`: the log is sorted
within a call, dict field order is fixed by `to_dict`, `seahaven_version` and the fixture hash are
constants for a build, and `episode_id` is the instance id, which is a UUID. The test therefore
compares documents with `episode_id` masked; over OpenEnv a caller who passes `episode_id` to
`reset` gets full byte identity.

## 16. Performance

- `state()` does no database work: the log is in memory; `render_log` runs at call time. Pinned by
  a test that installs an authorizer counter on the instance connection during `state()` and
  asserts zero statements.
- The second session's cost per written row is measured by a `bench/` probe (the existing
  harness) on ProjectTracker's write-heavy workload, with and without per-call recording (a
  private toggle the probe flips). Bound stated after measurement in the phase plan; the
  expectation is a few percent, since the session extension's per-row work is a C hash-table
  insert.
- `_columns` is filled once per table per instance; `tracked_tables` once per instance.

## 17. Open items

1. **Drop the lint rule.** §11 measured that STRICT already refuses NULL in a primary-key column
   and SH101 requires STRICT, so FS §12's new rule cannot fire on a linted world. Proposal: remove
   it from the functional spec and add one sentence to SH101's entry instead.
2. **When a custom pin is checked.** `World(state_format="acme.state/1")` names a format that is
   registered after the constructor runs, so it can only be resolved at instance creation (§5.2).
   FS §5 says unknown names error "at `World(...)`"; proposal: built-in names at construction,
   custom names at first instance creation, both before any directory exists.
3. **`format` before `reset` under a custom pin.** `before_reset` emits the fixed FS §3.5 shape
   with `format` set to the world's pin (§9). Under a custom pin that names a shape the document
   does not have. Alternatives: `format` is always `seahaven.state/1` before reset; or `null`.
   Proposal: the pin, documented as "the pre-reset document is fixed and format-independent".
4. **`SeahavenState` as an open model.** §9 types only `format` and lets `extra="allow"` carry
   the rest, because custom formats make a typed model impossible. FS §9 says "its fields are
   §3.1's". Proposal: the open model, with the built-in shape pinned by a JSON Schema in `tests/`.
5. **Startup keywords serialised at creation.** §6 refuses a non-JSON-able startup keyword at
   `reset`, which FS does not state. Proposal: adopt; fail early beats a `state()` that raises.
6. **The deprecation warning over the wire.** A `DeprecationWarning` raised in the server is not
   observable by a client; FS §13 says "in-process and over the wire". Proposal: test in process
   only, and say so in the spec.
7. **Two sessions per write.** §3.1's design doubles the session extension's per-row work. If the
   §16 measurement shows more than a few percent on the write-heavy workload, the fallback is to
   attach the cumulative session lazily, on the first `changes()` call, from a fold of the
   per-call changesets with `ChangesetBuilder`, accepting the changegroup-semantics risk in that
   path only. Proposal: measure first; decide in the phase.
