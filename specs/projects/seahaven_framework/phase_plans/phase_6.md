---
status: draft
---

# Phase 6: OpenEnv

## Overview

The framework gets its wire. Phases 1–5 built a world that runs in one process: `world.instance(...)`
makes an instance, `instance.call(...)` runs a tool, and everything — the chain, the transaction, the
error hierarchy, the changeset, the fixtures — was proved through those two calls. That is the whole
API an eval harness in the same process needs, and none of the API a training run needs. This phase
adds the other side: `seahaven.openenv`, which puts one world behind an OpenEnv server so that an
agent anywhere can `reset` a session, list the world's tools and call them over a websocket.

It is a small subpackage — four modules, 781 lines including their prose — because the
mapping it implements is one sentence long. **A session is an instance.** OpenEnv makes one
environment object per connected session and runs that session on its own thread; `SeahavenEnv` holds
one `Instance` on that object. Everything else follows: two sessions are independent because two
instances are, a dropped connection costs nothing because `close` destroys the instance, and the
concurrency gate that bounds tool calls in process bounds them on the server too, because it is the
same gate.

What this phase builds:

- **`openenv/env.py`** — `SeahavenObservation`, `SeahavenState` and `SeahavenEnv`: `reset`, `step`,
  `state`, `close`, `get_metadata`, and the rules for what each error does on the wire.
- **`openenv/__init__.py`** — `app(world, ...)`, the ASGI object a container serves, plus the
  subpackage's public surface.
- **`openenv/client.py`** — `SeahavenClient`, the one typed client every Seahaven world shares.
- **`openenv/serve.py`** — `serve(world, ...)`: the gate, the app and one uvicorn worker. The CLI
  that calls it is Phase 7's.
- **`worlds/projecttracker/src/projecttracker/openenv_app.py`** — the reference world's three-line
  app module, and the first world served over the wire.

What it deliberately does not build: the `openenv push` layout of `components/openenv.md` §6
(`openenv.yaml`, `Dockerfile`, root `__init__.py`, `client.py`, `models.py`). Those are template
output, and templates are Phase 7's; writing them here would put a hub scaffold in the repository
that no `seahaven new` produced. `cli/serve.py`, `test_serve_cli.py` and `test_push_layout.py` are
Phase 7's for the same reason — `serve.py` exists here so that the CLI module can be argument parsing
and nothing else.

## Steps

1. **`src/seahaven/openenv/env.py`** — the two models and `SeahavenEnv`, as `components/openenv.md`
   §1–§2 specify them, plus two module-level helpers: `_internal_error()`, which builds the fixed
   generic error through `ToolError.to_dict()`, and `_first_paragraph()`, which turns a README into
   the one-line description a hub card shows. Both are underscore-prefixed because neither is part
   of the module's surface: review round 4 found them the only two public-looking names in a module
   whose `__all__` names three classes and whose six other helpers are all private, and the survivor
   table calls `__all__` "documentation of the module's surface" — so a name outside it that reads
   public is the documentation contradicting itself. `_is_title_line` was renamed to `_is_title_line`
   in the same round for the same kind of reason: it sat one letter from `_is_underline` and meant
   something else, and its docstring had to open by warning the reader about that.
2. **`src/seahaven/openenv/__init__.py`** — `app(world, ...)` exactly as §3 spells it, the two
   defaults as named constants, and an `__all__` that re-exports the models, the client and the two
   MCP action types a caller needs to drive the wire by hand.
3. **`src/seahaven/openenv/client.py`** — `SeahavenClient`: the three parsers, `call` and
   `list_tools`, and `__enter__`/`__aenter__` narrowed to the subclass's own type.
4. **`src/seahaven/openenv/serve.py`** — `serve(world, ...)`: `set_concurrency` then `uvicorn.run`.
5. **`worlds/projecttracker/src/projecttracker/openenv_app.py`** — the world's app module, spelled as
   §3 spells it.
6. **`tests/serving.py`** — a context manager that starts uvicorn on a kernel-chosen port in a thread
   and answers its base URL. Not a fixture: it takes arguments, and two test modules use it.
7. **`tests/test_env.py`, `tests/test_client.py`, `tests/test_server.py`, `tests/test_serve.py`**, and
   **`worlds/projecttracker/tests/test_openenv_app.py`** — the suites, 134 tests in the framework's
   and 7 in the world's, plus one in `tests/test_instances.py` for the accessor step 8 adds.
8. **`src/seahaven/instances.py`** — one accessor, `concurrency()`, answering the gate's size as
   `set_concurrency` last set it, and one module-level `_concurrency` recording it. A change to an
   earlier phase's module, kept as small as it can be and made for a test: `test_serve.py` asserted
   `instances._gate._initial_value`, which is one module's private name and one standard-library
   class's private attribute in a single expression, and the only place in the repository that read
   either. `BoundedSemaphore` does not publish the value it was built with, so the size is recorded
   beside the gate rather than derived from it, `set_concurrency` writes both, and
   `tests/conftest.py`'s isolation fixture — which already saved and restored `_gate` — now saves
   and restores both. Three hand mutants cover the addition, including the one that matters: a
   refused resize must not record the size it refused.
9. **`pyproject.toml`** — register the `slow` marker the 500-session smoke test carries.
10. **`.github/workflows/ci.yml`** — install with `--extra serve`, without which every test in this
    phase skips and `ty` never sees the subpackage.

## Deviations and decisions a reviewer should check

- **Three artifacts this phase touches are known-stale, Phase 4's design governs, and this is
  `BACKLOG.md` B8.** `components/helpers_and_control.md` §3 says at `:88` that the control tools are
  "thin wrappers over `Instance.inspect()` and `Instance.changes()`" and at `:90` that
  "`controller_run_sql` runs on the instance's inspection `Db` (`Instance.inspect()`, opened on first
  use)"; a third sentence in the same section at `:98-100` gives the `RLock`'s reason as "the reads it
  then makes through the `inspect()` handle do not take it"; and
  `components/fixtures_instances.md` §2.4 at `:164-165` repeats both claims. All four sentences
  describe the connection Phase 4's round-1 Critical **removed**. The control tools run on
  `Instance._control_db()` (`src/seahaven/instances.py:287-306`), a *second* read-only handle opened
  once and closed with the instance, precisely because `sandbox.run_statement` sets the connection's
  authorizer for the length of a statement and the `inspect()` handle is the one a caller reads
  through *without* the instance lock — two threads on one connection, one changing the authorizer
  while the other steps a cursor, wedge inside SQLite and take the interpreter with them. The lock is
  an `RLock` for **same-thread re-entry** — a control tool asking the instance for its changeset and
  its control handle with the lock already held — and not for the reason those artifacts give.
  Nothing in this phase was written to match them and nothing in this phase was written to fix them:
  all three are `status: complete`, editing a complete artifact cascades its dependents to `draft`,
  and that is a maintainer's call which has not been made. This phase serves the control tools over
  the wire (`include_control_tools`), which is the first time they are reached from a thread that is
  not the caller's, so the record is made here rather than left to the next reader.
- **`ListToolsAction` is answered before the "reset first" guard, and the component document can be
  read either way** (filed as `BACKLOG.md` **B12**, with the two other findings in the same artifact). §2 says `ListToolsAction` is "checked first" and shows
  `ListToolsObservation(tools=instance.tools())`, which needs an instance; the same bullet says "A
  `step` before `reset` raises `WorldBug("reset first")`". Both cannot hold: OpenEnv's own `/mcp`
  `tools/list` path calls `env.step(ListToolsAction())` on a session that has never been reset, and
  MCP's contract is that discovery does not require one. Discovery wins, because the alternative
  makes a standard MCP client fail against every Seahaven world. `_listing()` therefore answers
  `instance.tools()` when there is an instance and the same list derived from `world.tools` when
  there is not — and `test_list_tools_answers_before_a_reset_and_agrees_with_the_instance` pins that the
  two are identical, so the branch cannot drift into two different tool lists. The "reset first"
  guard stays, on `CallToolAction`, where it belongs: a call needs a database and a list does not.
- **The generic internal error carries three keys, not the two the component document shows** (B12).
  §2 writes it as `{"code": "internal", "message": "internal error"}`; `architecture.md` §6 defines
  the wire shape of every error as `{"code", "message", "details"}`. Architecture wins on the
  cross-component shape, and the generic error is built by `ToolError(INTERNAL_ERROR_CODE,
  INTERNAL_ERROR_MESSAGE).to_dict()` rather than written out as a literal, so it cannot drift from
  the shape every other error on the wire has. A client that reads `error["details"]` must not have
  to special-case the one error a world did not write.
- **`seahaven/__init__.py` is not touched.** `architecture.md` §1 lists `openenv` among the
  subpackages `seahaven` exports, and the same paragraph says "importing `seahaven` never imports
  `seahaven.openenv`". The second sentence is the operative one — the whole reason the subpackage
  exists is that `openenv`'s wheel brings gradio, openai, fastmcp and pandas, about 354 MB, and a
  world used in pytest or a notebook must not pay for it. `import seahaven.openenv` is the spelling
  everywhere: in `openenv_app.py`, in the docs and in the tests.
