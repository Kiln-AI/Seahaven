"""`seahaven.mcp`: the server, driven through the SDK's own client.

Two clients, because `mcp` 2.2.0 serves two protocol eras. A handshake client
opens with `initialize`, which is what every MCP client in use today sends; a
client on the 2026-07-28 protocol -- which is what the SDK's own client opens
with by default -- never sends one at all. Both are served one instance for the
whole connection, which is what `functional_spec.md` §6 asks for, and the tests
below say so on both paths.

The two subprocess tests at the end drive `serve()` over real pipes with raw
protocol frames, because the exit code and the process's stdout are the part of
this module no in-process test can see.
"""

import json
import logging
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from seahaven.cli import CliError
from seahaven.ctx import Ctx
from seahaven.errors import INTERNAL_ERROR_MESSAGE, WorldBug
from seahaven.instances import Instance
from seahaven.world import World
from tests.conftest import WAIT, WORLDS, Boom, build_world, mcp_sdk

mcp_sdk()

import anyio
from mcp import Client, types
from mcp.shared.exceptions import MCPError
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS

from seahaven.mcp.server import Sessions, State, build_server, instructions_for

# A bound on a hang rather than a measurement: a subprocess test waits this long
# for the whole exchange, including the world's startup.
PROCESS_WAIT = 60.0

# What a tool that must overlap another one pauses for. Long enough that two
# calls that do not serialise lose an update, short enough not to be felt.
OVERLAP = 0.05


@dataclass
class Served:
    """A server, and everything behind it a test has to look at."""

    sessions: Sessions
    state: State
    key: object
    server: Any

    def talk[T](self, work: Callable[[Client], Awaitable[T]], *, modern: bool = False) -> T:
        """Run `work` against a connected client, and answer what it answered.

        `modern` is the SDK client's own default: `server/discover`, no
        handshake, and a fresh session object on every request.
        """

        async def main() -> T:
            async with Client(self.server, mode="auto" if modern else "legacy") as client:
                return await work(client)

        return anyio.run(main)

    def instance(self) -> Instance:
        """The instance the connection is holding, for a test that compares against it."""
        return self.sessions.get(self.key)


def serve_world(world: World, **reset_options: Any) -> Served:
    """A server over `world`, with a connection key of its own."""
    sessions, state, key = Sessions(), State(), object()
    server = build_server(
        world, reset_options=reset_options, sessions=sessions, state=state, key=key
    )
    return Served(sessions=sessions, state=state, key=key, server=server)


@pytest.fixture
def served(tmp_path: Path) -> Iterator[Served]:
    """A server over the shared test world, on a blank instance."""
    ready = serve_world(build_world(tmp_path, description="notes and the rows in them"))
    try:
        yield ready
    finally:
        ready.sessions.close_all()


def leaf(error: BaseException) -> BaseException:
    """The one exception inside however many task groups the client wrapped it in."""
    while isinstance(error, BaseExceptionGroup):
        assert len(error.exceptions) == 1, error
        error = error.exceptions[0]
    return error


def text(result: types.CallToolResult) -> str:
    """The one text block a result carries."""
    [block] = result.content
    assert isinstance(block, types.TextContent)
    return block.text


def triple(result: types.CallToolResult) -> dict[str, Any]:
    """The `{code, message, details}` an `isError` result carries in its text block."""
    assert result.is_error, result
    return json.loads(text(result))


async def call(client: Client, name: str, **arguments: Any) -> types.CallToolResult:
    return await client.call_tool(name, arguments)


# --- the instructions the world writes --------------------------------------


def test_instructions_are_the_worlds_own_when_it_sets_them(tmp_path: Path) -> None:
    """Verbatim, with nothing of the framework's added to it."""
    world = build_world(
        tmp_path,
        description="a description nobody should read here",
        mcp_server_instructions="Use notes to record what happened.",
    )

    assert instructions_for(world) == "Use notes to record what happened."


