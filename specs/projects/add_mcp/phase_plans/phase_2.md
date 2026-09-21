---
status: complete
---

# Phase 2: The server

## Overview

`src/seahaven/mcp/` — the package that serves one world over MCP, and the tests that drive it
through the SDK's own client. Three modules, the split of `architecture.md` §1: `__init__.py` holds
`serve()`, the instance map's lifetime and shutdown; `server.py` builds the `Server`, the middleware
that creates the instance and the two handlers; `wire.py` translates listings, results and errors.
Nothing here imports `openenv`, and nothing in `openenv` is touched.

Two facts about `mcp` 2.2.0 were read out of the installed package while planning this phase, and
both cut against `architecture.md` §4 and §5. They are recorded here, and the code carries a comment
at each point:

- **`ctx.session` is built once per inbound request, not once per connection** (`ServerSession` is
  documented as "Per-request proxy", and `ServerRunner._make_context` builds one per message). An
  instance map keyed on it would never find the instance again. The key is therefore an opaque
  connection token, which `serve()` makes one of for the stdio connection it serves. Functional spec
  §6's requirement stands unchanged: the key is the connection, not the process, nothing assumes one
  entry, and a Streamable HTTP transport makes one token per `Mcp-Session-Id` and calls `open` once
  per connection.
- **The 2026-07-28 protocol era has no `initialize` at all.** `serve_dual_era_loop` picks the era
  from the client's opening request, and the SDK's own `Client` in its default `auto` mode opens
  with `server/discover`, which is a modern envelope. A server that creates its instance only in an
  `initialize` middleware serves that client nothing. So the instance is created on the first
  request that needs one — `initialize`, `tools/list` or `tools/call` — which is the same moment for
  a handshake client and the only possible moment for a modern one. Everything functional spec §5.2
  asks for is unchanged: the instance is made inside a handler, its failure reaches the client as a
  JSON-RPC error carrying the framework's triple, the message is not scrubbed, and the process then
  exits non-zero.

The interim control tool refusal of `architecture.md` §9 ships here, with the comment that names
functional spec §15 as what deletes it.

CI's `mcp` job gains the import assertion. It asserts `import mcp.server.context, seahaven.mcp`:
`seahaven.mcp` alone is not the predicate the tests skip on, and the open item this phase was handed
is that an installed extra must not be able to skip the MCP suites quietly. `tests/test_ci_workflow.py`
holds the job's line and the guard in `tests/conftest.py` to the same module name.

## Steps

1. `src/seahaven/mcp/wire.py`. No knowledge of sessions or handlers; translation only.

   ```py
   CORRELATION_ID_LENGTH = 12
   def listing(entries: list[dict[str, Any]]) -> list[types.Tool]: ...
   def success(result: Any) -> types.CallToolResult: ...
   def failure(error: ToolError) -> types.CallToolResult: ...
   def internal_error(correlation: str) -> ToolError: ...
   def log_failure(what: str, name: str | None = None) -> str: ...
   ```

   `listing` renames `input_schema` to the model's field name, sets no `output_schema`, and passes
   name and description through byte for byte. `success` runs the value through `call.serialise`,
   renders it as indented JSON in one text block, and sets `structured_content` only for an object.
   `failure` renders `error.to_dict()` in a text block with `is_error=True`. `log_failure` logs the
   current exception with its traceback and answers the correlation id; the eight lines it shares
   with `openenv/env.py`'s `_log_failure` are deliberate, and a comment in each names the other.

2. `src/seahaven/mcp/server.py`.

   ```py
   NEEDS_AN_INSTANCE = frozenset({"initialize", "tools/list", "tools/call"})

   class Sessions:
       def open(self, key: object, instance: Instance) -> None: ...
       def get(self, key: object) -> Instance: ...      # raises WorldBug when absent
       def close(self, key: object) -> None: ...        # destroys, then forgets
       def close_all(self) -> None: ...                 # every instance; a failure is logged

   @dataclass
   class State:
       fatal: BaseException | None = None
       failed: anyio.Event = field(default_factory=anyio.Event)

   def instructions_for(world: World) -> str: ...
   def build_server(
       world: World | Callable[[], World],
       *,
       reset_options: Mapping[str, Any],
       sessions: Sessions,
       state: State,
       key: object,
   ) -> Server[None]: ...
   ```

   `build_server` closes over the four and answers a `Server` with `name`, `version`,
   `instructions_for(world)` and the two handlers. It appends the middleware to `server.middleware`
   rather than replacing the list, so the SDK's own middleware stays.

   The middleware: for a request (`ctx.request_id is not None`) whose method is in
   `NEEDS_AN_INSTANCE`, make the instance if the connection has none, then `await call_next(ctx)`.
   Creation is under an `anyio.Lock`, because two requests can be in flight on a modern connection;
   the world is resolved once and memoised; both the resolution and `world.instance(**reset_options)`
   run on a worker thread. A failure records `state.fatal`, sets `state.failed`, logs the traceback
   to stderr and raises `MCPError(INTERNAL_ERROR, str(error), data=...)`, where `data` is the
   framework triple for a `ToolError` and absent otherwise. A later request on a connection that has
   already failed raises the same error again and never retries.

   `_on_list_tools` answers `ListToolsResult(tools=wire.listing(instance.tools()))`.

   `_on_call_tool` looks the instance up, refuses a name in `world.CONTROL_TOOL_NAMES` with
   `UnknownTool(name)` before dispatch (`architecture.md` §9), and runs
   `anyio.to_thread.run_sync(functools.partial(instance.call, name, **arguments))`. The ladder is
   `ToolError` → `wire.failure`; `SeahavenError` → log and `MCPError(INTERNAL_ERROR, "internal error
   (<id>)")`; `Exception` → log and `wire.failure(wire.internal_error(id))`.

