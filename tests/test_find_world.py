"""How `seahaven` decides which world it is acting on.

The convention is the whole of it: the nearest `pyproject.toml`, its
`[project] name` normalised, and the attribute `world`. Everything here is about
one of the two ways that can go wrong -- the world is somewhere the convention
does not look, or the package is not a world -- and about whether the message
says what to do next.
"""

import sys
from pathlib import Path

import pytest

from seahaven.cli import CliError, discover, find_world, package_name
from seahaven.world import World
from tests.conftest import BROKEN_MODULE, WORLDS, broken_tidy

pytestmark = pytest.mark.usefixtures("isolated_imports")


def test_discovery_finds_the_world_from_the_project_root() -> None:
    world = find_world(None, WORLDS / "tidy")
    assert isinstance(world, World)
    assert world.name == "tidy"


def test_discovery_finds_the_world_from_a_subdirectory() -> None:
    """A world author runs `seahaven check` from wherever they happen to be."""
    world = find_world(None, WORLDS / "tidy" / "src" / "tidy" / "tools")
    assert world.name == "tidy"


def test_discovery_reports_the_project_root_it_resolved_from() -> None:
    found = discover(None, WORLDS / "tidy" / "src")
    assert found.root == (WORLDS / "tidy").resolve()
    assert found.package.__name__ == "tidy"


@pytest.mark.parametrize(
    ("name", "expected"),
    [("my-world", "my_world"), ("My.World", "my_world"), ("plain", "plain")],
)
def test_a_project_name_normalises_to_a_package_name(name: str, expected: str) -> None:
    assert package_name(name) == expected


def test_the_package_is_the_project_name_normalised(tmp_path: Path) -> None:
    """`[project] name = "no-world"` is the package `no_world`, not `no-world`."""
    with pytest.raises(CliError) as raised:
        discover(None, WORLDS / "no_world")
    assert "no_world" in str(raised.value)


def test_explicit_world_overrides_the_convention(tmp_path: Path) -> None:
    """`--world` reaches a world the convention would never find from here."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "unrelated"\n', encoding="utf-8")
    sys.path.insert(0, str(WORLDS / "tidy" / "src"))
    assert find_world("tidy.world:world", tmp_path).name == "tidy"


@pytest.mark.parametrize("spec", ["tidy", "", ":world", "tidy:"])
def test_a_malformed_world_option_says_what_it_wanted(spec: str) -> None:
    with pytest.raises(CliError) as raised:
        discover(spec, WORLDS / "tidy")
    assert "module:attr" in str(raised.value)
    # A user error, not a finding: there is nothing to lint until it is fixed.
    assert raised.value.code is None


def test_a_world_option_naming_a_module_that_does_not_import_is_sh501() -> None:
    with pytest.raises(CliError) as raised:
        discover("not_a_module_anyone_has:world", WORLDS / "tidy")
    assert raised.value.code == "SH501"
    assert "not_a_module_anyone_has" in str(raised.value)
    assert "Traceback" not in str(raised.value)
    assert raised.value.fix is not None and "--world" in raised.value.fix


@pytest.mark.parametrize(
    ("statement", "raised_as"),
    [
        ("def broken(:", "SyntaxError"),
        ("undefined_name", "NameError"),
        ("import not_installed_anywhere_x", "ModuleNotFoundError"),
    ],
    ids=["syntax error", "name error", "missing dependency"],
)
def test_an_error_in_world_code_names_its_file_and_line(
    tmp_path: Path, statement: str, raised_as: str
) -> None:
    """The place the import failed, not the export a package with no world is missing.

    A missing dependency is a `ModuleNotFoundError` too, and is placed at the
    import that needs it rather than read as the world module being missing.
    """
    root, line = broken_tidy(tmp_path, statement)
    with pytest.raises(CliError) as raised:
        discover(None, root)
    error = raised.value
    assert error.code == "SH501"
    assert (error.path, error.line) == ((root / BROKEN_MODULE).resolve(), line)
    assert raised_as in error.message
    assert f"{BROKEN_MODULE}:{line}" in error.message
    assert error.fix == f"fix the error at {BROKEN_MODULE}:{line}"
    assert "world = seahaven.World" not in f"{error.message} {error.fix}"


def test_a_world_that_refuses_a_tool_at_import_names_the_line(tmp_path: Path) -> None:
    """A `SeahavenError` out of world code is placed there, not in Seahaven's frames."""
    root, line = broken_tidy(tmp_path, "world.tool(write_note)")
    with pytest.raises(CliError) as raised:
        discover(None, root)
    error = raised.value
    assert error.code == "SH501"
    assert "write_note" in error.message
    assert (error.path, error.line) == ((root / BROKEN_MODULE).resolve(), line)
    assert error.fix == f"fix the error at {BROKEN_MODULE}:{line}"


def test_a_package_with_no_world_attribute_names_the_fix() -> None:
    with pytest.raises(CliError) as raised:
        discover(None, WORLDS / "no_world")
    message = str(raised.value)
    assert "world = seahaven.World(...)" in message
    assert "--world module:attr" in message
    assert raised.value.code == "SH501"


def test_a_root_init_imported_as_the_package_names_the_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What pytest does with a `tests/__init__.py` in a world `seahaven hub` has been run in."""
    root = tmp_path / "rootinit"
    root.mkdir()
    (root / "pyproject.toml").write_text('[project]\nname = "rootinit"\n', encoding="utf-8")
    (root / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(CliError) as raised:
        discover(None, root)
    error = raised.value
    assert error.code == "SH501"
    assert f"rootinit was imported from {root.resolve() / '__init__.py'}" in error.message
    assert error.fix is not None and "delete tests/__init__.py" in error.fix


def test_an_attribute_that_is_not_a_world_is_refused() -> None:
    with pytest.raises(CliError) as raised:
        discover("no_world:NOT_A_WORLD", WORLDS / "no_world")
    assert "str and not a World" in str(raised.value)


def test_a_world_that_refuses_to_be_constructed_is_sh104() -> None:
    """`broken_ddl` raises inside `World(...)`, so there is no world to lint."""
    with pytest.raises(CliError) as raised:
        discover(None, WORLDS / "broken_ddl")
    assert raised.value.code == "SH104"
    assert "Traceback" not in str(raised.value)


def test_no_pyproject_anywhere_is_a_user_error_and_not_a_finding(tmp_path: Path) -> None:
    """`tmp_path` has no project above it, so there is nothing to discover."""
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    with pytest.raises(CliError) as raised:
        discover(None, deep)
    assert raised.value.code is None
    assert "pyproject.toml" in str(raised.value)


def test_a_project_with_no_name_says_so(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[build-system]\n", encoding="utf-8")
    with pytest.raises(CliError) as raised:
        discover(None, tmp_path)
    assert "[project] name" in str(raised.value)


def test_a_pyproject_that_is_not_toml_says_so(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("this is not = = toml\n", encoding="utf-8")
    with pytest.raises(CliError) as raised:
        discover(None, tmp_path)
    assert "TOML" in str(raised.value)


def test_the_import_snapshot_holds_the_world_and_its_modules() -> None:
    """What SH301 compares against, and the reason discovery takes it at all."""
    found = discover(None, WORLDS / "tidy")
    assert "tidy" in found.imported
    assert "tidy.tools.notes" in found.imported
