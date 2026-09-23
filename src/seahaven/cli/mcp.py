"""`seahaven mcp`: one world behind an MCP server on stdio.

Argument parsing and nothing else. `seahaven/mcp/` is the server, and it lives
there so a harness that wants one inside its own process can call it without
going through `argv`. Nothing in this module imports the MCP SDK, so the module
imports -- and is tested -- whether or not the `mcp` extra is installed.

**Every option has an environment variable except `--world`.** MCP client
configurations pass `env` more comfortably than `args`, and `--world` names the
code to import, which belongs beside the command in the configuration file where
a reader of that file can see it.

**The general door and the convenience flags do not mix.** `--reset-options` is
the whole of what `world.instance()` is called with; `--fixture`, `--seed`,
`--now` and `--clock-mode` are convenience spellings of four of its keys, because
JSON inside an `.mcp.json` args array is painful to quote. Combining the two is
refused rather than merged: a user who has to reason about which fixture wins
has already lost.

**A world's own startup keywords go inside `"startup"`,** which is the namespace
`world.instance()` gives them. A key the signature does not name is refused
here, on the command line, rather than reaching the client as a `TypeError`
after the process has started. `control_tools` is the one keyword the signature
does name and this command withholds, because an MCP client is what is being
kept away from arbitrary SQL.
"""

import argparse
import inspect
import json
import os
import random
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from seahaven.cli import CliError, add_world_option, check_world_option, find_world
from seahaven.world import World

__all__ = [
    "MISSING_EXTRA",
    "RESET_OPTION_KEYS",
    "WITHHELD",
    "Options",
    "add_parser",
    "resolve_options",
    "run",
]

MISSING_EXTRA = 'seahaven mcp needs the mcp extra: pip install "seahaven[mcp]"'

# The keyword arguments of `world.instance()` that are common enough to have a
# flag of their own, and the variable each one answers to. `state_format` is not
# here: the state document is not published over MCP, so the format it would be
# rendered in has no reader (`functional_spec.md` §2.1).
CONVENIENCE = {
    "fixture": "SEAHAVEN_FIXTURE",
    "seed": "SEAHAVEN_SEED",
    "now": "SEAHAVEN_NOW",
    "clock_mode": "SEAHAVEN_CLOCK_MODE",
}

# The general door: a JSON object passed to `world.instance()` whole, which is
# how a world's own startup keywords are reached, inside `"startup"`.
GENERAL_FLAG = "--reset-options"
GENERAL_VARIABLE = "SEAHAVEN_RESET_OPTIONS"

# `seahaven mcp` publishes the world's own tools and nothing else
# (`functional_spec.md` §1), so this one keyword argument of `world.instance()`
# is not offered. `seahaven serve` has `--include-control-tools` because a
# harness drives the server it started; an MCP client is the thing being kept
# away from arbitrary SQL, and a general door is not the place to hand it over.
WITHHELD = "control_tools"

# The keys a `--reset-options` object may name. Read off the signature of
# `world.instance()` rather than written out, so a keyword argument the
# framework adds is one this command takes without an edit here.
# `tests/test_cli_mcp.py` pins the five names, so a parameter added to
# `world.instance()` fails a test rather than reaching an MCP client unreviewed.
RESET_OPTION_KEYS = frozenset(
    name
    for name, parameter in inspect.signature(World.instance).parameters.items()
    if name not in {"self", WITHHELD}
    and parameter.kind in {parameter.POSITIONAL_OR_KEYWORD, parameter.KEYWORD_ONLY}
)

# The seed this command picks when the user gave none, drawn below this. A signed
# 32-bit range, which is wide enough that two launches never collide and narrow
# enough to read off a terminal and type back after `--seed`.
SEED_CEILING = 2**31

# What a seed nobody asked for is announced with. It goes to stderr and nowhere
# else: the client never learns it (`functional_spec.md` §1 and §4).
SEED_LINE = "no seed was given, so this run uses --seed {seed}"

_EXAMPLE = """--reset-options '{"fixture": "small_startup", "seed": 7}'"""
_STARTUP_EXAMPLE = (
    """--reset-options '{"fixture": "small_startup", "startup": {"user_id": "u_12"}}'"""
)


@dataclass(frozen=True)
class Options:
    """What `world.instance()` will be called with, and where the seed came from.

    `architecture.md` §2 typed this as the keyword dictionary alone. The answer
    carries one more fact, because only a seed *this command* picked is written
    to stderr (`functional_spec.md` §4), and asking the sources a second time in
    `run` would be the resolution rules written twice.
    """

    reset_options: dict[str, Any]
    # The seed this command chose, or `None` when the user gave one by any door.
    random_seed: int | None


