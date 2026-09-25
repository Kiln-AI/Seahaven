"""`seahaven hub`: the hub files, added to a world `seahaven new` made.

`test_hub_scaffold.py` judges the result with OpenEnv's own code. This module is
about the command: what it writes, where the README sections go, and that every
refusal leaves the world exactly as it was.
"""

from pathlib import Path

import pytest

from seahaven.cli import CliError
from seahaven.cli import hub as hub_command
from seahaven.cli.hub import HUB_FILES, README, add_hub_files
from seahaven.cli.new import render
from tests.conftest import assert_scaffold_pytest_passes, run_cli

pytestmark = pytest.mark.usefixtures("isolated_imports")

CONNECTING = "## Connecting to the published world"


def tree(root: Path) -> dict[str, bytes]:
    """Every file under `root` and its bytes: what "nothing was written" is checked against."""
    return {
        str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }


@pytest.fixture
def world(tmp_path: Path) -> Path:
    return render("my-world", tmp_path / "my-world")


def refused(start: Path) -> str:
    """The refusal `seahaven hub` gives at `start`, after checking it wrote nothing."""
    before = tree(start)
    with pytest.raises(CliError) as raised:
        add_hub_files(start)
    assert tree(start) == before
    return str(raised.value)


def test_hub_adds_five_files_and_changes_only_the_readme(world: Path) -> None:
    before = tree(world)
    root, written = add_hub_files(world)
    after = tree(world)
    assert root == world.resolve()
    assert set(after) - set(before) == set(HUB_FILES)
    assert {name for name in before if before[name] != after[name]} == {README}
    assert written == [*(Path(name) for name in sorted(HUB_FILES)), Path(README)]


def test_the_hub_files_re_export_the_framework(world: Path) -> None:
    """Not placeholders: every Seahaven world's client and models are the same."""
    add_hub_files(world)
    assert "SeahavenClient as Client" in (world / "client.py").read_text(encoding="utf-8")
    models = (world / "models.py").read_text(encoding="utf-8")
    assert "SeahavenObservation" in models
    assert "SeahavenState" in models
    assert "my_world.openenv_app:app" in (world / "Dockerfile").read_text(encoding="utf-8")
    assert "name: my-world" in (world / "openenv.yaml").read_text(encoding="utf-8")


def test_the_readme_gets_front_matter_on_top_and_connecting_after_usage(world: Path) -> None:
    original = (world / README).read_text(encoding="utf-8")
    add_hub_files(world)
    readme = (world / README).read_text(encoding="utf-8")
    lines = readme.splitlines()
    assert lines[0] == "---"
    assert "title: my-world" in lines
    assert lines.index("## Using it") < lines.index(CONNECTING) < lines.index("## Making a fixture")
    assert "<owner>/my-world" in readme
    body = readme.split("\n---\n\n", 1)[1]
    before_section, _, after_section = body.partition(CONNECTING)
    assert before_section + after_section[after_section.index("## Making a fixture") :] == original


def test_a_readme_without_a_usage_section_gets_the_connecting_section_at_the_end(
    world: Path,
) -> None:
    (world / README).write_text("# my-world\n\nMine.\n", encoding="utf-8")
    add_hub_files(world)
    readme = (world / README).read_text(encoding="utf-8")
    assert readme.split("\n---\n\n", 1)[1].startswith(f"# my-world\n\nMine.\n\n{CONNECTING}\n")
    assert readme.endswith("`.\n")


def test_a_heading_inside_a_code_fence_does_not_end_the_usage_section(world: Path) -> None:
    (world / README).write_text(
        "# my-world\n\n## Using it\n\n```sh\n# a comment\n```\n\n## Next\n", encoding="utf-8"
    )
    add_hub_files(world)
    lines = (world / README).read_text(encoding="utf-8").splitlines()
    assert lines.index("# a comment") < lines.index(CONNECTING) < lines.index("## Next")


def test_a_missing_readme_is_created_with_both_sections(world: Path) -> None:
    (world / README).unlink()
    _, written = add_hub_files(world)
    assert Path(README) in written
    readme = (world / README).read_text(encoding="utf-8")
    assert readme.startswith("---\n")
    assert "\n# my-world\n" in readme
    assert CONNECTING in readme
    assert readme.endswith(".\n")


def test_a_readme_that_is_not_a_file_is_refused(world: Path) -> None:
    (world / README).unlink()
    (world / README).mkdir()
    assert "README.md is not a file" in refused(world)


