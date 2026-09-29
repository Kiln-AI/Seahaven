---
status: complete
---

# Functional Spec: CR Moderate Fixes

This project fixes the 18 moderate findings that [triage.md](triage.md) marks "Fix". Each
section below states the defect, the behaviour after the fix, and what an author or operator sees.
IDs are the review's `phase.M#` IDs, as used in the triage.

Guiding rules for every item:

- **No ergonomic change for default callers** unless this spec names it. Public signatures stay
  the same.
- **The real entry point is tested.** Every behaviour change has a test through
  `world.instance(...)` (or the server/CLI a caller uses), as well as any unit test (AGENTS.md).
- **Bundled docs stay current.** Any docs text a change makes false is fixed in the same phase;
  the docs phase then does one sweep over everything.

## 1. Engine and serving fixes

### 11.M1: faster instance creation
Creating an instance (a fork from a fixture, or a blank instance) no longer fsyncs when it
switches the fresh database copy to WAL. Instance databases are throwaway, so they give up
durability against an OS crash or power loss. Nothing else changes: fixture files written by
`freeze` keep their current durability. Expected effect: fork time roughly halves on `agency`.

### 7.M2: `seahaven serve` listens on localhost by default
`seahaven serve` and `seahaven.openenv.serve(...)` bind `127.0.0.1` by default, not `0.0.0.0`.
An operator who wants the server reachable from other hosts passes `--host 0.0.0.0`. The Docker
image a hub scaffold builds keeps working, because its Dockerfile already passes `--host 0.0.0.0`.
The CLI help and the docs state the new default.

### 5.M3: `SeahavenClient.step(ListToolsAction())` works
The typed client already accepts a `ListToolsAction`. Today the reply fails to parse and raises a
pydantic `ValidationError` after the server ran the step. After the fix, the client returns a
`StepResult` whose observation is a `ListToolsObservation` carrying the tools.

### 6.M1: import errors in world code are reported where they happen
When importing a world's package raises (a `SyntaxError`, a `NameError`, an `ImportError` from
the world's own code), `seahaven check` (rule SH501) and the pytest plugin report the exception
type, its message, and the file and line in the world's code where it was raised. They do not
tell the author to "export world = ...": that fix text is shown only when the package imports
cleanly and has no world.

### 3.M1: the SQL clock can no longer get into stored or indexed values
The clock's SQL functions (`datetime('now')`, `CURRENT_TIMESTAMP` and the rest of the
overrides) stop claiming to be deterministic. SQLite then applies its own rule: a schema that uses
the current time in a generated column, a CHECK constraint, or an index expression or partial
index `WHERE` clause is refused when the schema is applied, with SQLite's error. `DEFAULT`
clauses and triggers that use the current time keep working. Lint SH103 remains; the docs phase
restates what it is for.

### 4.M2: startup hooks run hosts first, even when a node is shared
Instance startup hooks run in an order where every world runs before every world it adds. When
nothing is shared, the order is identical to today's (root first, then each added world in
`add_world` order, depth first). When a node is added by two hosts, it runs after both. Example:
`company` adds `payments` and then `shop`, and `shop` adds `payments`; the order becomes
`company`, `shop`, `payments` (today: `company`, `payments`, `shop`).

## 2. The `bulk()` deadlock (2.M1)

Today, an `inst.call(...)` made inside `with inst.bulk():` waits for a slot of the process-wide
concurrency gate while its thread holds the instance lock. Another thread can hold the last slot
while it waits for that lock, and both threads then wait for ever. With a gate of 1 (the default
on a one-CPU host), two threads are enough.

After the fix:

- A thread that already holds a gate slot never waits for another one. Calls made inside
  `bulk()`, on the same instance or on any other instance, run without queueing at the gate.
- `bulk()` takes its gate slot before it takes the instance lock, as every call does.
- Nothing changes for a caller that does not nest: same signatures, same results, same limits.
  A `bulk()` block holds one gate slot for its duration.

## 3. Middleware on a shared node (4.M1)

Today, the middleware that runs on an agent-facing tool is chosen from the node's canonical route.
When a node becomes reachable by a second route, a tool contributed through the first route can
silently lose the middleware of the worlds on that route.

After the fix, the middleware that runs on an agent-facing tool is the middleware of the worlds on
the route through which that tool was contributed. When a tool reaches the surface under two
names through two routes, each name runs its own route's middleware. When nothing is shared,
behaviour is unchanged. `composition.md` states the rule.

## 4. Docs

The docs phase fixes every docs-only item and does one sweep:

- **9.M1:** restate what lint SH103 is for. The current reason is false: the SQL clock already
  writes canonical time text.
- **9.M7:** correct `composition.md`'s statement of what the benchmark measures.
- **9.M8:** correct the `projecttracker.md` block that runs `uv run seahaven check` from where it
  fails.
- **8.M4:** correct projecttracker's README, AGENTS.md and `_events.py` claims about which writes
  are recorded in the audit trail and which tools take `actor_id`.
- **2.M2 / 4.M3:** composite `bulk()` is not all-or-nothing when a later node's commit fails.
  This behaviour is accepted. Correct the code comments and docstrings that claim otherwise, and
  document the real behaviour in `composition.md`.
- **9.M6:** remove history, benchmark figures, version stories and spec citations from the bundled
  docs, per the AGENTS.md docs style.

## 5. Tests-only items

- **10.M1:** the call-log argument-copy test fails when the copy it guards is deleted.
- **10.M3:** a typed call through a handle in a diamond composition is tested.
- **10.M4:** `tests/test_docs.py` checks the relative links of every bundled page, not only
  `index.md`.

## Out of scope

- The 28 won't-fix and 9 pre-publish items in [triage.md](triage.md).
- The `seahaven new --hub` root `__init__.py` fix (another branch).
- The review's mild findings.
