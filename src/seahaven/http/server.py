"""The ASGI app that serves one world's handler, and `serve()`, which runs it.

A plain ASGI callable rather than a Starlette router: there is one catch-all,
and a router's own 404 and 405 bodies, method defaults and slash redirects
would each need turning off. Starlette is used to read a request body and to
send a response, and for its thread pool. Everything that blocks runs there, in
`seahaven.http.runtime`.
"""

import json
import logging
import re
from collections.abc import Mapping
from typing import Any

import uvicorn
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from seahaven.errors import SeahavenError, WorldBug
from seahaven.http.messages import HttpHandler, HttpRequest, HttpResponse
from seahaven.http.runtime import (
    INSTANCE_ID,
    PUT_BODY,
    SERVER_OPTIONS,
    CapacityReached,
    CreationFailed,
    DestroyFailed,
    Registry,
    check_reset_options,
    dispatch,
    seahaven_error,
)
from seahaven.instances import Instance
from seahaven.world import World

__all__ = ["app", "base_url", "serve"]

_ROUTE = re.compile(r"/worlds/([^/]*)(/.*)?", re.DOTALL)
# Starlette sets `content-length` from the body, and uvicorn frames the body by
# it. A handler's own framing header could only disagree with that.
_FRAMING_HEADERS = frozenset({"content-length", "transfer-encoding"})
_JSON_KINDS = {list: "array", str: "string", int: "number", float: "number", bool: "boolean"}

_log = logging.getLogger("seahaven.http")


def app(
    world: World,
    handler: HttpHandler,
    *,
    reset_options: Mapping[str, Any] | None,
    max_instances: int,
) -> ASGIApp:
    """The ASGI app. `seahaven.http.app` documents the arguments."""
    if not callable(handler):
        raise WorldBug(
            f"handler must be a function (ctx, request) -> HttpResponse, not "
            f"{type(handler).__name__}"
        )
    if isinstance(max_instances, bool) or not isinstance(max_instances, int) or max_instances < 0:
        raise WorldBug(
            f"max_instances is an int of 0 or more, 0 for no limit, not {max_instances!r}"
        )
    defaults = dict(reset_options or {})
    check_reset_options(world, defaults, source=SERVER_OPTIONS)
    return _App(world, handler, Registry(world, defaults, max_instances))


def base_url(host: str, port: int) -> str:
    """The URL a client puts in front of an instance's paths, with `{id}` left in.

    A server bound to every interface prints loopback, which is an address a
    client on the same machine can reach. `seahaven.openenv.serve.console_url`
    does the same, and is not imported because its module imports `openenv`.
    """
    reachable = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    if ":" in reachable and not reachable.startswith("["):
        reachable = f"[{reachable}]"
    return f"http://{reachable}:{port}/worlds/{{id}}"


def serve(
    world: World,
    handler: HttpHandler,
    *,
    host: str,
    port: int,
    reset_options: Mapping[str, Any] | None,
    max_instances: int,
) -> None:
    """Serve until the process is stopped. `seahaven.http.serve` documents the arguments."""
    served = app(world, handler, reset_options=reset_options, max_instances=max_instances)
    # `print` rather than logging: uvicorn configures logging inside `run`, so
    # an `info` line here would be dropped. `flush` because stdout is buffered
    # when it is not a terminal.
    print(f"Serving {world.name} at {base_url(host, port)}", flush=True)
    # One worker: instances are state in this process, and a second worker
    # would answer an ID with an instance that has never seen it.
    uvicorn.run(served, host=host, port=port, workers=1, log_level="info")


