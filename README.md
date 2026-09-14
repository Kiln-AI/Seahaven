# Seahaven

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

## A world, in one screen

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

A declaration, a function whose signature is its published contract, an instance that is a private
copy, a frozen clock, and a changeset an eval can grade. A real world spreads the same parts over a
package, which `seahaven new` lays out, and adds fixtures, errors and tests.

## Features

- **Stateful worlds.** Writes change every later read. Each instance is its own SQLite database,
  forked from a fixture in milliseconds. Expose it as REST-shaped tools, a sandboxed SQL tool,
  full-text search, or a protocol of your own.
- **Fixtures.** Freeze known starting states like `small_startup` or `agency` and reuse them across
  evals. Fixtures are immutable, hash-verified, and committed with the script that builds them.
- **Ephemeral instances.** An instance lives as long as the episode, then is deleted. Nothing to
  clean up, nothing shared between runs.
- **Hosting.** Serve hundreds of instances of a world from one process. Every session gets its own
  private copy.
- **Fast.** Thousands of tool calls per second per process, across concurrent instances. A tool
  call is low milliseconds.
- **Time is first class.** Every instance has a clock, frozen at its fixture's `now`. Tools read it,
  and so does every SQLite date function. The same fixture, seed and calls give the same run.
- **Changesets.** `inst.changes()` is the net diff between the fixture and what the agent left
  behind. Grade on state, not on transcripts.
- **Composable worlds** *(coming soon)*. YourWorld imports StripeWorld and ShopifyWorld. Reuse worlds
  others built and write only your domain's datastore and APIs.
