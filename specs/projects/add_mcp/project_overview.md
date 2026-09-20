---
status: complete
---

# Add MCP

Give a Seahaven world a real MCP server, so that an off-the-shelf MCP client (Claude Code, Claude
Desktop, Cursor, the `mcp` and `fastmcp` SDKs) can drive it with no adapter written by the user.

Today the answer to "how do I connect an MCP client to my world?" is "you cannot". That answer costs
us every user whose agent framework speaks MCP and not OpenEnv, which is most of them outside RL.

## Why there is no MCP server today

Two separate blockers, recorded in `src/seahaven/docs/serving_and_openenv.md`, section "Known
problems in OpenEnv".

1. **OpenEnv's `/mcp` is not MCP.** It dispatches four methods (`openenv/session/create`,
   `openenv/session/close`, `tools/list`, `tools/call`) and has no `initialize`, no capability
   negotiation, no notifications, no SSE, no `Mcp-Session-Id`, no resources and no prompts. A real
   MCP client opens with `initialize`, gets `-32601`, and stops. Seahaven refuses every method on
   that route on both transports.
2. **A Seahaven tool call needs an instance.** An instance starts with a fixture and a seed. MCP has
   no standard step for choosing them, so even against a correct MCP transport every `tools/call`
   would answer "reset first".

The docs currently state that Seahaven will not add MCP support until the standard supports stateful
servers. **This project changes that position**, because stdio removes the second blocker
completely: a stdio server is one client in one process, so the process boundary is the session
boundary, and the fixture and the seed are process configuration rather than a protocol step.
Blocker 1 does not apply either, because this does not go through OpenEnv at all.

## The design

A new subcommand, `seahaven mcp`. It speaks MCP over stdio, holds exactly one instance for the life
of the process, and drives the world **in process**.

```sh
seahaven mcp --world projecttracker:world --fixture small_startup --seed 7
```

```jsonc
// .mcp.json in the user's project
{
  "mcpServers": {
    "projecttracker": {
      "command": "uv",
      "args": ["run", "seahaven", "mcp", "--world", "projecttracker:world",
               "--fixture", "small_startup", "--seed", "7"]
    }
  }
}
```

**In process, not through the server.** `world.instance(...)`, `Instance.tools()`, `Instance.call()`
and `Instance.state()` are all core framework and import no `openenv`. Running the OpenEnv server to
hold a single session would add uvicorn, a WebSocket to ourselves, and the whole session manager for
one instance. It would also make MCP depend on the `serve` extra and its dependency tree. Put the
MCP SDK in a new `mcp` extra instead, independent of `serve`.

**One instance per process, created during `initialize`.** Create it in the `initialize` handler
rather than before the protocol starts. A bad fixture name or a raising startup hook then travels to
the client as a JSON-RPC error carrying the framework's `{code, message, details}`. If the process
dies before the handshake, the client can report only a broken pipe. Destroy the instance on stdin
EOF and on `SIGTERM` and `SIGINT`; `Instance` is a context manager, so one `with` block around the
serve loop covers the normal exits.

**stdout belongs to the protocol.** One stray `print` from a world's startup hook corrupts the JSON
stream, and the client reports a parse error far from the cause. Route every log, warning and
traceback to stderr, and guard the world's own code against writing to stdout.

**Serialize tool calls.** A client may have several `tools/call` in flight. The server's concurrency
gate does not apply in process, so this command owns the lock.

### CLI surface

- `--world module:attr`, from `add_world_option`, same as `serve`.
- `--reset-options '{...}'`, the general door. The JSON object is passed to `world.instance()` as
  keyword arguments, whole. `fixture`, `seed`, `now` and `state_format` are the framework's four
  (`world.RESET_ARGUMENTS`), and **a world adds its own**: every keyword its instance startup hooks
  name is a reset option too, and the framework already refuses a hook that tries to shadow one of
  the four. So the general door has to be a JSON object rather than a fixed list of flags, because
  the list is the world's to extend.
- `--fixture NAME`, `--seed N`, `--now ISO` as convenience flags for the common case, because JSON
  inside an `.mcp.json` args array is painful to quote.
- **No seed given means a random seed,** whichever door it came through. This command picks an
  integer, passes it to `world.instance()` and prints it to stderr, so that a user who wants the run
  again can pass it back with `--seed`. Note that this is **not** the framework's default: omitting
  `seed=` gives `ids.DEFAULT_CALLER_SEED`, a constant, so the same fixture replays the same ids and
  the same clock on every launch. That is right for a test and wrong here, where a user relaunches
  their MCP client all day and expects a world that moved on rather than one that reset to the same
  ids. Write the deviation down in the docs next to the flag.
