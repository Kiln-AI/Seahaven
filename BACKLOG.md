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

### B11. A built world wheel ships no fixtures, so `instance(id)` fails from an install

**Decision (2026-09-13, maintainer): a world is checked out, not installed. Document it and change
no code.** No build-time error and no lint: a world that builds a wheel is off the supported path
rather than wrong, and a lint would fire on every correct world that has fixtures. The runtime
error already names the cause and the `fixtures_dir=` escape hatch at the moment it matters.

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

### B15. CI installs the `serve` extra, and the licence gate does not cover it

**Decision (2026-09-13, maintainer): the rule is no copyleft, and the current closure meets it.**
Widen `check_licences.py` to cover extras under that rule rather than the permissive-only allowlist:
GPL, AGPL and LGPL are refused anywhere; permissive, file-level copyleft (MPL-2.0) and
public-domain-equivalent (CC0-1.0) are allowed. MPL-2.0 is copyleft at file scope only and is not
viral for linking a server process, which is why the `serve` closure passes as it stands.

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
starting with `#`) with neither a case nor a note; that deviation is now stated in
`components/openenv.md` §2. None of them was in `reset`, `step`, `state`, `close` or the client.
That is not a coincidence about difficulty so much as about
*locality*: the rules are the only part of this module that is a parser, and a parser wants its own
file, its own suite and its own name.

The move is small and mechanical — `seahaven/openenv/readme.py`, `_first_paragraph` re-exported or
imported by `env.py`, and `tests/test_readme.py` taking the fifty-five parametrized cases with it.
It is filed rather than done because `components/openenv.md` §1 names the subpackage's module list,
so adding a module changes the surface a `status: complete` artifact describes. That is a different
call from the one made on 2026-09-13, which corrected wrong sentences in completed artifacts
(B2, B7, B8, B10, B12, B14, B21) without touching what they design: this one would change the
design, so it still needs the maintainer.

---

### B20. The concurrency gate starves a caller whenever it binds

**Decision (2026-09-13, maintainer): leave the behaviour; do not replace the semaphore.** Record it
as a possible enhancement in one comment line where the `BoundedSemaphore` is created — one line, not
a treatise. The entry stays open as the full account.

**Found:** Phase 11, the gate sweep (`bench/results/latest.md` §4). **Owner:** unassigned.
**Risk:** real under load; no call is lost, but a session can wait seconds for a slot while
others are served thousands of times.

`instances.gate` is a `threading.BoundedSemaphore`, and a semaphore is not a queue. A thread that
releases a slot and immediately asks for another usually wins the race against the waiter that was
just woken: the waiter needs the GIL to make progress and the barging thread is already holding it.
So the gate has no fairness at all, and the effect is not subtle. With five threads calling and the
gate at 1, 2 or 4, one three-second window of the benchmark served its worst-served session **once**
while another session, in that same window, was served 12,874 times. (The probe reports the counts
per window rather than pooled across its repeats, so that range is two sessions running side by
side and not two different windows.) At a gate of 8 -- above the five threads offered, so the gate
never binds -- every session got 2,659 to 2,816 calls, and the same evenness holds at 16 and with
no gate. The sweep's `worst` column says the same thing at 32 sessions: 1.2-5.0 s for the worst
call at every gate that binds, against 148-244 ms with no gate at all.

**The shipped default is not exempt.** On the four-cpu machine the benchmark ran on, the default is
4, and gate 4 is one of the three sizes above that starved a reader to a single call. Any default
that is smaller than the number of sessions calling at once binds, and every gate that binds does
this; a server with 500 sessions and a gate of 16 is the ordinary case, not an edge one.

No value of `n` fixes this, which is why Phase 11 left the default alone and recorded the finding
here instead. `functional_spec.md` §13.1's promise -- "calls queue and nothing is rejected" -- is
kept to the letter, but a call that queues for seconds behind a thread that keeps barging in front
of it is not the service that sentence implies, and an eval whose episode times out because its
session was the unlucky one will read it as a dropped request.

The shape of a fix is a gate that hands slots out in arrival order -- a ticket lock, or a condition
variable with an explicit FIFO of waiters -- replacing the semaphore in `gate()` and
`set_concurrency`. It needs a test that drives more threads than slots and asserts that every
thread is served, which is a test the suite does not have today: the existing gate tests check that
`n` calls run at once and that the size is published, not that the `n + 1`th caller is ever let in.

