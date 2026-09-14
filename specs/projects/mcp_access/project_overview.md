---
status: draft
---

# MCP access to a world

A Seahaven world served over OpenEnv already answers MCP's `tools/list` and fails every
`tools/call`. This project closes that gap, so that any world is usable by any MCP client with no
configuration, and is usable *well* with one flag. It is the post-V1 "MCP access to a world" line in
`seahaven_framework/project_overview.md` §6.

This overview states what the project is for and what it is. It changes nothing about the WebSocket
path, which remains the transport every eval and `SeahavenClient` use.

## 1. Why

OpenEnv registers `/mcp` on every server unconditionally, and — unlike `/reset`, `/step` and
`/state` — it is **not** gated behind `ServerMode.SIMULATION`. In a production or hub deployment,
`/mcp` and `/ws` are the two transports that survive. So MCP is not an optional extra on the surface
a world publishes; it is half of what a hub deployment exposes.

Today that half is dead for us. MCP dispatches exactly four methods — `openenv/session/create`,
`openenv/session/close`, `tools/list`, `tools/call` — and has **no `reset` and no `state`**.
`openenv/session/create` takes no parameters, so nothing can carry a fixture, a seed or a clock into
a session. A Seahaven session therefore has no instance, `tools/list` works (a tool list is the
world's, not the episode's), and every `tools/call` fails.

The fix is not a workaround for a broken endpoint. It unlocks a product shape the framework does not
currently have: a world served as a plain tool server, pointed at by any MCP client, with no
knowledge of OpenEnv, fixtures or episodes required. "Here is an issue tracker with realistic data —
point your agent at it."

## 2. Goals

- **Every world works over MCP with no configuration.** A `tools/call` on a fresh MCP session
  succeeds, backed by a blank instance built from the world's DDL — the same thing `reset()` with no
  fixture already means in process. This is the 90% case and the one to optimise for.
- **`seahaven serve --mcp-reset-options <json>`** supplies the reset an MCP session is created with,
  because MCP has no reset verb of its own. Omitted, a session gets `reset()` with no arguments.
- **Session isolation is preserved.** One MCP session is one instance, as one WebSocket connection
  is one instance. Two MCP clients never share state.
- **The WebSocket path is untouched**, in behaviour and in its tests. A WebSocket client still calls
  `reset` itself; this flag does not apply to it, and its name says so.
- **No upstream change is required.** Nothing here waits on OpenEnv.

## 3. Shape

`--mcp-reset-options` takes the same payload a `reset` frame carries over the WebSocket —
`fixture`, `seed`, `now`, and any `**startup_kwargs` the world declares — so there is one reset
contract rather than two. JSON is the shape, because `startup_kwargs` are arbitrary and typed per
world, and repeated `key=value` flags cannot carry them without inventing coercion rules:

```sh
seahaven serve --mcp-reset-options '{"fixture": "small_startup", "seed": 7}'
```

It is not a *default*, and is deliberately not named as one: over MCP a client has no way to
override it, because the protocol offers no reset. It is the reset, supplied by the operator because
the protocol cannot supply it.

The flag is the long tail. **The unset case is the one that matters** — a world that works over MCP
the moment it is served, with no flag and no thought. Nothing about supporting the flag should add
complexity to the path that does not use it.

It must be validated by the **existing** reset path rather than a parallel copy of it: `now=`
together with a fixture is already refused, because the fixture carries the clock, and that rule and
every other has to hold here for free. Failing at the first session with a descriptive error is
acceptable. Failing at `seahaven serve` instead is nicer and worth doing if it falls out cheaply,
but it is not worth building machinery for.

The load-bearing decision: **auto-initialisation is MCP-only.** Over the WebSocket, a tool call
before `reset` stays exactly the error it is today. The principle is that auto-init exists where the
protocol offers no alternative, and nowhere else — a harness on `/ws` that calls before resetting
has a bug, and silently handing it a blank world would hide that bug rather than fix it.

## 4. Decisions this project must make

- **Sessionless `tools/call`.** MCP permits a call with no `session_id`; upstream then builds a
  throwaway environment per request and closes it. With auto-init that call would "succeed" and
  silently discard every write. That is precisely the silent-wrongness filed against upstream's HTTP
  trio, reproduced in our own surface. The recommendation is to refuse it with a descriptive error
  naming `openenv/session/create`, consistent with how the HTTP episode-control routes are handled.
- **Control tools over MCP.** They are never listed and are callable only with
  `--include-control-tools`. MCP is the agent-facing transport, and a control tool gives its caller
  arbitrary read access to the world it is inside. Whether that flag should apply to the MCP path at
  all — or be refused there — needs an explicit answer, not an inherited default.
- **Clock asymmetry.** A session with no reset options runs on wall time; one created from
  `--mcp-reset-options` runs on whatever that payload implies, which for a fixture is the fixture's
  frozen clock. Both are existing, correct Seahaven behaviour, but the difference is visible to an
  MCP client and should be documented rather than discovered.

## 5. Non-goals

- **Episode semantics over MCP.** MCP has no reset verb, and exposing `reset` as a *tool* is ruled
  out: it puts framework verbs in the world's namespace and lets an agent reset its own episode
  mid-run, which is the class of problem upstream's #1126 describes.
- **State over MCP.** There is no `state` method and we are not inventing one. An MCP client is an
  agent calling tools; reading state is the harness's job, on `/ws`.
- **Replacing `/ws` for evals.** One session is one instance, so an agent on MCP and a harness on
  `/ws` would see different worlds. Evals stay entirely on the WebSocket.
- **Per-session reset options.** The protocol gives a client no way to ask, and inventing an
  extension method to carry them is upstream's design to make, not ours to fork.
- **Changing anything upstream**, or waiting for anything upstream.

## 6. Constraints

- No upstream OpenEnv change, and no dependency on a private OpenEnv API without the same deliberate
  documentation and seam-guard test used for `SeahavenClient._disconnect_async`.
- `tests/test_server.py` stays green, unmodified in intent.
- `src/seahaven/docs/serving.md` must document the flag, the unset behaviour, and the limitations in
  §4 — it is documentation that must stay current.
- Nothing may depend on process-wide mutable state.

## 7. Consumers

- **Hub users** pointing any MCP client at a deployed world, with no OpenEnv knowledge.
- **Agent frameworks** that speak MCP and nothing else.
- **Not eval harnesses.** They drive worlds over the WebSocket and read state there.
