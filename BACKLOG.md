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
- **Before trusting a mutation survivor, prove the mutant is the code that ran.** Assert
  `module.__file__` points inside the mutation tree, from the same process the tests run in. Phase 4
  produced two independent false-survivor runs, each from a different cause and each reporting
  perfectly plausible output: a workspace path passed relative, so `PYTHONPATH` resolved against the
  subprocess's own `cwd` and every one of 37 mutants "survived"; and a workspace copied with its
  `.venv`, so an installed-package finder resolved `seahaven` back to the real tree and a restored
  Critical "survived". A whole sweep at 0% kills is obvious. One survivor in a sweep that otherwise
  looks right is not, and that is the one this check catches.