Worth knowing before acting: the benchmark measures a closed loop with no think time, which is the
worst case for barging. A scouting run before the harness existed, with a millisecond of think time
per session, did not reproduce it -- nobody is barging when everybody has just gone away to do
something else. That observation is not in `latest.md` and was not made by the committed harness;
take it as a hint about where to look, not as a measurement. It does suggest this is a saturation
defect rather than a defect of every serving process, which is worth establishing before deciding
how much to spend on it.

### B23. A world has no way to order rows by when they were written within one episode

**Decision (2026-09-13, maintainer): defer, and do not solve it with a column.** A per-table
sequence number is a client-side workaround for a static clock. The right fix is to the clock — a
monotonic option, or similar — which is a design question and not a backlog item. The entry stays
open as the record of the problem, not of the proposed workaround.

**Found:** Phase 10 implementation (code review, Moderate 3). **Owner:** unassigned. **Risk:** low
per world, but it is the same problem in every world that has an activity feed.

*Renumbered from a second B18 in Phase 12, which was the first phase to cite it by number alone.
`phase_plans/phase_10.md` cites it twice and was not edited, being `status: complete`: its `:241`
quotes this heading beside the number, so that citation still lands here, and its `:288` is a bare
"`BACKLOG.md` B18", which means this item -- and which now lands on no entry at all, the pytest
plugin's two-marker guard having been closed. That one dangling citation is the price of leaving a
completed artifact alone.*

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

---

---

### B24. `hatchling` is a leftover from the name squat; this is a uv project

**Found:** 2026-09-13, closing B17 and reviewing the packaging. **Owner:** unassigned.
**Risk:** none today — the builds work. It is a dependency and a config surface the project does
not need, in five files.

**Decision (2026-09-13, maintainer): remove `hatchling` and build with uv.** It was a placeholder
chosen when the package name was reserved, nothing more.

Five `pyproject.toml` files declare `requires = ["hatchling"]` with `build-backend =
"hatchling.build"` and a `[tool.hatch.build.targets.wheel]` block: the framework, the reference
world, the example extension, and the scaffold template `new` writes (so every world made from now
on inherits it).

The move is `uv_build`, and the thing to prove rather than assume is that the package data still
travels. Hatchling ships every file under `src/seahaven` with no `include` declared, which is what
carries `docs/*.md`, `docs/reference/*.md` and `cli/templates/**/*.tmpl` into the wheel — Phase 8
and Phase 12 both verified that by building and inspecting. `uv_build` has its own rules about what
is included, so:

- build the wheel for each of the four packages and inspect it, not just build it clean;
- confirm the framework wheel still carries all eleven docs pages and all 21 template files, and
  that `entry_points.txt` still has the `seahaven` console script and the `pytest11` entry point;
- confirm the reference world's wheel still carries `schema/*.sql`, which `sql_files` reads through
  `importlib.resources` (its `fixtures/` is outside the package and deliberately does not travel —
  that is B11, decided and documented);
- the scaffold template changes too, so a fresh `seahaven new` must still pass its own tests and
  `seahaven check`.

`tests/test_docs_examples.py` and `tests/test_cli_new.py` already build or inspect a wheel in
places; whatever proves the above belongs in the suite rather than in a phase plan, since the
failure mode is silent — a wheel that builds fine and is missing a file nobody imports until a
user does.

---

### B25. `_make_instance_dir` anchors the `mkdir` and then returns a composed path

**Found:** 2026-09-13, closing B6 — the test written for it is what made this visible. **Owner:**
unassigned. **Risk:** low and of the same shape as B6's: not reachable on today's code, because
every component above the instance directory has just been checked. It is the half of the anchoring
that stops at the `mkdir`.

`instances.py:_make_instance_dir` creates the instance directory relative to the checked `<world>`
descriptor — `os.mkdir(instance_id, 0o700, dir_fd=world_fd)`, which B6 now pins — and then answers
`root / process / self._world.name / instance_id`, a string composed from the same names all over
again. Every caller after it works on that path: `state = directory / STATE_NAME`, the
`build_blank` or `copyfile` that fills it, and every later open. So the inode the framework created
and the file it then writes are resolved twice, by two different routes, and only the first route
is the checked one.

Staged with B6's own swap — the `<world>` directory renamed away and a symlink to a victim left at
its name between the `_open_child` and the `mkdir`, plus a fixed instance id so the attacker's
directory can be named — a real `world.instance(None)` puts the empty directory where it belongs
and the database where it does not:

