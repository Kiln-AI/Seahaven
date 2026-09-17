"""The whole thing over a socket: uvicorn, websockets, sessions, the gate.

`test_env.py` proves the environment class and `test_client.py` proves the
client. This module proves the two of them together through the transport that
sits between, because that is where this project's defects have lived: a frame
that will not serialise, a session that is not a session, a lock that is held on
the wrong thread. Every test here starts a real server on a real port.

The stock `GenericEnvClient` is driven beside the typed one throughout. A world
that can only be reached by Seahaven's own client is not on the OpenEnv wire,
and the claim that it is is worth a test rather than a paragraph.
"""

import asyncio
import json
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import seahaven
from seahaven import instances
from seahaven.ctx import Ctx
from seahaven.errors import WorldBug
from seahaven.world import World
from tests.conftest import INSTANT_ISO, build_world

# The subpackage and not `openenv`: what this module imports is
# `seahaven.openenv`, so that is what has to import for the tests below to mean
# anything. Only an `ImportError` skips -- an extra that is absent, or installed
# and unimportable. Anything else raises, and CI asserts this import separately,
# because an installed extra that skips quietly is a green run that tested none
# of this.
pytest.importorskip(
    "seahaven.openenv", exc_type=ImportError, reason="the serve extra does not import here"
)

from openenv import GenericEnvClient
from openenv.core.env_server.mcp_types import CallToolAction, ListToolsAction
from openenv.core.utils import convert_to_ws_url
from websockets.asyncio.client import connect as ws_connect

from seahaven.openenv import SeahavenClient
from seahaven.openenv.env import SeahavenObservation
from tests.serving import serving

# Long enough that two threads genuinely overlap inside SQLite on a warm cache,
# short enough that the gate tests stay in the same order of magnitude as the
# rest of the suite. The assertion is about overlap, never about duration.
SLOW_ROWS = 300_000

SESSIONS = 500

# How long a connection that has a slot is given to prove it is not being
# refused. A refusal arrives immediately; this only bounds the quiet case.
UNSOLICITED_TIMEOUT = 2.0

# The idle timeout the reaper test serves with, and how long it waits for the
# sweep. OpenEnv checks every `max(timeout / 4, 5.0)` seconds, so the wait is
# floored at that 5 seconds however small the timeout is.
REAPED_AFTER = 1.0
REAP_DEADLINE = 30.0


def insert(id: str) -> str:
    return f"INSERT INTO notes VALUES ('{id}', 'a body', 0)"


def ids(observation: Any) -> list[str]:
    return [row["id"] for row in observation.result]


@pytest.fixture
def slow_world(tmp_path: Path) -> World:
    """A world with a tool that spends real time inside SQLite, for the gate."""
    world = build_world(tmp_path)

    @world.tool
    def slow(ctx: Ctx) -> dict[str, float]:
        """Burn time in the engine and report the wall clock either side of it."""
        started = time.perf_counter()
        ctx.db.one(
            "WITH RECURSIVE counter(x) AS ("
            "  SELECT 1 UNION ALL SELECT x + 1 FROM counter WHERE x < ?"
            ") SELECT count(*) AS n FROM counter",
            SLOW_ROWS,
        )
        return {"started": started, "ended": time.perf_counter()}

    return world


@pytest.fixture
def trivial_world(tmp_path: Path) -> Iterator[World]:
    """The smallest real world there is, for the sessions that are counted in hundreds."""
    world = World(
        "smoke",
        "1.0.0",
        "CREATE TABLE notes (id TEXT PRIMARY KEY) STRICT;",
        fixtures_dir=tmp_path / "fixtures",
        work_dir=tmp_path / "work",
        state_format="seahaven.state/1",
    )

    @world.tool
    def echo(ctx: Ctx, message: str = "pong") -> dict[str, str]:
        """Answer with what it was given."""
        return {"message": message}

    yield world


# --- the section 7 flow, both clients --------------------------------------


def test_the_typed_client_drives_a_session_end_to_end(world: World) -> None:
    fixture_id = _freeze(world)
    with serving(world) as url, SeahavenClient(base_url=url) as env:
        reset = env.reset(fixture=fixture_id, seed=7)
        assert reset.observation.result == {
            "fixture": fixture_id,
            "now": INSTANT_ISO,
            "tools": 6,
        }
        assert [tool["name"] for tool in env.list_tools()] == [
            "execute",
            "rows",
            "write_then_fail",
            "crash",
            "mint",
            "now",
        ]
        assert ids(env.call("rows", sql="SELECT id FROM notes")) == ["n0"]
        env.call("execute", sql=insert("n1"))
        assert ids(env.call("rows", sql="SELECT id FROM notes ORDER BY id")) == ["n0", "n1"]
        state = env.state()
        assert (state.world.name, state.world.version) == (world.name, world.version)
        assert state.fixture is not None and state.fixture.id == fixture_id
        assert (state.now, state.step_count, state.call_count) == (INSTANT_ISO, 4, 3)


