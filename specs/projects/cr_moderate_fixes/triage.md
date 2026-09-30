# Moderate Findings: Triage Decisions

Decisions on the 56 moderate findings from the full-repo deep code review of `0360978`
(September 2026; the review files are not in the repository). IDs are
`phase.M#`: the Nth item under "Moderate" in `phase_<phase>_feedback.md`. Two IDs on one row are the
same issue, found by two phases.

Status: **decided.** Every moderate is classified. The "Fix" items are the scope of this project;
the "Pre-publish" items are considered before release.

## Summary

| Bucket | Count | IDs |
|---|---:|---|
| Already fixed | 1 | 1.M2 (in `a1680c0`, the tool-argument validation fix) |
| Won't fix (permanent) | 29 | See [Won't fix](#wont-fix-permanent) |
| Fix | 17 | See [Fix](#fix): 13 easy fixes and 4 larger items |
| Pre-publish: consider before release | 9 | See [Pre-publish](#pre-publish-consider-before-release) |

---

## Won't fix (permanent)

Grouped by reason.

### A1. Goes away once published

| ID | Issue | Why |
|---|---|---|
| 12.M2 | README says `uv add seahaven` / `pip install seahaven`, which installs the PyPI placeholder | Correct as written once the real package is published |
| 6.M7 | `seahaven new` prints `uv sync` as a next step, which pulls the placeholder | Same: correct once published |

### B. Follows from the critical decisions (the world owns SQL limits)

| ID | Issue | Why |
|---|---|---|
| 7.M1 | `run_sql` has no default cap on result size | Same stance as critical #2: the world sets `max_rows` / `max_bytes` |
| 10.M2 | No test runs a recursive CTE through the sandbox | Callers are not expected to write recursive queries |

### D. Rare author-side edge cases that fail loudly or never come up

| ID | Issue | Why |
|---|---|---|
| 1.M1 | A parameter named `_x` or `model_config` is silently dropped | Odd names; every call then fails, so it is caught on first use |
| 1.M3 | A result reshaped by middleware is not checked before commit | Fails loudly on the wire; middleware rarely reshapes results |
| 1.M4 | `serialise` checks a model's raw attributes, not what it renders | Exotic model serializers only |
| 1.M5 | `ToolError.details` is not checked the way results are | Needs sets or bytes in error details |
| 2.M3 | Two concurrent freezes of the same fixture id can corrupt it | An authoring-time action by one person |
| 2.M4, 6.M2 | Fixture id vs directory name, and duplicate ids, are not checked | Only after hand-renaming a fixture directory; the runtime error is loud |
| 6.M3 | A non-UTF-8 fixture file crashes `seahaven check` | We write these files ourselves |
| 6.M4 | Lint SH103 misses date functions called with no argument | Lints are an advisory safety net |
| 6.M5 | Lint SH201 misses `datetime.today()` and `time.time_ns()` | Same |
| 6.M6 | The missing-extra message suggests an extra that conflicts with the installed one | The user sees a clear conflict error next |
| 3.M2 | FTS3/FTS4 and R*Tree internal tables fill the change log | No world uses them (projecttracker uses FTS5) |
| 5.M4 | The published reset schema and the server disagree on edge fields | Only a startup hook with required keywords or very long `episode_id`s |
| 9.M5 | The "what registration refuses" list is in a guide page, not `reference/` | Placement only; the content is correct |
| 8.M3 | projecttracker's fixture history does not show closing an issue dropping its assignee | Fixture realism only; the tools behave correctly |

### E. Performance: wait for a real workload

| ID | Issue | Why |
|---|---|---|
| 11.M4 | The result-safety check is 20% of a one-row read, and runs twice when served | Fine at current scale |
| 11.M5 | 500 sessions need more open files than the common 1024 soft limit | Operators raise `ulimit`; a docs line later |
| 11.M6 | The default gate can starve waiting callers | Already known and documented in the code |
| 8.M1 | projecttracker's index comments are wrong about sort order | Results are still correct; fixtures are small |
| 8.M2 | No `issue_id` index on comments or issue events | Small fixture data |

### F. Decided in the easy-fix round

| ID | Issue | Why |
|---|---|---|
| 5.M5 | After a client-side timeout, the next call silently runs on a new, blank session | Maintainer decision |
| 5.M2 | `reset` and `state` failures are not scrubbed the way `step`'s are | By design: `reset` and `state` answer the orchestrator, not the agent. Scrubbing is not wanted there |

### G. Decided in the final round

| ID | Issue | Why |
|---|---|---|
| 5.M1, 11.M3 | `state` is built on OpenEnv's event loop; the default format re-renders the whole log per read | We own the state code and it is fast enough |
| 3.M1 | Clock SQL functions are registered deterministic, so `'now'` can get into a generated column, an index or a partial index | Removing the flag also refuses deterministic uses such as an index on `date(created_at)`, and slows scans that call the clock. Lint SH103 already flags `'now'` anywhere in the schema |

---

## Fix

### Easy fixes

Clear what to do, a small change, low risk.

#### Docs only

| ID | Issue | Cost |
|---|---|---|
| 9.M1 | The docs' reason for SH103 is false: the SQL clock already writes canonical text | Rewrite one paragraph each in `db_schema_and_fixtures.md` and `reference/lints.md` |
| 9.M7 | `composition.md` says the benchmark measures one node per instance; it now measures composite trees | Fix one sentence |
| 9.M8 | `projecttracker.md` runs `uv run seahaven check` from the repo root, where it fails | Fix one code block |
| 8.M4 | projecttracker README and AGENTS.md overstate the audit trail and `actor_id` | Reword a few lines in 3 files |
| 2.M2, 4.M3 | Composite `bulk()` claims all-or-nothing, but is not when a later commit fails | Accepted as behaviour: fix the code comment, add 2-3 lines to `composition.md` |

#### Tests only

| ID | Issue | Cost |
|---|---|---|
| 10.M1 | The argument-copy test still passes with the copy deleted | Rewrite one test so the caller mutates its own list after the call |
| 10.M3 | A typed call through a handle in a diamond composition is untested | Add one test |
| 10.M4 | `test_docs.py` checks links only in `index.md` | Widen the check to every page |

#### Small code changes

| ID | Issue | Cost |
|---|---|---|
| 11.M1 | Switching each new instance to WAL fsyncs: more than half of fork time | `synchronous=OFF` before the WAL switch in `db.py` |
| 7.M2 | `seahaven serve` binds `0.0.0.0` with no auth | Default to `127.0.0.1`, plus a docs row; the Dockerfile template already passes `--host 0.0.0.0` |
| 5.M3 | `client.step(ListToolsAction())` crashes with a pydantic error | Parse a reply that carries `tools` as `ListToolsObservation`, plus a test |
| 6.M1 | An import error in world code shows no file or line, and gives the wrong fix text | Report the exception and its last frame; drop the wrong fix text (CLI and pytest plugin) |

---

### Larger fixes

| ID | Issue | Decision |
|---|---|---|
| 9.M6 | History, bench figures and spec citations in the bundled docs | Clean up, per the AGENTS.md docs style |
| 4.M2 | Startup hooks can run in the wrong order when a node is shared | Run hooks in topological order over the node graph: every host before every node it adds. Order is unchanged wherever nothing is shared. Add a test |
| 2.M1 | `inst.call(...)` inside `bulk()` takes the gate while holding the instance lock, so it can deadlock (reproduced with a gate of 1) | Option (a): make the gate re-entrant per thread. `bulk()` takes a gate slot and records it in a thread-local; a thread already holding a slot skips the gate (covers `Y.call()` inside X's `bulk()`). **No ergonomic change for default callers.** Add a two-thread test with a timeout |
| 4.M1 | A middle world's middleware stops running on its tools once a node it adds is shared | Option (a): build the middleware chain for each contributed tool from its contributing route, not the node's canonical route. Add tests for the shared-node case |

### Plan notes for the spec

- **Docs phase:** all the docs-only easy fixes, plus 9.M6.
- **2.M1 gets its own phase and commit**, so it can be reviewed and rolled back on its own.
- **4.M1 gets its own phase and commit**, so its complexity and tests can be judged on their own.
- The other easy fixes (tests only, small code changes) and 4.M2 can be batched.

## Pre-publish: consider before release

These only matter once Seahaven is public. Consider them before release.

| ID | Issue | To decide |
|---|---|---|
| 12.M1 | Scaffold pins `seahaven~=X.Y`, which only pins the major version | Pin style that follows the minor version |
| 12.M4, 9.M4 | MIT `LICENSE` exists, but `pyproject.toml` declares no licence; CONTRIBUTING and AGENTS.md say there is no LICENSE | Licence metadata, and aligning AGENTS.md and CONTRIBUTING |
| 12.M3 | The licence gate reads only package metadata; numpy's wheel bundles libgfortran (GPL-3 with the GCC runtime exception) and libquadmath (LGPL-2.1) | Record a legal decision; allow-list them, or teach the gate about bundled libraries |
| 12.M5 | `projecttracker` is a third party's PyPI name; `seahaven-xmlrpc` is unclaimed; docstrings say `pip install "projecttracker[serve]"` | Rename or claim the names; fix the docstrings |
| 9.M3 | README promises Hugging Face publishing, which does not work yet | Lands with the `--hub` branch, or soften the README |
| 5.M6 | Hub-pushed worlds share one global instance on `/web/*` | `--hub` branch: disable it, or document it as a single-user demo |
| 9.M2, 11.M2 | README capacity and speed claims are not measured by `bench/` | Soften the claims, or extend the benchmark |
