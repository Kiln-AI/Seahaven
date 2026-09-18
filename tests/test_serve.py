"""`serve()`: the gate it sets, and the arguments it hands uvicorn.

Every other module in this group starts a real server, because that is the only
way to prove a session is a session. This one does not: what `serve` adds over
`app` is three decisions -- the gate is sized before the app is built, the app
gets the operator's numbers, and uvicorn is given one worker and an app object
rather than an import string -- and a server that runs until the process is
stopped is not the way to read any of them. The server class is replaced, and
the arguments `serve` handed `uvicorn.Config` are the assertion.

The one-worker rule is the reason this file exists at all. A second worker
process answers a session's second frame with an environment that has never seen
its first, and nothing in a single-process test suite would ever notice.
"""

from typing import Any

import pytest

from seahaven import instances
from seahaven.world import World

# The subpackage and not `openenv`: what this module imports is
# `seahaven.openenv`, so that is what has to import for the tests below to mean
# anything. Only an `ImportError` skips -- an extra that is absent, or installed
# and unimportable. Anything else raises, and CI asserts this import separately,
# because an installed extra that skips quietly is a green run that tested none
# of this.
pytest.importorskip(
    "seahaven.openenv", exc_type=ImportError, reason="the serve extra does not import here"
)

from seahaven.openenv import DEFAULT_MAX_CONCURRENT_ENVS, DEFAULT_SESSION_TIMEOUT
from seahaven.openenv import serve as serve_module
from seahaven.openenv.serve import DEFAULT_HOST, DEFAULT_PORT, serve


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Record what `serve` does instead of letting it serve for ever.

    `app` and `uvicorn.Config` are wrapped rather than replaced: both are still
    built for real -- so a call either of them would refuse still fails here --
    and the arguments they were handed are recorded on the way past, which is
    the claim `serve` is making. Only the `run` that never returns is replaced.

    The arguments are read as `serve` passed them, never off the built
    `Config`. `Config.__init__` fills in a default for every one of them, so a
    `Config` answers `workers == 1` whether or not anything asked for one
    worker, and the one-worker assertion below would hold with the line that
    makes it true deleted.
    """
    call: dict[str, Any] = {}
    build = serve_module.app
    configure = serve_module.uvicorn.Config

    def app(world: World, **options: Any) -> Any:
        call["options"] = options
        return build(world, **options)

    def config(app: Any, **kwargs: Any) -> Any:
        call["app"] = app
        call["kwargs"] = kwargs
        return configure(app, **kwargs)

    class Recorder:
        """Stands in for the server, so that `run` returns."""

        announce = ""

        def __init__(self, config: Any) -> None:
            call["config"] = config

        def run(self) -> None:
            # The gate's size as it stands when uvicorn would start: the ordering
            # claim. `instances.concurrency()` rather than `_gate._initial_value`,
            # which reached into two libraries' privates to read one number.
            call["concurrency"] = instances.concurrency()
            call["announce"] = self.announce

    monkeypatch.setattr(serve_module, "app", app)
    monkeypatch.setattr(serve_module.uvicorn, "Config", config)
    monkeypatch.setattr(serve_module, "_AnnouncingServer", Recorder)
    return call


def test_serve_runs_one_worker_on_an_app_object(world: World, served: dict[str, Any]) -> None:
    """An app object and `workers=1`, which is the only spelling uvicorn honours."""
    serve(world)
    assert served["kwargs"]["workers"] == 1
    assert not isinstance(served["app"], str), "an import string would let uvicorn fork workers"
    assert callable(served["app"])
    # The literals, not the constants: comparing a default against itself would
    # pass whatever the default became, and "which interface" is the decision.
    assert (served["kwargs"]["host"], served["kwargs"]["port"]) == ("0.0.0.0", 8000)
    assert (DEFAULT_HOST, DEFAULT_PORT) == ("0.0.0.0", 8000)
    # An operator watching a training run reads uvicorn's request log; a quieter
    # default would be a decision, and it is not the one that was made.
    assert served["kwargs"]["log_level"] == "info"


def test_serve_binds_where_it_is_told(world: World, served: dict[str, Any]) -> None:
    serve(world, host="127.0.0.1", port=9123)
    assert (served["kwargs"]["host"], served["kwargs"]["port"]) == ("127.0.0.1", 9123)


def test_serve_sizes_the_gate_before_the_server_starts(
    world: World, served: dict[str, Any]
) -> None:
    """`--concurrency 3`: three slots, in force before the first frame arrives."""
    serve(world, concurrency=3)
    assert served["concurrency"] == 3


def test_serve_without_a_concurrency_uses_the_frameworks_default(
    world: World, served: dict[str, Any]
) -> None:
    instances.set_concurrency(1)
    serve(world)
    assert served["concurrency"] == instances.default_concurrency()


def test_serve_with_a_concurrency_of_zero_removes_the_gate(
    world: World, served: dict[str, Any]
) -> None:
    serve(world, concurrency=0)
    # `0` is how `set_concurrency` spells "no gate"; that it really stops gating
    # is `test_concurrency_zero_removes_the_gate` in the instances suite.
    assert served["concurrency"] == 0


def test_serve_passes_the_operators_session_numbers_to_the_app(
    world: World, served: dict[str, Any]
) -> None:
    """The app is built from `serve`'s arguments, not from the module defaults."""
    serve(world, max_concurrent_envs=7, session_timeout=None)
    assert served["options"]["max_concurrent_envs"] == 7
    assert served["options"]["session_timeout"] is None


