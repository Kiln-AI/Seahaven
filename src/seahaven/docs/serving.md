# Serving a world

Seahaven's remote lifecycle and transport are [OpenEnv](https://github.com/meta-pytorch/OpenEnv)
(v0.4.x). There is no other remote API for the agent side: a world is either driven in process
through `world.instance(...)`, or over OpenEnv.

The server and the client live in Seahaven's `serve` extra, because `openenv`'s dependency tree is
large and a world used in process should not pay for it.

**Do not `pip install "seahaven[serve]"`.** The framework is not published: the `seahaven` name on
PyPI currently holds a placeholder release that contains none of this and has no `serve` extra, so
that command succeeds and installs nothing useful, which is worse than failing. Until publication,
install the framework and its extra from a checkout of the Seahaven repository — for example
`uv pip install -e "/path/to/Seahaven[serve]"` into the environment your world runs in. That
environment has to be a **final** CPython 3.14 or newer and not a release candidate. On 3.14.0rc2
Seahaven does not import at all, before any of this: `pydantic` cannot evaluate its forward
references there. Under that there is a second breakage the extra would meet on its own, since
3.14.0rc2 has no `collections.abc.ByteString` and `beartype` — which `from fastmcp import Client`
reaches, and OpenEnv's MCP environment imports — asks for that name unguarded. Both are gone on
3.14.0 final, where the extra installs and works unpatched.

A world's whole server is one file, which `seahaven new` writes:

```py
# src/notes/openenv_app.py
import seahaven.openenv

from notes import world

app = seahaven.openenv.app(world)
```

It is a module of its own, and not part of the package's `__init__`, because importing it needs the
extra: a world used in process — in pytest, in a script, in a notebook — never imports `openenv`.
Anything that takes an ASGI import string points at `notes.openenv_app:app`.

And one command:

```sh
seahaven serve
seahaven serve --port 9000 --concurrency 0
```

One world per server process, many sessions. That is the standard OpenEnv shape and the one hubs
expect — one environment per image or Space. There is no way to serve several worlds from one
process, and `serve` always runs a single worker: a session's instance, connections and working
directory are in-process state, so a second worker would answer a session's second frame with an
environment that has never seen its first. Scaling out is more processes behind a load balancer with
connection affinity, which is the operator's business.

## Instance = session

A WebSocket connection is one session, and one session holds one instance.

- **`reset(fixture=..., seed=..., **startup_kwargs)`** creates it. `reset()` with no fixture creates
  a blank instance from the DDL, whose clock is wall time unless `now=` says otherwise; `now=`
  together with a fixture is refused, because the fixture carries the clock. Everything is passed
  straight to `world.instance(...)`, so the rules are the same ones in process.
- **A second `reset`** destroys the current instance before making the new one, so a session never
  holds two. If creation then fails, the session is left exactly as a fresh one — no instance, no
  episode, no steps — and is open for another `reset`.
- **Closing the connection** destroys the instance. A dropped client costs nothing once it is
  reaped.

## Calls and their answers

The action type is OpenEnv's `CallToolAction`; the server also answers `ListToolsAction`.

`ListToolsAction` returns every registered tool as `{name, description, input_schema}` — OpenEnv's
own `Tool` shape — and does not need a `reset`, because a tool list is the world's and not an
episode's. Control tools are never in it.

`CallToolAction(tool_name, arguments)` runs the call and answers an observation with `done=False`,
`reward=None`, and exactly one of:

```json
{"result": {"key": "ENG-12", "title": "The session cookie leaks a stack trace"}}
{"error": {"code": "NOT_FOUND", "message": "issue ENG-99 not found", "details": {"kind": "issue"}}}
```

**A tool error travels on `error`, as data.** This diverges from OpenEnv's convention, which reserves
`error` for transport failures and puts a tool's error inside `result`. Seahaven takes the divergence
deliberately: a tool error is something the agent reads and acts on, it must never close the session,
and one shape for it across every world — the same dict `ToolError.to_dict()` gives in process — is
worth more than the convention. Evals should read `observation.error` and expect `{"code",
"message", "details"}`.

What does *not* become an observation is a `WorldBug`: it propagates and fails the frame loudly,
because an eval that scored a run while the world was broken is the failure this design exists to
prevent. An unexpected Python exception inside a tool is logged with its traceback and answered with
a fixed `{"code": "internal", "message": "internal error"}`, so engine text cannot reach an agent
even from a world with no error handler.

The `state` message answers the state document, plus OpenEnv's own `step_count` — `episode_id` is
the document's own envelope field. Every step counts towards `step_count`, including one that was
refused and a tool listing: the count is of what the session asked for, which is why it is not the
document's `call_count`.