def add_parser(subcommands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subcommands.add_parser(
        "mcp",
        help="serve this world to an MCP client over stdio",
        description="Serve this world to one MCP client over stdio: one world, one instance.",
    )
    add_world_option(parser)
    parser.add_argument(
        "--fixture",
        metavar="NAME",
        default=None,
        help=f"the fixture the instance starts from (${CONVENIENCE['fixture']})",
    )
    # A string, not `type=int`: a seed that is not an integer is one line on
    # stderr and exit 1 like every other refusal here, and argparse's own
    # conversion would make it a usage error and exit 2.
    parser.add_argument(
        "--seed",
        metavar="N",
        default=None,
        help=(
            f"the caller seed, an integer (${CONVENIENCE['seed']}); "
            "the default is a random seed, written to stderr"
        ),
    )
    parser.add_argument(
        "--now",
        metavar="ISO",
        default=None,
        help=(
            f"the clock a blank instance starts at (${CONVENIENCE['now']}); "
            "not allowed with a fixture, which carries its own"
        ),
    )
    # A string, not `choices=`: an unknown mode is `world.instance()`'s one-line
    # refusal and exit 1, like every other refusal here, not an argparse usage error.
    parser.add_argument(
        "--clock-mode",
        metavar="MODE",
        default=None,
        help=(
            "how the instance's clock moves: fixed, tick, running or wall "
            f"(${CONVENIENCE['clock_mode']}); the default is the world's"
        ),
    )
    parser.add_argument(
        GENERAL_FLAG,
        metavar="JSON",
        default=None,
        help=(
            f"a JSON object passed to world.instance() whole (${GENERAL_VARIABLE}); "
            "not allowed with --fixture, --seed, --now or --clock-mode"
        ),
    )
    parser.set_defaults(handler=run)


def run(args: argparse.Namespace) -> int:
    """Resolve the options and hand the world to `seahaven.mcp.serve`.

    The world itself is resolved later, inside the server, where a failed import
    reaches the client as a JSON-RPC error instead of as a broken pipe
    (`functional_spec.md` §5.2). Only the *spelling* of `--world` is asked about
    here, because a user who misspells it is looking at their own command line
    and is owed the answer before anything is served.
    """
    options = resolve_options(args, os.environ)
    check_world_option(args.world)
    try:
        # The extra is checked before the world is: an SDK that is not installed
        # is the blocker whatever else is wrong, and "install the extra" is the
        # only thing to do about it. The import is here and not at module top so
        # that this module -- and its tests -- work in an environment that has
        # `seahaven[serve]` instead, which cannot hold both extras at once.
        from seahaven.mcp import serve
    except ImportError as error:
        raise CliError(MISSING_EXTRA) from error
    if options.random_seed is not None:
        print(SEED_LINE.format(seed=options.random_seed), file=sys.stderr)
    return serve(lambda: find_world(args.world), reset_options=options.reset_options)


def resolve_options(args: argparse.Namespace, environ: Mapping[str, str]) -> Options:
    """The keyword arguments `world.instance()` is called with, and the refusals.

    A pure function of the namespace and an environment mapping: no world, no
    filesystem, no SDK, so the whole of `functional_spec.md` §2.2 to §2.4 is
    covered by tests that start no server.
    """
    convenience = {
        name: given
        for name, variable in CONVENIENCE.items()
        if (given := _source(_flag(name), variable, getattr(args, name), environ)) is not None
    }
    general = _source(GENERAL_FLAG, GENERAL_VARIABLE, args.reset_options, environ)
    if general is not None and convenience:
        raise CliError(_mixing_refusal(convenience, general))
    if general is not None:
        return _with_a_seed(_object(general))
    reset_options: dict[str, Any] = {
        name: _seed(given) if name == "seed" else given.value for name, given in convenience.items()
    }
    return _with_a_seed(reset_options)


def _flag(name: str) -> str:
    """The command-line spelling of a convenience keyword: `clock_mode` is `--clock-mode`."""
    return f"--{name.replace('_', '-')}"


@dataclass(frozen=True)
class _Given:
    """One option's value, and the spelling the user reached it by.

    The spelling is carried because every refusal names the sources the user
    actually used: a message about `--fixture` is no help to somebody who set
    `SEAHAVEN_FIXTURE` in a client configuration months ago.
    """

    spelling: str
    value: str


def _source(
    spelling: str, variable: str, flag: str | None, environ: Mapping[str, str]
) -> _Given | None:
    """The flag, else the variable, else nothing.

    A flag beats the matching variable silently (`functional_spec.md` §2.2): the
    flag is the more specific of the two, and a warning on every launch about a
    variable the user set once is noise.

    A variable that is set to nothing is not a value. An MCP client
    configuration is a JSON file that people copy and edit, and an `env` entry
    left as `""` there means "not this one" -- not a fixture with no name, a
    seed that is not an integer, or a `--reset-options` document that is not
    JSON.
    """
    if flag is not None:
        return _Given(spelling, flag)
    value = environ.get(variable, "").strip()
    return _Given(variable, value) if value else None


def _mixing_refusal(convenience: dict[str, _Given], general: _Given) -> str:
    """The message of `functional_spec.md` §2.3, naming what the user actually gave.

    The example on the end is the flag's spelling, so it is offered when the
    general source was the flag and left off when it was the variable.
    """
    order = [name for name in CONVENIENCE if name in convenience]
    sources = _english([convenience[name].spelling for name in order])
    keys = _english([f'"{name}"' for name in order])
    message = (
        f"{sources} cannot be combined with {general.spelling}; put {keys} inside the "
        f"{general.spelling} JSON instead"
    )
    return f"{message}: {_EXAMPLE}" if general.spelling == GENERAL_FLAG else message


def _english(items: list[str]) -> str:
    """`a`, `a and b`, `a, b and c`: a list as a sentence names it."""
    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])} and {items[-1]}"


