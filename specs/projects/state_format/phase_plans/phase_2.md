---
status: complete
---

# Phase 2: Formats and the world pin

## Overview

Phase 1 captured the change log. This phase is the surface over it: the state document, the
formats that fill its `state` key, and the pin that decides which format a world produces. After
it, `inst.state()` answers the whole `functional_spec.md` §3.1 document in process, a world
registers formats of its own, and every `World(...)` in the repo names the format its instances
save in.

Nothing is removed. `Instance.changes()`, `Change`, `render()` and the long-lived session stay
until Phase 4; OpenEnv still answers its placeholder state until Phase 3.

## Steps

1. **`src/seahaven/state.py` (new) — the document and the two built-in formats.**

   ```python
   type Formatter = Callable[["World", "Instance | None"], dict[str, Any]]

   SEAHAVEN_STATE_V1 = "seahaven.state/1"
   SEAHAVEN_STATE_LAST_STEP_V1 = "seahaven.state+last_step/1"
   BUILTIN_PREFIX = "seahaven."
   BUILTIN_FORMATS: Mapping[str, Formatter]

   def check_format_name(name: str) -> None
   def envelope(world: World, instance: Instance | None, format: str) -> dict[str, Any]
   def document(world, instance, format, formatter) -> dict[str, Any]
   def state_v1(world, instance) -> dict[str, Any]
   def state_last_step_v1(world, instance) -> dict[str, Any]
   ```

   `check_format_name` refuses anything but `^[A-Za-z0-9_.+-]+/[1-9][0-9]*$`, with a `WorldBug`
   naming the rule. `envelope` writes FS §3.1's ten fields in FS §3.1's order, with every
   instance-dependent one `None` and `call_count` 0 when there is no instance; `seahaven_version`
   is read off the `seahaven` package, imported at module level (the module is imported from
   `world.py` during the package's own import, so the attribute is read at call time).
   `document` is `{**envelope(...), "state": formatter(world, instance)}` and raises `WorldBug`
   if the formatter answered anything but a dict. `state_v1` is `{"db": {"log": [...]}}` over
   `instance.change_log()`, empty with no instance; `state_last_step_v1` filters it to
   `record.i == instance.call_count - 1`, so an `i: None` record is never in it.

2. **`src/seahaven/world.py` — the pin, the registry, and the resolution.**

   - `World.__init__(..., *, state_format: str | None = None, ...)`: `None` is refused with a
     `WorldBug` naming the built-ins (ARCH §13's row; a bare required parameter could only raise
     `TypeError`). Then `check_format_name`, then the `seahaven.` names are checked against
     `BUILTIN_FORMATS`. Stored as `self.pinned_state_format`, because `world.state_format` is the
     registration method below — a deviation from ARCH §5.2, which gives the one name to both.
   - `self._state_formats: dict[str, Formatter] = {}`, a fourth registry beside the tools, the
     middleware and the startup hooks; `__copy__` copies it like the other three.
   - `world.state_format(name)` returns the decorator FS §6 spells `@world.state_format("acme.state/1")`:
     `check_format_name`, refuse the `seahaven.` prefix, and on registration refuse a
     non-callable and a duplicate.
   - `world.resolve_state_format(name) -> Formatter`: built-ins first, then the world's; unknown
     is a `WorldBug` naming both sets.
   - `RESET_ARGUMENTS` gains `state_format`, so a startup hook naming it is refused at
     registration by the existing check.
   - `World.instance(..., state_format: str | None = None)` passes it to the manager.

3. **`src/seahaven/instances.py` — creation, the instance's own provenance, and `state()`.**

   - `InstanceManager.create(..., state_format: str | None = None, episode_id: str | None = None)`.
     In order, all before a directory exists: `_check_startup_kwargs`; `startup =
     serialise(dict(kwargs))`, so a keyword that is not JSON-able is a `WorldBug` now rather than
     at `state()` time (the hooks still receive the raw values); `formatter =
     world.resolve_state_format(state_format or world.pinned_state_format)`; then the fixture
     checks as they are.
   - `Instance.__init__` gains `state_format: str`, `formatter: Formatter`, `episode_id: str`
     (`episode_id or instance_id`), `caller_seed: int | None`, `fixture_sha256: str | None` (the
     fixture sidecar's, `None` for a blank instance) and `startup: dict[str, Any]`.
   - `Instance.state(format: str | None = None) -> dict[str, Any]`: under `_held()`, resolve the
     format (the instance's own when none is named), set the formatting guard, and return
     `state.document(...)`. The guard is saved and restored rather than cleared, because a
     formatter may read `instance.state(format=...)` to build a variation of a built-in (FS §6).
   - `_refuse_if_formatting()` at the top of `_call` and `_bulk`: a formatter that writes lands on
     it through the re-entrant lock and gets a `WorldBug` saying a formatter only reads.

4. **`src/seahaven/cli/templates/base/src/PACKAGE/world.py.tmpl`** — `state_format="seahaven.state/1"`,
   with the two-line comment ARCH §10 asks for: the pin is chosen at creation, and changing it
   changes what every eval of the world saves, so bump `version` with it.

5. **The sweep** — `state_format=` on every `World(...)` in `worlds/`, `extensions/`, `tests/`,
   `README.md` and `src/seahaven/docs/`, including the examples the docs test executes. The test
   helpers (`tests/conftest.py`'s `build_world`, `worlds/projecttracker/tests/conftest.py`,
   `extensions/seahaven-xmlrpc/tests/conftest.py`) default it, so a call site that does not care
   does not say it.

## Tests

`tests/test_state.py` (new):

- `test_the_envelope_carries_every_field_of_a_fixture_instance` and
  `test_a_blank_instance_has_no_fixture` — field by field, in FS §3.1 order.
- `test_startup_keywords_are_the_ones_the_hooks_were_given` and
  `test_an_instance_with_no_startup_keywords_has_an_empty_object`.
- `test_a_startup_keyword_that_is_not_json_able_is_refused` — at creation, not at `state()`.
- `test_the_document_is_the_envelope_and_the_formatter_output`.
- `test_state_v1_is_the_whole_log` and `test_state_v1_is_empty_before_any_call`.
- `test_last_step_holds_only_the_last_call` , `test_last_step_documents_concatenate_into_the_log`,
  `test_last_step_is_empty_before_any_call`, `test_last_step_never_holds_a_bulk_write`.
- `test_a_second_read_between_calls_answers_the_same_document` — the idempotence FS §4 claims.
- `test_a_format_can_be_named_per_call` — `inst.state(format=...)` against the other built-in.
- `test_a_custom_formatter_fills_state_and_nothing_else` — the envelope is byte-identical to the
  built-in's.
- `test_a_custom_formatter_can_build_on_a_built_in` — it reads `inst.state(format=...)["state"]`
  from inside itself, which is what the saved-and-restored guard is for.
- `test_a_formatter_that_returns_something_else_is_a_world_bug`.
- `test_a_formatter_that_writes_is_a_world_bug` and `test_a_formatter_that_bulk_writes_is_a_world_bug`.
- `test_a_format_name_must_be_family_slash_major` (parametrised over bad names),
  `test_a_custom_format_cannot_take_a_seahaven_name`, `test_a_format_is_registered_once`,
  `test_a_formatter_must_be_callable`.
- `test_a_world_must_pin_a_format`, `test_an_unknown_builtin_is_refused_at_world`,
  `test_an_unregistered_custom_pin_is_refused_at_the_first_instance`,
  `test_a_custom_pin_registered_after_the_world_line_works`.
- `test_an_unknown_format_is_refused_before_a_directory_exists`.
- `test_each_builtin_answers_a_document_with_no_instance` — `state.document(world, None, ...)`.
- `test_a_formatter_that_raises_on_no_instance_surfaces_as_its_own_error`.
- `test_state_after_destroy_is_refused`.
- `test_the_builtin_documents_validate_against_the_published_schema` — both built-ins, an
  episode with an insert, an update, a delete and a bulk write, against
  `tests/support/state_v1.schema.json`.
- `test_state_does_no_database_work` — an exec tracer over `state()` counts zero statements.

`tests/test_world.py` (additions):

- `test_a_startup_hook_cannot_take_state_format` — the existing `RESET_ARGUMENTS` refusal.
- `test_a_copy_carries_the_state_formats` and `test_a_format_registered_after_a_copy_does_not_reach_it`.

`tests/test_cli_new.py` (addition):

- `test_the_scaffold_pins_a_state_format` — the generated `world.py` names `seahaven.state/1`
  (`seahaven check` on the scaffold is already covered).

`tests/support/state_v1.schema.json` (new): the JSON Schema of the built-in documents — the
envelope's ten fields plus `state.db.log`'s seven, `additionalProperties: false` at both levels
so a field added by accident fails the test.
