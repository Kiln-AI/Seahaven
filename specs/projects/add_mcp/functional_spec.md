---
status: complete
---

# Functional Spec: Add MCP

`seahaven mcp` is a new subcommand. It speaks MCP over stdio, holds one instance for the life of
the process, drives the world in process, and publishes the world's tools and nothing else.

This spec decides behaviour. `architecture.md` decides mechanism, and section 14 lists the facts
about the MCP SDK that the architecture step must verify against the installed package before it
chooses one.

## 1. The rule that settles the small questions

**The server looks like the server it clones.** A Seahaven world emulates a real product's tool
surface, and over MCP it is indistinguishable from that product's own MCP server: the tools, their
schemas, their errors, and a server instruction string the world's author writes. Nothing on the
protocol surface is about Seahaven. No fixture, no seed, no state document, no call log, no
composition report, no reset, no control tool.

The rule decides more than it is asked. When a feature would make the server legible as Seahaven
rather than as the product, it does not ship here. `seahaven mcp` is for a person or an agent
working against one world in one editor or chat; a harness that runs many episodes uses `/ws` and
`SeahavenClient`, which already give each rollout a private instance.

## 2. Command surface

```sh
seahaven mcp [--world module:attr] [--fixture NAME] [--seed N] [--now ISO]
             [--reset-options JSON]
```

The command runs until its client disconnects. It prints nothing to stdout that is not a protocol
frame.

### 2.1 Options

| Option | Meaning |
|---|---|
| `--world module:attr` | The world to serve. `add_world_option`, resolved by `find_world`, identical to `serve`. |
| `--fixture NAME` | The fixture the instance starts from. Omitted means a blank instance. |
| `--seed N` | The caller seed, an integer. Omitted means a random seed: see §4. |
| `--now ISO` | The clock a blank instance starts at. The framework refuses it together with a fixture. |
| `--reset-options JSON` | A JSON object passed to `world.instance()` as keyword arguments, whole. |

`--reset-options` is the general door. `fixture`, `seed`, `now` and `state_format` are the
framework's four (`world.RESET_ARGUMENTS`), and a world adds its own: every keyword its instance
startup hooks name is a reset option too. The list is the world's to extend, so the general door is
a JSON object rather than a fixed set of flags. `--fixture`, `--seed` and `--now` are convenience
spellings of the three that are common, because JSON inside an `.mcp.json` args array is painful to
quote.

`--state-format` is not offered. The state document is not published over MCP (§6), so the format
it would be rendered in has no reader. A world that wants a non-default format for some other reason
can pass `state_format` through `--reset-options`.

There is no `--include-control-tools`. `serve` has that flag because a harness drives the server it
starts; nothing that reaches an MCP client may run arbitrary SQL against the world.

### 2.2 Environment variables

| Variable | Equivalent to |
|---|---|
| `SEAHAVEN_RESET_OPTIONS` | `--reset-options` |
| `SEAHAVEN_FIXTURE` | `--fixture` |
| `SEAHAVEN_SEED` | `--seed` |
| `SEAHAVEN_NOW` | `--now` |

MCP client configurations pass `env` more comfortably than `args`, so every option except `--world`
has a variable. A flag beats the matching variable, silently: the flag is the more specific of the
two, and a warning about a variable the user set in their client config months ago is noise on every
launch.

`--world` has no variable. It names the code to import, which belongs beside the command in the
config file where a reader of that file can see it.

### 2.3 The general door and the convenience flags do not mix

`--reset-options` (or `SEAHAVEN_RESET_OPTIONS`) may not be combined with any convenience source,
flag or variable. They are not merged and neither takes precedence: a user who has to reason about
which `fixture` wins has already lost. The refusal names every conflicting source actually given,
in the spelling the user used:

```
--fixture cannot be combined with --reset-options; put "fixture" inside the --reset-options
JSON instead: --reset-options '{"fixture": "small_startup", "seed": 7}'
```

```
SEAHAVEN_FIXTURE and --seed cannot be combined with SEAHAVEN_RESET_OPTIONS; put "fixture" and
"seed" inside the SEAHAVEN_RESET_OPTIONS JSON instead
```

### 2.4 Refusals before the protocol starts

These are checked before anything is served, printed as one line on stderr, and exit 1. They need no
world, and a user who provokes one is looking at their own command line or their own client config.

