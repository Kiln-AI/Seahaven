"""`seahaven mcp`: the real command, spawned as a process and talked to over stdio.

`tests/test_cli_mcp.py` checks what the options resolve to and `tests/test_mcp_server.py`
drives the server in process. Neither one runs the command, and `AGENTS.md` asks
for the real entry point: this project has a history of defects that passed a
unit test and failed on the first real call, and everything between `main(argv)`
and `world.instance(**options)` is only proved by a process.

Two ways of talking to it, because they answer different questions. The SDK's
own `Client` launches the command and speaks the protocol, which is what an MCP
client does; raw pipes are for the questions a client cannot answer -- the exit
code, the stderr line, and whether every line of stdout is a protocol frame.

The world is written into `tmp_path` rather than taken from `tests/worlds/`: it
prints from three places on purpose, binds a startup keyword no flag spells, and
its `work_dir` is the test's, so the parent can watch the instance directory
appear and go.
"""

import json
import subprocess
import sys
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from seahaven.cli.mcp import SEED_LINE
from tests.conftest import mcp_sdk

mcp_sdk()

import anyio
from mcp import Client, StdioServerParameters, types

# A bound on a hang rather than a measurement: a whole exchange, including the
# world's import and its instance, inside this many seconds.
PROCESS_WAIT = 60.0

# The entry point `[project.scripts] seahaven` names, which is what a console
# script installed on a PATH calls. A test that ran the installed script would
# be testing the installer's wrapper instead.
ENTRY_POINT = "from seahaven.cli import main; raise SystemExit(main())"

# What the noisy world says, and where from. All three have to reach stderr and
# none of them may reach the wire (`functional_spec.md` §8 and §12 test 5).
IMPORT_NOISE = "the world's package printed while it was imported"
STARTUP_NOISE = "the startup hook printed to stdout"
TOOL_NOISE = "the tool printed to stdout"

# What two overlapping calls pause for, long enough that a lost update is
# certain if they do not serialise and short enough not to be felt.
OVERLAP = 0.05

PYPROJECT = """
[project]
name = "noisy"
version = "1.0.0"
requires-python = ">=3.14"
"""

WORLD = '''
"""A world for the MCP process tests: noisy on purpose, and easy to interrogate."""

import time
from typing import Any

import seahaven

world = seahaven.World(
    name="noisy",
    version="2.1.0",
    schema=(
        "CREATE TABLE notes (id TEXT PRIMARY KEY, body TEXT NOT NULL, "
        "n INTEGER NOT NULL DEFAULT 0) STRICT;"
    ),
    description="Notes, and the rows in them.",
    mcp_server_instructions="Write a note, then read it back.",
    state_format="seahaven.state/1",
    work_dir={work_dir!r},
)

print({import_noise!r})


class Refused(seahaven.ToolError):
    """This world's own error, as every world defines its own."""

    def __init__(self, message: str) -> None:
        super().__init__("refused", message, {{"because": "the test asked for it"}})


@world.instance_startup
def open_the_shop(ctx: seahaven.Ctx, *, region: str = "us") -> None:
    """Print, and remember the keyword only the general door can reach."""
    print({startup_noise!r})
    ctx.state["region"] = region


@world.tool
def write_note(ctx: seahaven.Ctx, body: str) -> dict[str, Any]:
    """Write a note and return it."""
    print({tool_noise!r})
    note = {{"id": ctx.ids.uuid(), "body": body, "created_at": ctx.clock.iso()}}
    ctx.db.execute("INSERT INTO notes (id, body) VALUES (?, ?)", note["id"], note["body"])
    return note


@world.tool
def notes(ctx: seahaven.Ctx) -> list[dict[str, Any]]:
    """Every note's body, in the order they were written."""
    return [row["body"] for row in ctx.db.rows("SELECT body FROM notes ORDER BY rowid")]


@world.tool
def shop(ctx: seahaven.Ctx) -> dict[str, Any]:
    """What this instance was started with."""
    return {{"region": ctx.state["region"], "now": ctx.clock.iso()}}


@world.tool
def refuse(ctx: seahaven.Ctx) -> None:
    """Fail the way a world fails."""
    raise Refused("this note cannot be written")


@world.tool
def bump(ctx: seahaven.Ctx) -> dict[str, int]:
    """Read, pause, then write: a lost update unless two calls serialise."""
    row = ctx.db.one("SELECT n FROM notes WHERE id = 'counter'")
    counted = int(row["n"]) + 1 if row is not None else 1
    time.sleep({overlap!r})
    ctx.db.execute(
        "INSERT INTO notes (id, body, n) VALUES ('counter', 'the counter', ?) "
        "ON CONFLICT(id) DO UPDATE SET n = ?",
        counted,
        counted,
    )
    return {{"n": counted}}
'''


