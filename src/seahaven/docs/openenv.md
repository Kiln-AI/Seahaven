# OpenEnv

Seahaven is an OpenEnv environment. `seahaven serve` speaks
[OpenEnv](https://github.com/huggingface/OpenEnv) (v0.4.x) over its WebSocket endpoint `/ws`,
and nothing else: a world is either driven in process through `world.instance(...)`, or over that
WebSocket. There is no Seahaven-specific remote API to learn, no SDK to install to talk to a world,
and nothing per-world to generate.

**OpenEnv** is an open standard for connecting to reinforcement-learning environments: a server
hosts an environment, a client connects, and the session is `reset` / `step` / `state` / `close`
over a WebSocket, with JSON frames on the wire. It is the interface RL trainers and eval harnesses
already speak, which is why Seahaven took it rather than inventing a protocol.

Two consequences worth stating plainly:

- **Any OpenEnv client works.** The stock Python client drives a Seahaven world; so does
  `SeahavenClient`, which is the same client with the observation typed and two conveniences on it.
  One divergence is worth reading before writing a client of your own: how a tool's *error* is
  carried, below.
- **Any language works.** The wire is JSON over a WebSocket, documented below. A harness in
  TypeScript, Go or Rust needs a WebSocket and a JSON encoder, not a Seahaven port.

This page is for whoever *drives* a world — an eval harness, an RL trainer, a scenario runner.
[serving.md](serving.md) is the other side of the same wire: running the server, its flags, its
capacity, and publishing to a hub.

## Driving a world

Start the server. One world per process; `seahaven serve` finds the world by convention.

```sh
seahaven serve
seahaven serve --port 9000
```

Then connect. Every connection is its own session with its own private instance, so a harness opens
one connection per rollout and closes it when the rollout ends.

```py
from seahaven.openenv import SeahavenClient

with SeahavenClient(base_url="http://127.0.0.1:8000") as env:
    # A private copy of the fixture, made in milliseconds. The seed makes the
    # run replayable: same fixture, same seed, same ids and the same clock.
    env.reset(fixture="small_startup", seed=7)

    # The world's tool surface, as JSON schemas, ready to hand to a model.
    for tool in env.list_tools():
        print(tool["name"], tool["description"], tool["input_schema"])

    # A tool call. Exactly one of `.result` and `.error` is set, always.
    observation = env.call("get_issue", key="ENG-12")
    if observation.error is None:
        print(observation.result["title"])
    else:
        print(observation.error["code"], observation.error["message"])

    # Writes are writes: the next read sees them.
    env.call("transition_issue", issue_id=observation.result["id"], status="done")

    print(env.state().now)  # the instance's frozen instant
```

`reset` takes what `world.instance(...)` takes, because it *is* `world.instance(...)`:

| Argument | What it does |
|---|---|
| `fixture=` | the frozen starting state to copy. Omitted, the instance is blank, built from the world's DDL |
| `seed=` | the seed behind `ctx.ids`, and behind SQL's `random()` and `randomblob()` |
| `now=` | the clock, for a blank instance only. A fixture carries its own, and `now=` with one is refused |
| `episode_id=` | your own id for the episode, echoed back on `state` so a trajectory ties to your run |
| anything else | passed to the world's startup hooks, so a world can be customised per episode |

Everything is awaitable in asynchronous code and direct in synchronous code, which is the stock
OpenEnv client's behaviour and not an addition:

```py
import asyncio

from seahaven.openenv import SeahavenClient


async def rollout(scenario: str, seed: int) -> None:
    async with SeahavenClient(base_url="http://127.0.0.1:8000") as env:
        await env.reset(fixture="agency", seed=seed)
        tools = await env.list_tools()
        observation = await env.call("search_issues", query=scenario)
        print(observation.result)


asyncio.run(rollout("billing", seed=1))
```

Hundreds of these run against one server. A dropped client costs nothing: the session is destroyed
and its copy of the fixture goes with it.

**Only a framework or protocol failure raises**, as `RuntimeError`. A tool's own error — a bad
argument, a missing row, a rule the world enforces — arrives on `observation.error` and never ends
the session, because an agent is meant to read it and try something else.

## Any client, any language

`SeahavenClient` is a convenience, not the protocol. The protocol is OpenEnv's, and a client in any
language needs only these frames on `ws://host:port/ws`.

**Client to server:**

| Frame | Meaning |
|---|---|
| `{"type": "reset", "data": {...}}` | make the instance; `data` holds the arguments in the table above |
| `{"type": "step", "data": {"type": "call_tool", "tool_name": "...", "arguments": {...}}}` | call a tool |
| `{"type": "step", "data": {"type": "list_tools"}}` | the world's tools; needs no `reset` |
| `{"type": "state"}` | the session's state: `episode_id`, `step_count`, `fixture`, `now`, `world`, `composition` |
| `{"type": "close"}` | end the session and destroy the instance |

**Server to client:**

| Frame | Meaning |
|---|---|
| `{"type": "observation", "data": {"observation": {...}, "reward": null, "done": false}}` | the answer to a `reset` or a `step` |
| `{"type": "state", "data": {...}}` | the answer to a `state` |
| `{"type": "error", "data": {"code": "...", "message": "..."}}` | a *protocol* failure, not a tool error |

A tool call and its two possible answers, whole:

```json
{"type": "step", "data": {"type": "call_tool", "tool_name": "get_issue", "arguments": {"key": "ENG-12"}}}

{"type": "observation", "data": {"observation": {"tool_name": "get_issue", "result": {"key": "ENG-12", "title": "The session cookie leaks a stack trace"}, "error": null}, "reward": null, "done": false}}

{"type": "observation", "data": {"observation": {"tool_name": "get_issue", "result": null, "error": {"code": "NOT_FOUND", "message": "issue ENG-99 not found", "details": {"kind": "issue"}}}, "reward": null, "done": false}}
```

The `error` frame is OpenEnv's own, and its `code` is one of `INVALID_JSON`, `UNKNOWN_TYPE`,
`VALIDATION_ERROR`, `EXECUTION_ERROR`, `CAPACITY_REACHED`, `FACTORY_ERROR` or `SESSION_ERROR`. It
means the frame or the session failed — a malformed action, a server at capacity, a world that
raised a `WorldBug`. It is never how a tool reports that an issue does not exist.

**`/ws` is the only path an episode travels.** `POST /reset`, `POST /step` and `GET /state` are
refused with a `501`, because OpenEnv builds a fresh environment inside each of those handlers and
closes it before replying — three requests, three instances, none of them the session's. They stay
in the published schema, since `openenv push` reads path names to decide what kind of environment a
world is, but only `/ws` holds an episode. "Rough edges" in [serving.md](serving.md) has the
refusal body and the upstream issue.

## What the OpenEnv model means here

Four things are worth knowing before designing a harness around this.

**One world per server process.** `seahaven serve` serves exactly one world, mounted at `/`, on one
worker. That is the shape hubs expect — one environment per image or Space — and it is not a
limitation Seahaven can lift from the inside: a session's instance is in-process state, so a second
worker would answer a session's second frame with an environment that has never seen its first.
Several worlds means several processes. Scaling one world out means more processes behind a load
balancer with connection affinity. One world is not one *package*, though: a world that adds other
worlds serves their tools as part of its own surface, under whatever names it publishes them as, so
a composite world is still one environment on the wire ([composition.md](composition.md)).

**One instance per connection.** A WebSocket connection is a session, and a session holds exactly
one instance: its own SQLite database, its own frozen clock, its own seeded ids. Sessions do not
see each other. One process serves hundreds of them — the default budget is 500 — and over capacity
OpenEnv answers `CAPACITY_REACHED` and closes the connection rather than degrading the sessions it
already has.

**`reset` is where an episode is customised.** The fixture, the seed, the clock for a blank
instance, and any startup keyword the world declares all travel on `reset`, so one served world
covers every scenario a fixture and a hook can express. A second `reset` destroys the first
instance before making the next, so a session never holds two; if that creation fails, the session
is left exactly as a fresh one and is open for another `reset`. Closing the connection destroys the
instance.

**Discovery does not need an episode.** `list_tools` is answered without a `reset`, because the
tool list belongs to the world rather than to the episode. Control tools are never in it.

## Reading what the agent did

An eval grades the state the episode left behind, and that state is a **changeset**: the net
difference between the fixture and the database as the agent left it — a list of
`{world, table, op, key, before, after}` records, where `op` is `insert`, `update` or `delete` and
`world` is the node the row belongs to (`main` for a world that adds none). Net, not a log: a write that changes nothing records nothing, an insert then an update of the same row is one
insert, and a rolled-back call leaves no trace. [concepts.md](concepts.md) has the full semantics.

**`state` is changing, and this section with it.** Today the `state` frame answers the session's
metadata only — `episode_id`, `step_count`, `fixture`, `now`, `world` — and the changeset is
reachable over the wire through the `controller_changes` control tool, which a server exposes only
when it was started with the flag for it:

```sh
seahaven serve --include-control-tools
```

```py
from seahaven.openenv import SeahavenClient

with SeahavenClient(base_url="http://127.0.0.1:8000") as env:
    env.reset(fixture="small_startup", seed=7)
    run_agent(env)  # your agent, your harness

    changes = env.call("controller_changes").result
    reward = grade(changes)  # your scenario's goal, your grader
```

The changeset is being moved onto `state`, so that `env.state()` answers the diff directly and an
eval needs neither the flag nor a control tool to grade a rollout. **Expect this to change**: this
page is updated when it lands, and until then the control tool is the path.

In process — in pytest, in a script, in a harness that does not serve — this has never needed a
flag: `inst.changes()` is the same changeset, and `inst.call("controller_changes")` always reaches
the control tool.

## No rewards, no done

**A Seahaven observation carries no reward.** `reward` is always `null` and `done` is always
`false`. The environment never ends an episode and never scores one.

That is deliberate, and it is the design decision most worth understanding before building on this.

Many RL environments model a world where reward is easy to state: a game has a win, a score, a
terminal state, and the environment is the natural place to compute it. Seahaven models the other
kind — a large, complex application: a CRM, an issue tracker, a billing system, a company's whole
internal tool surface. **There is no universal reward signal for a world like that.** Whether a
final state is good depends entirely on what the agent was asked to do in that session. The same
database, with the same three issues closed and one contact created, is a success for one scenario
and a failure for the next. **Whether the session is done is a decision of the caller, not of the
environment**, for the same reason: the environment cannot know what finishing looks like.

The thing that *does* know the goal is the eval or RL framework driving the episode. So Seahaven
gives it the material to judge with — the complete, net diff of what the agent changed — and stays
out of the judging.

**The payoff is reuse.** A world with no opinion about reward is a world you build once. Build
`MyCorp`, freeze the fixture `BigClient`, and then write hundreds of scenarios against that
pair — each with its own goal, its own grader, its own idea of what a good final state is. A world
that computed a reward would have baked one scenario's goal into the environment, and the next
scenario would need a new world.

## Where Seahaven diverges from OpenEnv's conventions

Two, both deliberate, both things a client author should know.

- **A tool's error travels on `error`.** OpenEnv's convention reserves `error` for transport
  failures and puts a tool's own error inside `result`. Seahaven does the opposite: a tool error is
  data the agent reads, it must never close the session, and one shape for it across every
  world — `{"code", "message", "details"}`, the same dict an in-process `ToolError` gives — is
  worth more than the convention. Read `observation.error`, expect those three keys.

  **This has a cost, and it is not only stylistic.** OpenEnv's own `CallToolObservation` types that
  field as its `ToolError` model — `{error_type, message}`, and `extra="forbid"` — so a client that
  validates a frame into *that* model rejects a Seahaven tool error outright, and OpenEnv's MCP
  client raises on any non-null `error` before reading `.message` off it. Successes are
  interoperable with every client; a tool *error* wants a client that parses `error` leniently, or
  `SeahavenClient`, whose observation model is this shape.

- **A `WorldBug` fails the frame loudly.** It is not rendered as an observation. An eval that
  scored a run while the world was broken is the failure this design exists to prevent, so the
  author sees their bug instead. An unexpected exception inside a tool is a different case: it is
  logged with its traceback and answered with a fixed `{"code": "internal", "message": "internal
  error"}`, so engine text never reaches an agent.

## MCP moves are not supported

OpenEnv RFC 003 defines support for MCP, but it is not approved and still in review. It notes no
session support as a known gap — and Seahaven is for building *stateful* MCP-shaped servers, where
the session is the whole point: an instance is a session, and two sessions must not see each
other's writes.

It does not make sense to add MCP support to Seahaven until there is upstream support for stateful
servers — support that does not mutate the tool interface, and does not require passing a
non-standard session id alongside every call.

So an OpenEnv app's `/mcp` endpoint is refused here, on both of its transports and for every method
it dispatches, rather than left to answer well-formed nothings: its dialect has no `reset`, and
without one every `tools/call` behind a successful `tools/list` can only answer `reset first`.
"Rough edges" in [serving.md](serving.md) has the refusal, its JSON-RPC envelope, and why `/ws` is
the agent-facing transport in spite of OpenEnv's own advice to the contrary.

For now: **use the standard WebSocket API and clients.** They are what every example on this page
uses, and they are what `SeahavenClient` and every other OpenEnv client speak.

## Publishing to a hub

A world can be published as an OpenEnv environment — a Docker image or a Hugging Face Space — and
driven by anyone with an OpenEnv client. `seahaven new --hub` writes the five files `openenv push`
validates a directory for, and the environment's card is the world's own `README.md` with
`World(description=...)` as the line beside it. The details, including what does not work yet about
the generated `Dockerfile`, are in ["Publishing to a hub" in serving.md](serving.md).

## Evaluating and optimizing with Kiln

[Kiln](https://kiln.tech) connects to a Seahaven world as an OpenEnv client, which is the whole
integration: point it at a served world and it drives the sessions.

Kiln is where the goal that Seahaven deliberately does not hold gets written down. Define a
scenario against a world and fixture, run it as an [eval](https://kiln.tech/features/evals) and
grade the final state against what the scenario asked for, then
[auto-optimize](https://kiln.tech/features/auto-optimize) the agent — prompts, models,
fine-tuning — against that eval. Same world, same fixture, as many scenarios as the job needs.

Seahaven is built by the Kiln AI team.

## Rough edges, and one install trap

OpenEnv's own rough edges under Seahaven — the three HTTP episode-control routes and `/mcp`,
both refused above — are listed at the end of [serving.md](serving.md), with the upstream issue for
each. None of them is in the WebSocket path an eval uses.

One install note that bites before any of this: **do not `pip install "seahaven[serve]"`.** The
framework is not published yet, and the `seahaven` name on PyPI holds a placeholder that has no
`serve` extra, so that command succeeds and installs nothing useful. Install from a checkout until
publication — [serving.md](serving.md) has the exact command and the Python version it needs.
