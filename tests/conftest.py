"""Shared fixtures: a frozen instant, a hardened database on it, and a small world."""

import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from seahaven import instances
from seahaven.clock import Clock
from seahaven.ctx import Ctx, InstanceInfo
from seahaven.db import Db, open_instance
from seahaven.errors import ToolError
from seahaven.ids import Ids, instance_seed
from seahaven.instances import Instance
from seahaven.world import World

WAIT = 5.0  # every thread test's patience, in seconds

# Milliseconds on purpose: a clock whose instant is a whole second hides the
# rounding mistakes that a world's canonical timestamps would trip over.
INSTANT = datetime(2024, 3, 5, 12, 0, 0, 123000, tzinfo=UTC)
INSTANT_ISO = "2024-03-05T12:00:00.123Z"

NOTES_SCHEMA = """
CREATE TABLE notes (
    id TEXT PRIMARY KEY,
    body TEXT NOT NULL,
    n INTEGER NOT NULL DEFAULT 0
) STRICT;
"""


class Caller(threading.Thread):
    """A call on another thread, whose failure is the test's failure.

    An exception in a bare `Thread` is a warning pytest prints and a test that
    passes anyway, which is no way to test a lock.
    """

    def __init__(self, run: Callable[[], Any]) -> None:
        super().__init__(daemon=True)
        self._run = run
        self.failure: BaseException | None = None

    def run(self) -> None:
        try:
            self._run()
        except BaseException as error:
            self.failure = error

    def finish(self, timeout: float = WAIT) -> None:
        self.join(timeout)
        assert not self.is_alive(), "the call never finished"
        if self.failure is not None:
            raise self.failure


@pytest.fixture
def clock() -> Clock:
    return Clock(INSTANT)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "state.sqlite"


@pytest.fixture
def db(db_path: Path, clock: Clock) -> Iterator[Db]:
    """A writable instance connection, hardened the way a real instance is."""
    database = open_instance(db_path, clock)
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def ctx(db: Db, clock: Clock) -> Ctx:
    """An instance context as a call receives it, without an instance behind it.

    Everything in the call path takes a `Ctx` and nothing else, which is what
    lets it be tested before `instances.py` exists.
    """
    return Ctx(
        db=db,
        clock=clock,
        ids=Ids(instance_seed("test")),
        state={},
        instance=InstanceInfo(id="i_test", fixture=None, seed=instance_seed("test")),
    )


@pytest.fixture(autouse=True)
def _process_wide_runtime_state() -> Iterator[None]:
    """Give every test the process state a fresh process would have.

    The concurrency gate and the once-per-process sweep are module-level by
    design (both are about the process, not about one world), so a test that
    changes either would otherwise change the next one.
    """
    concurrency, gate = instances._concurrency, instances._gate
    yield
    instances._concurrency, instances._gate = concurrency, gate
    instances._swept = False


def build_world(
    tmp_path: Path,
    schema: str = NOTES_SCHEMA,
    *,
    name: str = "testworld",
    version: str = "1.0.0",
    **options: Any,
) -> World:
    """A world on a throwaway directory, with the tools the instance tests drive.

    The tools are deliberately generic: `execute` lets a test drive any schema
    through a real `Instance.call`, which is the only way to exercise the
    transaction, the chain and the changeset the way a world does.
    """
    options.setdefault("fixtures_dir", tmp_path / "fixtures")
    options.setdefault("work_dir", tmp_path / "work")
    world = World(name, version, schema, **options)
    register_test_tools(world)
    return world


def register_test_tools(world: World) -> None:
    """The small toolset every world in these tests shares."""

    @world.tool
    def execute(ctx: Ctx, sql: str) -> dict[str, int]:
        """Run one statement for its effect."""
        result = ctx.db.execute(sql)
        return {"rowcount": result.rowcount}

    @world.tool
    def rows(ctx: Ctx, sql: str) -> list[dict[str, Any]]:
        """Run one query and return every row."""
        return ctx.db.rows(sql)

    @world.tool
    def write_then_fail(ctx: Ctx, sql: str) -> dict[str, str]:
        """Write, then fail: what the per-call transaction is for."""
        ctx.db.execute(sql)
        raise Boom("it did not work out")

    @world.tool
    def crash(ctx: Ctx) -> None:
        """Fail with something that is not a `ToolError` at all."""
        raise ValueError("a bug in world code")

    @world.tool
    def mint(ctx: Ctx) -> dict[str, str]:
        """Draw from the instance's seeded id stream."""
        return {"id": ctx.ids.uuid(), "roll": str(ctx.ids.random.random())}

    @world.tool
    def now(ctx: Ctx) -> dict[str, str]:
        """The instance's clock, from Python and from SQL."""
        row = ctx.db.one("SELECT datetime('now') AS sql_now")
        assert row is not None
        return {"python": ctx.clock.iso(), "sql": str(row["sql_now"])}


class Boom(ToolError):
    """A world's own error, as every world defines its own."""

    def __init__(self, message: str) -> None:
        super().__init__("boom", message)


@pytest.fixture
def world(tmp_path: Path) -> World:
    """A world with the notes schema and the shared toolset."""
    return build_world(tmp_path)


@pytest.fixture
def instance(world: World) -> Iterator[Instance]:
    """A blank instance of that world, destroyed when the test ends."""
    with world.instance(None, now=INSTANT_ISO) as live:
        yield live
