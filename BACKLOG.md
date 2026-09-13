# Backlog

Known issues that are real but out of scope for the phase that found them.

Phases are one reviewable unit each, so a defect found in phase N's review that belongs to earlier
code does not get fixed by widening phase N's diff. It lands here instead, with enough detail to act
on without re-deriving it.

**What belongs here:** a defect, test gap or spec inaccuracy in code or artifacts that are already
committed, found while working on something else. **What does not:** work the implementation plan
already schedules (that is the plan's job), or anything in the diff currently under review (that is
the review's job).

Each item names where it was found, so the finding can be traced back to the review that produced
it. Close an item by deleting it in the commit that fixes it.

---

## Open

### B1. `db.py` connection hardening is untested

**Found:** Phase 3 code review, round 4 — a clean-bytecode mutation sweep of 912 statements across
phases 1–3. **Owner:** unassigned. **Risk:** security-relevant; not a known regression.

`src/seahaven/db.py` was written in Phase 1 and never mutation-tested. Each of these can be deleted
with the entire suite still green:

- `db.py:257` — `_harden(conn)` in `open_inspection()`. Nothing proves the inspection connection
  gets `foreign_keys`, `DEFENSIVE`, `TRUSTED_SCHEMA=0` or extension loading off.
- `db.py:342` — `conn.enable_load_extension(False)` inside `_harden()`. No test proves extension
  loading is refused on any connection.
- `db.py:360-363` — the `SQLITE_PRAGMA` clause and its `return apsw.SQLITE_DENY` in
  `_deny_writes()`. The read-only-pragma denial on the *inspection* authorizer has no test.
  `tests/test_sandbox.py` exercises `sandbox.py`'s authorizer, a different code path — it does not
  cover this one.

Two further survivors need judgement rather than tests, and should be either pinned or recorded as
equivalent mutants in the style the phase plans use: `db.py:245` `conn.set_busy_timeout(0)`, and
`db.py:172, 233-234` `cursor.close(force=True)` and the helper close.

The hardening is believed correct — it is unproven, not broken. If any of it turns out to be
actually broken, that is a finding to report, not to quietly fix.

### B2. `components/runtime_db.md` §1.2 states something false about APSW

**Found:** Phase 1 implementation. **Owner:** unassigned. **Risk:** low now, rises with each phase.

§1.2 claims APSW returns a bare value for single-column rows. That is false on the pinned
`apsw>=3.53` floor: the bare value comes from `Cursor.get`, which `Db` never uses. The code is
right and the deviation is recorded in `phase_plans/phase_1.md` with a tripwire test.

The artifact is marked `status: complete` and still carries the claim, where a later-phase
implementer will read it as the spec. Left unedited because editing a completed artifact cascades
its dependents to `draft`; the fix needs a maintainer's call on whether to take that cascade or to
annotate the section in place.

### B3. Working directories from the bare-`<pid>` layout are never swept

**Found:** Phase 3 code review, round 5. **Owner:** unassigned. **Risk:** low — a disk leak, no
correctness or security consequence.

Phase 3 round 4 changed the default working directory from `<tempdir>/seahaven-<uid>/<pid>/` to
`<tempdir>/seahaven-<uid>/<pid namespace>-<pid>/`, because `os.kill(pid, 0)` answers in the
caller's pid namespace and two containers sharing a `<tempdir>` were sweeping each other's live
instances.

`instances.py:_pid_of` matches a directory name only when it starts with this namespace's prefix, so
on a machine where procfs exists a directory left by an earlier build — a bare `999999/` — returns
`None` and is stepped over. Nothing else removes it either: the sweep is the only thing that ever
deletes a working directory it did not create. Verified: a bare-numeric directory is still PRESENT
after a sweep. The behaviour is deliberate and safe (an unprefixed name cannot be judged against
this namespace's pids), but it silently leaks disk for anyone who ran an earlier build.

A fix would sweep a bare-numeric sibling too, but only where the name can be judged safely: where
`/proc/self/ns/pid` is unreadable the prefix is empty and bare names *are* this layout, so the
condition is "procfs exists, the name is bare numeric, and the pid is not alive" — which is exactly
the cross-namespace hazard the prefix was added to remove, and is therefore a decision about a
one-off migration, not a rule to add to the sweep. A one-shot cleanup at the root, or documented
`rm -rf`, is the likelier answer.

### B4. `world.name` is an unvalidated path component

**Found:** Phase 3 code review, round 6. **Owner:** unassigned. **Risk:** low — not attacker
reachable; a world's name is written by the world's author and never arrives off the wire.

The default working directory is `<tempdir>/seahaven-<uid>/<namespace>-<pid>/<world name>/`.
`instances.py:_open_child` hardens the *lookup* of each component — `O_NOFOLLOW`, an owner check,
`dir_fd` — and assumes the component it is given is a single path segment. `World.__init__` in
`world.py` never checks that. `O_NOFOLLOW` is no help here: `..` is not a symlink.

Verified by making a real instance of each:

- `World("..")` puts the instance directly in the working root, beside the per-process directories.
- `World("../..")` puts a live `state.sqlite` **outside the working root entirely**, where the sweep
  will never see it — a permanent leak of the world's data into `<tempdir>`.
- `World("../escaped")` lands inside the root but outside any per-process directory, likewise
  never swept.
- `World("a/b")` and `World("")` are refused, but only incidentally, as `ENOENT` wrapped in the
  working-directory `WorldBug`.

The fix is validation shaped like `fixtures.check_id` — one path segment, no leading dot, not `.`
or `..` — in `World.__init__`, refusing with a `WorldBug` that names `World(name=...)`. It is
recorded here rather than fixed in Phase 3 because the *use* is Phase 3's but the *validation*
belongs in `world.py`, which is Phase 2's and already committed. A world name is also likely to end
up in more than a path (a log line, a sidecar, a URL in Phase 6/7), so the rule belongs where the
name is accepted.

### B5. The default working root is created `0o777` and only then tightened

**Found:** Phase 3 code review, round 6. **Owner:** unassigned. **Risk:** low — hardening, not a
hole; every consequence of winning the window is refused by something else.

`instances.py:_make_instance_dir` does `root.mkdir(parents=True, exist_ok=True)` and then
`os.fchmod(root_fd, 0o700)` on a checked descriptor. `mkdir`'s default mode is `0o777`, masked by
the umask, so under `umask 000` the root exists world-writable between the two calls (confirmed:
mode at creation `0o777`, mode after the `fchmod` `0o700`). Anything planted in that window is
refused when it is used — `_open_child` checks owner and `O_NOFOLLOW` at every level, which is what
round 5 and round 6 verified with a second uid — so this is depth, not a live defect.

`root.mkdir(mode=0o700, parents=True, exist_ok=True)` costs nothing and makes the ceiling `0o700`
instead of `0o777` for the window. Note that the docstring's "`mkdir`'s mode is masked by the umask,
so it is a ceiling and not a setting" argument is the reason the `fchmod` exists, not a reason to
leave the ceiling at `0o777`; whoever makes this change should extend that paragraph to say both are
used and why.

### B6. The descriptor-anchored final `mkdir` is claimed by a docstring and pinned by no test

**Found:** Phase 3 code review, round 6. **Owner:** unassigned. **Risk:** low — harmless on today's
code; a test gap around a structural property.

`instances.py:_make_instance_dir` ends with `os.mkdir(instance_id, 0o700, dir_fd=world_fd)`, and its
docstring says the instance directory is created relative to the checked `<world>` descriptor rather
than composed as a string and resolved again. Replacing that line with a composed-path
`(root / process / world / instance_id).mkdir(0o700)` survives the entire suite under both umasks.
It is harmless today because every component above it has just been checked and is `0o700` and ours,
so there is nothing to redirect the composed path — the same position the `S_ISDIR` guard in the
sweep is in, where the structural property was pinned rather than the line dropped.

Two ways to close it, and the choice is the point: stage the swap (rename the checked `<world>`
directory away and leave a symlink at its name between the `_open_child` and the `mkdir`, in the
monkeypatch style `test_the_sweep_removes_through_the_descriptor_it_judged_through` uses) and assert
the instance is not made through the link; or soften the docstring to claim only what is tested.
Pinning it is the better answer if the anchoring is meant to survive later edits.

### B7. The FTS5 recipe does not work as written, in both artifacts that state it

**Found:** Phase 4 implementation. **Owner:** unassigned. **Risk:** low — the code is right and the
phase plan records it; a later reader of the artifact would follow the recipe and be stuck.

The table in §4 tells a world that wants `MATCH` through `run_sql` to list the FTS5 table's shadow
tables and "add `bm25`, `snippet`, `highlight` to the function allowlist". A world that does exactly
that gets `not allowed: function 'match'`: SQLite asks the authorizer about the `MATCH` *operator*
under the function name `match`, which the sentence omits. With `match` added it then gets
`action PRAGMA 'data_version'`, because FTS5 reads that pragma while preparing the statement —
which §4 does not mention at all, and which no spelling of the allowlists in the published API could
have allowed.

Phase 4 closed both in code: `sandbox.ALLOWED_FUNCTIONS`'s comment names `match` as the fourth
function to pass, and `sandbox._PRAGMAS` allows the `PRAGMA data_version` question (never its
assignment form) with the reasoning in place; `tests/test_fts5.py` drives the whole recipe through a
real world and pins both refusals a world that lists too little gets. What is left is the artifact:
§4 is `status: complete` and still carries the three-function sentence, and says nothing about the
pragma. Same shape as B2, and the same call to make — editing a complete artifact cascades its
dependents to `draft`, so whether to take that cascade or annotate in place is a maintainer's.

**Two artifacts, not one.** `components/runtime_db.md` §4 carries the same three-function sentence
("FTS5 auxiliary functions (`bm25`, `snippet`, `highlight`) are not in the default list"), so the
`match` omission is in both places and a fix to one leaves the other. `runtime_db.md` is also where
the pragma claim lives, and that side of it is B8.

### B8. Three `complete` artifacts describe the control path the Phase 4 Critical replaced

**Found:** Phase 4 code review, round 2. **Owner:** unassigned. **Risk:** the
`helpers_and_control.md` entry is the highest in this register: a later-phase implementer who
follows it as written reintroduces an interpreter-wide deadlock.

Phase 4's round-1 review raised a Critical: `controller_run_sql` ran `sandbox.run_statement` on the
`inspect()` handle, which the spec documents as readable *without* the instance lock. Two threads on
one connection, one setting the authorizer while the other steps a cursor, wedge inside SQLite — and
take the interpreter with them, because the thread waiting on the connection holds the GIL. The fix
gave the control tools a second read-only handle of their own (`Instance._control_db()`), opened once
and closed with the instance. Three committed artifacts still describe the connection that was
replaced, or facts that followed from it:

- `components/helpers_and_control.md` §3: "thin wrappers over `Instance.inspect()` and
  `Instance.changes()`", and "`controller_run_sql` runs on the instance's inspection `Db`
  (`Instance.inspect()`, opened on first use)". Both false now, and false in the direction that
  reintroduces the deadlock. A **third** sentence in the same section, at `:98-100`, states the
  `RLock`'s reason exactly as `fixtures_instances.md` §2.4 does: "the reads it then makes through
  the `inspect()` handle do not take it, which is why the lock is a `threading.RLock` and why the
  first control call does not deadlock." It is listed here because the next bullet attributes that
  sentence to the other file alone, and an amendment worked from this list would fix two sentences
  here and leave the third — which is this register's own recurring finding, that a fix closes the
  demonstrated case and leaves the adjacent one.
- `components/fixtures_instances.md` §2.4: the control-dispatch bullet repeats the same claim; the
  `RLock`'s stated reason ("the reads it then makes through the `inspect()` handle do not [take the
  lock]") has changed — the conclusion still holds, but the reason is now same-thread re-entry, a
  control tool asking the instance for its changeset and its control handle with the lock already
  held; and the `destroy()` bullet's list of what is closed ("close inspection, session, db") is
  missing the fourth handle.
- `components/runtime_db.md` §4: an `Authorizer` "carries per-statement mutable state (`refusals`)"
  is now incomplete — it also carries `_wrote_a_row`, whose scope is the call, not the statement.
  §5's test plan says "`ATTACH`/`PRAGMA` refused", which is contradicted for `PRAGMA data_version`
  (see B7) and needs a sentence for the changeset session's `table_xinfo` allowance.

Everything else in those sections holds word for word — one statement, positional params, no
authorizer beyond the connection's permanent write denial, no caps, `run_sql`'s result shape,
SQLite's message, and the lock-free `inspect()` reads, which are now true where they were not. All
three are `status: complete`, so this is the same call B2 and B7 leave open: editing a completed
artifact cascades its dependents to `draft`, and whether to take that cascade or annotate in place
is a maintainer's. The reasoning behind each change is recorded in
`specs/projects/seahaven_framework/phase_plans/phase_4.md`.

### B9. SH405 as specified fires on every fresh clone of a world that commits a fixture

**Found:** Phase 5, freezing the reference world's `empty` fixture. **Owner:** unassigned.
**Risk:** `seahaven check` (Phase 7) reports an error on a correct world; a world author's first
`check` after cloning their own repository fails.

`components/cli_and_check.md` §3 defines SH405 as "state file not read-only or has `-wal`/`-shm`
companions", and `components/fixtures_instances.md` §1 has `freeze` `chmod 0o444` the state file.
Both are right about the file `freeze` writes. Neither survives version control: git records only
the executable bit, so `fixtures/<id>/state.sqlite` comes out of a clone with whatever the umask
gave it — `0o644` under the usual `umask 022` — and the read-only half of SH405 reports an error on
a fixture that is byte-for-byte the one that was frozen.

The information SH405 wants is not lost, it is just not in the mode: the sidecar's `file_sha256`
(SH402) and `schema_hash` (SH403) already prove the bytes are the frozen ones, and the journal-file
half of SH405 is genuine and unaffected. Options, for whoever owns the lint: drop the mode check;
keep it as a warning rather than an error; or keep it as an error but only for a file the *running*
process froze, which in practice means dropping it. Nothing about `freeze` should change — a live
instance must not be able to write a fixture in place.

This affects Phase 7 (the lint) and Phase 10 (three fixtures instead of one).
`worlds/projecttracker/tests/test_empty_fixture.py::test_the_state_file_carries_no_journal_or_lock_file_beside_it`
asserts the half that survives a clone and says in its docstring why it does not assert the mode.

### B10. `architecture.md` says every name outside `__init__` is internal, and the code does not

**Found:** Phase 5, writing the reference world's `middleware/error_handler.py` — the first
middleware written outside the framework's own tests. **Owner:** unassigned. **Risk:** ergonomic,
and a stated rule that contradicts the shipped code; it already misled this phase.

This is a conflict inside a `complete` artifact, not a blank to fill in. `architecture.md:68` says,
of the list of names `seahaven/__init__.py` re-exports:

> Everything else is internal. `Tool.from_function` is part of that public surface: it is how an
> extension builds a tool. `seahaven.sandbox` is public too — `Authorizer`, `run_statement`,
> `SqlResult` and the refusal names, which are a documented `Literal` and are stable.

Read as written, the first sentence is the rule and the two that follow are its complete set of
exceptions: `Tool.from_function`, and `Authorizer`, `run_statement`, `SqlResult` and the refusal
names in `seahaven.sandbox`. Anything else a world imports from a `seahaven.*` module — the sentence
says — is internal.

The component documents then list wider interfaces, and the code implements them. `world_and_dispatch.md`
§1 gives `Handler` and `Middleware` beside the `World` they describe, and `seahaven/world.py` duly
re-exports both from `call.py` in its `__all__`, with a comment naming exactly the world-author case;
`fixtures_instances.md` §1 gives `load`, `load_all`, `verify` and `freeze` in `seahaven.fixtures`.
None of those are in `__init__`, and none are named as exceptions at `architecture.md:68`. So a world
author who reads §1 and believes it concludes that `from seahaven.world import Handler` reaches into
a private module, and declares a local copy of the type instead. That is what this phase's first
draft did, and the copy is exactly the drift a world should not carry.

Closing it means **amending `architecture.md` §1**, which is `complete`; a phase should not widen it
on its own judgement, and adding a sentence elsewhere would leave line 68 still saying the opposite.
The amendment is to replace the blanket "Everything else is internal" plus its two hand-listed
exceptions with the rule the codebase actually follows — *a name a component document's §1 lists as
part of a module's interface is public; `seahaven/__init__` re-exports only the subset worth a short
import* — under which `Tool.from_function` and the `sandbox` names stop being exceptions and become
instances. Separately, `Handler` and `Middleware` are strong candidates for that convenience subset:
a typed middleware is the ordinary case, not an advanced one, and `seahaven new`'s `middleware/`
template is where every world author meets it. Phase 5's world imports them from `seahaven.world`
under the rule above, and its `middleware/error_handler.py` docstring says why.

### B11. A built world wheel ships no fixtures, so `instance(id)` fails from an install

**Found:** Phase 5, building `worlds/projecttracker` and installing the wheel into a clean
environment. **Owner:** Phase 6 (`serve`) and Phase 7 (the `--hub` image). **Risk:** a world
installed rather than checked out can only be run blank; every fixture-backed eval fails at startup.

A world's `fixtures/` directory sits at the project root, beside `src/`, which is where `freeze`
writes it and where `World`'s `fixtures_dir` default finds it by walking up to the `pyproject.toml`.
A wheel has no project root. `uv build --project worlds/projecttracker` produces a wheel holding
`projecttracker/` and nothing else — the schema travels because `schema/*.sql` is *inside* the
package and `sql_files` reads it through `importlib.resources`, but `fixtures/` is outside it and is
simply absent. Installed into a clean venv, `projecttracker.world.fixtures()` is `[]` and

```
world.instance("empty")
WorldBug: world 'projecttracker' has no fixture 'empty' in .../site-packages/fixtures;
freeze one, or name the directory with World(fixtures_dir=...)
```

while `world.instance()` — blank, from the schema — works. The error message is good and the
`fixtures_dir=` escape hatch exists, so nothing here is broken as specified. What is missing is a
statement of which way a world is meant to be deployed — and `functional_spec.md` §2.2 half-makes
one already: `fixtures_dir` falls back "to `fixtures/` beside the package where there is none, as in
an installed wheel", which says the installed case was thought about and leaves open how the
directory gets there.

Three ways to close it have been looked at, and **none of the three is a one-line change**; this
entry records what each actually costs rather than proposing a fix.

*Move `fixtures/` inside the package* (`src/projecttracker/fixtures/`) and read it through
`importlib.resources` as the schema already is. This is the only shape that also survives a zipped
wheel, but it is **framework work, not a world's directory move**: `fixtures_dir` is not a
`Traversable` anywhere in the framework. `World.__init__` coerces whatever it is given with
`Path(fixtures_dir)` (in `World.__init__`, `src/seahaven/world.py:117` as this phase leaves it),
and `seahaven.fixtures` is `Path`-typed
throughout — `Fixture.dir` and `Fixture.state_path`, `load`, `load_all`, `verify`, `freeze` and the
copy that makes an instance all take or return `Path`, and `verify` hashes files off the filesystem.
`importlib.resources.files()` hands back a `Traversable`, which `Path(...)` rejects for a zip member.
Closing it this way means deciding whether fixtures are read through `Traversable` (and how `freeze`,
which writes, fits a read-only abstraction), which is a fixtures-component change.

*Keep the layout and have the build carry the directory* — hatchling's `force-include`, one line in
the world's `pyproject.toml`, which `seahaven new` would then render. This does build and install,
but it lands `fixtures/` **at the top of `site-packages`**, not under the package: the directory has
no package to be inside, so there is nowhere namespaced to put it. Two installed worlds that each
scaffolded an `empty` fixture then write to the same `site-packages/fixtures/empty/`, and the second
install silently overwrites the first — breaking *both* worlds, since each then loads the other's
state. That makes it unusable as the default a template renders, whatever it is worth for a single
world pinned in its own venv.

*Say plainly that a served world is a checkout and never a wheel.* This is defensible — `architecture.md`
§6's `serve` runs from a world directory — and it is the only one of the three that costs nothing to
state. But it needs saying explicitly, because Phase 7's `--hub` `Dockerfile` does `pip install .[serve]`
and would otherwise decide the question by accident, in the direction the first two paragraphs show
does not work.

Phase 5 changed nothing: the layout is what `functional_spec.md` §2.1 specifies, and the choice is
not a placeholder slice's to make.

**Phase 6 did not close it either, and can be struck off the owner line.** `components/openenv.md`
gives neither `app(world, ...)` nor `serve(world, ...)` a fixtures argument, so there is nothing in
this component's spec for `serve` to pass: a world reaches the server already built, and where its
fixtures live was decided before `serve` saw it. What Phase 6 adds is the consequence — a served
world that is an install rather than a checkout answers every fixture-backed `reset` with the
`WorldBug` above, over the wire, to an agent that cannot do anything about it. The choice is Phase
7's, where the `--hub` `Dockerfile` makes it whether or not anyone writes it down.

---

### B12. `components/openenv.md` contradicts itself on tool listing, and its two code sketches are wrong

**Found:** Phase 6 implementation. **Owner:** unassigned. **Risk:** the §5 sketch is the higher of
the two — it is copied verbatim into the per-world client that the same section schedules for a
later release, and it is broken in exactly the mode a training harness runs in.

Three statements in an artifact that is otherwise accurate line by line. Each is recorded with what
the code does instead and why, in
`specs/projects/seahaven_framework/phase_plans/phase_6.md`; none of them was worked around silently.

- **§2 `:61` and `:71` cannot both hold.** `:61` says `ListToolsAction` → `ListToolsObservation(tools=instance.tools())`
  is "checked first", which needs an instance; `:71` says "A `step` before `reset` raises
  `WorldBug("reset first")`". OpenEnv's own `/mcp` `tools/list` handler steps a `ListToolsAction` on
  a session that has never been reset, and MCP's contract is that discovery does not require one, so
  a literal reading makes every standard MCP client fail against every Seahaven world. The code
  answers the listing before the guard and derives it from `world.tools` when there is no instance,
  with a test pinning that the two derivations agree. Whichever way a maintainer settles it, one of
  the two sentences has to go.
- **§2 `:67` writes the generic internal error with two keys**, `{"code": "internal", "message": "internal error"}`,
  where `architecture.md` §6 defines the wire shape of every error as `{"code", "message", "details"}`.
  Architecture wins on a cross-component shape, and a client that reads `error["details"]` should not
  have to special-case the one error a world did not write. The code builds it through
  `ToolError.to_dict()` so that it cannot drift from the others.
- **§5 `:146` sketches `call` as `self.step(CallToolAction(...)).observation`.** `EnvClient.step` is
  dual-mode: in asynchronous code it answers an awaitable, and `.observation` on an awaitable is not
  an observation. The sketch is right about the signature and wrong about the mechanism; `call` and
  `list_tools` go through `EnvClient._dispatch`, which is how the base client produces a value in
  synchronous code and an awaitable in asynchronous code from one method. This is not a theoretical
  reading: Phase 6 ran the sketch as a hand mutation, and it passes the synchronous end-to-end test
  and fails only the asynchronous one. §5 is also silent on `__enter__`/`__aenter__`, which
  `EnvClient` annotates as returning `EnvClient`, so `with SeahavenClient(...) as env` type-checks
  as a value with neither `call` nor `list_tools` until the subclass narrows them — worth a sentence
  wherever the first is fixed.
- **§5 `:146` also writes the tool name as an ordinary parameter, `def call(self, tool, **arguments)`.**
  `Instance.call` is `def call(self, name: str, /, **arguments)` — positional-only, deliberately, so
  that `**arguments` can carry an argument the world happened to call `name`. A world may equally
  call one `tool`, or `self`; without the `/` such a tool lists, works through
  `step(CallToolAction(...))` and raises `TypeError: got multiple values for argument 'tool'` through
  the documented convenience, which is a tool no harness can call. Found in Phase 6's code review,
  round 1, and fixed in the code there with a test; the sketch should grow the `/` wherever §5 is
  next touched, since a per-world generated client written from it would reintroduce the same hole
  for every world.

`components/openenv.md` is `status: complete`, so this is the same call B2, B7 and B8 leave open:
editing a completed artifact cascades its dependents to `draft`, and whether to take that cascade or
annotate in place is a maintainer's.

---

### B13. Three OpenEnv behaviours a Seahaven world cannot fix from its own side

**Found:** Phase 6 code review, rounds 1 and 3. **Owner:** unassigned — upstream, or a Seahaven
workaround if upstream will not move. **Risk:** the log one is the higher: it makes a 500-session
server's log useless for finding real errors, which is the log an operator reaches for first.

All three were reproduced against a real server; none is in Seahaven's code, and none has a fix
that belongs inside this framework as it stands. The first two are one root cause at two endpoints:
OpenEnv uses the base `State` type where the environment's own subclass was meant.

- **`GET /schema` publishes the base `State`, so a client never sees the state model it is
  driving.** `create_app` takes an action class and an observation class and no state class, and
  `get_schemas` answers `state=State.model_json_schema()`
  (`openenv/core/env_server/http_server.py:1451` in 0.4.2), so the state block of `/schema` describes
  `episode_id` and `step_count` and nothing else — for every environment, whatever its state
  declares. Same root cause as the bullet below, one endpoint over: the base type is used where the
  environment's own was meant. Found in Phase 6 code review, round 3, by a test written to assert
  that `SeahavenState`'s field descriptions reach a client; they cannot, so that half of the rule is
  asserted in process instead and `/schema`'s state block is deliberately not pinned — asserting it
  would make upstream's answer Seahaven's contract. A fix upstream is one parameter.
- **`GET /state` strips every field the environment's state declares.** Over the websocket a state
  frame carries `world`, `fixture`, `now`, `episode_id` and `step_count`; over HTTP the same server
  answers `{"episode_id": null, "step_count": 0}`. OpenEnv's HTTP route is annotated
  `response_model=State`, so FastAPI serialises the base model and drops every subclass field, and
  the route is not session-bound in the first place, so the numbers it does answer are a fresh
  environment's rather than any session's — confirmed against a live session that had reset and
  stepped once: the websocket answered `step_count: 1` and a real episode id while HTTP answered
  `null` and `0` at the same moment. A harness that reads state over HTTP therefore sees
  nothing about the world it is driving. Nothing in Phase 6 pins this, deliberately: a test asserting
  the two-key answer would pin upstream's defect as Seahaven's contract. The websocket path — which
  is the path `SeahavenClient` and every eval use — is fully tested.
- **Every clean client disconnect logs `ERROR: Exception in ASGI application` with a traceback.**
  OpenEnv's `/ws` handler calls `await websocket.close()` on a connection the client has already
  closed (`openenv/core/env_server/http_server.py:1694` in 0.4.2) and lets the resulting
  `starlette.websockets.WebSocketDisconnect` escape into uvicorn's ASGI error path. `components/openenv.md` §4 has `serve` run at `log_level="info"`, so every session
  that ends normally leaves a traceback in the log. Workarounds, none of them free: a `logging`
  filter installed by `serve` (which would have to match on uvicorn's logger and the exception type,
  and would hide a real error of the same shape), running at `warning` (which loses the startup line
  that tells an operator the port, and contradicts §4), or an upstream `try/except` around that one
  `close`. Recorded rather than chosen, because filtering another library's error logs from inside
  `serve` is a decision with a blast radius, not a tidy-up.

---

### B14. "First paragraph of README" is four rules, and `components/openenv.md` states it as a phrase

**Found:** Phase 6 code review, rounds 1, 2 and 3. **Owner:** unassigned. **Risk:** low as a
defect, high as a time sink — the phrase cost three review rounds and is the only line of the
component document that a reader would not know was under-specified.

`components/openenv.md` §2 `:79` says the metadata's `description` is the "first paragraph of README
or `f"Seahaven world {name}"`". §6 `:180` then says that same `README.md` is the Space card. A Space
card does not begin with a paragraph: it begins with YAML front matter between `---` fences, usually
followed by a heading. So the phrase has to be read as four rules, and Phase 6 wrote all four:

- front matter is skipped as a block — closing fence searched for across the whole file *first*, and
  only if there is none does the block end at the first blank line or at the end of the file;
- a line that is furniture rather than prose is not the description, and "furniture" is two rules
  and not one: a thematic break is three or more `-`, `_` or `*` with spaces allowed between them,
  and a setext underline is a run of `=` or of `-` with no interior space. A rule is the whole line
  or nothing, so `- a bullet` and `--- not a rule ---` stay prose, and `**` and `* *` stay prose
  because two characters are not a break;
- a line with furniture under it is a heading, skipped as `# Title` is, scoped to the line that
  would start the paragraph;
- a UTF-8 byte-order mark is decoded away, because `str.strip()` does not remove it.

A sentence in §2 should also say what a heading is, because Seahaven's rule and CommonMark's differ
by a space: any line starting with `#` is furniture here, while CommonMark's ATX heading needs a
space (or the end of the line) after the run of `#`, so `#1 priority is shipping.` is a paragraph
there and skipped here. The effect is a fallback description (`f"Seahaven world {name}"`) and never
a wrong one, which is why Phase 6 recorded and pinned it rather than widening the rule — see that
phase's plan. A reader of §2 should know it, since the deviation is conservative by luck rather than
by design.

Each rule exists because the naive reading published something worse than no description: the card's
own YAML, a `---`, a `***`, or the world's title. Three review rounds were spent on two of the
rules — round 1 on a block with no end, round 2 on a well-formed block with a blank line in it, and
round 3 on `*`, the one thematic-break character the rule test did not name. The implementation and
the reasoning are in `specs/projects/seahaven_framework/phase_plans/phase_6.md`; fifty-five
parametrized cases and just under a million generated documents pin them.

Worth a sentence in the component document wherever §2 is next touched, because the next world
server written from that phrase will start from the naive reading. `components/openenv.md` is
`status: complete`, so this is the same maintainer's call as B2, B7, B8 and B12.

---

### B15. CI installs the `serve` extra, and the licence gate does not cover it

**Found:** Phase 6 code review, round 3. **Owner:** unassigned. **Risk:** low today, and the
decision is a policy one rather than a code one.

`scripts/check_licences.py` evaluates dependency markers with `{"extra": ""}` and says so in its own
docstring: it gates "what `pip install seahaven` pulls in, extras excluded". That was the right
scope while every extra was a development tool. Phase 6 made `serve` a *runtime* extra — CI now runs
`uv sync --locked --extra serve` because the OpenEnv server tests need it — so there is now an
installed, shipped-to-users closure that no gate looks at. Its licences are not all in the
MIT/Apache-2.0/BSD set `AGENTS.md` names: MPL-2.0 (certifi, orjson, tqdm), CC0-1.0 inside numpy's
licence expression, and MIT-CMU (pillow). None of those is copyleft-viral for linking a server
process, which is why this is recorded rather than fixed.

Two things have to be decided together, and both are a maintainer's call:

- **Does an extra's closure need a licence policy at all?** An extra is opt-in and not part of
  `pip install seahaven`, so a defensible answer is "no, and the docstring already says so".
- **If it does, which policy?** Widening `check_licences.py` to every extra would fail the gate
  today on the four licences above, so the change is not a one-line marker edit: it needs an
  allowed-set decision (permissive plus MPL-2.0 and CC0-1.0, say) or a per-extra scope.

Not fixed in Phase 6 on purpose: the phase's diff is the OpenEnv subpackage, and quietly widening a
project-wide gate — or quietly loosening its allowed set to keep it green — is the kind of change
that should be its own review. What Phase 6 does owe is that the gap is not invisible, which is this
entry.

---

### B16. The README rules are a quarter of `seahaven/openenv/env.py` and belong in their own module

**Found:** Phase 6 code review, round 4. **Owner:** unassigned. **Risk:** low as a defect, real as a
maintenance shape — every defect this phase's reviews found was in this one block.

`_first_paragraph` and its six helpers — `_after_front_matter`, `_is_rule`, `_is_underline`,
`_is_thematic_break`, `_is_title_line`, `_is_prose` — are about 130 lines and a large share of
`env.py`'s 106 statements. What they implement is a small CommonMark reader: front matter as a
block, thematic breaks, setext underlines, a byte-order mark. What the module they live in is *for*
is the server side of the wire: sessions, actions, observations, state. The block is there because
`get_metadata` needs a one-line description, which is a one-line need answered by a hundred and
thirty lines of someone else's format.

Four review rounds found four defects, and all four were in this block: an unbounded block skip
(round 1), that fix regressing the well-formed case (round 2), `*` missing from the break set
(round 3), and two miscounted kill rows for its own mutants (round 4). Round 5 found no defect in
the code and two more faults in its record: a kill count read off a mutant narrower than the row
describing it, and the block's one accidental deviation from CommonMark (a heading is any line
starting with `#`) with neither a case nor a note — see B14. None of them was in `reset`,
`step`, `state`, `close` or the client. That is not a coincidence about difficulty so much as about
*locality*: the rules are the only part of this module that is a parser, and a parser wants its own
file, its own suite and its own name.

