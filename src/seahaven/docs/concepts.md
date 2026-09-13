# Concepts

Nine words carry the whole framework. This page defines them; every other page uses them without
explaining them again.

## World

A **world** is a Python package that exposes one `seahaven.World` object, by convention as the
attribute `world` on the package. The object holds the world's name, its version, its schema, and
everything registered against it: tools, middleware and instance startup hooks.

```py
# src/notes/world.py
import seahaven

world = seahaven.World(
    name="notes",
    version="1.0.0",
    schema=seahaven.sql_files(__package__, "schema"),
)
```

A world's agent-facing surface is exactly its registered tools — there is no other door. The
`World` is built at import time, in its own module, so every tool module can import it without an
import cycle. Registration validates immediately: a mistake in a tool's signature is an exception at
import, naming the tool and the parameter, not a surprise on the first call.

Everything about a world is declared in code. There is no `world.yaml` and no `[tool.seahaven]`
table.

## Schema

The schema is hand-written SQLite DDL in `schema/`, applied in filename order to a blank database.
`seahaven.sql_files(__package__, "schema")` reads it. Three rules are enforced by `seahaven check`
and matter more than they look:

- every table is `STRICT`, so SQLite stores what the column says and not whatever it was handed;
- every table has an explicit primary key, because a table without one cannot be tracked in a
  changeset;
- no wall-clock expression anywhere — no `CURRENT_TIMESTAMP` default, no `datetime('now')` in a
  trigger. Timestamps are written by world code, from the instance's clock, in one format.

**Timestamps are TEXT**, ISO 8601 UTC with milliseconds and a trailing `Z`:
`2026-06-01T09:00:00.000Z`. One format across every door of the world. Presentation to the agent —
"3 days ago", a local time — is the world's job at the tool boundary.

## Fixture

A **fixture** is an immutable directory holding `state.sqlite` and `fixture.yaml`. It is the data an
eval starts from: a workspace with twelve people and six hundred issues in it, or an empty one.

A fixture is never opened. It is copied. The only way to mint one is to freeze an instance, and the
sidecar records where it came from: the world and version, the schema hash it conforms to, the
frozen clock, its parent fixture, the SHA-256 of the state file, and a description written for
whoever is choosing between fixtures.

A fixture is addressed by its id everywhere: `world.instance("agency")`, `reset(fixture="agency")`,
`@pytest.mark.seahaven(fixture="agency")`, `seahaven fixture list`.

See [fixtures.md](fixtures.md).

## Instance

An **instance** is a private copy of a fixture's SQLite file plus one connection, one clock, one id
generator and one changeset session. It lives for minutes, in a working directory under the system
temp directory, and it is what an episode actually drives.

```python
import projecttracker

with projecttracker.world.instance("small_startup", seed=7) as inst:
    issue = inst.call("get_issue", key="ENG-12")
    assert issue["key"] == "ENG-12"
    assert inst.fixture == "small_startup"
    assert inst.clock.iso() == "2026-06-01T09:00:00.000Z"
```

Leaving the `with` block destroys it: connections closed, directory removed. `inst.destroy()` does
the same explicitly, and waits for a call in flight to finish first.

Instances are cheap and there is no cap on how many a process may hold. Calls into one instance
serialise under its lock; calls into different instances do not. Over a server, one session is one
instance.

One thing does bound calls across instances, and it is on by default in **every** process, not only
under `seahaven serve`: a process-wide gate of `min(cpus, 16)` slots that a call takes before it
runs. Nothing is ever rejected — calls queue — but a harness driving many instances on threads is
sharing those slots, and the gate is unfair whenever it binds. `seahaven.instances.set_concurrency(n)`
resizes it and `0` removes it. See "The concurrency gate" in [serving.md](serving.md), which is where
the measurements and the open defect are.

## Tool

A **tool** is a synchronous Python function whose first parameter is the instance context. The rest
of its parameters are the tool's arguments, and the framework builds a pydantic model and a JSON
schema from them. The whole docstring is the description an agent reads.

```py
@world.tool
def get_issue(ctx: seahaven.Ctx, key: str) -> dict[str, object]:
    """Fetch one issue by its product key, such as ENG-12."""
    ...
```

Arguments are validated **strictly** before the tool runs: `"5"` is not an `int` and `1` is not a
`bool`. Unknown arguments are refused. Every violation is reported at once, so an agent can fix them
in one turn rather than one round trip per mistake.