- `--reset-options` that is not valid JSON.
- `--reset-options` that is valid JSON but not an object (`[1, 2]`, `"small_startup"`, `7`).
- `--seed` or `SEAHAVEN_SEED` that is not an integer.
- The mixing refusal of §2.3.
- `--world` that is not `module:attr` (`cli.discover` already raises this).

Argparse's own usage errors keep their exit code of 2.

Everything that needs the world is checked later, inside `initialize` (§5.2): an import that
fails, a fixture that does not exist, a reset option no hook names, a startup hook that raises.

## 3. Server identity and instructions

`serverInfo` carries the world's `name` and `version`.

The MCP `instructions` string is the world's, not the framework's:

- `World(..., mcp_server_instructions=...)` is a new optional constructor argument. When it is set,
  its value is returned verbatim, and nothing is added to it.
- When it is unset or blank, the default is built from the world's `name` and its `description`.
  Both are the author's own prose about the world, and neither carries anything about this process:
  no fixture, no seed, no instance. A world with no `description` falls back to the name alone.

A world author cloning a real MCP server reads that server's instructions and sets
`mcp_server_instructions` to match. `authoring.md` says so, and `reference/api.md` documents the
argument.

The argument is a free string, unvalidated, like `description`: it never becomes a path, a filename
or an identifier. It is the only thing about the MCP surface a world declares, and a world that
never serves MCP does not set it.

## 4. Seeds

A seed is not a Seahaven concept the client learns about; it is how this process is configured.

- A seed given through any door is used as given.
- **No seed means a random seed.** The command picks an integer, passes it to `world.instance()`,
  and writes it to stderr so a user who wants the run again can pass it back with `--seed`.
- The chosen seed appears nowhere else: not in `instructions`, not in a tool result, not in a
  resource. §1.

This is deliberately **not** the framework's default. Omitting `seed=` from `world.instance()` gives
`ids.DEFAULT_CALLER_SEED`, a constant, so the same fixture replays the same ids and the same clock
on every launch. That is right for a test and wrong here: a user relaunches their MCP client all day
and expects a world that moved on, not one that reset to the same ids. The docs state the deviation
next to the flag.

## 5. Protocol surface

### 5.1 Capabilities

The server declares **tools and nothing else**. No resources, no prompts, no logging capability, no
completions. The tool list is fixed for the life of the process, so no `tools/list_changed`
notification is ever sent.

### 5.2 `initialize`

The instance is created here, in the handler, not before the protocol starts. A bad fixture name or
a raising startup hook then reaches the client as a JSON-RPC error carrying the framework's
`{code, message, details}`; a process that died before the handshake gives the client nothing but a
broken pipe.

On failure the server answers the error and then **exits non-zero**. It does not stay up refusing
every later call: there is no instance, there never will be one in this process, and a server that
answers `tools/list` with an empty list looks like a world with no tools.

The error messages of `initialize` are **not scrubbed**. A working world appears as the world it
clones; a broken one says what is actually wrong, so the person who broke it can fix it. Nobody is
being evaluated against a server that never started, the person reading the message is the one who
wrote the `.mcp.json` that launched the process, and "fixture 'small_startupp' not found" is exactly
what they need to see.

A framework error *during* a session is still scrubbed to `internal error (<correlation id>)`
(§5.5). There an agent is in the loop and an eval must not be able to read an author's prose out of
an error (`errors.INTERNAL_ERROR_MESSAGE`); the real error and its traceback are on stderr, which
is where the person debugging is already looking.

### 5.3 `tools/list`

`Instance.tools()`, with each entry's `input_schema` renamed to `inputSchema`. Name and description
pass through unchanged, byte for byte: a contributed tool's listing is already the added world's
own, and nothing in it reveals where it came from.

Control tools are never in `Instance.tools()`, so they are never listed. No `outputSchema` is
published: Seahaven tools do not declare one.

Seahaven already refuses a tool name outside `^[A-Za-z0-9_-]{1,128}$` (`composition.TOOL_NAME`),
which is the same rule the strict MCP clients publish, so no name needs rewriting and no lint is
added. A client that prefixes the server name onto a tool name (`mcp__<server>__<tool>`) spends its
own budget doing so; the docs note it as a reason to keep tool names short, and nothing enforces it.

### 5.4 `tools/call`

One call is `Instance.call(name, **arguments)`.

