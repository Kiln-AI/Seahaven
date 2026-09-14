"""The fixture rules, over fixtures that were really frozen and then damaged.

A hand-written sidecar beside a hand-written file would test the reading and not
the rule: what these rules are for is the drift between a fixture and what its
sidecar says about it, and the only way to have that honestly is to freeze one
and then change something.

SH405 is the journal companions only, and `test_a_writable_state_file_is_not_a_finding`
is why: see the module docstring of `seahaven/lint/fixtures.py`.
"""

import os
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from seahaven.fixtures import SIDECAR_NAME, STATE_NAME
from seahaven.lint import Finding
from seahaven.lint import fixtures as fixtures_lint
from seahaven.world import World
from tests.conftest import build_world, stub_target

WRITABLE = 0o644


@pytest.fixture
def frozen(tmp_path: Path) -> World:
    """A world with one fixture in it, frozen the way `seahaven fixture` does."""
    world = build_world(tmp_path)
    with world.instance(None, now="2026-06-01T09:00:00.000Z") as instance:
        instance.freeze("empty", "The schema with no rows.")
    return world


def run(world: World) -> list[Finding]:
    return fixtures_lint.run(stub_target(world, world.fixtures_dir))


def damage(world: World, edit: Callable[[dict[str, object]], object]) -> None:
    """Rewrite a fixture's sidecar, the way a hand edit would."""
    sidecar = world.fixtures_dir / "empty" / SIDECAR_NAME
    data = yaml.safe_load(sidecar.read_text(encoding="utf-8"))
    edit(data)
    sidecar.write_text(yaml.safe_dump(data, sort_keys=True), encoding="utf-8")


def test_a_freshly_frozen_fixture_is_clean(frozen: World) -> None:
    assert run(frozen) == []


def test_a_world_with_no_fixtures_directory_is_clean(tmp_path: Path) -> None:
    """A world that only ever makes blank instances never mints one."""
    assert run(build_world(tmp_path)) == []


def test_a_sidecar_that_is_not_yaml_is_sh401(frozen: World) -> None:
    (frozen.fixtures_dir / "empty" / SIDECAR_NAME).write_text("{[", encoding="utf-8")
    (finding,) = run(frozen)
    assert finding.code == "SH401"
    assert "not valid YAML" in finding.message


def test_a_sidecar_that_is_not_a_mapping_is_sh401(frozen: World) -> None:
    (frozen.fixtures_dir / "empty" / SIDECAR_NAME).write_text("- one\n- two\n", encoding="utf-8")
    (finding,) = run(frozen)
    assert finding.code == "SH401"
    assert "not a mapping" in finding.message


def test_a_missing_sidecar_is_sh401(frozen: World) -> None:
    (frozen.fixtures_dir / "empty" / SIDECAR_NAME).unlink()
    (finding,) = run(frozen)
    assert finding.code == "SH401"


def test_a_sidecar_missing_a_field_lists_pydantics_error(frozen: World) -> None:
    damage(frozen, lambda data: data.pop("world_version"))
    (finding,) = run(frozen)
    assert finding.code == "SH401"
    assert "world_version" in finding.message


def test_a_format_version_this_seahaven_does_not_write_is_sh401(frozen: World) -> None:
    """The field that says this fixture was written by another Seahaven."""
    damage(frozen, lambda data: data.__setitem__("format_version", 3))
    (finding,) = run(frozen)
    assert finding.code == "SH401"
    assert "format_version" in finding.message


def test_a_broken_sidecar_is_reported_once_and_left(frozen: World) -> None:
    """Every other rule reads a field the sidecar does not have."""
    damage(frozen, lambda data: data.clear())
    assert {finding.code for finding in run(frozen)} == {"SH401"}


def test_a_modified_state_file_is_sh402(frozen: World) -> None:
    state = frozen.fixtures_dir / "empty" / STATE_NAME
    state.chmod(WRITABLE)
    state.write_bytes(state.read_bytes() + b"tampered")
    assert [finding.code for finding in run(frozen)] == ["SH402"]


def test_a_missing_state_file_is_sh402(frozen: World) -> None:
    (frozen.fixtures_dir / "empty" / STATE_NAME).unlink()
    (finding,) = run(frozen)
    assert finding.code == "SH402"
    assert f"no {STATE_NAME}" in finding.message


def test_a_schema_hash_from_another_world_is_sh403(frozen: World) -> None:
    damage(frozen, lambda data: data.__setitem__("schema_hash", "0" * 64))
    (finding,) = run(frozen)
    assert finding.code == "SH403"
    assert "seahaven fixture" in finding.fix


@pytest.mark.parametrize(
    "now",
    [
        pytest.param("2026-06-01T09:00:00Z", id="no milliseconds"),
        pytest.param("2026-06-01T09:00:00.000000Z", id="microseconds"),
        pytest.param("2026-06-01T09:00:00+00:00", id="an offset instead of Z"),
        pytest.param("2026-06-01 09:00:00.000Z", id="a space instead of T"),
        pytest.param("not a timestamp", id="not a timestamp at all"),
    ],
)
def test_a_now_that_is_not_canonical_is_sh404(frozen: World, now: str) -> None:
    """One format across every door: the sidecar's `now` is one of those doors."""
    damage(frozen, lambda data: data.__setitem__("now", now))
    assert [finding.code for finding in run(frozen)] == ["SH404"]


def test_a_writable_state_file_is_not_a_finding(frozen: World) -> None:
    """`freeze` seals the file at 0444, and git hands it back at 0644.

    Every committed fixture of every world would fail a mode rule after a clone,
    including the reference world's own. What the seal guards against -- the file
    changing -- is SH402, over a hash version control does preserve.
    """
    (frozen.fixtures_dir / "empty" / STATE_NAME).chmod(WRITABLE)
    assert run(frozen) == []


@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
def test_a_journal_companion_beside_the_state_file_is_sh405(frozen: World, suffix: str) -> None:
    """Either file means the fixture was opened for writing after it was frozen."""
    state = frozen.fixtures_dir / "empty" / STATE_NAME
    state.with_name(state.name + suffix).write_bytes(b"")
    (finding,) = run(frozen)
    assert finding.code == "SH405"
    assert suffix in finding.message


def test_a_pending_directory_is_not_a_fixture(frozen: World) -> None:
    """`.pending-<id>` is a freeze in flight, and nothing in it is published yet."""
    (frozen.fixtures_dir / ".pending-half").mkdir()
    assert run(frozen) == []


def test_a_loose_file_in_the_fixtures_directory_is_not_a_fixture(frozen: World) -> None:
    (frozen.fixtures_dir / "notes.md").write_text("about these fixtures\n", encoding="utf-8")
    assert run(frozen) == []


def test_every_fixture_is_checked(frozen: World) -> None:
    with frozen.instance("empty") as instance:
        instance.freeze("forked", "A fork of empty.")
    for fixture_id in ("empty", "forked"):
        state = frozen.fixtures_dir / fixture_id / STATE_NAME
        state.chmod(WRITABLE)
        state.write_bytes(b"not a database")
    assert [finding.code for finding in run(frozen)] == ["SH402", "SH402"]
    assert {os.path.basename(finding.path) for finding in run(frozen)} == {STATE_NAME}
