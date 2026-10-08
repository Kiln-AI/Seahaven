"""`seahaven.http.main`: the command line of a world's `serve_http.py`.

The reset-option rules are `seahaven mcp`'s, and `tests/test_cli_mcp.py` covers
them in full. This module checks that `main` reaches them, with the differences
that are its own: no seed is added, and `control_tools` is refused for this
server's reason. `uvicorn.run` is replaced, so everything from `argv` to the
built app runs and nothing is served, except in the last test, which runs the
test world as a script and talks to it over HTTP.
"""

import json
import queue
import re
import signal
import subprocess
import sys
import threading
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import seahaven.http
from seahaven import World
from seahaven.cli.mcp import CONVENIENCE, GENERAL_VARIABLE
from seahaven.http.command import WITHHELD_REASON
from tests.conftest import INSTANT_ISO
from tests.http_world import build, handle

# `main` serves through `seahaven.http.server`, which imports Starlette and
# uvicorn. Both extras install them.
pytest.importorskip(
    "seahaven.http.server", exc_type=ImportError, reason="the server stack does not import here"
)

from seahaven.http import server

SCRIPT = Path(__file__).parent / "http_world.py"
# A bound on a hang rather than a measurement.
PROCESS_WAIT = 60.0
RUNNING = re.compile(r"Uvicorn running on http://127\.0\.0\.1:(\d+)")
# The lifespan shutdown ran, which destroys every instance.
SHUT_DOWN = re.compile(r"Application shutdown complete")


@dataclass
class Served:
    """What `seahaven.http.server.serve` was called with, and whether uvicorn was reached."""

    calls: list[dict[str, Any]]
    ran: list[Any]

    @property
    def only(self) -> dict[str, Any]:
        assert len(self.calls) == 1
        return self.calls[0]


@pytest.fixture(autouse=True)
def _no_ambient_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whatever the developer's shell has set, these tests start with none of it."""
    for variable in (*CONVENIENCE.values(), GENERAL_VARIABLE):
        monkeypatch.delenv(variable, raising=False)


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> Served:
    """Record each call to the real `serve`, which then runs with `uvicorn.run` replaced."""
    recorded = Served(calls=[], ran=[])
    real = server.serve

    def serve(world: World, handler: Any, **kwargs: Any) -> None:
        recorded.calls.append({"world": world, "handler": handler, **kwargs})
        real(world, handler, **kwargs)

    monkeypatch.setattr(server, "serve", serve)
    monkeypatch.setattr(server.uvicorn, "run", lambda app, **kwargs: recorded.ran.append(app))
    return recorded


@pytest.fixture
def notes_world(tmp_path: Path) -> World:
    return build(fixtures_dir=tmp_path / "fixtures", work_dir=tmp_path / "work")


def refused(notes_world: World, capsys: pytest.CaptureFixture[str], *argv: str) -> str:
    """Run `main`, expect exit 1, and answer the one line it wrote to stderr."""
    with pytest.raises(SystemExit) as raised:
        seahaven.http.main(notes_world, handle, list(argv))
    assert raised.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    lines = captured.err.splitlines()
    assert len(lines) == 1, captured.err
    return lines[0]


def test_the_defaults_reach_serve(
    notes_world: World, served: Served, capsys: pytest.CaptureFixture[str]
) -> None:
    seahaven.http.main(notes_world, handle, [])

    assert served.only == {
        "world": notes_world,
        "handler": handle,
        "host": "127.0.0.1",
        "port": 8000,
        "reset_options": {},
        "max_instances": 100,
    }
    assert len(served.ran) == 1
    assert capsys.readouterr().out == "Serving notes_api at http://127.0.0.1:8000/worlds/{id}\n"


def test_host_port_and_max_instances_reach_serve(notes_world: World, served: Served) -> None:
    seahaven.http.main(
        notes_world, handle, ["--host", "0.0.0.0", "--port", "9000", "--max-instances", "0"]
    )

    assert (served.only["host"], served.only["port"], served.only["max_instances"]) == (
        "0.0.0.0",
        9000,
        0,
    )


