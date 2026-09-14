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
### B26. ProjectTracker's two rebuild tests are marked `@slow` and are no longer slow

**Found:** 2026-09-13, instrumenting the suites with `--durations` to answer "are the tests slow?".
**Owner:** unassigned. **Risk:** none to correctness. The cost is that the marker stops meaning
anything, which is how a real slow test later gets marked and ignored.

`pyproject.toml:95` defines `slow` for tests that take tens of seconds, and
`worlds/projecttracker/tests/test_fixtures.py`'s two rebuild tests --
`test_the_generator_still_makes_the_fixtures_that_are_committed` and
`test_the_generator_still_makes_the_committed_fixtures_byte_for_byte` -- carry it. Measured on
CPython 3.14.0 they are **0.26s and 0.23s of setup and 0.06s of call**, not tens of seconds. The
rebuild got cheap at some point and nobody re-measured; the whole ProjectTracker suite with both of
them running is 15.6s, and the pair is under 3% of it.

Two ways to close it, and the choice is a maintainer's: drop the marker from these two (they cost
nothing, so the default `-m "not slow"` run may as well cover them, which also removes the only
tests in the repo that a plain CI run skips), or keep it and re-state what `slow` means in
`pyproject.toml` so the definition matches the only tests that use it. Not taken here because the
measurement came out of a task that was told not to change tests for speed.

### B27. A world's schema files can read the wall clock and the host's entropy while `build_blank` runs

**Found:** 2026-09-14, reviewing the seeded `random()` and `randomblob()` change (`src/seahaven/ids.py`,
`src/seahaven/db.py`). **Owner:** unassigned. **Risk:** a blank instance of such a world is not
reproducible, and a fixture frozen from one bakes whatever the host gave it.

`build_blank` opens a plain connection with neither `register_clock_functions` nor
`register_random_functions` on it, because it runs before an instance exists: there is no instant
and no seed yet. It does not only run DDL, though. `db.py:317` iterates every statement in the
world's schema on purpose, so a world that seeds reference rows -- `INSERT INTO plans VALUES
('free', randomblob(8))`, or a `created_at` defaulting to `CURRENT_TIMESTAMP` -- reads the host on
every blank build. Both holes are the same shape and the clock one predates the randomness one; the
randomness change did not widen it, because `build_blank` never had either set of overrides.

Three ways to close it, and the choice is a maintainer's: give `build_blank` a clock and a seed of
its own (`World.name` is the natural source, which is what a blank instance's seed already derives
from); refuse a schema whose statements are not pure DDL, which `lint/ddl.py` is already the place
for and which `_WALL_CLOCK` there half does already as a warning; or state in `authoring.md` that
schema files are DDL only and that seed rows belong in a startup hook, where `ctx` is in hand. Not
taken here because it is neither the clock's phase nor the randomness change's scope.

---

## Deferred — upstream

**Deferred 2026-09-13: not part of this project.** Both are OpenEnv's, reproduced against a
real server, a small fix upstream and neither fixable from inside Seahaven. Kept as the record
of what was found and verified, so nobody re-derives it.

### B13. Two OpenEnv behaviours a Seahaven world cannot fix from its own side

**Found:** Phase 6 code review, rounds 1 and 3. **Owner:** upstream — the `/schema` half is
https://github.com/huggingface/OpenEnv/issues/1155, open against 0.4.2; the `/state` half is a
second, separate upstream defect, drafted in `.upstream-issue-http-stateless.md` and not yet filed.
**Risk:** silent rather than loud: a harness that reads schema over HTTP is told nothing about the
world it is driving, and nothing upstream says so. The `/state` half is no longer silent on a
Seahaven server — see its bullet — but it is still silent on every stock OpenEnv one.

Both were reproduced against a real server and neither is in Seahaven's code. Seahaven now
intercepts the `/state` half rather than fixing it; the `/schema` half has no fix that belongs inside
this framework as it stands.

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
- **`GET /state` cannot answer for a session, and Seahaven now refuses the route rather than let it
  try.** Two defects meet at it. OpenEnv annotates the route `response_model=State`, so FastAPI
  serialises the base model and drops every field `SeahavenState` declares; and the handler builds a
  fresh environment from the factory and closes it before returning, so the route is not
  session-bound in the first place and the numbers it does answer are a throwaway environment's.
  Confirmed against a live session that had reset and stepped once: the websocket answered
  `step_count: 1` and a real episode id while HTTP answered `null` and `0` at the same moment. The
  second defect is shared with `POST /reset` and `POST /step`, whose handlers do exactly the same —
  so the three routes cannot represent an episode between them, and every answer they give is a
  plausible `200` about an environment that is already gone.

  **Seahaven's served app now replaces all three handlers with a `501` that names the defect and
  points at `/ws`** (`seahaven.openenv.app`, covered in `tests/test_server.py` and documented in
  `src/seahaven/docs/serving.md`). The paths stay in the published OpenAPI schema, because
  `openenv push` reads path names to decide whether a world is a simulation or a production
  environment and deleting them would declare the wrong one. That is local protection and not a fix:
  the defect is upstream's, is unfixed on upstream `main`, and any world served by a stock OpenEnv
  server still has it. Nothing pins upstream's old two-key answer, deliberately — a test asserting it
  would have made upstream's defect Seahaven's contract. The websocket path — which is the path
  `SeahavenClient` and every eval use — is fully tested and untouched by the refusal.

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
