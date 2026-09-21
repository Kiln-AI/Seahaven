"""Fail if any shipped dependency carries a copyleft licence.

Seahaven is a library other people vendor into their own products, so everything
it installs at runtime -- what `pip install seahaven` pulls in, **and every extra
it declares**, because an extra is shipped to users too -- has to be a licence
that does not reach into the vendor's own code. The rule is **no copyleft**: GPL,
AGPL and LGPL are refused in any version or spelling, anywhere in the closure.
Permissive licences are allowed, and so are two that are not permissive but do
not propagate to a caller: MPL-2.0, whose copyleft is per *file* and does not
cross a process or a link boundary, and CC0-1.0, which is public-domain
equivalent. Development tools are not checked: `pytest`, `ruff` and `ty` are a
dependency group, not an extra, and are distributed with nothing.

This is an allowlist, not a denylist of the licences we have thought to refuse:
an identifier nobody has classified fails, which is the only way an unread
licence cannot ship by accident.

**Name the extras to audit**: `uv run python scripts/check_licences.py serve`
reads the base closure plus `serve`, and an extra named this way that is not
installed cannot have its licences read, so it fails rather than passing quietly.
CI names them, one job per extra, because `serve` and `mcp` are declared as
conflicting in `pyproject.toml` and no environment can hold both. Between them
the jobs cover every extra the project declares, which
`tests/test_licence_check.py` asserts against the workflow file.

Run bare -- `uv run python scripts/check_licences.py` -- it audits the base
closure plus every declared extra this environment actually has, and prints what
it could not read and the command that reads it. That is the developer's run;
the strict one is the gate.
"""

import re
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from importlib import metadata

from packaging.requirements import Requirement

# Attribution-only licences: a vendor complies by keeping the notice.
PERMISSIVE = frozenset(
    {
        "0BSD",
        "Apache-2.0",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "BSL-1.0",
        "ISC",
        "MIT",
        "MIT-0",
        # The historical MIT/X Consortium licence as CMU published it, which is
        # what Pillow's "PIL Software License" is. Permissive and OSI-approved:
        # attribution plus a no-endorsement clause, nothing reciprocal.
        "MIT-CMU",
        "PSF-2.0",
        "Python-2.0",
        "Unlicense",
        "Zlib",
        # APSW's own metadata. Its author licenses it under "any OSI approved
        # license", which is the caller's choice and therefore permissive: taking
        # it under MIT is allowed by the licence itself.
        "any-OSI",
    }
)

# Copyleft at file scope only. Changing an MPL-2.0 file means publishing that
# file; linking it into, or talking to, a server process obliges nothing. Allowed
# for that reason and no other -- this set is not a door for copyleft in general.
FILE_SCOPE_COPYLEFT = frozenset({"MPL-2.0"})

# Public-domain equivalent: a grant of every right, with no condition to comply
# with. It appears inside numpy's conjunction.
PUBLIC_DOMAIN = frozenset({"CC0-1.0"})

# SPDX identifiers this project accepts in a shipped dependency.
ALLOWED = PERMISSIVE | FILE_SCOPE_COPYLEFT | PUBLIC_DOMAIN

# The families refused however they are spelled: `GPL-3.0`, `GPL-3.0-only`,
# `GPL-3.0-or-later`, `AGPL-3.0`, `LGPL-2.1-only`, and a `... WITH <exception>`.
# Nothing on the allowlist begins this way, so the check below is belt and
# braces: what it buys is that adding one of these to `PERMISSIVE` by hand, in a
# hurry, still does not ship it.
COPYLEFT = re.compile(r"^(?:A|L)?GPL", re.IGNORECASE)

# Distributions that still publish the legacy classifiers rather than a PEP 639
# expression. Mapped to the identifier the classifier stands for; a classifier
# that is not here is treated as unknown, which fails.
CLASSIFIER_SPDX = {
    "License :: OSI Approved :: Apache Software License": "Apache-2.0",
    "License :: OSI Approved :: BSD License": "BSD-3-Clause",
    "License :: OSI Approved :: Boost Software License 1.0 (BSL-1.0)": "BSL-1.0",
    "License :: OSI Approved :: ISC License (ISCL)": "ISC",
    "License :: OSI Approved :: MIT License": "MIT",
    "License :: OSI Approved :: MIT No Attribution License (MIT-0)": "MIT-0",
    "License :: OSI Approved :: Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "License :: OSI Approved :: Python Software Foundation License": "PSF-2.0",
    "License :: OSI Approved :: The Unlicense (Unlicense)": "Unlicense",
    "License :: OSI Approved :: zlib/libpng License": "Zlib",
}

