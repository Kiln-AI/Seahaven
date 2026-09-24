"""Serve a world's HTTP handler, one instance per client-chosen ID.

A world writes its API as one function, `handler(ctx, request) -> response`, over
the types below, and its tools call that function. The types need no extra;
`app`, `serve` and `main` need the `serve` extra, and import it only when called.
"""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from seahaven.http.messages import HttpHandler, HttpRequest, HttpResponse
from seahaven.http.runtime import DEFAULT_MAX_INSTANCES
from seahaven.world import World

if TYPE_CHECKING:
    from starlette.types import ASGIApp

__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_MAX_INSTANCES",
    "DEFAULT_PORT",
    "MISSING_EXTRA",
    "HttpHandler",
    "HttpRequest",
    "HttpResponse",
    "app",
    "main",
    "serve",
]

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
MISSING_EXTRA = 'seahaven.http needs the serve extra: pip install "seahaven[serve]"'

# What `seahaven.http.server` imports that no bare install has.
_SERVER_STACK = frozenset({"starlette", "uvicorn"})


def app(
    world: World,
    handler: HttpHandler,
    *,
    reset_options: Mapping[str, Any] | None = None,
    max_instances: int = DEFAULT_MAX_INSTANCES,
) -> ASGIApp:
    """The ASGI app that serves `handler`, one instance of `world` per ID.

    `reset_options` are the keyword arguments each instance is created with, the
    keys `seahaven mcp --reset-options` takes. A PUT body is laid over them.
    They are checked here: an unknown key, `control_tools` or a fixture the
    world does not have raises `WorldBug`. `max_instances` is how many
    instances may exist at once, `0` for no limit.
    """
    with _server_stack():
        from seahaven.http.server import app as build

    return build(world, handler, reset_options=reset_options, max_instances=max_instances)


def serve(
    world: World,
    handler: HttpHandler,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    reset_options: Mapping[str, Any] | None = None,
    max_instances: int = DEFAULT_MAX_INSTANCES,
) -> None:
    """Serve `app(world, handler, ...)` with uvicorn until the process is stopped."""
    with _server_stack():
        from seahaven.http.server import serve as run

    run(
        world,
        handler,
        host=host,
        port=port,
        reset_options=reset_options,
        max_instances=max_instances,
    )


def main(world: World, handler: HttpHandler, argv: list[str] | None = None) -> None:
    """The command line of a world's `serve_http.py`: parse `argv` and call `serve`.

    `argv` defaults to `sys.argv[1:]`. It takes `--host`, `--port`,
    `--max-instances` and the reset-option flags and variables `seahaven mcp`
    takes. A refused option is one line on stderr and exit 1.
    """
    from seahaven.http.command import main as run

    run(world, handler, argv)


@contextmanager
def _server_stack() -> Iterator[None]:
    """Turn a missing Starlette or uvicorn into `ImportError(MISSING_EXTRA)`."""
    try:
        yield
    except ModuleNotFoundError as error:
        if (error.name or "").partition(".")[0] not in _SERVER_STACK:
            raise
        raise ImportError(MISSING_EXTRA) from error
