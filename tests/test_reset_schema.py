"""`GET /seahaven/schemas`: `/schema` plus the JSON schema of the reset message.

The route is driven through a real server where the transport is what is being
claimed, and the model is asked for its schema directly where a server adds
nothing. A world whose startup hooks the schema cannot describe fails `app()`,
so those tests call `app()` and nothing more.
"""

import asyncio
import copy
import inspect
import json
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import jsonschema
import pytest
from pydantic import Field

import projecttracker
from seahaven.ctx import Ctx
from seahaven.errors import WorldBug
from seahaven.world import World
from tests.conftest import build_world, composable_world

if TYPE_CHECKING:  # deliberately not importable at runtime: see the refusal below
    from decimal import Decimal

pytest.importorskip(
    "seahaven.openenv", exc_type=ImportError, reason="the serve extra does not import here"
)

from fastapi import FastAPI
from fastapi.routing import APIRoute
from openenv.core.env_server.types import SchemaResponse

import seahaven.openenv
from seahaven.openenv import (
    SCHEMAS_PATH,
    SeahavenClient,
    SeahavenEnv,
    SeahavenResetRequest,
    _serve_schemas,
    app,
)
from tests.serving import serving


def _get(url: str) -> Any:
    with urllib.request.urlopen(url) as response:
        assert response.status == 200
        return json.loads(response.read())


def _reset_schema(world: World) -> dict[str, Any]:
    return SeahavenResetRequest.for_world(world).model_json_schema()


def _startup_schema(world: World) -> dict[str, Any]:
    return _reset_schema(world)["$defs"]["SeahavenStartup"]


def _fixture_ids(schema: dict[str, Any]) -> list[str] | None:
    """The fixture enum, or `None` when the field is null-only."""
    kinds = schema["properties"]["fixture"]["anyOf"]
    assert kinds[-1] == {"type": "null"}
    return kinds[0]["enum"] if len(kinds) == 2 else None


# --- the route ----------------------------------------------------------------


def test_the_reference_world_publishes_its_reset_message() -> None:
    with serving(projecttracker.world) as url:
        extended = _get(url + SCHEMAS_PATH)
        plain = _get(url + "/schema")

    assert sorted(extended) == ["action", "observation", "reset", "state"]
    assert {key: extended[key] for key in plain} == plain
    reset = extended["reset"]
    assert reset["title"] == "SeahavenResetRequest"
    assert reset["additionalProperties"] is False
    # In the order the console lays its form out: fixture and startup first,
    # OpenEnv's own fields last.
    assert list(reset["properties"]) == [
        "fixture",
        "startup",
        "now",
        "clock_mode",
        "state_format",
        "seed",
        "episode_id",
    ]
    assert _fixture_ids(reset) == ["agency", "empty", "small_startup"]
    assert "seahaven.state/1" in reset["properties"]["state_format"]["anyOf"][0]["enum"]
    assert reset["properties"]["startup"]["anyOf"] == [
        {"$ref": "#/$defs/SeahavenStartup"},
        {"type": "null"},
    ]
    startup = reset["$defs"]["SeahavenStartup"]
    assert list(startup["properties"]) == ["user_id"]
    assert startup["additionalProperties"] is False


def test_the_route_is_not_in_the_openapi_schema() -> None:
    with serving(projecttracker.world) as url:
        paths = _get(url + "/openapi.json")["paths"]
    assert "/schema" in paths
    assert SCHEMAS_PATH not in paths


def test_the_route_is_served_without_the_console(world: World) -> None:
    with serving(world, console=False) as url:
        assert "reset" in _get(url + SCHEMAS_PATH)


def test_a_fixture_frozen_while_serving_appears_on_the_next_request(world: World) -> None:
    with serving(world) as url:
        assert _fixture_ids(_get(url + SCHEMAS_PATH)["reset"]) is None
        with world.instance(None) as instance:
            instance.freeze("start", "Empty.")
        assert _fixture_ids(_get(url + SCHEMAS_PATH)["reset"]) == ["start"]


def test_a_message_built_from_the_schema_validates_and_resets_over_the_socket(
    tmp_path: Path,
) -> None:
    """The schema describes the call the server really takes, not a neighbour of it."""
    world = copy.copy(projecttracker.world)
    world.work_dir = tmp_path
    with world.instance("agency") as instance:
        user_id = instance.inspect().rows("SELECT id FROM users ORDER BY id LIMIT 1")[0]["id"]

    with serving(world) as url:
        schema = _get(url + SCHEMAS_PATH)["reset"]
        fixture = _fixture_ids(schema)[0]  # ty: ignore[not-subscriptable]
        message = {"fixture": fixture, "seed": 7, "startup": {"user_id": user_id}}
        jsonschema.validate(message, schema)
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({"control_tools": True}, schema)
        with SeahavenClient(base_url=url) as env:
            assert env.reset(**message).observation.metadata["fixture"] == fixture
            state = env.state()
    assert state.fixture is not None
    assert state.fixture.id == fixture
    assert state.startup == {"user_id": user_id}