def test_instructions_fall_back_to_the_name_and_the_description(tmp_path: Path) -> None:
    world = build_world(tmp_path, description="Notes, and the rows in them.")

    assert instructions_for(world) == "testworld\n\nNotes, and the rows in them."


def test_blank_instructions_fall_back_the_same_way(tmp_path: Path) -> None:
    """A blank string is not prose, so it means the default and not an empty one."""
    world = build_world(tmp_path, description="Notes.", mcp_server_instructions="   ")

    assert instructions_for(world) == "testworld\n\nNotes."


def test_instructions_fall_back_to_the_name_alone_without_a_description(tmp_path: Path) -> None:
    world = build_world(tmp_path)

    assert instructions_for(world) == "testworld"


def test_the_handshake_carries_the_worlds_identity_and_its_instructions(served: Served) -> None:
    async def work(client: Client) -> tuple[Any, str | None]:
        return client.server_info, client.instructions

    info, instructions = served.talk(work)

    assert info is not None
    assert (info.name, info.version) == ("testworld", "1.0.0")
    assert instructions == "testworld\n\nnotes and the rows in them"


# --- what is published ------------------------------------------------------


def test_the_tool_list_is_the_instances_with_mcps_spelling(served: Served) -> None:
    async def work(client: Client) -> tuple[list[types.Tool], list[dict[str, Any]]]:
        listed = (await client.list_tools()).tools
        return listed, served.instance().tools()

    listed, expected = served.talk(work)

    assert [tool.model_dump(by_alias=True, exclude_none=True, mode="json") for tool in listed] == [
        {
            "name": entry["name"],
            "description": entry["description"],
            "inputSchema": entry["input_schema"],
        }
        for entry in expected
    ]


def test_the_server_publishes_tools_and_nothing_else(served: Served) -> None:
    """No resources and no prompts: `functional_spec.md` §5.1 and §5.6."""

    async def work(client: Client) -> types.ServerCapabilities | None:
        return client.server_capabilities

    capabilities = served.talk(work)

    assert capabilities is not None
    assert capabilities.tools is not None
    assert (capabilities.resources, capabilities.prompts) == (None, None)


# --- calling a tool ---------------------------------------------------------


def test_an_object_result_is_json_text_and_structured_content(served: Served) -> None:
    async def work(client: Client) -> types.CallToolResult:
        return await call(client, "execute", sql="INSERT INTO notes (id, body) VALUES ('a', 'hi')")

    result = served.talk(work)

    assert result.is_error is False
    assert json.loads(text(result)) == {"rowcount": 1}
    assert result.structured_content == {"rowcount": 1}


def test_a_result_that_is_not_an_object_has_no_structured_content(served: Served) -> None:
    """`structuredContent` is defined as an object, so a list only gets the text block."""

    async def work(client: Client) -> types.CallToolResult:
        await call(client, "execute", sql="INSERT INTO notes (id, body) VALUES ('a', 'hi')")
        return await call(client, "rows", sql="SELECT body FROM notes")

    result = served.talk(work)

    assert json.loads(text(result)) == [{"body": "hi"}]
    assert result.structured_content is None


def test_a_write_is_seen_by_a_later_read_in_the_same_session(served: Served) -> None:
    """The point of a stateful server: the world moved, and stayed moved."""

    async def work(client: Client) -> types.CallToolResult:
        await call(client, "execute", sql="INSERT INTO notes (id, body) VALUES ('a', 'first')")
        await call(client, "execute", sql="UPDATE notes SET body = 'second' WHERE id = 'a'")
        return await call(client, "rows", sql="SELECT body FROM notes")

    assert json.loads(text(served.talk(work))) == [{"body": "second"}]


