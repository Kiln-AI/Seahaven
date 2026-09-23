---
status: complete
---

# Phase 3: Docs

## Overview

Phases 1 and 2 gave an instance's clock a mode (`fixed`, `tick`, `running`, `wall`), a per-world
default (`running`), and a `clock_mode` option at every door. The bundled docs still describe the
clock as frozen. This phase rewrites them where they do (functional spec §7), documents the new
API in `reference/api.md` and `reference/cli.md`, and gives the scaffold README a runnable way to
call `build`. Examples that show a moving clock use `tick`, whose readings are exact.

## Steps

1. `concepts.md`
   - Contents row for Clock: "How the instance's time moves".
   - `ctx.clock` row in the Context table: the instance's clock, `now()` and `iso()` for the
     current reading, `mode` for the mode.
   - "Clock" rewritten: the start instant; the four modes as a table (reading, deterministic);
     `running` is the default; choosing one with `world.instance(clock_mode=...)` / `reset` and
     `World(default_clock_mode=...)`; a runnable `python` example under `tick` (startup reading S,
     call `i` reads S + (i+1) s, reading never ticks); SQL reads the same clock, one reading per
     statement; the wall-clock read statement limited to `fixed`, `tick` and `running`; ordering
     rows by timestamp per mode, linking to authoring.md.
   - "Reproducibility": `fixed` and `tick` replay; `running` and `wall` do not; point to `tick`.
2. `authoring.md`
   - "Ordering within one run" rewritten per mode: `fixed` shares one instant, `tick` orders across
     calls but not within one, `running`/`wall` are not an order.
   - The startup keyword sentence names `clock_mode` among `world.instance`'s parameters.
3. `state.md`
   - Envelope example gains `"clock_mode"` after `"now"`; field table: `now` is the reading when
     the document was produced (producing it never ticks), new `clock_mode` row.
   - "The state the episode started from": the blank instance clock sentence covers the mode.
4. `serving_and_openenv.md`
   - Reset bullet and reset table gain `clock_mode=`; the reset observation's metadata lists
     `clock_mode`; the `SeahavenState` table gains `clock_mode`; the "before the first reset"
     sentence names it; the client example comment and `state().now` comment drop "frozen"; the
     wire example's reset sends `"clock_mode": "tick"` and its metadata carries it.
5. `reference/api.md`
   - The export list gains `seahaven.ClockMode`.
   - `World` prose: `default_clock_mode`, its default and its refusal; member rows:
     `world.instance(...)` gains `clock_mode`, `world.default_clock_mode`.
   - `Instance` row for `inst.clock` unchanged; `Clock` section rewritten: `now()`, `iso()`,
     `mode`, `Clock(datetime)` is a fixed clock, `ClockMode`, SQL overrides with one reading per
     statement.
   - Marker signature gains `clock_mode=None`.
6. `reference/cli.md`
   - `seahaven mcp` synopsis, options table (`--clock-mode MODE`, `SEAHAVEN_CLOCK_MODE`), and the
     `--reset-options` prose (four convenience keys).
   - `fixture freeze` / `fork`: the instance runs in the world's default mode, so under `running`
     the fixture's `now` is `--now` plus the builder's time; a fixture that must rebuild to the
     same bytes is built by a generator that passes `clock_mode="fixed"`.
7. `testing.md`: marker table row `clock_mode="tick"`; the pass-through sentence names
   `clock_mode`.
8. `db_schema_and_fixtures.md`: the sidecar `now` row (the reading at freeze, where a new instance
   starts); "Building a fixture" and "The generator is committed" say what the CLI's default mode
   does to `now` and that `build` pins `fixed`; `fork` starts from the parent's `now`.
9. Other pages that call the clock frozen: `index.md` (the note after the first example),
   `projecttracker.md` ("Order within one run"), `reference/lints.md` (SH103 and SH201 "Why"),
   `composition.md` ("One clock"), the repository `README.md` feature line,
   `worlds/projecttracker/README.md` (list ordering), and the scaffold `AGENTS.md.tmpl` rule line.
10. Scaffold `README.md.tmpl`: the Fixtures row points at a new "Making a fixture" section, which
    gives `uv run python -c "from fixtures_src.generate import build; build('empty')"`, run from
    the world's directory.

## Tests

- `tests/test_docs_examples.py` runs the new `python` examples (the `tick` example in
  concepts.md) and parses the `py` fragments; no change to the harness.
- `tests/test_cli_new.py`
  - `test_the_readme_builds_the_first_fixture`: in a fresh scaffold, run the command from the
    rendered README's "Making a fixture" block (with this interpreter in place of `uv run python`
    and `src/` on `PYTHONPATH`, as the scaffold pytest test does); `fixtures/empty/fixture.yaml`
    exists with `now` equal to the generator's `NOW`.