@dataclass(frozen=True)
class Project:
    """A world on disk, and the directory its instances are made under."""

    root: Path
    work_dir: Path

    def living(self) -> list[Path]:
        """The instance directories that exist right now."""
        return list(self.work_dir.iterdir())


@pytest.fixture
def project(tmp_path: Path) -> Project:
    """The noisy world as a world project, found by the convention `seahaven` uses."""
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    package = tmp_path / "src" / "noisy"
    package.mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    (package / "__init__.py").write_text(
        WORLD.format(
            work_dir=str(work_dir),
            import_noise=IMPORT_NOISE,
            startup_noise=STARTUP_NOISE,
            tool_noise=TOOL_NOISE,
            overlap=OVERLAP,
        ),
        encoding="utf-8",
    )
    return Project(root=tmp_path, work_dir=work_dir)


def command(*options: str) -> list[str]:
    """`seahaven mcp ...`, as the argument list of a real process."""
    return [sys.executable, "-c", ENTRY_POINT, "mcp", *options]


def environment(**variables: str) -> dict[str, str]:
    """The child's whole environment: what it needs to run, and nothing of ours.

    `PYTHONUNBUFFERED` is deliberately absent. A client launching this command
    does not set it, and an unbuffered child would hide the one stdout question
    worth asking: what happens to a world's `print` that is still in Python's
    buffer when the transport gives the file descriptors back.
    """
    return {"PATH": "/usr/bin:/bin", **variables}


# --- through the SDK's own client -------------------------------------------


def talk[T](
    project: Project,
    work: Callable[[Client], Awaitable[T]],
    *options: str,
    mode: str = "legacy",
    **variables: str,
) -> T:
    """Launch the command, run `work` against it, and answer what `work` answered.

    `legacy` by default, which is the era every MCP client in use today opens
    with; `auto` is the SDK client's own default and never sends `initialize`.
    """
    parameters = StdioServerParameters(
        command=sys.executable,
        args=command(*options)[1:],
        cwd=str(project.root),
        env=environment(**variables),
    )

    async def main() -> T:
        with anyio.fail_after(PROCESS_WAIT):
            async with Client(parameters, mode=mode) as client:
                return await work(client)

    return anyio.run(main)


def text(result: types.CallToolResult) -> str:
    """The one text block a result carries."""
    [block] = result.content
    assert isinstance(block, types.TextContent)
    return block.text


def answered(result: types.CallToolResult) -> Any:
    assert result.is_error is False, text(result)
    return json.loads(text(result))


def triple(result: types.CallToolResult) -> dict[str, Any]:
    """The `{code, message, details}` an `isError` result carries."""
    assert result.is_error, result
    return json.loads(text(result))


@pytest.mark.parametrize("mode", ["legacy", "auto"])
def test_the_command_serves_the_world_it_finds(project: Project, mode: str) -> None:
    """The world discovery convention, the identity, the instructions and the tools.

    No `--world`: the command is run inside the world's project, which is what
    an `.mcp.json` does with `cwd`.
    """

    async def work(client: Client) -> tuple[Any, str | None, list[str]]:
        listed = await client.list_tools()
        return client.server_info, client.instructions, [tool.name for tool in listed.tools]

    info, instructions, names = talk(project, work, mode=mode)

    assert info is not None
    assert (info.name, info.version) == ("noisy", "2.1.0")
    assert instructions == "Write a note, then read it back."
    assert names == ["write_note", "notes", "shop", "refuse", "bump"]


