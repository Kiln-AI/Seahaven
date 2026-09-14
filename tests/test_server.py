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
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Iterator, MutableMapping
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any

import pytest

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

from fastapi import WebSocketDisconnect
from openenv import GenericEnvClient
from openenv.core.env_server.mcp_types import CallToolAction, ListToolsAction
from openenv.core.utils import convert_to_ws_url
from starlette.types import Receive, Scope, Send
from websockets.asyncio.client import connect as ws_connect

from seahaven.openenv import SeahavenClient, _SwallowWebSocketDisconnect
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

# OpenEnv's episode-control routes over plain HTTP, and the verb each answers.
HTTP_EPISODE_CONTROL = (("POST", "/reset"), ("POST", "/step"), ("GET", "/state"))


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
        assert (state.world, state.fixture, state.now) == (world.name, fixture_id, INSTANT_ISO)
        assert state.step_count == 4


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
        assert state["world"] == world.name
        assert state["fixture"] == fixture_id


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


# --- control tools ---------------------------------------------------------


def test_control_tools_are_callable_with_the_flag_and_never_listed(world: World) -> None:
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
        changes = env.call("controller_changes").result
        assert [(change["table"], change["op"]) for change in changes] == [("notes", "insert")]


def test_control_tools_are_unknown_without_the_flag(world: World) -> None:
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


# --- the refused HTTP episode-control routes -------------------------------


def test_the_http_episode_routes_refuse_rather_than_answer_a_throwaway_environment(
    world: World,
) -> None:
    """The three routes OpenEnv cannot serve, refused here in words.

    Upstream builds a fresh environment inside each of the `/reset`, `/step` and
    `/state` handlers and closes it before replying, so the three never observe
    one another and every answer is a plausible 200 about nothing. Seahaven
    replaces the handlers. What is asserted is what a reader at 2am needs: the
    status, the machine-readable code, which route they hit, where to go
    instead, and enough of the upstream citation to check the claim.
    """
    with serving(world) as url:
        for verb, path in HTTP_EPISODE_CONTROL:
            status, body = _request(url + path, method=verb)
            assert status == 501, (verb, path, body)
            refusal = body["detail"]
            assert refusal["code"] == "http_episode_control_unsupported"
            assert refusal["details"]["route"] == f"{verb} {path}"
            assert refusal["details"]["use_instead"] == "/ws"
            assert refusal["details"]["upstream"]["regression"] == "86a222d"
            assert "/ws" in refusal["message"]
            assert "SeahavenClient" in refusal["message"]


def test_a_well_formed_step_is_refused_in_the_same_words_as_a_malformed_one(world: World) -> None:
    """The refusal answers a good request, not only a bad one.

    A replacement handler that still declared OpenEnv's `StepRequest` would
    answer 422 to a body that does not parse, and the caller would go off fixing
    a payload that can never work. Both bodies have to reach the same 501.
    """
    step = json.dumps(
        {"action": CallToolAction(tool_name="rows", arguments={"sql": "SELECT 1"}).model_dump()}
    ).encode()
    with serving(world) as url:
        well_formed = _request(url + "/step", method="POST", data=step)
        nonsense = _request(url + "/step", method="POST", data=b"not json at all")
    assert well_formed[0] == 501
    assert nonsense == well_formed


def test_the_refused_routes_are_still_published_as_openapi_paths(world: World) -> None:
    """Refused and not removed, because `openenv push` reads the paths.

    `mode_endpoint_consistency` in `openenv/cli/_validation.py` calls an app that
    publishes `/reset` a simulation environment and then requires `/step` and
    `/state` beside it. It never calls the three, only names them, so deleting
    them would not fail that criterion -- it would quietly reclassify a Seahaven
    world as a *production* environment, which is a wrong declaration about what
    the world is. The paths stay, and what the schema now promises at each of
    them is the refusal and nothing else.
    """
    with serving(world) as url:
        status, document = _request(url + "/openapi.json")
    assert status == 200
    paths = document["paths"]
    for verb, path in HTTP_EPISODE_CONTROL:
        assert path in paths, sorted(paths)
        assert sorted(paths[path][verb.lower()]["responses"]) == ["501"]


def test_refusing_the_episode_routes_leaves_the_rest_of_the_http_surface_alone(
    world: World,
) -> None:
    """Three routes, and the neighbours they sit between are untouched.

    `/metadata` builds a throwaway environment exactly as the refused three do,
    and is deliberately still served: metadata is the world's and not an
    episode's, so a fresh environment answers it correctly. This is the test
    that fails if the refusal is ever widened to a path that did not need it.
    """
    with serving(world) as url:
        assert _request(url + "/health") == (200, {"status": "healthy"})
        metadata = _request(url + "/metadata")
        assert metadata[0] == 200
        assert metadata[1]["name"] == world.name
        schema = _request(url + "/schema")
        assert schema[0] == 200
        assert sorted(schema[1]) == ["action", "observation", "state"]


