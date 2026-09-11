"""Fail if any runtime dependency is not permissively licensed.

Seahaven is a library other people vendor into their own products, so its runtime
closure -- what `pip install seahaven` pulls in, extras excluded -- stays MIT,
Apache-2.0 or BSD-class. Development tools are not checked: they are not
distributed with anything.

Run it as `uv run python scripts/check_licences.py`; CI does.
"""

import re
import sys
from dataclasses import dataclass
from importlib import metadata

from packaging.requirements import Requirement

# SPDX identifiers this project accepts in a runtime dependency.
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

    def permissive(self) -> bool:
        """Whether every identifier in the expression is on the allowlist.

        Deliberately strict about `OR`: `MPL-2.0 OR Apache-2.0` is a choice a
        person should make and record, not one this script should make quietly.
        """
        identifiers = self.expression.replace("(", " ").replace(")", " ").split()
        terms = [term for term in identifiers if term.upper() not in ("AND", "OR", "WITH")]
        return bool(terms) and all(term in PERMISSIVE for term in terms)


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


def runtime_licences(root: str) -> list[Licence]:
    """The licence of everything installing `root` brings with it, extras excluded.

    Markers are evaluated in the environment this runs in, so a dependency gated
    on another platform (`sys_platform == "win32"`) is not reached from Linux CI
    and is not checked. Adding one means checking it by hand, or running this on
    that platform too.
    """
    licences: list[Licence] = []
    seen: set[str] = set()
    pending = [root]
    while pending:
        name = pending.pop()
        if _normalize(name) in seen:
            continue
        seen.add(_normalize(name))
        try:
            distribution = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            # A requirement whose marker is true here but which is not installed:
            # its licence cannot be read, so it cannot be cleared either.
            licences.append(Licence(name, "unknown", "<not installed>"))
            continue
        if _normalize(name) != _normalize(root):
            licences.append(licence_of(distribution))
        for requirement in distribution.requires or []:
            parsed = Requirement(requirement)
            # `extra == "..."` is false with no extra requested, which is how
            # `seahaven[serve]`'s dependencies stay out of the closure.
            if parsed.marker is None or parsed.marker.evaluate({"extra": ""}):
                pending.append(parsed.name)
    return sorted(licences, key=lambda licence: licence.distribution.lower())


def _normalize(name: str) -> str:
    """A distribution name as PEP 503 compares them."""
    return re.sub(r"[-_.]+", "-", name).lower()


def audit(root: str) -> list[Licence]:
    """The licences in `root`'s runtime closure that are not permissive."""
    return [licence for licence in runtime_licences(root) if not licence.permissive()]


def main() -> int:
    problems = audit("seahaven")
    for licence in problems:
        print(f"{licence.distribution} {licence.version}: {licence.expression} is not permissive")
    if problems:
        print(f"{len(problems)} runtime dependencies need review")
        return 1
    print("every runtime dependency is permissively licensed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
