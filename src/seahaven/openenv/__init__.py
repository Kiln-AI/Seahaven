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
from typing import Any, NoReturn

from fastapi import FastAPI, HTTPException, WebSocketDisconnect
from fastapi.routing import APIRoute
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


# OpenEnv's three plain-HTTP episode-control routes. Every one of their handlers
# builds an `Environment` from the factory, uses it and closes it in a `finally`
# before answering -- `http_server.py` lines 674/695 (`/reset`), 709/733
# (`/step`) and 1347/1351 (`/state`) in openenv 0.4.2 -- so each request runs
# against its own throwaway environment and none of the three observes another.
# The answers are well-formed, 200, and meaningless. `/metadata` builds one the
# same way and is deliberately not in this set: metadata is the world's and not
# an episode's, so a fresh environment answers it correctly.
_HTTP_EPISODE_CONTROL_ROUTES = frozenset({"/reset", "/step", "/state"})

# 501, weighed against 410 and 400. 501 is the one that is literally true: RFC
# 9110 gives it for functionality the server does not support, and holding an
# episode over plain HTTP is exactly that -- the request is well formed and the
# resource is real, the capability is absent. 410 asserts the resource was
# served here and is permanently gone with no forwarding address; neither half
# holds, since Seahaven never served a working episode over HTTP, the path stays
# in the OpenAPI schema on purpose, and `/ws` *is* the forwarding address. 400
# blames the request, which would send a caller off editing a payload that can
# never be right. The usual objection to a 5xx -- retry storms, a red dashboard
# -- does not bite here: 501 is non-transient by definition and is in no default
# retry set (`urllib3.Retry` forces no status; `httpx` retries no status at
# all), and nothing health-checks these. OpenEnv's own push-time validator
# probes `/health`, `/metadata`, `/schema`, `/mcp` and `/openapi.json` and reads
# these three only as OpenAPI path names (`cli/_validation.py`
# `mode_endpoint_consistency`), so it never sees this response.
_HTTP_EPISODE_CONTROL_STATUS = 501

# The body is FastAPI's `{"detail": ...}`, because that is the envelope every
# other HTTP error from this app already uses and a generic client expects, and
# inside it Seahaven's own `{"code", "message", "details"}` -- the shape
# `ToolError.to_dict()` gives, so one error shape spans the framework. Nested
# rather than raised to the top level on purpose: a tool error is data an agent
# reads off an observation, and this is a transport-level refusal aimed at
# whoever pointed an HTTP client at the wrong endpoint. They should not arrive
# looking like the same thing.
_HTTP_EPISODE_CONTROL_CODE = "http_episode_control_unsupported"


def _http_episode_control_message(route: str) -> str:
    """The refusal in prose, for the person reading it at 2am.

    It is three answers in one, because at that hour all three are wanted at
    once: what just happened, whose defect it is and how to check the claim, and
    which door to use instead.
    """
    return (
        f"{route} cannot hold an episode, so Seahaven refuses it rather than answer something "
        "that looks right and is not. OpenEnv builds a brand-new environment for every plain "
        "HTTP request and closes it again before replying, so this route would have answered "
        "from an environment that has never seen your reset, never ran your steps, and is about "
        "to be thrown away -- with a 200 and a plausible body. That is an upstream defect in "
        "openenv 0.4.2, not a Seahaven limitation and not something a world can fix from its own "
        "side: every /reset, /step and /state handler in openenv/core/env_server/http_server.py "
        "builds an Environment from the factory and closes it in a finally, a regression "
        "introduced in OpenEnv commit 86a222d. Drive the episode over the WebSocket transport at "
        "/ws instead, which is genuinely session-bound -- one connection is one session holding "
        "one instance. seahaven.openenv.SeahavenClient speaks it, and so does the stock OpenEnv "
        "EnvClient."
    )


def _http_episode_control_refusal(route: str) -> dict[str, Any]:
    """The whole body these routes answer, under FastAPI's `detail` key."""
    return {
        "code": _HTTP_EPISODE_CONTROL_CODE,
        "message": _http_episode_control_message(route),
        "details": {
            "route": route,
            "use_instead": "/ws",
            "clients": ["seahaven.openenv.SeahavenClient", "openenv.EnvClient"],
            "upstream": {
                "package": "openenv 0.4.2",
                "file": "openenv/core/env_server/http_server.py",
                "regression": "86a222d",
                "defect": (
                    "each /reset, /step and /state handler builds an Environment from the "
                    "factory and closes it before returning, so no two requests share one"
                ),
            },
        },
    }


def _refusing_route(route: APIRoute) -> APIRoute:
    """The same route, answering the refusal instead of a throwaway environment."""
    methods = route.methods or set()
    # `HEAD` is Starlette's own addition to every `GET` route, and `OPTIONS` the
    # CORS preflight: neither is what a caller typed, so neither belongs in the
    # line naming the route back to them.
    named = f"{'/'.join(sorted(methods - {'HEAD', 'OPTIONS'}))} {route.path}"
    refusal = _http_episode_control_refusal(named)

    # No parameters, so a request with a valid body, an invalid one, or none at
    # all, all reach the refusal. A handler that still declared `StepRequest`
    # would answer 422 to a malformed step and hide the real reason it failed.
    async def refuse() -> NoReturn:
        raise HTTPException(status_code=_HTTP_EPISODE_CONTROL_STATUS, detail=refusal)

    return APIRoute(
        route.path,
        refuse,
        methods=sorted(methods),
        name=route.name,
        tags=route.tags,
        summary=f"Refused: {named} cannot hold an episode over HTTP",
        description=_http_episode_control_message(named),
        status_code=_HTTP_EPISODE_CONTROL_STATUS,
        response_model=None,
        responses={
            _HTTP_EPISODE_CONTROL_STATUS: {
                "description": "Refused: use the WebSocket transport at /ws.",
            }
        },
    )


def _refuse_http_episode_control(served: FastAPI) -> None:
    """Swap OpenEnv's three episode-control handlers for one that refuses.

    Replaced in place rather than deleted, because `openenv push` validates a
    world by reading the app's OpenAPI paths: `mode_endpoint_consistency` in
    `openenv/cli/_validation.py` calls an app with `/reset` a simulation
    environment and then requires `/step` and `/state` beside it. Deleting the
    three would silently reclassify a Seahaven world as a production
    environment, which is a different and wrong declaration about what it is.
    Keeping the paths registered keeps the declaration honest and the validation
    passing; only the behaviour changes, which is the half that was lying.
    """
    for index, route in enumerate(served.router.routes):
        if isinstance(route, APIRoute) and route.path in _HTTP_EPISODE_CONTROL_ROUTES:
            served.router.routes[index] = _refusing_route(route)


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

    OpenEnv's plain-HTTP `/reset`, `/step` and `/state` are replaced by a route
    that refuses them, because upstream cannot hold an episode over HTTP and
    answers a throwaway environment instead. The websocket transport is the
    product and is untouched.

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
    _refuse_http_episode_control(served)
    return served