# The longest a legacy `License` field may be before it is read as licence *text*
# rather than as an identifier.
_IDENTIFIER_LENGTH = 64


@dataclass(frozen=True)
class Licence:
    """What a distribution says about its licence, and where it said it."""

    distribution: str
    version: str
    expression: str

    def allowed(self) -> bool:
        """Whether every identifier in the expression is on the allowlist.

        Every term, `OR` included: `GPL-3.0-only OR MIT` is a choice a person
        should make and record, not one this script should make quietly by
        reading past the branch it may not take. An `AND` has to be read that way
        regardless -- numpy's `BSD-3-Clause AND 0BSD AND MIT AND Zlib AND
        CC0-1.0` binds you to all five -- so one rule covers both: whichever
        branch a vendor ends up on, the answer is acceptable.
        """
        identifiers = self.expression.replace("(", " ").replace(")", " ").split()
        terms = [term for term in identifiers if term.upper() not in ("AND", "OR", "WITH")]
        if any(COPYLEFT.match(term) for term in terms):
            return False
        return bool(terms) and all(term in ALLOWED for term in terms)


def licence_of(distribution: metadata.Distribution) -> Licence:
    """Read a distribution's licence, preferring PEP 639 to the older spellings."""
    fields = distribution.metadata
    name = fields["Name"]
    version = fields["Version"]
    expression = fields.get("License-Expression") or ""

    if not expression:
        classifiers = [
            line for line in fields.get_all("Classifier") or [] if line.startswith("License ::")
        ]
        expression = " AND ".join(CLASSIFIER_SPDX.get(line, line) for line in classifiers)

    if not expression:
        declared = (fields.get("License") or "").strip()
        expression = declared if len(declared) <= _IDENTIFIER_LENGTH else "<licence text>"

    return Licence(name, version, expression or "<none declared>")


def declared_extras(root: str) -> list[str]:
    """Every extra `root` publishes, which is every extra a user can install."""
    try:
        distribution = metadata.distribution(root)
    except metadata.PackageNotFoundError:
        return []
    return sorted(distribution.metadata.get_all("Provides-Extra") or [])


def runtime_licences(root: str, extras: Iterable[str] | None = None) -> list[Licence]:
    """The licence of everything installing `root` and its extras brings with it.

    The walk is over (distribution, extra) pairs rather than distributions: a
    requirement is reached with the extra that asked for it, so
    `openenv; extra == "serve"` is in the closure and `pytest` -- a dependency
    group, which is not an extra and is not in the metadata at all -- is not.
    `extras` defaults to every extra `root` declares, so an extra added later is
    covered without editing this script.

    Markers are otherwise evaluated in the environment this runs in, so a
    dependency gated on another platform (`sys_platform == "win32"`) is not
    reached from Linux CI and is not checked. Adding one means checking it by
    hand, or running this on that platform too.
    """
    wanted = declared_extras(root) if extras is None else sorted(extras)
    licences: dict[str, Licence] = {}
    seen: set[tuple[str, str]] = set()
    pending = [(root, extra) for extra in ("", *wanted)]
    while pending:
        name, extra = pending.pop()
        if (_normalize(name), extra) in seen:
            continue
        seen.add((_normalize(name), extra))
        try:
            distribution = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            # A requirement whose marker is true here but which is not installed:
            # its licence cannot be read, so it cannot be cleared either. An
            # extra that was never synced arrives here.
            licences[_normalize(name)] = Licence(name, "unknown", "<not installed>")
            continue
        if _normalize(name) != _normalize(root):
            licences[_normalize(name)] = licence_of(distribution)
        for requirement in distribution.requires or []:
            parsed = Requirement(requirement)
            # `extra == "..."` is false for the base closure, which is how
            # `seahaven[serve]`'s dependencies are reached only under "serve".
            if parsed.marker is None or parsed.marker.evaluate({"extra": extra}):
                pending.append((parsed.name, ""))
                pending.extend((parsed.name, requested) for requested in parsed.extras)
    return sorted(licences.values(), key=lambda licence: licence.distribution.lower())