def test_each_flag_reaches_the_reset_options(notes_world: World, served: Served) -> None:
    seahaven.http.main(
        notes_world,
        handle,
        ["--seed", "7", "--now", "2024-03-05T12:00:00Z", "--clock-mode", "tick"],
    )

    assert served.only["reset_options"] == {
        "seed": 7,
        "now": "2024-03-05T12:00:00Z",
        "clock_mode": "tick",
    }


def test_each_variable_reaches_the_reset_options(
    notes_world: World, served: Served, monkeypatch: pytest.MonkeyPatch
) -> None:
    with notes_world.instance(None, now=INSTANT_ISO, clock_mode="fixed") as live:
        live.freeze("demo", "an empty fixture")
    monkeypatch.setenv("SEAHAVEN_FIXTURE", "demo")
    monkeypatch.setenv("SEAHAVEN_SEED", "7")
    monkeypatch.setenv("SEAHAVEN_NOW", "2099-03-05T12:00:00Z")
    monkeypatch.setenv("SEAHAVEN_CLOCK_MODE", "fixed")

    seahaven.http.main(notes_world, handle, [])

    assert served.only["reset_options"] == {
        "fixture": "demo",
        "seed": 7,
        "now": "2099-03-05T12:00:00Z",
        "clock_mode": "fixed",
    }


@pytest.mark.parametrize("door", ["flag", "variable"])
def test_reset_options_are_passed_whole(
    notes_world: World, served: Served, monkeypatch: pytest.MonkeyPatch, door: str
) -> None:
    document = {"startup": {"explode": False}, "clock_mode": "tick"}
    argv = ["--reset-options", json.dumps(document)] if door == "flag" else []
    if door == "variable":
        monkeypatch.setenv(GENERAL_VARIABLE, json.dumps(document))

    seahaven.http.main(notes_world, handle, argv)

    assert served.only["reset_options"] == document


def test_no_seed_is_left_to_each_instance(
    notes_world: World, served: Served, capsys: pytest.CaptureFixture[str]
) -> None:
    """Each instance mints its own seed in the registry, so `main` announces none."""
    seahaven.http.main(notes_world, handle, ["--clock-mode", "tick"])

    assert "seed" not in served.only["reset_options"]
    assert capsys.readouterr().err == ""


def test_the_two_doors_are_refused_together_with_the_shared_message(
    notes_world: World, served: Served, capsys: pytest.CaptureFixture[str]
) -> None:
    line = refused(notes_world, capsys, "--fixture", "demo", "--reset-options", "{}")

    assert line.startswith(
        '--fixture cannot be combined with --reset-options; put "fixture" inside the '
        "--reset-options JSON instead"
    )
    assert served.calls == []


@pytest.mark.parametrize("door", ["flag", "variable"])
def test_control_tools_are_refused_with_this_servers_reason(
    notes_world: World,
    served: Served,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    door: str,
) -> None:
    document = '{"control_tools": true}'
    argv = ["--reset-options", document] if door == "flag" else []
    if door == "variable":
        monkeypatch.setenv(GENERAL_VARIABLE, document)

    line = refused(notes_world, capsys, *argv)

    spelling = "--reset-options" if door == "flag" else GENERAL_VARIABLE
    assert line == f'{spelling} does not take "control_tools": {WITHHELD_REASON}'
    assert served.calls == []


def test_a_seed_that_is_not_an_integer_is_refused(
    notes_world: World, served: Served, capsys: pytest.CaptureFixture[str]
) -> None:
    assert refused(notes_world, capsys, "--seed", "seven") == (
        "--seed takes an integer, not 'seven'"
    )
    assert served.calls == []