def test_a_modern_client_that_never_initializes_is_served_one_instance(served: Served) -> None:
    """The 2026-07-28 era: no handshake, a new session object per request, one world.

    `architecture.md` §5 made the instance in an `initialize` middleware alone,
    which serves this client nothing. It is made on the first request that needs
    one instead, and the connection keeps it.
    """

    async def work(client: Client) -> tuple[str, types.CallToolResult]:
        await call(client, "execute", sql="INSERT INTO notes (id, body) VALUES ('a', 'hi')")
        return client.protocol_version, await call(client, "rows", sql="SELECT body FROM notes")

    version, result = served.talk(work, modern=True)

    assert version == "2026-07-28"
    assert json.loads(text(result)) == [{"body": "hi"}]


def test_two_calls_in_flight_serialise(tmp_path: Path) -> None:
    """Calls into one instance queue on the instance's lock, so neither update is lost."""
    world = build_world(tmp_path)

    @world.tool
    def bump(ctx: Ctx) -> dict[str, int]:
        """Read, pause, then write: a lost update unless the two calls serialise."""
        row = ctx.db.one("SELECT n FROM notes WHERE id = 'a'")
        assert row is not None
        time.sleep(OVERLAP)
        ctx.db.execute("UPDATE notes SET n = ? WHERE id = 'a'", int(row["n"]) + 1)
        return {"n": int(row["n"]) + 1}

    ready = serve_world(world)

    async def work(client: Client) -> types.CallToolResult:
        await call(client, "execute", sql="INSERT INTO notes (id, body, n) VALUES ('a', 'x', 0)")
        async with anyio.create_task_group() as calls:
            calls.start_soon(call, client, "bump")
            calls.start_soon(call, client, "bump")
        return await call(client, "rows", sql="SELECT n FROM notes WHERE id = 'a'")

    try:
        assert json.loads(text(ready.talk(work))) == [{"n": 2}]
    finally:
        ready.sessions.close_all()


def test_a_call_does_not_block_the_event_loop(tmp_path: Path) -> None:
    """The call is dispatched to a worker thread, so the connection keeps answering.

    The lower-level `Server` takes async handlers only, so moving a blocking
    call off the loop is this project's own work (`architecture.md` §6), and
    `functional_spec.md` §7's several calls in flight rest on it. The test above
    proves nothing about it: the instance's own lock serialises the two calls
    whether or not the loop is free.
    """
    world = build_world(tmp_path)
    running, release = threading.Event(), threading.Event()

    @world.tool
    def slow(ctx: Ctx) -> dict[str, bool]:
        """Hold the call open until the test lets go."""
        running.set()
        release.wait(WAIT)
        return {"done": True}

    ready = serve_world(world)

    async def work(client: Client) -> list[str]:
        async with anyio.create_task_group() as calls:
            calls.start_soon(call, client, "slow")
            with anyio.fail_after(WAIT):
                while not running.is_set():
                    await anyio.sleep(0.01)
                # Answered while the tool is still running, which a call made on
                # the loop would make impossible: `tools/list` takes no lock of
                # the instance's, so the loop is the only thing in its way.
                listed = await client.list_tools()
            release.set()
        return [tool.name for tool in listed.tools]

    try:
        assert "slow" in ready.talk(work)
    finally:
        release.set()
        ready.sessions.close_all()


# --- the error taxonomy -----------------------------------------------------


def test_a_tool_error_is_an_is_error_result_carrying_the_triple(served: Served) -> None:
    """Data the agent reads and recovers from, never a JSON-RPC error."""

    async def work(client: Client) -> types.CallToolResult:
        return await call(
            client, "write_then_fail", sql="INSERT INTO notes (id, body) VALUES ('a', 'hi')"
        )

    result = served.talk(work)

    assert triple(result) == {"code": "boom", "message": "it did not work out", "details": None}


def test_a_tool_error_rolls_the_call_back(served: Served) -> None:
    """The failed call's write is not there afterwards, and the session goes on."""

    async def work(client: Client) -> types.CallToolResult:
        await call(client, "write_then_fail", sql="INSERT INTO notes (id, body) VALUES ('a', 'x')")
        return await call(client, "rows", sql="SELECT body FROM notes")

    assert json.loads(text(served.talk(work))) == []