def _normalize(name: str) -> str:
    """A distribution name as PEP 503 compares them."""
    return re.sub(r"[-_.]+", "-", name).lower()


def audit(root: str, extras: Iterable[str] | None = None) -> list[Licence]:
    """The licences in `root`'s shipped closure that the rule does not allow."""
    return [licence for licence in runtime_licences(root, extras) if not licence.allowed()]


def extras_to_audit(named: Sequence[str], declared: Sequence[str]) -> list[str]:
    """The extras one run covers: the ones named on the command line, or all of them.

    An extra the project does not declare is a typo -- in a workflow file, or in
    a command a person typed -- and a typo that audited nothing quietly would be
    a green run proving nothing.
    """
    unknown = sorted(set(named) - set(declared))
    if unknown:
        raise ValueError(
            f"{', '.join(unknown)}: not an extra seahaven declares ({', '.join(declared)})"
        )
    return sorted(set(named)) if named else list(declared)


def unmet_requirements(root: str, extra: str) -> list[str]:
    """What this environment is missing before `root[extra]` can be audited.

    A requirement of the extra that is not installed, or one installed at a
    version the extra does not ask for. The second is not hypothetical: `serve`
    brings `mcp` 1.x transitively, so an environment synced for `serve` has a
    distribution called `mcp`, and a walk that only asked whether the name was
    installed would read that tree, clear it, and report the `mcp` extra as
    audited when nothing of it was.

    A requirement is this extra's when the marker is true for it and false
    without it, which is what excludes the base dependencies.
    """
    try:
        distribution = metadata.distribution(root)
    except metadata.PackageNotFoundError:
        # `main` never reaches this -- a `seahaven` that is not installed
        # declares no extras, so there is nothing to ask about -- but a caller
        # asking this function about another project does.
        return [f"{root} is not installed"]
    unmet: list[str] = []
    for requirement in distribution.requires or []:
        parsed = Requirement(requirement)
        if parsed.marker is None or not parsed.marker.evaluate({"extra": extra}):
            continue
        if parsed.marker.evaluate({"extra": ""}):
            continue
        try:
            version = metadata.version(parsed.name)
        except metadata.PackageNotFoundError:
            unmet.append(f"{parsed.name} is not installed")
            continue
        if not parsed.specifier.contains(version, prereleases=True):
            unmet.append(f"{parsed.name} {version} is not {parsed.name}{parsed.specifier}")
    return unmet


def main(argv: Sequence[str] | None = None) -> int:
    named = list(sys.argv[1:] if argv is None else argv)
    try:
        asked = extras_to_audit(named, declared_extras("seahaven"))
    except ValueError as error:
        print(error)
        return 2

    unsynced = {extra: unmet_requirements("seahaven", extra) for extra in asked}
    unsynced = {extra: lines for extra, lines in unsynced.items() if lines}
    if named and unsynced:
        for extra, lines in unsynced.items():
            for line in lines:
                print(f"{extra}: {line}")
        print("an unread licence cannot be cleared; sync the extra first:")
        for extra in unsynced:
            print(f"  uv sync --extra {extra}")
        return 1

    extras = [extra for extra in asked if extra not in unsynced]
    problems = audit("seahaven", extras)
    for licence in problems:
        print(f"{licence.distribution} {licence.version}: {licence.expression} is not allowed")
    if problems:
        print(f"{len(problems)} shipped dependencies need review")
        if any(licence.expression == "<not installed>" for licence in problems):
            # Not an extra -- an extra this environment does not have was
            # answered above -- so this is a requirement missing from the middle
            # of a tree, which a sync of what was audited puts back.
            command = " ".join(["uv sync", *(f"--extra {extra}" for extra in extras)])
            print(f"an unread licence cannot be cleared; try `{command}` first")
        return 1
    scope = ", ".join(extras) or "no extras"
    print(f"every shipped dependency is allowed (base closure plus: {scope})")
    for extra in unsynced:
        print(
            f"{extra} was not read here, because this environment is not synced for it: "
            f"`uv sync --extra {extra} && uv run python scripts/check_licences.py {extra}`"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
