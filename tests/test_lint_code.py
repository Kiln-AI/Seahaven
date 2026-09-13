"""The world-code rules: what a wall clock, a coin flip and a silent tool look like.

Two shapes of test. The two call rules are exercised over one-module worlds
written into `tmp_path`, because what is being tested is the reading of a line of
Python and there are a lot of lines to read. SH205 is exercised over a real
registered tool, because a description is a property of the registry and not of
the source.
"""

from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest

from seahaven.cli import discover
from seahaven.ctx import Ctx
from seahaven.errors import WorldBug
from seahaven.lint import Finding, Target
from seahaven.lint import code as code_lint
from seahaven.world import World
from tests.conftest import NOTES_SCHEMA, WORLDS, build_world, stub_target

pytestmark = pytest.mark.usefixtures("isolated_imports")


def findings(tmp_path: Path, source: str, *, module: str = "tools/notes.py") -> list[Finding]:
    """Every code finding for one module of a world that has no tools."""
    package_dir = tmp_path / "pkg"
    path = package_dir / module
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    world = World(
        "linted",
        "1.0.0",
        NOTES_SCHEMA,
        fixtures_dir=tmp_path / "fixtures",
        work_dir=tmp_path / "work",
    )
    return code_lint.run(stub_target(world, package_dir))


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("from datetime import datetime\nx = datetime.now()\n", id="datetime.now"),
        pytest.param("import datetime\nx = datetime.datetime.now()\n", id="datetime.datetime.now"),
        pytest.param("from datetime import datetime\nx = datetime.utcnow()\n", id="utcnow"),
        pytest.param("from datetime import date\nx = date.today()\n", id="date.today"),
        pytest.param("import time\nx = time.time()\n", id="time.time"),
        pytest.param("import time\nx = time.monotonic()\n", id="time.monotonic"),
        pytest.param("import time\nx = time.perf_counter()\n", id="time.perf_counter"),
    ],
)
def test_every_wall_clock_read_is_sh201(tmp_path: Path, source: str) -> None:
    (finding,) = findings(tmp_path, source)
    assert finding.code == "SH201"
    assert finding.severity == "warning"
    assert "ctx.clock" in finding.fix


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(
            "from datetime import datetime as dt\nx = dt.now()\n", id="datetime renamed on import"
        ),
        pytest.param("import time as clock\nx = clock.time()\n", id="time renamed on import"),
        pytest.param("from time import time\nx = time()\n", id="the function imported by name"),
    ],
)
def test_a_wall_clock_read_through_an_alias_is_still_sh201(tmp_path: Path, source: str) -> None:
    """A rule that only sees one spelling is a rule an author steps around by accident."""
    (finding,) = findings(tmp_path, source)
    assert finding.code == "SH201"


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("def t(ctx):\n    return ctx.clock.iso()\n", id="ctx.clock.iso"),
        pytest.param("def t(ctx):\n    return ctx.clock.now()\n", id="ctx.clock.now"),
        pytest.param("def t(ctx):\n    return ctx.ids.uuid()\n", id="ctx.ids.uuid"),
        pytest.param("def t(ctx):\n    return ctx.ids.random.random()\n", id="ctx.ids.random"),
    ],
)
def test_the_endorsed_spellings_are_clean(tmp_path: Path, source: str) -> None:
    """`ctx.clock.now()` is the instance's clock and must never read as the machine's."""
    assert findings(tmp_path, source) == []


def test_a_wall_clock_read_in_middleware_is_not_reported(tmp_path: Path) -> None:
    """Timing a call is a real wall clock doing a real job."""
    source = "import time\nx = time.perf_counter()\n"
    assert findings(tmp_path, source, module="middleware/timing.py") == []
    assert findings(tmp_path, source, module="middleware/deeper/timing.py") == []


def test_a_wall_clock_read_outside_middleware_is_reported(tmp_path: Path) -> None:
    """The exemption is the directory, not the file name."""
    source = "import time\nx = time.perf_counter()\n"
    assert [f.code for f in findings(tmp_path, source, module="helpers/timing.py")] == ["SH201"]


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("import random\n", id="import random"),
        pytest.param("from random import choice\n", id="from random import"),
        pytest.param("import random as r\n", id="import random renamed"),
    ],
)
def test_importing_random_is_sh203(tmp_path: Path, source: str) -> None:
    (finding,) = findings(tmp_path, source)
    assert finding.code == "SH203"
    assert "ctx.ids" in finding.fix


