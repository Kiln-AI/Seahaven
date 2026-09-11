"""Shared fixtures: a frozen instant and a hardened instance database on it."""

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from seahaven.clock import Clock
from seahaven.db import Db, open_instance

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