def test_a_name_the_world_does_not_have_is_an_is_error_result(served: Served) -> None:
    async def work(client: Client) -> types.CallToolResult:
        return await call(client, "no_such_tool")

    assert triple(served.talk(work)) == {
        "code": "unknown_tool",
        "message": "unknown tool: no_such_tool",
        "details": {"name": "no_such_tool"},
    }


def test_a_control_tool_is_answered_as_an_unknown_tool(served: Served) -> None:
    """Written against the behaviour, so `functional_spec.md` §15 changes nothing here."""

    async def work(client: Client) -> types.CallToolResult:
        return await call(client, "controller_run_sql", sql="SELECT 1")

    assert triple(served.talk(work)) == {
        "code": "unknown_tool",
        "message": "unknown tool: controller_run_sql",
        "details": {"name": "controller_run_sql"},
    }


def test_arguments_the_schema_refuses_are_an_is_error_result(served: Served) -> None:
    async def work(client: Client) -> types.CallToolResult:
        return await call(client, "execute", nonsense=1)

    answered = triple(served.talk(work))

    assert answered["code"] == "invalid_arguments"
    assert answered["details"]["tool"] == "execute"


def test_a_framework_error_is_a_json_rpc_error_that_says_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A `WorldBug` fails the frame, and the author's prose goes to the log instead."""
    world = build_world(tmp_path)

    @world.tool
    def author_bug(ctx: Ctx) -> None:
        """Fail the way a broken world fails."""
        raise WorldBug("the author's own prose about their own mistake")

    ready = serve_world(world)

    async def work(client: Client) -> BaseException:
        with pytest.raises(BaseException) as raised:
            await call(client, "author_bug")
        return raised.value

    with caplog.at_level(logging.ERROR):
        error = leaf(ready.talk(work))
    ready.sessions.close_all()

    assert isinstance(error, MCPError)
    assert "the author's own prose" not in error.error.message
    said = re.fullmatch(rf"{INTERNAL_ERROR_MESSAGE} \(([0-9a-f]+)\)", error.error.message)
    assert said is not None, error.error.message
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert said[1] in logged
    assert "author_bug" in logged


def test_an_unhandled_exception_is_an_is_error_result_with_the_generic_error(
    served: Served, caplog: pytest.LogCaptureFixture
) -> None:
    """An accident is data the agent reads, scrubbed, with its traceback on stderr."""

    async def work(client: Client) -> types.CallToolResult:
        return await call(client, "crash")

    with caplog.at_level(logging.ERROR):
        result = served.talk(work)

    answered = triple(result)
    assert answered["code"] == "internal"
    assert answered["message"] == INTERNAL_ERROR_MESSAGE
    assert answered["details"]["id"] in "\n".join(r.getMessage() for r in caplog.records)
    assert "a bug in world code" not in json.dumps(answered)


# --- a world that never starts ----------------------------------------------


def test_a_bad_fixture_fails_the_handshake_and_names_the_fixture(tmp_path: Path) -> None:
    """Not scrubbed: the reader is the person who wrote the client configuration."""
    ready = serve_world(build_world(tmp_path), fixture="small_startupp")

    async def work(client: Client) -> None:
        raise AssertionError("the handshake should not have succeeded")

    with pytest.raises(BaseException) as raised:
        ready.talk(work)

    error = leaf(raised.value)
    assert isinstance(error, MCPError)
    assert "small_startupp" in error.error.message
    assert isinstance(ready.state.fatal, WorldBug)


