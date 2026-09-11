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
  this project passed its unit test and failed on a real call.
