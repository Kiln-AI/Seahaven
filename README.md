# Seahaven

**Synthetic worlds for AI agents.** Fake, stateful replicas of the systems your agent works
against, for RL and evals.

[Docs](src/seahaven/docs/index.md) · [PyPI](https://pypi.org/project/seahaven/) · [Kiln AI](https://kiln.tech)

> **Seahaven** *(noun)*
>
> 1. A Python framework for building synthetic worlds for AI agents.
> 2. The town in *The Truman Show*. An entire world built so that one inhabitant believes it is
>    real.

RL and evals need thousands of rollouts, in parallel, each from a known state, each inspectable
afterwards. No real system or staging copy can do that.

Seahaven worlds can: clone the tools your agent uses in production, fork hundreds of private
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
- **[Composable worlds](#composing-worlds).** Add sub-worlds to your world, like a full Stripe
  or Shopify API. Compose, reuse and share worlds.
- **[OpenEnv](https://huggingface.co/docs/openenv/index).** `seahaven serve` is an OpenEnv
  environment. Drive it with any OpenEnv client, or publish it to Hugging Face.

## Quickstart

**Install:** `uv add seahaven` or `pip install seahaven`. Python 3.14+.

**Scaffold a world:** `uv run seahaven new crm`

**Build your world:** a schema and a set of tools. Here is a CRM with one table, a search index and two
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
    contact = {
        "id": ctx.ids.uuid(),
        "email": email,
        "notes": notes,
        "stage": "lead",
        "updated_at": ctx.clock.iso(),
    }
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

Each tool's signature is the JSON schema an agent sees, and its docstring is the description.

**Run your agent against it:** Every rollout gets a private copy of a fixture, the same seed replays the
same run, and what the agent changed is a diff:

```py
for rollout in range(100):
    with world.instance(
        "big_co", seed=rollout
    ) as world_instance:  # a private copy of the fixture, in ms
        run_agent(world_instance)  # your agent, your harness
        reward = grade(world_instance.changes())  # the net diff the agent left behind
```

**Serve it:** Host and OpenEnv endpoint. Every connection gets its own instance. Any OpenEnv client can connect.

```sh
uv run seahaven serve
```

```py
from seahaven.openenv import SeahavenClient

with SeahavenClient(base_url="http://127.0.0.1:8000") as env:
    env.reset(fixture="big_co", seed=42)
    env.call("create_contact", email="ada@example.com", notes="asked about pricing for 50 seats")
    stale = env.call("search_stale_leads", query="pricing").result
    changes = env.state()  # the final state, as a diff
```

**Example World:** see [ProjectTracker](worlds/projecttracker/), the reference world: a
fictional issue tracker with nine tables, 25 tools, search and three fixtures.

## Composing worlds

A world can **add other worlds**. Build a payments world once, a chat world once, and a company
world that adds both plus its own tables and tools. The agent sees one tool list; the company
world's own tools call the added worlds' tools in process; evals inspect every store through one SQL
connection.

```python
import seahaven

# Each world is normally its own package, and the host adds the `world` object
# that package exports. One file here so the example runs.
payments = seahaven.World(
    name="payments",
    version="1.0.0",
    schema="CREATE TABLE charges (id TEXT PRIMARY KEY, amount INTEGER NOT NULL) STRICT;",
)


@payments.tool
def create_charge(ctx: seahaven.Ctx, amount: int) -> dict[str, object]:
    """Charge the account and return the charge."""
    charge = {"id": ctx.ids.uuid(), "amount": amount}
    ctx.db.execute("INSERT INTO charges (id, amount) VALUES (?, ?)", charge["id"], charge["amount"])
    return charge


company = seahaven.World(
    name="company",
    version="0.1.0",
    schema="CREATE TABLE invoices (id TEXT PRIMARY KEY, charge_id TEXT NOT NULL) STRICT;",
)
company.add_world(payments, name="payments", tool_prefix="pay_")


@company.tool
def invoice(ctx: seahaven.Ctx, amount: int) -> dict[str, object]:
    """Charge the company's payment account and file an invoice against the charge."""
    charge = ctx.worlds.payments.call(create_charge, amount=amount)
    filed = {"id": ctx.ids.uuid(), "charge_id": charge["id"]}
    ctx.db.execute(
        "INSERT INTO invoices (id, charge_id) VALUES (?, ?)", filed["id"], filed["charge_id"]
    )
    return filed


with company.instance() as inst:
    assert [listed["name"] for listed in inst.tools()] == ["invoice", "pay_create_charge"]
    inst.call("invoice", amount=500)
    # One read-only connection over both stores, and one changeset across both.
    charged = inst.inspect().one("SELECT amount FROM payments.charges")
    assert charged is not None and charged["amount"] == 500
    assert {change.world for change in inst.changes()} == {"main", "payments"}
```

If several added worlds contain a payments world, they share one store by default, so a charge
created through one is visible to the others, as it would be with one real account. Override it
where it should not be shared:

```py
world.add_world(payments_world.world, name="payments_eu", tool_prefix="eu_", store="eu")
```

Each store is its own SQLite file with its own DDL, and fixtures are frozen and forked at the top
level with every store inside. [`composition.md`](src/seahaven/docs/composition.md) is the page.

## Serving (OpenEnv)

Seahaven's remote lifecycle and transport are [OpenEnv](https://huggingface.co/docs/openenv/index),
an open standard for connecting to RL environments. `seahaven serve` runs one world, creating a
unique instance and episode for each connection. Serve over 100 instances per process. Connect with
any OpenEnv client or tool, like [Kiln](https://kiln.tech).

## Agents

Building a world with an agent? Point it at `uv run seahaven docs`. The docs ship inside the package and
always match the installed version.

## License

Licensed under [MIT](LICENSE).

## Created by Kiln AI

Seahaven was created by [Kiln AI](https://github.com/Kiln-AI/Kiln). Kiln is an open-source
platform for building, evaluating and optimizing AI systems, and supports Seahaven worlds as the
environments its [evals](https://kiln.tech/features/evals) and
[optimizers](https://kiln.tech/features/auto-optimize) run against.
