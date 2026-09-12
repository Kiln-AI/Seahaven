"""The committed `empty` fixture: the artifact, and the recipe that made it.

A fixture is bytes in the repository, and bytes with no source are bytes nobody
can change. Two things are asserted here: that the committed artifact is intact
and is the one this world's schema belongs to, and that `fixtures_src/generate.py`
still makes it, so the recipe cannot rot beside the file it produced.
"""

import hashlib
import importlib
import importlib.util
import inspect
import shutil
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any

import apsw
import pytest

import seahaven
from conftest import FIXTURE_NOW
from projecttracker.world import world

# `load_all` and `verify` by their module, not through `seahaven`: a name a
# component document's §1 lists as part of a module's interface is public, and
# `components/fixtures_instances.md` §1 lists these. `seahaven/__init__.py`
# re-exports only the subset worth a short import, which is why the rule is
# about the component documents and not about that list -- the same rule under
# which `middleware/error_handler.py` imports `Handler` from `seahaven.world`.
# That the convenience surface is the narrower of the two, and that nothing
# states this anywhere an author reads, is `BACKLOG.md` B10.
from seahaven import fixtures as fixture_files

# Where SQLite stamps the version that wrote a database file: a four-byte big
# endian `SQLITE_VERSION_NUMBER` at offset 96 of the header.
_VERSION_OFFSET = 96


