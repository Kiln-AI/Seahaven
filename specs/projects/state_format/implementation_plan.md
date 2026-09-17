---
status: complete
---

# Implementation Plan: State Format

Ordered so that every phase leaves CI green: the new machinery lands beside the old, the callers
move, then the old is removed. Details are in `functional_spec.md` (FS) and `architecture.md`
(ARCH); this is the checklist.

## Phases

- [x] **Phase 1: Capture the change log.** `LogRecord`, `render_log`, `tracked_tables`,
  `open_session`, the infinity mapping (ARCH §2.1, §3.3, §3.4); per-call and per-bulk sessions,
  the call ordinal, `call_count`, `change_log()` on `Instance` (ARCH §2.2, §3.1, §3.2, §4);
  `seed` narrowed to `int | None` (ARCH §7). `changes()` stays for now, so the fold test's first
  oracle is `changes()` itself. Tests: `test_changes.py` additions, `test_change_log.py`,
  `test_fold.py` with `tests/support/fold.py`, `test_ids.py`, the pytest-marker seed test, the
  "no database work in `state()`" precursor on `change_log()`.

- [x] **Phase 2: Formats and the world pin.** `seahaven/state.py` with `envelope`, `document`,
  the two built-ins and name validation (ARCH §5); `World(state_format=)` required,
  `RESET_ARGUMENTS`, `world.state_format()`, `resolve_state_format`, `__copy__` (ARCH §5.2);
  `Instance.state()` and the formatting guard (ARCH §5.3); `InstanceManager.create` changes and
  startup serialisation (ARCH §6); the scaffold template; the sweep adding `state_format=` to
  every `World(...)` in the repo, docs examples included (ARCH §10). Tests: `test_state.py`,
  `test_world.py` additions, scaffold tests, `tests/support/state_v1.schema.json`.

- [x] **Phase 3: OpenEnv.** `reset(state_format=, episode_id)`, the `state` property before and
  after `reset`, `SeahavenState` typed over the envelope, client docstring (ARCH §9). Tests:
  `test_env.py` rewritten state tests, `test_client.py`, and the WebSocket confirmation in
  `test_server.py` (FS §9). This phase is the gate: if the document does not arrive whole over
  the wire, stop and report before anything else is built on it.

- [x] **Phase 4: Remove the old surface.** Delete `Instance.changes()`, `Change`, `render()`, the
  long-lived session and `controller_changes`; deprecate `controller_run_sql` (ARCH §1, §2.1, §3.1,
  §8, §13); switch the fold test's oracle to `tests/support/oracle.py` (ARCH §14); rewrite every
  test and doc example that called `changes()` or `controller_changes` to the log, minimally, so
  the suites and the docs test stay green; the `bench/` probe and the performance test (ARCH §16),
  with the measured number recorded in the phase plan.

- [ ] **Phase 5: Documentation.** `state.md` in the three-case order; `serving.md`, `testing.md`,
  `concepts.md`, `authoring.md`, `index.md`, `reference/api.md`, `reference/cli.md`,
  `reference/lints.md` SH101 sentence, the README example; `openenv.md` if it has landed, else a
  `BACKLOG.md` entry (FS §12, ARCH §12). Every example executes under the docs test; no doc
  mentions `controller_` except `cli.md`'s one line, and none mentions `changes()`.