def _object(given: _Given) -> dict[str, Any]:
    """A `--reset-options` document, which has to be a JSON object `world.instance()` takes."""
    try:
        document = json.loads(given.value)
    except json.JSONDecodeError as error:
        raise CliError(f"{given.spelling} is not valid JSON: {error}") from error
    if not isinstance(document, dict):
        raise CliError(
            f"{given.spelling} takes a JSON object, not {type(document).__name__}: "
            f"{_EXAMPLE} names the keyword arguments world.instance() is called with"
        )
    _check_keys(given, document)
    return document


def _check_keys(given: _Given, document: dict[str, Any]) -> None:
    """Refuse the withheld keyword, and a key `world.instance()` does not take.

    The check is on the command line and not at instance creation, where
    `world.instance()` answers an unknown keyword with a `TypeError` once the
    client has already connected. A user who misspells a key is looking at their
    own command line and is owed the answer before anything is served.

    `"startup"` is named in the message because a world's own keyword is the key
    most likely to be written at the top level: startup keywords sat there until
    they were given a namespace of their own.
    """
    if WITHHELD in document:
        raise CliError(
            f'{given.spelling} does not take "{WITHHELD}": seahaven mcp publishes the world\'s '
            "own tools and nothing else, and nothing reaching an MCP client may run SQL "
            "against the world"
        )
    unknown = sorted(set(document) - RESET_OPTION_KEYS)
    if not unknown:
        return
    named = _english([f'"{key}"' for key in unknown])
    takes = _english([f'"{key}"' for key in sorted(RESET_OPTION_KEYS)])
    raise CliError(
        f"{given.spelling} does not take {named}; {given.spelling} takes {takes}, and a "
        f'world\'s own startup keywords go inside "startup": {_STARTUP_EXAMPLE}'
    )


def _seed(given: _Given) -> int:
    try:
        return int(given.value)
    except ValueError as error:
        raise CliError(f"{given.spelling} takes an integer, not {given.value!r}") from error


def _with_a_seed(reset_options: dict[str, Any]) -> Options:
    """The answer, with a seed of this command's own when nobody named one.

    This is deliberately *not* the framework's default. Omitting `seed=` from
    `world.instance()` gives `ids.DEFAULT_CALLER_SEED`, a constant, so the same
    fixture replays the same ids on every launch. That is right for a test and
    wrong here: a user relaunches their MCP client all day and expects a world
    that moved on, not one that reset to the same ids (`functional_spec.md` §4).
    The clock is not part of this either way: it starts at the fixture's own
    `now`, or wall time for a blank instance, and runs in the world's default
    mode unless one is given. No seed moves it.

    A `"seed"` key inside a `--reset-options` object counts as given, whatever
    its value, so a caller who deliberately passes `{"seed": null}` gets the
    framework's constant back.
    """
    if "seed" in reset_options:
        return Options(reset_options=reset_options, random_seed=None)
    seed = random.randrange(SEED_CEILING)
    return Options(reset_options={**reset_options, "seed": seed}, random_seed=seed)
