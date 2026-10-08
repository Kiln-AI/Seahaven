---
status: complete
---

# Phase 3: Docs

## Overview

Phases 1 and 2 built `setup_sql` and put it on every entry point and in the state document. This
phase documents it in the six existing pages the plan names, with no new page, per the AGENTS.md
docs style: short, plain sentences, every name checked against the code, examples tested.

## Steps

1. `db_schema_and_fixtures.md`: a new section, "Adjusting a fixture per episode", after "Building a
   fixture" and before "The generator is committed", with a row in the page's table of contents.
   Content: what `setup_sql` is (SQL run on the new instance after the copy and before the startup
   hooks, in one transaction), write rows only and change the schema with a new world version, a
   composed world's table names (link to `composition.md`), and when a fixture or a startup hook is
   the better tool. One runnable `python` example on an inline world: `world.instance(...,
   setup_sql=...)` with two statements, the rows visible through `inst.inspect()`,
   `inst.change_log() == []`, and `inst.state()["setup_sql"]` as given.
2. `serving_and_openenv.md`:
   - the reset argument table gains a `setup_sql=` row after `startup=`;
   - the paragraph after it ("One served world covers every scenario a fixture and a startup hook
     can express") names setup SQL too;
   - one example reset message with `setup_sql` beside the existing `startup` example;
   - the `SeahavenState` table gains a `setup_sql` row after `startup`;
   - the pre-reset null list gains `setup_sql`.
3. `state.md`:
   - the example envelope gains `"setup_sql": null` after `startup`;
   - the envelope table gains a `setup_sql` row;
   - "Startup writes are not in the log" covers `setup_sql` rows too;
   - "The state the episode started from" names `setup_sql` as part of the starting state, beside
     `startup`.
4. `composition.md`: one sentence and a `py` fragment in "One instance, many stores" or "What an eval
   sees": `setup_sql` names tables as `inst.inspect()` does, `payments.charges` for an added node.
5. `reference/api.md`:
   - the `world.instance` row gains `setup_sql=None` and one clause;
   - the `inst.*` table gains `inst.setup_sql`;
   - the marker signature gains `setup_sql=None`;
   - a short list of what `setup_sql` refuses (the reference home for the refusal list).
6. `reference/cli.md`: under `seahaven mcp`, one example `--reset-options` line or sentence naming
   the `"setup_sql"` key.

## Tests

- `tests/test_docs_examples.py` runs the new `python` example and parses the new `py` fragment; it
  also resolves `inst.setup_sql` in the api table against a live instance.
- `tests/test_docs.py` checks every new anchor link resolves (the new table-of-contents row and any
  cross-page link to the new section).
- No new test file: the behaviour is covered by phases 1 and 2.
