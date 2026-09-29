"""The bundled docs: the layout exists, every link resolves, and the lint reference is not stale.

The pages are hand-written and nothing generates them, so there is no drift test
to write for their prose. What can go wrong silently is checked here: a page
named by the layout (and by `index.md`, and by a scaffolded world's `AGENTS.md`)
that is not there; a relative link or anchor in a page, `README.md` or
`CONTRIBUTING.md` that goes nowhere; the lint reference falling behind the rules,
which is the one page whose content is a list of things that exist elsewhere in
the code; and a page still naming either of the two surfaces the state-format
project removed from the docs, which no fence check catches in prose.
"""

import re
from pathlib import Path
from urllib.parse import unquote

import pytest

import seahaven
from seahaven.cli.docs import docs_path

# The layout, as `index.md` gives it. `components/pytest_and_docs.md` §2 wrote
# down an earlier version of this list; the pages were since rewritten, with
# `fixtures.md` renamed and `serving.md` and `openenv.md` merged.
PAGES = (
    "index.md",
    "concepts.md",
    "authoring.md",
    "composition.md",
    "db_schema_and_fixtures.md",
    "clock.md",
    "testing.md",
    "state.md",
    "serving_and_openenv.md",
    "http_apis.md",
    "extensions.md",
    "projecttracker.md",
    "reference/api.md",
    "reference/lints.md",
    "reference/cli.md",
)

LINTS_PAGE = "reference/lints.md"

# A code as a rule module spells it when it builds the finding, and as `cli/`
# spells it on the `CliError` that `check` renders as one. The constraint this
# puts on the rules is worth knowing: a code must appear as a double-quoted
# literal right after `code=`, and one assembled from a constant or handed in by
# a helper would be outside this check entirely.
_REGISTERED = re.compile(r'code="(SH\d+)"')

# A code as *documented*: the leading cell of a table row, and never a mention in
# prose. Both tables below say in their own text that a retired code is never
# reused, and the sentence naming a code as the example of that would otherwise
# be enough to keep the code "documented" after its row was deleted -- which is
# exactly the staleness these tests exist to catch.
_DOCUMENTED = re.compile(r"^\|\s*(SH\d+)\s*\|", re.MULTILINE)

# A markdown link's target, with an optional title after it. Read from text with
# its code removed, so that code is never taken for a link.
_LINK = re.compile(r"\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
# A target with a scheme (`https:`, `mailto:`) or no scheme but a host is a link
# out, which this suite does not fetch.
_EXTERNAL = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.-]*:|//)")
_FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,}).*?^[ \t]*\1", re.MULTILINE | re.DOTALL)
_CODE_SPAN = re.compile(r"(`+).+?\1")
_HEADING = re.compile(r"^#{1,6}[ \t]+(.+?)[ \t#]*$", re.MULTILINE)
_EXPLICIT_ANCHOR = re.compile(r"<a\s+(?:id|name)=\"([^\"]+)\"")
_LINK_MARKUP = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_HTML_TAG = re.compile(r"<[^>]+>")

DOCS = Path(docs_path())
SOURCE = Path(seahaven.__file__).resolve().parent
# The suite always runs from a checkout, where the repository's own pages are.
REPO = Path(__file__).resolve().parents[1]
LINKED_PAGES = (*(DOCS / page for page in PAGES), REPO / "README.md", REPO / "CONTRIBUTING.md")


def registered_codes() -> set[str]:
    """Every `SHnnn` a finding can be built with, read from the code that builds them.

    The rules are functions and not a registry -- there is no table to import --
    so the source is what there is to ask. Both directories, because `cli/`
    carries the two codes for a world that never got as far as being a `World`.
    """
    found = {
        code
        for directory in ("lint", "cli")
        for path in (SOURCE / directory).rglob("*.py")
        for code in _REGISTERED.findall(path.read_text(encoding="utf-8"))
    }
    assert found, "no lint codes were found in the source; this test is asking the wrong question"
    return found


def documented_codes() -> set[str]:
    return set(_DOCUMENTED.findall((DOCS / LINTS_PAGE).read_text(encoding="utf-8")))


@pytest.mark.parametrize("page", PAGES)
def test_every_page_of_the_layout_exists(page: str) -> None:
    assert (DOCS / page).is_file()


@pytest.mark.parametrize("page", PAGES)
def test_every_page_has_a_heading(page: str) -> None:
    """A stub is a page with a heading on it, never an empty file."""
    assert (DOCS / page).read_text(encoding="utf-8").startswith("# ")


def test_the_docs_are_where_seahaven_docs_says_they_are() -> None:
    """`seahaven docs` prints a directory read out of the installed package.

    The whole tree and not the top level: `reference/` is part of the layout, and
    a page added there without being added to the layout is one nothing links to
    and nobody maintains.
    """
    assert DOCS.is_dir()
    found = sorted(path.relative_to(DOCS).as_posix() for path in DOCS.rglob("*.md"))
    assert found == sorted(PAGES)


def without_fences(text: str) -> str:
    return _FENCE.sub("", text)


def relative_links(text: str) -> list[str]:
    """Every link target in `text` that points into the repository, outside code."""
    prose = _CODE_SPAN.sub("", without_fences(text))
    return [target for target in _LINK.findall(prose) if not _EXTERNAL.match(target)]


def slug(heading: str) -> str:
    """A heading's anchor as GitHub spells it, before a duplicate's `-1` suffix."""
    text = _LINK_MARKUP.sub(r"\1", heading)
    # Code keeps its text, `<name>` included; a tag outside code is dropped.
    text = _HTML_TAG.sub("", _CODE_SPAN.sub(lambda span: re.sub(r"[`<>]", "", span[0]), text))
    return re.sub(r"[^\w\- ]", "", text.lower()).replace(" ", "-")


