<p align="center">
  <img width="200" height="168" alt="seahaven logo" src="https://github.com/user-attachments/assets/79e3b5cd-3522-48ae-b5a6-ba356da4ee00" />
</p>
<h3 align="center">
  Synthetic world framework for agent evals and RL.
</h3>

<p align="center">
  <a href="#quickstart"><strong>Quick Start</strong></a> •
  <a href="src/seahaven/docs/index.md"><strong>Docs</strong></a> •
  <a href="#example-worlds"><strong>Examples</strong></a>
</p>

<p align="center">
  <a href="https://pypi.org/project/seahaven/"><img src="https://img.shields.io/pypi/v/seahaven?logo=pypi&label=PyPI&logoColor=gold" alt="PyPI"></a>
  <a href="https://github.com/Kiln-AI/Seahaven/actions/workflows/ci.yml"><img src="https://github.com/Kiln-AI/Seahaven/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-brightgreen" alt="MIT License"></a>
</p>

Evals and RL need thousands of agent runs, each isolated, starting from a known state, and graded
on what the agent changed. Production systems can't do that. Seahaven is a Python framework for
building synthetic worlds that can: working copies of your agent's tools, realistic enough that the
agent can't tell the difference.

Seahaven handles the hard parts: parallel instances, reproducibility, serving, and change logs. You only write what's specific to your world: its tables and its tools.

> Named after the town in *The Truman Show*: an entire world built so that one inhabitant believes
> it is real.

## Features

### Realistic Worlds

