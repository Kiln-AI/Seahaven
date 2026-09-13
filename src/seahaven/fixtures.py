"""Fixtures: the starting state an instance is a copy of.

A fixture is a directory, not an object: `state.sqlite`, which is a compacted,
journal-free, read-only database file, and `fixture.yaml`, which says where it
came from and what its hash is. Nothing opens `state.sqlite` in place -- it is
verified and copied, and the copy is what runs -- so a fixture cannot be damaged
by using it and two instances of one fixture cannot see each other.

`freeze` is the only way to mint one. It is a whole-directory publish: the file
is vacuumed into a `.pending-` sibling, hashed, described and sealed, and only
then renamed into place, so a fixture directory either does not exist or is
complete.
"""

import hashlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import apsw
import pydantic
import yaml

from seahaven import conformance
from seahaven.clock import Clock
from seahaven.errors import WorldBug

if TYPE_CHECKING:  # `instances.py` imports this module; the annotation is all that is needed here
    from seahaven.instances import Instance

__all__ = [
    "PENDING_PREFIX",
    "SIDECAR_NAME",
    "STATE_NAME",
    "Fixture",
    "FixtureMeta",
    "check_id",
    "freeze",
    "load",
    "load_all",
    "verify",
]

STATE_NAME = "state.sqlite"
SIDECAR_NAME = "fixture.yaml"
# A fixture under construction. The dot is what keeps it out of `load_all`, so a
# half-written fixture is invisible to everything that lists them.
PENDING_PREFIX = ".pending-"

# The mode a frozen state file is left in: readable by anyone, writable by
# nobody. The copy an instance runs on is a fresh file and does not inherit it.
STATE_MODE = 0o444

# Verified fixtures, as (path, mtime, size, expected hash). Hashing a large
# fixture on every instance creation would be the cost of creating one, and a
# file that has not changed cannot have a different hash.
_verified: set[tuple[str, int, int, str]] = set()


class FixtureMeta(pydantic.BaseModel, frozen=True, extra="forbid"):
    """`fixture.yaml`: where this state came from and what it is.

    `format_version` is on every YAML file Seahaven writes, and `load` refuses
    any value but the one it knows: a fixture is an artifact that outlives the
    process that wrote it.
    """

    format_version: Literal[1]
    id: str
    world: str
    world_version: str
    schema_hash: str
    now: str
    parent_id: str | None
    file_sha256: str
    created_at: str
    description: str


@dataclass(frozen=True)
class Fixture:
    """One fixture on disk: its sidecar and the directory it lives in."""

    meta: FixtureMeta
    dir: Path

    @property
    def id(self) -> str:
        return self.meta.id

    @property
    def now(self) -> str:
        """The clock every instance of this fixture starts at."""
        return self.meta.now

    @property
    def description(self) -> str:
        return self.meta.description

    @property
    def parent_id(self) -> str | None:
        """The fixture this one was forked from, or `None` for one frozen from blank."""
        return self.meta.parent_id

    @property
    def state_path(self) -> Path:
        return self.dir / STATE_NAME


def check_id(fixture_id: str) -> None:
    """A fixture id is a directory name, so it has to be one.

    Ids arrive over the wire (`reset(fixture=...)`), and an id that is a path is
    a path traversal. The rule is the same one `freeze` applies when it mints
    one: a single path segment, no leading dot.

    The NUL is part of the segment rule rather than an addition to it -- no
    filename can hold one -- and it is named because `Path` does not refuse it:
    `"a\\x00b" == Path("a\\x00b").name`, so without this clause the id reached
    `freeze`'s `rmtree` -- whose `ignore_errors=True` suppresses `OSError` and a
    `ValueError` is not one -- and came back as a bare `ValueError` about an
    embedded null character instead of the refusal above. `world._check_name` applies the same
    rule to a world's name, and refuses both separators rather than the
    platform's; keep the two in step.
    """
    if (
        not fixture_id
        or "\x00" in fixture_id
        or fixture_id != Path(fixture_id).name
        or fixture_id.startswith(".")
    ):
        raise WorldBug(
            f"not a fixture id: {fixture_id!r}; a fixture id is one directory name, with no "
            f"separator and no leading dot"
        )