class _App:
    def __init__(self, world: World, handler: HttpHandler, registry: Registry) -> None:
        self._world = world
        self._handler = handler
        self._registry = registry

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        match scope["type"]:
            case "lifespan":
                await self._lifespan(receive, send)
            case "http":
                response = await self._respond(scope, Request(scope, receive))
                await _to_starlette(response)(scope, receive, send)
            case _:
                # A websocket: closing before accepting refuses the handshake.
                await send({"type": "websocket.close", "code": 1000})

    async def _lifespan(self, receive: Receive, send: Send) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await run_in_threadpool(self._registry.close)
                await send({"type": "lifespan.shutdown.complete"})
                return

    async def _respond(self, scope: Scope, request: Request) -> HttpResponse:
        path: str = scope["path"]
        root = scope.get("root_path", "")
        if root and path.startswith(root):
            path = path[len(root) :]
        matched = _ROUTE.fullmatch(path)
        if matched is None:
            return seahaven_error(
                404,
                f"no route {path}: the world's API is under /worlds/{{id}}/, and PUT or "
                "DELETE /worlds/{id} manages an instance",
            )
        id, rest = matched[1], matched[2]
        if not INSTANCE_ID.fullmatch(id):
            return seahaven_error(
                400, f"instance ids are 1 to 64 letters, digits, '-' or '_', not {id!r}"
            )
        if rest is not None:
            return await self._forward(id, rest, scope, request)
        match scope["method"]:
            case "PUT":
                return await self._put(id, request)
            case "DELETE":
                return await self._delete(id)
            case _:
                return seahaven_error(
                    405,
                    "/worlds/{id} takes PUT or DELETE; the world's API is under /worlds/{id}/",
                    headers=(("allow", "PUT, DELETE"),),
                )

    async def _forward(self, id: str, path: str, scope: Scope, request: Request) -> HttpResponse:
        sent = HttpRequest(
            method=scope["method"],
            path=path,
            query=scope["query_string"].decode("latin-1"),
            headers=tuple(
                (name.decode("latin-1"), value.decode("latin-1"))
                for name, value in scope["headers"]
            ),
            body=await request.body(),
        )

        def respond(instance: Instance) -> HttpResponse:
            return dispatch(instance, self._handler, sent)

        def run() -> HttpResponse:
            return self._registry.run(id, respond)

        try:
            return await run_in_threadpool(run)
        except CapacityReached as error:
            return seahaven_error(503, str(error))
        except CreationFailed as error:
            return _failed(id, "created", error.cause)

    async def _put(self, id: str, request: Request) -> HttpResponse:
        raw = await request.body()
        try:
            body = json.loads(raw) if raw.strip() else {}
        except (ValueError, RecursionError) as error:
            return seahaven_error(400, f"the PUT body is not JSON: {error}")
        if not isinstance(body, dict):
            kind = _JSON_KINDS.get(type(body), "null")
            return seahaven_error(
                400, f"the PUT body is a JSON object of reset options, not a JSON {kind}"
            )
        try:
            check_reset_options(self._world, body, source=PUT_BODY)
        except WorldBug as error:
            return seahaven_error(400, str(error))
        try:
            created = await run_in_threadpool(self._registry.put, id, body)
        except CapacityReached as error:
            return seahaven_error(503, str(error))
        except CreationFailed as error:
            # Seahaven's own refusal of the options: an unknown fixture or clock
            # mode, a `now` before the fixture, an unknown startup keyword.
            if isinstance(error.cause, SeahavenError):
                return seahaven_error(400, str(error.cause))
            return _failed(id, "created", error.cause)
        except DestroyFailed as error:
            return _failed(id, "replaced", error.cause)
        return HttpResponse.json({"id": id}, status=201 if created else 200)

    async def _delete(self, id: str) -> HttpResponse:
        try:
            deleted = await run_in_threadpool(self._registry.delete, id)
        except DestroyFailed as error:
            return _failed(id, "destroyed", error.cause)
        if deleted:
            return HttpResponse(204)
        return seahaven_error(404, f"no instance {id}")


def _failed(id: str, done: str, cause: Exception) -> HttpResponse:
    """A 500 for an instance that could not be created, replaced or destroyed."""
    _log.error("instance %s could not be %s", id, done, exc_info=cause)
    return seahaven_error(
        500, f"instance {id} could not be {done}: {type(cause).__name__}: {cause}"
    )


def _to_starlette(response: HttpResponse) -> Response:
    sent = Response(content=response.body_bytes, status_code=response.status)
    # A list, so a header the handler repeats, such as `set-cookie`, is sent twice.
    sent.raw_headers += [
        (name.lower().encode("latin-1"), value.encode("latin-1"))
        for name, value in response.headers
        if name.lower() not in _FRAMING_HEADERS
    ]
    return sent