The move is small and mechanical — `seahaven/openenv/readme.py`, `_first_paragraph` re-exported or
imported by `env.py`, and `tests/test_readme.py` taking the fifty-five parametrized cases with it.
It is filed rather than done because `components/openenv.md` §1 names the subpackage's module list,
so adding a module changes the surface a `status: complete` artifact describes. Same maintainer's
call as B2, B7, B8 and B12, and worth pairing with whichever of those is answered first.

---

### B17. `beartype` breaks the `serve` extra on Python 3.14

**Found:** Phase 7 implementation. **Owner:** unassigned. **Risk:** high for anyone running the
OpenEnv tests; it is an environment and lockfile problem, not a defect in Seahaven's code.

`import seahaven.openenv` fails on a clean `uv sync --locked --extra serve` under Python 3.14:

```
openenv.core.env_server.mcp_environment -> from fastmcp import Client
  -> fastmcp.client -> ... -> beartype.typing
  -> ImportError: cannot import name 'ByteString' from 'collections.abc'
```

`collections.abc.ByteString` was removed in Python 3.14. The `beartype` the lock resolves believes
the removal was deferred to 3.17 (`_IS_PYTHON_AT_MOST_3_16`) and imports it unconditionally, and
`fastmcp` turns the resulting `ImportError` into "FastMCP client support is not installed", which
names the wrong cause. The effect is that `tests/test_client.py`, `tests/test_env.py`,
`tests/test_serve.py`, `tests/test_server.py` and `tests/test_cli_serve.py` all fail to *collect*:
they guard with `pytest.importorskip("openenv")`, which succeeds, and then import
`seahaven.openenv`, which does not.

