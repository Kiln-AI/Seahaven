"""The fixture rules: SH401 to SH405.

A fixture is a binary artifact with a text sidecar, and every way the two can
drift apart is a way an eval silently starts from state nobody meant. The
framework already refuses a fixture it cannot verify at instance creation; these
rules find the same problems before a commit rather than in a run, and say which
fixture and which field.

The sidecar is checked first and alone: the other four rules read fields a
sidecar that does not validate does not have, so a fixture that fails SH401 is
reported once and left.

**SH405 is the journal companions, and not the file mode**, which is how
`components/cli_and_check.md` §3 now gives the rule. It read "state file not
read-only or has `-wal`/`-shm` companions" when this module was written, and the
mode half was dropped from the specification rather than implemented here: git
records only the executable bit, so every committed fixture -- the whole point of
a fixture -- comes back from a clone at `0644`, and the reference world's own
`empty` failed the rule on a fresh checkout. A lint that fires on every fixture
of every world after every clone is one authors learn to ignore, which costs more
than the rule is worth. What the seal was guarding against is the file changing,
and that is SH402, over a hash version control does preserve. `freeze` still
seals the file at `0444`, and that is what stops a live instance writing a
fixture in place; the mode is simply not something `check` can ask about.
"""

import hashlib
from pathlib import Path
from typing import Any

import pydantic
import yaml

from seahaven.clock import Clock
from seahaven.errors import WorldBug
from seahaven.fixtures import SIDECAR_NAME, STATE_NAME, FixtureMeta
from seahaven.lint import Finding, Target

__all__ = ["run"]

# The journal files a live database has beside it. A fixture is checkpointed and
# vacuumed before it is sealed, so either of these means the file was opened for
# writing after it was frozen -- and whatever the fixture's hash covers, it does
# not cover what is in them.
_COMPANIONS = ("-wal", "-shm")

_REGENERATE = "regenerate it with `seahaven fixture freeze` or `seahaven fixture fork`"


def run(target: Target) -> list[Finding]:
    """SH401 to SH405 over every fixture directory of the world."""
    findings: list[Finding] = []
    for directory in _fixture_directories(target.world.fixtures_dir):
        sidecar = directory / SIDECAR_NAME
        meta = _read(sidecar)
        if isinstance(meta, Finding):
            findings.append(meta)
            continue
        findings += _sidecar_findings(meta, target, sidecar)
        findings += _state_findings(meta, directory / STATE_NAME)
    return findings


def _fixture_directories(fixtures_dir: Path) -> list[Path]:
    """Every fixture directory, in id order.

    Dot-directories are the framework's own -- `.pending-<id>` is a freeze in
    flight -- and a world with no fixtures directory at all simply has no
    fixtures, which is not a finding: a world may only ever make blank instances.
    """
    try:
        children = sorted(fixtures_dir.iterdir())
    except FileNotFoundError, NotADirectoryError:
        return []
    return [child for child in children if child.is_dir() and not child.name.startswith(".")]


def _read(sidecar: Path) -> FixtureMeta | Finding:
    """The sidecar, or the one SH401 that says why it is not one."""
    try:
        data: Any = yaml.safe_load(sidecar.read_text(encoding="utf-8"))
    except OSError as error:
        return _sh401(sidecar, f"cannot be read: {error}")
    except yaml.YAMLError as error:
        return _sh401(sidecar, f"is not valid YAML: {error}")
    if not isinstance(data, dict):
        return _sh401(sidecar, "is not a mapping")
    try:
        return FixtureMeta.model_validate(data)
    except pydantic.ValidationError as error:
        return _sh401(sidecar, "; ".join(_violations(error)))


def _violations(error: pydantic.ValidationError) -> list[str]:
    """Pydantic's errors, one per line of the message, field first.

    Every one of them, not the first: a sidecar hand-edited into the wrong shape
    usually has several, and a `format_version` that is not `1` shows up here as
    the field it is -- which is the whole of what a reader needs to know that the
    fixture came from another version of Seahaven.
    """
    return [
        f"{'.'.join(str(part) for part in violation['loc']) or 'sidecar'}: {violation['msg']}"
        for violation in error.errors()
    ]


def _sidecar_findings(meta: FixtureMeta, target: Target, sidecar: Path) -> list[Finding]:
    findings: list[Finding] = []
    if not _is_canonical(meta.now):
        findings.append(
            Finding(
                code="SH404",
                severity="error",
                path=sidecar,
                message=(
                    f"fixture {meta.id!r} has now={meta.now!r}, which is not a canonical timestamp"
                ),
                fix="canonical is 2026-06-01T09:00:00.000Z: UTC, milliseconds, trailing Z",
            )
        )
    if meta.schema_hash != target.world.schema_hash:
        findings.append(
            Finding(
                code="SH403",
                severity="error",
                path=sidecar,
                message=(
                    f"fixture {meta.id!r} was frozen from a different schema than world "
                    f"{target.world.name!r} declares"
                ),
                fix=_REGENERATE,
            )
        )
    return findings


def _state_findings(meta: FixtureMeta, state: Path) -> list[Finding]:
    findings: list[Finding] = []
    if not state.is_file():
        return [
            Finding(
                code="SH402",
                severity="error",
                path=state,
                message=f"fixture {meta.id!r} has no {STATE_NAME}",
                fix=_REGENERATE,
            )
        ]
    if _sha256(state) != meta.file_sha256:
        findings.append(
            Finding(
                code="SH402",
                severity="error",
                path=state,
                message=(
                    f"fixture {meta.id!r} does not match its sidecar's file_sha256; it has been "
                    f"modified since it was frozen"
                ),
                fix="fixtures are immutable: fork it, change the fork, and freeze that",
            )
        )
    for suffix in _COMPANIONS:
        companion = state.with_name(state.name + suffix)
        if companion.exists():
            findings.append(
                Finding(
                    code="SH405",
                    severity="error",
                    path=companion,
                    message=(
                        f"fixture {meta.id!r} has a {suffix} file beside its state, so it was "
                        f"opened for writing after it was frozen"
                    ),
                    fix=_REGENERATE,
                )
            )
    return findings


def _is_canonical(now: str) -> bool:
    """Whether a timestamp is the one text every door of a world writes.

    Asked by round-tripping it through the clock rather than by a pattern: the
    clock is what produces canonical text everywhere else, so what it renders is
    the definition and a second one here could disagree with it.
    """
    try:
        return Clock.from_iso(now).iso() == now
    except WorldBug:
        return False


def _sh401(sidecar: Path, why: str) -> Finding:
    return Finding(
        code="SH401",
        severity="error",
        path=sidecar,
        message=f"{SIDECAR_NAME} {why}",
        fix=_REGENERATE,
    )


def _sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()
