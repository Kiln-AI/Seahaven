"""The `World`: a name, a schema, and everything registered against them.

One `World` per world package, built at import time in the package's `world.py`,
and the object every tool module imports to register against. It owns the tool
registry, the middleware list and the startup hooks, and it is where a mistake in
any of them is found: registration validates immediately and fails with a
`WorldBug` naming what is wrong.

Registration is open for the life of the world. The registry and the middleware
chain are read at call time, so a tool or a middleware registered after instances
exist applies to them from their next call. Import-time registration is the
convention the scaffold encourages, not a rule enforced here; registering while
calls are in flight is unsupported.
"""

import hashlib
import inspect
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

import apsw

from seahaven.call import Handler, Middleware, build_chain, invoke
from seahaven.ctx import Ctx
from seahaven.db import build_blank
from seahaven.errors import WorldBug
from seahaven.tool import Tool

__all__ = [
    "CONTROL_TOOL_NAMES",
    "RESERVED_TOOL_NAMES",
    "Handler",
    "Middleware",
    "RegisteredStartupHook",
    "StartupHook",
    "World",
]

# `Handler` and `Middleware` are defined where the chain is built, in `call.py`,
# and re-exported here: `components/world_and_dispatch.md` §1 lists all three
# aliases with the `World` they describe, and this is where a world author looks
# for the shape its middleware has to have.
type StartupHook = Callable[..., None]

# Reserved by OpenEnv: `reset`, `step`, `state` and `close` are the environment's
# own verbs, and a tool by one of those names could not be called over the wire.
RESERVED_TOOL_NAMES = frozenset({"close", "reset", "state", "step"})

# The framework's own tools (`control.py`). They are registered on every world,
# bypass the chain and are never listed; a world registering either name is
# refused whether or not they are registered yet.
CONTROL_TOOL_NAMES = frozenset({"controller_changes", "controller_run_sql"})

# `reset`'s own arguments, which a startup hook therefore cannot take.
RESET_ARGUMENTS = frozenset({"fixture", "now", "seed"})

FIXTURES_DIRNAME = "fixtures"