Two things to decide, and both are a maintainer's call because both touch `uv.lock`:

- **Which version of the closure works on 3.14.** A `beartype` release that knows about 3.14, or an
  `openenv`/`fastmcp` that does not reach `beartype.typing` on the import path, or a lower bound on
  one of them. None was reachable from the sandbox this was found in, which only had 3.14.0rc2 and a
  fixed index.
- **Whether the guard should be `seahaven.openenv` rather than `openenv`.** `importorskip("openenv")`
  asks whether the extra is installed; what these modules need is whether it *works*. Changing the
  guard would turn five collection errors into five skips, which is honest about an optional extra
  but would also hide exactly this breakage.

Related, and found the same way: the same lock's `pydantic` (2.13.5) fails on Python **3.14.0rc2**
with `AssertionError` inside `eval_type_backport`, so `import seahaven` itself does not work on that
interpreter; 2.12.3 does. That one is rc-only and should disappear against a final 3.14, which is
what CI installs, so it is recorded here as context rather than as work.

### B18. The pytest plugin's two-marker guard sees only the test's own node

**Found:** Phase 8 code review, round 2, verified by running it. **Owner:** unassigned. **Risk:**
low: a test silently runs against a fixture other than the one a reader would pick, in a spelling
nobody writes on purpose.

