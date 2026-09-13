# API reference

The public API is the names exported from `seahaven`, the `seahaven.helpers` and `seahaven.sandbox`
modules, `seahaven.openenv` (in the `serve` extra), and the pytest plugin's two fixtures. A name that
is not below is internal and may change without notice.

Six names on this page live outside `seahaven/__init__.py` and are documented anyway, in two groups:

- `seahaven.world.Handler`, `Middleware` and `StartupHook` — the type aliases the scaffolded error
  handler imports, and part of that module's stated interface in the repository's
  `components/world_and_dispatch.md` §1.
- `seahaven.instances.default_concurrency`, `concurrency` and `set_concurrency` — the concurrency
  gate, which no component document lists. **This page declares them public on its own authority**,
  because the gate is on in every process and `serve --concurrency` is otherwise the only documented
  way to touch it, which leaves an in-process harness with a real knob and no name for it.

Seahaven's own `architecture.md` says every name outside `seahaven/__init__.py` is internal, which
all six contradict; that disagreement is `BACKLOG.md` B10 in the repository, and the second group is
a wider claim than B10's own proposed replacement rule would sanction. This page describes what the
code does and says where it is going further than the specification.

```py
import seahaven

seahaven.World, seahaven.Instance, seahaven.Ctx, seahaven.Tool, seahaven.Call
seahaven.Db, seahaven.Clock, seahaven.Ids, seahaven.Change, seahaven.Fixture
seahaven.SeahavenError, seahaven.WorldBug
seahaven.ToolError, seahaven.ArgumentError, seahaven.DbError, seahaven.UnknownTool
seahaven.sql_files, seahaven.helpers, seahaven.sandbox
seahaven.__version__
```

## `World`

```py
class World:
    def __init__(
        self,
        name: str,
        version: str,
        schema: str,
        *,
        fixtures_dir: Path | str | None = None,
        work_dir: Path | str | None = None,
        untracked_tables: Sequence[str] = (),
    ) -> None: ...
```

One per world package, built at import in `world.py`. `schema` is the DDL as one string, usually
from `sql_files`. `name` and `version` are informational and appear in the OpenEnv metadata and in
every fixture's sidecar. `fixtures_dir` defaults to `fixtures/` at the project root, found by
walking up from the constructing module to the directory holding `pyproject.toml`, and to
`fixtures/` beside the package where there is none. `work_dir` is where instance copies live; the
default is a per-process directory under the system temp directory, which is swept of previous
processes' leftovers, and a directory you name is used exactly as given and never swept.
`untracked_tables` names tables the changeset session does not attach.

A `World` whose DDL does not execute cannot be constructed: the schema is built in memory to compute
the schema hash, and SQLite's own message is reported.

| Member | What it is |
|---|---|
| `world.tool(obj=None, *, name=None, description=None, transaction=None)` | register a tool, as a decorator or a call. Passing a built `Tool` and any of the three keywords is refused — a factory decided them |
| `world.middleware(obj=None)` | register a middleware, as a decorator or a call. Order is registration order, outermost first |
| `world.instance_startup(obj=None)` | register a startup hook, as a decorator or a call |
| `world.instance(fixture=None, *, seed=None, now=None, **startup_kwargs)` | make an instance; a context manager |
| `world.fixtures()` | every fixture in the fixtures directory, by id. A world with no fixtures directory has none, which is not an error |
| `world.tools` | the registry, in registration order. Read-only |
| `world.middlewares` | the middleware, outermost first |
| `world.startup_hooks` | the hooks, in registration order |
| `world.accepted_startup_kwargs` | every keyword some hook names |
| `world.name`, `world.version`, `world.schema`, `world.schema_hash`, `world.fixtures_dir` | as given, plus the hash of the normalised DDL |

Registration validates immediately and raises `WorldBug`; the full list of what is refused is in
[../authoring.md](../authoring.md).

### `sql_files`