def test_the_stock_client_drives_the_same_session(world: World) -> None:
    """No Seahaven on the client side at all: dictionaries in, dictionaries out."""
    fixture_id = _freeze(world)
    with serving(world) as url, GenericEnvClient(base_url=url) as env:
        reset = env.reset(fixture=fixture_id)
        assert reset.observation["result"]["fixture"] == fixture_id
        listed = env.step(ListToolsAction().model_dump()).observation
        assert [tool["name"] for tool in listed["tools"]] == [
            "execute",
            "rows",
            "write_then_fail",
            "crash",
            "mint",
            "now",
        ]
        observation = env.step(
            CallToolAction(tool_name="rows", arguments={"sql": "SELECT id FROM notes"}).model_dump()
        ).observation
        assert observation == {
            "tool_name": "rows",
            "result": [{"id": "n0"}],
            "error": None,
            "metadata": {},
        }
        state = env.state()
        assert state["world"] == {"name": world.name, "version": world.version}
        assert state["fixture"]["id"] == fixture_id


def test_a_tool_error_reaches_the_stock_client_as_data(world: World) -> None:
    with serving(world) as url, GenericEnvClient(base_url=url) as env:
        env.reset()
        result = env.step(CallToolAction(tool_name="no_such_tool").model_dump())
        assert result.observation["error"]["code"] == "unknown_tool"
        assert result.done is False
        assert result.reward is None


def test_a_world_bug_reaches_the_client_as_an_error_frame(tmp_path: Path) -> None:
    """The author's bug is loud on the wire, and the session survives it."""
    world = build_world(tmp_path)

    @world.tool
    def misuse(ctx: Ctx) -> None:
        """Fail the way a broken world fails."""
        raise WorldBug("the world is wrong")

    with serving(world) as url, SeahavenClient(base_url=url) as env:
        env.reset()
        with pytest.raises(RuntimeError, match="the world is wrong"):
            env.call("misuse")
        assert env.call("rows", sql="SELECT 1 AS n").result == [{"n": 1}]


# --- the state document, over the wire -------------------------------------

# `functional_spec.md` §9's confirmation step, and the gate this project's
# implementation plan put at this phase: an earlier session established that a
# subclass's fields travel over the websocket state message while `GET /state`
# strips them (`BACKLOG.md` B13), and nothing above `SeahavenState` works unless
# that holds for a document with nested models and an arbitrarily deep `state`
# in it. So it is driven over a real socket rather than assumed.


@pytest.fixture
def document_world(tmp_path: Path) -> World:
    """The notes world with a startup keyword, so no envelope field is empty."""
    world = build_world(tmp_path)

    @world.instance_startup
    def tenant(ctx: Ctx, *, tenant: str = "acme") -> None:
        """Accept the keyword; the document reports it whatever the hook does."""

    return world


# The one episode both clients drive: a fixture, a seed, a named episode, a
# startup keyword and one write, so every envelope field below has a value that
# is not the default.
EPISODE: dict[str, Any] = {"seed": 7, "episode_id": "ep-1", "tenant": "globex"}
WRITE = CallToolAction(tool_name="execute", arguments={"sql": insert("n1")})


def expected_document(world: World, fixture_id: str) -> dict[str, Any]:
    """What `EPISODE` plus `WRITE` leaves behind, field by field, as §3.1 defines it."""
    sha256 = {fixture.id: fixture.meta.file_sha256 for fixture in world.fixtures()}[fixture_id]
    return {
        "format": "seahaven.state/1",
        "seahaven_version": seahaven.__version__,
        "world": {"name": world.name, "version": world.version},
        "fixture": {"id": fixture_id, "file_sha256": sha256},
        "episode_id": "ep-1",
        "seed": 7,
        "now": INSTANT_ISO,
        "startup": {"tenant": "globex"},
        "call_count": 1,
        "state": {
            "db": {
                "log": [
                    {
                        "i": 0,
                        "subworld": None,
                        "table": "notes",
                        "op": "insert",
                        "key": {"id": "n1"},
                        "before": None,
                        "after": {"id": "n1", "body": "a body", "n": 0},
                    }
                ]
            }
        },
    }


