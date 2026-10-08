"""`seahaven`: the command, its parser, and how it finds a world.

Seven subcommands, one module each, and nothing in any of them that is not
argument parsing, world discovery or output. The work is done by code that
already exists -- `World.instance`, `Instance.freeze`, `seahaven.openenv.serve`,
`seahaven.lint` -- which is what keeps the CLI from becoming a second API beside
the Python one.

**Finding the world** is the convention of functional spec §2.2 and nothing more:
the nearest `pyproject.toml` walking up, its `[project] name` normalised to a
package name, and the attribute `world` on that package. `--world module:attr`
overrides it for a layout the convention does not fit. A world that has not been
installed still resolves, because the project root and its `src/` are put on
`sys.path` first; the docs recommend `uv sync` and an editable install rather
than relying on that.

**Failure** is one line on stderr and exit 1, never a traceback: every error a
user can provoke here says what to do about it, and a traceback out of
`import_module` says only that Python was involved. Exit 2 is argparse's, for a
usage error.

There is one deliberate exception, and it is not a user error: an exception out
of the generator `seahaven fixture --run` names is the author's own code failing,
and its traceback points at the line. `cli/fixture.py` says why; phase 7's plan
records it as a departure from the component spec, which asked for a message.
"""

import argparse
import re
import sys
import tomllib
import traceback
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from types import ModuleType

from seahaven.errors import SeahavenError
from seahaven.world import DDL_DOES_NOT_EXECUTE, World

__all__ = [
    "CliError",
    "Discovery",
    "build_parser",
    "discover",
    "find_world",
    "main",
    "package_name",
    "project_name",
    "project_root",
]

PROJECT_FILE = "pyproject.toml"
SOURCE_DIRNAME = "src"
WORLD_ATTRIBUTE = "world"

# `my-world` and `my.world` are both the package `my_world`: PEP 503-ish
# normalisation, which is what an installer would have done to the same name.
_NON_PACKAGE = re.compile(r"[-.]+")

_OVERRIDE = "or pass --world module:attr"

# Seahaven's own package: its frames are never where a world's import failed.
_SEAHAVEN_DIR = Path(__file__).resolve().parents[1]


