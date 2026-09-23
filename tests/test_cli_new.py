"""`seahaven new`: what a world author's first five minutes look like.

The two tests that matter are the last two claims of `components/cli_and_check.md`
§2: a fresh scaffold passes `seahaven check` with no findings, and passes
`pytest` with no fixture on disk. Everything an author does after that is an edit
to something that already worked, so the first failure they see is one they made.
"""

import os
import shlex
import subprocess
import sys
import tomllib
from importlib.metadata import metadata
from pathlib import Path

import pytest

from seahaven.cli import CliError
from seahaven.cli.new import HUB_FILES, render, seahaven_requirement
from seahaven.fixtures import load
from tests.conftest import CliResult, run_cli

pytestmark = pytest.mark.usefixtures("isolated_imports")

# The layout of functional spec §2.1, as paths relative to the world's directory.
EXPECTED = {
    ".gitignore",
    "AGENTS.md",
    "README.md",
    "fixtures/.gitkeep",
    "fixtures_src/generate.py",
    "pyproject.toml",
    "src/my_world/__init__.py",
    "src/my_world/errors.py",
    "src/my_world/middleware/__init__.py",
    "src/my_world/middleware/error_handler.py",
    "src/my_world/openenv_app.py",
    "src/my_world/schema/001_items.sql",
    "src/my_world/tools/__init__.py",
    "src/my_world/tools/items.py",
    "src/my_world/world.py",
    "tests/test_fixtures.py",
    "tests/test_items.py",
}


def files(root: Path) -> set[str]:
    return {str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()}


@pytest.fixture
def scaffold(tmp_path: Path) -> Path:
    """A world named `my-world`, rendered and untouched."""
    return render("my-world", tmp_path / "my-world")


def test_the_scaffold_is_the_layout_the_spec_names(scaffold: Path) -> None:
    assert files(scaffold) == EXPECTED


def test_the_scaffold_has_no_fixture_on_disk(scaffold: Path) -> None:
    """`new` builds nothing: the world's first fixture is its author's to freeze."""
    assert [path.name for path in (scaffold / "fixtures").iterdir()] == [".gitkeep"]


def test_the_package_name_is_the_world_name_normalised(scaffold: Path) -> None:
    assert (scaffold / "src" / "my_world" / "world.py").is_file()
    assert 'name = "my-world"' in (scaffold / "pyproject.toml").read_text(encoding="utf-8")
    # Spelled out in the rendered file rather than left to the build backend's
    # default, because `package_name` and uv's own normalisation of a
    # distribution name are not the same function: they part company on a run of
    # underscores, and the backend would then refuse to find the module.
    assert 'module-name = "my_world"' in (scaffold / "pyproject.toml").read_text(encoding="utf-8")


def test_the_scaffold_pins_a_state_format(scaffold: Path) -> None:
    """A pin is chosen once, when the world is created, and the scaffold is where."""
    source = (scaffold / "src" / "my_world" / "world.py").read_text(encoding="utf-8")

    assert 'state_format="seahaven.state/1"' in source


def test_the_scaffold_re_exports_every_extra_the_framework_publishes(scaffold: Path) -> None:
    """A world is its own package, so `uv sync --extra serve` reads *its* extras.

    The framework's extras are read from its own metadata rather than written out
    here, because the failure this catches is an extra that the framework gained
    and the template never re-exported: `mcp` was added to the framework and to
    nothing else, and `uv run --extra mcp` in a generated world answered "Extra
    `mcp` is not defined" until this test existed.
    """
    declared = tomllib.loads((scaffold / "pyproject.toml").read_text(encoding="utf-8"))
    extras = declared["project"]["optional-dependencies"]
    published = metadata("seahaven").get_all("Provides-Extra") or []
    assert published, "seahaven publishes no extras, so this test is asking the wrong question"
    for extra in published:
        assert extras.get(extra) == [f"seahaven[{extra}]"], (
            f"a generated world does not re-export seahaven's {extra!r} extra"
        )


def test_the_scaffold_declares_the_conflict_between_serve_and_mcp(scaffold: Path) -> None:
    """Without the declaration, `uv lock` in a generated world is unsolvable.

    The two extras cannot be installed together, and uv reads `conflicts` from
    the project it is locking and never from a dependency, so the framework's own
    declaration does not reach a world outside this workspace.
    """
    declared = tomllib.loads((scaffold / "pyproject.toml").read_text(encoding="utf-8"))
    conflicts = declared["tool"]["uv"]["conflicts"]
    paired = [{entry["extra"] for entry in pair} for pair in conflicts]
    assert {"serve", "mcp"} in paired, (
        f"a generated world does not declare serve and mcp as conflicting extras: {conflicts}"
    )