```
returned dir                        : <root>/<ns>-<pid>/notesworld/1111...5555
inode made through the descriptor   : ['1111...5555']          # under the renamed-away directory
what the attacker's directory holds : ['state.sqlite', 'state.sqlite-shm', 'state.sqlite-wal']
state.sqlite in the checked dir     : False
```

Without the pre-created `victim/<instance id>` the same staging fails at `build_blank` with a bare
`apsw.CantOpenError: unable to open database file`, which is what makes this hard to see: the
obvious staging of it looks like a crash rather than a redirection.

The docstring is already honest about what comes back — "What comes back is a path, because that is
what SQLite and the rest of this module take; by then every component of it is an inode this user
made or owns" — and that sentence is exactly the assumption above. It could say so in one clause:
the path is composed, and it is only as good as the components having been checked a moment
earlier.

Two ways to close it, and neither is a line: hand the caller the `<world>` descriptor (or an
`os.open` of the new directory) so that the state file is created with `dir_fd` as well, which
means `build_blank`, `shutil.copyfile` and `apsw.Connection` all taking a descriptor — APSW takes a
path, so this bottoms out at `/proc/self/fd/<n>` on Linux and at nothing portable; or `os.fstat`
the new directory through the descriptor and again through the composed path and refuse if they are
not the same inode, which closes the window without widening any signature. The second is the
cheaper and is not free of races either. Filed rather than taken because the choice belongs with
whoever decides how far down the descriptor discipline goes, and because B6's decision was about
the `mkdir` specifically.

---

## Deferred — upstream

**Deferred 2026-09-13: not part of this project.** All three are OpenEnv's, reproduced against a
real server, each a small fix upstream and none fixable from inside Seahaven. Kept as the record
of what was found and verified, so nobody re-derives it.

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

---

## Deferred — publication

**Deferred 2026-09-13: publication is not part of this project** (see Phase 13's plan). These stay
open as a record rather than as work. Nothing here is wrong with the code; each becomes correct, or
becomes real, only if the framework is published. Revisit them together if that ever changes.

### B22. Three places tell a user to install `seahaven` from PyPI, where a placeholder answers

**Found:** Phase 12 (docs), checking what the docs and the scaffold may tell a reader to install.
**Owner:** unassigned. **Risk:** low but silent: every one of them succeeds and installs nothing
useful, which is worse than failing.

The framework is not published (Phase 13, sign-off gated). What *is* on PyPI under `seahaven` is a
placeholder release -- 0.0.1, uploaded 2026-09-11, a 1.4 KB wheel with no dependencies and
`requires_python >=3.10` -- and `seahaven~=0.0` (`>=0.0, ==0.*`) resolves to it. Three artifacts send
a user there:

- `src/seahaven/cli/serve.py:19` answers a missing extra with `pip install "seahaven[serve]"`. That
  resolves, reports success, provides no `serve` extra, and the user is told again that the extra is
  missing.
- `src/seahaven/cli/new.py` prints `next: cd <name> / uv sync / uv run pytest / uv run seahaven
  check`. The `uv sync` resolves the scaffold's `seahaven~=0.0` to the placeholder and succeeds,
  installing two packages and no pytest -- the scaffold declares no test dependency. `uv run pytest`
  therefore runs whatever pytest is on `PATH`, which reports `ModuleNotFoundError: No module named
  'seahaven'` from outside the new environment; with a pytest inside it
  (`uv run --with pytest pytest`) collection succeeds and all three tests error with `fixture
  'instance' not found` / `fixture 'world' not found` under an unknown-marker warning, the
  placeholder having no pytest plugin. `uv run seahaven check` answers `error: Failed to spawn:
  seahaven`, it having no console script either. The world's own `ModuleNotFoundError: No module
  named 'seahaven.world'` waits for something to import the package, which the scaffold's tests do
  not. No message names the placeholder, or PyPI.
- `src/seahaven/cli/templates/hub/Dockerfile.tmpl` runs `uv sync --extra serve`, and the scaffold's
  `serve` extra is `seahaven[serve]`. The image builds and the container cannot start.

`src/seahaven/docs/{authoring,serving}.md` and `reference/cli.md` say all of this in prose and give
the checkout install instead, which is why this is recorded rather than fixed there: all three are
Phase 7's code, and the fix wants one decision about what they should say between now and
publication (name the checkout install, or drop the command and say "install the framework"). Once
Phase 13 publishes a real release every one of them becomes correct as written, so the cheapest
resolution may be to close this when that happens -- provided someone checks that it *was* closed by
the release rather than assumed to be.

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
