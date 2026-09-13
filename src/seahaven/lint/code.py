"""The world-code rules: SH201, SH203 and SH205.

All three are warnings. A wall-clock read and a draw from `random` are nearly
always a mistake -- fixture-relative data dated from the machine's clock, a run
that will not replay -- but sometimes deliberate, and a rule an author has to
argue with is a rule an author turns off. An empty tool description is the same
shape of problem: the description is what an agent reads to decide whether to
call the tool, and a world under development has tools that do not have one yet.

The two call rules work on the dotted name of the expression being called, with
its root expanded through the module's own imports, so `datetime.now()`,
`datetime.datetime.now()`, `from datetime import datetime as dt; dt.now()` and
`from time import time; time()` are one rule and not four. It is also what keeps
the endorsed spellings clean: the root of `ctx.clock.now()` and of
`ctx.ids.random.random()` is `ctx`, which no import binds.
"""

import ast
import inspect
from collections.abc import Iterator
from pathlib import Path

from seahaven.lint import Finding, Target
from seahaven.tool import Tool

__all__ = ["run"]

# The directory whose modules are exempt from SH201: the error handler and its
# neighbours are the layer a world logs and times a call in, and that is a real
# wall clock doing a real job.
_MIDDLEWARE = "middleware"

# Every spelling of a wall-clock read, as the whole resolved dotted name. Whole
# and not a suffix: a suffix match reports `self.time.time()` and anything else
# whose last two segments happen to line up, which is the very thing resolving
# the root is supposed to prevent. Both the module-qualified form (`import
# datetime`) and the imported-name form (`from datetime import datetime`, which
# `_resolve` rewrites to `datetime.datetime`) are listed, because the alias map
# turns one into the other and either can arrive here.
_WALL_CLOCK = frozenset(
    {
        "datetime.now",
        "datetime.utcnow",
        "datetime.datetime.now",
        "datetime.datetime.utcnow",
        "date.today",
        "datetime.date.today",
        "time.time",
        "time.monotonic",
        "time.perf_counter",
    }
)

# `uuid.uuid4()` and `uuid.uuid1()` read the OS entropy pool, so a run that uses
# either does not replay. `import uuid` on its own is not a finding: `uuid.UUID`
# is how `ctx.ids.uuid()`'s own output is parsed.
_UUID = frozenset({"uuid.uuid4", "uuid.uuid1"})

_CLOCK_FIX = "take the instance's time from ctx.clock.iso() or ctx.clock.now()"
_IDS_FIX = "draw from ctx.ids: ctx.ids.uuid() for an identifier, ctx.ids.random for anything else"


def run(target: Target) -> list[Finding]:
    """SH201, SH203 and SH205 over every module in the world's package."""
    findings: list[Finding] = []
    package_dir = target.package_dir
    for path in sorted(package_dir.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except OSError, UnicodeDecodeError, SyntaxError:
            # A module that does not parse did not import either, and the import
            # failure is already SH501; two reports of one broken file is noise.
            continue
        findings += _module_findings(path, tree, in_middleware=_in_middleware(path, package_dir))
    findings += _description_findings(target)
    return findings


def _module_findings(path: Path, tree: ast.Module, *, in_middleware: bool) -> list[Finding]:
    aliases = _aliases(tree)
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom) and _imports_random(node):
            findings.append(
                Finding(
                    code="SH203",
                    severity="warning",
                    path=path,
                    line=node.lineno,
                    message="the random module does not replay",
                    fix=_IDS_FIX,
                )
            )
            continue
        if not isinstance(node, ast.Call):
            continue
        called = _resolve(_dotted(node.func), aliases)
        if called is None:
            continue
        if not in_middleware and called in _WALL_CLOCK:
            findings.append(
                Finding(
                    code="SH201",
                    severity="warning",
                    path=path,
                    line=node.lineno,
                    message=f"{called}() reads the wall clock, not the instance's",
                    fix=_CLOCK_FIX,
                )
            )
        if called.startswith("random.") or called in _UUID:
            findings.append(
                Finding(
                    code="SH203",
                    severity="warning",
                    path=path,
                    line=node.lineno,
                    message=f"{called}() does not replay",
                    fix=_IDS_FIX,
                )
            )
    return findings