def generate() -> Any:
    """`fixtures_src/generate.py`, imported by path.

    It is not part of the installed package -- it is authoring source that ships
    with the repository and not with the world -- so a test that must work from
    any rootdir names the file. That is *not* what the CLI will do: `--run
    module:function` takes a module path (`architecture.md` §8.6,
    `functional_spec.md` §18), and the module path this file has is
    `fixtures_src.generate`, which
    `test_the_generator_is_reachable_by_the_module_path_its_docstring_names`
    pins.
    """
    path = Path(world.fixtures_dir).parent / "fixtures_src" / "generate.py"
    # A name of this test's own, so nothing reads it as the module path the
    # recipe documents -- that is `fixtures_src.generate`, tested below.
    spec = importlib.util.spec_from_file_location("_generate_loaded_by_path", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_generator_is_reachable_by_the_module_path_its_docstring_names() -> None:
    """`fixtures_src.generate` resolves, because that is the contract Phase 7 will read.

    The generator's own docstring tells an author to freeze with
    `--run fixtures_src.generate:<id>`, and a recipe that names an entry point
    nobody can import is a recipe that will be found broken by the phase that
    builds the CLI. `fixtures_src/` has no `__init__.py`; it resolves as a
    namespace package with the world root on `sys.path`, which is where the CLI
    runs from.
    """
    world_root = Path(world.fixtures_dir).parent
    added = str(world_root) not in sys.path
    if added:
        sys.path.insert(0, str(world_root))
    try:
        for name in ("fixtures_src.generate", "fixtures_src"):
            sys.modules.pop(name, None)
        module = importlib.import_module("fixtures_src.generate")
        assert Path(module.__file__ or "") == world_root / "fixtures_src" / "generate.py"
        # `<module>:<function>` -- both halves, since both are in the command.
        assert callable(module.empty)
    finally:
        for name in ("fixtures_src.generate", "fixtures_src"):
            sys.modules.pop(name, None)
        if added:
            sys.path.remove(str(world_root))


def test_the_world_has_exactly_one_fixture_and_it_is_empty() -> None:
    assert [fixture.id for fixture in world.fixtures()] == ["empty"]


def test_the_fixture_describes_itself_to_whoever_writes_an_eval() -> None:
    """The description is the whole of what a fixture tells an eval author."""
    (fixture,) = world.fixtures()
    assert fixture.description == (
        "The tracker's schema with no rows. Start here to write a history, or to test setup flows."
    )
    assert fixture.now == FIXTURE_NOW
    assert fixture.parent_id is None


def test_the_committed_bytes_are_the_bytes_the_sidecar_records() -> None:
    """`file_sha256`, checked the way instance creation checks it."""
    (fixture,) = world.fixtures()
    fixture_files.verify(fixture)
    digest = hashlib.sha256(fixture.state_path.read_bytes()).hexdigest()
    assert digest == fixture.meta.file_sha256


def test_the_fixture_was_frozen_from_this_worlds_schema() -> None:
    """The conformance check instance creation makes, made here with a name on it.

    A schema change without a regenerated fixture is the commonest way a world
    breaks, and the failure it causes otherwise is at the next `instance()`.
    """
    (fixture,) = world.fixtures()
    assert fixture.meta.schema_hash == world.schema_hash
    assert fixture.meta.world == world.name
    assert fixture.meta.world_version == world.version


def test_the_state_file_carries_no_journal_or_lock_file_beside_it() -> None:
    """Half of what SH405 will check: a fixture is a sealed file, not a live database.

    The other half -- the file's mode -- is deliberately not asserted here, and
    not because it does not matter. `freeze` sets `0o444`, and the framework's
    own suite tests that it does. But git records only the executable bit, so
    the mode of a *committed* fixture is whatever the cloning umask gave it, and
    any assertion about it here would pass in the tree that froze the file and
    fail in every clone. That is `BACKLOG.md` B9, because SH405 as specified
    reports the same thing.
    """
    (fixture,) = world.fixtures()
    assert not list(fixture.dir.glob("state.sqlite-*")), (
        "a fixture is checkpointed and journal-free"
    )
    assert sorted(path.name for path in fixture.dir.iterdir()) == ["fixture.yaml", "state.sqlite"]


def test_an_instance_of_the_fixture_starts_at_the_frozen_instant_with_no_rows(
    tracker: seahaven.Instance,
) -> None:
    assert tracker.clock.iso() == FIXTURE_NOW
    assert tracker.fixture == "empty"
    assert tracker.call("ping") == {"message": "pong", "now": FIXTURE_NOW, "users": 0}


def test_using_the_fixture_does_not_touch_the_committed_file() -> None:
    """A fixture is copied, never opened: writing to an instance cannot damage it."""
    (fixture,) = world.fixtures()
    before = fixture.state_path.read_bytes()
    with world.instance("empty") as instance, instance.bulk() as ctx:
        ctx.db.execute(
            "INSERT INTO users (id, email, name, role, created_at) VALUES ('u', 'a@b.invalid',"
            " 'A', 'admin', '2026-06-01T09:00:00.000Z')"
        )
    assert fixture.state_path.read_bytes() == before


@pytest.fixture
def rebuilt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> tuple[Any, Any]:
    """Run the committed recipe into `tmp_path`, and hand back what it made and what is committed.

    Through `generate.main`, not through a copy of what it does: a test that
    rebuilt the fixture its own way would pass while the committed recipe rotted.
    The world's fixtures directory is redirected for the duration so the rebuild
    lands in `tmp_path` -- `freeze` refuses to overwrite, and nothing may write
    into the repository from a test.
    """
    # Resolved before the redirect, because afterwards `world.fixtures()` is
    # the rebuild. The `Fixture` holds absolute paths, so it stays readable.
    (committed,) = world.fixtures()
    module = generate()

    rebuilt_dir = tmp_path / "fixtures"
    rebuilt_dir.mkdir()
    monkeypatch.setattr(world, "fixtures_dir", rebuilt_dir)
    assert module.main([]) == 0

    printed = capsys.readouterr()
    assert f"froze empty at {FIXTURE_NOW}" in printed.out

    (made,) = world.fixtures()
    assert made.dir == rebuilt_dir / "empty"
    return made, committed


def test_the_generator_still_makes_the_fixture_that_is_committed(
    rebuilt: tuple[Any, Any],
) -> None:
    """What the recipe makes today has the same schema, rows, clock and description.

    True on any SQLite build, which is why it is the test that must always run.
    The bytes are the test next door.
    """
    made, committed = rebuilt
    assert made.meta.schema_hash == committed.meta.schema_hash
    assert made.now == committed.now
    assert made.description == committed.description
    assert made.parent_id == committed.parent_id
    assert _dump(made.state_path) == _dump(committed.state_path)


def test_the_generator_still_makes_the_committed_fixture_byte_for_byte(
    rebuilt: tuple[Any, Any],
) -> None:
    """And on the build that wrote the committed file, the same bytes.

    `VACUUM INTO` is byte-reproducible on one build, and its output carries the
    version that made it in the header, so the comparison can ask rather than
    assume. The ask is the first thing here rather than the last: a run that
    verified everything it could and then called `pytest.skip` would report as
    "not run", which is not what happened. Splitting the byte comparison out is
    what lets the content test above be reported as the pass it is.
    """
    made, committed = rebuilt
    committed_bytes = committed.state_path.read_bytes()
    wrote = _sqlite_version_of(committed_bytes)
    if wrote != apsw.SQLITE_VERSION_NUMBER:
        pytest.skip(
            f"the committed fixture was written by SQLite {wrote}, "
            f"not {apsw.SQLITE_VERSION_NUMBER}: bytes cannot be compared, and "
            "test_the_generator_still_makes_the_fixture_that_is_committed "
            "compares the content"
        )
    assert made.state_path.read_bytes() == committed_bytes, (
        "the generator no longer reproduces the committed fixture; "
        "regenerate it and commit the new bytes"
    )


def test_the_generator_run_as_a_script_refuses_to_overwrite_a_committed_fixture() -> None:
    """The way a fixture is built by hand today, driven as a script.

    `empty` is already in the repository, so the run must fail and change
    nothing: a fixture is immutable, and rebuilding one means deleting it
    deliberately first. This is also the only thing that runs the module's
    `__main__` block, which is what the instructions in its docstring tell an
    author to use.
    """
    script = Path(world.fixtures_dir).parent / "fixtures_src" / "generate.py"
    before = sorted(path.name for path in world.fixtures_dir.iterdir())

    done = subprocess.run([sys.executable, str(script), "empty"], capture_output=True, text=True)

    assert done.returncode != 0
    assert "already exists" in done.stderr
    assert sorted(path.name for path in world.fixtures_dir.iterdir()) == before


def test_the_generators_functions_say_in_their_types_what_a_builder_is() -> None:
    """`--run module:function` has to find a real signature at the end of it.

    The recipe's contract is its two signatures: a builder takes a live instance
    and returns nothing, and `build` hands back the frozen fixture. They are
    annotations, so nothing at runtime evaluates them on its own -- this asks
    them to resolve, which is what a reader, `ty`, and Phase 7's CLI all assume
    they do.
    """
    module = generate()

    assert inspect.get_annotations(module.empty, eval_str=True) == {
        "inst": seahaven.Instance,
        "return": None,
    }
    assert inspect.get_annotations(module.build, eval_str=True) == {
        "fixture_id": str,
        "return": seahaven.Fixture,
    }


def test_the_generator_run_as_a_script_builds_the_world_it_lives_beside(tmp_path: Path) -> None:
    """From a checkout, the script uses that checkout's world, installed or not.

    Its docstring tells an author to run `python .../generate.py <id>` from a
    clone, where the world is source on disk and not a package on the path. So
    the module puts its own `src/` first, and this proves it does: the copy here
    calls itself version 9.9.9, and that is the version that has to reach the
    sidecar. An installed `projecttracker` is on this interpreter's path the
    whole time, and it is the wrong one.
    """
    source = Path(world.fixtures_dir).parent
    checkout = tmp_path / "projecttracker"
    (checkout / "src").mkdir(parents=True)
    (checkout / "fixtures").mkdir()
    (checkout / "fixtures_src").mkdir()
    # The nearest `pyproject.toml` above the package is what `fixtures/` hangs
    # off, so the copy needs one for the fixture to land where a checkout's does.
    (checkout / "pyproject.toml").write_text('[project]\nname = "projecttracker"\n')
    shutil.copytree(source / "src" / "projecttracker", checkout / "src" / "projecttracker")
    shutil.copy(source / "fixtures_src" / "generate.py", checkout / "fixtures_src" / "generate.py")
    world_py = checkout / "src" / "projecttracker" / "world.py"
    world_py.write_text(world_py.read_text().replace('version="1.0.0"', 'version="9.9.9"'))

    done = subprocess.run(
        [sys.executable, str(checkout / "fixtures_src" / "generate.py"), "empty"],
        capture_output=True,
        text=True,
    )

    assert done.returncode == 0, done.stderr
    built = fixture_files.load_all(checkout / "fixtures")
    assert built["empty"].meta.world_version == "9.9.9"
    assert built["empty"].now == FIXTURE_NOW


def test_the_generator_freezes_what_its_builder_wrote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A builder's writes reach the frozen file. `empty`'s builder writes nothing.

    That is the whole contract the fixtures of Phase 10 rest on, and `empty`
    alone cannot show it: its builder is empty, so a `build` that never called
    the builder would produce exactly the same fixture. A builder that does
    write is registered here, taken through the same `build`, and the row is
    looked for in the instance the fixture makes.
    """
    module = generate()

    def one_user(inst: seahaven.Instance) -> None:
        with inst.bulk() as ctx:
            ctx.db.execute(
                "INSERT INTO users (id, email, name, role, created_at) VALUES (?, ?, ?, ?, ?)",
                ctx.ids.uuid(),
                "founder@example.invalid",
                "Founder",
                "admin",
                ctx.clock.iso(),
            )

    monkeypatch.setitem(module.BUILDERS, "one_user", one_user)
    monkeypatch.setitem(module.DESCRIPTIONS, "one_user", "One user, for a test.")
    monkeypatch.setattr(world, "fixtures_dir", tmp_path / "fixtures")
    (tmp_path / "fixtures").mkdir()

    fixture = module.build("one_user")

    assert fixture.description == "One user, for a test."
    assert fixture.now == FIXTURE_NOW
    with world.instance("one_user") as instance:
        assert instance.call("ping") == {"message": "pong", "now": FIXTURE_NOW, "users": 1}


def test_the_generator_refuses_a_fixture_it_does_not_know(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The CLI is Phase 7's; until then `main` is how a fixture is built by hand."""
    assert generate().main(["no_such_fixture"]) == 1
    printed = capsys.readouterr()
    assert printed.out == ""
    assert "no such fixture: no_such_fixture" in printed.err
    assert "known fixtures: empty" in printed.err


def _dump(state: Path) -> list[Any]:
    """Everything in a database file that is not the file's own layout.

    The schema and every row of every table, which is what "the same fixture"
    means on a SQLite build that lays its pages out differently.
    """
    connection = apsw.Connection(
        f"file:{state}?mode=ro", flags=apsw.SQLITE_OPEN_READONLY | apsw.SQLITE_OPEN_URI
    )
    try:
        schema = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_schema ORDER BY type, name"
        ).fetchall()
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
                " AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        rows = [
            (table, connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall())
            for table in tables
        ]
        return [schema, rows]
    finally:
        connection.close()


def _sqlite_version_of(database: bytes) -> int:
    """The `SQLITE_VERSION_NUMBER` SQLite stamped into the file header."""
    return struct.unpack(">I", database[_VERSION_OFFSET : _VERSION_OFFSET + 4])[0]
