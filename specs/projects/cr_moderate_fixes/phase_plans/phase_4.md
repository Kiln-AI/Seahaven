---
status: complete
---

# Phase 4: Docs

## Overview

The last phase corrects what the bundled docs, ProjectTracker's notes and two code comments say,
now that phases 1-3 have settled the code. It restates why lint SH103 exists (9.M1), fixes the
composite cost note (9.M7) and ProjectTracker's check command (9.M8), narrows the audit-trail and
`actor_id` claims to the writes that make them true (8.M4), documents that a composite `bulk()` is
not all-or-nothing when a commit fails (2.M2/4.M3), and removes history, benchmark figures,
version stories and spec citations from the bundled docs (9.M6). Per
[components/phase_4_docs.md](../components/phase_4_docs.md). No heading is renamed, so no anchor
or `tests/test_docs.py` change.

## Steps

1. **SH103 (9.M1).**
   - `db_schema_and_fixtures.md`, the seeded-rows paragraph: replace from "The wall-clock rule
     below is unchanged…" to the end of that sentence with Edit 1a.
   - `db_schema_and_fixtures.md`, "No expression reads the wall clock": replace the three
     sentences from "The problem is" with Edit 1b.
   - `reference/lints.md`, SH103: the "Rule" names every place the regex covers (Edit 1d); the
     "Why" becomes Edit 1c. "Fix" and the false-positive paragraph are unchanged.
2. **Composite cost (9.M7, H18).** `composition.md`, the "idle composite instance" bullet: replace
   the design-target and benchmark sentences with the per-node cost sentence.
3. **ProjectTracker check (9.M8).** `projecttracker.md`, "Its tests": the fence runs
   `uv run seahaven check --world projecttracker:world`, with "Run both from the root of the
   Seahaven repository." above it.
4. **Audit trail and `actor_id` (8.M4).** Apply the component's table to
   `worlds/projecttracker/README.md`, `projecttracker.md` (schema intro and viewer paragraph),
   `worlds/projecttracker/AGENTS.md` (viewer bullet, plus one new bullet naming what the trail does
   not record), and the `_events.py` module docstring. `schema/001_core.sql` is not edited.
5. **Composite `bulk()` atomicity (2.M2/4.M3).**
   - `instances.py` `Instance.bulk` docstring: the last two sentences say what happens when the
     block raises and when a commit fails.
   - `instances.py` `_bulk`: the comment above the transaction loop becomes the constraint comment
     (one file per node, SQLite cannot commit two files atomically, reverse commit order).
   - `composition.md` "One instance, many stores": after the `inst.bulk()` sentence, add the
     three-sentence paragraph on a raise, a failed commit, and not freezing after one.
   - `reference/api.md`, the `inst.bulk()` row: the root node's `Ctx`, one transaction per node,
     linked to `composition.md#one-instance-many-stores`.
6. **History sweep (9.M6).** Apply H1-H24 as the component table gives them:
   - `state.md`: drop the bench probe and its figures (H1-H4), keeping "Recording is not free…"
     and "A read-only call records nothing, so it costs less than a write."
   - `serving_and_openenv.md`: delete H5, H7, H9, H10; rewrite H6 and H8 as present-tense fact;
     rewrite "which is the whole integration" in the Kiln section.
   - `composition.md`: H11-H17 as listed; delete "That example is the whole feature."
   - `reference/api.md`: H19 without spec citations or a count; H20 names the two cases.
   - `db_schema_and_fixtures.md`: H21, H22.
   - `projecttracker.md`: delete H24.
7. Wrap every changed paragraph at 100 columns; run `ruff format` and the docs tests.

## Tests

- `tests/test_composite_instance.py::test_a_bulk_commit_that_fails_keeps_the_nodes_committed_before_it`:
  a two-node instance whose root schema has a `DEFERRABLE INITIALLY DEFERRED` foreign key; a
  `bulk()` block writes a child row and a root row that breaks the key. `bulk()` raises `DbError`,
  the root has no row, and the child keeps its row and its change-log record. Drives it through
  `world.instance(...)`, and pins the behaviour the new docs describe.
- Existing guards cover the rest: `tests/test_docs.py` (page list, every relative link and anchor)
  and `tests/test_docs_examples.py` (every example still runs).
