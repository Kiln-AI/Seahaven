"""What every test of this world starts from: the two instants, and a probe world.

A live instance comes from Seahaven's own pytest plugin: `@pytest.mark.seahaven`
says which fixture it starts from and the `instance` fixture makes it, so nothing
here builds one. That is the same path an eval takes -- `world.instance(...)`
then `instance.call(...)` -- and it is the only one that proves the chain, the
transaction and the serialiser are wired up. Nothing here calls a tool function
directly.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import seahaven
from projecttracker.middleware.error_handler import error_handler
from projecttracker.world import world

# The instant every fixture of this world is frozen at (`fixtures_src/generate.py`).
FIXTURE_NOW = "2026-06-01T09:00:00.000Z"

# A blank instance's clock in the tests that do not use a fixture. Deliberately
# not `FIXTURE_NOW`, so a test asserting one cannot pass because of the other.
BLANK_NOW = "2026-07-04T12:30:45.678Z"


@pytest.fixture
def probe(tmp_path: Path) -> Callable[..., seahaven.World]:
    """Builds a throwaway world carrying this world's real error handler.

    The handler's job is to map what the tools *below* it raise, and the
    placeholder has one tool, which raises nothing. Rather than test the
    middleware as a function with a stub for `next_` -- which proves it maps an
    exception, not that it maps one raised by a tool inside a real chain -- each
    case registers a tool that fails the way a real one would on a world of its
    own, and drives it through `Instance.call`.

    The world is this world's schema and this world's middleware; only the tools
    are the test's. Registering them on the real `world` would add them to the
    world every other test and every later phase sees, and registration is for
    the life of the process.
    """

    def build(*tools: Any, name: str = "probe", schema: str = "") -> seahaven.World:
        built = seahaven.World(
            name,
            world.version,
            world.schema + schema,
            fixtures_dir=tmp_path / "fixtures",
            work_dir=tmp_path / "work",
        )
        built.middleware(error_handler)
        for tool in tools:
            built.tool(tool)
        return built

    return build
