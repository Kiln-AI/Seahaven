---
status: draft
---

# Architecture: Add MCP

How `seahaven mcp` is built. The functional spec decides behaviour; this decides mechanism, against
`mcp` 2.2.0 as installed and read (`functional_spec.md` §14).

## 1. Shape

Four modules, and one argument on `World`.

| Module | Holds | Imports the SDK |
|---|---|---|
| `seahaven/cli/mcp.py` | The parser, option resolution, the extra check | no |
| `seahaven/mcp/__init__.py` | `serve(...)`: the serve loop, the instance map, shutdown | yes |
| `seahaven/mcp/server.py` | Building the `Server`, the middleware and the two handlers | yes |
| `seahaven/mcp/wire.py` | Translation: tool listings, results, errors | yes |

The split is `cli/serve.py`'s: the CLI module is argument parsing and nothing else, and the work
lives where a harness can call it without going through `argv`. `cli/mcp.py` imports no SDK, so it
is importable — and testable — whether or not the extra is installed.

`seahaven/mcp/` imports no `openenv`, and `seahaven/openenv/` is not touched by this project.

## 2. Public interfaces

```py
# seahaven/cli/mcp.py
MISSING_EXTRA: str
def add_parser(subcommands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None: ...
def run(args: argparse.Namespace) -> int: ...
def resolve_options(
    args: argparse.Namespace, environ: Mapping[str, str]
) -> dict[str, Any]: ...        # raises CliError; the whole of §2.2-§2.4

# seahaven/mcp/__init__.py
DEFAULT_FATAL_GRACE: float = 5.0
def serve(
    world: World | Callable[[], World],
    *,
    reset_options: Mapping[str, Any],
    fatal_grace: float = DEFAULT_FATAL_GRACE,
) -> int: ...           # exit code: 0 for a clean disconnect, 1 for a failed initialize
```

`world` takes a callable as well as a `World` so the CLI can defer discovery: `find_world` is called
inside `initialize` (§5), where an import failure reaches the client as a JSON-RPC error instead of
a broken pipe. A harness driving this in process passes the `World` itself.

`World.__init__` gains `mcp_server_instructions: str | None = None`, stored on the instance
unvalidated beside `description` and read by nothing else in core.

## 3. Option resolution

`resolve_options` is a pure function of the namespace and an environment mapping: no world, no
filesystem, no SDK. It answers the keyword arguments `world.instance()` will be called with, and
raises `CliError` for every refusal in functional spec §2.4.

```py
_CONVENIENCE = {"fixture": "SEAHAVEN_FIXTURE", "seed": "SEAHAVEN_SEED", "now": "SEAHAVEN_NOW"}
_GENERAL = "SEAHAVEN_RESET_OPTIONS"
```

The order is: read each source, then refuse a mix, then parse.

1. For each of the four options, take the flag when it is not `None`, else the variable when it is
   set, else nothing. Record **which source** each value came from — the flag's spelling
   (`--fixture`) or the variable's name (`SEAHAVEN_FIXTURE`) — because the refusal message names
   the sources the user actually used.
2. If a general source and any convenience source are both present, raise, naming every conflicting
   source in the order `fixture, seed, now` and the general source by its own spelling.
3. Parse: `--reset-options` through `json.loads`, refusing a document that is not an object;
   `--seed` through `int`, refusing anything else. Both messages name the source.
4. Answer the general object as it stands, or the convenience keywords that were given.
5. **The seed**: when no seed was given by either door, put `seed=random.randrange(0, 2**31)` into
   the answer and record it, so `run` can write it to stderr. A seed given inside a
   `--reset-options` object counts as given; the key is `"seed"`, whatever its value, so a caller
   that deliberately passes `seed=None` gets the framework's constant.

The random seed is chosen here rather than inside `serve`, so that one function owns "what
`world.instance()` is called with" and a test of the resolution covers the deviation from
`ids.DEFAULT_CALLER_SEED` without starting a server.

`run(args)` then: resolves options, imports `seahaven.mcp` (raising `CliError(MISSING_EXTRA)` on
`ImportError`, before the world is touched, exactly as `cli/serve.py` does), writes the seed line to
stderr, and returns `serving.serve(lambda: find_world(args.world), reset_options=options)`.

## 4. The instance map

```py
class _Sessions:
    """One instance per MCP session. A stdio process has exactly one."""
    def __init__(self) -> None:
        self._instances: dict[ServerSession, Instance] = {}
    def open(self, session: ServerSession, instance: Instance) -> None: ...
    def get(self, session: ServerSession) -> Instance: ...   # raises WorldBug when absent
    def close(self, session: ServerSession) -> None: ...     # destroys, then forgets
    def close_all(self) -> None: ...            # every instance; a destroy failure is logged
```

Keyed by the `ServerSession` object, which every handler and the middleware reach as `ctx.session`.
Nothing in the class assumes one entry: functional spec §6 asks for the session to be the key from
the first commit so that a Streamable HTTP transport is a second caller of `open`, not a rewrite,
and test 12 exercises it with two.