By default one call is one transaction: begun before the tool runs, committed when it returns,
rolled back if it raises. See [authoring.md](authoring.md).

## Context (`ctx`)

Every tool, middleware and startup hook receives the same `ctx` for the duration of one call. It is
the whole of what world code is given, and the framework exposes nothing else — no working
directory, no other instance, no process.

| Member | What it is |
|---|---|
| `ctx.db` | The connection: `one`, `rows`, `execute`, `executemany`, `transaction()`, and `conn` for the raw APSW connection |
| `ctx.clock` | The instance's frozen instant: `now()` for an aware UTC `datetime`, `iso()` for the canonical text |
| `ctx.ids` | The seeded stream: `uuid()` and `random`, a `random.Random` seeded per instance |
| `ctx.state` | A plain `dict` that lives as long as the instance; where a startup hook leaves what it worked out |
| `ctx.call` | The current call: `name`, `arguments`, `tool`, and `with_arguments(**changes)` |
| `ctx.instance` | `id`, `fixture` (or `None` for a blank instance) and `seed`, read-only |

## Clock

An instance's clock is **static**. It is the fixture's frozen `now` for the instance's whole life,
and every connection overrides SQLite's own date and time functions to return it — `CURRENT_TIMESTAMP`,
`datetime('now')`, `strftime`, `julianday` and the rest — so SQL sees the same instant world code
does. There is no wall-clock read anywhere in the data path.

A blank instance's clock defaults to the wall time at creation, and `now=` overrides it. Freezing
bakes that value into the fixture, so it never moves again. Those two — a blank instance's default
and the `created_at` a freeze stamps on the sidecar — are the only wall-clock reads in a world.

The consequence to design around: **every row one episode writes carries the same timestamp**. A
timestamp does not order them. See "Ordering within an episode" in [authoring.md](authoring.md).

## Reproducibility

Each instance derives a seed from the fixture id (or the world name, for a blank instance) and the
caller's optional `seed=`. `ctx.ids.uuid()` and `ctx.ids.random` are driven by it, so the same
fixture, the same seed and the same calls give the same ids.

```python
import projecttracker

world = projecttracker.world


def first_issue_id() -> str:
    with world.instance("small_startup", seed=11) as inst:
        project = inst.inspect().one("SELECT id FROM projects ORDER BY id LIMIT 1")
        return str(inst.call("create_issue", project_id=str(project["id"]), title="A")["id"])


assert first_issue_id() == first_issue_id()
```

**Reproducibility is offered, not enforced.** The framework gives you a frozen clock and a seeded
stream and guarantees those are deterministic. A world that calls `datetime.now()` or `uuid.uuid4()`
gets exactly what it asked for, and `seahaven check` warns about both (`SH201`, `SH203`) rather than
refusing them, because a wall-clock read is occasionally deliberate. Lists a world returns need a
deterministic tiebreak — order by a column *and* by the id — or two identical runs disagree about
the order of rows sharing a value. Anything outside the world, such as a live external tool an eval
also gives the agent, is outside the promise by rule.

## Changeset

`inst.changes()` returns what the instance has changed since it was created: a list of records
carrying the table, the operation, the row's key, and the row before and after.

```python
import projecttracker

with projecttracker.world.instance("small_startup") as inst:
    issue = inst.call("get_issue", key="ENG-3")
    inst.call("transition_issue", issue_id=issue["id"], status="done")
    changed = {(change.table, change.op) for change in inst.changes()}
    assert ("issues", "update") in changed
```

**A changeset is a net difference, not a log of calls.** It is the difference between the fixture
and the current state:

- a write that leaves a value unchanged records nothing;
- an insert followed by an update of the same row is one insert;
- a call that rolled back leaves no trace;
- rows written by startup hooks are not in it — the session is attached after the hooks have run,
  because those rows are the world's setup and not the agent's work;
- tables the world names in `World(untracked_tables=...)` are not in it, and neither are FTS5's
  shadow tables.

This is what an eval grades on: the state the episode left behind, rather than the transcript of how
it got there.

## What is not here

Seahaven does not bound a tool call, project or filter tools per instance, mock a tool's response,
progress a clock, or generate data for you. Containment exists for agent-written SQL and nowhere
else. Those are deliberate absences, not gaps waiting to be filled.