Metadata is the world's `name` and `version`, and the environment's README is the world's top-level
`README.md`, the one beside `pyproject.toml`, published whole as the card a hub shows. The one-line
description beside it is `World(description=...)` and nothing else: an explicit sentence about the
world, written where the world is defined. A world that gives none — or gives a string that is
blank — publishes `Seahaven world <name>`, which is a fallback rather than a wrong sentence, so a
world worth serving should say something better.

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

## The client

`seahaven.openenv.SeahavenClient` is one client for every Seahaven world: every world speaks the
same wire shape, so there is nothing per-world to generate.

```py
from seahaven.openenv import SeahavenClient

with SeahavenClient(base_url="http://127.0.0.1:8000") as env:
    env.reset(fixture="small_startup", seed=7)

    tools = env.list_tools()  # [{"name", "description", "input_schema"}, ...]
    observation = env.call("get_issue", key="ENG-12")
    if observation.error is None:
        print(observation.result["title"])
    else:
        print(observation.error["code"], observation.error["message"])

    state = env.state()
    print(state.now)  # the instance's frozen instant
    print(state.state["db"]["log"])  # what the format produced; here, the change log
```

`call` and `list_tools` are awaitable in asynchronous code and direct in synchronous code, like the
rest of OpenEnv's client. Only a framework or protocol failure raises, as `RuntimeError`; a tool
error arrives on the observation. The tool name is positional-only, so a world is free to have a
tool argument called `tool`.

The stock OpenEnv client works too — `SeahavenClient` adds the typed observation and the two
conveniences, not a different protocol.

## Reading the instance

An eval reads what the episode left behind from the `state` message, which answers the **state
document**: the framework's provenance envelope, and `state` from the format this episode is
running in. Over OpenEnv that is `SeahavenClient.state()`; in process it is `inst.state()`. The
page for the document itself — both built-in formats, registering your own, the change log and how
to grade on it — is [state.md](state.md).

Two things are true only over the wire:

- **`SeahavenState` is the document plus `step_count`, and nothing else.** `episode_id` is the
  document's own envelope field and lands on the base model's field of that name; `step_count` is
  the only thing OpenEnv adds. So `state().state` is the format's output, and
  `state().model_dump(exclude={"step_count"})` is exactly the dict `inst.state()` answers in
  process — which is what a harness saves as its `final_state`.
- **The format is chosen at `reset`, and nowhere else.** `reset(state_format="…")` overrides the
  world's pin for that episode; the `state` message itself carries no arguments, so the in-process
  `inst.state(format=…)` has no counterpart here. An unregistered name is refused before anything
  is copied, as `EXECUTION_ERROR`, and the session stays open.

A read the document does not answer — arbitrary inspection SQL against a live episode — is what
`--include-control-tools` is for. It is deprecated, undocumented beyond the flag's line in
[reference/cli.md](reference/cli.md), never listed in the tool list, off unless the operator asks
for it, and `state` is what an eval reads now.

## Operator options

| Option | Default | What it does |
|---|---|---|
| `--host` | `0.0.0.0` | the address to bind. A container serves on the network it was given; `--host 127.0.0.1` for loopback |
| `--port` | `8000` | the port to bind |
| `--max_concurrent_envs` | `500` | how many sessions may be open at once. Over capacity, OpenEnv answers `CAPACITY_REACHED` and closes the connection |
| `--concurrency` | `min(cpus, 16)` | how many tool calls execute at once; `0` for no gate |
| `--session-timeout` | `3600` | seconds of idleness before a session is reaped; `0` disables the reaper |
| `--include-control-tools` | off | make the deprecated control tool callable over the wire; it is never listed |
| `--world module:attr` | the convention | which world to serve |

**The idle reaper matters.** A held session costs its fixture copy on disk and about a megabyte of
memory, and a client that drops without closing holds one for ever. An hour is long enough that no
live eval is reaped and short enough that a crashed harness does not accumulate instances.

### The concurrency gate, and what is wrong with it

The gate bounds how many tool calls execute at once. It never bounds admission: calls queue, and
nothing is rejected. A call takes the gate before the instance lock, so a queued call cannot block a
`destroy` or a `freeze`; instance creation, tool listing and the control tool bypass it entirely.

Its default follows the process's CPU affinity, which respects a container's limit rather than the
host's core count.