```py
def sql_files(package: str | None, directory: str) -> str: ...
```

Every `*.sql` in a package directory, in sorted filename order, joined with newlines — which is what
the `001_`, `002_` prefix convention is for. Read through `importlib.resources`, so a world works
the same from a checkout, an installed wheel or a zip. `seahaven.sql_files(__package__, "schema")`
is the spelling; `__name__` is a module inside the package and is refused, as is a directory that
leaves the package.

## `Instance`

Made by `world.instance(...)`, never by hand. A context manager; leaving the block destroys it.

| Member | What it is |
|---|---|
| `inst.call(name, /, **arguments)` | run one tool: validation, the middleware chain, the tool, on the calling thread, under the instance's lock. Raises the world's `ToolError` subclasses |
| `inst.tools()` | the tool list with JSON schemas, as `{"name", "description", "input_schema"}`. Control tools are never in it |
| `inst.inspect()` | a read-only `Db` on a second connection: every table, the instance clock, opened once and kept. Never a tool |
| `inst.changes()` | the cumulative changeset since creation, as `list[Change]` |
| `inst.freeze(id, description)` | mint a fixture from the current state; returns the `Fixture`. Refuses inside `bulk()` |
| `inst.bulk()` | a context manager yielding the instance's own `Ctx`, in one transaction, for loading rows fast |
| `inst.destroy()` | close everything and remove the working directory. Idempotent, and waits for a call in flight |
| `inst.id`, `inst.fixture`, `inst.seed` | the instance id, the fixture id (or `None`), the derived seed bytes |
| `inst.clock`, `inst.world`, `inst.state_path` | the clock, the world, and the instance's own database file |

The tool name is positional-only, so a world may have a tool argument called `name`.

## `Ctx`

What every tool, middleware and startup hook receives. Nothing else is exposed: no working
directory, no other instance, no process.

| Member | What it is |
|---|---|
| `ctx.db` | the `Db` for this instance |
| `ctx.clock` | the `Clock` |
| `ctx.ids` | the `Ids` |
| `ctx.state` | a `dict` that lives as long as the instance |
| `ctx.call` | the current `Call`, or `None` outside one (a startup hook, `bulk()`) |
| `ctx.instance` | `id`, `fixture` and `seed`, read-only |

## `Db`

```py
class Db:
    def one(self, sql, *params): ...  # the first row as a dict, or None
    def rows(self, sql, *params): ...  # every row, each a dict keyed by column name
    def execute(self, sql, *params): ...  # for effect; returns Exec(rowcount, last_rowid)
    def executemany(self, sql, rows): ...  # one statement per row of bindings; rows changed
    def transaction(self): ...  # BEGIN at the top level, SAVEPOINT when nested

    conn: apsw.Connection  # the raw connection
    in_transaction: bool  # whether one is open
```

Parameters are positional `?` bindings. Every SQLite failure arrives as `seahaven.DbError`, so world
code catches one type.

`Exec.last_rowid` is `sqlite3_last_insert_rowid` as SQLite reports it: it belongs to the connection
rather than the statement, so read it straight after an `INSERT`.

`db.conn` is there for what the wrapper does not cover — blob I/O, an exec trace. The invariants:
**do not close it, change its pragmas or its authorizer, or open a second connection to the instance
file.** The clock functions, the changeset session and the per-call transaction all live on that one
connection.

## `Clock`

```py
class Clock:
    def now(self): ...  # an aware UTC datetime
    def iso(self): ...  # the canonical text: 2026-06-01T09:00:00.000Z
```

Static for the life of the instance. Every connection overrides SQLite's `current_timestamp`,
`current_date`, `current_time` and the `'now'` argument of `datetime`, `date`, `time`, `strftime`,
`julianday`, `unixepoch` and `timediff` to return it, so SQL sees the same instant world code does.
The overrides are registered as innocuous, so schema objects may reference them. What they return
compares and sorts correctly against the canonical text a world stores.