- **[OpenEnv](https://huggingface.co/docs/openenv/index) compatible.** `seahaven serve` is an OpenEnv
  environment. Drive it with any OpenEnv client, or publish it to a hub like Hugging Face.
- **Built for AI authors.** Docs ship inside the package, `seahaven check` turns every framework
  rule into a lint with a named fix, and a pytest plugin gives every world tests for free.
- **Extensible.** New protocols and query languages are ordinary Python packages on documented
  seams. An XML-RPC extension ships as the worked example.

## Quickstart

Seahaven is not on PyPI yet (the name holds a placeholder release), so install it from a checkout.
You need [uv](https://docs.astral.sh/uv/) and a **final** release of Python 3.14 or newer, not a
release candidate.

```sh
git clone https://github.com/Kiln-AI/Seahaven && cd Seahaven
uv python install 3.14
uv sync --extra serve
```

Scaffold a world. The scaffold is a working world: one table, two tools, an error handler, passing
tests, and the script every fixture will be built by.

```sh
uv run seahaven new crm
uv pip install -e ./crm --no-deps
uv run pytest crm                          # the scaffold's tests
uv run seahaven check --world crm:world    # every lint, each with a named fix
```

A world is a schema, tools, fixtures and tests. The schema is hand-written SQLite DDL:

```sql
-- crm/src/crm/schema/001_core.sql
CREATE TABLE customers (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    plan TEXT NOT NULL CHECK (plan IN ('free', 'pro')),
    created_at TEXT NOT NULL
) STRICT;
```

Tools are plain functions. The signature is the JSON schema an agent sees, the docstring is the
description, and errors are the ones the real product would return:

```py
# crm/src/crm/tools/customers.py
from typing import Any

import seahaven

from crm.errors import NotFound
from crm.world import world


@world.tool
def create_customer(ctx: seahaven.Ctx, email: str, plan: str = "free") -> dict[str, Any]:
    """Create a customer and return it."""
    customer = {"id": ctx.ids.uuid(), "email": email, "plan": plan, "created_at": ctx.clock.iso()}
    ctx.db.execute(
        "INSERT INTO customers (id, email, plan, created_at) VALUES (?, ?, ?, ?)",
        customer["id"],
        customer["email"],
        customer["plan"],
        customer["created_at"],
    )
    return customer


@world.tool
def get_customer(ctx: seahaven.Ctx, customer_id: str) -> dict[str, Any]:
    """Fetch one customer by id."""
    row = ctx.db.one("SELECT * FROM customers WHERE id = ?", customer_id)
    if row is None:
        raise NotFound("customer", customer_id)
    return row
```

Fixtures are frozen from a generator function, so every one is reproducible from source:

```py
# crm/fixtures_src/generate.py
import seahaven


def small_saas(inst: seahaven.Instance) -> None:
    """Two customers, one of them paying."""
    inst.call("create_customer", email="ada@example.com", plan="pro")
    inst.call("create_customer", email="grace@example.com")
```

```sh
uv run seahaven fixture freeze small_saas \
    --now 2026-06-01T09:00:00.000Z \
    --run fixtures_src.generate:small_saas \
    --description "Two customers, one of them paying." \
    --world crm:world
```

Tests come from the pytest plugin. Name a fixture, and every test gets a fresh instance of it:

```py
# crm/tests/test_customers.py
import pytest

import seahaven

pytestmark = pytest.mark.seahaven(fixture="small_saas")


def test_a_missing_customer_is_not_found(instance: seahaven.Instance) -> None:
    with pytest.raises(seahaven.ToolError) as raised:
        instance.call("get_customer", customer_id="nope")
    assert raised.value.code == "NOT_FOUND"
```

Then serve it. One command, one world, as many sessions as your evals need:

```sh
uv run seahaven serve --world crm:world
```

## Driving a world

**In process**, from Python, an instance is a context manager. This drives the reference world,
ProjectTracker, and grades the result on state:

```python
import projecttracker

with projecttracker.world.instance("agency", seed=7) as tracker:
    issue = tracker.call("get_issue", key="ENG-12")
    tracker.call("transition_issue", issue_id=issue["id"], status="done")

    # What did the agent change? The net diff against the fixture.
    assert ("issues", "update") in {(c.table, c.op) for c in tracker.changes()}

    # Or ask the database directly, read-only.
    row = tracker.inspect().one("SELECT status FROM issues WHERE key = ?", "ENG-12")
    assert row == {"status": "done"}
```

**Over the wire**, a world is an OpenEnv environment. `seahaven serve` runs one world, and every
session gets its own instance. `SeahavenClient` is one client for every Seahaven world, and the
stock OpenEnv client works too:

```py
from seahaven.openenv import SeahavenClient

with SeahavenClient(base_url="http://127.0.0.1:8000") as env:
    env.reset(fixture="agency", seed=7)
    tools = env.list_tools()  # [{"name", "description", "input_schema"}, ...]
    observation = env.call("get_issue", key="ENG-12")
    print(observation.result["title"])
```

Pass `--include-control-tools` to `serve` and an eval can read changesets and run inspection SQL
over the same connection.

## The reference world

[`worlds/projecttracker/`](worlds/projecttracker/) is ProjectTracker, a fictional Linear/Jira-shaped
issue tracker: nine tables, 25 tools, full-text search, an audit trail, and three fixtures (`empty`,
`small_startup`, `agency`). It is the pattern a new world copies and the world the docs are written
against.

```sh
cd worlds/projecttracker
uv run seahaven fixture list
uv run seahaven check
uv run pytest
```

## Status

**Early development.** The framework, the reference world, the example extension and the benchmark
are complete and tested in this repository, but nothing is published yet and names may still move.
The `seahaven` name on PyPI holds a placeholder, so `pip install seahaven` gets you a stub. Install
from a checkout as above.

Python 3.14 has to be a final release: on the 3.14 release candidates two locked dependencies break
and `import seahaven` fails, and uv will sync onto a release candidate without warning. `uv run
python -V` should print `3.14.0` or higher with no `rc` in it.

## Documentation

The docs ship **inside the installed package**, so they always match the version you have, and
`seahaven docs` prints where they are. In this repository they are
[`src/seahaven/docs/`](src/seahaven/docs/):

- [`index.md`](src/seahaven/docs/index.md): what Seahaven is, the reading order, the commands
- [`concepts.md`](src/seahaven/docs/concepts.md): world, fixture, instance, tool, clock, changesets
- [`authoring.md`](src/seahaven/docs/authoring.md): writing tools, errors, middleware, startup hooks, the schema
- [`fixtures.md`](src/seahaven/docs/fixtures.md): freezing, forking, generators
- [`testing.md`](src/seahaven/docs/testing.md): the pytest plugin, what to test
- [`serving.md`](src/seahaven/docs/serving.md): `seahaven serve`, the client, control tools, publishing to a hub
- [`extensions.md`](src/seahaven/docs/extensions.md): the extension contract
- [`projecttracker.md`](src/seahaven/docs/projecttracker.md): a walkthrough of the reference world
- [`reference/`](src/seahaven/docs/reference/): the API, every lint code, every CLI option

Every example on those pages, and on this one, is executed by the test suite.

## Contributing

Read [`CONTRIBUTING.md`](CONTRIBUTING.md): setup, the checks that have to pass, and the guidelines.
Seahaven is early and the API is still moving, so open an issue before starting anything larger
than a bug fix.

## License

MIT.

## Created by Kiln AI

Seahaven was created by [Kiln AI](https://github.com/Kiln-AI/Kiln). Kiln works with Seahaven to
[evaluate](https://kiln.tech/features/evals) and [optimize](https://kiln.tech/features/evals)
agents.