def test_a_startup_hook_that_raises_carries_its_triple_to_the_client(tmp_path: Path) -> None:
    """A `ToolError` out of a startup hook is `{code, message, details}` in the error's data."""
    world = build_world(tmp_path)

    @world.instance_startup
    def refuse(ctx: Ctx) -> None:
        raise Boom("this world will not start today")

    ready = serve_world(world)

    async def work(client: Client) -> None:
        raise AssertionError("the handshake should not have succeeded")

    with pytest.raises(BaseException) as raised:
        ready.talk(work)

    error = leaf(raised.value)
    assert isinstance(error, MCPError)
    assert error.error.message == "this world will not start today"
    assert error.error.data == {
        "code": "boom",
        "message": "this world will not start today",
        "details": None,
    }


def test_a_failed_start_is_answered_again_and_never_retried(tmp_path: Path) -> None:
    """One process, one answer: the world is not made a second time."""
    world = build_world(tmp_path)
    attempts: list[dict[str, Any]] = []
    make = world.instance

    def counted(*args: Any, **kwargs: Any) -> Any:
        attempts.append(kwargs)
        return make(*args, **kwargs)

    world.instance = counted  # ty: ignore[invalid-assignment]
    ready = serve_world(world, fixture="not_a_fixture")

    async def work(client: Client) -> None:
        raise AssertionError("the handshake should not have succeeded")

    for _ in range(2):
        with pytest.raises(BaseException) as raised:
            ready.talk(work)
        assert isinstance(leaf(raised.value), MCPError)

    assert len(attempts) == 1


def test_a_world_that_cannot_be_resolved_is_answered_rather_than_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed import reaches the client, because a dead process is a broken pipe."""

    def discover() -> World:
        raise CliError("no module named 'nope'; or pass --world module:attr")

    sessions, state, key = Sessions(), State(), object()
    with caplog.at_level(logging.ERROR):
        server = build_server(discover, reset_options={}, sessions=sessions, state=state, key=key)

        async def main() -> None:
            async with Client(server, mode="legacy"):
                raise AssertionError("the handshake should not have succeeded")

        with pytest.raises(BaseException) as raised:
            anyio.run(main)

    error = leaf(raised.value)
    assert isinstance(error, MCPError)
    assert "no module named 'nope'" in error.error.message
    assert isinstance(state.fatal, CliError)


def test_a_modern_client_is_refused_before_it_learns_anything_about_the_server() -> None:
    """A process that can never serve answers no handshake, in either era.

    The modern era's handshake is `server/discover`, which needs no instance, so
    a server that refused only the three methods that do would answer it -- with
    a name and a version of its own, which `functional_spec.md` §1 forbids and
    §5.2 owes the client an error instead.
    """

    def discover() -> World:
        raise CliError("no module named 'nope'; or pass --world module:attr")

    sessions, state, key = Sessions(), State(), object()
    server = build_server(discover, reset_options={}, sessions=sessions, state=state, key=key)
    seen: list[Any] = []

    async def main() -> None:
        async with Client(server) as client:
            seen.append(client.server_info)

    with pytest.raises(BaseException) as raised:
        anyio.run(main)

    error = leaf(raised.value)
    assert isinstance(error, MCPError)
    assert "no module named 'nope'" in error.error.message
    assert seen == [], "the client completed a handshake against a world that never resolved"


# --- the instance map -------------------------------------------------------


def test_the_session_map_holds_one_instance_per_key(tmp_path: Path) -> None:
    """Two connections, two instances: the stdio case is the degenerate one."""
    world = build_world(tmp_path)
    sessions = Sessions()
    first, second = object(), object()

    with world.instance(None) as one, world.instance(None) as two:
        sessions.open(first, one)
        sessions.open(second, two)

        assert sessions.get(first) is one
        assert sessions.get(second) is two

        sessions.close(first)

        assert not sessions.has(first)
        assert one.closed
        assert sessions.get(second) is two

        sessions.close_all()

        assert not sessions.has(second)
        assert two.closed


def test_a_connection_never_gets_a_second_instance(tmp_path: Path) -> None:
    """One episode per session: there is no reset, and a client restarts instead."""
    world = build_world(tmp_path)
    sessions = Sessions()
    key = object()

    with world.instance(None) as one, world.instance(None) as two:
        sessions.open(key, one)

        with pytest.raises(WorldBug, match="already has an instance"):
            sessions.open(key, two)

        sessions.close_all()


def test_a_key_with_no_instance_is_a_world_bug(tmp_path: Path) -> None:
    """Nothing a client can provoke: a handler runs only after the middleware."""
    with pytest.raises(WorldBug, match="no instance"):
        Sessions().get(object())


# --- the real entry point ---------------------------------------------------


@dataclass
class Finished:
    """What a served process left behind: its exit code, its frames and its stderr."""

    code: int
    frames: list[dict[str, Any]]
    errors: str

    def by_id(self) -> dict[Any, dict[str, Any]]:
        return {frame["id"]: frame for frame in self.frames}


SERVE_SCRIPT = """
import copy
import sys
from pathlib import Path