## `Ids`

```py
class Ids:
    def uuid(self): ...  # a UUIDv4-shaped identifier from the seeded stream

    random: random.Random  # seeded per instance
```

Product-shaped keys — `ENG-13`, a sequential invoice number — are the world's own business, built on
`ids.random` or on its tables. This is the stream they draw from.

## `Call`

```py
call.name  # the name the caller asked for
call.arguments  # raw until validation runs inside the chain, validated after
call.tool  # the Tool that was found
call.with_arguments(**changes)  # a copy with changes merged over the arguments
```

Frozen. A middleware that wants typed arguments before validation calls
`call.tool.validate(call.arguments)` itself; the model is built once at registration, so that is
cheap.

## `Tool`

```py
class Tool:
    @classmethod
    def from_function(cls, fn, *, name=None, description=None, transaction=True): ...
    def validate(self, arguments): ...  # the arguments as the parameters, or ArgumentError
    def listing(self): ...  # {"name", "description", "input_schema"}

    name: str
    description: str
    fn: Callable[..., Any]
    params: type[pydantic.BaseModel]
    schema: dict[str, Any]
    transaction: bool
```

`from_function` is what a tool factory — a helper, an extension — builds its tool with. `control` is
the framework's own flag for the control tools and cannot be set through it.

## `Change`

```py
change.table  # the table
change.op  # "insert" | "update" | "delete"
change.key  # the row's primary key columns, as a dict
change.before, change.after  # row dicts, or None where not applicable
change.to_dict()  # the wire shape an eval reads
```

`before` and `after` hold only the columns the change carries. A changeset marks the rest
`apsw.no_change`, which is not the same as `NULL`, so an update's `before` holds the key columns and
the old values of what changed — flattening the two would turn "changed the assignee" into "rewrote
the row".

## `Fixture`

```py
fixture.id, fixture.now, fixture.parent_id, fixture.description, fixture.state_path
```

What `world.fixtures()` and `inst.freeze(...)` return.

## Errors

```
SeahavenError
├── WorldBug                  the author's mistake; never shown to an agent
└── ToolError(code, message, details=None)
    ├── ArgumentError         .violations: every problem with the arguments
    ├── DbError               .sqlite_message, .sqlite_code, .refusals
    └── UnknownTool           .details["name"]
```

`ToolError.to_dict()` is `{"code", "message", "details"}` — the same shape in process and over the
wire. The framework's own three codes are `invalid_arguments`, `db_error` and `unknown_tool`; a
world's codes are its own.

`DbError` carries SQLite's text but never puts it in the agent-facing message by itself: a world's
SQL door is where engine text is the right answer, and `run_sql`'s own tool puts SQLite's message on
the error it raises before the error handler ever sees it. Elsewhere, `error.sqlite_message` is
there for a handler that wants to log it.

## `seahaven.helpers`

```py
def run_sql(
    *,
    name: str = "run_sql",
    tables: Sequence[str],
    read_only: bool = True,
    max_rows: int | None = None,
    max_bytes: int | None = None,
    functions: Sequence[str] = (),
    description: str | None = None,
) -> Tool: ...
```

A tool taking one `query` string in the SQLite dialect and returning
`{"columns": [...], "rows": [[...]], "row_count": n, "truncated": bool}`. Exactly one statement per
call.

Containment is the framework's: a SQLite authorizer that allows the listed tables plus
`sqlite_master`, `sqlite_schema`, `json_each` and `json_tree`, and **denies** everything else —
never ignores. `ATTACH`, `PRAGMA`, `load_extension` and schema changes are refused whatever
`read_only` says, and with it, every write is. A statement-level check backs the authorizer, and a
fixed, non-configurable cap on the size of a single SQL value stops a query materialising an
enormous one. `read_only=False` allows writes to the listed tables, which commit with the call like
any other tool's.