`src/seahaven/pytest_plugin.py`'s `_refuse_two_markers_on_one_test` counts the `seahaven` markers in
`request.node.own_markers`, which refuses two decorators on one test. The same ambiguity a level up
is not refused:

```python
pytestmark = [pytest.mark.seahaven(fixture="empty"), pytest.mark.seahaven(fixture=None)]
```

`get_closest_marker` resolves that to the **first** of the two, which is the opposite of the
decorator case the guard does refuse (there the lower one wins), so the two spellings of "two
markers" disagree with each other as well as being silent. Scanning `own_markers` over
`node.listchain()` rather than the item alone would refuse both; `iter_markers` still must not be
used, because a module's `pytestmark` plus one marker on a test is the legitimate override and is
tested as such (`tests/test_pytest_plugin.py::test_a_marker_on_a_test_overrides_the_modules`).

Not fixed in Phase 8 because the fix belongs with a test of the module-level case and the phase's
diff was under review; it is a residual of a defect that review found, not a new one.

Smaller, found beside it: `tests/test_docs.py::test_every_page_index_md_links_to_is_a_page_of_the_layout`
matches every `](...)` target in `index.md`, so the first external `http` link Phase 12 adds to that
page fails a test about the docs *layout*. Filtering to targets that are not `http` would make the
test say what it means.