def load(fixture_dir: Path) -> Fixture:
    """Read one fixture's sidecar. The state file is not opened."""
    sidecar = fixture_dir / SIDECAR_NAME
    try:
        text = sidecar.read_text(encoding="utf-8")
    except OSError as error:
        raise WorldBug(f"{sidecar}: cannot be read: {error}") from error
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise WorldBug(f"{sidecar}: is not valid YAML: {error}") from error
    if not isinstance(data, dict):
        raise WorldBug(f"{sidecar}: is not a mapping")
    # Named before the model is built, because the model can only say that `1`
    # was expected, and what a reader needs to know is that this fixture was
    # written by a different version of Seahaven.
    if data.get("format_version") != 1:
        raise WorldBug(
            f"{sidecar}: format_version is {data.get('format_version')!r}, not 1; this fixture was "
            f"written by a different version of Seahaven"
        )
    try:
        meta = FixtureMeta.model_validate(data)
    except pydantic.ValidationError as error:
        raise WorldBug(f"{sidecar}: is not a fixture sidecar: {error}") from error
    return Fixture(meta=meta, dir=fixture_dir)


def load_all(fixtures_dir: Path) -> dict[str, Fixture]:
    """Every fixture in a directory, by id.

    A directory with no fixtures in it, or none at all, is not an error: a world
    that only makes blank instances never mints one.
    """
    found: dict[str, Fixture] = {}
    for child in sorted(_children(fixtures_dir)):
        # Dot-directories are the framework's own: `.pending-<id>` is a freeze in
        # flight, and nothing else in here belongs to a listing of fixtures.
        if not child.is_dir() or child.name.startswith("."):
            continue
        fixture = load(child)
        if fixture.id in found:
            raise WorldBug(
                f"two fixtures claim the id {fixture.id!r}: {found[fixture.id].dir} and {child}"
            )
        found[fixture.id] = fixture
    return found


def verify(fixture: Fixture) -> None:
    """Refuse a fixture whose state file is not the one its sidecar describes."""
    path = fixture.state_path
    try:
        stat = path.stat()
    except OSError as error:
        raise WorldBug(f"fixture {fixture.id!r} has no {STATE_NAME}: {error}") from error
    key = (str(path), stat.st_mtime_ns, stat.st_size, fixture.meta.file_sha256)
    if key in _verified:
        return
    if _sha256(path) != fixture.meta.file_sha256:
        raise WorldBug(
            f"fixture {fixture.id!r} at {path} does not match its sidecar's file_sha256; it has "
            f"been modified or truncated. Fixtures are immutable: fork it instead (create an "
            f"instance from it, change it, and freeze the result under a new id)."
        )
    _verified.add(key)


def freeze(instance: Instance, fixture_id: str, description: str, *, fixtures_dir: Path) -> Fixture:
    """Mint a fixture from a live instance. Called under the instance's lock.

    The instance is untouched: `VACUUM INTO` reads the database (the WAL
    included) and writes a new file, so a freeze is safe to take mid-authoring
    and the instance is usable afterwards.
    """
    check_id(fixture_id)
    target = fixtures_dir / fixture_id
    if target.exists():
        raise WorldBug(
            f"fixture {fixture_id!r} already exists at {target}; fixtures are immutable, so "
            f"freeze under a new id"
        )
    world = instance.world
    conformance.check(instance.db.conn, world)
    pending = fixtures_dir / f"{PENDING_PREFIX}{fixture_id}"
    try:
        shutil.rmtree(pending, ignore_errors=True)
        pending.mkdir(parents=True)
        state = pending / STATE_NAME
        # A compacted, rollback-journal file, and the live database untouched.
        try:
            instance.db.conn.execute("VACUUM INTO ?", (str(state),))
        except apsw.Error as error:
            # Freezing is an authoring step, not a tool call: a full disk or a
            # fixtures directory this process cannot write to is the author's
            # problem to read, in the same currency as every other refusal here.
            raise WorldBug(f"could not write fixture {fixture_id!r} to {state}: {error}") from error
        meta = FixtureMeta(
            format_version=1,
            id=fixture_id,
            world=world.name,
            world_version=world.version,
            schema_hash=world.schema_hash,
            now=instance.clock.iso(),
            parent_id=instance.fixture,
            file_sha256=_sha256(state),
            # The wall clock, and one of the two places in a world that reads it:
            # this says when the fixture was made, not what time it is inside.
            created_at=Clock.wall().iso(),
            description=description,
        )
        (pending / SIDECAR_NAME).write_text(
            yaml.safe_dump(meta.model_dump(), sort_keys=True), encoding="utf-8"
        )
        state.chmod(STATE_MODE)
        # The publish: a fixture directory either does not exist or is whole.
        os.rename(pending, target)
    except BaseException:
        shutil.rmtree(pending, ignore_errors=True)
        raise
    return Fixture(meta=meta, dir=target)


def _children(directory: Path) -> list[Path]:
    try:
        return list(directory.iterdir())
    except FileNotFoundError:
        return []


def _sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()
