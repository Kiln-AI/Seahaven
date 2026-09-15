# Seahaven

Seahaven is a framework for building **synthetic worlds**: faithful, stateful mocks of the tool
surface a real company's agent works against, on SQLite, that agents work against in evals.

A world is an ordinary Python package. Its schema is hand-written SQLite DDL, its tools are plain
Python functions, and its data is a set of frozen SQLite files called fixtures. An eval makes an
*instance* — a private copy of one fixture — drives it through tool calls, and grades it on the
state it is left in.

These pages ship inside the installed `seahaven` package, so they always match the version you
have. `seahaven docs` prints the directory they are in.

## A world, whole

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

That is the whole framework in one screen: a declaration, a function whose signature is its
published contract, an instance that is a private copy, a frozen clock, and a changeset an eval can
grade. A real world spreads the same three things over a package — `seahaven new <name>` lays one
out — and adds fixtures, errors and an error handler.

## Reading order

| Page | What it covers |
|---|---|
| [concepts.md](concepts.md) | World, fixture, instance, tool, clock, reproducibility, changesets |
| [authoring.md](authoring.md) | Writing tools, errors and the error handler, middleware, startup hooks, schema rules |
| [composition.md](composition.md) | Adding other worlds: `add_world`, `ctx.worlds`, shared stores, composite fixtures |
| [fixtures.md](fixtures.md) | Freezing, forking, generators, descriptions for eval authors |
| [testing.md](testing.md) | The pytest plugin, what to test in a world |
| [serving.md](serving.md) | `seahaven serve`, the OpenEnv client, control tools, publishing to a hub |
| [openenv.md](openenv.md) | OpenEnv compatibility: driving a world from any client, the wire, why there are no rewards |
| [extensions.md](extensions.md) | The extension contract, the XML-RPC example |
| [projecttracker.md](projecttracker.md) | A walkthrough of the reference world |

Reference:

| Page | What it covers |
|---|---|
| [reference/api.md](reference/api.md) | The public API |
| [reference/lints.md](reference/lints.md) | Every `SHnnn` code: rule, why, fix |
| [reference/cli.md](reference/cli.md) | Every subcommand and option |

If you are an agent asked to build or extend a world, read `concepts.md` and `authoring.md` before
writing anything, and keep `reference/lints.md` beside you: every rule in it is a mistake that is
otherwise made silently.

## The commands

```sh
seahaven new <name>           # scaffold a world
seahaven check                # run every lint; do this before a commit
seahaven fixture list         # every fixture: id, parent, now, description
seahaven serve                # run this world's OpenEnv server
seahaven docs                 # print this directory
```

Every command except `new` and `docs` finds the world by convention: the project's package, from
`[project] name` in the nearest `pyproject.toml`, exporting an attribute called `world`.
`--world module:attr` overrides it. [reference/cli.md](reference/cli.md) has the details.

## The rules that are not negotiable

- **Time comes from `ctx.clock`** and ids and randomness from `ctx.ids`. An instance's clock does
  not move, so a replay of the same fixture and seed gives the same run — for a world that takes
  both from `ctx`. SQL's own `CURRENT_TIMESTAMP`, `random()` and `randomblob()` are overridden on
  every connection to read the same instant and the same seed. The framework offers
  reproducibility; it does not enforce it.
- **Fixtures are immutable.** Fork, change the fork, freeze that. There is no in-place edit path.
- **SQL goes through `ctx.db`**, on the one connection the instance owns. `ctx.db.conn` is the raw
  APSW connection for what the wrapper does not cover; never close it or change its pragmas.
- **Every table is `STRICT` and has an explicit primary key**, and no DDL anywhere reads the wall
  clock. `seahaven check` fails on all three.
- **Nothing engine-shaped reaches the agent** unless the world chose it. That is the error
  handler's job, and every world has one.
