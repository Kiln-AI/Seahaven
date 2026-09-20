"""`seahaven mcp`: the options, the four doors they arrive by, and every refusal.

The server is `seahaven/mcp/`, which is tested in `tests/test_mcp_server.py` and
driven as a real process in `tests/test_mcp_process.py`. What the CLI adds is the
resolution of `functional_spec.md` §2.2 to §2.4 -- a flag, a variable, the
general door, and what happens when they are mixed -- so `serve` is replaced by a
recorder here and this module is about what it would have been called with.

Nothing here imports the MCP SDK, deliberately: `seahaven[serve]` and
`seahaven[mcp]` cannot be installed together, and these rules are the same rules
in both environments, so they are checked in both.
"""

import sys
from collections.abc import Iterator
from dataclasses import dataclass
from types import ModuleType
from typing import Any

import pytest

from seahaven.cli.mcp import (
    CONVENIENCE,
    GENERAL_VARIABLE,
    MISSING_EXTRA,
    SEED_CEILING,
    SEED_LINE,
)
from tests.conftest import WORLDS, CliResult, run_cli

pytestmark = pytest.mark.usefixtures("isolated_imports")

VARIABLES = (*CONVENIENCE.values(), GENERAL_VARIABLE)


@dataclass(frozen=True)
class Call:
    """What `seahaven.mcp.serve` was called with, instead of a server on stdio."""

    world: Any
    reset_options: dict[str, Any]

    @property
    def seed(self) -> Any:
        return self.reset_options["seed"]


@pytest.fixture(autouse=True)
def _no_ambient_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whatever the developer's shell has set, these tests start with none of it."""
    for variable in VARIABLES:
        monkeypatch.delenv(variable, raising=False)


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[Call]]:
    """`seahaven.mcp` as a module with nothing in it but a recorder.

    A stub module rather than a patched attribute, because the real package
    imports the SDK: in an environment synced for the `serve` extra there is no
    `seahaven.mcp` to patch, and these rules still have to be checked there.
    """
    recorded: list[Call] = []

    def serve(world: Any, *, reset_options: dict[str, Any]) -> int:
        recorded.append(Call(world=world, reset_options=dict(reset_options)))
        return 0

    stub = ModuleType("seahaven.mcp")
    stub.serve = serve  # ty: ignore[unresolved-attribute]
    monkeypatch.setitem(sys.modules, "seahaven.mcp", stub)
    yield recorded


def serve_mcp(capsys: pytest.CaptureFixture[str], *argv: str) -> CliResult:
    return run_cli(capsys, "mcp", *argv)


# --- the four doors ---------------------------------------------------------


def test_each_flag_reaches_the_reset_options(
    calls: list[Call], capsys: pytest.CaptureFixture[str]
) -> None:
    result = serve_mcp(
        capsys, "--fixture", "small_startup", "--seed", "7", "--now", "2024-03-05T12:00:00Z"
    )

    assert result.code == 0
    assert calls[0].reset_options == {
        "fixture": "small_startup",
        "seed": 7,
        "now": "2024-03-05T12:00:00Z",
    }