- **`serve` is not re-exported from `seahaven.openenv`.** `serve.py` imports `app`,
  `DEFAULT_MAX_CONCURRENT_ENVS` and `DEFAULT_SESSION_TIMEOUT` from the package's `__init__`, so the
  package cannot import `serve` back without a cycle. The Phase 7 CLI imports
  `seahaven.openenv.serve` directly, which is what `components/openenv.md` §4 describes anyway
  ("`openenv/serve.py`, called by `cli/serve.py`"). A lazy module-level `__getattr__` was written and
  then removed: it worked, and it made `seahaven.openenv.serve` mean two different things depending
  on whether the submodule had been imported yet.
- **The client's two conveniences go through `_dispatch`, not through `self.step(...)`** (B12). §5 sketches
  `call` as `self.step(CallToolAction(...)).observation`. `EnvClient.step` is itself dual-mode: in
  asynchronous code it answers an awaitable, and `.observation` on an awaitable is not a
  `SeahavenObservation`. The sketch is right about the shape and wrong about the mechanism, so `call`
  and `list_tools` are `self._dispatch(lambda: self._call_async(...))`, which is how the base client
  produces a value in synchronous code and an awaitable in asynchronous code from one method.
  `test_the_client_drives_a_world_synchronously` and `..._asynchronously` are the same flow in both
  modes; without the second one this would have shipped broken for every async harness.
- **`__enter__` and `__aenter__` are overridden only to narrow their type.** `EnvClient.__enter__` is
  annotated as returning `EnvClient`, so `with SeahavenClient(...) as env` gave a value that `ty`
  says has no `call` and no `list_tools` — the two methods the typed client exists for. The overrides
  call `super()` and return `self`, typed `Self`. This is the phase's one change to behaviour that is
  really a change to a type: without it, every world author's first line of harness code fails the
  type check that `AGENTS.md` requires them to run.
- **The client's tool name is positional-only, as `Instance.call`'s is** (review round 1, and B12's
  fourth item). `Instance.call` is `def call(self, name: str, /, **arguments)` and the `/` is
  deliberate: a world may declare a tool argument called `name`. `SeahavenClient.call` did not
  inherit the fix, and `components/openenv.md` §5 sketches it without one, so a world with a tool
  argument called `tool` — or `self` — had a tool that listed, worked through
  `step(CallToolAction(...))`, and raised `TypeError: got multiple values for argument 'tool'`
  through the one API the docs tell a harness author to use. Both `call` and `_call_async` now take
  the name positionally, and `test_a_tool_argument_called_tool_is_callable_through_the_client` drives
  such a tool over a real server, in both spellings, so the permissive side is pinned too.
- **Every field `SeahavenObservation` redeclares carries its own description, and `GET /schema` is
  read by a test that asserts over the whole set** (review rounds 1 and 2). The redeclarations are
  deliberate: `CallToolObservation` documents `result` as "Tool-specific result (may include tool
  errors)" and `error` as "Transport/framework error if call failed", which are exactly the two
  conventions this class swaps. What neither this plan nor the code noticed is that redeclaring a
  field *replaces* its schema entry, so a redeclaration without a `description` publishes none at
  all. Round 1 found that on `result` and the plan had recorded the redeclaration as changing
  "neither the field nor its default", which was only half true. Round 2 found the fix had closed
  `result` and left `tool_name` and `error` — the same defect, one field over, twice.

  Round 3 then found the same thing a class over. `SeahavenState` declares `fixture`, `now` and
  `world`; the two fields it *inherits* arrive described by OpenEnv, and those three had nothing
  said about them at all. Three findings, one defect, three times "the fix closed the case that was
  demonstrated". All six fields now describe themselves.

  So the test is not "`result` has its description", and it is not about a class either. The rule is
  **every field either model declares itself carries a description**, and
  `test_every_declared_field_publishes_a_description` asserts it over the set: the fields are read
  off `model.__annotations__`, so the test cannot drift from the code, and each must publish exactly
  the text a table in `tests/test_env.py` names. A field added to either model without a description
  fails there; one added with a description has to be listed there.

  The wire half is separate and smaller than it looks. `test_the_served_schema_publishes_the_observation_with_its_descriptions`
  reads `GET /schema` off a real server and asserts the observation's published descriptions are the
  ones its model carries — which proves the text reaches a client. It cannot do the same for the
  state, and finding out why is a finding of its own: `create_app` takes no state class and
  `get_schemas` answers `State.model_json_schema()`, so `/schema`'s state block describes
  `episode_id` and `step_count` for every environment there will ever be. That is filed as a third
  bullet on B13 — the same root cause as `GET /state`'s, one endpoint over — and deliberately not
  pinned by a test, because asserting the base model's answer would make upstream's defect
  Seahaven's contract. `SeahavenState` describes itself anyway: the model is what is right or wrong
  about its own fields, and the day upstream passes the real one through, the text is already there.

  `/schema` is a published surface of the app this phase builds and nothing in either suite was
  requesting it, which is exactly why the first mutant survived the first sweep.
- **The idle reaper has a behavioural test, not just a value-passed-through test** (review round 1).
  `session_timeout` was pinned only where `serve` hands it on. It is the only thing between a
  long-lived server and a disk of abandoned instances, and OpenEnv's `_cleanup_session_resources`
  wraps `env.close()` in `except Exception: pass` — so an `Instance.destroy()` that began failing
  would leak a fixture copy and an APSW connection per session with nothing red anywhere.
  `test_an_idle_session_is_reaped_and_its_instance_destroyed` serves with a one-second timeout and
  waits for the instance's working directory to go. OpenEnv's reaper wakes every
  `max(timeout / 4, 5.0)` seconds, so the test costs about six seconds and its deadline is generous;
  the assertion is only that the directory goes.
- **Every step is counted, including one that is refused.** `step` increments before it dispatches,
  so a `ListToolsAction`, a refused control tool and an unsupported action all advance
  `state.step_count`. The count is of what the session asked for, which is what a caller reconciling
  a trajectory against a transcript needs; a count of what succeeded would make two different frames
  indistinguishable in the state.
