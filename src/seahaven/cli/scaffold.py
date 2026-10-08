"""The templates `seahaven new` and `seahaven hub` render, and how they are rendered.

The templates are ordinary files with a `.tmpl` suffix under `templates/<group>/`,
substituted with `string.Template` and written out without the suffix. The suffix
is what keeps a directory full of `$package` out of the repository's own ruff, ty
and pytest runs: a `.py` holding `from $package.world import world` is not Python,
and every tool in the tree would have an opinion about it. The package directory
is spelled `PACKAGE` in the template tree for the same reason, and is renamed on
the way out.
"""

from collections.abc import Iterator, Mapping
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from string import Template

__all__ = ["TEMPLATE_SUFFIX", "render_group", "render_part"]

TEMPLATE_SUFFIX = ".tmpl"
# The directory the world's package is spelled as inside `templates/`.
PACKAGE_PLACEHOLDER = "PACKAGE"


def render_group(group: str, package: str, substitutions: Mapping[str, str]) -> dict[Path, str]:
    """Every template in `templates/<group>/`, rendered, by the path it is written to."""
    return {
        _destination(relative, package): _substitute(source, substitutions)
        for source, relative in _templates(_root(group), Path())
    }


def render_part(group: str, name: str, substitutions: Mapping[str, str]) -> str:
    """One template, `templates/<group>/<name>.tmpl`, rendered."""
    return _substitute(_root(group) / f"{name}{TEMPLATE_SUFFIX}", substitutions)


def _root(group: str) -> Traversable:
    return resources.files(__package__) / "templates" / group


def _substitute(source: Traversable, substitutions: Mapping[str, str]) -> str:
    return Template(source.read_text(encoding="utf-8")).substitute(substitutions)


def _templates(root: Traversable, prefix: Path) -> Iterator[tuple[Traversable, Path]]:
    """Every template under `root`, with its path relative to the group."""
    for entry in sorted(root.iterdir(), key=lambda entry: entry.name):
        here = prefix / entry.name
        if entry.is_dir():
            yield from _templates(entry, here)
        elif entry.name.endswith(TEMPLATE_SUFFIX):
            yield entry, here


def _destination(relative: Path, package: str) -> Path:
    """Where a template lands: the suffix dropped, `PACKAGE` renamed."""
    parts = [package if part == PACKAGE_PLACEHOLDER else part for part in relative.parts]
    parts[-1] = parts[-1][: -len(TEMPLATE_SUFFIX)]
    return Path(*parts)