---

## Method notes

Standing practice discovered the hard way; kept here because it changes how findings above are
verified.

- **Mutation testing needs clean bytecode.** CPython invalidates a `.pyc` on (size, whole-second
  mtime), so a byte-length-identical mutation — a statement reordering, most often — written and run
  inside the same second silently executes the *original* bytecode and reports a false survivor.
  Clear `__pycache__` and set `PYTHONDONTWRITEBYTECODE=1`. Found in Phase 3; the audit it prompted
  re-ran every recorded survivor in phases 1–3 and found no concealed kills, so the existing records
  stand.
- **Verify end-to-end, through the real entry point.** Every defect that has cost a review round on
  this project passed its unit test and failed on a real call. Phase 4's second-worst defect — a
  denied authorizer call latching `SQLITE_AUTH` into the changeset session and voiding
  `Instance.changes()` for the life of an instance — was found this way and by nothing else: the
  unit tests were green, because the session had already been shown every table a world's own
  fixtures seeded.
- **A mutation harness that edits the repository must never be killed; it must be waited out.** The
  harnesses that mutate the working tree rather than a copy — the `ty` survivor check, and the pass
  that names every test a mutant kills — restore each file in a `finally`, which protects against a
  failing mutant and not against a signal. Phase 6 killed one by `pkill` and left a mutated
  `_after_front_matter` in `src/`, where it survived until the next `pytest` run and would have
  survived into a commit if that run had been a green one. Two rules follow: wait for such a harness
  instead of signalling it, and make `git status` plus a full suite run part of finishing with one,
  not part of debugging it. A file the harness owns that is *untracked* — as a new phase's source
  is — cannot be recovered with `git checkout`, which is what makes this worth a note rather than a
  shrug.