def test_a_write_is_seen_by_a_later_read_in_the_same_process(project: Project) -> None:
    """One instance for the life of the process: the world moved, and stayed moved."""

    async def work(client: Client) -> types.CallToolResult:
        await client.call_tool("write_note", {"body": "first"})
        await client.call_tool("write_note", {"body": "second"})
        return await client.call_tool("notes", {})

    assert answered(talk(project, work)) == ["first", "second"]


def test_a_tool_error_is_an_is_error_result_carrying_the_triple(project: Project) -> None:
    """Data the agent reads and recovers from, never a JSON-RPC error."""

    async def work(client: Client) -> types.CallToolResult:
        return await client.call_tool("refuse", {})

    assert triple(talk(project, work)) == {
        "code": "refused",
        "message": "this note cannot be written",
        "details": {"because": "the test asked for it"},
    }


def test_a_control_tool_is_answered_as_an_unknown_tool(project: Project) -> None:
    """Nothing that reaches an MCP client may run arbitrary SQL against the world.

    The refusal is the framework's: the instance is made without
    `control_tools`, and a control tool's name then earns `UnknownTool` in the
    same words a name the world does not have earns. Nothing in `seahaven/mcp/`
    filters the name.
    """

    async def work(client: Client) -> tuple[list[str], types.CallToolResult]:
        listed = await client.list_tools()
        return (
            [tool.name for tool in listed.tools],
            await client.call_tool("controller_run_sql", {"sql": "SELECT 1"}),
        )

    names, result = talk(project, work)

    assert "controller_run_sql" not in names
    assert triple(result) == {
        "code": "unknown_tool",
        "message": "unknown tool: controller_run_sql",
        "details": {"name": "controller_run_sql"},
    }


def test_the_convenience_flags_reach_the_instance(project: Project) -> None:
    """`--now` and `--seed` are what the world's clock and its ids answer with."""

    async def work(client: Client) -> tuple[Any, Any]:
        written = await client.call_tool("write_note", {"body": "hello"})
        return answered(written), answered(await client.call_tool("shop", {}))

    fixed = ("--now", "2024-03-05T12:00:00Z", "--clock-mode", "fixed")
    note, shop = talk(project, work, *fixed, "--seed", "7")
    again, _ = talk(project, work, *fixed, "--seed", "7")
    other, _ = talk(project, work, *fixed, "--seed", "8")

    assert note["created_at"] == "2024-03-05T12:00:00.000Z"
    assert shop["now"] == "2024-03-05T12:00:00.000Z"
    # The same seed replays the same ids, which is what passing one back is for.
    assert again["id"] == note["id"]
    assert other["id"] != note["id"]


def test_the_environment_variables_reach_the_instance(project: Project) -> None:
    """An MCP client configuration passes `env` more comfortably than `args`."""

    async def work(client: Client) -> Any:
        return answered(await client.call_tool("write_note", {"body": "hello"}))

    note = talk(
        project,
        work,
        SEAHAVEN_NOW="2024-03-05T12:00:00Z",
        SEAHAVEN_SEED="7",
        SEAHAVEN_CLOCK_MODE="fixed",
    )
    by_flag = talk(project, work, "--now", "2024-03-05T12:00:00Z", "--seed", "7")

    assert note["created_at"] == "2024-03-05T12:00:00.000Z"
    assert note["id"] == by_flag["id"]


def test_the_clock_mode_flag_reaches_the_instance(project: Project) -> None:
    """`tick`: each call is one second after the one before it."""

    async def work(client: Client) -> list[Any]:
        return [answered(await client.call_tool("shop", {})) for _ in range(2)]

    first, second = talk(project, work, "--now", "2024-03-05T12:00:00Z", "--clock-mode", "tick")

    assert (first["now"], second["now"]) == ("2024-03-05T12:00:01.000Z", "2024-03-05T12:00:02.000Z")


def test_reset_options_reach_a_startup_hook(project: Project) -> None:
    """The general door: a keyword of the world's own, which no flag spells.

    A startup keyword travels inside `"startup"`, the namespace `world.instance()`
    gives a world's own keywords, and a key at the top level is refused.
    """

    async def work(client: Client) -> Any:
        return answered(await client.call_tool("shop", {}))

    given = '{"startup": {"region": "eu"}, "seed": 7}'

    assert talk(project, work)["region"] == "us"
    assert talk(project, work, "--reset-options", given)["region"] == "eu"