def test_a_negative_max_instances_is_refused(
    notes_world: World, served: Served, capsys: pytest.CaptureFixture[str]
) -> None:
    assert refused(notes_world, capsys, "--max-instances", "-1") == (
        "--max-instances takes 0 or more, 0 for no limit, not -1"
    )
    assert served.calls == []


def test_an_unknown_fixture_is_refused_before_serving(
    notes_world: World, served: Served, capsys: pytest.CaptureFixture[str]
) -> None:
    line = refused(notes_world, capsys, "--fixture", "nope")

    assert "names the fixture 'nope'" in line
    assert served.ran == []


def test_the_help_names_this_servers_defaults(
    notes_world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as raised:
        seahaven.http.main(notes_world, handle, ["--help"])

    assert raised.value.code == 0
    text = " ".join(capsys.readouterr().out.split())
    assert "Serve the notes_api world's HTTP API" in text
    assert "the default is a random seed for each instance" in text
    assert "the default is wall" in text
    assert "written to stderr" not in text


@pytest.mark.parametrize("missing", ["uvicorn", "starlette"])
def test_without_the_server_stack_main_says_to_install_the_extra(
    notes_world: World,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
) -> None:
    monkeypatch.delitem(sys.modules, "seahaven.http.server")
    # The package and every submodule already imported, as if none were installed.
    for name in [each for each in sys.modules if each.partition(".")[0] == missing]:
        monkeypatch.setitem(sys.modules, name, None)

    assert refused(notes_world, capsys) == seahaven.http.MISSING_EXTRA


# --- the script, as a process ------------------------------------------------


@pytest.fixture
def script() -> Iterator[tuple[subprocess.Popen[str], queue.Queue[str]]]:
    """`python tests/http_world.py --port 0`, and a queue of its stderr lines."""
    process = subprocess.Popen(
        [sys.executable, str(SCRIPT), "--port", "0"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stderr is not None
    errors: queue.Queue[str] = queue.Queue()
    stream = process.stderr

    def drain() -> None:
        for line in stream:
            errors.put(line)

    drainer = threading.Thread(target=drain, name="test-script-stderr", daemon=True)
    drainer.start()
    try:
        yield process, errors
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(PROCESS_WAIT)
        drainer.join(PROCESS_WAIT)
        stream.close()
        if process.stdout is not None:
            process.stdout.close()


def test_the_script_serves_over_http(
    script: tuple[subprocess.Popen[str], queue.Queue[str]],
) -> None:
    process, errors = script
    assert process.stdout is not None

    assert process.stdout.readline().startswith("Serving notes_api at http://127.0.0.1:")
    port = _port(errors)
    base = f"http://127.0.0.1:{port}/worlds/proc"
    created = _call("POST", f"{base}/notes", {"body": "from a process"})
    fetched = _call("GET", f"{base}/notes/{created['id']}")

    assert fetched == created
    assert fetched["body"] == "from a process"
    process.terminate()
    # uvicorn shuts down, then raises the signal again, so the exit is SIGTERM's.
    _wait_for(errors, SHUT_DOWN)
    assert process.wait(PROCESS_WAIT) == -signal.SIGTERM


def _port(errors: queue.Queue[str]) -> int:
    """Read stderr until uvicorn says which port the kernel gave it."""
    return int(_wait_for(errors, RUNNING)[1])


def _wait_for(errors: queue.Queue[str], pattern: re.Pattern[str]) -> re.Match[str]:
    """Read stderr until a line matches `pattern`."""
    seen: list[str] = []
    while True:
        try:
            line = errors.get(timeout=PROCESS_WAIT)
        except queue.Empty:
            raise AssertionError(f"no line matched {pattern.pattern!r}: {seen}") from None
        seen.append(line)
        if found := pattern.search(line):
            return found


def _call(method: str, url: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, method=method)
    # No proxy: a proxy in the environment would be asked for 127.0.0.1.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=PROCESS_WAIT) as response:
        return json.loads(response.read())