def test_each_variable_reaches_the_reset_options(
    calls: list[Call], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An MCP client configuration passes `env` more comfortably than `args`."""
    monkeypatch.setenv("SEAHAVEN_FIXTURE", "small_startup")
    monkeypatch.setenv("SEAHAVEN_SEED", "7")
    monkeypatch.setenv("SEAHAVEN_NOW", "2024-03-05T12:00:00Z")

    assert serve_mcp(capsys).code == 0
    assert calls[0].reset_options == {
        "fixture": "small_startup",
        "seed": 7,
        "now": "2024-03-05T12:00:00Z",
    }


def test_a_flag_beats_the_matching_variable_silently(
    calls: list[Call], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The flag is the more specific of the two, and a warning every launch is noise."""
    monkeypatch.setenv("SEAHAVEN_FIXTURE", "from_the_variable")
    monkeypatch.setenv("SEAHAVEN_SEED", "1")

    result = serve_mcp(capsys, "--fixture", "from_the_flag", "--seed", "2")

    assert calls[0].reset_options == {"fixture": "from_the_flag", "seed": 2}
    assert result.err == ""


def test_a_variable_set_to_nothing_is_not_a_value(
    calls: list[Call], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An `env` entry left as `""` in a client configuration means "not this one"."""
    for variable in VARIABLES:
        monkeypatch.setenv(variable, "")

    assert serve_mcp(capsys, "--fixture", "small_startup").code == 0
    assert calls[0].reset_options["fixture"] == "small_startup"


def test_reset_options_are_passed_whole(
    calls: list[Call], capsys: pytest.CaptureFixture[str]
) -> None:
    """The general door: a world's own startup keyword has no flag and needs none."""
    assert (
        serve_mcp(
            capsys, "--reset-options", '{"fixture": "small_startup", "seed": 7, "region": "eu"}'
        ).code
        == 0
    )
    assert calls[0].reset_options == {"fixture": "small_startup", "seed": 7, "region": "eu"}


def test_the_general_variable_is_passed_whole(
    calls: list[Call], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(GENERAL_VARIABLE, '{"now": "2024-03-05T12:00:00Z", "seed": 7}')

    assert serve_mcp(capsys).code == 0
    assert calls[0].reset_options == {"now": "2024-03-05T12:00:00Z", "seed": 7}


# --- the general door and the convenience flags do not mix ------------------


@pytest.mark.parametrize(
    ("argv", "environ", "expected"),
    [
        (
            ["--fixture", "small_startup", "--reset-options", "{}"],
            {},
            '--fixture cannot be combined with --reset-options; put "fixture" inside the '
            "--reset-options JSON instead: "
            """--reset-options '{"fixture": "small_startup", "seed": 7}'""",
        ),
        (
            ["--seed", "7"],
            {"SEAHAVEN_FIXTURE": "small_startup", "SEAHAVEN_RESET_OPTIONS": "{}"},
            "SEAHAVEN_FIXTURE and --seed cannot be combined with SEAHAVEN_RESET_OPTIONS; put "
            '"fixture" and "seed" inside the SEAHAVEN_RESET_OPTIONS JSON instead',
        ),
        (
            ["--now", "2024-03-05T12:00:00Z", "--reset-options", "{}"],
            {"SEAHAVEN_SEED": "7"},
            "SEAHAVEN_SEED and --now cannot be combined with --reset-options; put "
            '"seed" and "now" inside the --reset-options JSON instead: '
            """--reset-options '{"fixture": "small_startup", "seed": 7}'""",
        ),
    ],
    ids=["flag with flag", "variables and a flag", "a variable and a flag"],
)
def test_the_two_doors_are_refused_together_in_the_spelling_the_user_used(
    calls: list[Call],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    environ: dict[str, str],
    expected: str,
) -> None:
    """The refusal names every conflicting source actually given, and no other."""
    for variable, value in environ.items():
        monkeypatch.setenv(variable, value)

    result = serve_mcp(capsys, *argv)

    assert result.code == 1
    assert calls == [], "a refusal must happen before anything is served"
    assert result.err.strip() == expected


def test_the_refusal_names_all_three_conflicting_sources_in_order(
    calls: list[Call], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(GENERAL_VARIABLE, '{"seed": 1}')

    result = serve_mcp(
        capsys, "--now", "2024-03-05T12:00:00Z", "--seed", "7", "--fixture", "small_startup"
    )

    assert result.code == 1
    assert result.err.strip() == (
        "--fixture, --seed and --now cannot be combined with SEAHAVEN_RESET_OPTIONS; put "
        '"fixture", "seed" and "now" inside the SEAHAVEN_RESET_OPTIONS JSON instead'
    )


# --- what a value has to be -------------------------------------------------


def test_reset_options_that_are_not_json_are_refused(
    calls: list[Call], capsys: pytest.CaptureFixture[str]
) -> None:
    result = serve_mcp(capsys, "--reset-options", "{fixture: small_startup}")

    assert (result.code, calls) == (1, [])
    assert result.err.startswith("--reset-options is not valid JSON:")


@pytest.mark.parametrize(
    ("document", "named"), [("[1, 2]", "list"), ('"small_startup"', "str"), ("7", "int")]
)
def test_reset_options_that_are_not_an_object_are_refused(
    calls: list[Call], capsys: pytest.CaptureFixture[str], document: str, named: str
) -> None:
    """`world.instance(**options)` takes keyword arguments, so the document is an object."""
    result = serve_mcp(capsys, "--reset-options", document)

    assert (result.code, calls) == (1, [])
    assert result.err.startswith(f"--reset-options takes a JSON object, not {named}:")


def test_a_seed_that_is_not_an_integer_is_refused(
    calls: list[Call], capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit 1 and one line, not argparse's exit 2: this is the user's own to fix."""
    result = serve_mcp(capsys, "--seed", "the first one")

    assert (result.code, calls) == (1, [])
    assert result.err.strip() == "--seed takes an integer, not 'the first one'"


def test_a_seed_variable_that_is_not_an_integer_names_the_variable(
    calls: list[Call], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SEAHAVEN_SEED", "7.5")

    result = serve_mcp(capsys)

    assert (result.code, calls) == (1, [])
    assert result.err.strip() == "SEAHAVEN_SEED takes an integer, not '7.5'"


# --- the seed ---------------------------------------------------------------


def test_no_seed_is_a_random_seed_and_it_is_written_to_stderr(
    calls: list[Call], capsys: pytest.CaptureFixture[str]
) -> None:
    """Not `ids.DEFAULT_CALLER_SEED`: a relaunched client expects a world that moved on."""
    result = serve_mcp(capsys)

    assert result.code == 0
    assert isinstance(calls[0].seed, int)
    assert 0 <= calls[0].seed < SEED_CEILING
    assert result.err.strip() == SEED_LINE.format(seed=calls[0].seed)
    # stdout belongs to the protocol, from before the first frame is written.
    assert result.out == ""


def test_two_launches_without_a_seed_differ(
    calls: list[Call], capsys: pytest.CaptureFixture[str]
) -> None:
    serve_mcp(capsys)
    serve_mcp(capsys)

    assert calls[0].seed != calls[1].seed


@pytest.mark.parametrize(
    ("argv", "environ"),
    [
        (["--seed", "7"], {}),
        ([], {"SEAHAVEN_SEED": "7"}),
        (["--reset-options", '{"seed": 7}'], {}),
    ],
    ids=["flag", "variable", "inside the general door"],
)
def test_a_seed_that_was_given_is_used_as_given_and_not_announced(
    calls: list[Call],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    environ: dict[str, str],
) -> None:
    for variable, value in environ.items():
        monkeypatch.setenv(variable, value)

    result = serve_mcp(capsys, *argv)

    assert calls[0].seed == 7
    assert result.err == ""


def test_a_seed_key_inside_the_general_door_counts_as_given(
    calls: list[Call], capsys: pytest.CaptureFixture[str]
) -> None:
    """Whatever its value: `{"seed": null}` asks for the framework's own constant."""
    result = serve_mcp(capsys, "--reset-options", '{"seed": null}')

    assert calls[0].reset_options == {"seed": None}
    assert result.err == ""


def test_the_general_door_without_a_seed_still_gets_a_random_one(
    calls: list[Call], capsys: pytest.CaptureFixture[str]
) -> None:
    """A seed through any door, and no seed through any door is one of this command's."""
    result = serve_mcp(capsys, "--reset-options", '{"region": "eu"}')

    assert result.code == 0
    assert calls[0].reset_options["region"] == "eu"
    assert isinstance(calls[0].seed, int)
    assert 0 <= calls[0].seed < SEED_CEILING
    assert result.err.strip() == SEED_LINE.format(seed=calls[0].seed)


# --- the world, and the extra -----------------------------------------------


def test_the_world_option_is_resolved_by_the_server_and_not_here(
    calls: list[Call], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`serve` is handed a callable, so a failed import reaches the client.

    A process that died before the handshake gives an MCP client nothing but a
    broken pipe (`functional_spec.md` §5.2), so discovery is deferred to the
    handler that can answer it.
    """
    monkeypatch.syspath_prepend(str(WORLDS / "tidy" / "src"))
    monkeypatch.chdir(WORLDS / "messy")

    assert serve_mcp(capsys, "--world", "tidy:world").code == 0
    assert callable(calls[0].world)
    assert calls[0].world().name == "tidy"


def test_a_world_option_that_is_not_module_attr_is_refused_before_serving(
    calls: list[Call], capsys: pytest.CaptureFixture[str]
) -> None:
    """The one thing about `--world` that is answered before the protocol starts."""
    result = serve_mcp(capsys, "--world", "tidy")

    assert (result.code, calls) == (1, [])
    assert result.err.strip().startswith("--world takes module:attr, not 'tidy'")


def test_a_missing_mcp_extra_names_the_extra(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """One line, and the one thing to do about it."""
    # `None` in `sys.modules` is what Python treats as "this import is blocked".
    monkeypatch.setitem(sys.modules, "seahaven.mcp", None)

    result = serve_mcp(capsys)

    assert result.code == 1
    # One line: the extra is checked before the world and before the seed line,
    # because it is the blocker whatever else is wrong.
    assert result.err.splitlines() == [MISSING_EXTRA]