def test_reset_options_carry_setup_sql_to_the_instance(project: Project) -> None:
    """The instance starts with the rows the SQL wrote, before the first call."""

    async def work(client: Client) -> Any:
        return answered(await client.call_tool("notes", {}))

    given = json.dumps({"setup_sql": "INSERT INTO notes (id, body) VALUES ('n1', 'seeded')"})

    assert talk(project, work, "--reset-options", given) == ["seeded"]


def test_two_calls_in_flight_serialise(project: Project) -> None:
    """Calls into one instance queue on the instance's lock, so no update is lost."""

    async def work(client: Client) -> list[Any]:
        async with anyio.create_task_group() as calls:
            calls.start_soon(client.call_tool, "bump", {})
            calls.start_soon(client.call_tool, "bump", {})
        return answered(await client.call_tool("bump", {}))

    assert talk(project, work) == {"n": 3}


# --- through raw pipes ------------------------------------------------------


@dataclass
class Finished:
    """What a served process left behind: its exit code and its two streams."""

    code: int
    out: str
    err: str

    def frames(self) -> list[dict[str, Any]]:
        """Every line of stdout as a protocol frame. A line that is not one fails here."""
        parsed: list[dict[str, Any]] = []
        for line in self.out.splitlines():
            if not line.strip():
                continue
            try:
                parsed.append(json.loads(line))
            except json.JSONDecodeError:
                pytest.fail(f"stdout carried something that is not a protocol frame: {line!r}")
        return parsed

    def by_id(self) -> dict[Any, dict[str, Any]]:
        return {frame["id"]: frame for frame in self.frames() if "id" in frame}


def handshake() -> list[dict[str, Any]]:
    """The two frames a client opens a handshake-era connection with."""
    return [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "seahaven-tests", "version": "1.0.0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
    ]


def spawn(project: Project, *options: str, **variables: str) -> subprocess.Popen[str]:
    """The command on real pipes, with its instances under the project's work directory."""
    return subprocess.Popen(
        command(*options),
        cwd=str(project.root),
        env=environment(**variables),
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


def run_command(
    project: Project,
    *options: str,
    requests: list[dict[str, Any]] | None = None,
    answers: int = 0,
    **variables: str,
) -> Finished:
    """A whole exchange: send the requests, read the answers, then close stdin and wait.

    The answers are read before stdin is closed, because closing it is the
    disconnect: the serve loop ends with the read stream.
    """
    process = spawn(project, *options, **variables)
    watchdog = watched(process)
    try:
        answered = ask(process, requests or [], answers=answers) if requests else ""
        # `communicate` closes stdin, which is the EOF the process exits on.
        rest, errors = process.communicate()
    finally:
        watchdog.cancel()
    return Finished(code=process.returncode, out=answered + rest, err=errors)


@pytest.fixture
def noisy_exchange() -> list[dict[str, Any]]:
    """A handshake and one call, which is one print from each of the world's three."""
    return [
        *handshake(),
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "write_note", "arguments": {"body": "quiet"}},
        },
    ]


def test_a_world_that_prints_cannot_corrupt_the_stream(
    project: Project, noisy_exchange: list[dict[str, Any]]
) -> None:
    """stdout belongs to the protocol, whatever the world says on it.

    The world prints while it is imported, again from its startup hook and again
    from the tool. The assertion is the property and not the mechanism: every
    line of stdout parses as a frame, whatever put it there, and the prose is on
    stderr where the person debugging is already looking.
    """
    finished = run_command(project, requests=noisy_exchange, answers=2)

    assert finished.code == 0, finished.err
    assert finished.by_id()[2]["result"]["structuredContent"]["body"] == "quiet"
    for said in (IMPORT_NOISE, STARTUP_NOISE, TOOL_NOISE):
        assert said in finished.err


def test_the_seed_it_picked_is_on_stderr_and_nowhere_else(
    project: Project, noisy_exchange: list[dict[str, Any]]
) -> None:
    """A user who wants the run again can pass the seed back with `--seed`."""
    finished = run_command(project, requests=noisy_exchange, answers=2)

    [line] = [said for said in finished.err.splitlines() if said.startswith("no seed")]
    seed = int(line.rsplit(maxsplit=1)[-1])
    assert line == SEED_LINE.format(seed=seed)
    # §1: the client is never told, so the seed is in no frame the client read.
    assert str(seed) not in finished.out