sys.path.insert(0, {source!r})
import tidy
from seahaven.mcp import serve

# A copy, because a world is imported once and moving `work_dir` on the object
# every holder shares would move it for all of them.
world = copy.copy(tidy.world)
world.work_dir = Path({work_dir!r})


def discover():
    # What importing a world's package can do, at the moment `serve` does it.
    print({noise!r})
    return world


raise SystemExit(
    serve(
        discover if {noise!r} else world,
        reset_options={reset_options!r},
        fatal_grace={fatal_grace!r},
    )
)
"""


def spawn_server_process(
    reset_options: dict[str, Any],
    work_dir: Path,
    *,
    fatal_grace: float = 5.0,
    noise: str = "",
) -> subprocess.Popen[str]:
    """`serve()` as a real process, on real pipes, with its instances under `work_dir`.

    `noise` is what the world says on stdout while it is being resolved, which
    is a world's package printing as it is imported.
    """
    script = SERVE_SCRIPT.format(
        source=str(WORLDS / "tidy" / "src"),
        work_dir=str(work_dir),
        reset_options=reset_options,
        fatal_grace=fatal_grace,
        noise=noise,
    )
    return subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def watched(process: subprocess.Popen[str]) -> threading.Timer:
    """Kill the process if it stops answering, so a hang fails instead of waiting."""
    watchdog = threading.Timer(PROCESS_WAIT, process.kill)
    watchdog.start()
    return watchdog


def ask(process: subprocess.Popen[str], requests: list[dict[str, Any]], *, answers: int) -> str:
    """Send `requests` and read `answers` frames back, leaving stdin open."""
    assert process.stdin is not None and process.stdout is not None
    for request in requests:
        process.stdin.write(f"{json.dumps(request)}\n")
    process.stdin.flush()
    return "".join(process.stdout.readline() for _ in range(answers))


def run_server_process(
    reset_options: dict[str, Any],
    requests: list[dict[str, Any]],
    work_dir: Path,
    *,
    answers: int,
    noise: str = "",
) -> Finished:
    """A whole exchange: send `requests`, read `answers`, then close stdin and wait.

    The answers are read before stdin is closed, because closing it is the
    disconnect: the serve loop ends with the read stream, and an answer still on
    a worker thread goes with it.
    """
    process = spawn_server_process(reset_options, work_dir, noise=noise)
    watchdog = watched(process)
    try:
        answered = ask(process, requests, answers=answers)
        # `communicate` closes stdin, which is the EOF this process exits on.
        rest, errors = process.communicate()
    finally:
        watchdog.cancel()
    return Finished(code=process.returncode, frames=frames(answered + rest), errors=errors)


def handshake(version: str = HANDSHAKE_PROTOCOL_VERSIONS[-1]) -> list[dict[str, Any]]:
    """The two frames a client opens a handshake-era connection with."""
    return [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": version,
                "capabilities": {},
                "clientInfo": {"name": "seahaven-tests", "version": "1.0.0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
    ]


def frames(stdout: str) -> list[dict[str, Any]]:
    """Every line of stdout as a protocol frame. A line that is not one fails here."""
    return [json.loads(line) for line in stdout.splitlines() if line.strip()]


def test_serve_speaks_the_protocol_over_real_pipes_and_exits_on_eof(tmp_path: Path) -> None:
    """The whole exchange against `serve()` itself: the handshake, two calls, then EOF."""
    work_dir = tmp_path / "work"
    finished = run_server_process(
        {"now": "2024-03-05T12:00:00Z"},
        [
            *handshake(),
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "write_note", "arguments": {"body": "from a real process"}},
            },
        ],
        work_dir,
        answers=3,
    )

    answered = finished.by_id()
    assert finished.code == 0, finished.errors
    assert answered[1]["result"]["serverInfo"] == {"name": "tidy", "version": "1.0.0"}
    assert [tool["name"] for tool in answered[2]["result"]["tools"]] == ["write_note"]
    written = json.loads(answered[3]["result"]["content"][0]["text"])
    assert written["body"] == "from a real process"
    assert written["created_at"] == "2024-03-05T12:00:00.000Z"
    # The disconnect destroyed the instance, and destruction is what removes its
    # directory and its copy of the fixture.
    assert list(work_dir.iterdir()) == []


def test_serve_answers_a_failed_start_and_then_exits_one(tmp_path: Path) -> None:
    """The client is told why, on the wire, and the exit code says the process failed."""
    finished = run_server_process({"fixture": "not_a_fixture"}, handshake(), tmp_path, answers=1)

    [answered] = finished.frames
    assert "not_a_fixture" in answered["error"]["message"]
    assert finished.code == 1


def test_serve_destroys_the_instance_on_sigterm(tmp_path: Path) -> None:
    """A client that is killed rather than closed leaves nothing behind.

    Without a handler of ours the signal's default disposition would end the
    process where it stood, and the instance's directory and its copy of the
    fixture would stay on disk.
    """
    work_dir = tmp_path / "work"
    process = spawn_server_process({}, work_dir)
    watchdog = watched(process)
    try:
        ask(process, handshake(), answers=1)
        living = list(work_dir.iterdir())
        process.send_signal(signal.SIGTERM)
        code = process.wait(timeout=PROCESS_WAIT)
    finally:
        watchdog.cancel()
        process.kill()

    assert len(living) == 1, "the handshake should have made one instance"
    assert code == 0
    assert list(work_dir.iterdir()) == []


def test_serve_does_not_wait_for_a_client_that_will_not_leave(tmp_path: Path) -> None:
    """A process that has answered a failed start exits on its own, stdin or no stdin."""
    process = spawn_server_process({"fixture": "not_a_fixture"}, tmp_path, fatal_grace=0.5)
    watchdog = watched(process)
    try:
        [answered] = frames(ask(process, handshake(), answers=1))
        # The client is told, and then says nothing and closes nothing.
        code = process.wait(timeout=PROCESS_WAIT)
    finally:
        watchdog.cancel()
        process.kill()

    assert "not_a_fixture" in answered["error"]["message"]
    assert code == 1


def test_a_world_that_prints_while_it_is_found_cannot_corrupt_the_stream(tmp_path: Path) -> None:
    """stdout belongs to the protocol, from before the world is resolved.

    The assertion is on the property and not on the mechanism: `frames` fails on
    a line of stdout that is not a protocol frame, whatever put it there.
    """
    noise = "a world's package said something on stdout"
    finished = run_server_process(
        {},
        [
            *handshake(),
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "write_note", "arguments": {"body": "quiet"}},
            },
        ],
        tmp_path / "work",
        answers=2,
        noise=noise,
    )

    assert finished.code == 0, finished.errors
    assert finished.by_id()[1]["result"]["serverInfo"]["name"] == "tidy"
    assert noise in finished.errors
