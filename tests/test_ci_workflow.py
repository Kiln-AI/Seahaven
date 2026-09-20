"""The CI workflow's two environments, read out of the workflow file.

`serve` and `mcp` are conflicting extras, so CI is two jobs and no single run
sees the whole tree. `ty` resolves imports against the environment it runs in,
so the type check is split by path: what the `serve` job excludes is what the
`mcp` job includes. Nothing in the code can say whether that split still covers
everything -- the answer is in `ci.yml` -- so these tests read it, and a file
that no job type-checks fails here rather than being found by whoever next edits
the job it fell out of.

`tests/test_licence_check.py` reads the same file for the same reason, about the
licence gate.
"""

import re
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
# The two pages that tell a person which checks to run before committing. Both
# claim to be what CI runs, so both carry the split type check as CI spells it.
COMMAND_LISTS = (REPO_ROOT / "AGENTS.md", REPO_ROOT / "CONTRIBUTING.md")

# The directories `[tool.ty.src] include` names in `pyproject.toml`, which is
# what either job's `ty check` walks. Read rather than copied: a root added
# there would otherwise leave the tests below quietly covering less.
CHECKED_ROOTS: tuple[str, ...] = tuple(
    tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["ty"]["src"][
        "include"
    ]
)

# The `serve` job's type check, which walks the whole tree except the paths it
# names: `ty check -c 'src.exclude=["a", "b"]'`.
_TY_EXCLUDE = re.compile(r"""ty check -c 'src\.exclude=\[([^\]]*)\]'""")
_QUOTED = re.compile(r'"([^"]+)"')

# The `mcp` job's type check, which names its paths as arguments, because only a
# path argument narrows what `ty` walks. The paths go through `ls` first, since
# `ty` exits 2 on one that phase 2 has not written yet.
_TY_PATHS = re.compile(r"ls -d (.+?) 2>/dev/null")

# An import of the MCP SDK, and one of OpenEnv: the two packages that are in one
# environment and not the other. A module that imports either can only be
# type-checked on that side of the split.
_IMPORTS_THE_SDK = re.compile(r"^\s*(?:from|import)\s+mcp\b", re.MULTILINE)
_IMPORTS_OPENENV = re.compile(r"^\s*(?:from|import)\s+(?:seahaven\.)?openenv\b", re.MULTILINE)

pytestmark = pytest.mark.skipif(
    not WORKFLOW.is_file(), reason="the workflow file needs the checkout"
)


def ty_split() -> dict[str, list[str]]:
    """The paths each job's type check names: what one drops and what the other takes."""
    workflow = WORKFLOW.read_text(encoding="utf-8")
    [excluded] = _TY_EXCLUDE.findall(workflow)
    [checked] = _TY_PATHS.findall(workflow)
    return {"serve drops": _QUOTED.findall(excluded), "mcp takes": checked.split()}


def python_files() -> list[Path]:
    """Every Python file either job's `ty check` walks, as a repository path."""
    found = [
        path.relative_to(REPO_ROOT)
        for root in CHECKED_ROOTS
        for path in (REPO_ROOT / root).rglob("*.py")
    ]
    assert found, "no Python files were found; this test is asking the wrong question"
    return found


def on_the_mcp_side(path: Path, mcp_paths: list[str]) -> bool:
    """Whether `path` is type-checked by the `mcp` job rather than the `serve` one."""
    return any(path == Path(named) or Path(named) in path.parents for named in mcp_paths)


def test_both_jobs_type_check_and_each_names_the_paths_it_covers() -> None:
    split = ty_split()

    assert split["serve drops"], "the serve job's ty check names no paths"
    assert split["mcp takes"], "the mcp job's ty check names no paths"


def test_what_one_job_drops_from_the_type_check_the_other_takes() -> None:
    """The union claim: every file is type-checked by one job or the other.

    The split is by path, and a path dropped from one job without being picked
    up by the other is checked by nobody. `ty` reports no such gap: both jobs
    pass, and the file is simply never read.
    """
    split = ty_split()

    assert set(split["serve drops"]) == set(split["mcp takes"])


def test_the_command_lists_tell_a_person_to_run_the_type_check_ci_runs() -> None:
    """A bare `ty check` is neither half of the split, and fails on the other's paths.

    The `mcp` job's half is not asked for here: it names paths that do not exist
    yet, and a command list is a list of commands a person can run today.
    """
    workflow = WORKFLOW.read_text(encoding="utf-8")
    [command] = [
        line.strip().removeprefix("run: ")
        for line in workflow.splitlines()
        if "ty check -c" in line
    ]

    for page in COMMAND_LISTS:
        written = {line.strip() for line in page.read_text(encoding="utf-8").splitlines()}
        assert command in written, f"{page.name} does not carry `{command}`"


def test_a_module_that_imports_the_mcp_sdk_is_on_the_mcp_side_of_the_split() -> None:
    """The split is by path, and what decides the right side is what a file imports.

    A new file that imports the SDK and is not named by the workflow is checked
    by the `serve` job, where the SDK is not installed, and that job turns red on
    an unresolved import. This says so here instead, against the file that did
    it.
    """
    mcp_paths = ty_split()["mcp takes"]

    for path in python_files():
        if _IMPORTS_THE_SDK.search((REPO_ROOT / path).read_text(encoding="utf-8")):
            assert on_the_mcp_side(path, mcp_paths), f"{path} imports the MCP SDK"


def test_a_module_that_imports_openenv_is_not_on_the_mcp_side_of_the_split() -> None:
    """The same rule the other way round, and the half with subjects today."""
    mcp_paths = ty_split()["mcp takes"]
    importers = [
        path
        for path in python_files()
        if _IMPORTS_OPENENV.search((REPO_ROOT / path).read_text(encoding="utf-8"))
    ]

    assert importers, "no file imports openenv; this test is asking the wrong question"
    for path in importers:
        assert not on_the_mcp_side(path, mcp_paths), f"{path} imports openenv"
