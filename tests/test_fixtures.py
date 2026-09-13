"""Fixtures: what `freeze` writes, what `load` refuses, and what `verify` catches."""

import hashlib
import os
import stat as stat_module
from pathlib import Path
from typing import Any

import apsw
import pytest
import yaml

from seahaven import fixtures
from seahaven.errors import WorldBug
from seahaven.fixtures import SIDECAR_NAME, STATE_NAME, Fixture, load, load_all, verify
from seahaven.instances import Instance
from seahaven.world import World
from tests.conftest import INSTANT_ISO, build_world


def add(instance: Instance, id: str, body: str = "a body") -> None:
    instance.call("execute", sql=f"INSERT INTO notes VALUES ('{id}', '{body}', 0)")


def contents(directory: Path) -> list[str]:
    """What a directory holds, for a directory that may never have been made."""
    return sorted(path.name for path in directory.iterdir()) if directory.is_dir() else []


def sidecar_of(fixture: Fixture) -> dict[str, Any]:
    return yaml.safe_load((fixture.dir / SIDECAR_NAME).read_text())


def test_freeze_writes_a_directory_with_a_state_file_and_a_sidecar(world: World) -> None:
    with world.instance(None, now=INSTANT_ISO) as instance:
        add(instance, "n1")

        fixture = instance.freeze("start", "One note, for the tests that need one.")

    assert fixture.dir == world.fixtures_dir / "start"
    assert sorted(path.name for path in fixture.dir.iterdir()) == [SIDECAR_NAME, STATE_NAME]
    assert fixture.id == "start"
    assert fixture.now == INSTANT_ISO
    assert fixture.description == "One note, for the tests that need one."
    assert fixture.parent_id is None


def test_the_sidecar_carries_every_field(world: World) -> None:
    with world.instance(None, now=INSTANT_ISO) as instance:
        fixture = instance.freeze("start", "Empty.")

    written = sidecar_of(fixture)

    assert written == {
        "format_version": 1,
        "id": "start",
        "world": world.name,
        "world_version": world.version,
        "schema_hash": world.schema_hash,
        "now": INSTANT_ISO,
        "parent_id": None,
        "file_sha256": hashlib.sha256(fixture.state_path.read_bytes()).hexdigest(),
        "created_at": written["created_at"],
        "description": "Empty.",
    }
    # The wall clock, not the instance's: when the fixture was made, not what
    # time it is inside it.
    assert written["created_at"] != INSTANT_ISO
    assert written["created_at"].endswith("Z")


def test_the_frozen_file_is_read_only_and_journal_free(world: World) -> None:
    with world.instance(None) as instance:
        add(instance, "n1")
        fixture = instance.freeze("start", "One note.")

    mode = stat_module.S_IMODE(fixture.state_path.stat().st_mode)
    frozen = apsw.Connection(str(fixture.state_path), flags=apsw.SQLITE_OPEN_READONLY)
    try:
        assert frozen.pragma("journal_mode") == "delete"
        assert frozen.execute("SELECT count(*) FROM notes").get == 1
    finally:
        frozen.close()
    assert mode == 0o444


def test_the_frozen_file_is_compacted(world: World) -> None:
    """`VACUUM INTO`, not a copy: the free pages of a churned database do not travel."""
    with world.instance(None) as instance:
        for number in range(500):
            add(instance, f"n{number}", "a body long enough to take up a page or two" * 8)
        instance.call("execute", sql="DELETE FROM notes")
        live_size = instance.state_path.stat().st_size

        fixture = instance.freeze("start", "Nothing left.")

    assert fixture.state_path.stat().st_size < live_size


def test_freezing_the_same_content_twice_gives_the_same_bytes(world: World) -> None:
    """Determinism on one build: identical content, identical file."""
    with world.instance(None) as instance:
        add(instance, "n1")

        first = instance.freeze("one", "One note.")
        second = instance.freeze("two", "One note.")

    assert first.state_path.read_bytes() == second.state_path.read_bytes()


def test_an_instance_can_be_made_from_what_freeze_wrote(world: World) -> None:
    with world.instance(None, now=INSTANT_ISO) as instance:
        add(instance, "n1", "frozen")
        instance.freeze("start", "One note.")
        # And the instance is still usable: `VACUUM INTO` leaves it alone.
        add(instance, "n2", "after the freeze")

    with world.instance("start") as forked:
        assert forked.inspect().rows("SELECT id FROM notes") == [{"id": "n1"}]
        assert forked.clock.iso() == INSTANT_ISO


def test_freeze_refuses_an_id_that_already_exists(world: World) -> None:
    with world.instance(None) as instance:
        instance.freeze("start", "Empty.")

        with pytest.raises(WorldBug, match="already exists"):
            instance.freeze("start", "Empty again.")

    assert contents(world.fixtures_dir) == ["start"]