**Results.** A successful call answers a text content block holding the result as JSON. When the
result is a JSON object, the same object is also returned as `structuredContent`; when it is a
scalar, a list, or anything else that is not an object, only the text block is returned, because
`structuredContent` is defined as an object.

**Failures are results, not protocol errors.** A tool failure is an MCP result with `isError: true`
whose text block carries the `{code, message, details}` triple, so the model can read it and
recover. This is the same split `SeahavenClient.call` already makes. JSON-RPC errors are reserved
for framework and protocol failures.

**Control tools need no special case here**, because §15 takes them off every world by default.
`seahaven mcp` never enables them, so `controller_run_sql` is not in the registry `Instance.call`
resolves against, and a `tools/call` naming it gets `UnknownTool` from the framework — the same
answer as any other name the world does not have.

That is a change to core, specified in §15, and it is expected to land separately. Until it does,
this command filters the name itself, the way `openenv/env.py` does today. Either way the
observable behaviour of `seahaven mcp` is the one stated above, and the tests in §12 do not change
when the filter is deleted.

### 5.5 Error taxonomy

| What failed | What the client gets |
|---|---|
| A name the world does not have, or a control tool's name | `isError` result, `UnknownTool`'s triple |
| Arguments the tool's schema refuses | `isError` result, `ArgumentError`'s triple |
| Any `ToolError` from the world, including `DbError` | `isError` result, that error's triple |
| `WorldBug` or any other `SeahavenError` | JSON-RPC internal error, message `internal error (<correlation id>)`; the real error and its traceback go to stderr |
| Any other exception out of the call | `isError` result with `{"code": "internal", "message": "internal error (<correlation id>)"}`; the real error and its traceback go to stderr |

This mirrors `openenv/env.py` deliberately: one taxonomy, whichever wire a call arrives on. The
correlation id is the only join between what the client sees and what stderr holds.

### 5.6 What is not published

No resources. No prompts. No `reset` tool, no `state` tool, no control tool. `Instance.state()`,
`Instance.call_log()`, `Instance.change_log()` and the composition report are not reachable over
MCP at all.

The state document is how an episode is graded, and a client that exposes resources to the model
would hand the agent its own grade. A client that does not would still put a Seahaven-shaped object
in front of a user who came for the emulated product. Neither is wanted, and an eval has `/ws` and
`SeahavenClient`.

## 6. Lifecycle

**One instance per MCP session.** The server holds a map from session to instance. A stdio process
always has exactly one session, so the map always has exactly one entry — but the key is the
session, not the process, from the first commit, so that adding a Streamable HTTP transport later
is a map lookup on `Mcp-Session-Id` and not a rewrite. Nothing in the implementation may assume the
map has one entry.

**One episode per session.** A session never gets a second instance. There is no reset, and a
client that wants a fresh world restarts the server. This is a rule about how many episodes a
session may run, not about how the instance is keyed.

**Destruction.** The instance is destroyed on stdin EOF, on `SIGTERM` and on `SIGINT`. `Instance` is
a context manager, so one `with` block around the serve loop covers the normal exits. After
destruction the instance's working directory and its copy of the fixture are gone.

**Exit codes.** 0 when the client disconnects normally. 1 for a refusal before the protocol starts
(§2.4) and for a failed `initialize` (§5.2). 2 for argparse's own usage errors.

## 7. Concurrency

A client may have several `tools/call` in flight. Calls into one instance serialise on the
instance's own lock, so a second call waits for the first to commit and the world is never read
half-written. Arrival order is not promised: a client that needs a specific order sends its calls
one at a time.

A cancellation notification does not stop a call that is already running. Seahaven cannot interrupt
a tool call; the call runs to completion and its result is discarded.

## 8. stdout and stderr

**stdout belongs to the protocol.** One stray `print` from a world's startup hook or a tool corrupts
the JSON stream, and the client reports a parse error far from the cause. Nothing a world writes to
stdout may reach the client: it goes to stderr instead, and the protocol stream stays valid.

Every log, warning and traceback goes to stderr, as does the chosen seed (§4). There is no
`--log-level` flag; the framework's own logging configuration stands.

## 9. Packaging

`pyproject.toml` gains an extra:

```toml
[project.optional-dependencies]
mcp = ["mcp>=2.2,<3"]
```

The official SDK, on its lower-level server API. `fastmcp` depends on `mcp` anyway and adds
composition, proxying and OpenAPI generation this command has no use for.

