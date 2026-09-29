"""The dependency licence gate: nothing Seahaven ships is copyleft.

`serve` and `mcp` are conflicting extras, so no environment holds both and this
suite runs in each of them in turn. A test that needs an extra's tree skips
where that tree is not installed, and what covers every extra between the two
environments is the gate itself: one `check_licences.py <extra>` per CI job,
which the last test here reads out of the workflow file.
"""

import re
from importlib import metadata
from pathlib import Path
from typing import Any

import pytest

from tools import check_licences
from tools.check_licences import (
    Licence,
    audit,
    declared_extras,
    extras_to_audit,
    licence_of,
    main,
    runtime_licences,
    unmet_requirements,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

COPYLEFT = """Metadata-Version: 2.4
Name: copyleftlib
Version: 1.0
License-Expression: GPL-3.0-only
"""

# A project with one extra, so the version check below is asked a question whose
# answer does not depend on which environment the suite is running in.
ROOT_WITH_AN_EXTRA = """Metadata-Version: 2.4
Name: probe
Version: 1.0
Provides-Extra: mcp
Requires-Dist: apsw>=3.53.4
Requires-Dist: mcp<3,>=2.2; extra == "mcp"
"""


class FakeDistribution(metadata.Distribution):
    """A distribution that exists only as its metadata."""

    def __init__(self, text: str) -> None:
        self._text = text

    def read_text(self, filename: str) -> str | None:
        return self._text if filename == "METADATA" else None

    def locate_file(self, path: Any) -> Any:
        raise NotImplementedError


def synced_extras() -> list[str]:
    """The declared extras this environment has, which is what it can prove."""
    return [
        extra for extra in declared_extras("seahaven") if not unmet_requirements("seahaven", extra)
    ]


def needs(extra: str) -> None:
    """Skip a test whose answer is in a tree this environment does not have."""
    unmet = unmet_requirements("seahaven", extra)
    if unmet:
        pytest.skip(f"this environment is not synced for the {extra} extra: {unmet[0]}")


def test_the_project_declares_both_extras() -> None:
    assert declared_extras("seahaven") == ["mcp", "serve"]


@pytest.mark.parametrize("extra", declared_extras("seahaven"))
def test_the_closure_of_every_declared_extra_is_allowed(extra: str) -> None:
    needs(extra)

    assert audit("seahaven", [extra]) == []


def test_the_base_closure_is_the_one_without_extras() -> None:
    base = {licence.distribution.lower() for licence in runtime_licences("seahaven", extras=[])}

    assert {"apsw", "pydantic", "pyyaml"} <= base
    assert "openenv" not in base
    assert audit("seahaven", extras=[]) == []


def test_the_serve_closure_is_the_runtime_tree_and_not_the_tooling() -> None:
    needs("serve")
    names = {licence.distribution.lower() for licence in runtime_licences("seahaven", ["serve"])}

    # `serve` is a runtime extra: CI installs it, users install it, so the gate
    # reads it. The tooling is a dependency group, which is not in the metadata
    # at all and is distributed with nothing.
    assert {"openenv", "fastmcp", "numpy", "pillow"} <= names
    assert {"pytest", "ruff", "ty"} & names == set()


def test_the_mcp_closure_is_the_sdk_and_the_types_package_it_splits_into() -> None:
    needs("mcp")
    by_name = {
        licence.distribution.lower(): licence for licence in runtime_licences("seahaven", ["mcp"])
    }

    assert by_name["mcp"].expression == "MIT"
    assert by_name["mcp-types"].expression == "MIT"
    assert "openenv" not in by_name


def test_every_way_of_declaring_a_licence_is_read() -> None:
    by_name = {
        licence.distribution.lower(): licence for licence in runtime_licences("seahaven", extras=[])
    }

    # A PEP 639 expression, the legacy classifiers, and the legacy free-text
    # field: all three are in the base closure today.
    assert by_name["pydantic"].expression == "MIT"
    assert by_name["pyyaml"].expression == "MIT"
    assert by_name["apsw"].expression == "any-OSI"


def test_the_extras_closure_carries_the_licences_this_rule_exists_for() -> None:
    needs("serve")
    by_name = {
        licence.distribution.lower(): licence for licence in runtime_licences("seahaven", ["serve"])
    }

    assert by_name["numpy"].expression == "BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0"
    assert by_name["orjson"].expression == "MPL-2.0 AND (Apache-2.0 OR MIT)"
    assert by_name["pillow"].expression == "MIT-CMU"
    # certifi declares MPL-2.0 only as a classifier, so the mapping has to carry
    # it or the gate reads the classifier line itself and fails on a licence it
    # allows.
    assert by_name["certifi"].expression == "MPL-2.0"
    assert all(licence.allowed() for licence in by_name.values())


def test_a_requirement_that_is_not_installed_cannot_be_cleared() -> None:
    [licence] = runtime_licences("no-such-distribution")

    assert licence == Licence("no-such-distribution", "unknown", "<not installed>")
    assert not licence.allowed()


def test_copyleft_is_rejected() -> None:
    licence = licence_of(FakeDistribution(COPYLEFT))

    assert licence == Licence("copyleftlib", "1.0", "GPL-3.0-only")
    assert not licence.allowed()


def test_every_spelling_of_the_refused_families_is_refused() -> None:
    for expression in (
        "GPL-2.0",
        "GPL-3.0-only",
        "GPL-3.0-or-later",
        "AGPL-3.0",
        "AGPL-3.0-or-later",
        "LGPL-2.1",
        "LGPL-2.1-only",
        "LGPL-3.0-or-later",
        "gpl-3.0-only",
        "GPL-2.0-only WITH Classpath-exception-2.0",
        "MIT AND LGPL-3.0-only",
    ):
        assert not Licence("x", "1", expression).allowed(), expression


def test_file_scope_copyleft_and_public_domain_are_allowed() -> None:
    # MPL-2.0 is copyleft per file and does not reach a linking or calling
    # process; CC0-1.0 grants everything and asks nothing.
    assert Licence("x", "1", "MPL-2.0").allowed()
    assert Licence("x", "1", "MPL-2.0 AND MIT").allowed()
    assert Licence("x", "1", "MPL-2.0 AND (Apache-2.0 OR MIT)").allowed()
    assert Licence("x", "1", "CC0-1.0").allowed()
    assert Licence("x", "1", "BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0").allowed()


def test_a_choice_of_licences_needs_a_person() -> None:
    # Every branch of an `OR` has to be acceptable, because the script does not
    # get to pick the good one quietly: a person makes that choice and records
    # it. Both of these are in the closure today and both branches are fine.
    assert Licence("x", "1", "Apache-2.0 OR BSD-3-Clause").allowed()
    assert Licence("x", "1", "MIT AND Apache-2.0").allowed()
    # One refused branch refuses the whole expression, even though a vendor
    # could lawfully take the other one.
    assert not Licence("x", "1", "GPL-3.0-only OR MIT").allowed()
    assert not Licence("x", "1", "MIT OR SSPL-1.0").allowed()


def test_an_undeclared_licence_is_not_allowed() -> None:
    assert not Licence("x", "1", "<none declared>").allowed()
    assert not Licence("x", "1", "").allowed()
    assert not Licence("x", "1", "<licence text>").allowed()


def test_an_unrecognised_licence_is_refused_rather_than_assumed() -> None:
    # The gate is an allowlist. A licence nobody has classified -- a new SPDX
    # identifier, a vendor's own name for one, a classifier line that fell
    # through the mapping -- fails and is read by a person.
    assert not Licence("x", "1", "SSPL-1.0").allowed()
    assert not Licence("x", "1", "Elastic-2.0").allowed()
    assert not Licence("x", "1", "License :: OSI Approved :: Artistic License").allowed()
    assert not Licence("x", "1", "MIT AND Some-New-Thing-1.0").allowed()


def test_the_extras_a_run_covers_are_the_ones_named_or_all_of_them() -> None:
    declared = ["mcp", "serve"]

    assert extras_to_audit([], declared) == declared
    assert extras_to_audit(["mcp"], declared) == ["mcp"]
    assert extras_to_audit(["serve", "serve"], declared) == ["serve"]


def test_an_extra_the_project_does_not_declare_is_a_typo_and_not_an_empty_audit() -> None:
    with pytest.raises(ValueError) as refusal:
        extras_to_audit(["srve"], ["mcp", "serve"])

    assert "srve" in str(refusal.value)
    assert "mcp, serve" in str(refusal.value)


def fake_environment(monkeypatch: pytest.MonkeyPatch, installed: dict[str, str]) -> None:
    """A project whose metadata and installed versions are this test's."""

    def distribution(name: str) -> metadata.Distribution:
        if name != "probe":
            raise metadata.PackageNotFoundError(name)
        return FakeDistribution(ROOT_WITH_AN_EXTRA)

    monkeypatch.setattr(check_licences.metadata, "distribution", distribution)

    def version(name: str) -> str:
        if name not in installed:
            raise metadata.PackageNotFoundError(name)
        return installed[name]

    monkeypatch.setattr(check_licences.metadata, "version", version)


def test_an_extra_whose_requirements_are_met_is_ready_to_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_environment(monkeypatch, {"mcp": "2.2.0"})

    assert unmet_requirements("probe", "mcp") == []


def test_an_extra_installed_at_a_version_it_does_not_ask_for_is_not_audited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The trap the conflicting extras set, and the reason the check reads versions.

    An environment synced for `serve` has a distribution called `mcp`, because
    `fastmcp` brings 1.x. A check that asked only whether the name was installed
    would clear that tree and report the `mcp` extra as audited.
    """
    fake_environment(monkeypatch, {"mcp": "1.30.0"})

    assert unmet_requirements("probe", "mcp") == ["mcp 1.30.0 is not mcp<3,>=2.2"]


def test_an_extra_that_is_not_installed_at_all_is_not_audited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_environment(monkeypatch, {})

    assert unmet_requirements("probe", "mcp") == ["mcp is not installed"]


def test_a_project_that_is_not_installed_has_nothing_to_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`main` never asks this -- it is for a caller asking about another project."""
    fake_environment(monkeypatch, {})

    assert unmet_requirements("no-such-distribution", "mcp") == [
        "no-such-distribution is not installed"
    ]


def test_a_requirement_missing_from_the_middle_of_a_tree_says_what_to_sync(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The extras are synced and something deeper is not, which a sync repairs.

    The extras themselves are answered before the audit runs, so this hint is
    for the tree below them.
    """
    monkeypatch.setattr(
        check_licences,
        "audit",
        lambda root, extras=None: [Licence("gone", "unknown", "<not installed>")],
    )

    assert main([]) == 1

    printed = capsys.readouterr().out
    assert "gone unknown: <not installed> is not allowed" in printed
    assert "an unread licence cannot be cleared; try `uv sync" in printed


def test_naming_an_extra_audits_that_one_and_says_which(
    capsys: pytest.CaptureFixture[str],
) -> None:
    here = synced_extras()
    if not here:
        pytest.skip("no declared extra is synced in this environment")

    assert main([here[0]]) == 0
    assert capsys.readouterr().out.splitlines()[0].endswith(f"(base closure plus: {here[0]})")


def test_a_bare_run_reads_what_is_here_and_names_what_it_could_not_read(
    capsys: pytest.CaptureFixture[str],
) -> None:
    here = synced_extras()
    if not here:
        pytest.skip("no declared extra is synced in this environment")

    assert main([]) == 0

    printed = capsys.readouterr().out
    assert f"(base closure plus: {', '.join(here)})" in printed
    for extra in sorted(set(declared_extras("seahaven")) - set(here)):
        assert f"{extra} was not read here" in printed
        assert f"uv sync --extra {extra}" in printed


def test_an_extra_this_environment_is_not_synced_for_fails_rather_than_passing_quietly(
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing = sorted(set(declared_extras("seahaven")) - set(synced_extras()))
    if not missing:
        pytest.skip("every declared extra is synced in this environment")

    assert main(missing) == 1

    printed = capsys.readouterr().out
    assert "an unread licence cannot be cleared" in printed
    assert f"uv sync --extra {missing[0]}" in printed


def test_an_unknown_extra_is_refused_before_anything_is_audited(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["srve"]) == 2
    assert "not an extra seahaven declares" in capsys.readouterr().out


@pytest.mark.skipif(not WORKFLOW.is_file(), reason="the workflow file needs the checkout")
def test_every_declared_extra_is_audited_by_some_ci_job() -> None:
    """The gate is split across two environments, so nothing else notices a gap.

    An extra added to `pyproject.toml` without a job that syncs it and audits it
    would be shipped to users and read by nobody.
    """
    audited = set(re.findall(r"tools/check_licences\.py ([a-z0-9-]+)", WORKFLOW.read_text()))

    assert audited == set(declared_extras("seahaven"))
