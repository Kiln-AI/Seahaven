# `/spec task` prompt: make control tools truly opt in

Paste everything below the line as the description for `/spec task`.

---

Make control tools truly opt in, rather than always callable and merely hidden from listings.

**Today**, `World.__init__` registers `control.TOOLS` on every world, and `Instance._target`
deliberately falls back to the root's own registry for a control tool, so
`instance.call("controller_run_sql", sql=...)` works on any instance of any world, in process, with
nothing to turn it on. `Instance.tools()` filters control tools out of the listing, so they are
hidden — but hidden is not off. The only real gate is at the wire, in `openenv/env.py`, which
raises `UnknownTool` for a control tool name unless the server was started with
`include_control_tools`.

That is backwards. Every new way of running a world has to remember to re-implement that filter, or
it hands an agent read access to every table, around the tool surface the agent is being evaluated
against. A new `seahaven mcp` command is the case that surfaced it, and the next surface will have
the same problem.

**The change.** The Python interface for running a world grows a new keyword-only parameter,
default false:

```py
world.instance(fixture, *, control_tools: bool = False, ...)
```

An instance created without it cannot call a control tool by any path — by name, by function
reference, or over a wire. The refusal is `UnknownTool`, in the same words as a name the world does
not have: whether a control tool exists is not something a caller gets to learn by calling. An
instance created with `control_tools=True` behaves exactly as every instance does today.

**Then fix the callers**, which is most of the work:

- `SeahavenEnv.reset` passes `control_tools=self.include_control_tools`, so
  `seahaven serve --include-control-tools` keeps its exact meaning and its exact spelling.
- The filter in `openenv/env.py` is deleted. The refusal now comes from the instance, and it
  produces the same message the wire produced before — `worlds/projecttracker/tests/test_openenv.py`
  asserts that message today and must keep passing **unchanged**. That test is the proof the wire
  behaviour did not move.
- The pytest plugin's `instance` fixture gets the default, false.
- `tests/test_control.py` passes `control_tools=True` where it creates instances.
- `worlds/projecttracker/tests/test_package.py` and `worlds/projecttracker/tests/test_errors.py`
  assert the current default and need updating.

**The one thing that must not go wrong.** `SeahavenEnv.reset(**startup_kwargs)` passes every
keyword straight through to `world.instance()`. The moment `control_tools` is a named parameter of
`world.instance()`, a client that sends `control_tools: true` in a reset message binds to it and
turns control tools on for itself, over the wire — which is worse than today. `reset` must name
`control_tools` explicitly so it can never fall into `**startup_kwargs`, exactly the way
`state_format` is named today and for the same reason, and must pass the server's own value rather
than the client's. A test asserts that a reset carrying `control_tools: true` enables nothing.

Decide whether `control_tools` joins `world.RESET_ARGUMENTS` — the four names a startup hook may not
shadow. It should, for the same reason the other four are there, and that makes a world whose
startup hook declares a `control_tools` keyword newly refused. Say so in the commit message.

**What does not change.** Control tools stay out of every listing whether or not they are enabled,
so `Instance.tools()` keeps filtering them. `controller_run_sql` stays deprecated and still warns on
every call. The existing refusals stay: a world registering the name without `control=True`
(`world.py`), and a contributed control tool name (`composition.py`). The control tool still reads
through the instance's own control handle, still takes the instance lock, and still bypasses the
concurrency gate.

Registration stays on the `World` — the gate moves to the instance, which is the only place it
matters, and keeping the name registered is what lets the framework keep refusing a world that tries
to register it itself. If you find somewhere else that the registry leaks a control tool to a caller
who did not ask for one (`world.tools`, `seahaven check`, the web console), raise it rather than
fixing it silently.

**Tests.** Beyond the suites above: an instance with no flag has no callable control tool and raises
`UnknownTool`; an instance with the flag can call it; neither lists it; a reset carrying
`control_tools: true` enables nothing; `serve(..., include_control_tools=True)` can call it over the
wire and without the flag gets the unchanged `UnknownTool` message. Per `AGENTS.md`, cover the real
entry point, not only the unit.

**Docs.** These say today that the control tool is on every world, and each sentence needs
correcting: `reference/api.md` (the new parameter), `reference/cli.md` (what
`--include-control-tools` now does), `serving_and_openenv.md`, `authoring.md`, `composition.md`,
`extensions.md`.

Context, not instructions: this was specified while writing `specs/projects/add_mcp/`. §15 of
`specs/projects/add_mcp/functional_spec.md` records an earlier design that put the opt-in on the
`World` rather than the instance; this task supersedes it, and §15 should be updated to match once
this lands.
