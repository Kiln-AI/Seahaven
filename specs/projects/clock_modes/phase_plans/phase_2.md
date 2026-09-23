---
status: complete
---

# Phase 2: Doors

## Overview

Phase 1 gave an instance's clock a mode and put `clock_mode` on `world.instance(...)` and on
`seahaven mcp`. This phase puts it on the remaining doors: the OpenEnv reset (method, published
reset schema, reset observation and `State` model), the OpenEnv client's docstrings, and the pytest
plugin's registered marker signature. With `reset` able to name a mode, the OpenEnv tests that held
the monotonic source still to read an exact timestamp pin `clock_mode="fixed"` at the reset
instead, and `still_monotonic_time` is left serving only the fixture CLI tests.

## Steps

1. `src/seahaven/openenv/env.py`
   - Import `ClockMode` from `seahaven.clock`.
   - `SeahavenState` gains, directly after `now`:
     `clock_mode: ClockMode.__value__ | None = Field(default=None, description="How the instance's
     clock moves: fixed, tick, running or wall; null before the first reset.")`. The `__value__`
     (the `Literal` itself) keeps the enum inline in the published schema, the shape
     `state_format` has, instead of a `$ref` to a `ClockMode` definition, which is what pydantic
     makes of a PEP 695 alias. The comment that counts the fields defaulting to `None` goes from
     five to six.
   - `SeahavenResetRequest` gains `clock_mode: ClockMode.__value__ | None = Field(default=None,
     description="How the instance's clock moves: fixed, tick, running or wall. Null uses the
     world's default.")`. The `now` description says it is where a blank instance's clock starts.
   - `RESET_ORDER = ("fixture", "startup", "now", "clock_mode", "state_format")`.
   - `SeahavenEnv.reset(..., now=None, clock_mode: str | None = None, state_format=None, ...)`,
     passed to `create`; the observation `metadata` gains `"clock_mode": instance.clock.mode`
     after `now`. Docstring: the metadata keys, and one sentence on `clock_mode`.
   - `SEAHAVEN_ERROR_KEY` comment and `SeahavenObservation` docstring name `clock_mode` among the
     reset's metadata keys.
2. `src/seahaven/openenv/client.py`: the class docstring example comment and the prose listing
   the reset's metadata keys add `clock_mode`; the prose names `reset(clock_mode=...)` beside
   `reset(state_format=...)`.
3. `src/seahaven/pytest_plugin.py`: `_MARKER_SIGNATURE` adds `clock_mode=None` after `now=None`;
   the `_fixture_and_arguments` docstring's list of forwarded keywords gains it.
4. Tests adapted:
   - `tests/test_env.py`, `tests/test_server.py`, `tests/test_client.py`: every
     `@pytest.mark.usefixtures("still_monotonic_time")` is removed and the reset in that test passes
     `clock_mode="fixed"`; observation metadata assertions gain `"clock_mode"`.
   - `tests/test_server.py`: `EPISODE` gains `"clock_mode": "fixed"`; `expected_document` expects
     `"clock_mode": "fixed"`; the `/web/reset` facts list gains `clock_mode`.
   - `worlds/projecttracker/tests/test_openenv.py`: the inline monkeypatch is replaced by
     `env.reset(fixture="empty", clock_mode="fixed")`.
   - `tests/conftest.py`: the `still_monotonic_time` docstring names only the fixture CLI.
   - `tests/test_env.py`: `RESET_PARAMETERS` gains `clock_mode` after `now`.
   - `tests/test_reset_schema.py`: the property order gains `clock_mode` after `now`.

## Tests

- `tests/test_env.py`
  - `test_reset_takes_a_clock_mode_and_reports_it`: `reset(now=S, clock_mode="tick")`; the
    observation's `clock_mode` is `tick` and `now` is S; a call reads S + 1 s; `state.clock_mode`
    is `tick` and `state.now` is S + 1 s.
  - `test_reset_without_a_clock_mode_takes_the_worlds_default`: the notes world reports `running`;
    a world built with `default_clock_mode="tick"` reports `tick`.
  - `test_an_unknown_clock_mode_refuses_the_reset_and_leaves_the_session_fresh`: the `WorldBug`
    names the four modes; no instance; a later reset works.
  - `test_state_before_reset_is_the_worlds_pinned_format_with_no_instance` gains
    `state.clock_mode is None`.
- `tests/test_reset_schema.py`
  - `test_clock_mode_is_published_as_the_four_modes`: `properties.clock_mode.anyOf` is the inline
    four-value string enum then null, with the model's description, and no `$defs.ClockMode`.
  - The reference-world property-order test covers `RESET_ORDER`.
- `tests/test_server.py`
  - `test_a_clock_mode_reaches_the_instance_over_the_wire`: typed client
    `reset(now=S, clock_mode="tick")`, a tool call reads S + 1 s, and `state()` carries `tick`.
  - `test_the_served_state_schema_publishes_clock_mode_as_an_enum`: `GET /schema`'s `state`
    carries `clock_mode` as the inline enum (the existing description test covers its text).
  - the existing whole-document tests cover `clock_mode` in the envelope over the wire.
- `tests/test_pytest_plugin.py`
  - `test_a_clock_mode_in_the_marker_passes_through`: marker `clock_mode="tick", now=NOW`; two
    calls read NOW + 1 s and NOW + 2 s, and `instance.clock.mode == "tick"`.
  - `test_the_registered_marker_names_every_keyword_world_instance_takes`: `pytest --markers`
    output's `seahaven(...)` line names exactly `World.instance`'s parameters, in order.