`max_rows` and `max_bytes` are the world's truncation policy, for a product that truncates; unset
means the whole result. `functions` allows SQLite functions beyond the default list.

Nothing is inferred from a name: an FTS5 virtual table and its shadow tables are denied like any
other table the world did not list, and allowed when it does list them. Full-text `MATCH` therefore
does not work through a door that lists only the world's ordinary tables — which is the default, and
why a world's own search tool is the usual path — but a world that wants `MATCH` here lists the
virtual table **and its shadow tables** and adds the search functions. The recipe, with a worked
example, is in [../authoring.md](../authoring.md).

```py
def describe_schema(
    *, name: str = "describe_schema", tables: Sequence[str], description: str | None = None
) -> Tool: ...
```

No arguments, writes nothing, and returns, from the live schema:
`{"tables": [{"name", "columns": [{"name", "type", "nullable", "primary_key"}], "foreign_keys":
[{"columns", "references_table", "references_columns"}]}]}`. The companion every real SQL tool ships
with.

## `seahaven.sandbox`

Public so that an extension serving another SQL dialect runs its translated statement through the
same containment the framework's own helper uses.

```py
class Authorizer:
    def __init__(self, tables, *, read_only=True, functions=ALLOWED_FUNCTIONS) -> None: ...


def run_statement(
    db, sql, params=(), *, authorizer, max_rows=None, max_bytes=None
) -> SqlResult: ...


class SqlResult:
    def __init__(self, columns, rows, truncated) -> None: ...

    row_count: int


def refusal_kind(refusal) -> Refusal: ...  # "read" | "write" | "function" | "action" | ...


REFUSALS: frozenset[str]
ALLOWED_FUNCTIONS: frozenset[str]
MAX_VALUE_BYTES: int
```

A refusal is classified by what the authorizer recorded, not by the message text: SQLite reports its
own refusals inconsistently — a denied table read raises `apsw.AuthError` and a denied function
raises `apsw.SQLError` — and the two are siblings in APSW's flat hierarchy.

## `seahaven.openenv` (the `serve` extra)

```py
def app(
    world, *, include_control_tools=False, max_concurrent_envs=500, session_timeout=3600.0
) -> FastAPI: ...


class SeahavenClient:  # .reset(...), .call(tool, /, **arguments), .list_tools(), .state()
    ...


# Also exported: SeahavenEnv, SeahavenObservation, SeahavenState, and OpenEnv's own
# CallToolAction, ListToolsAction and ListToolsObservation.
```

See [../serving.md](../serving.md).

## The pytest plugin

Activated by installing `seahaven`. Two fixtures — `world` (session-scoped) and `instance` (one per
test) — one marker, `@pytest.mark.seahaven(fixture, seed=None, now=None, **startup_kwargs)`, and one
option, `--seahaven-world module:attr`. See [../testing.md](../testing.md).

## The concurrency gate

```py
def default_concurrency() -> int: ...  # min(cpus, 16), following CPU affinity
def concurrency() -> int: ...  # the size in force, or 0 for no gate
def set_concurrency(size: int) -> None: ...  # resize it; 0 removes it
```

In `seahaven.instances`, not on the package root, and documented on this page's own authority (see
the top): `serve --concurrency` is `set_concurrency`, and an in-process harness that drives many
instances on threads has the same gate and the same knob. It is process-wide and on by default. Calls already running are
unaffected by a resize; nothing is ever rejected. Read the gate's paragraph in
[../serving.md](../serving.md) before changing it — it is unfair whenever it binds, and that is a
known defect rather than a tuning question.

## Middleware and hook types

```py
from seahaven.world import Handler, Middleware, StartupHook
```

`Handler` is `(ctx, call) -> Any` — what the rest of the chain looks like from inside a middleware.
`Middleware` is `(ctx, call, next_) -> Any`. `StartupHook` is `(ctx, **kwargs) -> None`. They are
type aliases for annotating your own code; nothing subclasses them, and the shape is checked
structurally at registration.