def test_eof_destroys_the_instance_and_removes_its_working_directory(project: Project) -> None:
    """Closing the client takes the world's copy of the fixture with it."""
    process = spawn(project)
    watchdog = watched(process)
    try:
        ask(process, handshake(), answers=1)
        living = project.living()
        assert process.stdin is not None
        process.stdin.close()
        code = process.wait(timeout=PROCESS_WAIT)
    finally:
        watchdog.cancel()
        process.kill()

    assert len(living) == 1, "the handshake should have made exactly one instance"
    assert code == 0
    assert project.living() == []


def test_a_bad_fixture_is_answered_on_the_wire_and_exits_one(project: Project) -> None:
    """Not scrubbed: the reader is the person who wrote the client configuration."""
    finished = run_command(project, "--fixture", "not_a_fixture", requests=handshake(), answers=1)

    [answer] = finished.frames()
    assert "not_a_fixture" in answer["error"]["message"]
    assert finished.code == 1
    assert project.living() == []


def test_a_refusal_is_one_line_on_stderr_and_exit_one(project: Project) -> None:
    """A user who provokes this is looking at their own client configuration."""
    finished = run_command(project, "--fixture", "small_startup", SEAHAVEN_RESET_OPTIONS="{}")

    assert finished.code == 1
    assert finished.err.splitlines() == [
        '--fixture cannot be combined with SEAHAVEN_RESET_OPTIONS; put "fixture" inside the '
        "SEAHAVEN_RESET_OPTIONS JSON instead"
    ]
    # Nothing was served, so the world was never imported and nothing was made.
    assert finished.out == ""
    assert project.living() == []


def test_a_startup_keyword_at_the_top_level_is_refused_before_anything_is_served(
    project: Project,
) -> None:
    """`--reset-options` names what `world.instance()` takes, and a world's own keyword is not one.

    The message names `"startup"`, because that is where a world's own keywords
    moved to. Without this refusal the key reaches `world.instance()` after the
    client has connected, and the user reads a `TypeError` instead.
    """
    finished = run_command(project, "--reset-options", '{"region": "eu"}')

    assert finished.code == 1
    assert finished.err.splitlines() == [
        '--reset-options does not take "region"; --reset-options takes "clock_mode", "fixture", '
        '"now", "seed", "setup_sql", "startup" and "state_format", and a world\'s own startup '
        'keywords go inside "startup": --reset-options \'{"fixture": "small_startup", '
        '"startup": {"user_id": "u_12"}}\''
    ]
    assert finished.out == ""
    assert project.living() == []


def test_control_tools_cannot_be_turned_on_through_the_general_door(project: Project) -> None:
    """`control_tools` is a keyword `world.instance()` takes and this command withholds.

    `seahaven serve` has `--include-control-tools`; `seahaven mcp` has no such
    flag, and the general door is not a way around that.
    """
    finished = run_command(project, "--reset-options", '{"control_tools": true}')

    assert finished.code == 1
    assert finished.err.splitlines() == [
        '--reset-options does not take "control_tools": seahaven mcp publishes the world\'s '
        "own tools and nothing else, and nothing reaching an MCP client may run SQL "
        "against the world"
    ]
    assert finished.out == ""
    assert project.living() == []


def test_a_world_that_cannot_be_imported_is_answered_rather_than_raised(project: Project) -> None:
    """A process that died before the handshake gives a client nothing but a broken pipe."""
    finished = run_command(project, "--world", "nope:world", requests=handshake(), answers=1)

    [answer] = finished.frames()
    assert "nope" in answer["error"]["message"]
    assert finished.code == 1


def test_a_world_option_that_is_not_module_attr_never_starts_the_protocol(
    project: Project,
) -> None:
    """The one thing about `--world` that is refused before anything is served."""
    finished = run_command(project, "--world", "nope")

    assert finished.code == 1
    assert finished.err.splitlines() == [
        "--world takes module:attr, not 'nope'; for example --world myworld:world"
    ]
    assert finished.out == ""