def anchors(text: str) -> set[str]:
    """Every fragment a link into `text` may name: its headings' slugs and explicit anchors."""
    found: set[str] = set()
    seen: dict[str, int] = {}
    for heading in _HEADING.findall(without_fences(text)):
        base = slug(heading)
        count = seen.get(base, 0)
        seen[base] = count + 1
        found.add(base if count == 0 else f"{base}-{count}")
    return found | set(_EXPLICIT_ANCHOR.findall(text))


def broken_links(page: Path) -> list[str]:
    """Each relative link in `page` whose file, or whose anchor in that file, is not there."""
    bundled = page.is_relative_to(DOCS)
    broken = []
    for target in relative_links(page.read_text(encoding="utf-8")):
        path, _, fragment = target.partition("#")
        resolved = (page.parent / unquote(path)).resolve() if path else page
        if not resolved.exists():
            broken.append(f"{target} (missing)")
        elif bundled and not resolved.is_relative_to(DOCS):
            # The docs ship in the wheel without the rest of the repository.
            broken.append(f"{target} (outside the bundled docs)")
        elif (
            bundled
            and resolved.suffix == ".md"
            and resolved.relative_to(DOCS).as_posix() not in PAGES
        ):
            broken.append(f"{target} (not a page of the layout)")
        elif (
            fragment
            and resolved.suffix == ".md"
            and fragment not in anchors(resolved.read_text(encoding="utf-8"))
        ):
            broken.append(f"{target} (no anchor)")
    return broken


@pytest.mark.parametrize("page", LINKED_PAGES, ids=lambda page: page.name)
def test_every_relative_link_resolves(page: Path) -> None:
    """Pages, files and anchors, for every page that ships and the two a contributor reads first."""
    broken = broken_links(page)
    assert not broken, f"{page.relative_to(REPO)}: {', '.join(broken)}"


def test_a_link_out_of_the_docs_is_not_read_as_a_page_of_the_layout() -> None:
    """Asserted on a sample, because a page with no such link cannot show this."""
    sample = (
        "[the OpenEnv spec](https://example.invalid/spec), [mail](mailto:a@example.invalid), "
        '[concepts](concepts.md "Concepts"), [here](#setup) and `[code](not-a-link.md)`\n'
        "```md\n[fenced](not-a-link-either.md)\n```\n"
    )
    assert relative_links(sample) == ["concepts.md", "#setup"]


def test_a_heading_anchor_is_the_one_github_makes() -> None:
    assert slug("SH103 — a wall-clock `DEFAULT`") == "sh103--a-wall-clock-default"
    assert slug("[Composition](composition.md) and `ctx.worlds`") == "composition-and-ctxworlds"
    assert slug("`seahaven new <name>`") == "seahaven-new-name"
    text = '# Setup\n\n## Setup\n\n```sh\n# not a heading\n```\n<a id="kept"></a>\n'
    assert anchors(text) == {"setup", "setup-1", "kept"}


def test_every_registered_lint_code_is_documented() -> None:
    """The one thing in the docs that goes stale without anyone noticing."""
    missing = registered_codes() - documented_codes()
    assert not missing, (
        f"{sorted(missing)} can be reported by `seahaven check` and is not in {LINTS_PAGE}"
    )


def test_the_lint_reference_documents_no_code_that_does_not_exist() -> None:
    """The other direction: a retired rule left in the reference is a lie too."""
    extra = documented_codes() - registered_codes()
    assert not extra, f"{sorted(extra)} is documented in {LINTS_PAGE} and no rule reports it"


def test_the_lint_packages_own_table_agrees_with_the_reference() -> None:
    """`seahaven/lint/__init__.py`'s docstring is the third copy of the list.

    It is what a reader of the code sees, `reference/lints.md` is what a reader
    of the docs sees, and the rules themselves are the truth. Pinning the two
    copies to the truth is what keeps a new rule from being documented in one
    place only.
    """
    from seahaven import lint

    assert set(_DOCUMENTED.findall(lint.__doc__ or "")) == registered_codes()


# `functional_spec.md` §11: `controller_run_sql` is deprecated and leaves the docs
# but for the one line of `reference/cli.md` that documents the flag it is behind.
# The prefix is what to search for, because it is how every control tool is
# spelled, and the exception is one line rather than a whole page, so that the
# deprecation line cannot quietly grow a worked example.
_CONTROL_PREFIX = "controller_"
_CONTROL_PAGE = "reference/cli.md"

# `Instance.changes()` was removed with the change log. A page that still calls it
# is an example that raises `AttributeError` for whoever pastes it, and no fence
# check catches a mention in prose.
_REMOVED_CALL = "changes()"


@pytest.mark.parametrize("page", PAGES)
def test_no_page_names_a_control_tool_but_the_cli_reference(page: str) -> None:
    text = (DOCS / page).read_text(encoding="utf-8")
    naming = [line for line in text.splitlines() if _CONTROL_PREFIX in line]
    if page != _CONTROL_PAGE:
        assert not naming, f"{page} names a control tool: {naming}"
    else:
        assert len(naming) == 1, f"{page} names a control tool on {len(naming)} lines, not one"


@pytest.mark.parametrize("page", PAGES)
def test_no_page_calls_the_removed_changes_method(page: str) -> None:
    text = (DOCS / page).read_text(encoding="utf-8")
    naming = [line for line in text.splitlines() if _REMOVED_CALL in line]
    assert not naming, f"{page} still calls a method the framework removed: {naming}"