**The gate is unfair, and the default is not exempt.** It is a `threading.BoundedSemaphore`, and a
semaphore is not a queue: a thread that releases a slot and immediately asks for another usually wins
the race against the waiter that was just woken, because the waiter needs the GIL to make progress
and the barging thread already has it. In the framework's own benchmark, with five threads calling
and the gate at 1, 2 or 4, one three-second window served its worst-served session **once** while
another session, in that same window, was served thousands of times (12,874 at a gate of 1; 5,351 and
4,011 at 2 and 4). At a gate size above the number of threads offered — so the gate never binds —
every session got an even share. **Every gate size that binds does this**, and a server with 500
sessions and a gate of 16 is the ordinary case rather than an edge one.

Nothing is dropped: the promise that calls queue is kept to the letter. But a call that queues for
seconds behind a thread barging in front of it is not the service that promise implies, and an
episode whose session is the unlucky one will time out. This is `BACKLOG.md` B20 in the Seahaven
repository, and the fix is a gate that hands slots out in arrival order rather than a different
number.

**The gate is not a serving feature.** It is process-wide and on by default in *any* process that
calls a tool, an in-process eval harness driving instances on threads included; `serve` only gives it
a flag. A harness in that position resizes it with `seahaven.instances.set_concurrency(n)` — `0`
removes it — which is the same call `--concurrency` makes, and it meets the same unfairness when it
binds.

What to do meanwhile, for a workload that is saturated and cares about the slowest session:
`--concurrency 0` was the one setting measured that served every session evenly, and at 32 sessions
it matched or beat the default on throughput while cutting the worst observed wait by an order of
magnitude. It pays for that in median and 95th-percentile latency. The measurements, with the
caveats they need — one machine, one afternoon, a closed loop with no think time — are in
`bench/results/latest.md` in the Seahaven repository. They are not a service-level objective, they
are not a capacity model, and no number from them should be quoted as a property of the framework.

Nothing else in the framework bounds a call. A world's own code runs until it returns; containment
exists only for agent-written SQL.

## Publishing to a hub

`seahaven new --hub` adds the five files `openenv push` validates a directory for — `openenv.yaml`,
a root `Dockerfile`, a root `__init__.py`, `client.py` and `models.py` — and nothing else. A world
that does not publish to a hub carries none of them. `client.py` is a single re-export, because the
typed client for every Seahaven world is `SeahavenClient`.

**That `Dockerfile` does not build a working image today.** Its build step is `RUN uv sync --extra
serve`, and the world's `serve` extra is `seahaven[serve]` — which, while Seahaven is unpublished,
resolves to the placeholder release described at the top of this page. The image builds, and the
container cannot start: there is no `seahaven.openenv` in it. Until publication, an image has to get
the framework from a checkout or a private index, which means editing that `RUN` line.

**The image is a checkout, and has to stay one.** A world is deployed by checking it out, never by
installing it: that `Dockerfile` does `COPY . /app` and then `uv sync`, so the container holds the
world's whole directory — `pyproject.toml`, `src/`, `fixtures/` — with the framework installed into
its environment. Replace those two lines with a plain install of the world (`pip install .`,
`pip install <world>`, a wheel built elsewhere) and the image builds, the server starts, every
`reset()` with no fixture works, and every `reset(fixture=...)` fails: `fixtures/` is outside the
package and is not in a wheel. [fixtures.md](fixtures.md) has why, the exact error, and
`World(fixtures_dir=...)` for a deployment that has to put the directory somewhere else.

One catch if you take the `fixtures_dir=` route: the README the environment publishes is looked for
*beside the fixtures directory*, because that is the same project root `World` derives when it is
left to find `fixtures/` itself. Move the fixtures somewhere with no `README.md` next to them and the
card is empty — the one-line description is unaffected, since that is `World(description=...)` and
travels with the world rather than with the directory. Put the world's `README.md` beside the
directory you named, or leave `fixtures_dir` alone and ship `fixtures/` where it was.

Seahaven's own reference world is not published anywhere; that step is gated on a maintainer's
sign-off and has not happened.

## Rough edges worth knowing before you meet them

These are real, reproduced, and recorded in the Seahaven repository's `BACKLOG.md`. None of them is
in the WebSocket path an eval and `SeahavenClient` use.

- **`GET /state` over HTTP answers the base model.** OpenEnv annotates that route with its own
  `State` type, so `fixture`, `now` and `world` are stripped, and the route is not session-bound
  either — the numbers it does return are a fresh environment's. Read state over the WebSocket.
  (B13.)
- **`GET /schema` publishes the base state model** for the same reason, so a client never sees the
  state shape it is driving. (B13.)
- **Every clean client disconnect logs `ERROR: Exception in ASGI application` with a traceback.**
  OpenEnv closes a socket the client has already closed. The sessions are fine; the log is noisy.
  (B13.)
