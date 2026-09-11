---
status: complete
---

# Component: the OpenEnv environment and `seahaven serve`

Modules: `seahaven/openenv/env.py`, `openenv/__init__.py`, `openenv/serve.py`. Depends on
`openenv>=0.4.2,<0.5` through the `seahaven[serve]` extra.

## 1. Models (`env.py`)

```python
from openenv.core.env_server.types import Observation, State, EnvironmentMetadata
from openenv.core.env_server.mcp_types import CallToolAction, ListToolsAction, CallToolObservation, ListToolsObservation

class SeahavenObservation(CallToolObservation):        # subclassing keeps /mcp tools/call working
    tool_name: str = ""
    result: Any | None = None
    error: dict[str, Any] | None = None                # {"code", "message", "details"}
    # inherited: done: bool = False, reward: float | None = None, metadata: dict

class SeahavenState(State):                            # extra="allow" on the base
    fixture: str | None = None
    now: str | None = None
    world: str
```

`action_cls` is `CallToolAction` (a union breaks deserialisation, `/schema` and `/mcp`; the
server delivers `ListToolsAction` for `{"type": "list_tools"}` regardless).

Putting a tool error in `error` diverges from OpenEnv's convention, where `error` is a transport
failure and a tool's own error travels in `result`. Seahaven does it deliberately and the docs say
so: a tool error is data the agent reads, it must never close the session, and one shape for it
across every world is worth the divergence.

## 2. The environment class

```python
class SeahavenEnv(Environment):
    SUPPORTS_CONCURRENT_SESSIONS = True

    def __init__(self, world: World, *, include_control_tools: bool) -> None
    def reset(self, seed: int | None = None, episode_id: str | None = None, *,
              fixture: str | None = None, now: str | None = None,
              **startup_kwargs: Any) -> SeahavenObservation
    def step(self, action: Action, timeout_s: float | None = None, **kwargs: Any) -> Observation
    @property
    def state(self) -> SeahavenState
    def close(self) -> None
    def get_metadata(self) -> EnvironmentMetadata
```

- **`reset`.** If an instance exists on this session, destroy it first. Then
  `world.instance(fixture, seed=seed, now=now, **startup_kwargs)`: `fixture=None`
  (or omitted) is a blank instance from the DDL, clock at the wall time unless `now=` is given, the
  same rule as in-process; `now=` with a fixture is refused in V1 (the per-instance override is
  post-V1). Unknown
  startup kwargs raise from inside `world.instance` (before any copy); the exception propagates,
  OpenEnv sends an `EXECUTION_ERROR` frame and the session stays open for another `reset`.
  Returns `SeahavenObservation(result={"fixture": id or None, "now": clock.iso(), "tools": len(tools)})`.
- **`step`.** `ListToolsAction` → `ListToolsObservation(tools=instance.tools())` (checked first);
  control tools are never in that list. `CallToolAction` → if the named tool is a control tool and
  `include_control_tools` is off, raise `UnknownTool(name)` before dispatch, exactly as for an
  unregistered name; otherwise `instance.call(tool_name, **arguments)`. A `ToolError` is caught and
  rendered as `SeahavenObservation(tool_name, error=e.to_dict())`. Anything that is **not** a
  `SeahavenError` is caught too, logged at `ERROR` with its traceback, and rendered as a fixed
  generic observation (`{"code": "internal", "message": "internal error"}`), so engine text never
  reaches an agent even in a world with no error handler. A `WorldBug` is not caught: it propagates,
  OpenEnv sends an `EXECUTION_ERROR` frame, and the author sees their bug instead of the eval
  quietly grading against "internal error" responses — which is the whole point of the class
  (`functional_spec.md` §5.3). A `step` before `reset` raises `WorldBug("reset first")`.
  `done` is always `False`, `reward` always `None`. `timeout_s` is accepted and ignored: Seahaven
  does not bound a call.
- **`state`.** `SeahavenState(episode_id=self._episode_id, step_count=self._steps, fixture=...,
  now=..., world=world.name)`; before `reset`, `fixture` and `now` are `None`.
- **`close`.** Destroys the instance if any. Called by the server on disconnect, on the session's
  thread.