def test_the_whole_document_arrives_over_the_websocket(document_world: World) -> None:
    """Every field of `functional_spec.md` §3.1, with the value it has in process."""
    fixture_id = _freeze(document_world)
    with serving(document_world) as url, SeahavenClient(base_url=url) as env:
        env.reset(fixture=fixture_id, **EPISODE)
        env.call("execute", sql=insert("n1"))
        state = env.state()
    assert state.model_dump(exclude={"step_count"}) == expected_document(document_world, fixture_id)
    assert state.step_count == 1


def test_the_stock_client_sees_the_same_document(document_world: World) -> None:
    """No Seahaven on the client side at all: the document is plain JSON.

    The same episode driven twice against the same server answers the same
    document down to the last field, which is `functional_spec.md` §15's
    determinism read over the wire: the episode id is the one both were given,
    and everything else is the world's or the episode's.
    """
    fixture_id = _freeze(document_world)
    with serving(document_world) as url:
        with GenericEnvClient(base_url=url) as generic:
            generic.reset(fixture=fixture_id, **EPISODE)
            generic.step(WRITE.model_dump())
            stock = generic.state()
        with SeahavenClient(base_url=url) as typed:
            typed.reset(fixture=fixture_id, **EPISODE)
            typed.call("execute", sql=insert("n1"))
            document = typed.state().model_dump()
    assert stock == expected_document(document_world, fixture_id) | {"step_count": 1}
    assert stock == document


def test_reset_selects_a_state_format_over_the_wire(world: World) -> None:
    """The state message carries no arguments, so the episode's format is the only one."""
    with serving(world) as url, SeahavenClient(base_url=url) as env:
        env.reset(state_format="seahaven.state+last_step/1")
        env.call("execute", sql=insert("n1"))
        env.call("execute", sql=insert("n2"))
        state = env.state()
    assert state.format == "seahaven.state+last_step/1"
    assert [record["key"]["id"] for record in state.state["db"]["log"]] == ["n2"]


def test_an_unknown_state_format_is_an_error_frame_and_the_session_survives(world: World) -> None:
    with serving(world) as url, SeahavenClient(base_url=url) as env:
        with pytest.raises(RuntimeError, match="state format"):
            env.reset(state_format="acme.state/1")
        env.reset()
        assert env.call("rows", sql="SELECT 1 AS n").result == [{"n": 1}]


# --- sessions --------------------------------------------------------------


def test_two_sessions_are_independent(world: World) -> None:
    with (
        serving(world) as url,
        SeahavenClient(base_url=url) as first,
        SeahavenClient(base_url=url) as second,
    ):
        first.reset()
        second.reset()
        first.call("execute", sql=insert("only-in-first"))
        assert ids(first.call("rows", sql="SELECT id FROM notes")) == ["only-in-first"]
        assert ids(second.call("rows", sql="SELECT id FROM notes")) == []
        assert first.state().episode_id != second.state().episode_id


def test_an_unknown_reset_kwarg_is_an_error_frame_and_the_session_survives(world: World) -> None:
    with serving(world) as url, SeahavenClient(base_url=url) as env:
        with pytest.raises(RuntimeError, match=r"unknown reset argument\(s\)"):
            env.reset(nonsense=1)
        reset = env.reset(now=INSTANT_ISO)
        assert reset.observation.result["now"] == INSTANT_ISO
        assert env.call("rows", sql="SELECT 1 AS n").result == [{"n": 1}]


def test_a_second_reset_over_the_wire_starts_from_the_fixture_again(world: World) -> None:
    fixture_id = _freeze(world)
    with serving(world) as url, SeahavenClient(base_url=url) as env:
        env.reset(fixture=fixture_id)
        env.call("execute", sql=insert("n1"))
        assert len(ids(env.call("rows", sql="SELECT id FROM notes"))) == 2
        env.reset(fixture=fixture_id)
        assert ids(env.call("rows", sql="SELECT id FROM notes")) == ["n0"]


# --- the control tool ------------------------------------------------------


# `controller_run_sql` is deprecated and warns on every call. These tests are about the
# tool, not the warning; `tests/test_control.py` is where the warning itself is pinned.
@pytest.mark.filterwarnings("ignore:controller_run_sql is deprecated")
def test_the_control_tool_is_callable_with_the_flag_and_never_listed(world: World) -> None:
    with (
        serving(world, include_control_tools=True) as url,
        SeahavenClient(base_url=url) as env,
    ):
        env.reset()
        assert "controller_run_sql" not in [tool["name"] for tool in env.list_tools()]
        env.call("execute", sql=insert("n1"))
        assert env.call("controller_run_sql", sql="SELECT id FROM notes").result == {
            "columns": ["id"],
            "rows": [["n1"]],
            "row_count": 1,
            "truncated": False,
        }