- **Refuse `--reset-options` together with any convenience flag.** Do not merge them, and do not
  give one precedence over the other: a user who has to reason about which `fixture` wins has
  already lost. One clear error, naming the flags actually given:

  ```
  --fixture cannot be combined with --reset-options; put "fixture" inside the --reset-options
  JSON instead: --reset-options '{"fixture": "small_startup", "seed": 7}'
  ```

- `SEAHAVEN_RESET_OPTIONS`, `SEAHAVEN_FIXTURE` and `SEAHAVEN_SEED` environment variables, because
  MCP client configs pass `env` more comfortably than args. A flag beats the matching environment
  variable. The same exclusivity rule applies across sources, so that setting `SEAHAVEN_FIXTURE` in
  the client config and passing `--reset-options` on the command line is an error too, with the same
  message.

### Wire mapping

- `Instance.tools()` gives OpenEnv's `Tool` shape with `input_schema`. MCP wants `inputSchema`.
- A tool failure is an MCP **result** with `isError: true`, not a JSON-RPC error. Put the
  `{code, message, details}` triple in the content so the model can read it and recover. Reserve
  JSON-RPC errors for framework and protocol failures, which is the same split `SeahavenClient.call`
  already makes.
- Return `structuredContent` beside the text block. Seahaven tools answer JSON objects.
- Publish `Instance.state()` as an MCP **resource** (`seahaven://state`), not a tool. MCP has
  resources and OpenEnv's dialect does not. This keeps the grading document out of the agent's tool
  list.
- Do not publish `reset` as a tool. An agent that can reset can erase its own episode.

## Open questions for the functional spec

- **Which MCP SDK,** and what it costs. Check the dependency tree before committing, since keeping
  the install small is half the reason for not reusing `serve`.
- **Tool name constraints.** Some MCP clients restrict the character set or the length of a tool
  name, and some prefix them with the server name. Check Seahaven's tool names against the
  strictest client we care about, and decide whether `seahaven check` should lint for it.
- **More resources.** `seahaven://call-log` and the composition report are candidates. Keep the
  first release small.

## Non-goals

- **Streamable HTTP.** Not shipped in this project, but **designed for**. The internal boundary is
  one instance per MCP **session**, not one per process, even though a stdio process always holds
  exactly one session. Key the instance off the session from the first commit, so that adding a
  Streamable HTTP transport later is a map from `Mcp-Session-Id` to an instance and not a rewrite.
  A stdio process is then the degenerate case of that map with a single entry. This is the one piece
  of future-proofing the project asks for; do not collapse it into a process-global instance because
  stdio cannot tell the difference.
- **Changing `/mcp` on the OpenEnv app.** It stays refused. This project does not touch
  `seahaven.openenv`.
- **An MCP client.** `SeahavenClient` stays the way to drive a world over the network.
- **Evals and RL.** This command is for a person, or an agent, working against one world in one
  editor or chat. It is not the road for a harness that runs many episodes: that is what `/ws` and
  `SeahavenClient` are for, and one connection per rollout already gives a harness a private
  instance with no process to respawn. So there is no `--expose-reset` and no second episode inside
  a live session: one session, one instance, from `initialize` to close. That is a rule about how
  many episodes a session may run, not about how the instance is keyed; the session stays the key,
  for the reason above. The docs should say this plainly where a reader might otherwise reach for
  MCP to run an eval.

## Where the work lands

- `src/seahaven/mcp/` for the server, with `src/seahaven/cli/mcp.py` for argument parsing only, the
  way `cli/serve.py` splits from `openenv/serve.py`.
- `src/seahaven/cli/__init__.py`: add the module to the tuple in `build_parser`, and update the
  module docstring, which counts the subcommands.
- `pyproject.toml`: the new `mcp` extra.
- `src/seahaven/cli/templates/`: consider a `.mcp.json` in what `seahaven new` writes.

## Docs work

- `src/seahaven/docs/serving_and_openenv.md`: rewrite "Seahaven will not add MCP support until the
  standard supports stateful servers". The `/mcp` refusal stays and keeps its reasoning; what
  changes is that there is now an answer for a user who wants an MCP client.
- A new page, or a section of the serving page, for `seahaven mcp`. `tests/test_docs.py` holds the
  page list, so a new page means updating that list and every link.
- `src/seahaven/docs/reference/cli.md`: the new subcommand and every flag.
- `README.md`: MCP belongs in the first description of what a world can do.

## Acceptance

A user installs the world and the `mcp` extra, adds ten lines to `.mcp.json`, restarts their MCP
client, and calls the world's tools from a chat with no other setup. Writes are writes: the next
read in the same conversation sees them. Closing the client destroys the instance and its copy of
the fixture.