def test_a_websocket_session_is_untouched_by_the_refusal(world: World) -> None:
    """The transport that is the product, on the same server at the same moment.

    The `state` frame in particular: its HTTP namesake now answers 501, and the
    session's own state has to keep arriving over the wire, whole and real.
    """
    with serving(world) as url, SeahavenClient(base_url=url) as env:
        env.reset()
        assert _request(url + "/state")[0] == 501
        env.call("execute", sql=insert("n1"))
        state = env.state()
        assert (state.world, state.step_count) == (world.name, 1)
        assert ids(env.call("rows", sql="SELECT id FROM notes")) == ["n1"]


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


# --- disconnects -----------------------------------------------------------


class _Kept(logging.Handler):
    """A handler that keeps every record it is handed."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@contextmanager
def _watched(world: World, **options: Any) -> Iterator[tuple[str, list[logging.LogRecord]]]:
    """Serve a world, and collect what uvicorn logs on its own error channel.

    `caplog` cannot see this: uvicorn's loggers set `propagate=False`, so nothing
    uvicorn logs ever reaches the handler pytest puts on the root logger. The
    handler has to go on *after* the server starts, because uvicorn configures
    logging with `dictConfig` as it boots and that drops every handler already on
    `uvicorn.error`, and it has to come off *after* the server stops, or a
    traceback logged while the last connection is torn down would be missed.
    `ExitStack` unwinds in reverse, so registering the removal first buys that
    order.
    """
    logger = logging.getLogger("uvicorn.error")
    kept = _Kept()
    with ExitStack() as stack:
        stack.callback(logger.removeHandler, kept)
        url = stack.enter_context(serving(world, **options))
        logger.addHandler(kept)
        yield url, kept.records


def _errors(records: list[logging.LogRecord]) -> list[str]:
    return [record.getMessage() for record in records if record.levelno >= logging.ERROR]


def test_a_session_that_ends_normally_leaves_nothing_in_the_error_log(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    """A clean disconnect is silent, and both halves of that are asserted.

    Nothing on `uvicorn.error` is the operator's half: a session that ended
    normally must not look like a failure in the log they grep when something
    real goes wrong. Nothing from `seahaven.openenv` is the client's half: the
    middleware absorbs a disconnect that escapes OpenEnv's handler and says so at
    debug level, so silence there means the client waited for the server rather
    than that something quietly cleaned up after it.
    """
    caplog.set_level(logging.DEBUG, logger="seahaven.openenv")
    with _watched(world) as (url, records), SeahavenClient(base_url=url) as env:
        env.reset()
        assert env.call("rows", sql="SELECT 1 AS n").result == [{"n": 1}]
    assert _errors(records) == []
    absorbed = [record for record in caplog.records if record.name == "seahaven.openenv"]
    assert [record.getMessage() for record in absorbed] == []


def test_a_peer_that_vanishes_is_not_logged_as_a_server_error(world: World) -> None:
    """The half no client-side fix can reach: a socket that simply stops.

    A harness that dies mid-session, a stock client, anything that hangs up
    without waiting -- OpenEnv is then closing a connection that is already gone.
    That is the peer's business and never the server's, and it must not reach the
    error log either.
    """

    async def connect_and_vanish(url: str) -> None:
        async with ws_connect(convert_to_ws_url(url) + "/ws", proxy=None):
            pass

    with _watched(world) as (url, records):
        asyncio.run(connect_and_vanish(url))
    assert _errors(records) == []


def test_the_middleware_absorbs_only_a_disconnected_websocket() -> None:
    """Narrow on purpose: this scope, this exception, nothing else.

    A `WebSocketDisconnect` reaching the top of a websocket connection means the
    peer went away, which no server can act on. Anything else out of a handler is
    a real failure and has to stay loud, and an HTTP request is not this
    middleware's business at all.
    """

    async def raise_through(error: Exception, scope_type: str) -> None:
        async def failing(scope: Scope, receive: Receive, send: Send) -> None:
            raise error

        await _SwallowWebSocketDisconnect(failing)(
            {"type": scope_type, "path": "/ws"}, _unused_receive, _unused_send
        )

    asyncio.run(raise_through(WebSocketDisconnect(code=1006), "websocket"))
    with pytest.raises(WebSocketDisconnect):
        asyncio.run(raise_through(WebSocketDisconnect(code=1006), "http"))
    with pytest.raises(RuntimeError, match="the world is on fire"):
        asyncio.run(raise_through(RuntimeError("the world is on fire"), "websocket"))


async def _unused_receive() -> MutableMapping[str, Any]:
    raise AssertionError("the stub app never reads the connection")


async def _unused_send(message: MutableMapping[str, Any]) -> None:
    raise AssertionError("the stub app never writes to the connection")


def _request(url: str, *, method: str = "GET", data: bytes | None = None) -> tuple[int, Any]:
    """Answer the status and decoded body, for the statuses urllib calls errors.

    `urlopen` raises on anything from 400 up, and the body of a refusal is the
    whole point of these tests, so both halves are read the same way.
    """
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as refused:
        return refused.code, json.loads(refused.read())


def _freeze(world: World, fixture_id: str = "start") -> str:
    with world.instance(None, now=INSTANT_ISO) as instance:
        instance.call("execute", sql=insert("n0"))
        instance.freeze(fixture_id, "One note.")
    return fixture_id
