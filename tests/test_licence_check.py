"""The dependency licence gate: what ships with Seahaven stays permissive."""

from importlib import metadata
from typing import Any

from scripts.check_licences import Licence, audit, licence_of, runtime_licences

COPYLEFT = """Metadata-Version: 2.4
Name: copyleftlib
Version: 1.0
License-Expression: GPL-3.0-only
"""


class FakeDistribution(metadata.Distribution):
    """A distribution that exists only as its metadata."""

    def __init__(self, text: str) -> None:
        self._text = text

    def read_text(self, filename: str) -> str | None:
        return self._text if filename == "METADATA" else None

    def locate_file(self, path: Any) -> Any:
        raise NotImplementedError


def test_the_runtime_closure_is_permissive() -> None:
    assert audit("seahaven") == []


def test_the_closure_is_the_runtime_one() -> None:
    names = {licence.distribution.lower() for licence in runtime_licences("seahaven")}

    assert {"apsw", "pydantic", "pyyaml"} <= names
    # `seahaven[serve]` is an extra, and the tooling is a dependency group:
    # neither is distributed with the library.
    assert "openenv" not in names
    assert {"pytest", "ruff", "ty"} & names == set()


def test_every_way_of_declaring_a_licence_is_read() -> None:
    by_name = {licence.distribution.lower(): licence for licence in runtime_licences("seahaven")}

    # A PEP 639 expression, the legacy classifiers, and the legacy free-text
    # field: all three are in the closure today.
    assert by_name["pydantic"].expression == "MIT"
    assert by_name["pyyaml"].expression == "MIT"
    assert by_name["apsw"].expression == "any-OSI"


def test_a_requirement_that_is_not_installed_cannot_be_cleared() -> None:
    [licence] = runtime_licences("no-such-distribution")

    assert licence == Licence("no-such-distribution", "unknown", "<not installed>")
    assert not licence.permissive()


def test_copyleft_is_rejected() -> None:
    licence = licence_of(FakeDistribution(COPYLEFT))

    assert licence == Licence("copyleftlib", "1.0", "GPL-3.0-only")
    assert not licence.permissive()


def test_a_choice_of_licences_needs_a_person() -> None:
    assert Licence("x", "1", "MIT AND Apache-2.0").permissive()
    assert not Licence("x", "1", "MPL-2.0 OR Apache-2.0").permissive()


def test_an_undeclared_licence_is_not_permissive() -> None:
    assert not Licence("x", "1", "<none declared>").permissive()
    assert not Licence("x", "1", "").permissive()