def test_a_missing_upstream_schema_route_fails_the_build() -> None:
    served = FastAPI()
    with pytest.raises(RuntimeError, match="/schema"):
        _serve_schemas(served, SeahavenResetRequest)


def test_a_sync_upstream_handler_is_extended_too(world: World) -> None:
    served = FastAPI()

    def get_schemas() -> SchemaResponse:
        return SchemaResponse(action={}, observation={}, state={})

    served.router.routes.append(APIRoute("/schema", get_schemas, methods=["GET"]))
    _serve_schemas(served, SeahavenResetRequest.for_world(world))

    route = next(r for r in served.router.routes if getattr(r, "path", None) == SCHEMAS_PATH)
    answer = asyncio.run(route.endpoint())  # ty: ignore[unresolved-attribute]
    assert answer.action == {}
    assert answer.reset["title"] == "SeahavenResetRequest"


# --- fixtures and formats -------------------------------------------------------


def test_a_world_with_no_fixtures_directory_publishes_fixture_as_null_only(
    tmp_path: Path,
) -> None:
    world = build_world(tmp_path, fixtures_dir=tmp_path / "nowhere")
    assert _reset_schema(world)["properties"]["fixture"]["anyOf"] == [{"type": "null"}]


def test_registered_state_formats_follow_the_built_ins(world: World) -> None:
    @world.state_format("notes.summary/1")
    def summary(world: World, instance: Any) -> dict[str, Any]:
        return {}

    names = _reset_schema(world)["properties"]["state_format"]["anyOf"][0]["enum"]
    assert names == [
        "seahaven.state+calls/1",
        "seahaven.state+last_step/1",
        "seahaven.state/1",
        "notes.summary/1",
    ]
    assert world.state_formats == frozenset({"notes.summary/1"})


def test_clock_mode_is_published_as_the_four_modes(world: World) -> None:
    """Inline, as `state_format` is, so the console offers the modes as a choice."""
    schema = _reset_schema(world)
    clock_mode = schema["properties"]["clock_mode"]
    assert clock_mode["anyOf"] == [
        {"type": "string", "enum": ["fixed", "tick", "running", "wall"]},
        {"type": "null"},
    ]
    assert clock_mode["default"] is None
    assert clock_mode["description"] == (
        "How the instance's clock moves: fixed, tick, running or wall. Null uses the world's "
        "default."
    )
    assert "ClockMode" not in schema.get("$defs", {})


# --- startup --------------------------------------------------------------------


def test_a_world_with_no_startup_hooks_publishes_an_empty_startup(world: World) -> None:
    startup = _startup_schema(world)
    assert startup["properties"] == {}
    assert startup["additionalProperties"] is False


def test_a_startup_keyword_is_built_from_its_hook_parameter(world: World) -> None:
    @world.instance_startup
    def hook(
        ctx: Ctx,
        *,
        region: str,
        tier: Annotated[int, Field(description="The account tier.")] = 2,
        anything=None,
    ) -> None:
        """One required keyword, one described, one unannotated."""

    startup = _startup_schema(world)
    assert startup["required"] == ["region"]
    assert startup["properties"]["region"]["type"] == "string"
    assert startup["properties"]["tier"] == {
        "default": 2,
        "description": "The account tier.",
        "title": "Tier",
        "type": "integer",
    }
    assert "type" not in startup["properties"]["anything"]
    # Optional at the top level all the same: omitting it is `{}`, and the hook
    # refuses the missing keyword when the instance is made.
    assert "required" not in _reset_schema(world)


def test_a_hook_taking_var_kwargs_opens_startup(world: World) -> None:
    @world.instance_startup
    def hook(ctx: Ctx, *, region: str = "us", **rest: object) -> None:
        """Takes anything."""

    startup = _startup_schema(world)
    assert list(startup["properties"]) == ["region"]
    assert startup["additionalProperties"] is True


def test_an_added_worlds_keywords_are_published(world: World) -> None:
    child, other = composable_world("child"), composable_world("other")

    @world.instance_startup
    def host_hook(ctx: Ctx, *, region: str = "us") -> None:
        """The host's."""

    @child.instance_startup
    def child_hook(ctx: Ctx, *, region: str = "eu", seat: int = 1) -> None:
        """Shares `region` with the host, at the same type."""

    @other.instance_startup
    def other_hook(ctx: Ctx, *, team: str | None = None) -> None:
        """A keyword of its own."""

    child.add_world(other, name="other")
    world.add_world(child, name="child")

    properties = _startup_schema(world)["properties"]
    assert list(properties) == ["region", "seat", "team"]
    assert properties["region"]["default"] == "us"


