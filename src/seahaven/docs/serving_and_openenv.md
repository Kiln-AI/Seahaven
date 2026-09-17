# Serving a world

A world can run as a server, so that an eval harness, an RL trainer or an agent framework drives it
over the network instead of importing it. Each connection gets its own session, and each session
gets its own private instance. Hundreds of sessions run against one process.

Seahaven speaks [OpenEnv](https://github.com/huggingface/OpenEnv) (v0.4.x), an open standard for
connecting to reinforcement-learning environments. A server hosts an environment, a client connects,
and the session is `reset` / `step` / `state` / `close` over a WebSocket, with JSON frames on the
wire. Seahaven took that standard rather than inventing a protocol, which means two useful things:
any OpenEnv client can drive a Seahaven world, and a harness written in TypeScript, Go or Rust needs
a WebSocket and a JSON encoder rather than a Seahaven port.

A world is driven either in process through `world.instance(...)`, or over the WebSocket endpoint
`/ws`. There is no third way and no Seahaven-specific remote API to learn.

| Section | What it covers |
|---|---|
| [Running the server](#running-the-server) | Installing the extra, the app file, the command and its options |
| [Sessions and instances](#sessions-and-instances) | What a connection holds, and what `reset` does |
| [Driving a world from Python](#driving-a-world-from-python) | `SeahavenClient`, synchronous and asynchronous |
| [Calls, results and errors](#calls-results-and-errors) | What comes back from a tool call |
| [Grading a run](#grading-a-run) | Changesets over the wire, and the control tools |
| [Why there are no rewards](#why-there-are-no-rewards) | The design decision behind an empty `reward` |
| [The wire protocol](#the-wire-protocol) | Every frame, for a client in any language |
| [The concurrency gate](#the-concurrency-gate) | What bounds tool calls, and a known defect in it |
| [Running it in production](#running-it-in-production) | Reaping, disconnects, and scaling out |
| [Publishing to a hub](#publishing-to-a-hub) | `seahaven new --hub`, and what does not work yet |
| [Evaluating with Kiln](#evaluating-with-kiln) | Where the scenario and the grader belong |
| [Known problems in OpenEnv](#known-problems-in-openenv) | Routes Seahaven refuses, and why |

## Running the server

### Install the serve extra

The server and the client are in Seahaven's `serve` extra, because OpenEnv's dependency tree is
large and a world used in process should not pay for it. The environment needs a final CPython 3.14
or newer, not a release candidate.

**Do not run `pip install "seahaven[serve]"`.** The framework is not published yet. The `seahaven`
name on PyPI currently holds a placeholder release that contains none of this and has no `serve`
extra, so that command succeeds and installs nothing useful, which is worse than failing. Until
publication, install the framework and its extra from a checkout of the Seahaven repository, for
example `uv pip install -e "/path/to/Seahaven[serve]"` into the environment your world runs in.

### The app file

A world's whole server is one file, which `seahaven new` writes:

```py
# src/notes/openenv_app.py
import seahaven.openenv

from notes import world

app = seahaven.openenv.app(world)
```

It is a module of its own rather than part of the package's `__init__`, because importing it needs
the extra. A world used in process — in pytest, in a script, in a notebook — never imports
`openenv`. Anything that takes an ASGI import string points at `notes.openenv_app:app`.

### The command

```sh
seahaven serve
seahaven serve --host 127.0.0.1 --port 9000
```

| Option | Default | What it does |
|---|---|---|
| `--host` | `0.0.0.0` | the address to bind. A container serves on the network it was given; pass `--host 127.0.0.1` for loopback |
| `--port` | `8000` | the port to bind |
| `--max_concurrent_envs` | `500` | how many sessions may be open at once. Over capacity, OpenEnv answers `CAPACITY_REACHED` and closes the connection |
| `--concurrency` | `min(cpus, 16)` | how many tool calls run at once; `0` for no gate |
| `--session-timeout` | `3600` | seconds of idleness before a session is reaped; `0` disables the reaper |
| `--include-control-tools` | off | make the control tools callable over the wire |
| `--world module:attr` | the convention | which world to serve |

`--max_concurrent_envs` is spelled with underscores because that is OpenEnv's own option name, and a
second spelling here would be one more thing to translate.

If the command fails on a release candidate of CPython 3.14, that is expected and there are two
separate breakages under it. On 3.14.0rc2 Seahaven does not import at all, because pydantic cannot
evaluate its forward references there. The extra would also meet a second one on its own: 3.14.0rc2
has no `collections.abc.ByteString`, and `beartype` asks for that name unguarded. Both are gone on
3.14.0 final, where the extra installs and works unpatched.

One process serves one world, and `serve` always runs a single worker. A session's instance,
connections and working directory are in-process state, so a second worker would answer a session's
second frame with an environment that has never seen its first.
[Running it in production](#running-it-in-production) covers scaling out.

## Sessions and instances

A WebSocket connection is one session, and one session holds one instance.

- **`reset(fixture=..., seed=..., **startup_kwargs)`** creates the instance. `reset()` with no
  fixture creates a blank instance from the schema, whose clock is wall time unless `now=` says
  otherwise. Passing `now=` together with a fixture is refused, because the fixture carries its own
  clock. Everything is passed straight to `world.instance(...)`, so the rules are the ones you
  already know from running in process.
- **A second `reset`** destroys the current instance before making the new one, so a session never
  holds two. If creation then fails, the session is left exactly as a fresh one — no instance, no
  episode, no steps — and is open for another `reset`.
- **Closing the connection** destroys the instance. A dropped client costs nothing once it is
  reaped.

`reset` takes what `world.instance(...)` takes, because it *is* `world.instance(...)`:

| Argument | What it does |
|---|---|
| `fixture=` | the frozen starting state to copy. Omit it for a blank instance, built from the world's schema |
| `seed=` | the seed behind `ctx.ids`, and behind SQL's `random()` and `randomblob()` |
| `now=` | the clock, for a blank instance only. A fixture carries its own, and `now=` with one is refused |
| `episode_id=` | your own id for the episode, echoed back on `state` so a trajectory ties to your run |
| anything else | passed to the world's startup hooks, so a world can be set up per episode |

`reset` is therefore where a run is customised. One served world covers every scenario a fixture and
a startup hook can express.

Listing the tools does not need a `reset`. The tool list belongs to the world rather than to the
episode, so a client may ask for it before it starts.

## Driving a world from Python

`seahaven.openenv.SeahavenClient` drives every Seahaven world. Every world speaks the same wire
shape, so there is nothing per-world to generate.

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

Every call is awaitable in asynchronous code and direct in synchronous code. That is the stock
OpenEnv client's behaviour rather than an addition:

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

Hundreds of these run against one server. A harness opens one connection per rollout and closes it
when the rollout ends. A dropped client costs nothing: the session is destroyed and its copy of the
fixture goes with it.

The tool name is positional-only, so a world is free to have a tool argument called `tool`. The
stock OpenEnv client works too. `SeahavenClient` adds a typed observation and two conveniences, not
a different protocol.

## Calls, results and errors

A tool call answers an observation with `done=False`, `reward=None`, and exactly one of:

```json
{"result": {"key": "ENG-12", "title": "The session cookie leaks a stack trace"}}
{"error": {"code": "NOT_FOUND", "message": "issue ENG-99 not found", "details": {"kind": "issue"}}}
```

**A tool error travels on `error`, as data.** The agent reads it and acts on it. It never closes the
session, and it has one shape across every world: the same dict `ToolError.to_dict()` gives in
process. Read `observation.error` and expect `{"code", "message", "details"}`.

Only a framework or protocol failure raises in the client, as `RuntimeError`.

Two other outcomes are worth knowing about:

- A **`WorldBug`** is not turned into an observation. It propagates and fails the frame loudly,
  because an eval that scored a run while the world was broken is the failure this design exists to
  prevent. The author sees the bug instead.
- An **unexpected Python exception** inside a tool is logged with its traceback and answered with a
  fixed `{"code": "internal", "message": "internal error"}`, so engine text cannot reach an agent
  even from a world with no error handler.

The `state` message answers `episode_id`, `step_count`, `fixture`, `now`, `world` and `composition`.
Every step counts, including one that was refused: the count is of what the session asked for.
`composition` is the session's stores, one record per node of a world that adds other worlds, and
`null` before the first `reset` ([composition.md](composition.md)). No observation carries any of
it. `state` is the eval's, never the agent's.

The environment's metadata is the world's `name` and `version`. Its README is the world's top-level
`README.md`, the one beside `pyproject.toml`, published whole as the card a hub shows. The one-line
description beside it comes from `World(description=...)` and nothing else. A world that gives none,
or gives a blank string, publishes `Seahaven world <name>`, which is a fallback rather than a wrong
sentence — so a world worth serving should say something better.

```py
world = seahaven.World(
    name="projecttracker",
    version="1.0.0",
    schema=seahaven.sql_files(__package__, "schema"),
    description=(
        "A Seahaven world: a fictional issue tracker for a fictional company, and the reference "
        "world the framework is developed against. Nothing here mimics a real product's names, "
        "schema or error text."
    ),
    state_format="seahaven.state/1",
)
```

## Grading a run

An eval grades the state the run left behind, and that state is a **changeset**: the net difference
between the fixture and the database as the agent left it. Each record is `{world, table, op, key,
before, after}`, where `op` is `insert`, `update` or `delete` and `world` is the node the row
belongs to (`main` for a world that adds none). [concepts.md](concepts.md) has the full semantics.

In process, `inst.changes()` is that changeset and needs nothing else. Over a server, an eval reads
it through a **control tool**: a tool the eval calls on its own session, rather than a second API.

Tools are the right shape for this because of what a session is. An instance is reached through the
connection that made it, so a read has to travel on that connection to be a read of that instance. A
separate control plane would need its own listener, its own token and a way to name an instance from
outside the session that owns it, and none of those exist here. Seahaven registers two control tools
on every world:

| Tool | What it does |
|---|---|
| `controller_run_sql(sql, params=None)` | one statement on a read-only connection of the instance's own: every table, the instance clock, no caps, SQLite's own error text. Same result shape as the `run_sql` helper |
| `controller_changes()` | the changeset, as JSON |

They are thin wrappers over the instance and own no SQL and no rendering of their own.
`controller_changes` is `inst.changes()`. `controller_run_sql` reads through the instance's own
read-only control connection rather than through the `inspect()` handle: same file, same read-only
opener, a second connection, because a control read borrows connection-level state for the length of
a statement while `inspect()` is the handle you read through on any thread you like.

Their arguments are validated like any tool's, so a bad `sql` argument is an `ArgumentError`.
Nothing else about a normal call applies: no middleware, no error handler, no transaction and no
concurrency gate, because an eval wants the real message.

**Over a server they exist only with `--include-control-tools`.**

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

Without the flag, calling one is `UnknownTool` in exactly the words an unregistered name earns, so
an agent cannot tell the two apart and a hub deployment does not expose them. They never appear in
the tool list, flag or not. In process, `inst.call("controller_changes")` always reaches them, and
`inst.tools()` never lists them.

## Why there are no rewards

**A Seahaven observation carries no reward.** `reward` is always `null` and `done` is always
`false`. The environment never ends an episode and never scores one. That is deliberate, and it is
the design decision most worth understanding before you build on this.

Many RL environments model a world where reward is easy to state. A game has a win, a score and a
terminal state, and the environment is the natural place to compute them. Seahaven models the other
kind: a stateful system with no built-in idea of winning, such as a CRM, a payment ledger or an
issue tracker. **There is no universal reward signal for a world like that.** Whether a final state
is good depends entirely on what the agent was asked to do in that session. The same database, with
the same three issues closed and one contact created, is a success for one scenario and a failure
for the next. Whether the session is finished is a decision of the caller for the same reason: the
environment cannot know what finishing looks like.

The thing that does know the goal is the eval or RL framework driving the episode. So Seahaven gives
it the material to judge with — the complete, net difference of what the agent changed — and stays
out of the judging.

The payoff is reuse. A world with no opinion about reward is a world you build once. Build `MyCorp`,
freeze the fixture `BigClient`, and then write hundreds of scenarios against that pair, each with
its own goal and its own grader. A world that computed a reward would have baked one scenario's goal
into the environment, and the next scenario would need a new world.

## The wire protocol

`SeahavenClient` is a convenience. The protocol is OpenEnv's, and a client in any language needs
only these frames on `ws://host:port/ws`.

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

A tool call and its two possible answers, in full:

```json
{"type": "step", "data": {"type": "call_tool", "tool_name": "get_issue", "arguments": {"key": "ENG-12"}}}

{"type": "observation", "data": {"observation": {"tool_name": "get_issue", "result": {"key": "ENG-12", "title": "The session cookie leaks a stack trace"}, "error": null}, "reward": null, "done": false}}

{"type": "observation", "data": {"observation": {"tool_name": "get_issue", "result": null, "error": {"code": "NOT_FOUND", "message": "issue ENG-99 not found", "details": {"kind": "issue"}}}, "reward": null, "done": false}}
```

The `error` *frame* is OpenEnv's own, and its `code` is one of `INVALID_JSON`, `UNKNOWN_TYPE`,
`VALIDATION_ERROR`, `EXECUTION_ERROR`, `CAPACITY_REACHED`, `FACTORY_ERROR` or `SESSION_ERROR`. It
means the frame or the session failed: a malformed action, a server at capacity, a world that raised
a `WorldBug`. It is never how a tool reports that an issue does not exist.

The action type for a tool call is OpenEnv's `CallToolAction`, and the server also answers
`ListToolsAction`, which returns every registered tool as `{name, description, input_schema}` —
OpenEnv's own `Tool` shape. Control tools are never in that list.

### One thing to plan for if you write your own client

OpenEnv's docstrings say `error` is only for transport failures, but its own implementation uses it
for tool errors too: `MCPEnvironment` answers a failed tool call with
`ToolErrorType.EXECUTION_ERROR`, "tool ran but failed". Seahaven follows the convention their code
establishes rather than the one their docstrings describe.

The shape does differ. OpenEnv types the field as its own `ToolError`, which is `{error_type,
message}` with extra keys forbidden, so a `code` and a `details` do not fit in it. A client that
validates a frame into that model will reject a tool error. Successful results are interoperable
everywhere. For errors, parse `error` leniently, or use `SeahavenClient`, whose observation model is
this shape.

## The concurrency gate

The gate bounds how many tool calls run at once. It never bounds admission: calls queue, and nothing
is rejected. A call takes the gate before the instance lock, so a queued call cannot block a
`destroy` or a `freeze`. Instance creation, tool listing and the control tools bypass it entirely.

Its default follows the process's CPU affinity, so it respects a container's limit rather than the
host's core count.

**The gate is unfair whenever it binds, and the default is not exempt.** It is a
`threading.BoundedSemaphore`, and a semaphore is not a queue. A thread that releases a slot and
immediately asks for another usually wins the race against the waiter that was just woken, because
the waiter needs the GIL to make progress and the barging thread already has it. In the framework's
own benchmark, with five threads calling and the gate at 1, 2 or 4, one three-second window served
its worst-served session **once** while another session in that same window was served thousands of
times: 12,874 at a gate of 1, and 5,351 and 4,011 at 2 and 4. At a gate size above the number of
threads offered, so that the gate never binds, every session got an even share. Every gate size that
binds behaves this way, and a server with 500 sessions and a gate of 16 is the ordinary case rather
than an edge one.

Nothing is dropped, so the promise that calls queue is kept to the letter. But a call that queues
for seconds behind a thread barging in front of it is not the service that promise implies, and a
run whose session is the unlucky one will time out. The fix is a gate that hands slots out in
arrival order, not a different number.

What to do meanwhile, for a workload that is saturated and cares about the slowest session:
`--concurrency 0` was the one setting measured that served every session evenly, and at 32 sessions
it matched or beat the default on throughput while cutting the worst observed wait by an order of
magnitude. It pays for that in median and 95th-percentile latency. The measurements, with the
caveats they need — one machine, one afternoon, a closed loop with no think time — are in
`bench/results/latest.md` in the Seahaven repository. They are not a service-level objective, they
are not a capacity model, and no number from them should be quoted as a property of the framework.

**The gate is not a serving feature.** It is process-wide and on by default in *any* process that
calls a tool, including an in-process eval harness driving instances on threads. `serve` only gives
it a flag. A harness resizes it with `seahaven.instances.set_concurrency(n)`, and `0` removes it,
which is the same call `--concurrency` makes. It meets the same unfairness when it binds.

Nothing else in the framework bounds a call. A world's own code runs until it returns, and
containment exists only for agent-written SQL.

## Running it in production

**Scaling out is more processes.** One process serves one world on one worker. Several worlds means
several processes. Scaling one world out means more processes behind a load balancer with connection
affinity, which is the operator's business. One world is not one *package*, though: a world that
adds other worlds serves their tools as part of its own surface, so a composite world is still one
environment on the wire ([composition.md](composition.md)).

**The idle reaper matters.** A held session costs its fixture copy on disk and about a megabyte of
memory, and a client that drops without closing holds one for ever. An hour is long enough that no
live eval is reaped and short enough that a crashed harness does not accumulate instances.

**A disconnect is not an error in the log.** OpenEnv's WebSocket handler closes the connection from
its own side after the peer has usually already gone, and lets the `WebSocketDisconnect` escape into
uvicorn's ASGI error path — an `ERROR: Exception in ASGI application` and a full traceback for a
session that ended perfectly normally. Seahaven closes both halves of that. `SeahavenClient` asks
the server to close and waits for it to, so the handshake finishes and nothing is raised at all. The
app also carries ASGI middleware that absorbs a `WebSocketDisconnect` escaping a WebSocket route,
which covers every client Seahaven does not ship: a stock `GenericEnvClient`, a raw socket, a
harness that dies mid-session. An absorbed disconnect is one `DEBUG` line on the `seahaven.openenv`
logger, so it can still be found. The price is that a `WebSocketDisconnect` reaching the top of a
WebSocket connection is never reported as a server error, which is the right trade: it only ever
means the peer went away.

**A dropped connection is a lost episode, so `SeahavenClient` waits longer before calling one
dead.** A session is one connection holding one instance, and there is no resume: any disconnect
destroys the instance, and reconnecting builds a new one. A server that stalls at the event-loop
level for longer than the keepalive timeout would lose every episode on the box at once.
`SeahavenClient` therefore defaults its WebSocket ping *timeout* to 120 seconds where OpenEnv
defaults to 20, and keeps the ping interval at OpenEnv's 20. That is six times the tolerance for a
stall without pinging any less often. The price runs the other way: a genuinely dead server or a
severed network takes up to two minutes to notice instead of twenty seconds. That is the right trade
for an eval or an RL rollout and the wrong one for a short interactive session, which is why it is a
default rather than a fixed value. Pass `websocket_ping_timeout_s=` to choose your own. A client
Seahaven does not ship keeps OpenEnv's 20 seconds.

## Publishing to a hub

A world can be published as an OpenEnv environment — a Docker image or a Hugging Face Space — and
driven by anyone with an OpenEnv client.

`seahaven new --hub` adds the five files `openenv push` validates a directory for: `openenv.yaml`, a
root `Dockerfile`, a root `__init__.py`, `client.py` and `models.py`. It adds nothing else, and a
world that does not publish to a hub carries none of them. `client.py` is a single re-export,
because the typed client for every Seahaven world is `SeahavenClient`.

**That `Dockerfile` does not build a working image today.** Its build step is `RUN uv sync --extra
serve`, and the world's `serve` extra is `seahaven[serve]`, which resolves to the placeholder
release described at the top of this page. The image builds, and the container cannot start: there
is no `seahaven.openenv` in it. Until publication, an image has to get the framework from a checkout
or a private index, which means editing that `RUN` line.

**The image is a checkout, and has to stay one.** The generated `Dockerfile` does `COPY . /app` and
then `uv sync`, so the container holds the world's whole directory with the framework installed into
its environment. Replace those two lines with a plain install of the world and the image builds, the
server starts, every `reset()` with no fixture works, and every `reset(fixture=...)` fails, because
`fixtures/` is outside the package and is not in a wheel.
[db_schema_and_fixtures.md](db_schema_and_fixtures.md) has the reason, the exact error, and
`World(fixtures_dir=...)` for a deployment that has to put the directory somewhere else.

One catch if you take the `fixtures_dir=` route: the README the environment publishes is looked for
*beside the fixtures directory*, because that is the project root `World` derives when it is left to
find `fixtures/` itself. Move the fixtures somewhere with no `README.md` next to them and the card
is empty. The one-line description is unaffected, since that is `World(description=...)` and travels
with the world rather than with the directory. Put the world's `README.md` beside the directory you
named, or leave `fixtures_dir` alone and ship `fixtures/` where it was.

Seahaven's own reference world is not published anywhere. That step is gated on a maintainer's
sign-off and has not happened.

## Evaluating with Kiln

[Kiln](https://kiln.tech) connects to a Seahaven world as an OpenEnv client, which is the whole
integration: point it at a served world and it drives the sessions.

Kiln is where the goal that Seahaven deliberately does not hold gets written down. Define a scenario
against a world and a fixture, run it as an [eval](https://kiln.tech/features/evals) and grade the
final state against what the scenario asked for, then
[auto-optimize](https://kiln.tech/features/auto-optimize) the agent — prompts, models, fine-tuning —
against that eval. Same world, same fixture, as many scenarios as the job needs.

Seahaven is built by the Kiln AI team.

## Known problems in OpenEnv

These are real and reproduced, and every one of them is OpenEnv's rather than Seahaven's. None of
them is in the WebSocket path an eval and `SeahavenClient` use.

### `POST /reset`, `POST /step` and `GET /state` are refused with a `501`

OpenEnv builds a brand-new environment inside each of those three handlers and closes it again
before replying, so no two requests ever share one. `/reset` resets one instance, `/step` steps a
different one, `/state` reads a third, and none of them observes the others. Nothing errors:
upstream answers a well-formed `200` describing an environment that is already gone, which is the
worst way for an endpoint to be wrong. Seahaven replaces those three handlers with one that says so.
The body is FastAPI's `{"detail": ...}` envelope wrapped around the `{"code", "message", "details"}`
triple the rest of the framework uses:

```json
{
  "detail": {
    "code": "http_episode_control_unsupported",
    "message": "GET /state cannot hold an episode, so Seahaven refuses it ...",
    "details": {
      "route": "GET /state",
      "use_instead": "/ws",
      "clients": ["seahaven.openenv.SeahavenClient", "openenv.EnvClient"],
      "upstream": {
        "package": "openenv 0.4.2",
        "file": "openenv/core/env_server/http_server.py",
        "regression": "86a222d",
        "defect": "each handler builds an Environment from the factory and closes it ..."
      }
    }
  }
}
```

The three paths stay in the published OpenAPI schema on purpose. `openenv push` decides what kind of
environment a world is by reading path *names*: an app that publishes `/reset` is a simulation
environment and must publish `/step` and `/state` beside it, and an app that publishes none of the
three is a production environment. Deleting them would pass that check while declaring a Seahaven
world to be something it is not, so only the behaviour changes. This is local protection and not a
fix: the defect is upstream's, is unfixed there, and a world built on a stock OpenEnv server still
has it. Drive episodes over `/ws`.
([huggingface/OpenEnv#1156](https://github.com/huggingface/OpenEnv/issues/1156).)

### `POST /mcp` and `ws /mcp` are refused with a JSON-RPC error

**There is no MCP server here.** An OpenEnv app publishes a `/mcp` endpoint, but it is not the MCP
protocol. It dispatches exactly four methods — `openenv/session/create`, `openenv/session/close`,
`tools/list` and `tools/call` — and has no `initialize`, no capability negotiation, no
notifications, no SSE, no `Mcp-Session-Id`, no resources and no prompts. An off-the-shelf MCP client
(Claude Desktop, Cursor, the `mcp` and `fastmcp` SDKs) opens with `initialize`, gets `-32601 Method
not found: initialize`, and never gets further. Upstream says this is deliberate and temporary: its
RFC 003 leans on MCP's custom-transports clause, lists no SSE streaming, no server-initiated
messages and no session management as known gaps, and plans standard Streamable HTTP later.

Underneath that, every door on it is dead for one reason: the dialect has no `reset`, and a Seahaven
tool call needs an instance. Left alone, `tools/list` would succeed and advertise every tool the
world has, and then every `tools/call` behind it would answer `reset first` — with or without an
`openenv/session/create` session id, and over the WebSocket exactly as over `POST`. That is a
well-formed answer about nothing, which is what the three HTTP routes above are refused for. So all
four methods are refused, on both transports, in one statement.

**The refusal is an HTTP `200`, not the `501` above**, because `openenv push` probes this exact
route: `mcp_endpoint` in `openenv/cli/_validation.py` POSTs `{}` to `/mcp` and passes only on a
`200` whose JSON body has `"jsonrpc": "2.0"`. A `501` would fail a push that has nothing wrong with
it. The refusal therefore travels in the JSON-RPC envelope, where a JSON-RPC caller looks for it
anyway: error code `-32601`, whose definition is "method does not exist / *is not available*", with
the framework's `{"code", "message", "details"}` triple in `data`. `ws /mcp` answers the same frame
and then closes normally, because every method is refused, so a second frame could only earn the
same answer. `/mcp` stays in the published OpenAPI schema for the same reason the three paths above
do.

**Seahaven will not add MCP support until the standard supports stateful servers.** Seahaven exists
to build stateful MCP-shaped servers, where the session is what matters: an instance is a session,
and two sessions must not see each other's writes. Support would have to come without mutating the
tool interface and without passing a non-standard session id alongside every call.

**`/ws` is the agent-facing transport, which is a deliberate divergence from OpenEnv's advice.**
OpenEnv's own lifecycle guide says `/ws` "is not an agent-facing interface … must not be given
directly to agents" and points agents at `/mcp` instead. Seahaven inverts that on purpose: a
Seahaven episode needs a `reset`, the MCP dialect has no verb for one, and a transport an agent
cannot start an episode on is not an agent-facing interface either. This is not the end of MCP
frames — a `{"type": "mcp"}` message on a `/ws` connection reaches the same upstream handler with
the *session's* environment, and works correctly once the session has been reset. Anything built on
`openenv/session/create` would be thrown away the day upstream ships real Streamable HTTP; a world
reached over `/ws` would not.

### `GET /schema` publishes the base state model

A client never sees the state shape it is driving: `create_app` takes an action class and an
observation class and no state class, and the route answers `State.model_json_schema()` for every
environment. ([huggingface/OpenEnv#1155](https://github.com/huggingface/OpenEnv/issues/1155).)