def test_the_control_tool_is_unknown_without_the_flag(world: World) -> None:
    with serving(world) as url, SeahavenClient(base_url=url) as env:
        env.reset()
        assert "controller_run_sql" not in [tool["name"] for tool in env.list_tools()]
        observation = env.call("controller_run_sql", sql="SELECT 1")
        assert observation.error == {
            "code": "unknown_tool",
            "message": "unknown tool: controller_run_sql",
            "details": {"name": "controller_run_sql"},
        }


# --- what a world is allowed to call its arguments -------------------------


def test_a_tool_argument_called_tool_is_callable_through_the_client(tmp_path: Path) -> None:
    """`Instance.call` made the tool name positional-only; review round 1 found
    the client had not.

    A world may declare a tool argument called `tool`, or `self`, or anything
    else -- the name is the world author's to choose and the framework validates
    it against their signature. With the name a keyword parameter of
    `SeahavenClient.call`, such a tool listed, worked through
    `step(CallToolAction(...))` and raised `TypeError: got multiple values for
    argument 'tool'` through the documented convenience: a tool nobody could
    call from a harness.
    """
    world = build_world(tmp_path)

    @world.tool
    def describe(ctx: Ctx, tool: str, self: str = "unset") -> dict[str, str]:
        """Two argument names that collide with a method's own parameters."""
        return {"tool": tool, "self": self}

    with serving(world) as url, SeahavenClient(base_url=url) as env:
        env.reset()
        assert env.call("describe", tool="rows").result == {"tool": "rows", "self": "unset"}
        assert env.call("describe", tool="rows", self="mine").result == {
            "tool": "rows",
            "self": "mine",
        }


# --- the published schema --------------------------------------------------


def test_the_served_schema_publishes_the_observation_with_its_descriptions(
    world: World,
) -> None:
    """`GET /schema` is part of the app's surface, and nothing was reading it.

    Review round 1 found `SeahavenObservation.result`'s redeclaration dropping
    `CallToolObservation`'s inherited field description from this endpoint,
    which the phase plan had recorded as changing nothing; round 2 found the fix
    closing exactly that field and leaving `tool_name` and `error` published
    with no description at all. This is the half of the rule that needs a
    server: the text really does reach a client. The other half -- that *every*
    field either model declares has a description in the first place -- is
    `test_every_declared_field_publishes_a_description`, in process, because
    `/schema` answers `State.model_json_schema()` and never sees
    `SeahavenState` (`BACKLOG.md` B13).
    """
    with serving(world) as url, urllib.request.urlopen(url + "/schema") as response:
        schema = json.loads(response.read())
    assert schema["action"]["title"] == "CallToolAction"
    observation = schema["observation"]
    assert observation["title"] == "SeahavenObservation"
    properties = observation["properties"]
    assert sorted(properties) == ["done", "error", "metadata", "result", "reward", "tool_name"]
    # What the model says is what the wire publishes, for every field the class
    # declares itself. The texts themselves are pinned in `test_env.py`, which
    # is also where the rule that each declared field *has* one lives; asserting
    # them again here would duplicate a literal rather than test a second thing.
    declared = set(SeahavenObservation.__annotations__)
    assert declared == {"tool_name", "result", "error"}
    assert {name: properties[name]["description"] for name in declared} == {
        name: SeahavenObservation.model_fields[name].description for name in declared
    }


# --- the idle reaper -------------------------------------------------------


def test_an_idle_session_is_reaped_and_its_instance_destroyed(world: World, tmp_path: Path) -> None:
    """`session_timeout` was pinned as a number passed through, never as behaviour.

    It is the only thing standing between a long-lived server and a disk full of
    abandoned instances, and OpenEnv's cleanup swallows every exception
    `env.close()` raises -- so an `Instance.destroy()` that started failing would
    leak a fixture copy and an APSW connection per session with nothing red
    anywhere. This test asks the filesystem instead.

    The reaper wakes every `max(timeout / 4, 5.0)` seconds, so the deadline is
    generous and the assertion is only that the directory goes.
    """
    work = tmp_path / "work"
    with serving(world, session_timeout=REAPED_AFTER) as url, SeahavenClient(base_url=url) as env:
        env.reset()
        instances_on_disk = list(work.iterdir())
        assert len(instances_on_disk) == 1, instances_on_disk
        directory = instances_on_disk[0]
        deadline = time.monotonic() + REAP_DEADLINE
        while directory.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not directory.exists(), "the idle session's instance was not destroyed"
        assert list(work.iterdir()) == []