3. `src/seahaven/mcp/__init__.py`: `DEFAULT_FATAL_GRACE = 5.0` and

   ```py
   def serve(
       world: World | Callable[[], World],
       *,
       reset_options: Mapping[str, Any],
       fatal_grace: float = DEFAULT_FATAL_GRACE,
   ) -> int: ...
   ```

   `serve` makes the `Sessions`, the `State` and the connection token, builds the server, and runs
   `anyio.run` on an async body that opens `stdio_server()` and a task group holding three things:
   a signal receiver for `SIGINT` and `SIGTERM` that cancels the scope, a watcher that cancels the
   scope `fatal_grace` seconds after `state.failed` is set, and `server.run(...)`. The scope is
   cancelled when `server.run` returns, so neither helper outlives the connection.
   `finally: sessions.close_all()` destroys every instance on every path. The answer is 1 when
   `state.fatal` is set and 0 otherwise.

4. `.github/workflows/ci.yml`: the import assertion in the `mcp` job, after the install step.

   ```yaml
   - name: The mcp extra imports
     run: uv run python -c "import mcp.server.context, seahaven.mcp"
   ```

5. `tests/conftest.py`: the `mcp_sdk()` guard of `architecture.md` §10, and `MCP_SDK_MODULE =
   "mcp.server.context"` beside it for the workflow test to read.

6. `tests/test_ci_workflow.py`: one test that the `mcp` job's import assertion names the module the
   guard skips on, so the job cannot pass while every MCP test skips.

## Tests

`tests/test_mcp_server.py`, behind the `mcp_sdk()` guard, driving the SDK's `Client` against a
server built with `build_server` over a world from `tests/conftest.py`.

- `test_instructions_are_the_worlds_own_when_it_sets_them` — verbatim, nothing added.
- `test_instructions_fall_back_to_the_name_and_the_description` — and the blank string falls back
  too.
- `test_instructions_fall_back_to_the_name_alone_without_a_description`.
- `test_the_tool_list_is_the_instances_with_mcps_spelling` — equal to `instance.tools()` with
  `input_schema` spelled `inputSchema`, name and description unchanged.
- `test_a_call_answers_json_text_and_structured_content` — object result: both, and they agree.
- `test_a_scalar_result_has_no_structured_content` — text block only.
- `test_a_write_is_seen_by_a_later_read_in_the_same_session`.
- `test_a_tool_error_is_an_is_error_result_carrying_the_triple` — not a JSON-RPC error.
- `test_an_unknown_tool_is_an_is_error_result`.
- `test_arguments_the_schema_refuses_are_an_is_error_result` — `ArgumentError`'s triple.
- `test_a_framework_error_is_a_json_rpc_internal_error_with_a_correlation_id` — the message is
  `internal error (<id>)`, the id is on the logged line, and no world text reaches the client.
- `test_an_unhandled_exception_is_an_is_error_result_with_the_generic_error`.
- `test_a_control_tool_is_answered_as_an_unknown_tool` — written against the behaviour, so §15
  deleting the filter changes nothing here.
- `test_a_bad_fixture_fails_the_handshake_with_the_fixture_named` — the message is not scrubbed, and
  `state.fatal` is set.
- `test_a_failed_initialize_is_not_retried_by_a_later_request`.
- `test_the_session_map_holds_one_instance_per_key` — two keys, two instances, `close` destroys one
  and leaves the other, `close_all` destroys the rest.
- `test_a_modern_client_that_never_initializes_is_served_one_instance` — the SDK client in its
  default mode, two calls, one instance, the write of the first seen by the second.
- `test_two_calls_in_flight_serialise` — both answer, and neither sees a half-written world.
- `test_serve_runs_a_real_stdio_process` — `serve()` in a subprocess over real pipes: the handshake,
  a tool call, then EOF, exit 0. The real entry point of this phase, per `AGENTS.md`.
