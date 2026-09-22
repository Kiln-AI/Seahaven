---
status: complete
---

# Phase 1: Server — the reset message schema and `GET /seahaven/schemas`

## Overview

Publish what `reset()` accepts. A world-independent pydantic model, `SeahavenResetRequest`
(OpenEnv's `ResetRequest` plus Seahaven's keywords), is narrowed per world by `for_world(world)`:
the `state_format` enum, the fixture enum (filled on every schema generation), and a
`SeahavenStartup` model built from the startup hooks of the whole composition. `app()` builds it
before `create_app`, so a bad hook fails startup with a `WorldBug`, and registers
`GET /seahaven/schemas`, which answers upstream's own `/schema` result plus `reset`. Docs gain a
short section and `World.state_formats`.

## Steps

1. `src/seahaven/world.py`: add a read-only property
   `World.state_formats -> frozenset[str]`, the names this world registered (not the built-ins).
2. `src/seahaven/openenv/env.py`:
   - `class SeahavenResetRequest(ResetRequest)` with `model_config = ConfigDict(extra="forbid")`
     (keeping the base's `json_schema_extra` examples) and fields `fixture`, `now`,
     `state_format`, `startup`, each `... | None = None` with a short docs-style description.
   - `@classmethod for_world(cls, world) -> type[SeahavenResetRequest]`: `create_model(
     "SeahavenResetRequest", __base__=cls, fixture=Annotated[str | None, Field(default=None,
     description=..., json_schema_extra=fill)], state_format=Literal[*names] | None, startup=
     _startup_model(world) | None)`. `fill` closes over the world and writes
     `anyOf [{type: string, enum: ids}, {type: null}]`, or `[{type: null}]` with no fixtures.
     `names` is `sorted(BUILTIN_FORMATS) + sorted(world.state_formats)`.
   - `_startup_model(world)`: walk `world.composition().nodes` and each node's startup hooks;
     keyword-only parameters become `(annotation or Any, default or ...)`; `**kwargs` anywhere makes
     the model `extra="allow"`, else `"forbid"`; a keyword named twice with different annotations
     is a `WorldBug` naming both hooks; an unresolvable annotation (`NameError`) or an annotation
     with no JSON schema is a `WorldBug` naming the hook and the keyword (found by building each
     field alone).
   - Bound keywords (decided in review): a keyword bound on a hook's node with
     `add_world(..., startup={...})` never reaches that hook from the caller, so that hook's
     parameter is skipped for `required` and for the conflict check. A keyword only bound hooks
     name is still accepted by the server (and ignored), so it is published optional, untyped and
     with no default. Names pydantic reserves (`_x`, `model_config`) are a `WorldBug`; the
     `BaseModel`-shadowing warning is suppressed.
   - `class SeahavenSchemaResponse(SchemaResponse)` with `reset: dict[str, Any]`.
   - Export both from `env.py` `__all__` and from `seahaven.openenv.__all__`.
3. `src/seahaven/openenv/__init__.py`:
   - `SCHEMAS_PATH = "/seahaven/schemas"` and `_serve_schemas(served, reset_cls)`: find upstream's
     `GET /schema` `APIRoute` (else `RuntimeError`), register the route with
     `include_in_schema=False, response_model=SeahavenSchemaResponse`, awaiting upstream's handler
     when its result is awaitable.
   - `app()`: build `reset_cls` before `create_app`; call `_serve_schemas` after
     `_refuse_mcp_transport`, always. One docstring paragraph.
4. Docs: `serving_and_openenv.md` gains `### GET /seahaven/schemas adds the reset message` after
   the `/schema` section, plus one sentence in "The web console"; `reference/api.md` gains
   `world.state_formats`.

## Tests

New file `tests/test_reset_schema.py`.

- `test_the_reference_world_publishes_its_reset_message`: through a real server, the three shared
  keys equal `/schema`'s; fixture enum is `["agency", "empty", "small_startup"]`; `state_format`
  enum holds `seahaven.state/1`; `$defs.SeahavenStartup.properties` has `user_id`;
  `additionalProperties` false at both levels; no `control_tools`; title `SeahavenResetRequest`.
- `test_the_route_is_not_in_openapi`.
- `test_the_route_is_served_without_the_console`.
- `test_a_fixture_frozen_while_serving_appears_on_the_next_request`.
- `test_a_world_with_no_fixtures_publishes_fixture_as_null_only`.
- `test_a_world_with_no_startup_hooks_publishes_an_empty_startup`.
- `test_a_required_keyword_is_required_inside_startup`, description from `Annotated`, unannotated
  keyword has no `type`, `**kwargs` hook opens `startup`.
- `test_registered_state_formats_are_in_the_enum`.
- `test_an_added_worlds_keyword_is_published`, same keyword same type → one property with the
  first default, different types → `WorldBug` from `app()` naming both hooks.
- `test_an_unschemable_annotation_fails_app_naming_hook_and_keyword`, and an unresolvable one.
- `test_the_model_fields_match_the_reset_signature` (drift) and
  `test_the_startup_properties_are_the_accepted_startup_kwargs`.
- `test_a_message_built_from_the_schema_validates_and_resets_over_the_socket`: jsonschema-validate
  a message from the schema, send it over `/ws`, get a live episode.
- `test_a_missing_upstream_schema_route_fails_the_build`.
- `world.state_formats` unit test in `tests/test_world.py` (or the state test module).