def test_serve_passes_the_module_defaults_when_it_is_told_nothing(
    world: World, served: dict[str, Any]
) -> None:
    serve(world)
    assert served["options"] == {
        "include_control_tools": False,
        "max_concurrent_envs": 500,
        "session_timeout": 3600.0,
        "console": True,
    }
    assert (DEFAULT_MAX_CONCURRENT_ENVS, DEFAULT_SESSION_TIMEOUT) == (500, 3600.0)


def test_serve_can_expose_the_control_tools(world: World, served: dict[str, Any]) -> None:
    """The flag is the operator's, and `serve` is the only thing that carries it."""
    serve(world, include_control_tools=True)
    assert served["options"]["include_control_tools"] is True


def test_serve_announces_the_console_at_an_address_a_browser_can_open(
    world: World, served: dict[str, Any]
) -> None:
    """The line an operator reads after `seahaven serve`.

    The default bind is `0.0.0.0`, which is not an address, so the message names
    loopback. `console_url` owns that translation and is tested against every
    spelling in `test_server.py`.
    """
    serve(world)
    assert served["announce"] == "Web console available at http://127.0.0.1:8000/console"
    serve(world, host="127.0.0.1", port=8001)
    assert served["announce"] == "Web console available at http://127.0.0.1:8001/console"


def test_serve_without_a_console_neither_serves_nor_announces_one(
    world: World, served: dict[str, Any]
) -> None:
    """`--no-console`: the route is not registered and nothing is printed."""
    serve(world, console=False)
    assert served["options"]["console"] is False
    assert served["announce"] == ""


def test_the_announcement_is_made_after_uvicorn_has_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under uvicorn's own line, and only for a server that came up.

    `_AnnouncingServer.startup` is the whole mechanism, and a uvicorn release
    that renamed or resignatured `startup` would leave `serve` silent with
    nothing else failing. The socket is not bound: the base `startup` is
    replaced, and what is asserted is that it is awaited before the line.

    A handler on uvicorn's own logger rather than `caplog`, because building a
    `uvicorn.Config` configures logging and takes `uvicorn.error` off the root
    logger, which is where `caplog` listens.
    """
    import asyncio
    import logging

    started: list[str] = []
    said: list[str] = []

    async def startup(self: Any, sockets: Any = None) -> None:
        started.append("uvicorn")

    class Listener(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            said.append(record.getMessage())

    monkeypatch.setattr(serve_module.uvicorn.Server, "startup", startup)
    server = serve_module._AnnouncingServer(serve_module.uvicorn.Config(lambda: None))
    server.announce = "Web console available at http://127.0.0.1:8000/console"

    listener = Listener()
    logging.getLogger("uvicorn.error").addHandler(listener)
    try:
        asyncio.run(server.startup())
    finally:
        logging.getLogger("uvicorn.error").removeHandler(listener)

    assert started == ["uvicorn"]
    assert said == ["Web console available at http://127.0.0.1:8000/console"]


def test_nothing_is_announced_when_there_is_no_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty `announce` is silence, not an empty line in the operator's log."""
    import asyncio
    import logging

    said: list[str] = []

    async def startup(self: Any, sockets: Any = None) -> None:
        return None

    class Listener(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            said.append(record.getMessage())

    monkeypatch.setattr(serve_module.uvicorn.Server, "startup", startup)
    server = serve_module._AnnouncingServer(serve_module.uvicorn.Config(lambda: None))

    listener = Listener()
    logging.getLogger("uvicorn.error").addHandler(listener)
    try:
        asyncio.run(server.startup())
    finally:
        logging.getLogger("uvicorn.error").removeHandler(listener)

    assert said == []