- **[Recreate Any Environment](src/seahaven/docs/authoring.md#writing-a-tool):** Mock AI tool
  calls, REST APIs, sandboxed SQL, search, or any custom format.
- **[Stateful](src/seahaven/docs/concepts.md#instance):** Each instance of a world has its own independent
  SQLite database.
- **[Composable](#composing-worlds):** Compose, reuse and share worlds. Example: MyCoWorld
  can include [StripeAPIWorld](https://github.com/Kiln-AI/stripe_world) and ShopifyAPIWorld.

### Built for Evals and RL

- **[Fixtures](src/seahaven/docs/db_schema_and_fixtures.md):** Freeze known starting states like
  `small_startup`, `agency` or `big_co`, and reuse them across runs.
- **[Concurrent Instances](src/seahaven/docs/serving_and_openenv.md):** Serve hundreds of world
  instances per process, at thousands of requests per second.
- **[Evaluate World State](src/seahaven/docs/state.md):** Grade on state, not on transcripts.
  Every row the agent changed is logged.
- **[Reproducible](src/seahaven/docs/concepts.md#reproducibility):** Same initial state (fixture),
  same clock/time, same random seed: the same run, every time.

### Connect Anything

- **[OpenEnv](#serve-with-openenv):** `seahaven serve` is an OpenEnv environment. Drive it with any
  OpenEnv client, in any language, or publish it to Hugging Face.
- **[Web Console](src/seahaven/docs/serving_and_openenv.md#the-web-console):** `seahaven serve`
  includes a web UI: open instances, call tools, and inspect state in your browser.
- **[MCP](#use-with-mcp-clients):** `seahaven mcp` serves one world to an MCP client, so you can
  work against it by hand from an editor or chat app.

### Easy to Build

- **[Built for Coding Agents](#build-worlds-with-your-coding-agent):** Docs optimized for agents
  authoring worlds. `seahaven check` tells an agent the exact fix for every mistake.
- **[Just Python](src/seahaven/docs/authoring.md#writing-a-tool):** Tools are just functions.
  Tests use pytest. Your agent already knows how to write and test Seahaven worlds.

## Seahaven vs. Real Systems and Mocks

|                                      | Seahaven | Production or staging | Hand-written mocks |
|--------------------------------------|:--------:|:---------------------:|:------------------:|
| Realistic tools and data             |    ✅    |          ✅           |         ❌         |
| Stateful across arbitrary tool calls |    ✅    |          ✅           |         ❌         |
| A private instance for every run     |    ✅    |          ❌           |         ✅         |
| Hundreds of parallel instances       |    ✅    |          ❌           |         ✅         |
| Every run starts from a known state  |    ✅    |          ❌           |         ✅         |
| Reproducible                         |    ✅    |          ❌           |         ✅         |
| Every change logged for grading      |    ✅    |          ❌           |         ❌         |
| Safe for the agent to break things   |    ✅    |          ❌           |         ✅         |

## Quickstart

**Create a world.** This writes a complete project: schema, tools, tests, a fixture generator, and
an `AGENTS.md` that points your coding agent at the docs.

```sh
uvx seahaven new crm_world # your world name
cd crm_world && uv sync
```

**Write your world.** A world is a schema and a set of tools. Here is a small CRM:

```python
import seahaven

world = seahaven.World(
    name="crm",
    version="1.0.0",
    schema="""
    CREATE TABLE contacts (
        id TEXT PRIMARY KEY,
        email TEXT NOT NULL,
        stage TEXT NOT NULL,
        updated_at TEXT NOT NULL
    ) STRICT;
    """,
    state_format="seahaven.state/1",
)


@world.tool
def create_lead(ctx: seahaven.Ctx, email: str) -> dict[str, str]:
    """Add a contact to the pipeline as a new lead."""
    lead = {"id": ctx.ids.uuid(), "email": email, "stage": "lead", "updated_at": ctx.clock.iso()}
    ctx.db.execute("INSERT INTO contacts VALUES (?, ?, ?, ?)", *lead.values())
    return lead


@world.tool
def list_stale_leads(ctx: seahaven.Ctx) -> list[dict[str, object]]:
    """List leads nobody has touched in 30 days."""
    return ctx.db.rows(
        "SELECT * FROM contacts WHERE stage = 'lead' "
        "AND updated_at < strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-30 days')"
    )
```

**Freeze a starting state.** A fixture is a frozen database that every run starts from:

```py
with world.instance(now="2026-06-01T09:00:00.000Z", clock_mode="fixed") as inst:
    for n in range(500):
        inst.call("create_lead", email=f"lead{n}@example.com")
    inst.freeze("big_co", "A pipeline of 500 new leads.")
```

**Run your agent.** Each run gets a private copy of the fixture in milliseconds. The same seed
replays the same run, and what the agent changed is a document you grade:

```py
for rollout in range(100):
    with world.instance("big_co", seed=rollout) as inst:
        run_agent(inst)  # your agent, your harness
        reward = grade(inst.state())  # every row the agent changed
```

**Serve it.** `seahaven serve` hosts an OpenEnv endpoint where every connection gets its own
instance. Open `http://127.0.0.1:8000/console` to drive it by hand.

```sh
uv run --extra serve seahaven serve
```

```py
from seahaven.openenv import SeahavenClient

with SeahavenClient(base_url="http://127.0.0.1:8000") as env:
    env.reset(fixture="big_co", seed=42)
    env.call("create_lead", email="ada@example.com")
    final_state = env.state()  # the document the eval grades
```

## Example Worlds

- **[ProjectTracker](worlds/projecttracker/)**: the reference world, a fictional issue tracker
  shaped like Linear or Jira. Nine tables, 25 tools, full-text search, and fixtures from an empty
  workspace to a twelve-person agency with six months of history. Start here to learn the patterns
  ([walkthrough](src/seahaven/docs/projecttracker.md)).
- **[Stripe World](https://github.com/Kiln-AI/stripe_world)**: a mock of Stripe's Billing and
  Payments core, with 24 tables and 155 API operations behind the same tools as Stripe's own MCP
  server. It also serves Stripe's REST API, so the Stripe SDKs work against it unchanged.

## Composing Worlds

Build a world once and reuse it everywhere. A company world can add a payments world, such as
[Stripe World](https://github.com/Kiln-AI/stripe_world), and a chat world, plus its own tables and
tools. The agent sees one tool list, and an eval grades what changed in every world from one state
document. See the [composition docs](src/seahaven/docs/composition.md).

```py
company.add_world(payments_world.world, name="payments", tool_prefix="pay_")
company.add_world(chat_world.world, name="chat", tool_prefix="chat_")


@company.tool
def refund_order(ctx: seahaven.Ctx, charge_id: str, channel: str) -> dict[str, object]:
    """Refund a charge and tell the support channel it is done."""
    refund = ctx.worlds.payments.call("create_refund", charge_id=charge_id)
    ctx.worlds.chat.call("post_message", channel=channel, text=f"refunded {refund['amount']}")
    return refund
```

## Serve with OpenEnv

`seahaven serve` hosts your world as an [OpenEnv](https://github.com/huggingface/OpenEnv)
environment, the open standard for RL environments. Every connection gets its own private instance.
Each process can host hundreds of parallel instances. Drive it from Python, from
[Kiln](https://kiln.tech), or from any OpenEnv client, such as OpenEnv's own generic client:

```py
from openenv import GenericEnvClient
from openenv.core.env_server.mcp_types import CallToolAction

with GenericEnvClient(base_url="http://127.0.0.1:8000") as env:
    env.reset(fixture="big_co", seed=7)
    create = CallToolAction(tool_name="create_lead", arguments={"email": "ada@example.com"})
    env.step(create.model_dump())
    final_state = env.state()
```

See the [serving docs](src/seahaven/docs/serving_and_openenv.md) for the client, the wire protocol
and running in production.

## Use with MCP Clients

`seahaven mcp` connects a world to Claude, Cursor, or any MCP client. Explore a world by hand, debug
your tools, or try a task yourself before you give it to an agent.

```sh
uv run --extra mcp seahaven mcp --fixture big_co
```

## Build Worlds with Your Coding Agent

Seahaven is designed to be built by coding agents. `seahaven new` writes an `AGENTS.md` that points
your agent at the docs for the version you have installed, not stale ones from the web.
`seahaven check` catches the mistakes that are easy to make and hard to notice, and names the fix.

## Contributing

See [CONTRIBUTING.md](.github/CONTRIBUTING.md) for setup and the checks CI runs.

## License

[MIT](LICENSE).

## Created by Kiln AI

Seahaven is built by the team behind [Kiln](https://kiln.tech), a free app and open-source library
for building better AI products. Kiln connects to any Seahaven world: write scenarios against a
fixture, [evaluate](https://kiln.tech/features/evals) your agent on the state it leaves behind, then
[auto-optimize](https://kiln.tech/features/auto-optimize) prompts and models against those evals.
