---
status: complete
---

# Phase 5: Documentation

## Overview

The code shipped in phases 1–4; this phase makes the bundled docs describe it. `state.md` is
written from FS §3, §4, §5, §6 and §10 in the three-case order FS §12 gives, `index.md` lists it,
and every page that still speaks of changesets, `changes()` or the control tool is brought to the
change log and the state document.

Two plan-level constraints govern the sweep, from `implementation_plan.md` Phase 5 and FS §13:
**no doc mentions `controller_` except one line in `reference/cli.md`**, and **no doc mentions
`changes()`**. Every example executes under `tests/test_docs_examples.py`.

`openenv.md` has not landed on `main` (the docs directory holds eleven pages and none is it), so
this phase records the deferred edit in `BACKLOG.md` rather than making it, per ARCH §12.

Where the specs and the code disagree on a name, the code is what gets documented: the
registration decorator is `world.state_format(...)` and the pinned string is
`world.pinned_state_format`; `controller_run_sql` warns once per call *site*, not once per call,
because Python deduplicates by location and the warning is attributed to the caller's own line
through `skip_file_prefixes`.

## Steps

1. **`src/seahaven/docs/state.md`** — a new page, in this order:
   - *The document*: `inst.state()` answers one JSON-able dict; envelope above `state`,
     formatter's output in `state`; a worked `jsonc` block and the envelope field table (FS §3.1).
   - *Choosing a format*, the three cases in FS §12's order, each with its reason:
     `seahaven.state/1` for a `final_state` read once at the end; `seahaven.state+last_step/1` for
     a per-step reader such as an OpenEnv rollout harness (whole log per step is quadratic);
     `world.state_format(...)` for a caller whose needs differ. An executable example per case.
   - *Where the format is chosen*: `World(state_format=...)` required, `world.instance(
     state_format=)`, `reset(state_format=)`, `inst.state(format=)` in process only; when each
     name is checked; the `World.version` recommendation.
   - *The change log*: the record shape table and the rules of FS §3.2, plus order (§3.3).
   - *The fold, and the two traps*: FS §3.6's algorithm as prose steps; overcounting straight off
     the log; comparing logs instead of folds. Say the framework ships no fold helper.
   - *Values*: the SQLite → JSON mapping table (§3.4), with the 2^53 and infinity notes.
   - *The state the episode started from*: the lookup is `world.name`, `world.version`,
     `fixture.id` and `fixture.file_sha256`; for a blank instance it is the DDL plus whatever the
     startup hooks wrote, which the format does not promise to make reproducible.
   - *The compatibility contract*: FS §10, both halves.
   - *Why nothing derivable is in the document*: no fold, no counts, no end-state rows.
   - *What it costs*: the change log is recorded per call; point at `bench/` and the `recording`
     probe, quoting only the ranges phase 4 measured with the command that produced them.
2. **`index.md`** — add a `state.md` row to the reading-order table; replace "changesets" in the
   `concepts.md` row with the change log and the state document.
3. **`concepts.md`** — `:40` changeset → the change log; point the "Change log" section at
   `state.md`.
4. **`authoring.md`** — `:224` drop the literal `controller_run_sql` from the refused-names list
   (the framework's control tool, which the error names); `:492` "out of changesets" → out of the
   change log; `:570` "state and changesets" → the state document.
5. **`testing.md`** — `:190` "that the changeset renders" → that the change log renders.
6. **`serving.md`** — the `state` message paragraph becomes the state document over the wire
   (`SeahavenState`, `state()` and `model_dump(exclude={"step_count"})`, `step_count` is not
   `call_count`, `reset(state_format=)`); delete the "The control tool" section whole (FS §11: it
   leaves the docs entirely); the client example reads the document; the
   `--include-control-tools` row keeps its line without the tool's name.
7. **`reference/api.md`** — `:64` and `:160` changeset session → the change log's per-call
   session; add `seahaven.state`'s public names where the page lists what is public; note
   `SeahavenState` against `state.md`.
8. **`reference/lints.md`** — SH101 gains the sentence FS §12 asks for: STRICT is also what keeps
   every row visible to the change log, because a STRICT table refuses `NULL` in a primary-key
   column and such a row would never be recorded.
9. **`README.md`** — the "Changesets" feature bullet becomes the state document; the
   `env.state()` comment stops calling it a diff.
10. **Source docstrings the docs mirror** — `db.py`'s `conn` docstring (the same sentence as
    `api.md:160`) and `ids.py`'s module docstring, which still say "changeset". `sandbox.py` and
    `instances.py` keep theirs: those describe SQLite's session extension, whose own vocabulary
    is `changeset()`.
11. **`BACKLOG.md`** — a new open item recording the one deferred docs edit: `openenv.md` has not
    landed, and whoever lands it owns the state-surface edit this phase would have made.

## Tests

The docs have no drift test by design (`components/pytest_and_docs.md` §2); what guards this phase
is the existing harness plus two new checks of the plan's own constraints.

- `tests/test_docs_examples.py` — unchanged, and every new `python` block in `state.md` runs under
  it: the three format cases, the custom formatter, the fold trap, and the starting-state lookup.
- `tests/test_docs.py::test_every_page_is_reachable` and the layout test — `state.md` is added to
  `PAGES` and to `index.md`, so both keep passing.
- `tests/test_docs.py` — new `test_no_page_mentions_a_control_tool_by_name`: no page under
  `docs/` contains `controller_`, except `reference/cli.md`, which contains it on exactly one
  line.
- `tests/test_docs.py` — new `test_no_page_mentions_the_removed_changes_call`: no page contains
  `changes()`.