def test_the_scaffold_pins_the_installed_minor_version(scaffold: Path) -> None:
    requirement = seahaven_requirement()
    assert requirement.startswith("seahaven~=")
    assert requirement in (scaffold / "pyproject.toml").read_text(encoding="utf-8")


def test_check_passes_on_a_fresh_scaffold(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(scaffold)
    result = run_cli(capsys, "check")
    assert result.out == ""
    assert result.code == 0


def test_pytest_passes_on_a_fresh_scaffold(scaffold: Path) -> None:
    """A subprocess, because a second pytest cannot run inside this one."""
    finished = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=scaffold,
        env={**os.environ, "PYTHONPATH": str(scaffold / "src")},
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert finished.returncode == 0, finished.stdout + finished.stderr


def test_the_readme_builds_the_first_fixture(scaffold: Path) -> None:
    """The command under the README's "Making a fixture", run as written.

    `uv run python` is this interpreter with the package on `PYTHONPATH`, as in
    the pytest test above: the scaffold's own environment would install seahaven
    from an index it has not been published to.
    """
    readme = (scaffold / "README.md").read_text(encoding="utf-8")
    section = readme.split("## Making a fixture\n", 1)[1]
    command = section.split("```sh\n", 1)[1].split("\n```", 1)[0]
    launcher = "uv run python "
    assert command.startswith(launcher)

    finished = subprocess.run(
        [sys.executable, *shlex.split(command.removeprefix(launcher))],
        cwd=scaffold,
        env={**os.environ, "PYTHONPATH": str(scaffold / "src")},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert finished.returncode == 0, finished.stdout + finished.stderr
    generator = (scaffold / "fixtures_src" / "generate.py").read_text(encoding="utf-8")
    fixture = load(scaffold / "fixtures" / "empty")
    assert fixture.id == "empty"
    assert f'NOW = "{fixture.now}"' in generator


def test_nothing_is_imported_and_no_world_is_built(scaffold: Path) -> None:
    """`new` renders templates and stops; `check` is what imports a world."""
    assert "my_world" not in sys.modules


def test_an_existing_directory_is_refused(tmp_path: Path) -> None:
    (tmp_path / "taken").mkdir()
    with pytest.raises(CliError) as raised:
        render("taken", tmp_path / "taken")
    assert "already exists" in str(raised.value)


@pytest.mark.parametrize("name", ["1world", "my world", "class", "", "-"])
def test_a_name_that_is_not_a_package_name_is_refused(tmp_path: Path, name: str) -> None:
    with pytest.raises(CliError) as raised:
        render(name, tmp_path / "world")
    assert "not a Python identifier" in str(raised.value)
    assert not (tmp_path / "world").exists()


@pytest.mark.parametrize("name", ["café", "con", "trailing.", "x" * 129])
def test_a_name_that_is_not_a_world_name_is_refused(tmp_path: Path, name: str) -> None:
    """All four make a legal package name, and none makes a legal `World(name=...)`.

    Refused here or the scaffold renders, exits 0, and fails at the author's first
    `pytest` with a `WorldBug` from a `world.py` they did not write.
    """
    with pytest.raises(CliError) as raised:
        render(name, tmp_path / "world")
    assert "is not a world name" in str(raised.value)
    assert not (tmp_path / "world").exists()


def test_hub_adds_five_files_and_nothing_else(tmp_path: Path) -> None:
    """One world, rendered twice: the difference is exactly what `openenv push` wants."""
    plain = render("hubbed", tmp_path / "plain" / "hubbed")
    hubbed = render("hubbed", tmp_path / "hub" / "hubbed", hub=True)
    assert files(hubbed) - files(plain) == set(HUB_FILES)
    assert files(plain) - files(hubbed) == set()


def test_the_hub_files_re_export_the_framework(tmp_path: Path) -> None:
    """Not placeholders: every Seahaven world's client and models are the same."""
    hubbed = render("hubbed", tmp_path / "hub" / "hubbed", hub=True)
    assert "SeahavenClient as Client" in (hubbed / "client.py").read_text(encoding="utf-8")
    models = (hubbed / "models.py").read_text(encoding="utf-8")
    assert "SeahavenObservation" in models
    assert "SeahavenState" in models
    assert "hubbed.openenv_app:app" in (hubbed / "Dockerfile").read_text(encoding="utf-8")
    assert "name: hubbed" in (hubbed / "openenv.yaml").read_text(encoding="utf-8")


def test_new_prints_the_next_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    result: CliResult = run_cli(capsys, "new", "my-world")
    assert result.code == 0
    assert "cd my-world" in result.out
    assert "uv run pytest" in result.out
    assert "uv run seahaven check" in result.out


def test_new_writes_into_the_directory_it_is_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    assert run_cli(capsys, "new", "elsewhere", "--dir", "worlds").code == 0
    assert (tmp_path / "worlds" / "elsewhere" / "pyproject.toml").is_file()
