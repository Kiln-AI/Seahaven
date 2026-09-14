"""Seahaven over OpenEnv: the ASGI app, the environment, the models, the client.

This subpackage is the only part of Seahaven that needs the `serve` extra, and
it imports `openenv` at module top: `import seahaven` never reaches here, so a
world used in-process -- in pytest, in a script, in a notebook -- never pays for
a dependency whose wheel brings gradio, openai, fastmcp and pandas with it.

```python
from projecttracker import world
import seahaven.openenv

app = seahaven.openenv.app(world)
```

That file, `openenv_app.py`, is the whole of a world's server. One world per
app, mounted at `/`, which is the shape a hub expects.
"""

import functools
import logging

from fastapi import FastAPI, WebSocketDisconnect
from openenv.core.env_server.http_server import create_app
from openenv.core.env_server.mcp_types import (
    CallToolAction,
    ListToolsAction,
    ListToolsObservation,
)
from openenv.core.env_server.types import ConcurrencyConfig
from starlette.types import ASGIApp, Receive, Scope, Send

from seahaven.openenv.client import SeahavenClient
from seahaven.openenv.env import SeahavenEnv, SeahavenObservation, SeahavenState
from seahaven.world import World

__all__ = [
    "CallToolAction",
    "ListToolsAction",
    "ListToolsObservation",
    "SeahavenClient",
    "SeahavenEnv",
    "SeahavenObservation",
    "SeahavenState",
    "app",
]

_log = logging.getLogger(__name__)

# The operator's budget, not a design limit. Seahaven refuses no session of its
# own accord; over capacity OpenEnv answers `CAPACITY_REACHED` and closes the
# connection, and `seahaven serve --max_concurrent_envs` moves the number.
DEFAULT_MAX_CONCURRENT_ENVS = 500

# A held session costs its fixture copy on disk and about a megabyte of memory,
# and a client that drops without closing holds one for ever. An hour idle is
# long enough that no live eval is reaped and short enough that a crashed
# harness does not accumulate; `--session-timeout 0` turns the reaper off.
DEFAULT_SESSION_TIMEOUT = 3600.0


class _SwallowWebSocketDisconnect:
    """ASGI middleware: a peer that has gone away is not a server error.

    OpenEnv's websocket handlers close the connection in a `finally` guarded by
    `except RuntimeError`, and closing one whose peer has already closed raises
    `WebSocketDisconnect` instead -- `http_server.py` lines 1694 (`/ws`) and 1254
    (`/mcp`) in openenv 0.4.2. It escapes the app, and uvicorn logs `ERROR:
    Exception in ASGI application` with a full traceback for a session that ended
    perfectly normally. On a 500-session server that buries the errors an
    operator is actually looking for.

    `SeahavenClient` no longer provokes it -- it waits for the server to close
    first -- so this is for the clients Seahaven does not ship: a stock
    `GenericEnvClient`, a raw socket, a harness that simply dies. It is scoped as
    narrowly as the fault allows, to a websocket connection and to
    `WebSocketDisconnect` alone, which reaching this far up the stack means the
    peer went away and never anything the server can act on. Every other
    exception, and every HTTP request, passes through untouched.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "websocket":
            await self.app(scope, receive, send)
            return
        try:
            await self.app(scope, receive, send)
        except WebSocketDisconnect:
            # Absorbed, not hidden. One debug line, so an operator chasing a
            # disconnect can still find it, and so a test can tell "nobody left
            # early" apart from "somebody did and we swallowed it".
            _log.debug("websocket peer disconnected before the handler finished: %s", scope["path"])


def app(
    world: World,
    *,
    include_control_tools: bool = False,
    max_concurrent_envs: int = DEFAULT_MAX_CONCURRENT_ENVS,
    session_timeout: float | None = DEFAULT_SESSION_TIMEOUT,
) -> FastAPI:
    """The ASGI app serving one world: one session per instance, many sessions.

    `include_control_tools` makes `controller_run_sql` and `controller_changes`
    callable over the wire. They are never listed either way; the flag is for a
    harness that drives the world itself, and a server an agent talks to should
    not have it.

    `session_timeout` is seconds of inactivity before OpenEnv reaps a session,
    or `None` for no reaper. It is passed as part of a `ConcurrencyConfig`
    because OpenEnv refuses both that and `max_concurrent_envs` together, and
    `env_name` is passed explicitly because the factory is a `functools.partial`
    and OpenEnv cannot read a name off one.
    """
    served = create_app(
        functools.partial(SeahavenEnv, world, include_control_tools=include_control_tools),
        CallToolAction,
        SeahavenObservation,
        env_name=world.name,
        concurrency_config=ConcurrencyConfig(
            max_concurrent_envs=max_concurrent_envs,
            session_timeout=session_timeout,
        ),
    )
    # Added to the app rather than wrapped around it, so this function still
    # answers a `FastAPI` and every way of serving a world -- `seahaven serve`,
    # the test helper, a hub world's `openenv_app.py` -- is covered without each
    # of them remembering to wrap.
    served.add_middleware(_SwallowWebSocketDisconnect)
    return served