# --- the gate --------------------------------------------------------------


def test_the_gate_serialises_two_sessions(slow_world: World) -> None:
    """With one slot, two calls on two sessions cannot be inside SQLite together."""
    instances.set_concurrency(1)
    first, second = _two_slow_calls(slow_world)
    assert not _overlap(first, second), f"the calls overlapped: {first} and {second}"


def test_without_the_gate_two_sessions_run_together(slow_world: World) -> None:
    """The permissive half of the same rule: two slots, and the calls do overlap.

    Without this, a gate that serialised everything -- or a server that ran every
    session on one thread -- would pass the test above and nothing would notice.
    """
    instances.set_concurrency(2)
    first, second = _two_slow_calls(slow_world)
    assert _overlap(first, second), f"the calls did not overlap: {first} and {second}"


def _two_slow_calls(world: World) -> tuple[dict[str, float], dict[str, float]]:
    """One slow call on each of two sessions, started as close together as possible."""

    async def drive(url: str) -> tuple[dict[str, float], dict[str, float]]:
        async with (
            SeahavenClient(base_url=url) as first,
            SeahavenClient(base_url=url) as second,
        ):
            await first.reset()
            await second.reset()
            both = await asyncio.gather(first.call("slow"), second.call("slow"))
            return both[0].result, both[1].result

    with serving(world) as url:
        return asyncio.run(drive(url))


def _overlap(first: dict[str, float], second: dict[str, float]) -> bool:
    return first["started"] < second["ended"] and second["started"] < first["ended"]


# --- capacity --------------------------------------------------------------


def test_over_capacity_the_server_refuses_rather_than_queueing(world: World) -> None:
    """The budget is a refusal, not a queue: the connection over it is told so.

    Read straight off the socket rather than through a client. The server sends
    the refusal and then closes, so whether a client sees the frame or the close
    first is a race, and a test that asserted on the client's exception would be
    asserting on who won it.
    """
    with serving(world, max_concurrent_envs=1) as url, SeahavenClient(base_url=url) as env:
        env.reset()
        refusal = asyncio.run(_first_frame(url))
        assert refusal is not None, "the connection over the budget was served"
        assert refusal["type"] == "error"
        assert refusal["data"]["code"] == "CAPACITY_REACHED"
        assert (refusal["data"]["active_sessions"], refusal["data"]["max_sessions"]) == (1, 1)
        # The session that has capacity is untouched by the one that did not.
        assert env.call("rows", sql="SELECT 1 AS n").result == [{"n": 1}]


def test_under_capacity_a_second_connection_is_served(world: World) -> None:
    """The permissive half: the same second connection, with a slot for it."""
    with serving(world, max_concurrent_envs=2) as url, SeahavenClient(base_url=url) as env:
        env.reset()
        assert asyncio.run(_first_frame(url)) is None


async def _first_frame(url: str) -> dict[str, Any] | None:
    """Connect and answer the frame the server volunteers, or `None` if it waits."""
    async with ws_connect(convert_to_ws_url(url) + "/ws", proxy=None) as websocket:
        try:
            return dict(json.loads(await asyncio.wait_for(websocket.recv(), UNSOLICITED_TIMEOUT)))
        except TimeoutError:
            return None


@pytest.mark.slow
def test_five_hundred_sessions_reset_and_call_with_no_errors(trivial_world: World) -> None:
    """The smoke test: as many sessions as the default budget, all at once."""

    async def drive(url: str) -> list[dict[str, str]]:
        clients = [SeahavenClient(base_url=url) for _ in range(SESSIONS)]
        try:
            await asyncio.gather(*(client.connect() for client in clients))
            await asyncio.gather(*(client.reset() for client in clients))
            results = await asyncio.gather(
                *(client.call("echo", message=str(number)) for number, client in enumerate(clients))
            )
            return [observation.result for observation in results]
        finally:
            await asyncio.gather(*(client.close() for client in clients))

    with serving(trivial_world) as url:
        answers = asyncio.run(drive(url))
    assert answers == [{"message": str(number)} for number in range(SESSIONS)]


def _freeze(world: World, fixture_id: str = "start") -> str:
    with world.instance(None, now=INSTANT_ISO) as instance:
        instance.call("execute", sql=insert("n0"))
        instance.freeze(fixture_id, "One note.")
    return fixture_id
