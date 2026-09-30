---
status: complete
---

# Architecture: CR Moderate Fixes

This project is 17 independent fixes to an existing codebase. There is no new component or data
model, so this document gives the shape of the work, the rules every phase follows and how the
phases depend on each other. The design for each fix — files, functions, code sketches, test names
and what each test asserts — is in the component doc for its phase:

| Phase | Component doc | Items |
|---|---|---|
| 1 | [components/phase_1_batch.md](components/phase_1_batch.md) | 11.M1, 7.M2, 5.M3, 6.M1, 4.M2, 10.M1, 10.M3, 10.M4 |
| 2 | [components/phase_2_bulk_gate.md](components/phase_2_bulk_gate.md) | 2.M1 |
| 3 | [components/phase_3_route_middleware.md](components/phase_3_route_middleware.md) | 4.M1 |
| 4 | [components/phase_4_docs.md](components/phase_4_docs.md) | 9.M1, 9.M7, 9.M8, 8.M4, 2.M2/4.M3, 9.M6 |

Every design was prototyped on a scratch copy of the tree at `38c6f3c`, with the framework,
projecttracker and xmlrpc suites passing apart from the test changes each doc names. Line numbers
in the component docs are at that commit; later phases move some of them, so the coding agent
finds the quoted code rather than trusting a line number.

## 1. Rules for every phase

- **Commit per phase.** Phases 2 and 3 in particular must land as one commit each, so each can be
  reviewed and reverted on its own.
- **No ergonomic change for default callers.** No public signature changes. The only default that
  changes on purpose is `seahaven serve`'s bind address (7.M2).
- **Test through the real entry point.** Each behaviour change gets a test through
  `world.instance(...)`, the served client, `seahaven check` or pytest, as the component doc names,
  as well as any unit test (AGENTS.md).
- **Tests must fail without the fix.** Each component doc names the tests that fail on today's
  code, or the mutation that makes them fail. The coding agent confirms this before it attests.
- **Docs follow the code in the same phase.** Any bundled-docs sentence a phase makes false is
  fixed in that phase (each component doc lists them). Phase 4 then sweeps the docs once more.
- **Checks.** Every phase runs the full AGENTS.md "Automated checks" list in both environments
  (`serve` and `mcp`), and ends synced on `serve`.

## 2. Phase order and dependencies

```
Phase 1 (batch) ──► Phase 2 (gate) ──► Phase 3 (route middleware) ──► Phase 4 (docs)
```

- **Phase 1 before phase 3.** Phase 1's `hosts_first` (4.M2) walks `Node.added`. Phase 3 removes
  the `parents` map from `composition._walk` and replaces `Node.agent_chain` with a chain per
  contributed entry. `hosts_first` does not read either, so the two do not conflict, but phase 3's
  seal tests then run against the new hook order.
- **Phase 1's link check (10.M4) before phase 4.** The docs rewrite is then guarded by a test that
  checks every relative link and anchor in every bundled page, README and CONTRIBUTING.
- **Phase 2 is independent** of phases 1 and 3 in code (it touches `instances.gate`, `_bulk` and
  the module docstring). It sits between them so that the two riskier changes are reviewed one at a
  time.
- **Phase 4 last**, so that SH103's restated rationale, the `bulk()` atomicity text and the
  history sweep describe the final code.

## 3. Files touched

| Phase | Source | Tests | Docs |
|---|---|---|---|
| 1 | `db.py`, `openenv/serve.py`, `cli/serve.py`, `openenv/client.py`, `cli/__init__.py`, `cli/check.py`, `pytest_plugin.py` (comment), `composition.py` (`hosts_first`), `instances.py` (`_run_startup_hooks`) | `test_db.py`, `test_instances.py`, `test_serve.py`, `test_cli_new.py`, `test_client.py`, `test_find_world.py`, `test_cli_check.py`, `test_pytest_plugin.py`, `test_composite_instance.py`, `test_composition.py`, `test_call_log.py`, `test_typed_call.py`, `test_docs.py` | `serving_and_openenv.md`, `reference/cli.md`, `reference/lints.md` (SH501), `reference/api.md` (optional client line), `composition.md` (hook order) |
| 2 | `instances.py` (`gate`, `_bulk`, module docstring) | `test_instances.py` | `serving_and_openenv.md` (the gate section) |
| 3 | `composition.py`, `instances.py`, `world.py`, `handles.py` | `test_composite_dispatch.py`, `test_composition.py` | `composition.md` (Middleware), `reference/lints.md` (SH207) |
| 4 | `instances.py` (bulk docstring and comment only), `worlds/projecttracker/src/projecttracker/tools/_events.py` (docstring only) | — | `db_schema_and_fixtures.md`, `reference/lints.md`, `composition.md`, `projecttracker.md`, `state.md`, `serving_and_openenv.md`, `reference/api.md`, `worlds/projecttracker/README.md`, `worlds/projecttracker/AGENTS.md` |

Out of bounds for every phase: `src/seahaven/http/` (its behaviour must not change; phase 2's
design keeps HTTP requests ungated), `ui/`, `specs/` (history), and
`worlds/projecttracker/src/projecttracker/schema/*.sql` (a comment edit changes the schema hash
and breaks every fixture).

## 4. Key technical decisions

- **11.M1:** `PRAGMA synchronous=OFF` before `journal_mode=WAL` on instance connections and on the
  blank-instance builder. Instance directories are deleted by the sweep after a crash, so they need
  no durability. `VACUUM INTO` (freeze) is not fsynced at any setting, so fixture durability does
  not change.
- **6.M1:** `CliError` gains `line` and `fix`. A helper finds the innermost frame in the world's own
  code (the `SyntaxError`'s own filename and line for a syntax error), skipping Seahaven, the
  standard library and installed packages.
- **4.M2:** hook order is a depth-first walk in `add_world` order that emits a node only once all of
  its distinct hosts have been emitted. It equals today's canonical preorder when nothing is shared
  (proof in the component doc).
- **2.M1:** `gate()` is re-entrant per thread through a `threading.local` flag (not a `ContextVar`:
  Starlette and `asyncio.to_thread` copy context variables into worker threads). `_bulk` enters
  `gate(bypass=True)` before the instance lock: it marks the thread and takes no slot, because
  taking a slot would gate HTTP requests and make an open `bulk()` hold a slot.
- **4.M1:** `Contributed` carries the chain of the route that contributed it; chains are built once
  per distinct route at the seal. `Node.agent_chain` is removed, and `Node.internal_chain` becomes
  `own_chain`, used by handle calls and the root's own tools. Host code through a handle keeps
  running only the owner's own chain.

## 5. Error handling

No new error types. 6.M1 changes the message, location and fix text of SH501 and the pytest
plugin's import failure. 2.M1's new tests use a gate whose `acquire` raises after a timeout, so a
regression fails the test instead of hanging pytest in `destroy()` at teardown.

## 6. Testing strategy

Each component doc lists its tests with names, files and assertions. Across the project:

- Concurrency tests (phase 2) run their bodies on a fresh thread, because the session-scoped
  `receivers` fixture in `tests/test_docs_examples.py` leaves the main thread inside a `bulk()`.
- Tests that need a broken world (6.M1) write it into `tmp_path`: a committed file with a syntax
  error would fail `ruff` and `ty`.
- One known environment-only failure, `test_typed_call.py::test_ty_resolves_the_result_type_of_a_
  call_by_reference`, appeared on the scratch copies (ty could not resolve the copy's
  environment). It must pass on the real tree.
