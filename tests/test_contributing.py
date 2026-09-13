"""The contribution guide, checked against the things it makes claims about.

`CONTRIBUTING.md`'s whole value is that its commands are the ones this repository
actually runs, so the two claims a reader cannot verify by eye are checked here:
that the guide names every check CI runs, in CI's order, and that it is collected
by the docs example harness, which is what parses the `seahaven ...` command lines
in it.

Command lines are compared whole. Substring containment would have let the most
important of them disappear unnoticed: `uv run pytest` occurs inside `uv run
pytest worlds/projecttracker`, so a guide that had stopped telling anyone to run
the framework suite would still have passed a containment check.

Its prose is not checked, and cannot be. What the guide says about the state of
the project -- its interpreter requirement included -- was verified by running it
on a final CPython 3.14 against the committed lock. It quotes no suite counts,
timings or pinned versions on purpose: those drift with every phase, and the lock
and the pytest summary line are where the current figures live.

Nothing here fails when the suite runs against an installed wheel rather than a
checkout, which is where neither file exists: the readers return nothing and the
tests skip, rather than erroring at collection.
"""

import functools
import re
from itertools import chain
from pathlib import Path

import pytest
import yaml

from tests.test_docs_examples import blocks_of, pages

REPO_ROOT = Path(__file__).resolve().parents[1]
GUIDE = REPO_ROOT / "CONTRIBUTING.md"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

needs_checkout = pytest.mark.skipif(
    not (GUIDE.is_file() and WORKFLOW.is_file()),
    reason="the repository's own files are absent from an installed wheel",
)

# A trailing comment on a command line: the guide annotates its three suites in
# line, and the command is what comes before the annotation. The required
# whitespace keeps a `#` that is part of a command out of it.
_TRAILING_COMMENT = re.compile(r"\s+#.*$")


@functools.cache
def ci_commands() -> tuple[str, ...]:
    """Every `uv run` line CI executes, in the order the workflow runs them.

    Only `uv run`: `uv sync` is how CI builds its environment rather than a check
    a contributor performs, and the guide discusses it separately, in the section
    on getting a checkout that runs.

    Empty when the workflow is not there, and empty rather than raising when it is
    there and shaped differently -- a renamed job, say. The readers return nothing
    because they are called at collection time, to parametrise, where an exception
    is a collection error that takes the whole suite down and a skip mark is not
    consulted at all. Nothing is what the floor test below fails loudly on.
    """
    if not WORKFLOW.is_file():
        return ()
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow.get("jobs", {}).get("check", {}).get("steps", [])
    return tuple(
        line.strip()
        for step in steps
        for line in str(step.get("run", "")).splitlines()
        if line.strip().startswith("uv run ")
    )


@functools.cache
def guide_command_blocks() -> tuple[tuple[str, ...], ...]:
    """The guide's shell blocks, each as its command lines with comments stripped.

    Blocks and not one flat list, because the order CI runs its checks in is a
    claim the guide makes about one block of seven lines, not about the page.
    """
    if not GUIDE.is_file():
        return ()
    return tuple(
        tuple(
            stripped
            for line in block.source.splitlines()
            if (stripped := _TRAILING_COMMENT.sub("", line).strip())
        )
        for block in blocks_of(GUIDE)
        if block.language == "sh"
    )


@functools.cache
def guide_commands() -> frozenset[str]:
    """Every command line the guide tells a reader to run, whole."""
    return frozenset(chain.from_iterable(guide_command_blocks()))


@needs_checkout
def test_the_workflow_still_has_checks_to_compare_against() -> None:
    """A workflow this stopped being able to read would make the next test vacuous."""
    assert len(ci_commands()) >= 7, f"only found {ci_commands()} in {WORKFLOW.name}"


@needs_checkout
@pytest.mark.parametrize("command", ci_commands(), ids=str)
def test_every_check_ci_runs_is_in_the_contribution_guide(command: str) -> None:
    """A check CI runs and the guide does not name is a check a contributor skips.

    Whole command lines on both sides: the guide tells a reader to run these, so
    the line is what has to be there, and a line that merely contains it is a
    different instruction.
    """
    assert command in guide_commands(), (
        f"CI runs `{command}` and no command line in CONTRIBUTING.md is it"
    )


@needs_checkout
def test_the_guide_lists_the_checks_in_the_order_ci_runs_them() -> None:
    """The guide says "in its order", and one of its blocks has to be exactly that.

    Not the page: the three suites are named twice, once on their own and once
    among the checks, and only the second is claiming to be CI's list.
    """
    assert ci_commands() in guide_command_blocks(), (
        "no shell block in CONTRIBUTING.md is CI's checks, in CI's order"
    )


@needs_checkout
def test_the_guide_is_checked_by_the_docs_example_harness() -> None:
    """The guide's `seahaven ...` command lines are parsed like any other page's."""
    assert GUIDE in pages()