def _description_findings(target: Target) -> list[Finding]:
    """SH205: a registered tool with nothing for an agent to read.

    Asked of the `World` rather than of the source, because a tool's description
    is its docstring *or* the `description=` it was registered with *or* whatever
    a factory put on the `Tool`, and only the registry knows which.
    """
    findings: list[Finding] = []
    for tool in target.world.tools.values():
        # The framework's own two. They are never listed and never reach an
        # agent, so a description is not what they are for.
        if tool.control or tool.description.strip():
            continue
        path, line = _source_of(tool, target.package_dir)
        findings.append(
            Finding(
                code="SH205",
                severity="warning",
                path=path,
                line=line,
                message=f"tool {tool.name!r} has an empty description",
                fix=(
                    "give the function a docstring, which becomes the whole description, or pass"
                    " description= to @world.tool"
                ),
            )
        )
    return findings


def _source_of(tool: Tool, fallback: Path) -> tuple[Path, int | None]:
    """Where a tool's function is written, as far as Python can say."""
    try:
        path = inspect.getsourcefile(tool.fn)
        if path is None:
            return fallback, None
        return Path(path), inspect.getsourcelines(tool.fn)[1]
    except OSError, TypeError:
        # A tool built from a callable with no source of its own: a factory's
        # closure, a partial, something defined in a REPL.
        return fallback, None


def _in_middleware(path: Path, package_dir: Path) -> bool:
    """Whether a module is part of the world's middleware layer."""
    parts = path.relative_to(package_dir).parts
    return _MIDDLEWARE in parts[:-1] or parts[-1] == f"{_MIDDLEWARE}.py"


def _imports_random(node: ast.Import | ast.ImportFrom) -> bool:
    """Whether a statement imports the standard library's `random`.

    `level == 0` for the same reason `_bindings` insists on it: `from .random
    import seeded` is the world's own `random.py`, which is a module a world is
    perfectly entitled to have and is not the module this rule is about.
    """
    if isinstance(node, ast.ImportFrom):
        return node.module == "random" and node.level == 0
    return any(alias.name == "random" or alias.name.startswith("random.") for alias in node.names)


def _dotted(node: ast.expr) -> str | None:
    """`a.b.c` for an attribute chain rooted in a plain name, else `None`.

    A call on anything else -- a subscript, a call's result, a literal -- is not
    a name these rules can reason about, and guessing is how a lint earns a
    reputation for crying wolf.
    """
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    return ".".join(reversed(parts))


def _resolve(dotted: str | None, aliases: dict[str, str]) -> str | None:
    """The dotted name with its root replaced by what the module imported it as."""
    if dotted is None:
        return None
    root, _, rest = dotted.partition(".")
    target = aliases.get(root)
    if target is None:
        return dotted
    return f"{target}.{rest}" if rest else target


def _aliases(tree: ast.Module) -> dict[str, str]:
    """Every name the module's imports bind, mapped to what it names.

    `import datetime as dtm` gives `dtm -> datetime`; `from datetime import
    datetime` gives `datetime -> datetime.datetime`. Only module-level and
    function-level `import` statements exist to find, and `ast.walk` gets both.
    """
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        for name, full in _bindings(node):
            aliases[name] = full
    return aliases


def _bindings(node: ast.AST) -> Iterator[tuple[str, str]]:
    match node:
        case ast.Import():
            # Only the renames. `import datetime` and `import a.b` bind a name
            # that already spells what it means, so resolving them would map a
            # name to itself.
            for alias in node.names:
                if alias.asname:
                    yield alias.asname, alias.name
        case ast.ImportFrom(module=str(module), level=0):
            for alias in node.names:
                yield alias.asname or alias.name, f"{module}.{alias.name}"
        case _:
            return