- **"Randomized order" is not something this repository can claim without bringing a shuffler.**
  No randomization plugin is installed, so `uv run pytest` runs collection order every time and
  `-p no:randomly` disables a plugin that is not there. Phase 6 reported "three randomized orders"
  that were three passes of one order. Order-independence is worth checking — it is what the
  autouse isolation fixtures exist for — and the cheap way is a `pytest_collection_modifyitems`
  plugin kept outside the tree and loaded with `-p`, seeded from the environment: no dependency, no
  lockfile change, and a command a reader can repeat. Note too that a mutant of an isolation
  fixture may only be visible in *some* orders (dropping Phase 6's gate restore fails in five of
  seven), so a single green shuffled run is not evidence a fixture is redundant.
- **A mutation harness's tally must be reconciled with the test runner's own summary.** Phase 6
  reported a mutant at 151 killed tests where the suite said `159 failed, 572 passed, 5 errors`:
  the harness read pytest's `-rf` short summary, which lists failures and *not* errors, and keyed
  its results on a `(\w+)` match that collapsed every parametrized case of one function into a
  single name. Both defects only ever undercount, both look plausible, and neither shows up in a
  kill/survive verdict — only in the number beside it. Ask for `-rfE`, keep whole node ids, and
  assert the count against the summary line. A row quoting a number no run can reproduce is the
  same class of finding as a row naming the wrong mutant.
