"""`seahaven hub`: add what publishing to a hub needs to a world that exists.

Run in a world `seahaven new` made. It reads the world's name from
`pyproject.toml`, writes the five files `openenv push` validates a directory for,
and adds two sections to `README.md`: a Hugging Face Space's settings as front
matter at the top, and a section on connecting to the published world. Nothing
is imported, so the world does not have to be installed.

All or nothing, and never over anything: every target is checked before the
first file is written, and one that is already there refuses the whole command
with a message naming it. A world that publishes has usually edited these files,
and an edit is not something to guess a merge for.
"""

import argparse
import re
from pathlib import Path

from seahaven.cli import SOURCE_DIRNAME, CliError, package_name, project_name, project_root
from seahaven.cli.scaffold import render_group, render_part

__all__ = ["HUB_FILES", "README", "add_hub_files", "add_parser", "run"]

HUB_TEMPLATES = "hub"
HUB_README_TEMPLATES = "hub_readme"

# The files `openenv push` validates a pushed directory for, at the world's root.
HUB_FILES = ("Dockerfile", "__init__.py", "client.py", "models.py", "openenv.yaml")

README = "README.md"
# The connecting section follows the scaffold README's usage section, the one a
# visitor to the Space reads first; a README without it gets the section at the end.
USAGE_HEADING = "## Using it"
APP_MODULE = "openenv_app.py"

_REMEDY = "seahaven hub adds to a world made by seahaven new"
_FENCE = ("```", "~~~")
_SECTION_HEADING = re.compile(r"#{1,2} ")


def add_parser(subcommands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subcommands.add_parser(
        "hub",
        help="add the files a world needs to publish to a hub",
        description=(
            "Add the files `openenv push` requires, and a Hugging Face Space's README "
            "sections, to this world. Nothing that exists is overwritten."
        ),
    )
    parser.set_defaults(handler=run)


def run(args: argparse.Namespace) -> int:
    """Add the hub files to the world here, and say what was written."""
    root, written = add_hub_files(Path.cwd())
    print(f"added to {root}:")
    for relative in written:
        print(f"  {relative}")
    return 0


def add_hub_files(start: Path) -> tuple[Path, list[Path]]:
    """Add the hub files to the world at or above `start`.

    Returns the world's root and what was written, relative to it.
    """
    root = project_root(start.resolve(), remedy=_REMEDY)
    name = project_name(root, remedy=_REMEDY)
    package = package_name(name)
    _require_the_app(root, package)
    substitutions = {"name": name, "package": package}
    files = render_group(HUB_TEMPLATES, package, substitutions)
    card = render_part(HUB_README_TEMPLATES, "space_card.md", substitutions)
    connecting = render_part(HUB_README_TEMPLATES, "connecting.md", substitutions)

    readme_path = root / README
    readme = readme_path.read_text(encoding="utf-8") if readme_path.is_file() else None
    in_the_way = [f"{relative} exists" for relative in files if (root / relative).exists()]
    if readme is None and readme_path.exists():
        in_the_way.append(f"{README} is not a file")
    in_the_way += _readme_conflicts(readme or "", connecting)
    if in_the_way:
        raise CliError(
            f"seahaven hub never overwrites, so it wrote nothing: {'; '.join(in_the_way)}. "
            f"Move these aside and run it again, or add the rest by hand"
        )

    updated: dict[Path, str] = {}
    if readme is None:
        # `pyproject.toml` names the README, so a world without one does not build.
        files[Path(README)] = card + f"# {name}\n\n" + connecting.rstrip("\n") + "\n"
    else:
        updated[Path(README)] = card + _with_section(readme, connecting)
    _write_all(root, files, updated)
    return root, [*files, *updated]


def _require_the_app(root: Path, package: str) -> None:
    """Refuse a world whose served app is not where the hub files will point."""
    source = root / SOURCE_DIRNAME / package
    if not source.is_dir():
        raise CliError(
            f"{source} is not a directory, and it is where the world's package {package!r} "
            f"has to be; {_REMEDY}"
        )
    if not (source / APP_MODULE).is_file():
        raise CliError(
            f"{source / APP_MODULE} does not exist, and the Dockerfile and openenv.yaml serve "
            f"{package}.openenv_app:app; create it holding `app = seahaven.openenv.app(world)`, "
            f"as seahaven new does"
        )


def _readme_conflicts(readme: str, connecting: str) -> list[str]:
    conflicts = []
    if readme.partition("\n")[0].rstrip() == "---":
        conflicts.append(f"{README} already starts with front matter")
    heading = connecting.partition("\n")[0]
    if heading in readme.splitlines():
        conflicts.append(f"{README} already has a {heading.lstrip('# ')!r} section")
    return conflicts


def _write_all(root: Path, new: dict[Path, str], updated: dict[Path, str]) -> None:
    """Create every file in `new` and rewrite every one in `updated`, or change nothing."""
    created: list[Path] = []
    try:
        for relative, text in new.items():
            path = root / relative
            # Exclusive creation: a file that appeared since the check is refused, not replaced.
            with path.open("x", encoding="utf-8") as handle:
                created.append(path)
                handle.write(text)
        for relative, text in updated.items():
            (root / relative).write_text(text, encoding="utf-8")
    except BaseException:
        for path in created:
            path.unlink(missing_ok=True)
        raise


def _with_section(readme: str, section: str) -> str:
    """`readme` with `section` after its usage section, or at its end."""
    lines = readme.splitlines(keepends=True)
    after = _next_section(lines, USAGE_HEADING)
    if after is None:
        return readme.rstrip("\n") + "\n\n" + section.rstrip("\n") + "\n"
    return "".join(lines[:after]) + section + "".join(lines[after:])


def _next_section(lines: list[str], heading: str) -> int | None:
    """The line the section after `heading`'s starts on, skipping code fences."""
    in_fence = False
    found = False
    for index, line in enumerate(lines):
        text = line.rstrip("\r\n")
        if text.startswith(_FENCE):
            in_fence = not in_fence
        elif in_fence:
            continue
        elif found and _SECTION_HEADING.match(text):
            return index
        elif text == heading:
            found = True
    return None