class CliError(Exception):
    """A failure with a fix in it, printed as one line and never as a traceback.

    `code` is set when `seahaven check` has to render the failure as a finding
    rather than as a user error: an import that never produced a `World` is
    SH501, and one that failed on the world's DDL is SH104. A `CliError` without
    a code -- no `pyproject.toml`, a malformed `--world` -- is the user's to fix
    before any lint can run at all.

    `path` and `line` place a finding, `fix` is the edit `check` prints beside
    it, and `root` is the project `check` shows the path relative to: the same
    as `path` unless `path` names a file inside it.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        path: Path | None = None,
        line: int | None = None,
        fix: str | None = None,
        root: Path | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.path = path
        self.line = line
        self.fix = fix
        self.root = root or path


@dataclass(frozen=True)
class Discovery:
    """A world and everything the tooling knows about where it came from."""

    world: World
    package: ModuleType
    # `sys.modules` immediately after the package was imported: what SH301 has to
    # compare against, and it cannot be recovered later.
    imported: frozenset[str]
    root: Path


def main(argv: list[str] | None = None) -> int:
    """The entry point `[project.scripts] seahaven` names."""
    args = build_parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except (CliError, SeahavenError) as error:
        print(str(error), file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    """The whole command tree. Each subcommand module owns its own arguments.

    Every subcommand's parser carries its function as the default `handler`, so
    no subcommand may take an option spelled `--handler`: argparse would write
    the option's value over it. `--run` is why the name is not `run`.
    """
    # Imported here rather than at module top: every subcommand module imports
    # this one for `CliError` and `find_world`.
    from seahaven.cli import check, docs, fixture, hub, mcp, new, serve

    parser = argparse.ArgumentParser(prog="seahaven", description="Build and run Seahaven worlds.")
    subcommands = parser.add_subparsers(dest="command", required=True, metavar="<command>")
    for module in (new, hub, check, docs, fixture, serve, mcp):
        module.add_parser(subcommands)
    return parser


def add_world_option(parser: argparse.ArgumentParser) -> None:
    """`--world module:attr`, on every subcommand that needs a world."""
    parser.add_argument(
        "--world",
        metavar="module:attr",
        default=None,
        help=(
            "the world to act on, as an importable module and the attribute holding it; "
            "the default is the convention: the project's package, attribute 'world'"
        ),
    )


def check_world_option(explicit: str | None) -> None:
    """Refuse a `--world` that is not `module:attr`, without importing anything.

    `discover` asks the same question on its way to the import, and every
    subcommand but one lets it. `seahaven mcp` resolves its world late, inside
    the server, so that a failed import reaches the MCP client as an error
    rather than as a broken pipe -- and a misspelled option is not that: the
    user is looking at their own command line and is owed the answer before
    anything is served (MCP functional spec §2.4).
    """
    if explicit is not None:
        _split_world_option(explicit)


def _split_world_option(explicit: str) -> tuple[str, str]:
    """`module:attr` in two parts, or the refusal that names the spelling."""
    module_name, separator, attribute = explicit.partition(":")
    if not separator or not module_name or not attribute:
        raise CliError(
            f"--world takes module:attr, not {explicit!r}; for example --world myworld:world"
        )
    return module_name, attribute


def find_world(explicit: str | None, start: Path | None = None) -> World:
    """The world `seahaven` and the pytest plugin act on."""
    return discover(explicit, start).world


def discover(explicit: str | None, start: Path | None = None) -> Discovery:
    """`find_world`, plus the module, the project root and the import snapshot."""
    start = (start or Path.cwd()).resolve()
    if explicit is not None:
        module_name, attribute = _split_world_option(explicit)
        _make_importable(start, start / SOURCE_DIRNAME)
        return _import(module_name, attribute, root=start)
    root = project_root(start)
    _make_importable(root, root / SOURCE_DIRNAME)
    return _import(package_name(project_name(root)), WORLD_ATTRIBUTE, root=root)


def project_root(start: Path, *, remedy: str = _OVERRIDE) -> Path:
    """The nearest directory at or above `start` holding a `pyproject.toml`.

    `remedy` ends the refusal, for a command that has no `--world` to offer.
    """
    for directory in (start, *start.parents):
        if (directory / PROJECT_FILE).is_file():
            return directory
    raise CliError(
        f"no {PROJECT_FILE} in {start} or any directory above it, so there is no world here; "
        f"run seahaven from inside a world's project, {remedy}"
    )


def project_name(root: Path, *, remedy: str = _OVERRIDE) -> str:
    """The `[project] name` of the project at `root`, as written."""
    path = root / PROJECT_FILE
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise CliError(f"{path}: cannot be read as TOML: {error}") from error
    name = data.get("project", {}).get("name")
    if not isinstance(name, str) or not name:
        raise CliError(f"{path} has no [project] name, so there is no package to import; {remedy}")
    return name


def package_name(name: str) -> str:
    """The package name a distribution name normalises to: `my-world` is `my_world`."""
    return _NON_PACKAGE.sub("_", name).lower()


def _import(module_name: str, attribute: str, *, root: Path) -> Discovery:
    """Import a module, take the world off it, and say what went wrong if not."""
    try:
        module = import_module(module_name)
    except SeahavenError as error:
        # A world that refused to be constructed. Its message is written for its
        # author: SH104 when the DDL is what SQLite refused, SH501, placed at the
        # line of world code that made the call, for everything else a `World`
        # raises at import -- a duplicate tool name, a middleware of the wrong shape.
        if DDL_DOES_NOT_EXECUTE in str(error):
            raise CliError(str(error), code="SH104", path=root) from error
        raise _placed(str(error), module_name, error, root) from error
    except Exception as error:
        raise _import_failure(module_name, error, root) from error
    imported = frozenset(sys.modules)
    world = getattr(module, attribute, None)
    if not isinstance(world, World):
        if _is_the_project_roots_init(module, root):
            raise CliError(
                _root_init_imported(module_name, root),
                code="SH501",
                path=root,
                fix=(
                    "delete tests/__init__.py, move conftest.py into tests/, or rename the "
                    "project directory"
                ),
            )
        raise CliError(
            _no_world(module_name, attribute, world),
            code="SH501",
            path=root,
            fix="give the package a world, or point at the one it has",
        )
    package = _package_of(module)
    if package is None:
        # Functional spec §2.1: a world is a package, never a single module and
        # never a directory loaded by path. Said here, where the layout is what
        # the user chose, rather than later as whatever the first rule to read a
        # directory happens to raise.
        raise CliError(
            f"{module_name} is a module, not a package, and a world is a package: move it to "
            f"{module_name}/__init__.py, with world.py, schema/, tools/ and middleware/ beside it"
        )
    return Discovery(world=world, package=package, imported=imported, root=root)


def _package_of(module: ModuleType) -> ModuleType | None:
    """The package the lints walk: `module`, or the package it lives in.

    `--world mypkg.world:world` names the module the `World` is built in, which
    is the natural way to spell the override for a world in the standard layout.
    What the lints need is the package around it -- the directory holding
    `schema/`, `tools/` and `middleware/` -- so a module is climbed out of rather
    than refused. Only a world that is one top-level module has nowhere to climb
    to, and that is the layout §2.1 rules out.
    """
    while not getattr(module, "__path__", None):
        parent = module.__name__.rpartition(".")[0]
        found = sys.modules.get(parent) if parent else None
        if found is None:
            return None
        module = found
    return module


def _no_world(module_name: str, attribute: str, found: object) -> str:
    what = "has no" if found is None else f"has a {type(found).__name__} and not a World for its"
    return (
        f"{module_name} {what} {attribute!r}; "
        f"{module_name}/__init__.py must export {attribute} = seahaven.World(...), {_OVERRIDE}"
    )


def _is_the_project_roots_init(module: ModuleType, root: Path) -> bool:
    """Whether `module` is the `__init__.py` `seahaven hub` writes at the project root."""
    file = getattr(module, "__file__", None)
    return file is not None and Path(file).resolve() == root / "__init__.py"


def _root_init_imported(module_name: str, root: Path) -> str:
    # pytest names a module after every package above it: a test module in a
    # `tests/` that has an `__init__.py`, and a `conftest.py` beside the root's
    # `__init__.py`, both import that `__init__.py` as `module_name`.
    return (
        f"{module_name} was imported from {root / '__init__.py'} and not from "
        f"{SOURCE_DIRNAME}/{module_name}/; under pytest a tests/__init__.py or a conftest.py "
        f"beside that file does this: delete tests/__init__.py, move conftest.py into tests/, "
        f"or rename the directory {root.name} so it is not the package's name"
    )


def _import_failure(module_name: str, error: Exception, root: Path) -> CliError:
    """SH501 for an import that raised, placed at the line of the world's code that raised it."""
    summary = _last_traceback_line(error)
    if isinstance(error, ModuleNotFoundError) and _is_the_world_module(module_name, error.name):
        return CliError(
            f"cannot import {module_name!r}: {summary}",
            code="SH501",
            path=root,
            fix=(
                f"make {module_name!r} importable (the package the [project] name names, under "
                f"{SOURCE_DIRNAME}/), or point at the world with --world module:attr"
            ),
        )
    return _placed(f"cannot import {module_name!r}: {summary}", module_name, error, root)


