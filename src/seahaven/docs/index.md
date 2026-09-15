# Seahaven

Seahaven is a Python framework for building **synthetic worlds**. A synthetic world is a working
copy of a system an agent works against: a CRM behind a REST API, a payment processor with a SQL
database, an issue tracker, a warehouse system. The copy is fake, but it stores real state in a
SQLite database, so a write changes every later read.

Evals and reinforcement learning need thousands of runs. Each run has to start from a known state,
run in parallel with the others, and be inspectable afterwards. A real system cannot do that, and
neither can a staging copy. A synthetic world can: it forks a private database in milliseconds, runs
one agent against it, reports exactly what changed, and then throws the copy away.

## How a world is put together

A world is an ordinary Python package with three parts:

- a **schema**, the tables the world stores its data in, written as plain SQLite `CREATE TABLE`
  statements;
- **tools**, plain Python functions that the agent calls;
- **fixtures**, frozen databases that hold a starting state such as "a twelve-person agency with six
  months of history".

An eval makes an **instance** — a private copy of one fixture that lasts for one run — drives it
through tool calls, and grades the state the agent left behind.

## A small world, end to end

```python
import seahaven

world = seahaven.World(
    name="notes",
    version="1.0.0",
    schema="""
    CREATE TABLE notes (
        id TEXT PRIMARY KEY,
        body TEXT NOT NULL,
        created_at TEXT NOT NULL
    ) STRICT;
    """,
)


@world.tool
def add_note(ctx: seahaven.Ctx, body: str) -> dict[str, str]:
    """Write a note down and return it."""
    note = {"id": ctx.ids.uuid(), "body": body, "created_at": ctx.clock.iso()}
    ctx.db.execute(
        "INSERT INTO notes (id, body, created_at) VALUES (?, ?, ?)",
        note["id"],
        note["body"],
        note["created_at"],
    )
    return note


with world.instance(now="2026-06-01T09:00:00.000Z") as inst:
    note = inst.call("add_note", body="buy milk")
    assert note["created_at"] == "2026-06-01T09:00:00.000Z"
    assert [change.op for change in inst.changes()] == ["insert"]
```

Five things in that example are worth naming, because the rest of these pages use them constantly.
`World` is the declaration. `@world.tool` publishes a function to the agent; its signature becomes
the JSON schema the agent sees, and its docstring becomes the description. `inst` is a private
database copy. `ctx.clock` is frozen, so the timestamp is the same on every replay. `inst.changes()`
is the difference between the starting state and the state the run left behind, which is what an
eval grades.

A real world spreads the same parts over a package instead of one file, and adds fixtures, error
types and an error handler. `seahaven new <name>` writes that layout for you.

## Getting started

```sh
seahaven new <name>           # scaffold a world
seahaven check                # run every lint; do this before a commit
seahaven fixture list         # every fixture: id, parent, now, description
seahaven serve                # run this world's server
seahaven docs                 # print the directory holding these pages
```

Every command except `new` and `docs` finds the world by convention: the project's package, taken
from `[project] name` in the nearest `pyproject.toml`, exporting an attribute called `world`. Pass
`--world module:attr` to name it yourself. [reference/cli.md](reference/cli.md) has every command
and option.

## Where to go next

Read these in order the first time. Each page assumes the ones above it.

| Page | What it covers |
|---|---|
| [concepts.md](concepts.md) | The nine words the rest of the docs use: world, tool, schema, fixture, instance, context, clock, reproducibility, changeset |
| [authoring.md](authoring.md) | Writing a world: tools, arguments, transactions, errors, middleware, startup hooks |
| [db_schema_and_fixtures.md](db_schema_and_fixtures.md) | The database, the schema rules, and how fixtures are built and kept |
| [testing.md](testing.md) | The pytest plugin, and what is worth testing in a world |
| [serving_and_openenv.md](serving_and_openenv.md) | `seahaven serve`, driving a world over the network, and publishing it |
| [composition.md](composition.md) | Building a world out of other worlds |
| [extensions.md](extensions.md) | Packaging something several worlds need, with a worked example |
| [projecttracker.md](projecttracker.md) | A walkthrough of the reference world |

Reference pages, for looking things up rather than reading through:

| Page | What it covers |
|---|---|
| [reference/api.md](reference/api.md) | The public API |
| [reference/cli.md](reference/cli.md) | Every subcommand and option |
| [reference/lints.md](reference/lints.md) | Every `SHnnn` code: the rule, why it exists, and the fix |

## A note for agents

If you are an agent asked to build or extend a world, read [concepts.md](concepts.md) and
[authoring.md](authoring.md) before writing anything, and keep
[reference/lints.md](reference/lints.md) open beside you. Every rule in it is a mistake that is
otherwise easy to make and hard to notice.

These pages ship inside the installed `seahaven` package, so they always describe the version you
have. `seahaven docs` prints the directory they are in.