- **`_first_paragraph` skips a README's furniture: front matter as a block, headings, rules, and a
  title underlined by a rule.** A README opens with a Markdown heading; a Space card — which is what
  a world's `README.md` becomes once it is pushed to a hub — opens with YAML front matter between
  `---` fences. Neither is prose, and a description reading `# ProjectTracker` or `title: ...` is
  worse than no description at all. Four rules, each with its own job:

  1. **The block skip** (`_after_front_matter`) is what keeps YAML out of a card. The opening fence
     is the document's first *non-blank* line, and the block ends at the closing fence — searched for
     across the whole file *before* any other bound is considered. Only when no closing fence exists
     anywhere does an unterminated block end at the first blank line, or at the end of the file.
     The order of those two searches is the correctness argument, not a detail: whether a blank line
     is inside the block or after it is not knowable until the file has been scanned to its end.
  2. **The rule test** (`_is_rule`) is about a line *further down* a document, and it is two rules
     rather than one because CommonMark has two kinds of furniture. A **thematic break** is three or
     more `-`, `_` or `*` with spaces allowed between them, so `***`, `* * *` and `- - -` are breaks
     and the spaces are condensed out before the run is tested. A **setext underline** is a run of
     `=` or of `-` with no interior space and of any length, so `===` and `--` underline a heading
     and `___` does not. Both halves are needed and neither contains the other. Either way a rule is
     the whole line or nothing, so `- a bullet`, `* a bullet` and `--- not a rule ---` stay prose,
     and two characters are not a break, so `**` (literal text) and `* *` (a bullet list whose item
     is `*`) stay prose too. Review round 3 found `*` missing from what was then a single list of
     characters, which published `***` as a world's description — the same defect as publishing the
     card's YAML, one character of a constant away.
  3. **The setext lookahead** (`_is_title_line`) skips a heading spelled without a `#`: `Title` with
     `=====` under it is an `<h1>`. It is scoped to the line that would *start* the paragraph,
     because a rule under a later line ends the paragraph it is part of rather than deleting it.
     It asks `_is_rule` and not `_is_underline`, which is a deliberate deviation from CommonMark:
     `Title` over `___` is strictly a paragraph followed by a break, but it is still a title, and a
     world's title is exactly what this rule set exists to keep out of its description.
  4. **The byte-order mark** is decoded away by `_readme` (`encoding="utf-8-sig"`) and stripped again
     in `_first_paragraph`, which is handed strings by callers and tests as well as by the reader.
     `str.strip()` does not remove it — it is not whitespace — so left in place it stops the first
     line being an opening fence or a heading and publishes `\ufeff---` followed by the card's YAML.

  One consequence is worth stating rather than leaving to be discovered: an unclosed block with a
  rule further down is read as a block closed by that rule, so the description comes from the prose
  *after* the rule rather than before it. The two readings are the same bytes and no rule can
  separate them. What the block skip guarantees is the property that matters — the description is
  prose, never YAML — and `test_first_paragraph` pins which reading is taken.

  **Three of the four rules are review findings, and rule 1 replaces a paragraph in this plan that
  was wrong twice.** Round 1 found the block skip with *no* end: an unclosed fence was one stray line that
  `_is_prose` stepped over, so the keys below it were prose by every rule the function had, and
  `---\ntitle: Notes\nsdk: docker\n\n# ProjectTracker\n\nReal prose here.\n` published
  `description="title: Notes sdk: docker"` — *instead of* the real prose, because the keys started a
  paragraph that the blank line after them ended. Round 2 found the fix for that regressing the
  well-formed case: the blank-line bound and the fence search had been fused into one loop, which
  stops at a blank line while a closing fence is still ahead — and a blank line is legal YAML and
  ordinary in a card, between keys, after the opening fence, inside a list, or as a whitespace-only
  line. The same YAML, published for the opposite reason. Both times the suite was green, because
  not one of the parametrized cases had a blank line inside a *closed* block; nine do now, and
  `test_first_paragraph` runs fifty-four READMEs in all.

  **Round 3 then found that six of round 2's nine no longer discriminate the defect they were
  written for**, which is why three more exist. Rule 3, added in the same round, masks them: with the
  fused loop's bound landing on the blank line inside the block, the key after it is skipped as a
  setext heading — the closing `---` sits directly beneath it — and the fence is then skipped as a
  rule, so the prose is reached anyway and by accident. A case only separates the fused loop from
  the correct one when the line after the block's blank line is *not* directly above the closing
  fence, and the three added for it are a gap before the fence, a tag list behind a gap, and two
  keys behind a gap. Four of the fifty-four now fail on that mutant. The finding is not that the six
  cases are worthless — they pin the answer for six real card shapes — but that regression coverage
  is a claim about a *mutant*, and a claim about a mutant is only worth what running it against the
  mutant says.

  Rules 3 and 4 are round 2's as well, and rule 2 is round 2 generalising a rule round 1 had written
  for the exact string `---` only — and then round 3 generalising it again, because "a run of one
  character" named three of CommonMark's four break characters and left `*` out. All three came from
  the same question asked of the same function: what else does a README open with that is not prose?
  A title underlined with `=====` instead of prefixed with `#`; a `----`, `___` or `***` section
  break; a byte-order mark an editor wrote and `str.strip()` does not remove. None of the three was
  reachable from the defect round 1 reported, which is the argument for asking that question rather
  than only closing what was found — and round 3's finding is the argument for asking it of every
  spelling the format allows, not of the spellings that came to mind.

  The lesson the two rounds share is in the Method notes: a bound that depends on a fact about the
  whole document cannot be evaluated while that fact is still unknown, and a guard whose permissive
  side has no test is not a rule. Every rule above now has a case for the thing it skips *and* a
  case for the thing it must not skip — `One\nTwo\n===\n` for rule 3's scope, `- a bullet list
  item` for rule 2's whole-line requirement, `\ufeffJust prose.\n` for rule 4 stripping the mark
  out of the text rather than tolerating it in front.
- **`reset` destroys before it creates, and clears before it destroys.** `_forget()` sets
  `_instance`, `_episode_id` and `_steps` to their fresh-session values *first* and calls
  `instance.destroy()` last, so a `destroy` that raises still leaves the session in the state a fresh
  one is in rather than holding a half-destroyed instance. A `reset` whose `world.instance(...)`
  fails leaves the same state and the session open for another `reset`, which
  `test_a_failed_reset_leaves_the_session_as_a_fresh_one` pins in process and
  `test_an_unknown_reset_kwarg_is_an_error_frame_and_the_session_survives` pins on the wire.
- **The capacity test reads the raw frame instead of a client's exception.** OpenEnv sends
  `CAPACITY_REACHED` and then closes the connection, so whether a client sees the frame or the close
  first is a race — the first version of this test passed alone and failed about one run in three in
  the file. What the server does is not racy, only what a client observes is, so the test opens a
  websocket with the `websockets` library and asserts on the first frame the server volunteers. Its
  permissive counterpart, `test_under_capacity_a_second_connection_is_served`, asserts that the same
  connection *with* a slot is sent nothing at all: a server that refused every second connection
  would otherwise pass the first test.
- **The gate has a permissive test as well as a restrictive one.** `test_the_gate_serialises_two_sessions`
  sets `concurrency=1` and asserts two slow calls on two sessions do not overlap. On its own that
  passes for the wrong reasons too — a gate that serialised everything, or a server that ran every
  session on one thread, would satisfy it and nothing would notice.
  `test_without_the_gate_two_sessions_run_together` sets `concurrency=2` and asserts the same two
  calls *do* overlap. The measurement is the tool's own `perf_counter()` either side of a recursive
  CTE, and the assertion is about intervals overlapping, never about duration.
- **`serve()` is tested with `uvicorn.run` replaced and `app` wrapped, not replaced.** What `serve`
  adds over `app` is three decisions — the gate is sized *before* the app is built, the operator's
  numbers reach the app, and uvicorn gets one worker and an app *object* rather than an import string
  — and a function that serves until the process is stopped is no way to read any of them. The
  `served` fixture replaces `uvicorn.run` outright and wraps `app`, so the real app is still built
  from the real arguments and the arguments are recorded on the way past. The one-worker rule has a
  test because a second worker process answers a session's second frame with an environment that has
  never seen its first, and nothing in a single-process suite would ever notice.
- **CI now installs `--extra serve`.** Without it every module in this phase is
  `pytest.importorskip`-ed away and `ty` never type-checks the subpackage at all: the phase would
  have been green in CI and untested. The `importorskip` calls stay, because a contributor without
  the extra should get a skip rather than a collection error — and for that to work the modules that
  import `openenv` transitively, `tests/serving.py` among them, have to be imported *below* the
  `importorskip` rather than at the top of the file.

## Tests

135 tests in the framework's suite and 7 in the world's, over four new framework modules, one new
world module, and one test added to `tests/test_instances.py` for the `concurrency()` accessor.
Totals: the framework's suite goes 599 → 734, the world's 72 → 79. Six of those tests
were written because a mutant survived and forty-two because a review round found a surface with no
test on it: four in round 1, twenty-four in round 2, two while round 2 was being closed, twelve in
round 3 and one in round 4. Thirty-five of the forty-three are `_first_paragraph` cases, which is
what it costs to pin four rules, the permissive side of each, and — round 3's lesson — a mutant that
a later rule had stopped the earlier cases from separating. The "Mutation check" section below says
which.

**`tests/test_env.py` (96).** The environment class in process, with no server anywhere: `reset`
makes an instance and a second `reset` destroys the first (the directory is gone); a blank instance
is at the wall clock, `now=` moves it and `now=` with a fixture is refused; `seed` reaches the
instance; startup kwargs reach the hooks and an unknown one raises before any directory exists; a
failed `reset` leaves the session as fresh as a new one; episode ids are the given one or a `uuid4`;
and the object is initialised as an OpenEnv environment, which is to say `transform` and `rubric`
exist on it.
For `step`: a tool list excludes control tools, is answered before any `reset`, and equals
`instance.tools()` once there is one; a call before `reset` raises `WorldBug("reset first")`;
`timeout_s` is accepted and ignored; a `ToolError`, an `UnknownTool` and an `ArgumentError` each
render into `error`; a control tool is callable with the flag and `unknown_tool` without it, in the
same words an unregistered name earns; an unexpected exception becomes the generic observation *and*
is logged with its traceback; a `WorldBug` propagates; a bare `SeahavenError` propagates; a world's
own `ToolError` subclass renders; an action that is neither kind is a `WorldBug`. Then `state` before
and after `reset`, the step count, `close`'s idempotence, `get_metadata` with a README, without one
with an unreadable one and with one an editor saved with a byte-order mark, and `_first_paragraph`
parametrized over fifty-four READMEs. Nineteen of the fifty-four are round 1's, four of those the
unclosed-fence cases it asked for. Round 2 added twenty-three: six with a blank line inside a
*closed* front-matter block (between keys, after the opening fence, inside a tag list, as a
whitespace-only line, after a comment, and with no prose after it at all), the unclosed block with a
rule further down whose reading is decided rather than accidental, two with a blank line before the
card, three with a byte-order mark, and eleven for rules and setext headings — `===`, `----`, `___`,
a title underlined by each, and the five permissive counterparts that keep `- a bullet list item`,
`--- not a rule ---`, `=> an arrow`, `One\nTwo\n===` and `First line\nSecond line\n---` as prose.
Round 3 added twelve. Three are the closed-block cases that actually separate round 2's Major from
its fix — a gap before the closing fence, a tag list behind a gap, two keys behind a gap — because
its original six no longer do. Five are the thematic break spelled the other three ways: `***`, a
title over `***`, and the spaced `* * *`, `- - -` and `_ _ _`. Four are that rule's permissive side:
`**` and `* *` are published as prose because two characters are not a break, and `* a bullet list
item` and `*** not a break ***` because a break is the whole line or nothing.
Then `test_every_declared_field_publishes_a_description`, parametrized over `SeahavenObservation`
and `SeahavenState`: every field either model declares itself must publish the description the
module's table names — a finding made while closing round 2 rather than reported by it, turned
into a rule over the set rather than a fix to the field that was reported.

**`tests/test_client.py` (13).** The three parsers against the frames OpenEnv actually sends,
including one that carries an error, one that the observation model must *refuse* (a `tools`
frame, which is why `list_tools` does not go through it), one that omits the fields a pre-`reset`
state does not have, and one that omits the single field a state may not omit; then the whole client
against a real server in both of the base client's modes, `call` never raising on a tool error,
`list_tools` answering plain dictionaries, and both context managers connecting on the way in,
closing the session on the way out and answering a `SeahavenClient` rather than an `EnvClient`.

**`tests/test_server.py` (17).** Everything over a socket. The §7 flow with the typed client; the
same session with the stock `GenericEnvClient` and no Seahaven on the client side at all; a tool
error reaching the stock client as data with `done=False` and `reward=None`; a `WorldBug` arriving as
a `RuntimeError` error frame with the session surviving it; two sessions that cannot see each other's
writes and have different episode ids; an unknown `reset` kwarg as an error frame followed by a
`reset` that works; a second `reset` starting from the fixture again; control tools with and without
the flag; the two gate tests; the two capacity tests; and the 500-session smoke test, marked `slow`,
which connects, resets and calls on 500 clients at once and checks every answer is the one that
client asked for. Review round 1 added three: a tool whose arguments are called `tool` and `self`,
called through the typed client in both spellings; `GET /schema` read off a real server, asserting that the
observation's published descriptions are the ones its model carries — round 2 found that test
asserting one hard-coded string for one field, and the state cannot be asserted the same way at all
because `/schema` answers the base `State` (B13); and the idle reaper, asserted by the instance
directory disappearing.

**`tests/test_serve.py` (8).** `serve`'s three decisions, described above. Round 4 rewrote the two
gate-size assertions: they read `served["gate"]._initial_value`, and they now read
`instances.concurrency()`, which is the accessor step 8 adds. The behavioural half of "zero removes
the gate" was already in the instances suite and stays there — a test of `serve` is a test of what
`serve` decides, and a test that nothing is gated is a test of the gate.

**`tests/test_instances.py` (+1).** `test_the_gates_size_can_be_read_back`: the accessor answers the
framework default, then each size it is set to, then `0`, and a refused negative resize leaves the
recorded size alone — which is the mutant worth having, because recording before validating is the
natural way to write it wrong.

**`worlds/projecttracker/tests/test_openenv_app.py` (7).** The reference world over the wire, through
the object a container serves — `projecttracker.openenv_app:app`, imported by name. The typed client
resets onto the committed `empty` fixture, lists `ping` and calls it; the stock client reaches the
same world and gets `ping`'s schema and result as dictionaries; this world's own `INVALID_INPUT` and
its `{"field": "message"}` details survive the wire to a client that never imports the world's error
classes; the control tools are not served; two sessions do not see each other; a fresh
interpreter resolves the import string and finds `/ws` on it; and `seahaven.openenv.serve.serve`
itself is run in a subprocess against this world, its port read back from uvicorn's own startup
line, and driven with the typed client — the end-to-end path through the real entry point, which no
test that replaces `uvicorn.run` can cover.

The suites were run under `umask 022` and under `umask 077`: 734 passed and 79 passed under both.

## Mutation check

Every statement of the four modules in `src/seahaven/openenv/` and of the world's
`openenv_app.py` was replaced with `pass` in turn — a multi-line statement replaced as a block —
and the suites that file can affect were re-run on cleared `__pycache__` with
`PYTHONDONTWRITEBYTECODE=1`: both suites for a framework file, the world's for the world file.
`seahaven/instances.py` is deliberately not in that set — it is an earlier phase's module and was
swept there — so the one accessor round 4 added to it is covered by hand mutants instead, which is
the same standard applied in the only way this phase's scope allows. Then
sixty-six mutations statement deletion cannot express — an inverted guard, a reordered `except`,
a changed default, a swapped pair of branches, an off-by-one in a bound, two loops fused into one,
two loops in the wrong order, a hand-written literal where the code builds one, a dropped `/`, a
dropped codec, six dropped field descriptions, a character missing from a set and a character too
many, a length bound moved either way, a condensing step removed, each half of a two-part predicate
dropped in turn, and the component document's own sketch of `call` — were made by hand.

**The numbers below are review round 4's: both sweeps were re-run from scratch over the changed
code, not adjusted.** Every review round so far has changed this file, so every round has recounted:
round 2's findings rewrote `_first_paragraph`, `_after_front_matter`, `_readme` and the observation's
three redeclared fields; closing round 2 turned up three more undescribed fields on `SeahavenState`;
round 3 split the rule test into a thematic-break test and a setext-underline test; and round 4
renamed three helpers and added one accessor to `seahaven/instances.py`. The hand
set has grown with them, thirty-seven → fifty-five → sixty-three → sixty-six. Round 2's eighteen
were two for
the closing-fence-first rule (its precedence, and the two searches in the wrong order), two for the
leading-blank opening scan, two for the byte-order mark — one on each side of it, the codec and the
`lstrip` — three for the rule test, three for the setext lookahead, five for the field descriptions
that had none, and one for a `_is_prose` that keeps only the exact fence out of a paragraph rather
than every rule. Round 3's eight are the two break characters the rule set can lose, the spaces it
must condense, the length bound in both directions, the deliberate deviation in `_is_title_line`,
and each half of `_is_rule` dropped in turn. Round 4's three are the gate accessor's: a resize that
does not record, a record that ignores the resize, and a record written above the guard that
refuses a negative one. Round 4 also *rewrote* two of round 3's, which had been mutating the wrong
character — see the README table.

A sweep is only ever reported as one collection. Round 2's fix pass ran both sweeps twice from
scratch, because its own finding changed `SeahavenState` after the first run, and the first run's
numbers are not reported anywhere; round 3 did the same for the same reason. Earlier rounds' numbers
are quoted for comparison and nothing below is arithmetic on them; each table is recounted from the
harness's own result files, which are also checked for duplicate rows — the shards partition the
work by index, so a mutant appearing twice would mean the partition was wrong. 151 statement rows,
151 distinct; 66 hand rows, 66 distinct.

Every number comes from a harness that proves the mutant is the code that ran. The mutation tree is
a copy of the repository; `PYTHONPATH` puts its `src` directories first, which wins over the editable
installs because those are plain `.pth` path entries; and a pytest plugin loaded into the tree,
running in the same interpreter the tests run in, imports each package the suite depends on at
session start and aborts unless `module.__file__` is inside the tree by absolute path. The guard was
re-proved this round rather than carried over: with the tree's `src` off `PYTHONPATH`, pytest exits
4 with `seahaven resolved to /home/user/Seahaven/src/seahaven/__init__.py, which is outside the
mutation tree /…/mutation/tree0`, so a mutant that silently tested the repository is not a thing
this sweep can report as a kill. Each shard asserts both baselines are green before it counts
anything, and the hand harness aborts with `SystemExit` on a pattern that does not match exactly
once. A statement mutant that does not compile is logged `SKIP (syntax)` and left out of the tally
rather than counted either way — an earlier draft of this section said it aborted, which it does
not; no round's sweep has logged a single skip, which is why the 151 rows, the 151 distinct rows
and the independent AST count of 151 all agree.

**That last rule earned its keep in round 2.** Adding the `/` to `SeahavenClient.call` changed the
line the "call is written as the component document sketches it" mutant patches, and the shard
carrying it stopped with `BAD PATTERN (0 matches)` instead of quietly scoring a mutant it had never
applied. Without that check it would have counted as a kill — the tally and the log would have looked
exactly the same — and the deviation's one piece of evidence would have been fiction. The pattern was
repaired and the whole hand set re-run so that round's 37 numbers were one collection. The same
rule is why round 2's fix pass re-ran its hand set in full twice: the first run's patterns were
written against code that same pass then changed, and a tally assembled from two runs of different
code would be exactly the fiction the rule exists to stop. Round 3 collected the same debt and paid
it the same way — splitting `_is_rule` moved three patterns, and the whole set was re-run rather
than the three.

The statement sweep, recounted from the harness's own result files:

| File | Statement mutants | Killed | After round 2 | Round 2 | Round 1 |
|---|---|---|---|---|---|
| `seahaven/openenv/__init__.py` | 12 | 9 | 9/12 | 9/12 | 9/12 |
| `seahaven/openenv/env.py` | 106 | 102 | 97/101 | 91/95 | 86/91 |
| `seahaven/openenv/client.py` | 20 | 18 | 18/20 | 18/20 | 18/20 |
| `seahaven/openenv/serve.py` | 10 | 8 | 8/10 | 8/10 | 8/10 |
| `projecttracker/openenv_app.py` | 3 | 3 | 3/3 | 3/3 | 3/3 |
| **Total** | **151** | **140** | **135/146** | **129/140** | **124/136** |

`env.py` grows a statement at a time as the README rules do: 91 of 95 killed when round 2 found it,
97 of 101 after round 2's fixes, and 102 of 106 now that round 3's rule split has added the two
break constants, the length bound, the condensing assignment and `_is_thematic_break`'s return —
five new statements, five killed. The survivor count has not moved through any of it: the eleven are
the same eleven, in the same three families, and none of them is in code any round has touched.
Cross-checked against an independent AST count of the same five files — 12, 106, 20, 10, 3 — run
outside the harness, so the harness's own idea of what a statement is does not go unexamined. 151
rows, 151 distinct, no `SKIP (syntax)` in any shard, and each of the three shards logged its
`baseline green` before counting anything. Round 4 changed no statement count — renaming three
helpers moves no line and adds none — and the sweep was re-run from scratch anyway rather than
carried over, which is how that is known rather than assumed: 151/140 again, the same eleven
survivors at the same line numbers.

**What the round-1 sweep found, and what closed it.** Seven gaps, every one in a line that is not a
statement about behaviour so much as a line the tests never looked at from the outside. Three were
the fields of `SeahavenState` — `fixture`, `now` and `world` — indistinguishable from the
`extra="allow"` that `State` gives them for free until a frame *omits* them, which
`test_state_answers_none_for_the_fields_a_pre_reset_frame_leaves_out` and
`test_state_refuses_a_frame_that_does_not_name_a_world` now do. Two were the `super()` calls in
`__enter__` and `__aenter__`: the context-manager test entered the client and never asked whether
entering had connected. One was `super().__init__()` in `SeahavenEnv`, without which `self.transform`
and `self.rubric` never exist. The last was the `break` ending the front-matter block skip, which
needed a README with a horizontal rule further down.

**What review round 1 found that the sweep could not.** `result: Any | None = None` survived and was
recorded as an equivalent, and the record was wrong: the redeclaration drops
`CallToolObservation`'s inherited field description from `GET /schema`, a published surface of the app
this phase builds that neither suite was requesting. No mutation of a line can be killed by a test
that does not exist, and no survivor analysis catches an endpoint nobody reads — which is the
argument for a review with fresh context on top of a sweep, not instead of one.

Eleven statement survivors remain, in three families, all recorded rather than killed:

| Survivor | Why it is not a test's job |
|---|---|
| `__all__` in all four modules | It steers `from module import *`, which nothing in this package, the reference world or the tests does. It is documentation of the module's surface, and the same equivalence phases 1–5 record. |
| the annotation-only imports: `FastAPI` and `World` in `__init__.py`, `World` in `serve.py`, `Instance` in `env.py`, `Any, Self` in `client.py` | Each name appears only in annotations, which PEP 649 never evaluates, so deleting the import changes nothing at runtime. Not equivalent to the gate: each one is `error[unresolved-reference]` from `ty`, which `AGENTS.md` requires before every commit. Confirmed by mutating each of the five in the repository itself and running `ty check .` against a clean baseline — five mutants, five killed, re-run each round rather than carried over. The diagnostics per mutant are 1, 1, 1, 2 and 10, because one import line carries two names and one name is annotated eight times; the claim the row needs is that all five are caught, not that each costs one error. |
| `if instance is not None:` and `return instance.tools()` in `_listing` (two mutants) | The two branches answer the same list, which is the point: `Instance.tools()` *is* `world.tools` with the control tools filtered out, and `test_list_tools_answers_before_a_reset_and_agrees_with_the_instance` asserts the two are equal. A test that could tell them apart would be a test that the branches disagree. The same equivalence is the hand mutant "the instance branch of the listing is dropped" below. |

The sixty-six hand mutations and the test that kills each. Sixty-three are killed; the three
survivors are recorded under the tables. Thirty-nine of the sixty-six — the two whose killing tests
round 1 had to name by hand, every mutant of the README rules, all six of the field descriptions and
all three of the gate accessor's — were re-run one at a time in the repository, with the whole
framework suite and no `-x`, so their entry below is every test that fails and not whichever one
pytest reached first. The rest carry the sharded run's first failure, which is the only one a `-x`
run records.

Where a mutation is killed by `test_first_paragraph`, the count is of the fifty-four parametrized
READMEs, named by what they are rather than by a truncated pytest id. **Every count in these tables
is read off that run's own per-mutant failure list; none is reasoned from the mutation.** That
sentence is here because round 4 found two that were: one claiming 6 where the named mutant kills 3
(the mutant had been written to drop the wrong character), and one claiming "all 54 but the empty
one" where the run says 42. Both are corrected in place below, with what went wrong, rather than
quietly renumbered — the counts are this phase's only instrument for the decay round 3 found, so a
count that was written rather than counted is a broken instrument and not a typo.

The environment's behaviour:

| Mutation | Killed by |
|---|---|
| `ListToolsAction` is answered only after a reset | `test_list_tools_answers_before_a_reset_and_agrees_with_the_instance` |
| the step count is taken only on a tool call | `test_every_step_counts_and_reset_starts_again_from_zero` |
| the world-derived listing keeps the control tools | `test_list_tools_answers_before_a_reset_and_agrees_with_the_instance` |
| the instance branch of `_listing` is dropped | **survives** — equivalent, see below |
| `SeahavenError` is caught after `Exception`, so a `WorldBug` becomes the generic error | `test_a_world_bug_propagates_out_of_step`, `test_a_seahaven_error_that_is_neither_propagates`, `test_a_world_bug_reaches_the_client_as_an_error_frame` |
| the generic error is written out as a two-key literal instead of built from `ToolError` | `test_an_unexpected_exception_becomes_the_generic_error_and_is_logged` |
| a refused control tool says it was refused rather than that it is unknown | `test_a_control_tool_is_unknown_without_the_flag` |
| the control-tool guard is inverted | `test_a_control_tool_is_unknown_without_the_flag` |
| `SUPPORTS_CONCURRENT_SESSIONS = False` | `test_the_client_drives_a_world_synchronously` |
| `reset` destroys the old instance *after* making the new one | `test_a_failed_reset_leaves_the_session_as_a_fresh_one` |
| the state reports no fixture | `test_state_after_reset_carries_the_fixture_and_the_clock` |

The four README rules — the front-matter block, the rule test, the setext lookahead and the
byte-order mark. Every mutant here except the two recorded survivors is a review round's:

| Mutation | Killed by |
|---|---|
| the README is read from the package directory rather than beside the fixtures | `test_get_metadata_reads_the_readme` |
| **the blank-line bound is tested before the closing fence is ruled out** (round 2's Major, as a mutant: the fix as round 1 wrote it) | `test_first_paragraph`, 4 cases — a gap before the closing fence, a tag list behind a gap, two keys behind a gap, and the unclosed block with a rule further down. **Round 3 found this row naming a case that does not discriminate it**: five of round 2's six closed-block cases put the block's blank line directly above the closing fence, where rule 3 reaches the prose by a second route, so the first three cases above were written for this mutant and against the masking. |
| **the two searches run in the wrong order** (blank line first, closing fence second) | `test_first_paragraph`, the same 4 cases — the failure mode of one pass and of the wrong order is the same failure mode |
| **an unclosed front-matter block is treated as one stray fence** (round 1's Major, as a mutant) | `test_first_paragraph`, 5 cases — the four unclosed-fence cases round 1 asked for and the card behind a leading blank line |
| **an unclosed front-matter block swallows the whole document** (`return len(lines)` where the blank line should bound it) | `test_first_paragraph`, 2 cases |
| the fence search starts at line 1 rather than past the opening fence | `test_first_paragraph`, 1 case — the card behind a leading blank line |
| the opening fence has to be line 0 | `test_first_paragraph`, 1 case — the same one |
| **the end-of-file bound leaves the last front-matter line behind** (`len(lines) - 1`) | `test_first_paragraph`, 2 cases — the unclosed fence that runs to the end of the file, with two keys and with one |
| front matter is skipped one line short (`return index` at the closing fence) | **survives** — equivalent, see below |
| the closing fence's index runs one past the end (`len(lines) + 1`) | **survives** — vacuous, see below |
| **the rule test keeps only the setext-underline half** (thematic breaks become prose) | `test_first_paragraph`, 7 cases — `***`, `___`, the spaced `* * *`, `- - -` and `_ _ _`, and the two titles over `***` and `___` |
| **the rule test keeps only the thematic-break half** (setext underlines become prose) | `test_first_paragraph`, 3 cases — `====` bare, `Title` over `=====`, and `One\nTwo\n===` |
| **the break set loses the asterisk** (round 3's finding, as a mutant) | `test_first_paragraph`, 3 cases — `***`, `* * *`, and the title over `***` |
| **the break set loses the underscore** | `test_first_paragraph`, 3 cases — `___` bare, the spaced `_ _ _`, and the title over `___`. **Round 4 found this row reading 6, and the mutant it named dropping the wrong character:** the pair had been written as `"-*"` and `"-="`, so one dropped the underscore under the asterisk's name and the other dropped both and added `=`, which is where the extra three kills came from. Both are now `"-_"` and `"-*"`, one character each, and re-run. |
| **the spaces are not condensed out of a break** (`* * *` stops being `***`) | `test_first_paragraph`, 3 cases — the spaced `* * *`, `- - -` and `_ _ _` |
| **a break may be one character long** (the length bound removed) | `test_first_paragraph`, 2 cases — both permissive: `**` is literal text and `* *` is a bullet list whose item is `*` |
| a break needs four characters | `test_first_paragraph`, 7 cases |
| **the underline test subscripts before it has ruled out the empty line** (the two conjuncts swapped) | `test_first_paragraph`, 42 cases, plus `test_get_metadata_reads_the_readme` and `test_get_metadata_reads_a_readme_that_begins_with_a_byte_order_mark` — it raises `IndexError`, but only from `_is_title_line`'s lookahead past the end of the document: `_is_prose` tests `bool(stripped)` first and never hands the underline test an empty string, so twelve documents never reach it with one. **Round 4 found this row claiming 53** — "all 54 but the empty one", which was reasoned from the mutation rather than read off the run. |
| **a line that merely begins with a rule character counts as a rule** | `test_first_paragraph`, 3 cases — all three permissive: `- a bullet list item`, `--- not a rule ---`, `=> an arrow, not an underline` |
| **only dashes underline a heading** | `test_first_paragraph`, 3 cases — the `=` underlines |
| a rule counts as prose | `test_first_paragraph`, 14 cases |
| only the exact front-matter fence is kept out of a paragraph (`stripped != "---"` for the rule test) | `test_first_paragraph`, 12 cases |
| a heading counts as prose | `test_first_paragraph`, 9 cases, plus `test_get_metadata_reads_the_readme` and `test_get_metadata_falls_back_when_the_readme_has_no_paragraph` |
| **a title underlined by a rule is published as the description** (the setext lookahead dropped) | `test_first_paragraph`, 5 cases — `Text.\n---`, and a title underlined by `=====`, by `-----`, by `___` and by `***` |
| **the setext lookahead is not scoped to the paragraph's first line** | `test_first_paragraph`, 2 cases — the permissive pair, `One\nTwo\n===` and `First line\nSecond line\n---`, whose second line is *not* a heading being underlined |
| the setext lookahead reads the line itself rather than the next one | `test_first_paragraph`, 5 cases |
| **the setext lookahead asks only about a setext underline** (the deliberate deviation, as a mutant) | `test_first_paragraph`, 2 cases — a title over `___` and a title over `***`, which CommonMark calls paragraphs and this rule set calls titles |
| **the byte-order mark is left on the front of the file** (`readme.splitlines()` for `readme.lstrip(BOM).splitlines()`) | `test_first_paragraph`, 3 cases — a BOM before a card, before a heading and before prose |
| **the reader hands the byte-order mark on as text** (`utf-8` for `utf-8-sig`) | `test_get_metadata_reads_a_readme_that_begins_with_a_byte_order_mark` |

The six fields this project declares on models OpenEnv publishes, each mutated by dropping its
`description` — which is the defect review rounds 1, 2 and 3 each found one field further on:

| Mutation | Killed by |
|---|---|
| **the `result` field loses the description it publishes** (round 1's finding) | `test_every_declared_field_publishes_a_description[SeahavenObservation]`, `test_the_served_schema_publishes_the_observation_with_its_descriptions` |
| **the `tool_name` field loses the description it publishes** (round 2's) | `test_every_declared_field_publishes_a_description[SeahavenObservation]`, `test_the_served_schema_publishes_the_observation_with_its_descriptions` |
| **the `error` field loses the description it publishes** (round 2's) | `test_every_declared_field_publishes_a_description[SeahavenObservation]`, `test_the_served_schema_publishes_the_observation_with_its_descriptions` |
| **the state's `fixture` field loses its description** (found while closing round 2) | `test_every_declared_field_publishes_a_description[SeahavenState]` |
| **the state's `now` field loses its description** (found while closing round 2) | `test_every_declared_field_publishes_a_description[SeahavenState]` |
| **the state's `world` field loses its description** (found while closing round 2) | `test_every_declared_field_publishes_a_description[SeahavenState]` |

Each of the three observation fields is killed twice over, in process and off a live `GET /schema`;
each of the three state fields is killed once, in process only, because `/schema` answers the base
`State` for every environment there will ever be and no test here pins upstream's defect as
Seahaven's contract (B13).

The app, the client, `serve` and the reference world:

| Mutation | Killed by |
|---|---|
| the app always exposes the control tools | `test_control_tools_are_unknown_without_the_flag` |
| the app never exposes the control tools | `test_control_tools_are_callable_with_the_flag_and_never_listed` |
| the default session budget is one | `test_serve_passes_the_module_defaults_when_it_is_told_nothing` |
| the idle reaper is off by default | `test_serve_passes_the_module_defaults_when_it_is_told_nothing` |
| **the app never arms the idle reaper** (`session_timeout=None` into the `ConcurrencyConfig`) | `test_an_idle_session_is_reaped_and_its_instance_destroyed` |
| the observation is parsed from the whole frame rather than from `observation` | `test_parse_result_answers_a_typed_observation` |
| `list_tools` reads the tools list off the wrong level of the frame | `test_the_client_drives_a_world_synchronously` |
| every step frame is marked `done` | `test_parse_result_carries_an_error_through` |
| `call` is written as `components/openenv.md` §5 sketches it | `test_the_client_drives_a_world_asynchronously` |
| **the client's tool name is a keyword parameter again** (the `/` dropped from `call`) | `test_a_tool_argument_called_tool_is_callable_through_the_client` |
| **only the outer `call` makes the tool name positional** (the `/` dropped from `_call_async`) | `test_a_tool_argument_called_tool_is_callable_through_the_client` |
| the gate is sized *after* the app is built | `test_serve_sizes_the_gate_before_the_server_starts` |
| `concurrency=0` is read as "no answer" rather than as zero | `test_serve_with_a_concurrency_of_zero_removes_the_gate` |
| the server binds to loopback by default | `test_serve_runs_one_worker_on_an_app_object` |
| the server is asked for more than one worker | `test_serve_runs_one_worker_on_an_app_object` |
| the server logs nothing below critical | `test_serve_runs_one_worker_on_an_app_object` |
| the reference world serves its control tools | world suite: `test_control_tools_are_not_served_by_this_worlds_app` |

The gate accessor round 4 added to `seahaven/instances.py`, mutated in the same way even though the
statement sweep's five files do not include it — an untested addition is an untested addition
wherever it lands:

| Mutation | Killed by |
|---|---|
| `set_concurrency` resizes the gate and does not record the new size | `test_the_gates_size_can_be_read_back`, `test_serve_sizes_the_gate_before_the_server_starts`, `test_serve_with_a_concurrency_of_zero_removes_the_gate` |
| the accessor answers the framework default rather than the size that was set | `test_the_gates_size_can_be_read_back`, `test_serve_sizes_the_gate_before_the_server_starts`, `test_serve_with_a_concurrency_of_zero_removes_the_gate` |
| **a refused resize records the size it refused** (the record moved above the guard) | `test_the_gates_size_can_be_read_back` |

Some rows carry more than their own weight. The component document's own sketch of `call` is a
mutation that **passes the synchronous test** and is killed only by the asynchronous one, which is the
evidence that the deviation recorded above is load-bearing rather than a preference. Round 1's Major
and round 2's Major both reappear here as mutants that parametrized cases now kill, where before
each fix it was the code — and round 2's is the more interesting of the two, because the mutation
*is* round 1's fix, so the row is a record that the second fix strictly contains the first. Three
rows are killed **only** by *permissive* cases, which is the other half of every boundary the rule
set draws: "a line that merely begins with a rule character counts as a rule" dies on `- a bullet
list item`, `--- not a rule ---` and `=> an arrow, not an underline`; "a break may be one character
long" dies on `**` and `* *`; and "the setext lookahead is not scoped to the paragraph's first line"
dies on `One\nTwo\n===` and `First line\nSecond line\n---`. Seven cases, not one of which any
*stricter* reading of a rule would catch. A rule set with only the restrictive half is the failure
mode `AGENTS.md` names, and it is the mirror of round 3's finding: `***` published as a description
is the restrictive half missing a character, and `* *` eaten as furniture would be the permissive
half missing a bound.

The three survivors:

| Survivor | Why |
|---|---|
| the instance branch of `_listing` is dropped | The same equivalence as the two statement survivors above: both branches answer the same list, by a test that says so. |
| front matter is skipped one line short (`return index` rather than `index + 1` at the closing fence) | The paragraph scan then starts *on* the closing `---`, and `_is_prose` refuses a bare rule, so the fence is skipped one line later and the paragraph is the same. A genuine equivalence rather than a gap: this branch returns only at a line that is a fence, and a fence is exactly what the scan already discards. Two independent rules cover this one line, and that redundancy is deliberate, because the rule test exists for a `---` anywhere in a document and not for this position. Checked rather than argued, and re-checked after round 3 changed the rule test: the mutant was compiled beside the real function and run over all fifty-four parametrized READMEs and over every document of up to five lines drawn from a twelve-element alphabet of lines — fences, an indented fence, blanks, a whitespace-only line, a key, a heading, prose, `===`, `___`, `***`, `* * *` and `**` — 54 and 271,453 documents, zero differences. |
| the closing fence's index runs one past the end (`len(lines) + 1`) | **A vacuous mutant, recorded rather than quietly dropped.** The index is used as the start of `range(start, len(body))`, and that range is empty for every `start >= len(body)`, so `len(lines) + 1` and `len(lines)` cannot be told apart by any input — the same 271,453 documents differ on none. It was written to probe the end-of-file bound and probes nothing; `len(lines) - 1`, two rows above it in the same table, is the mutation that actually tests that bound, and two cases kill it. The lesson is the mutant's, not the code's: a mutation whose two sides are equal by the language's own semantics is not evidence about the tests. |

One of the fifty-four `_first_paragraph` cases — a closing fence on the very last line, with nothing
after it — is worth naming for what it does *not* do. No mutation of `_after_front_matter`
distinguishes it, because there is no document after the fence to get right or wrong. It is a bounds
case: it says the index does not run off the end and the function does not raise, which is a
property no mutant in this set can express and which review round 1 was right to ask for anyway.

## Follow-up, not this phase

- **B11 is not closed by this phase, and `serve` cannot close it alone.** A built world wheel ships
  no `fixtures/` directory, so a world installed rather than checked out can only be run blank —
  which means `seahaven serve` on an installed world serves a world whose every fixture-backed eval
  fails at `reset`. Nothing in `components/openenv.md` gives `serve` or `app` a fixtures argument,
  and inventing one here would be inventing spec: the fix is a packaging decision (a
  `force-include` in the world's `[tool.hatch.build]`, or `fixtures/` moved inside the package), and
  it belongs with the `--hub` image in Phase 7 and with whatever `seahaven new` writes. Recorded
  rather than guessed, and B11's owner line — which named Phase 6 — is corrected there to say why
  this phase could not be the one to close it.
- **`components/openenv.md`'s four inaccuracies are filed as B12, not fixed.** The self-contradiction
  on tool listing, the two-key generic error, and the `call` sketch — twice over, once for
  `self.step(...).observation` and once for the missing `/` on the tool name, which review round 1
  added — are each worked around here and recorded above; the artifact is `status: complete`, so
  amending it is the same maintainer's call that B2, B7 and B8 are waiting on.
- **§2's "first paragraph of README" is filed as B14.** The phrase is four rules once §6 also says
  that README is the Space card, and the four are written and tested here; the component document
  states it as a phrase, which is the only line in it that a reader would not know was
  under-specified. Three review rounds were spent on two of the four, so B14 asks for a sentence in
  §2 rather than leaving the next world server to start from the naive reading. B14 now spells the
  rule test as the two CommonMark rules it is, because "a run of one rule character" is exactly the
  shorthand that left `*` out.
- **The README rules want their own module, and that is B16 rather than this diff.**
  `_first_paragraph` and its five helpers are about 130 lines and a large share of `env.py`'s 106
  statements — a small CommonMark reader inside the module whose job is the server side of the wire.
  Four review rounds found four defects and every one of them was in this block; none was in
  `reset`, `step`, `state`, `close` or the client. `seahaven/openenv/readme.py` with its own suite is
  the obvious shape, and the move is mechanical. It is filed because `components/openenv.md` §1
  names the subpackage's module list, so adding a module changes what a `status: complete` artifact
  describes — the same maintainer's call as B2, B7, B8 and B12. What this phase did instead is the
  part that needs no permission: the block's names now read as one family, and every rule in it has
  a mutant with a counted kill list.
- **The `serve` extra is installed in CI and not licence-gated, which is filed as B15 and not fixed
  here.** `scripts/check_licences.py` evaluates markers with `{"extra": ""}` and says in its own
  docstring that it covers "what `pip install seahaven` pulls in, extras excluded" — correct while
  every extra was a development tool, and no longer the whole story now that this phase has made
  `serve` a runtime extra that CI installs with `uv sync --locked --extra serve`. Its closure is not
  permissive-only under a strict reading of `AGENTS.md`: MPL-2.0 (certifi, orjson, tqdm), CC0-1.0
  inside numpy's licence expression, MIT-CMU (pillow). None of that is copyleft-viral for running a
  server process, so the finding is a *policy* gap rather than a licence problem, and closing it
  means either deciding that an opt-in extra needs no gate or choosing a wider allowed set — a
  project-wide decision that would fail the gate the day it landed. Widening the checker inside a
  phase whose diff is one subpackage, or loosening its allowed set to keep it green, is precisely
  the change that should be its own review. What this phase owes is that the gap is written down.
- **Three OpenEnv behaviours are filed as B13, and none has a fix that belongs here.** `GET /state`
  answers `{"episode_id": null, "step_count": 0}` — FastAPI serialises the route's `response_model=State`
  and drops every field `SeahavenState` declares, and the route is not session-bound either, so a
  harness reading state over HTTP learns nothing about the world it is driving. `GET /schema`'s state
  block has the same root cause one endpoint over: `create_app` takes no state class and
  `get_schemas` answers `State.model_json_schema()`, so the published state schema describes
  `episode_id` and `step_count` for every environment there will ever be. And every clean
  client disconnect logs `ERROR: Exception in ASGI application` with a traceback, because OpenEnv's
  `/ws` handler lets `WebSocketDisconnect` escape and §4 has `serve` run at `log_level="info"`. The
  first two are deliberately *not* pinned by a test: asserting the base-model answer would make
  upstream's defect Seahaven's contract. The second could be filtered from inside `serve`, and B13 says why
  suppressing another library's error logs is a decision rather than a tidy-up.
- **The `openenv push` layout (§6) and `cli/serve.py` (§4) are Phase 7's**, along with
  `test_serve_cli.py` and `test_push_layout.py` from §8's test plan. §4's step 4 — the
  `ImportError` message `seahaven serve needs the serve extra` — is the CLI's, not `serve.py`'s, and
  is not implemented here.
- **A per-world typed client, with one method per tool generated from the registry**, is named in §5
  as a later release and is not started.

## Method notes

- **A permissive test is half of every restrictive one, and the gate and the capacity budget each
  needed theirs.** "With one slot the calls do not overlap" is satisfied by a server with no
  concurrency at all; "over the budget the server refuses" is satisfied by a server that refuses
  everything. Both restrictive tests here were written first and both were passing before their
  counterparts existed, which is exactly the failure mode `AGENTS.md` describes: an untested
  permissive boundary is not a rule.
- **A test that asserts on who won a race is a flaky test, not a strict one.** The capacity test
  failed about one run in three and passed every time it was run alone. The mistake was asserting on
  a client's exception when the thing being tested is what the *server* sends; the frame is
  deterministic and the client's view of it is not. Reading the socket directly made the test both
  stricter and stable.
- **`importorskip` guards nothing if the import it guards is above it.** Three test modules imported
  `tests/serving.py` — which imports `openenv` transitively — before their own `importorskip` line,
  so without the extra the suite would have failed to collect rather than skipped. Found by writing
  the CI change and then asking what the skip actually protects.
- **A test written to kill a survivor has to be run against that survivor.** The six tests this
  phase added for mutants were not filed as kills on the strength of the reasoning that produced
  them: a separate pass rebuilt the mutation tree from the repository as it then stood, asserted both
  baselines green, re-applied all nineteen surviving mutations one at a time and reported which were
  now dead. Seven were — two of them the `super()` calls, which one new test covers and one
  strengthened test covers, and the strengthened one is exactly the case where reasoning would have
  been wrong about which test did the work. Twelve survived that pass; eleven survive the round-2
  re-sweep, which is the number the table above reports. The pass also re-checks that each survivor's
  recorded line still reads what the sweep recorded, and aborts if it does not, so a stale survivor
  list cannot be re-reported as a fresh result.
- **Never signal a mutation harness that edits the repository; wait it out.** The `ty` check and the
  name-every-killing-test pass mutate `src/` rather than a copy, and each restores its file in a
  `finally` — which covers a failing mutant and not a `SIGTERM`. One was killed by `pkill` during
  round 3 and left the fused-loop mutation of `_after_front_matter` in the working tree. It was
  caught by the next `pytest` run, where exactly the four cases written to discriminate that mutant
  failed and nothing else did, which is a reassuring way to find out and a bad one to rely on: the
  file is *untracked* in this phase, so `git checkout` could not have restored it, and a green run
  would have hidden it. Finishing with such a harness means waiting for it, then `git status` and a
  full suite — not reaching for a signal when it is slow. Filed as a project-wide method note in
  `BACKLOG.md` as well, because it is not specific to this phase.
- **A mutant's name is a claim about its replacement, and the harness does not check it.** The
  "pattern must match exactly once" rule catches a mutation that no longer applies. It says nothing
  about whether the replacement does what the row calls it, and round 4 found two that did not:
  "the break set loses the asterisk" dropped the *underscore*, and "the break set loses the
  underscore" dropped both and added `=`. Both mutants were real, both died, and both kill counts
  were honest — but the count reported against the second was 6 where the mutant its *name*
  described kills 3, which is how the row was caught. Mislabelled mutants are worse than missing
  ones: they read as coverage of a rule that nothing is actually probing (here, `*` on its own), so
  the pair was rewritten and re-run. **The check that was missing is arithmetic against the table:
  a kill count has to be reachable from the cases that exist**, and "six documents in this table
  contain a `_`" is a question anyone can ask of a fifty-four-row list. Two of the sixty-six rows
  failed it.
- **A regression test can stop testing the regression, and only the mutant will say so.** The six
  cases written for round 2's Major all still pass, all still describe real card shapes, and none of
  them fails on the defect any more. Rule 3 — the setext lookahead, added in the same round for an
  unrelated reason — reaches the prose by a second route whenever the block's blank line is
  immediately above the closing fence, which is where five of the six put it. Nothing about the
  suite looked wrong: the tests were green before the fix for the reason they were written, and
  green after it for a different reason. What found it was running the mutant, which is the point:
  **"this test covers that defect" is a claim about a mutant, and it decays silently as the code
  around it grows.** Two practices follow. A case added for a mutant gets its mutant re-run when any
  *other* rule in the same function changes — the phase's hand set is the ratchet that makes that
  cheap. And when a mutant's kill count drops (here from six cases to one), that is a finding even
  though the number is still above zero, because the one that is left may be incidental: the single
  surviving case here was an unrelated document about which *reading* of an unclosed block is taken,
  so a later narrowing of rule 3 would have re-opened round 2's Major with a green suite.
- **A survivor that only the type checker can kill has to be run against the type checker.** Five of
  the eleven are annotation-only imports. Calling them "killed by `ty`" is a claim, and the claim was
  checked the way the tests are: clean baseline first, then one mutation at a time in the repository
  itself — `ty` resolves the project through the editable install, so a copied tree would be checking
  the repository anyway — each restored in a `finally`, and `git status` inspected afterwards. Five
  mutants, **five killed**, none of them silent — and the count is of *mutants*, deliberately, not of
  diagnostics. The per-mutant diagnostic totals are 1, 1, 1, 2 and 10: deleting `from typing import
  Any, Self` costs ten references (two `Self` and eight `Any`) and each `World` costs one. Round 3
  reported that "one each" did not reproduce, this bullet was rewritten to say so, and **round 4
  found "one each" still standing eleven lines further down the same bullet** — the phase's own
  signature failure mode, in the paragraph about that failure mode, for the fourth time. Round 3's
  rewrite also had the attribution backwards, blaming the ten on `World`. Both are corrected here,
  and the lesson is the one the rest of this section keeps re-learning: **a claim is not fixed where
  it was quoted, it is fixed everywhere the file makes it**, which means grepping for the number
  rather than editing the sentence the reviewer pasted.
  Round 3 also re-ran the pass, and the "pattern must match" rule paid for itself a second time:
  this harness addresses its lines by *number*, and round 2's fix had pushed `env.py`'s `Instance`
  import from line 38 to line 39, so the run stopped with `BAD PATTERN
  src/seahaven/openenv/env.py L38: 'from seahaven.errors import ...'` rather than deleting an
  `except` clause and reporting a `ty` error that had nothing to do with the survivor it claimed to
  be testing. The general shape is worth keeping: **a harness that names code by position must
  assert the position still holds what it names**, and a mutation harness gets that assertion for
  free only if someone writes it. Corrected to line 39 and re-run in rounds 3 and 4: five mutants,
  five killed, every one of them an `error[unresolved-reference]`.
- **A bound that depends on a fact about the whole document cannot be evaluated before that fact is
  known.** Round 1's Major was a block skip with no end; the fix gave it two ends — a closing fence,
  or a blank line — and wrote them as one loop, which is where round 2 found it. Written that way
  the loop asks "is this a blank line?" while the answer that decides whether a blank line means
  anything ("does a closing fence exist?") is still ahead of the cursor. Two ends in one pass is a
  *precedence* question, and precedence between a local test and a global one can only be settled by
  doing the global one first. The shape the fix needs is two passes, and the give-away that one pass
  cannot work is that the earlier test's meaning depends on the later test's result. Worth carrying
  forward as a smell: **a loop whose first condition is only correct when the second never fires.**
- **The same fix, one field over, three times.** Round 1 found `result`'s redeclaration dropping
  its published description; the fix added a description to `result` and a test asserting that one
  string. Round 2 found `tool_name` and `error` — redeclared in the same class, by the same
  mechanism, for the same reason — still publishing nothing, so the fix had closed the demonstrated
  case again; round 2's fix then described all three and asserted over
  `SeahavenObservation.__annotations__`, which felt like the rule. It was the rule for one class.
  Round 3 found `SeahavenState`'s three declared fields, on the same model surface, described by
  nobody. What finally makes it a rule is a test parametrized over *both* models that reads each
  one's declared fields off the class and requires a description for every one of them. The general
  form: **when a defect is "this member of a set is wrong", the test has to assert over the set —
  and the set is as wide as the mechanism, not as wide as the report.** The mechanism here is
  "a field this project declares on a model OpenEnv publishes", and the first two fixes both drew
  the set narrower than that.
- **Writing the assertion is how the third one was found, and it found an upstream defect too.** The
  round-3 gap was not spotted by reading the class. It was spotted by trying to extend the wire test
  to the state schema and watching it fail with `assert 'State' == 'SeahavenState'` — `create_app`
  takes no state class, so `/schema` answers `State.model_json_schema()` for every environment, and
  `SeahavenState`'s fields cannot reach a client at all. Two findings from one assertion: three
  undescribed fields, and a third face of B13. The note is that **an assertion written to cover an
  adjacent case is worth writing even when you expect it to pass** — the failure told us something
  neither the code nor the component document did.
- **A brute-force invariant is a claim too, and the first one here was false.** The new
  `_first_paragraph` was checked against 916,500 generated documents — 171,500 with a well-formed
  front-matter block (nothing between the two fences may reach the output), 245,000 with an
  unterminated one (nothing between the opening fence and the first blank line may), and 500,000
  unconstrained (the output is never a heading, never a rule, never carries a byte-order mark) —
  with zero violations. Round 3's rule split is in that run: the unconstrained family's line
  alphabet carries `***` and `* * *`, so "never a rule" now means the new break characters too. Both
  this run and the two equivalence runs were repeated after round 4's renames — a rename should
  change nothing and the cheapest way to say so is to re-run rather than to reason — and the three
  numbers came back identical: 916,500 with zero violations, 54 and 271,453 with zero differences.
  **Two attempts at the invariant were wrong before this one, in the same way, and both were caught
  by the checker firing on a correct answer.** The first asserted that no line *looking like*
  `title: Notes` may ever be published (82,264 violations), which is false for a document that
  simply begins with that sentence. The second — written this round — asserted that no key from an
  unterminated block's generated key list may be published (11,340 violations), which is false for
  `---\n\ntitle: Notes\n`: the rules end an unterminated block at the first blank line, so a key
  *below* that blank line is outside the block and is the document's first paragraph, exactly as
  recorded. The invariant that holds computes its forbidden set the way the rules define the block,
  which is the general lesson: **an invariant over a function's output has to be derived from the
  same definition the function implements, or it is a second, unreviewed implementation** — and one
  that fires on a correct answer has to be narrowed before its silence means anything.
- **"The fix is a rule, not a patch" is a claim about the code, and this phase got it wrong once.**
  The plan's own front-matter bullet argued the fix was general — two rules that each close what the
  other does not — and concluded "neither spelling can put YAML in a card". The demonstrated case was
  closed; the adjacent one, a card whose keys sit *below* the unclosed fence, was not, and the plan's
  confident wording is what stopped anyone looking. What the first version was missing is not another
  rule but a *bound*: a block skip with no end is not a block skip. The tell, in hindsight, is that
  the passing parametrized case (`---\ntitle: Notes\n` → `title: Notes`) asserted a value nobody
  would have chosen on purpose — a test that pins an output the docstring calls "worse than no
  description at all" is a test agreeing with the bug.
- **A sweep cannot find a gap on a surface no test names.** `result: Any | None = None` survived
  round 1's statement sweep and was recorded as an equivalent, with a reason that was true about
  validation and false about the schema the app publishes. Nothing was wrong with the sweep: the
  mutant changes only `GET /schema`, and no test requested `GET /schema`, so there was no test for it
  to fail. A survivor analysis reasons about the tests that exist, which makes it blind in exactly
  the places a fresh reading of the endpoint list is not — an argument for review on top of mutation
  testing rather than instead of it. The general form, worth carrying forward: **before recording a
  survivor as equivalent, enumerate the surfaces the line changes, not the tests it passes.**
- **A fix applied to one entry point is not a rule until its sibling has it.** `Instance.call` made
  the tool name positional-only in an earlier phase, for a reason that applies word for word to
  `SeahavenClient.call`; the client was written from the component document's sketch instead, and a
  world with a tool argument called `tool` had a tool no harness could invoke. The fix was two
  characters. Finding it needed someone to ask which *other* function takes a tool name beside
  `**arguments` — which is the question this phase's recurring failure mode ("the fix closes the
  demonstrated case and leaves the adjacent one") looks like when the adjacent case is in a different
  file.
- **Check the spec against the library before calling a spec line wrong.** Two of this phase's
  deviations (list-tools before reset, `call` through `_dispatch`) are only visible by reading
  OpenEnv's own code: the `/mcp` handler that steps a `ListToolsAction` without a reset, and
  `EnvClient._dispatch`'s dual mode. A component document written against a library is a claim about
  that library, and the library is the thing to check it against.