@pytest.mark.parametrize("name", HUB_FILES)
def test_an_existing_hub_file_refuses_everything(world: Path, name: str) -> None:
    (world / name).write_text("mine\n", encoding="utf-8")
    message = refused(world)
    assert f"{name} exists" in message
    assert "wrote nothing" in message


def test_existing_front_matter_refuses_everything(world: Path) -> None:
    readme = world / README
    readme.write_text("---\ntitle: mine\n---\n\n" + readme.read_text(encoding="utf-8"))
    assert "README.md already starts with front matter" in refused(world)


def test_an_existing_connecting_section_refuses_everything(world: Path) -> None:
    readme = world / README
    readme.write_text(readme.read_text(encoding="utf-8") + f"\n{CONNECTING}\n\nMine.\n")
    assert "'Connecting to the published world' section" in refused(world)


def test_every_obstacle_is_named_at_once(world: Path) -> None:
    (world / "Dockerfile").write_text("FROM mine\n", encoding="utf-8")
    (world / "openenv.yaml").write_text("name: mine\n", encoding="utf-8")
    message = refused(world)
    assert "Dockerfile exists" in message
    assert "openenv.yaml exists" in message


def test_a_hub_run_twice_refuses_the_second_time(world: Path) -> None:
    add_hub_files(world)
    message = refused(world)
    assert all(f"{name} exists" in message for name in HUB_FILES)
    assert "front matter" in message


def test_a_directory_that_is_not_a_project_is_refused(tmp_path: Path) -> None:
    message = refused(tmp_path)
    assert "no pyproject.toml" in message
    assert "--world" not in message


def test_a_world_without_its_package_is_refused(world: Path) -> None:
    (world / "src" / "my_world").rename(world / "src" / "elsewhere")
    assert "where the world's package 'my_world' has to be" in refused(world)


def test_a_world_without_its_app_module_is_refused(world: Path) -> None:
    (world / "src" / "my_world" / "openenv_app.py").unlink()
    assert "my_world.openenv_app:app" in refused(world)


def test_a_file_that_cannot_be_written_takes_the_others_with_it(
    world: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rendered = hub_command.render_group

    def with_an_unwritable_file(
        group: str, package: str, substitutions: dict[str, str]
    ) -> dict[Path, str]:
        return {**rendered(group, package, substitutions), Path("missing/file"): ""}

    monkeypatch.setattr(hub_command, "render_group", with_an_unwritable_file)
    before = tree(world)
    with pytest.raises(FileNotFoundError):
        add_hub_files(world)
    assert tree(world) == before


def test_a_readme_that_cannot_be_written_takes_the_new_files_with_it(
    world: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(path: Path, *args: object, **kwargs: object) -> int:
        raise PermissionError(f"{path} is read-only")

    before = tree(world)
    monkeypatch.setattr(Path, "write_text", refuse)
    with pytest.raises(PermissionError):
        add_hub_files(world)
    assert tree(world) == before


def test_seahaven_hub_from_inside_the_world(
    world: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The entry point, from a directory below the root, as `check` finds the world from."""
    monkeypatch.chdir(world / "src" / "my_world")
    result = run_cli(capsys, "hub")
    assert result.code == 0
    assert result.lines[0] == f"added to {world.resolve()}:"
    assert sorted(line.strip() for line in result.lines[1:]) == sorted([*HUB_FILES, README])
    assert all((world / name).is_file() for name in HUB_FILES)
    assert not (world / "src" / "my_world" / "Dockerfile").exists()


def test_a_refusal_through_the_entry_point_is_one_line_and_exit_one(
    world: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (world / "client.py").write_text("mine\n", encoding="utf-8")
    monkeypatch.chdir(world)
    result = run_cli(capsys, "hub")
    assert result.code == 1
    assert result.out == ""
    assert result.err.count("\n") == 1
    assert "client.py exists" in result.err
    assert (world / "client.py").read_text(encoding="utf-8") == "mine\n"


def test_pytest_passes_on_a_world_named_like_its_directory_after_hub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A one-word name makes the directory and the package the same name, `demo`,
    and the root `__init__.py` the hub files add is then a package `demo` too."""
    monkeypatch.chdir(tmp_path)
    assert run_cli(capsys, "new", "demo").code == 0
    monkeypatch.chdir(tmp_path / "demo")
    assert run_cli(capsys, "hub").code == 0
    assert (tmp_path / "demo" / "__init__.py").is_file()
    assert_scaffold_pytest_passes(tmp_path / "demo")