- **Before trusting a mutation survivor, prove the mutant is the code that ran.** Assert
  `module.__file__` points inside the mutation tree, from the same process the tests run in. Phase 4
  produced two independent false-survivor runs, each from a different cause and each reporting
  perfectly plausible output: a workspace path passed relative, so `PYTHONPATH` resolved against the
  subprocess's own `cwd` and every one of 37 mutants "survived"; and a workspace copied with its
  `.venv`, so an installed-package finder resolved `seahaven` back to the real tree and a restored
  Critical "survived". A whole sweep at 0% kills is obvious. One survivor in a sweep that otherwise
  looks right is not, and that is the one this check catches.

### B18. A world has no way to order rows by when they were written within one episode

**Found:** Phase 10 implementation (code review, Moderate 3). **Owner:** unassigned. **Risk:** low
per world, but it is the same problem in every world that has an activity feed.

An instance's clock is frozen (`functional_spec.md` §11, and a progressing clock is a §24 non-goal),
so every row an episode writes carries one `created_at`. A table whose rows are meant to be read in
the order they happened therefore has no ordering key for the part of its contents the episode
itself produced: `ORDER BY created_at` is not an order, and the usual tiebreak on a UUID primary key
is stable but arbitrary.

For a reader writing raw SQL the answer is `ORDER BY created_at, rowid`, which ProjectTracker's
`AGENTS.md` gives for `issue_events`. For a *tool* it is not: a keyset cursor has to carry its
tiebreaker as a value, and `rowid` is not a column a world projects. ProjectTracker's
`list_comments` therefore documents the behaviour rather than fixing it, and
`tools/comments.py` records why a per-table sequence column was not taken.

