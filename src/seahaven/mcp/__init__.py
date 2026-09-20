"""`seahaven mcp`'s server: one world, one MCP connection, one instance.

The process speaks MCP over stdio, holds one instance for its whole life, drives
the world in process, and publishes the world's tools and nothing else. What a
client sees is the product the world clones; nothing on the protocol surface is
about Seahaven (`functional_spec.md` §1).

`seahaven.openenv` is the other wire, and the two share no code: this module
imports no OpenEnv, and serving a single session through the OpenEnv server
would cost uvicorn, a WebSocket to ourselves and the whole session manager for
one instance.

**stdout needs no work of ours.** `stdio_server()` claims the file descriptors
while serving: fd 0 points at the null device and fd 1 at stderr, restored on
exit. A world's `print`, a C extension writing to fd 1 and a child process the
world spawns all miss the wire, which is stronger than a redirection of ours
would be.
"""

import logging
import os
import signal
import sys
from collections.abc import Callable, Mapping
from typing import Any, NoReturn

import anyio
from mcp.server.stdio import stdio_server

from seahaven.mcp.server import Sessions, State, build_server
from seahaven.world import World

__all__ = ["DEFAULT_FATAL_GRACE", "serve"]

_log = logging.getLogger(__name__)

# How long a process that can never serve stays up after it has answered a
# client. The client is told why the world did not start and closes, which ends
# the read stream; this is the bound on a client that does not close.
DEFAULT_FATAL_GRACE = 5.0


def serve(
    world: World | Callable[[], World],
    *,
    reset_options: Mapping[str, Any],
    fatal_grace: float = DEFAULT_FATAL_GRACE,
) -> int:
    """Serve one world over stdio until the client disconnects. Answers the exit code.

    0 when the client disconnects normally, and 1 when the world or its instance
    could not be made -- which the client is told about first, as a JSON-RPC
    error carrying the framework's message (`functional_spec.md` §5.2).

    The instance is destroyed on the way out, on every path, which is what
    removes its working directory and its copy of the fixture.

    This owns the process while it runs: stdin and stdout are the protocol's,
    and `SIGINT` and `SIGTERM` end the process here rather than reaching a
    caller.
    """
    sessions = Sessions()
    try:
        return anyio.run(_serve, world, reset_options, fatal_grace, sessions)
    finally:
        sessions.close_all()


async def _serve(
    world: World | Callable[[], World],
    reset_options: Mapping[str, Any],
    fatal_grace: float,
    sessions: Sessions,
) -> int:
    """The serve loop, and the two things that can end it early.

    A signal ends it because a client that is killed rather than closed must not
    leave an instance behind; a fatal start ends it because a client that has
    been told the world will never start may still not close.
    """
    state = State()
    # The connection this process serves, and the key its instance is held
    # under. One object, because stdio is one connection; a Streamable HTTP
    # transport would make one per `Mcp-Session-Id` (`seahaven/mcp/server.py`).
    connection = object()
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(_stop_on_signal, sessions, state)
        tasks.start_soon(_stop_after_fatal, sessions, state, fatal_grace)
        async with stdio_server() as (read, write):
            # Inside the claim on the file descriptors, and not before it:
            # resolving the world imports the world's package, and a package
            # that prints while it is imported would write to the wire.
            server = build_server(
                world,
                reset_options=reset_options,
                sessions=sessions,
                state=state,
                key=connection,
            )
            await server.run(read, write, server.create_initialization_options())
        # The read stream ended: the client disconnected, and the two tasks
        # above have nothing left to wait for.
        tasks.cancel_scope.cancel()
    return _exit_code(state)


async def _stop_on_signal(sessions: Sessions, state: State) -> None:
    """Stop on the first `SIGINT` or `SIGTERM`."""
    with anyio.open_signal_receiver(signal.SIGINT, signal.SIGTERM) as signals:
        async for received in signals:
            _log.info("stopping on %s", signal.Signals(received).name)
            _stop_the_process(sessions, _exit_code(state))


async def _stop_after_fatal(sessions: Sessions, state: State, grace: float) -> None:
    """Stop `grace` seconds after a client was told the world will never start."""
    await state.failed.wait()
    await anyio.sleep(grace)
    _log.info("stopping: this process has no instance and cannot make one")
    _stop_the_process(sessions, _exit_code(state))


def _exit_code(state: State) -> int:
    return 1 if state.fatal is not None else 0


def _stop_the_process(sessions: Sessions, code: int) -> NoReturn:
    """Destroy every instance, then end the process.

    Cancelling the serve loop would not end it: `stdio_server` reads stdin on a
    worker thread, and a blocking read on a thread is the one thing `anyio`
    cannot cancel, so a process parked on that read stays parked whatever its
    cancel scope is told. Destruction is the part that must not be skipped --
    it is what removes the instance's directory and its copy of the fixture --
    so it happens here, and the process then ends rather than waiting for a
    client that has already gone or that will never speak again.
    """
    sessions.close_all()
    logging.shutdown()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