def test_a_call_into_random_is_sh203(tmp_path: Path) -> None:
    """The import and the call are both reported: the fix is the same for both."""
    codes = [f.code for f in findings(tmp_path, "import random\nx = random.random()\n")]
    assert codes == ["SH203", "SH203"]


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("import uuid\nx = uuid.uuid4()\n", id="uuid4"),
        pytest.param("import uuid\nx = uuid.uuid1()\n", id="uuid1"),
        pytest.param("from uuid import uuid4\nx = uuid4()\n", id="uuid4 imported by name"),
    ],
)
def test_uuid_from_the_entropy_pool_is_sh203(tmp_path: Path, source: str) -> None:
    (finding,) = findings(tmp_path, source)
    assert finding.code == "SH203"


def test_importing_uuid_alone_is_not_a_finding(tmp_path: Path) -> None:
    """`uuid.UUID` is how `ctx.ids.uuid()`'s own output is parsed."""
    assert findings(tmp_path, "import uuid\nx = uuid.UUID(int=0, version=4)\n") == []


def test_a_module_that_does_not_parse_is_left_to_the_import_error(tmp_path: Path) -> None:
    assert findings(tmp_path, "def broken(:\n") == []


def test_a_tool_with_no_description_is_sh205(tmp_path: Path) -> None:
    world = build_world(tmp_path)

    @world.tool(description="")
    def silent(ctx: Ctx) -> None:
        """Registered with an empty description."""

    found = [f for f in code_lint.run(stub_target(world, tmp_path)) if f.code == "SH205"]
    assert len(found) == 1
    assert "silent" in found[0].message
    assert found[0].severity == "warning"
    assert "docstring" in found[0].fix


def test_a_tool_with_a_docstring_is_clean(tmp_path: Path) -> None:
    """Every tool in the shared toolset has one, and none of them is reported."""
    world = build_world(tmp_path)
    assert [f for f in code_lint.run(stub_target(world, tmp_path)) if f.code == "SH205"] == []


def test_a_control_tool_is_never_sh205(tmp_path: Path) -> None:
    """The framework's own two are never listed and never reach an agent.

    Both have descriptions, so the only way to ask whether `control` is what
    excludes them is to put one without a description in the registry, which
    `world.tool` refuses by name. Hence the private dict: the alternative is a
    branch nothing exercises.
    """
    world = build_world(tmp_path)
    silent = replace(world.tools["controller_run_sql"], description="")
    world._tools[silent.name] = silent
    assert [f for f in code_lint.run(stub_target(world, tmp_path)) if f.code == "SH205"] == []


def test_sh205_points_at_the_function(tmp_path: Path) -> None:
    world = build_world(tmp_path)

    @world.tool(description="")
    def silent(ctx: Ctx) -> None:
        """Registered with an empty description."""

    (found,) = [f for f in code_lint.run(stub_target(world, tmp_path)) if f.code == "SH205"]
    assert found.path == Path(__file__)
    assert found.line is not None


def test_the_tidy_world_has_no_code_findings() -> None:
    found = discover(None, WORLDS / "tidy")
    assert code_lint.run(Target(found.world, found.package, found.imported)) == []


def test_the_messy_world_has_every_code_finding() -> None:
    """A wall clock, `uuid4`, `random` twice and an undescribed tool, none from middleware."""
    found = discover(None, WORLDS / "messy")
    reported = code_lint.run(Target(found.world, found.package, found.imported))
    assert sorted({f.code for f in reported}) == ["SH201", "SH203", "SH205"]
    # `middleware/timing.py` reads `time.perf_counter()` twice and is exempt.
    assert not any("timing" in f.path.name for f in reported)
    # `tools/orphan.py` is never imported, but it is still world code on disk.
    assert not any(f.code == "SH201" and "orphan" in f.path.name for f in reported)


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("def t(self):\n    return self.time.time()\n", id="an attribute named time"),
        pytest.param("def t(clocks):\n    return clocks.date.today()\n", id="one named date"),
    ],
)
def test_a_chain_that_merely_ends_in_a_wall_clock_name_is_not_sh201(
    tmp_path: Path, source: str
) -> None:
    """Resolving the root is the point; matching the tail would undo it."""
    assert findings(tmp_path, source) == []


def test_a_relative_import_of_the_worlds_own_random_is_not_sh203(tmp_path: Path) -> None:
    """A world may have a `random.py` of its own, and `.random` is not the stdlib's."""
    assert findings(tmp_path, "from .random import seeded\n") == []


def test_the_stdlib_random_is_still_sh203_beside_it(tmp_path: Path) -> None:
    assert [f.code for f in findings(tmp_path, "from random import choice\n")] == ["SH203"]


def test_a_world_that_is_one_module_is_refused_by_the_target(tmp_path: Path) -> None:
    """A `Target` built by hand gets a sentence, never an `AttributeError`."""
    module = ModuleType("flat")
    world = build_world(tmp_path)
    with pytest.raises(WorldBug, match="a world is a package"):
        _ = Target(world=world, package=module, imported=frozenset()).package_dir
