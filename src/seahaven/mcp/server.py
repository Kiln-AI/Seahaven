"""The `Server`: the instance map, the middleware that fills it, and two handlers.

One instance per MCP connection, made inside a handler and never before the
protocol starts, so that a fixture that does not exist or a startup hook that
raises reaches the client as a JSON-RPC error instead of a broken pipe
(`functional_spec.md` §5.2).

The server publishes tools and nothing else: no resources, no prompts, no
control tool, no state document. What a client sees is the world's own surface,
and nothing on it is about Seahaven (`functional_spec.md` §1).
"""

import functools
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import anyio
import anyio.to_thread
from mcp import types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server
from mcp.shared.exceptions import MCPError
from mcp.types import INTERNAL_ERROR

from seahaven.errors import INTERNAL_ERROR_MESSAGE, SeahavenError, ToolError, UnknownTool, WorldBug
from seahaven.instances import Instance
from seahaven.mcp import wire
from seahaven.world import CONTROL_TOOL_NAMES, World

__all__ = [
    "NEEDS_AN_INSTANCE",
    "UNRESOLVED_NAME",
    "Sessions",
    "State",
    "build_server",
    "instructions_for",
]

_log = logging.getLogger(__name__)

# The methods that cannot be answered without an instance, which is also every
# moment an instance may be made. `initialize` is the one a handshake client
# opens with; the other two are where a client on the 2026-07-28 protocol -- a
# connection that has no handshake at all -- first needs a world.
NEEDS_AN_INSTANCE = frozenset({"initialize", "tools/list", "tools/call"})

# What the server calls itself when the world could not be resolved at all.
# Nothing a client reads carries it, because the middleware refuses every request
# such a server receives and an error frame has no `serverInfo` -- which is why
# that refusal is on every method rather than on the three that need an instance.
UNRESOLVED_NAME = "seahaven"


class Sessions:
    """One instance per MCP connection. A stdio process serves exactly one.

    The key is the connection and not the process, from the first commit, so
    that a second transport is a second caller of `open` and not a rewrite
    (`functional_spec.md` §6). This class is the part that is transport-agnostic:
    nothing here assumes one entry, and nothing here knows what a key is.

    The key is an opaque object rather than the SDK's `ServerSession`, which
    `architecture.md` §4 named: in `mcp` 2.2.0 a `ServerSession` is a per-request
    proxy built by `ServerRunner._make_context` for every inbound message, in
    both protocol eras, so a map keyed on it would never find an instance again.
    `serve()` makes one key, because one stdio process is one connection.

    No lock: the map is touched only from the event loop thread -- the
    middleware and the handlers are async, and the only thing handed to a worker
    thread is the `Instance` itself.
    """

    def __init__(self) -> None:
        self._instances: dict[object, Instance] = {}

    def open(self, key: object, instance: Instance) -> None:
        """Hold `instance` for `key`. One episode per connection, so never twice."""
        if key in self._instances:
            raise WorldBug("this MCP connection already has an instance; there is no reset")
        self._instances[key] = instance

    def get(self, key: object) -> Instance:
        """The connection's instance, or a `WorldBug`.

        A request that needs an instance is answered by a handler the middleware
        has already run for, so an absent one is this server's own mistake and
        not something a client can provoke.
        """
        instance = self._instances.get(key)
        if instance is None:
            raise WorldBug("this MCP connection has no instance")
        return instance

    def has(self, key: object) -> bool:
        return key in self._instances

    def close(self, key: object) -> None:
        """Destroy the connection's instance and forget it. Idempotent."""
        instance = self._instances.pop(key, None)
        if instance is not None:
            instance.destroy()

    def close_all(self) -> None:
        """Destroy every instance. A destruction that fails is logged, not raised.

        Every other instance still has to be destroyed, and this runs on the way
        out of the process, where there is nobody left to answer.
        """
        for key in list(self._instances):
            try:
                self.close(key)
            except Exception:
                _log.exception("destroying the instance of MCP connection %r failed", key)


@dataclass
class State:
    """What this process learned that outlives one request.

    `fatal` is set when the instance could not be made: there is no instance,
    there never will be one in this process, and `serve` answers 1. `failed` is
    how the serve loop hears about it without polling.
    """

    fatal: BaseException | None = None
    failed: anyio.Event = field(default_factory=anyio.Event)


def instructions_for(world: World) -> str:
    """The MCP `instructions` string: the world's own prose, never the framework's.

    `mcp_server_instructions` is returned verbatim when the author set it, so an
    author cloning a real MCP server can match that server's instructions word
    for word. Otherwise the default is built from the world's name and its
    description, which are the author's prose too; a world with no description
    falls back to the name alone. Nothing about this process -- the fixture, the
    seed, the instance -- is ever in it (`functional_spec.md` §3).
    """
    given = world.mcp_server_instructions
    if given is not None and given.strip():
        return given
    description = (world.description or "").strip()
    return f"{world.name}\n\n{description}" if description else world.name


