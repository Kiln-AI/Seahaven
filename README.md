# Seahaven

[Docs](src/seahaven/docs/index.md) · [PyPI](https://pypi.org/project/seahaven/) ·
[OpenEnv](https://huggingface.co/docs/openenv/index) · [Kiln AI](https://kiln.tech)

**Synthetic worlds for AI agents.** Fake, stateful replicas of the systems your agent works
against, for RL and evals.

> **Seahaven** *(noun)*
>
> 1. A Python framework for building synthetic worlds for AI agents.
> 2. The town in *The Truman Show*. An entire world built so that one inhabitant believes it is
>    real.

RL and evals need thousands of rollouts, in parallel, each from a known state, each inspectable
afterwards. No real system or staging copy can do that.

A Seahaven world can. Clone the tools your agent uses in production, fork hundreds of private
copies in milliseconds, run an agent in each, see exactly what it changed, then throw them away.

## Features

- **Stateful.** Writes change every later read. Each instance is its own SQLite database.
- **Fixtures.** Freeze known starting states like `small_startup`, `agency` or `big_co`, and reuse
  them across evals. Immutable and hash-verified.
- **Any interface.** Expose tools that match REST APIs, sandboxed SQL, search, or any custom
  protocol.
- **Serving.** Hundreds of instances per process, one private instance per session, thousands of
  tool calls per second.
- **Reproducible.** Same fixture, same frozen clock, same seeded ids: the same run, every time. The
  clock is frozen in Python and in SQL.
- **Changesets.** The net diff between the fixture and what the agent left behind. Grade on state,
  not on transcripts.
- **Composable worlds.** Add sub-worlds to your world, like a full Stripe or Shopify API. Compose,
  reuse and share worlds.
- **[OpenEnv](https://huggingface.co/docs/openenv/index).** `seahaven serve` is an OpenEnv
  environment. Drive it with any OpenEnv client, or publish it to Hugging Face.

## Quickstart

Python 3.14 or newer.

```sh
uv add seahaven          # or: pip install seahaven
```

A world is a schema, a set of tools, and the fixtures an instance starts from. The smallest one is
a single file:

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
    assert note["created_at"] == "2026-06-01T09:00:00.000Z"  # the instance's clock, frozen
    assert [change.op for change in inst.changes()] == ["insert"]  # what an eval grades
```

The tool's signature is the JSON schema an agent sees and its docstring is the description. The
instance is a private copy with a frozen clock and seeded ids, and its changeset is what an eval
grades.

A real world is a package. `seahaven new` scaffolds one, with an error handler, a fixture
generator, tests on the bundled pytest plugin, and every framework rule as a lint:

```sh
seahaven new crm
cd crm
uv sync
uv run pytest
uv run seahaven check
```

For a full-size example, see [ProjectTracker](worlds/projecttracker/), the reference world: a
fictional issue tracker with nine tables, 25 tools, full-text search and three fixtures.

Building a world with an agent? Point it at `seahaven docs`. The docs ship inside the package and
always match the installed version.

## Serving

Seahaven's remote lifecycle and transport are [OpenEnv](https://huggingface.co/docs/openenv/index).
One command serves a world; every session gets its own instance, hundreds per process:

```sh
seahaven serve
```

`SeahavenClient` is one client for every Seahaven world, and the stock OpenEnv client works too:

```py
from seahaven.openenv import SeahavenClient

with SeahavenClient(base_url="http://127.0.0.1:8000") as env:
    env.reset(fixture="agency", seed=7)
    tools = env.list_tools()  # [{"name", "description", "input_schema"}, ...]
    observation = env.call("get_issue", key="ENG-12")
    print(observation.result["title"])
```

An eval reads the instance over the same connection: `seahaven serve --include-control-tools`
exposes the changeset and read-only inspection SQL as two control tools, never listed to the agent.
`seahaven new --hub` adds the files a hub expects, so a world publishes with `openenv push`.

## License

MIT.

## Created by Kiln AI

Seahaven was created by [Kiln AI](https://github.com/Kiln-AI/Kiln). Kiln is an open-source
platform for building, evaluating and optimizing AI systems, and uses Seahaven worlds as the
environments its [evals](https://kiln.tech/features/evals) and
[optimizers](https://kiln.tech/features/auto-optimize) run against.