- **`get_metadata`.** `EnvironmentMetadata(name=world.name, version=world.version,
  description=first paragraph of README or f"Seahaven world {name}", readme_content=README text)`.
  The README is the world package's top-level `README.md`, located as `fixtures_dir.parent /
  "README.md"` (same root derivation as the fixtures directory); absent means empty.

The instance lives on the environment object; one environment object per session, so instance =
session by construction. `episode_id` is the one given to `reset` or a `uuid4`.

## 3. `app(world, ...)` (`openenv/__init__.py`)

```python
def app(world: World, *, include_control_tools: bool = False, max_concurrent_envs: int = 500,
        session_timeout: float | None = 3600.0) -> FastAPI:
    return create_app(
        functools.partial(SeahavenEnv, world, include_control_tools=include_control_tools),
        CallToolAction, SeahavenObservation, env_name=world.name,
        concurrency_config=ConcurrencyConfig(max_concurrent_envs=max_concurrent_envs,
                                             session_timeout=session_timeout))
```

Pass `concurrency_config` only (both arguments raise); `env_name` must be explicit for a
partial; `session_timeout=None` disables the idle reaper, and the default of 3600 seconds exists
because a dropped client otherwise holds its instance for ever (a held session costs its fixture
copy on disk plus about a megabyte of memory); `max_concurrent_envs > 1` requires the class
attribute, which is set. One world per app, at `/`, the shape hubs expect. A world's
`openenv_app.py` (scaffolded) is:

```python
from projecttracker import world
import seahaven.openenv
app = seahaven.openenv.app(world)
```

Over capacity, OpenEnv sends `CAPACITY_REACHED` and closes the connection. Seahaven never refuses
by design; the default of 500 and the CLI override are the operator's budget, and the docs say
so.

## 4. `seahaven serve` (`openenv/serve.py`, called by `cli/serve.py`)

```
seahaven serve [--world module:attr] [--host 0.0.0.0] [--port 8000]
               [--max_concurrent_envs 500] [--concurrency <min(cpus, 16)>] [--session-timeout 3600]
               [--include-control-tools]
```

1. Resolve the world (package convention or `--world`); set the concurrency gate to `--concurrency`
   (default `min(os.process_cpu_count() or 4, 16)`, `0` for no gate).
2. Build `app(world, include_control_tools=..., max_concurrent_envs=..., session_timeout=...)`.
3. `uvicorn.run(app, host, port, workers=1, log_level="info")`. One worker: sessions are in-process
   state; scaling is more processes behind a load balancer with connection affinity, which is the
   operator's business (hosting is out of scope).
4. Missing `openenv` (`ImportError` on `import seahaven.openenv`) prints
   `seahaven serve needs the serve extra: pip install "seahaven[serve]"` and exits 1.

`--session-timeout 0` disables the reaper: the CLI translates it to `session_timeout=None`, which
is what OpenEnv wants (it requires `> 0` or `None`).

## 5. The typed client (`openenv/client.py`)

Every Seahaven world speaks one wire shape, so one typed client serves them all:

```python
class SeahavenClient(EnvClient[CallToolAction | ListToolsAction, SeahavenObservation, SeahavenState]):
    def _step_payload(self, action) -> dict: return action.model_dump()
    def _parse_result(self, payload) -> StepResult[SeahavenObservation]: ...   # observation -> SeahavenObservation
    def _parse_state(self, payload) -> SeahavenState: ...
    # conveniences (sync and async follow the base client's dual mode)
    def list_tools(self) -> list[dict]: ...   # reads the raw step payload's `tools` list
    def call(self, tool: str, **arguments) -> SeahavenObservation: return self.step(CallToolAction(tool_name=tool, arguments=arguments)).observation