_POSITIONAL = (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class RegisteredStartupHook:
    """A startup hook and the keyword arguments it accepts.

    Callable, so `world.startup_hooks` is a sequence of hooks rather than a
    sequence of records about them; `accepts` and `takes_var_kwargs` are what
    instance creation reads to give each hook the `reset` arguments it asked for
    and no others.
    """

    fn: StartupHook
    accepts: frozenset[str]
    takes_var_kwargs: bool

    def __call__(self, ctx: Ctx, **kwargs: Any) -> None:
        self.fn(ctx, **kwargs)


class World:
    """A world: its identity, its schema, and what is registered against it."""

    def __init__(
        self,
        name: str,
        version: str,
        schema: str,
        *,
        fixtures_dir: Path | str | None = None,
        work_dir: Path | str | None = None,
        untracked_tables: Sequence[str] = (),
    ) -> None:
        self.name = name
        self.version = version
        self.schema = schema
        self.schema_hash = _schema_hash(schema)
        _prove_the_ddl_executes(name, schema)
        self.fixtures_dir = (
            Path(fixtures_dir)
            if fixtures_dir is not None
            # The module that called `World(...)`, which is where the world
            # package is, and the only thing the derivation has to go on.
            else _derive_fixtures_dir(sys._getframe(1).f_globals.get("__file__"))
        )
        # `None` means the default: a per-process directory under the system
        # temporary directory, resolved when the first instance is made. A
        # directory given here is the caller's and is never swept.
        self.work_dir = Path(work_dir) if work_dir is not None else None
        self.untracked_tables = tuple(untracked_tables)
        self._tools: dict[str, Tool] = {}
        self._middlewares: list[Middleware] = []
        self._startup_hooks: list[RegisteredStartupHook] = []
        self.chain: Handler = build_chain((), invoke)

    @property
    def tools(self) -> Mapping[str, Tool]:
        """The registry, in registration order. Read-only: register through `tool`."""
        return MappingProxyType(self._tools)

    @property
    def middlewares(self) -> Sequence[Middleware]:
        """The middleware, outermost first."""
        return tuple(self._middlewares)

    @property
    def startup_hooks(self) -> Sequence[RegisteredStartupHook]:
        """The startup hooks, in registration order."""
        return tuple(self._startup_hooks)

    @property
    def accepted_startup_kwargs(self) -> frozenset[str]:
        """Every keyword argument some startup hook names.

        A hook taking `**kwargs` accepts anything, and instance creation checks
        `takes_var_kwargs` for that; this set is the named ones only.
        """
        return frozenset().union(*(hook.accepts for hook in self._startup_hooks))

    def tool(
        self,
        obj: Callable[..., Any] | Tool | None = None,
        /,
        *,
        name: str | None = None,
        description: str | None = None,
        transaction: bool | None = None,
    ) -> Any:
        """Register a tool, as a decorator or as a call.

        `transaction` defaults to `True`; it is spelled `None` here so that a
        `Tool` from a factory, which decided its own, can tell an option that was
        passed from one that was not.
        """
        if obj is None:
            return lambda fn: self._register_tool(
                fn, name=name, description=description, transaction=transaction
            )
        return self._register_tool(obj, name=name, description=description, transaction=transaction)

    def middleware(self, obj: Middleware | None = None, /) -> Any:
        """Register a middleware, as a decorator or as a call. Order is outermost first."""
        if obj is None:
            return self._register_middleware
        return self._register_middleware(obj)

    def instance_startup(self, obj: StartupHook | None = None, /) -> Any:
        """Register a hook run once per instance, before its first call."""
        if obj is None:
            return self._register_startup_hook
        return self._register_startup_hook(obj)

    def _register_tool(
        self,
        obj: Callable[..., Any] | Tool,
        *,
        name: str | None,
        description: str | None,
        transaction: bool | None,
    ) -> Any:
        if isinstance(obj, Tool):
            if name is not None or description is not None or transaction is not None:
                # The factory built the schema and the argument model from the
                # options it was given; replacing one here would leave the tool
                # disagreeing with its own schema.
                raise WorldBug(
                    f"tool {obj.name!r} was built by a factory: pass name=, description= or "
                    f"transaction= to the factory, not to world.tool()"
                )
            self._add(obj)
            return obj
        if not callable(obj):
            raise WorldBug(f"a tool is a function or a Tool, not {type(obj).__name__}")
        self._add(
            Tool.from_function(
                obj,
                name=name,
                description=description,
                transaction=True if transaction is None else transaction,
            )
        )
        # The function itself, so a decorated tool stays an ordinary callable.
        return obj

    def _add(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise WorldBug(f"tool {tool.name!r} is registered twice")
        # OpenEnv's verbs are refused whoever is registering: they are the wire's,
        # and no flag of this framework's can reclaim them.
        if tool.name in RESERVED_TOOL_NAMES:
            raise WorldBug(
                f"tool {tool.name!r} uses a name OpenEnv reserves for the environment "
                f"({', '.join(sorted(RESERVED_TOOL_NAMES))})"
            )
        if tool.name in CONTROL_TOOL_NAMES and not tool.control:
            raise WorldBug(f"tool {tool.name!r} uses the name of a control tool")
        self._tools[tool.name] = tool

    def _register_middleware(self, obj: Middleware) -> Middleware:
        _check_middleware_shape(obj)
        self._middlewares.append(obj)
        # Rebuilt rather than walked per call: the chain is a closure over the
        # middleware it had when it was built, and instances read it at call time.
        self.chain = build_chain(self._middlewares, invoke)
        return obj

    def _register_startup_hook(self, obj: StartupHook) -> StartupHook:
        self._startup_hooks.append(_as_startup_hook(obj))
        return obj


def _schema_hash(schema: str) -> str:
    """The DDL's identity, insensitive to how it is laid out.

    Whitespace is collapsed so that reformatting the schema does not invalidate
    every fixture frozen from it, while any change to a name, a type or a
    constraint does.
    """
    collapsed = _WHITESPACE.sub(" ", schema).strip()
    return hashlib.sha256(collapsed.encode("utf-8")).hexdigest()


def _prove_the_ddl_executes(name: str, schema: str) -> None:
    """Build the schema in memory, so a world with broken DDL cannot exist.

    The DDL *rules* -- STRICT, primary keys, no wall clock -- belong to
    `seahaven check` and are not applied here: a world under development runs
    long before it lints clean.
    """
    try:
        build_blank(":memory:", schema).close()
    except apsw.Error as error:
        raise WorldBug(f"world {name!r} has DDL that does not execute: {error}") from error


def _derive_fixtures_dir(caller_file: str | None) -> Path:
    """`fixtures/` at the project root, found by walking up from the world's module.

    The project root is the nearest directory holding a `pyproject.toml`. An
    installed wheel has none above it, and `fixtures/` beside the package
    directory is the answer there. This never fails: a world that only makes
    blank instances never reads the directory, and a fixture that cannot be found
    says `World(fixtures_dir=...)` in its message.
    """
    # A `World` built somewhere with no file at all -- a REPL, an `exec` -- has
    # only the working directory to go on, and `fixtures/` under it is the answer
    # there: "beside the package" means nothing when there is no package.
    if caller_file is None:
        return _fixtures_dir_at_project_root(Path.cwd()) or Path.cwd() / FIXTURES_DIRNAME
    package_dir = Path(caller_file).resolve().parent
    return _fixtures_dir_at_project_root(package_dir) or package_dir.parent / FIXTURES_DIRNAME


def _fixtures_dir_at_project_root(start: Path) -> Path | None:
    """`fixtures/` under the nearest directory holding a `pyproject.toml`, walking up."""
    for directory in (start, *start.parents):
        if (directory / "pyproject.toml").is_file():
            return directory / FIXTURES_DIRNAME
    return None


def _check_middleware_shape(obj: Middleware) -> None:
    """A middleware is anything callable as `(ctx, call, next_)`.

    Structural, with nothing to subclass: the check is that the three arguments
    can be passed positionally, and a `*args` middleware satisfies it too.
    """
    try:
        parameters = list(inspect.signature(obj).parameters.values())
    except (TypeError, ValueError) as error:
        raise WorldBug(f"middleware must be callable as (ctx, call, next_): {obj!r}") from error
    if any(p.kind is inspect.Parameter.VAR_POSITIONAL for p in parameters):
        return
    if len([p for p in parameters if p.kind in _POSITIONAL]) < 3:
        raise WorldBug(f"middleware must be callable as (ctx, call, next_): {obj!r}")


def _as_startup_hook(obj: StartupHook) -> RegisteredStartupHook:
    """Record what `reset` arguments a hook accepts, refusing a shape that cannot work."""
    try:
        parameters = list(inspect.signature(obj).parameters.values())
    except (TypeError, ValueError) as error:
        raise WorldBug(
            f"an instance startup hook takes the context and keyword arguments: {obj!r}"
        ) from error
    positional = [p for p in parameters if p.kind in _POSITIONAL]
    variadic_positional = any(p.kind is inspect.Parameter.VAR_POSITIONAL for p in parameters)
    if len(positional) != 1 or variadic_positional:
        raise WorldBug(
            f"an instance startup hook takes the context as its only positional parameter and "
            f"everything else by keyword: {obj!r}"
        )
    for parameter in parameters[1:]:
        if parameter.name in RESET_ARGUMENTS:
            raise WorldBug(
                f"an instance startup hook cannot take {parameter.name!r}: it is reset's own "
                f"argument ({', '.join(sorted(RESET_ARGUMENTS))})"
            )
    return RegisteredStartupHook(
        fn=obj,
        accepts=frozenset(p.name for p in parameters if p.kind is inspect.Parameter.KEYWORD_ONLY),
        takes_var_kwargs=any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters),
    )