The map is touched only from the event loop thread — the middleware and the handlers are async, and
the only thing handed to a worker thread is the `Instance` itself — so it needs no lock. A comment
says so, because the absence of one is otherwise a question.

## 5. Creating the instance during `initialize`

`initialize` is reserved by the SDK: `Server.add_request_handler("initialize", ...)` raises, because
the runner owns the handshake. The documented way in is `Server.middleware`, which wraps every
inbound request *including* `initialize`, and where **raising before `call_next(ctx)` vetoes the
handshake**.

```py
async def _open_session(ctx: ServerRequestContext[None], call_next: Callable[..., Awaitable[Any]]):
    if ctx.method == "initialize" and ctx.request_id is not None:
        try:
            instance = await anyio.to_thread.run_sync(_make_instance)
        except SeahavenError as error:
            state.fatal = error
            raise MCPError(INTERNAL_ERROR, str(error), data=_details(error)) from error
        sessions.open(ctx.session, instance)
    return await call_next(ctx)
```

- **`_make_instance`** resolves the world (once, memoised) and calls
  `world.instance(**reset_options)`. Both steps are blocking and both can be slow — a fixture is
  copied on disk — so they run on a worker thread rather than on the loop.
- **The error is not scrubbed** (functional spec §5.2). A `SeahavenError` carries a message written
  for the person who launched the process; a `ToolError` subclass also carries `{code, message,
  details}`, which goes in `MCPError`'s `data`, the same placement `seahaven.openenv` uses when it
  refuses `/mcp`. A `CliError` from world discovery is rendered the same way.
- **`state.fatal`** records that this process can never serve. §7 says what is done about it.
- The SDK warns that `initialize` is handled inline and that awaiting a server-to-client request
  during it deadlocks the connection. Nothing here sends one.

A middleware rather than a lifespan: `lifespan` runs around `Server.run`, which is before the
handshake, so a failure there is exactly the broken pipe functional spec §5.2 exists to avoid.

## 6. The two handlers

Both are async, both are passed to the `Server` constructor, and neither does any work of its own
beyond translation.

```py
Server(
    name=world.name,
    version=world.version,
    instructions=instructions_for(world),
    on_list_tools=_on_list_tools,
    on_call_tool=_on_call_tool,
)
```

**`instructions_for(world)`** (in `server.py`): `world.mcp_server_instructions` when it is set and
not blank, verbatim. Otherwise the world's `name`, a blank line, and its `description` — or the name
alone when there is no description. The framework writes no prose of its own into it.

**`_on_list_tools`** answers `ListToolsResult(tools=[...])` built from `instance.tools()`. The
listing's `input_schema` key is the model's field name, and `types.Tool` serialises it as
`inputSchema`, so the rename functional spec §5.3 asks for is the type's and not ours. No
`output_schema` is set.

**`_on_call_tool`** looks the instance up by session, refuses a control tool name (§9), and runs the
call on a worker thread:

```py
result = await anyio.to_thread.run_sync(functools.partial(instance.call, name, **arguments))
```

`Instance.call` is synchronous and blocking, and the lower-level `Server` takes async handlers only
— the worker-thread dispatch the high-level `MCPServer` does for sync tool functions is not
available here, so this project does it. Several calls in flight are several worker threads, which
queue on the instance's own lock: calls into one instance serialise, which is what functional spec
§7 promises. The default `anyio` thread limiter (40) is far above any client's concurrency and is
left alone.

## 7. Errors, logging and exit

`wire.py` owns the rendering, and mirrors `openenv/env.py`'s taxonomy exactly (functional spec
§5.5):

```py
def success(result: Any) -> CallToolResult:
    value = serialise(result)
    text = json.dumps(value, indent=2, ensure_ascii=False)
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content=value if isinstance(value, dict) else None,
    )

def failure(error: ToolError) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(error.to_dict(), indent=2))],
        is_error=True,
    )
```

`structured_content` is set only for an object, because `structuredContent` is defined as one. The
SDK validates it against an output schema only when the tool declares one, and these declare none,
so the pairing is safe.

The handler's `except` ladder, in order:

| Caught | Answer |
|---|---|
| `ToolError` (including `UnknownTool`, `ArgumentError`, `DbError`) | `failure(error)` |
| `SeahavenError` | log traceback with a correlation id; `raise MCPError(INTERNAL_ERROR, f"internal error ({correlation})")` |
| `Exception` | log traceback with a correlation id; `failure(_internal_error(correlation))` |

The correlation id is a `uuid4().hex[:8]`, on the wire and on the stderr line and nowhere else.
`openenv/env.py` has a `_log_failure` of its own that this deliberately does not share: that one
logs an episode id and a call ordinal from the OpenEnv session, neither of which exists here, and a
helper general enough for both would be a helper that says less than either. The duplication is
eight lines and is noted in both.

