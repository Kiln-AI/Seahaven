"""Shared fixtures: a frozen instant and a hardened instance database on it."""

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from seahaven.clock import Clock
from seahaven.ctx import Ctx, InstanceInfo
from seahaven.db import Db, open_instance
from seahaven.ids import Ids, instance_seed

# Milliseconds on purpose: a clock whose instant is a whole second hides the
# rounding mistakes that a world's canonical timestamps would trip over.
INSTANT = datetime(2024, 3, 5, 12, 0, 0, 123000, tzinfo=UTC)


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