def build_server(
    world: World | Callable[[], World],
    *,
    reset_options: Mapping[str, Any],
    sessions: Sessions,
    state: State,
    key: object,
) -> Server[None]:
    """The `Server` for one connection's worth of world.

    `world` takes a callable as well as a `World` so that the CLI can defer
    discovery to here, where a failed import is answered rather than raised: a
    process that died before the handshake gives the client nothing but a broken
    pipe (`functional_spec.md` §5.2). A harness driving this in process passes
    the `World` itself.

    `key` is one connection's, which is what a stdio process has: one `Server`
    and one client. A Streamable HTTP transport would serve many connections
    from one `Server`, so it would take a key *function* of the request context
    -- `Mcp-Session-Id` -- in place of this argument. `Sessions` itself would be
    unchanged.
    """
    resolved = _resolve(world, state)
    making = anyio.Lock()

    async def open_connection(
        ctx: ServerRequestContext[None, Any],
        call_next: Callable[[ServerRequestContext[None, Any]], Awaitable[Any]],
    ) -> Any:
        # A notification carries no `request_id` and is answered with nothing, so
        # it neither makes an instance nor is worth refusing.
        if ctx.request_id is not None:
            if state.fatal is not None:
                # Every method, and not only the three that need an instance:
                # `server/discover` is the modern era's handshake, and a process
                # that can never serve must not answer one with an identity of
                # its own (`functional_spec.md` §1 and §5.2). The SDK stamps
                # `serverInfo` onto results and never onto error frames, so a
                # refusal here says nothing about this server at all.
                state.failed.set()
                raise _fatal_error(state.fatal)
            if ctx.method in NEEDS_AN_INSTANCE:
                await ensure_instance()
        return await call_next(ctx)

    async def ensure_instance() -> None:
        """Make this connection's instance, once, before the first handler that needs it.

        `architecture.md` §5 made it in the `initialize` middleware alone. In
        `mcp` 2.2.0 a connection on the 2026-07-28 protocol never sends
        `initialize` -- the era is chosen from the client's opening request, and
        the SDK's own client opens with `server/discover` -- so an instance made
        only there would leave every such client with no world. The first request
        that needs one is the same moment for a handshake client and the only one
        there is for a modern client.

        The lock is for the modern connection too: several requests may be in
        flight there, and the handshake's guarantee that `initialize` runs inline
        and alone does not hold.
        """
        async with making:
            if state.fatal is not None:
                # A request that was let through above and then waited here while
                # another one failed. The world is not made a second time: there
                # is no instance, and there never will be one in this process.
                state.failed.set()
                raise _fatal_error(state.fatal)
            if sessions.has(key):
                return
            assert resolved is not None  # no fatal error means the world resolved
            try:
                instance = await anyio.to_thread.run_sync(
                    functools.partial(resolved.instance, **reset_options)
                )
            except Exception as error:
                # Not scrubbed (`functional_spec.md` §5.2). A working world looks
                # like the world it clones; a broken one says what is actually
                # wrong, to the person who wrote the client configuration that
                # launched this process. Nobody is being evaluated against a
                # server that never started.
                state.fatal = error
                state.failed.set()
                _log.error("the instance could not be made: %s", error, exc_info=True)
                raise _fatal_error(error) from error
            sessions.open(key, instance)

    async def on_list_tools(
        ctx: ServerRequestContext[None, Any], params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        """The world's tools, and nothing else. Control tools are never listed.

        The list is fixed for the life of the process, so it is never paginated
        and no `tools/list_changed` notification is ever sent.
        """
        return types.ListToolsResult(tools=wire.listing(sessions.get(key).tools()))

    async def on_call_tool(
        ctx: ServerRequestContext[None, Any], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        """One `Instance.call`, with every failure rendered rather than raised.

        The ladder is `seahaven/openenv/env.py`'s, on another wire: a `ToolError`
        is data the agent reads and recovers from, a framework error is the
        author's and fails the frame with nothing in it, and an accident is
        answered with the fixed generic error. Only the two scrubbed rows can
        carry an author's prose, and both send it to stderr instead.

        Several calls in flight are several worker threads, which queue on the
        instance's own lock: calls into one instance serialise, which is what
        `functional_spec.md` §7 promises.
        """
        instance = sessions.get(key)
        name = params.name
        arguments = dict(params.arguments or {})
        try:
            if name in CONTROL_TOOL_NAMES:
                # The interim filter of `architecture.md` §9, in the same words a
                # name the world does not have earns: whether a control tool
                # exists is not something a client gets to learn by calling.
                # `functional_spec.md` §15 makes control tools opt-in in core,
                # and these three lines are what it deletes.
                raise UnknownTool(name)
            result = await anyio.to_thread.run_sync(
                functools.partial(instance.call, name, **arguments)
            )
        except ToolError as error:
            return wire.failure(error)
        except SeahavenError as error:
            correlation = wire.log_failure(name, "raised a framework error")
            raise MCPError(INTERNAL_ERROR, f"{INTERNAL_ERROR_MESSAGE} ({correlation})") from error
        except Exception:
            correlation = wire.log_failure(name, "failed with an unhandled exception")
            return wire.failure(wire.internal_error(correlation))
        return wire.success(result)

    server = Server[None](
        name=resolved.name if resolved is not None else UNRESOLVED_NAME,
        version=resolved.version if resolved is not None else "",
        instructions=instructions_for(resolved) if resolved is not None else None,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )
    # Appended rather than assigned: the SDK ships middleware of its own on that
    # list, and replacing the list would drop it.
    server.middleware.append(open_connection)
    return server


def _resolve(source: World | Callable[[], World], state: State) -> World | None:
    """The world, however it was given, or `None` with the failure recorded.

    A failure is answered and not raised, because the client is owed the reason:
    every request this process goes on to receive is refused with it, and the
    process then exits non-zero.
    """
    if isinstance(source, World):
        return source
    try:
        return source()
    except Exception as error:
        state.fatal = error
        _log.error("the world could not be resolved: %s", error, exc_info=True)
        return None


def _fatal_error(error: BaseException) -> MCPError:
    """The JSON-RPC error a failed start is answered with, every time it is asked.

    A `ToolError` carries the framework's `{code, message, details}`, which goes
    in `data` -- the placement `seahaven/openenv/env.py` already uses when it
    refuses `/mcp`.
    """
    data = error.to_dict() if isinstance(error, ToolError) else None
    return MCPError(INTERNAL_ERROR, str(error), data)