The extra is independent of `serve`. `seahaven mcp` imports no `openenv`, and `seahaven serve` needs
no `mcp`. Running the OpenEnv server to hold a single session would add uvicorn, a WebSocket to
ourselves, and the whole session manager for one instance.

Missing extra is one line on stderr and exit 1, checked before the world is, in the same shape
`serve` uses:

```
seahaven mcp needs the mcp extra: pip install "seahaven[mcp]"
```

`scripts/check_licences.py` is a gate on this project: `mcp` is MIT, but its transitive tree must
pass the script as well. If anything in that tree is copyleft, the extra does not ship as specified
and the architecture step says so rather than working around the script.

## 10. Where the work lands

- `src/seahaven/mcp/` for the server; `src/seahaven/cli/mcp.py` for argument parsing only, the way
  `cli/serve.py` splits from `openenv/serve.py`.
- `src/seahaven/cli/__init__.py`: the module joins the tuple in `build_parser`, and the module
  docstring, which counts the subcommands, is updated.
- `src/seahaven/world.py`: the `mcp_server_instructions` argument.
- `pyproject.toml`: the `mcp` extra.

`seahaven new` does not gain a `.mcp.json`. A freshly scaffolded world has no fixtures, so the file
would be a blank-instance config with a comment explaining what to put in it later.

## 11. Docs

- `serving_and_openenv.md`: a short section on `seahaven mcp` — what it is, the `.mcp.json` that
  launches it, one instance per process, and `seahaven mcp -h` for the flags. It also covers the
  random-seed deviation of §4.
- `serving_and_openenv.md`: the sentence "Seahaven will not add MCP support until the standard
  supports stateful servers" is rewritten. The `POST /mcp` and `ws /mcp` refusals stay, and keep
  their reasoning; what changes is that a user who wants an MCP client now has an answer. The same
  section says plainly that `seahaven mcp` is not the road for an eval or an RL harness.
- `reference/cli.md`: the subcommand, every flag and every environment variable.
- `authoring.md`: `mcp_server_instructions`, and the instruction to read the real server's
  instructions when cloning one.
- `reference/api.md`: the new `World` argument.
- `README.md`: MCP belongs in the first description of what a world can do.

No new page, so `tests/test_docs.py`'s `PAGES` tuple is unchanged.

## 12. What must be proven by tests

Unit tests for the resolution rules, and tests that drive the real entry point, per `AGENTS.md`:
this project has a history of defects that passed a unit test and failed on the first real call.

**Through a real client, against a real subprocess:**

1. `initialize`, `tools/list`, `tools/call` against `projecttracker` — the tool list equals
   `Instance.tools()` with `inputSchema` spelled MCP's way.
2. Writes are writes: a write tool, then a read tool in the same session sees it.
3. A tool that raises `ToolError` gives `isError: true` and the `{code, message, details}` triple,
   not a JSON-RPC error.
4. `controller_run_sql` is answered as an unknown tool. This test is written against the
   behaviour, not the mechanism, so it passes whether the refusal comes from §15's core change or
   from the interim filter.
5. A world whose startup hook and whose tool both `print` to stdout: every frame still parses.
6. A bad fixture name: the client receives a JSON-RPC error naming the fixture, and the process
   exits non-zero.
7. Stdin EOF destroys the instance and removes its working directory.
8. Two calls in flight serialise; neither sees a half-written world.

**Unit:**

9. The option matrix: each flag, each variable, flag beats variable, and every mixing refusal of
   §2.3 with its message naming the sources actually given.
10. No seed gives a random seed, two launches differ, and the seed reaches stderr.
11. `mcp_server_instructions` set is returned verbatim; unset falls back to a default built from
    the world's name and description; unset with no description falls back to the name alone.
12. The session-to-instance map is exercised with more than one entry, so the stdio case is proved
    to be the degenerate case and not the only one the code supports.

## 13. Acceptance

A user installs the world and the `mcp` extra, adds ten lines to `.mcp.json`, restarts their MCP
client, and calls the world's tools from a chat with no other setup. Writes are writes: the next
read in the same conversation sees them. Closing the client destroys the instance and its copy of
the fixture. Nothing the client shows them names Seahaven.

## 14. Verified against `mcp` 2.2.0

Research was skipped for this project, so §14 originally listed the SDK facts this spec was written
against as assumptions. They were then checked by installing `mcp` 2.2.0 and reading the package.
All six hold, two of them more strongly than assumed. `architecture.md` names the API this resolves
to; what follows is the answer to each question the spec depended on.