**Logging** is `logging.getLogger(__name__)` and nothing more. The command installs no handler and
sets no level; the framework's configuration stands, and stderr is where Python's default sends it.

**Exit.** `serve` returns 0 when the read stream ends — stdin EOF, which is what a client closing
gives. When `state.fatal` is set, it returns 1. A client that has been handed a failed `initialize`
closes, so the usual path to that return is the same EOF; `fatal_grace` seconds after a fatal
initialize the serve scope is cancelled anyway, so a client that does not close cannot hang the
process. `finally: sessions.close_all()` destroys every instance on every path, and `Instance`
destruction is also what removes the working directory and the copy of the fixture.

**Signals.** `serve` runs `Server.run` inside an `anyio` task group with
`anyio.open_signal_receiver(SIGINT, SIGTERM)` cancelling the scope on the first signal. The
`finally`
above then destroys the instance, so a client that is killed rather than closed leaves nothing
behind.

## 8. `stdout` needs no work of ours

`stdio_server()` claims the file descriptors while serving: fd 0 points at the null device and fd 1
at stderr, restored on exit. A world's `print`, a C extension writing to fd 1, and a child process
the world spawns all miss the wire, which is stronger than a `contextlib.redirect_stdout` would be.

This project therefore adds no redirection of its own — and test 5 (functional spec §12) asserts the
property rather than the mechanism, so the test still means something if the SDK's behaviour ever
changes.

## 9. Control tools

Until the core change of functional spec §15 lands, `_on_call_tool` refuses the names in
`world.CONTROL_TOOL_NAMES` before dispatch, raising `UnknownTool(name)` so the ladder above renders
it as any unknown name. Four lines, one test, and a comment naming §15 as what deletes them.

After the core change, the instance `seahaven mcp` creates simply has no control tool to call, the
four lines go, and no test changes.

## 10. Packaging and CI

```toml
[project.optional-dependencies]
mcp = ["mcp>=2.2,<3"]
```

`uv.lock` is regenerated. `scripts/check_licences.py` covers every declared extra and runs in CI in
the environment the sync built, so the workflow's `uv sync --locked --extra serve` becomes
`--extra serve --extra mcp`; otherwise the new tree is never licence-checked. `mcp` is MIT and
`mcp-types` is MIT; the rest of the tree is checked by the script rather than by assertion here, and
a copyleft transitive dependency stops the extra rather than being worked around.

CI asserts the extra imports, beside the line that does it for `serve`:

```yaml
- name: The mcp extra imports
  run: uv run python -c "import seahaven.mcp"
```

That line exists because the tests below skip when the extra is absent, and an installed extra that
skips quietly is a green run that tested none of this — the reasoning `tests/test_cli_serve.py`
already records for `seahaven.openenv`.

## 11. Tests

Three files, following the split the repository already uses for `serve`:

**`tests/test_cli_mcp.py`** — no SDK. `run_cli` drives the parser; `serve` is replaced by a
recorder,
as `tests/test_cli_serve.py` replaces `openenv.serve`. Covers the whole of §3: each flag, each
variable, flag beats variable, every mixing refusal and its message, bad JSON, a JSON document that
is not an object, a non-integer seed, the random seed and its stderr line, and `MISSING_EXTRA`.

**`tests/test_mcp_server.py`** — the SDK in process, `pytest.importorskip("mcp")`. Builds the server
against a world from `tests/worlds/`, drives it through the SDK's own client over an in-memory
stream pair, and covers: the tool list against `instance.tools()`, a successful call's text and
`structuredContent`, a scalar result with no `structuredContent`, each row of the error table, the
control tool refusal, a failed `initialize` and its unscrubbed message, `instructions_for` in its
three forms, and the session map with two sessions open at once.

**`tests/test_mcp_process.py`** — the real entry point, `pytest.importorskip("mcp")`. Spawns
`seahaven mcp` as a subprocess against a test world and talks to it with the SDK client over stdio,
per `AGENTS.md`: a unit test that never left the process is not evidence here. Covers: the
handshake,
a write then a read seeing the write, a world that prints to stdout from a startup hook and from a
tool, a bad fixture giving a JSON-RPC error and exit 1, stdin EOF destroying the instance and
removing its working directory, and two calls in flight serialising.

The working-directory assertion reads the path off the instance the subprocess reports — a test
world whose startup hook writes its `state_path` to stderr — because the parent has no other handle
on it.

## 12. What was considered and rejected

- **The high-level `MCPServer`.** It runs sync tool functions on worker threads, which is the one
  thing the lower-level `Server` makes us do ourselves, but it wants tools declared as Python
  functions with type hints and derives the schema from them. Seahaven already owns its schemas, and
  handing them to something that would rather infer them is the wrong shape.
- **A `lifespan` for the instance.** §5.
- **Sharing `_log_failure` with `openenv/env.py`.** §7.
- **Running the OpenEnv server for one session.** Functional spec §9: uvicorn, a WebSocket to
  ourselves and the whole session manager, for one instance.