def test_one_keyword_with_two_types_fails_app_naming_both_hooks(world: World) -> None:
    child = composable_world("child")

    @world.instance_startup
    def host_hook(ctx: Ctx, *, region: str = "us") -> None:
        """A string."""

    @child.instance_startup
    def child_hook(ctx: Ctx, *, region: int = 1) -> None:
        """An integer."""

    world.add_world(child, name="child")
    with pytest.raises(WorldBug, match=r"'region'.*host_hook.*child_hook"):
        app(world)


def test_a_keyword_bound_on_its_node_is_optional_and_untyped(world: World) -> None:
    """The caller's value never reaches a bound hook, and the server accepts it all the same."""
    child = composable_world("child")

    @child.instance_startup
    def child_hook(ctx: Ctx, *, account: str) -> None:
        """Required, but bound by the host."""

    world.add_world(child, name="child", startup={"account": "acme"})

    startup = _startup_schema(world)
    assert "required" not in startup
    assert startup["properties"]["account"] == {"title": "Account"}
    jsonschema.validate({}, startup | {"$defs": {}})
    with world.instance(None, startup={}):
        pass
    with world.instance(None, startup={"account": "ignored"}):
        pass


def test_a_bound_keyword_does_not_conflict_with_an_unbound_one(world: World) -> None:
    child = composable_world("child")

    @world.instance_startup
    def host_hook(ctx: Ctx, *, region: str) -> None:
        """The caller's value reaches this one."""

    @child.instance_startup
    def child_hook(ctx: Ctx, *, region: int) -> None:
        """Bound, so the caller's value never reaches this one."""

    world.add_world(child, name="child", startup={"region": 1})

    startup = _startup_schema(world)
    assert startup["properties"]["region"]["type"] == "string"
    assert startup["required"] == ["region"]


def test_two_separately_written_annotated_types_conflict_and_say_so(world: World) -> None:
    child = composable_world("child")

    @world.instance_startup
    def host_hook(ctx: Ctx, *, region: Annotated[str, Field(description="Where.")] = "") -> None:
        """One `Annotated`."""

    @child.instance_startup
    def child_hook(ctx: Ctx, *, region: Annotated[str, Field(description="Where.")] = "") -> None:
        """An equal-looking one, which is a different object."""

    world.add_world(child, name="child")
    with pytest.raises(WorldBug, match="module-level alias"):
        app(world)


@pytest.mark.parametrize("name", ["_private", "model_config"])
def test_a_keyword_pydantic_reserves_fails_app_naming_it(world: World, name: str) -> None:
    exec(f"def hook(ctx, *, {name}=None): pass", namespace := {})
    world.instance_startup(namespace["hook"])
    with pytest.raises(WorldBug, match=rf"startup hook hook: keyword {name!r}.*rename it"):
        app(world)


@pytest.mark.filterwarnings("error")
def test_a_keyword_shadowing_a_model_method_is_published_quietly(world: World) -> None:
    @world.instance_startup
    def hook(ctx: Ctx, *, schema: str = "", copy: bool = False) -> None:
        """Two names `BaseModel` also uses."""

    assert list(_startup_schema(world)["properties"]) == ["schema", "copy"]


def test_an_unschemable_annotation_fails_app_naming_hook_and_keyword(world: World) -> None:
    class Opaque:
        pass

    @world.instance_startup
    def hook(ctx: Ctx, *, fine: str = "", handle: Opaque | None = None) -> None:
        """No JSON schema for `handle`."""

    with pytest.raises(WorldBug, match=r"startup hook .*hook: keyword 'handle': no JSON schema"):
        app(world)


def test_an_unresolvable_annotation_fails_app_naming_the_hook(world: World) -> None:
    # A string, because an unquoted one fails registration itself on 3.14.
    def hook(ctx: Ctx, *, when: "Decimal | None" = None) -> None:  # noqa: UP037
        """Annotated with a name that does not exist."""

    world.instance_startup(hook)
    with pytest.raises(WorldBug, match=r"hook is annotated with 'Decimal'"):
        app(world)


# --- drift ----------------------------------------------------------------------


def test_the_model_fields_are_the_reset_parameters() -> None:
    """A reset parameter with no schema field, or the reverse, fails here."""
    parameters = set(inspect.signature(SeahavenEnv.reset).parameters)
    assert set(SeahavenResetRequest.model_fields) == parameters - {
        "self",
        "control_tools",
        "unknown",
    }


def test_the_startup_properties_are_the_accepted_startup_kwargs() -> None:
    world = projecttracker.world
    assert set(_startup_schema(world)["properties"]) == world.composition().accepted_startup_kwargs


def test_the_models_are_exported() -> None:
    assert {"SeahavenResetRequest", "SeahavenSchemaResponse"} <= set(seahaven.openenv.__all__)