The framework question is whether `Ctx` should offer a monotonic per-instance counter beside
`ctx.ids` and `ctx.clock` — one that a world can store in a column and page on — or whether the
right answer is that evals should grade on state and on changesets rather than on the order of an
activity feed. Either way it is a decision for the framework, not for one world, and it should be
settled before the docs phase describes activity tables as a pattern.

### B19. A fixture generator cannot be pointed at a world, so testing one means monkeypatching

**Found:** Phase 10 code review (Mild 6). **Owner:** unassigned. **Risk:** low; it costs every world
a monkeypatch in its own fixture test.

`seahaven fixture freeze --run module:function` calls a generator with a live instance, and the
scaffolded `generate.py` gets that instance by importing the world package and calling
`world.instance(...)` itself. The fixtures directory is therefore whatever the imported `World`
object says, and a test that rebuilds the fixtures to compare them with the committed bytes cannot
say "build them over here" — it has to reach into the singleton
(`monkeypatch.setattr(world, "fixtures_dir", tmp_path)`), which is what
`worlds/projecttracker/tests/test_fixtures.py` does.

Nothing is broken: the CLI works, the recipe works, and the monkeypatch is contained. What is
missing is a seam. Either the scaffold's `build()` should take the world (`build(fixture_id, *,
world=...)`, defaulting to the package's), which is a template change and a docs sentence, or
`Instance.freeze` should take a destination directory, which is a framework change. It was not taken
in Phase 10 because it is the scaffold's shape as much as the framework's and belongs with the
template work rather than inside one world's diff.
