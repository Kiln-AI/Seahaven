"""`SeahavenClient`: the parsers on their own, and the whole client on a server.

The parsers are unit-tested against the frames OpenEnv actually sends, because a
parser that is only ever exercised through a happy path proves nothing about the
frame it is given. Everything below them is driven against a real server in both
of the base client's modes, because "sync and async follow the base client's
dual mode" is a claim about `_dispatch` and is not true by construction.
"""

import asyncio
from typing import Any

import pytest

from seahaven.world import World
from tests.conftest import INSTANT_ISO

pytest.importorskip("openenv", reason="the serve extra is not installed")

from openenv.core.env_server.mcp_types import CallToolAction, ListToolsAction

from seahaven.openenv import SeahavenClient, SeahavenObservation, SeahavenState
from tests.serving import serving

UNCONNECTED = "http://127.0.0.1:1"


@pytest.fixture
def client() -> SeahavenClient:
    """A client that never connects: enough to drive the three parsers."""
    return SeahavenClient(base_url=UNCONNECTED)


# --- the parsers -----------------------------------------------------------


def test_step_payload_is_the_action_as_a_dict(client: SeahavenClient) -> None:
    assert client._step_payload(CallToolAction(tool_name="ping", arguments={"message": "hi"})) == {
        "type": "call_tool",
        "tool_name": "ping",
        "arguments": {"message": "hi"},
        "metadata": {},
    }
    assert client._step_payload(ListToolsAction()) == {"type": "list_tools", "metadata": {}}


def test_parse_result_answers_a_typed_observation(client: SeahavenClient) -> None:
    result = client._parse_result(
        {
            "observation": {"tool_name": "ping", "result": {"n": 1}, "error": None, "metadata": {}},
            "reward": None,
            "done": False,
        }
    )
    assert isinstance(result.observation, SeahavenObservation)
    assert (result.observation.tool_name, result.observation.result) == ("ping", {"n": 1})
    assert (result.reward, result.done, result.metadata) == (None, False, None)


def test_parse_result_carries_an_error_through(client: SeahavenClient) -> None:
    error = {"code": "not_found", "message": "no note n9", "details": {"key": "n9"}}
    result = client._parse_result({"observation": {"tool_name": "fetch", "error": error}})
    assert result.observation.error == error
    assert result.observation.result is None
    assert result.done is False


def test_parse_state_answers_a_typed_state(client: SeahavenClient) -> None:
    state = client._parse_state(
        {
            "episode_id": "ep-1",
            "step_count": 3,
            "fixture": "start",
            "now": INSTANT_ISO,
            "world": "testworld",
        }
    )
    assert isinstance(state, SeahavenState)
    assert (state.episode_id, state.step_count) == ("ep-1", 3)
    assert (state.fixture, state.now, state.world) == ("start", INSTANT_ISO, "testworld")


def test_state_answers_none_for_the_fields_a_pre_reset_frame_leaves_out(
    client: SeahavenClient,
) -> None:
    """`fixture` and `now` are absent from a state before the first reset.

    `State` allows extra fields, so a field Seahaven declares is the only reason
    reading one that the frame did not carry answers `None` instead of raising.
    A harness that logs `state.fixture` every step must not fail on step zero.
    """
    state = client._parse_state({"episode_id": "ep-1", "step_count": 0, "world": "testworld"})
    assert (state.fixture, state.now) == (None, None)


def test_state_refuses_a_frame_that_does_not_name_a_world(client: SeahavenClient) -> None:
    """`world` is the one field of a Seahaven state that is not optional."""
    with pytest.raises(ValueError, match="world"):
        client._parse_state({"episode_id": "ep-1", "step_count": 0})


def test_the_observation_model_refuses_a_frame_it_does_not_know(client: SeahavenClient) -> None:
    """`SeahavenObservation` forbids extras, which is why `list_tools` does not use it."""
    with pytest.raises(ValueError, match="tools"):
        client._parse_result({"observation": {"tools": []}})


# --- the client, on a server -----------------------------------------------


def test_the_client_drives_a_world_synchronously(world: World) -> None:
    with serving(world) as url, SeahavenClient(base_url=url) as env:
        reset = env.reset(now=INSTANT_ISO)
        assert reset.observation.result["now"] == INSTANT_ISO
        names = [tool["name"] for tool in env.list_tools()]
        assert "rows" in names and "controller_run_sql" not in names
        assert env.call("execute", sql="INSERT INTO notes VALUES ('n1', 'b', 0)").result == {
            "rowcount": 1
        }
        observation = env.call("rows", sql="SELECT id FROM notes")
        assert observation.result == [{"id": "n1"}]
        assert observation.error is None
        state = env.state()
        assert (state.world, state.fixture, state.now) == (world.name, None, INSTANT_ISO)


def test_the_client_drives_a_world_asynchronously(world: World) -> None:
    async def drive(url: str) -> None:
        async with SeahavenClient(base_url=url) as env:
            await env.reset(now=INSTANT_ISO)
            names = [tool["name"] for tool in await env.list_tools()]
            assert "rows" in names
            observation = await env.call("rows", sql="SELECT 1 AS n")
            assert observation.result == [{"n": 1}]
            state = await env.state()
            assert state.now == INSTANT_ISO

    with serving(world) as url:
        asyncio.run(drive(url))


def test_entering_the_client_asynchronously_also_connects(world: World) -> None:
    """`__aenter__`'s half of the same rule, which is a separate method."""

    async def drive(url: str) -> None:
        client = SeahavenClient(base_url=url)
        assert client._ws is None
        async with client as env:
            assert env is client
            assert env._ws is not None

    with serving(world) as url:
        asyncio.run(drive(url))


def test_call_answers_the_observation_and_never_raises_on_a_tool_error(world: World) -> None:
    with serving(world) as url, SeahavenClient(base_url=url) as env:
        env.reset()
        observation = env.call("no_such_tool")
        assert observation.result is None
        assert observation.error == {
            "code": "unknown_tool",
            "message": "unknown tool: no_such_tool",
            "details": {"name": "no_such_tool"},
        }
        # And the session is still good: a tool error is data, not a failure.
        assert env.call("rows", sql="SELECT 1 AS n").result == [{"n": 1}]


def test_list_tools_answers_plain_dictionaries(world: World) -> None:
    with serving(world) as url, SeahavenClient(base_url=url) as env:
        env.reset()
        tools = env.list_tools()
        assert isinstance(tools, list)
        rows: dict[str, Any] = next(tool for tool in tools if tool["name"] == "rows")
        assert sorted(rows) == ["description", "input_schema", "name"]
        assert rows["input_schema"]["properties"]["sql"]["type"] == "string"


def test_the_client_is_a_context_manager_that_closes_its_session(world: World) -> None:
    with serving(world) as url:
        client = SeahavenClient(base_url=url)
        assert client._ws is None
        with client as env:
            # `__enter__` answers this client, not a bare `EnvClient`: the two
            # verbs the typed client exists for have to survive a `with`. And it
            # has connected on the way in -- the base connects lazily on the
            # first frame too, so without this the session would open on the
            # first `reset` and every timing a harness took would include it.
            assert env is client
            assert env._ws is not None
            env.reset()
        # The session is gone with the connection; a new client gets a new one.
        with SeahavenClient(base_url=url) as second:
            second.reset()
            assert second.call("rows", sql="SELECT * FROM notes").result == []