| Question | Answer |
|---|---|
| The lower-level server API | `mcp.server.lowlevel.Server(name, *, version, instructions, lifespan, on_list_tools=..., on_call_tool=...)`. Handlers are async, `(ctx, params) -> result`, passed as constructor arguments. |
| A hook at `initialize` | `initialize` is reserved — the runner owns the handshake and registering a handler for it raises. `Server.middleware` observes and can **veto** it: a middleware that raises before `call_next(ctx)` fails the handshake. That is where the instance is created. |
| A session identity to key on | `ServerRequestContext` carries `session`, `lifespan_context`, `protocol_version`, `method`, `params` and `request_id`. The session object is the map key §6 asks for. |
| stdout | Stronger than assumed. `stdio_server()` claims the file descriptors: while serving, **fd 0 points at the null device and fd 1 at stderr**, so stray output from handlers *and from child processes* misses the wire, and both are restored on exit. §8 needs no work of its own. |
| Synchronous handlers | Not free at this layer. The high-level `MCPServer` runs sync tool functions on worker threads; the lower-level `Server` takes async handlers, so `Instance.call` is moved to a thread by this project. |
| `structuredContent` without an `outputSchema` | Safe. The SDK validates `structured_content` against an output schema only when the tool declares one; a tool with no `outputSchema` is never checked, and the failure case is the reverse — declaring a schema and sending no structured content. |

Two spellings follow from the types rather than from this spec. `types.Tool` names its field
`input_schema` and serialises it as `inputSchema`, so §5.3's rename is the model's, not ours; and
`CallToolResult` carries `content`, `structured_content` and `is_error`, aliased to
`structuredContent` and `isError`.

A JSON-RPC error is raised as `MCPError(code, message, data)`, and `data` is where the framework's
`{code, message, details}` triple goes — the same placement `seahaven.openenv` already uses when it
refuses `/mcp`.

## 15. Dependency: control tools become opt-in in core

**This change is expected to land separately, before or alongside this project.** It is not part of
this project's phases. It is written down here because `seahaven mcp` depends on it, and because
this is where the need for it was found. The prompt that hands it to another agent is
`control_tools_task.md`, beside this file.

### 15.1 What is true today

`World.__init__` registers `control.TOOLS` on every world, and `Instance._target` falls back to the
root's own registry for a control tool, so `instance.call("controller_run_sql", sql=...)` works on
any instance of any world, in process, with nothing to turn it on. `Instance.tools()` filters
control tools out of the listing, so they are hidden — but hidden is not off. The only real gate is
at the wire, in `openenv/env.py`, which raises `UnknownTool` unless the server was started with
`include_control_tools`.

### 15.2 What changes

The Python interface for running a world grows a keyword-only parameter, default false:

```py
world.instance(fixture, *, control_tools: bool = False, ...)
```

An instance created without it cannot call a control tool by any path — by name, by function
reference, or over a wire — and the refusal is `UnknownTool`, in the same words as a name the world
does not have. Then the callers are fixed: `SeahavenEnv.reset` passes
`control_tools=self.include_control_tools`, the filter in `openenv/env.py` is deleted, and the
pytest plugin's `instance` fixture takes the default.

`seahaven serve --include-control-tools` keeps its exact meaning and its exact spelling, and
`worlds/projecttracker/tests/test_openenv.py` — which asserts the wire's refusal message today —
must keep passing unchanged. That test is the proof the wire behaviour did not move.

### 15.3 The hazard the change has to close

`SeahavenEnv.reset(**startup_kwargs)` passes every keyword straight through to `world.instance()`.
The moment `control_tools` is a named parameter of `world.instance()`, a client that sends
`control_tools: true` in a reset message binds to it and turns control tools on for itself, over
the wire — which is worse than today. `reset` must name `control_tools` explicitly so it can never
fall into `**startup_kwargs`, the way `state_format` is named today and for the same reason, and
must pass the server's own value rather than the client's.

### 15.4 What this project needs from it

One line: an instance `seahaven mcp` created can never call a control tool.

**If the change has not landed when this project is implemented**, `seahaven mcp` refuses control
tool names itself, exactly as `openenv/env.py` does today, and the filter is deleted when the core
change arrives. That interim is four lines and one test. The tests in §12 are written against the
behaviour, so none of them change when the filter goes.