@pytest.mark.parametrize("bad", ["", ".", "..", "a/b", "/abs", ".hidden", "a/../b", "a\x00b"])
def test_freeze_refuses_an_id_that_is_not_a_directory_name(world: World, bad: str) -> None:
    with world.instance(None) as instance, pytest.raises(WorldBug, match="not a fixture id"):
        instance.freeze(bad, "Empty.")

    assert contents(world.fixtures_dir) == []


def test_freeze_refuses_an_instance_that_no_longer_holds_the_schema(world: World) -> None:
    with world.instance(None) as instance:
        instance.call("execute", sql="CREATE TABLE extra (id TEXT PRIMARY KEY) STRICT")

        with pytest.raises(WorldBug, match="extra"):
            instance.freeze("start", "Empty.")

    assert contents(world.fixtures_dir) == []


def test_a_failure_part_way_through_leaves_nothing_behind(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The publish is the rename: a half-written fixture is never a fixture."""

    def explode(*_args: object, **_kwargs: object) -> str:
        raise OSError("the disk is full")

    monkeypatch.setattr(fixtures.yaml, "safe_dump", explode)
    with world.instance(None) as instance, pytest.raises(OSError, match="disk is full"):
        instance.freeze("start", "Empty.")

    assert contents(world.fixtures_dir) == []


def test_freeze_refuses_to_run_inside_bulk(world: World) -> None:
    """`VACUUM` cannot run in a transaction, and a fixture of uncommitted rows is not one."""
    with world.instance(None) as instance:
        with instance.bulk() as ctx:
            ctx.db.execute("INSERT INTO notes VALUES ('n1', 'a body', 0)")

            with pytest.raises(WorldBug, match="bulk"):
                instance.freeze("start", "One note.")

        assert contents(world.fixtures_dir) == []
        # And afterwards, with the rows committed, it is the ordinary thing again.
        frozen = instance.freeze("start", "One note.")

    assert frozen.id == "start"


def test_a_state_file_that_cannot_be_written_is_a_world_bug(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full disk or an unwritable fixtures directory reads like every other refusal here."""
    monkeypatch.setattr(fixtures, "STATE_NAME", "no/such/directory/state.sqlite")

    with world.instance(None) as instance, pytest.raises(WorldBug, match="could not write fixture"):
        instance.freeze("start", "Empty.")

    assert contents(world.fixtures_dir) == []


def test_load_reads_a_sidecar_without_opening_the_state_file(world: World) -> None:
    with world.instance(None, now=INSTANT_ISO) as instance:
        frozen = instance.freeze("start", "One note.")
    frozen.state_path.chmod(0o000)

    loaded = load(frozen.dir)

    assert loaded.meta == frozen.meta


@pytest.mark.parametrize(
    ("broken", "message"),
    [
        (None, "cannot be read"),
        ("{{{ not yaml", "not valid YAML"),
        ("- a list", "not a mapping"),
        ("format_version: 2\nid: start\n", "format_version is 2"),
        ("id: start\n", "format_version is None"),
    ],
)
def test_load_refuses_a_broken_sidecar_naming_the_file(
    tmp_path: Path, broken: str | None, message: str
) -> None:
    fixture_dir = tmp_path / "broken"
    fixture_dir.mkdir()
    if broken is not None:
        (fixture_dir / SIDECAR_NAME).write_text(broken)

    with pytest.raises(WorldBug) as raised:
        load(fixture_dir)

    assert message in str(raised.value)
    assert str(fixture_dir / SIDECAR_NAME) in str(raised.value)


def test_load_refuses_a_sidecar_with_a_missing_or_unknown_field(world: World) -> None:
    with world.instance(None) as instance:
        frozen = instance.freeze("start", "Empty.")
    written = sidecar_of(frozen)

    (frozen.dir / SIDECAR_NAME).write_text(yaml.safe_dump({**written, "extra": "field"}))
    with pytest.raises(WorldBug, match="not a fixture sidecar"):
        load(frozen.dir)

    (frozen.dir / SIDECAR_NAME).write_text(
        yaml.safe_dump({k: v for k, v in written.items() if k != "schema_hash"})
    )
    with pytest.raises(WorldBug, match="not a fixture sidecar"):
        load(frozen.dir)


def test_verify_passes_the_file_freeze_wrote(world: World) -> None:
    with world.instance(None) as instance:
        frozen = instance.freeze("start", "Empty.")

    verify(frozen)


def test_verify_refuses_a_modified_file_and_says_to_fork(world: World) -> None:
    with world.instance(None) as instance:
        frozen = instance.freeze("start", "Empty.")
    frozen.state_path.chmod(0o644)
    frozen.state_path.write_bytes(b"not a database at all")

    with pytest.raises(WorldBug) as raised:
        verify(frozen)

    assert "start" in str(raised.value)
    assert "fork it" in str(raised.value)


def test_verify_caches_a_good_fixture_and_re_reads_a_changed_one(world: World) -> None:
    """Cached on (path, mtime, size, hash): hashing every copy would be the cost of a copy."""
    with world.instance(None) as instance:
        frozen = instance.freeze("start", "Empty.")
    verify(frozen)
    before = frozen.state_path.stat()

    frozen.state_path.chmod(0o644)
    frozen.state_path.write_bytes(b"x" * before.st_size)
    os.utime(frozen.state_path, ns=(before.st_atime_ns, before.st_mtime_ns))
    verify(frozen)  # the file looks untouched, so it is not hashed again

    os.utime(frozen.state_path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
    with pytest.raises(WorldBug, match="does not match"):
        verify(frozen)


def test_verify_refuses_a_fixture_with_no_state_file(world: World) -> None:
    with world.instance(None) as instance:
        frozen = instance.freeze("start", "Empty.")
    frozen.state_path.unlink()

    with pytest.raises(WorldBug, match=STATE_NAME):
        verify(frozen)


def test_load_all_finds_every_fixture_by_id(world: World) -> None:
    with world.instance(None) as instance:
        instance.freeze("one", "First.")
        instance.freeze("two", "Second.")

    found = load_all(world.fixtures_dir)

    assert sorted(found) == ["one", "two"]
    assert found["two"].description == "Second."


def test_load_all_skips_dot_directories_and_plain_files(world: World) -> None:
    """A `.pending-` fixture is a freeze in flight, and is invisible to a listing."""
    with world.instance(None) as instance:
        frozen = instance.freeze("one", "First.")
    pending = world.fixtures_dir / ".pending-two"
    pending.mkdir()
    (pending / SIDECAR_NAME).write_text("not even yaml {{{")
    (world.fixtures_dir / "README.md").write_text("fixtures live here")

    assert list(load_all(world.fixtures_dir)) == [frozen.id]


def test_load_all_refuses_two_fixtures_claiming_one_id(world: World) -> None:
    with world.instance(None) as instance:
        frozen = instance.freeze("one", "First.")
    impostor = world.fixtures_dir / "two"
    impostor.mkdir()
    (impostor / SIDECAR_NAME).write_text((frozen.dir / SIDECAR_NAME).read_text())

    with pytest.raises(WorldBug, match="claim the id 'one'"):
        load_all(world.fixtures_dir)


def test_load_all_of_a_directory_that_does_not_exist_is_empty(tmp_path: Path) -> None:
    assert load_all(tmp_path / "nothing here") == {}


def test_world_fixtures_lists_them_by_id(world: World) -> None:
    with world.instance(None) as instance:
        instance.freeze("two", "Second.")
        instance.freeze("one", "First.")

    assert [fixture.id for fixture in world.fixtures()] == ["one", "two"]


def test_a_fork_chain_of_three_keeps_its_parents(world: World) -> None:
    with world.instance(None) as instance:
        add(instance, "n1")
        instance.freeze("first", "One note.")
    with world.instance("first") as instance:
        add(instance, "n2")
        instance.freeze("second", "Two notes.")
    with world.instance("second") as instance:
        add(instance, "n3")
        third = instance.freeze("third", "Three notes.")

    fixtures_by_id = load_all(world.fixtures_dir)

    assert fixtures_by_id["first"].parent_id is None
    assert fixtures_by_id["second"].parent_id == "first"
    assert third.parent_id == "second"
    with world.instance("third") as instance:
        assert instance.inspect().rows("SELECT count(*) AS c FROM notes") == [{"c": 3}]


def test_the_fixtures_directory_is_made_on_the_first_freeze(tmp_path: Path) -> None:
    """A world that never freezes never touches the directory at all."""
    world = build_world(tmp_path, fixtures_dir=tmp_path / "deep" / "fixtures")
    assert not world.fixtures_dir.exists()

    with world.instance(None) as instance:
        instance.freeze("start", "Empty.")

    assert (world.fixtures_dir / "start" / STATE_NAME).is_file()


def test_freeze_replaces_a_pending_directory_a_crash_left_behind(world: World) -> None:
    """`.pending-<id>` is scratch space, and a process that died mid-freeze must not own it."""
    stale = world.fixtures_dir / ".pending-start"
    stale.mkdir(parents=True)
    (stale / STATE_NAME).write_bytes(b"half a database")

    with world.instance(None) as instance:
        add(instance, "n1")
        fixture = instance.freeze("start", "One note.")

    assert contents(world.fixtures_dir) == ["start"]
    verify(fixture)
