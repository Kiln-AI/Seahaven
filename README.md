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

Install with `uv add seahaven` or `pip install seahaven`. Python 3.14+.

Scaffold a world:

```sh
seahaven new crm
```

Build your world: a schema and a set of tools. Here is a CRM with one table, a search index and two
tools:

```python
import seahaven

world = seahaven.World(
    name="crm",
    version="1.0.0",
    schema="""
    CREATE TABLE contacts (id TEXT PRIMARY KEY, email TEXT NOT NULL, notes TEXT NOT NULL, stage TEXT NOT NULL, updated_at TEXT NOT NULL) STRICT;
    CREATE VIRTUAL TABLE contacts_fts USING fts5(notes, content='contacts');
    """,
)

@world.tool
def create_contact(ctx: seahaven.Ctx, email: str, notes: str = "") -> dict[str, str]:
    """Add a contact to the pipeline as a lead."""
    contact = {"id": ctx.ids.uuid(), "email": email, "notes": notes, "stage": "lead", "updated_at": ctx.clock.iso()}
    row = ctx.db.execute("INSERT INTO contacts VALUES (?, ?, ?, ?, ?)", *contact.values())
    ctx.db.execute("INSERT INTO contacts_fts (rowid, notes) VALUES (?, ?)", row.last_rowid, notes)
    return contact

@world.tool
def search_stale_leads(ctx: seahaven.Ctx, query: str) -> list[dict[str, str]]:
    """Full-text search over leads nobody has touched in 30 days."""
    return ctx.db.rows(
        "SELECT contacts.* FROM contacts_fts JOIN contacts ON contacts.rowid = contacts_fts.rowid "
        "WHERE contacts_fts MATCH ? AND stage = 'lead' AND updated_at < datetime('now', '-30 days')",
        query,
    )
```

Each tool's signature is the JSON schema an agent sees, and its docstring is the description. Time
comes from the instance's clock, in Python and in SQL: `search_stale_leads` gives the same answer in
every rollout, this year and next.

Run your agent against it. Every rollout gets a private copy of a fixture, the same seed replays the
same run, and what the agent changed is a diff:

```py
for rollout in range(100):
    with world.instance("big_co", seed=rollout) as world_instance:  # a private copy of the fixture, in ms
        run_agent(world_instance)                                    # your agent, your harness
        reward = grade(world_instance.changes())                     # the net diff the agent left behind
```

Serve it. Every session gets its own instance, and any OpenEnv client can drive it:

```sh
seahaven serve
```

```py
from seahaven.openenv import SeahavenClient

with SeahavenClient(base_url="http://127.0.0.1:8000") as env:
    env.reset(fixture="big_co", seed=7)
    env.call("create_contact", email="ada@example.com", notes="asked about pricing for 50 seats")
    stale = env.call("search_stale_leads", query="pricing").result
```

For a full-size example, see [ProjectTracker](worlds/projecttracker/), the reference world: a
fictional issue tracker with nine tables, 25 tools, search and three fixtures.

Building a world with an agent? Point it at `seahaven docs`. The docs ship inside the package and
always match the installed version.

## Serving

Seahaven's remote lifecycle and transport are [OpenEnv](https://huggingface.co/docs/openenv/index).
`seahaven serve` runs one world and hundreds of sessions per process, each with its own instance.
`SeahavenClient` is one client for every Seahaven world, and the stock OpenEnv client works too.

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