```

`_parse_result` is used only for `CallToolAction` results. `list_tools` does not go through
`SeahavenObservation`, which forbids extras and has no `tools` field; it reads the `tools` list
(a `list[dict]`) straight off the step payload.

`call` returns the observation; the caller reads `.result` or `.error`. It never raises on a tool
error (consistent with the stock client). This is the client the docs show and the reference
harness code uses. A per-world client with one typed method per tool, generated from the tool
registry, is a later release.

## 6. Packaging for `openenv push` (`seahaven new --hub`)

`openenv push` validates the pushed directory for `openenv.yaml` (only `name` is read),
`__init__.py`, `client.py`, `models.py`, `README.md`, `pyproject.toml` and a root `Dockerfile`; with
a root Dockerfile no `server/` directory is needed. These are OpenEnv's and Hugging Face's
requirements (a Space is a Docker container; the two modules are OpenEnv's repository convention).
`seahaven new --hub` writes them, so a world that publishes to a hub is push-clean and a world that
does not carries none of them. Each file opens with a comment stating why it exists and what it
re-exports:

```
openenv.yaml          # spec_version: 1, name: <world>, type: space, runtime: fastapi, port: 8000, version
Dockerfile            # FROM ghcr.io/huggingface/openenv-base:latest; uv sync; CMD uvicorn <pkg>.openenv_app:app --host 0.0.0.0 --port 8000
__init__.py           # "# Present because `openenv push` requires a package marker at the repository root."
client.py             # "# Required by `openenv push`. The typed client for every Seahaven world lives in seahaven; this world's client is it."
                      # from seahaven.openenv import SeahavenClient as Client
models.py             # "# Required by `openenv push`. A Seahaven world's action, observation and state models are the framework's."
                      # from seahaven.openenv import CallToolAction, ListToolsAction, SeahavenObservation, SeahavenState
src/<pkg>/openenv_app.py   # app = seahaven.openenv.app(world)
```

The re-exports are correct, not placeholders: the models and the client are the same for every
world by construction. `README.md` is the Space card and the metadata's `readme_content`.
`openenv validate` (a separate, optional check) still wants `server/app.py` and `uv.lock`; the
scaffold does not chase it.

## 7. Driving a world (for the docs)

```python
from seahaven.openenv import SeahavenClient

with SeahavenClient(base_url="http://127.0.0.1:8000") as env:
    env.reset(fixture="small_startup", seed=7)
    tools = env.list_tools()
    obs = env.call("get_issue", key="ENG-12")
    obs.result, obs.error
    env.state().now
```

The stock `GenericEnvClient` works identically with `step(CallToolAction(...))` and dict
observations. Tool errors arrive on the observation; only framework or protocol failures raise
`RuntimeError` on the client.

## 8. Test plan

- `test_env.py` (in-process, no server): `reset` creates an instance and a second `reset` destroys
  the first (directory gone); `reset` without `fixture` gives a blank instance at wall time, and
  with `now=` at that time; `now=` with a fixture raises; unknown startup kwarg raises before
  any directory exists; `step` before `reset` raises; `ListToolsAction` excludes control tools
  ever; a `CallToolAction` naming a control tool without the flag is an `unknown_tool` error
  observation and succeeds with it; `CallToolAction` success and `ToolError` rendering;
  `UnknownTool` rendering; an exception that is neither a `ToolError` nor a `SeahavenError` becomes
  the generic internal observation and is logged, while a `WorldBug` propagates out of `step`;
  `state` before and after `reset`; `close` destroys; `get_metadata`
  reads the README.
- `test_client.py`: `SeahavenClient` parses observations and state; `call` and `list_tools`; sync
  and async modes.
- `test_server.py` (uvicorn on a free port, `SeahavenClient` and the stock `GenericEnvClient`, sync
  and async): the section 7 flow end to end; two concurrent sessions are independent (a write in one is invisible in the
  other); with `--include-control-tools`, `controller_run_sql` is callable and never listed;
  without it, calling it is an `unknown_tool` error observation and it is never listed;
  an unknown reset kwarg surfaces as `RuntimeError` and the session accepts a following `reset`;
  the gate: with `concurrency=1` and two sessions calling a tool that sleeps inside SQLite (a slow
  recursive CTE), the calls serialise; 500 sessions open, reset and one call each with a trivial
  world, zero errors (marked slow).
- `test_serve_cli.py`: `seahaven serve --help`; missing extra message (run in a venv without
  `openenv`, marked slow, or by import-blocking `openenv` in `sys.modules`).
- `test_push_layout.py`: a `seahaven new --hub` scaffold passes `openenv`'s
  `validate_env_structure` with no errors (import it directly; skip if the extra is absent).