def _placed(message: str, module_name: str, error: Exception, root: Path) -> CliError:
    """SH501 at the innermost frame of the world's own code, or with the command that finds it."""
    command = f"python -c 'import {module_name}'"
    place = _world_frame(error, root)
    if place is None:
        return CliError(
            f"{message}; {command} prints the whole traceback",
            code="SH501",
            path=root,
            fix=f"fix the error {command} ends in",
        )
    file, line = place
    where = f"{_shown(file, root)}:{line}"
    return CliError(
        f"{message}, at {where}; {command} prints the whole traceback",
        code="SH501",
        path=file,
        line=line,
        fix=f"fix the error at {where}",
        root=root,
    )


def _world_frame(error: Exception, root: Path) -> tuple[Path, int] | None:
    """The file and line in the world's own code that raised, preferring the project's own files.

    A `SyntaxError` is raised by the compiler, not by a frame of the file it is
    in, so its place comes off the exception.
    """
    if isinstance(error, SyntaxError) and error.filename and error.lineno:
        path = Path(error.filename)
        if path.is_file():
            return path.resolve(), error.lineno
    places = [
        (Path(frame.filename).resolve(), frame.lineno)
        for frame in reversed(traceback.extract_tb(error.__traceback__))
        if frame.lineno is not None and _authored(Path(frame.filename))
    ]
    in_project = [place for place in places if place[0].is_relative_to(root)]
    return next(iter(in_project or places), None)


def _authored(path: Path) -> bool:
    """Whether a traceback frame is in code an author wrote, not in Python, a library or Seahaven.

    `sys.prefix` holds a project's own virtual environment, and `sys.base_prefix`
    the standard library. An `importlib` frame names a file that does not exist.
    """
    if not path.is_file():
        return False
    resolved = path.resolve()
    outside = (_SEAHAVEN_DIR, Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve())
    return not any(resolved.is_relative_to(directory) for directory in outside)


def _is_the_world_module(module_name: str, missing: str | None) -> bool:
    """Whether the module that was not found is the one named, or a package above it."""
    return missing is not None and (missing == module_name or module_name.startswith(f"{missing}."))


def _shown(path: Path, root: Path) -> str:
    """`path` relative to the project when it is inside it, as `check` prints paths."""
    return str(path.relative_to(root)) if path.is_relative_to(root) else str(path)


def _last_traceback_line(error: BaseException) -> str:
    """The exception as Python would print it, without the frames above it."""
    return traceback.format_exception_only(error)[-1].strip()


def _make_importable(*directories: Path) -> None:
    """Put a world's project on `sys.path`, so an uninstalled one still imports."""
    for directory in directories:
        entry = str(directory)
        if directory.is_dir() and entry not in sys.path:
            sys.path.insert(0, entry)
