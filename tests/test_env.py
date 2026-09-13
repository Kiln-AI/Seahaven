"""`SeahavenEnv`, in process and with no server: the whole session contract.

Nothing here starts uvicorn. The environment class is what OpenEnv calls, so
these tests call it the same way -- `reset`, `step`, `state`, `close`, one
environment object standing for one session -- and `test_server.py` proves the
same behaviour arrives over a real socket.
"""

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from seahaven.ctx import Ctx
from seahaven.errors import SeahavenError, ToolError, WorldBug
from seahaven.world import World
from tests.conftest import INSTANT_ISO, build_world

# The subpackage and not `openenv`: what this module imports is
# `seahaven.openenv`, so that is what has to import for the tests below to mean
# anything. Only an `ImportError` skips -- an extra that is absent, or installed
# and unimportable. Anything else raises, and CI asserts this import separately,
# because an installed extra that skips quietly is a green run that tested none
# of this.
pytest.importorskip(
    "seahaven.openenv", exc_type=ImportError, reason="the serve extra does not import here"
)

from openenv.core.env_server.mcp_types import (
    CallToolAction,
    ListToolsAction,
    ListToolsObservation,
)
from openenv.core.env_server.types import Action, EnvironmentMetadata
from pydantic import BaseModel

from seahaven.openenv.env import (
    SeahavenEnv,
    SeahavenObservation,
    SeahavenState,
    _first_paragraph,
)

CONTROL_SQL = "SELECT count(*) AS n FROM notes"


@pytest.fixture
def env(world: World) -> SeahavenEnv:
    """A session on the shared notes world, with the control tools off."""
    return SeahavenEnv(world, include_control_tools=False)


def make_fixture(world: World, fixture_id: str = "start") -> str:
    """One fixture of a world, made the only way a fixture is ever made."""
    with world.instance(None, now=INSTANT_ISO) as instance:
        instance.call("execute", sql="INSERT INTO notes VALUES ('n0', 'a body', 0)")
        instance.freeze(fixture_id, "One note.")
    return fixture_id


def call(env: SeahavenEnv, tool: str, **arguments: Any) -> SeahavenObservation:
    """One tool call through `step`, typed as the observation it answers."""
    observation = env.step(CallToolAction(tool_name=tool, arguments=arguments))
    assert isinstance(observation, SeahavenObservation)
    return observation


def listing(env: SeahavenEnv) -> ListToolsObservation:
    observation = env.step(ListToolsAction())
    assert isinstance(observation, ListToolsObservation)
    return observation


# --- construction ----------------------------------------------------------


def test_the_environment_is_initialised_as_an_openenv_environment(env: SeahavenEnv) -> None:
    """`__init__` chains to the base's, which is the only thing that sets these.

    `Environment.__init__` binds `transform` and `rubric`. Nothing on Seahaven's
    own path reads either, so an `__init__` that did not chain would look
    perfectly healthy here and raise `AttributeError` on whichever OpenEnv path
    does -- which is the kind of omission that is found in a training run rather
    than in a suite.
    """
    assert env.transform is None
    assert env.rubric is None


# --- reset -----------------------------------------------------------------


def test_reset_makes_an_instance_and_describes_it(env: SeahavenEnv, world: World) -> None:
    fixture_id = make_fixture(world)
    observation = env.reset(fixture=fixture_id)
    instance = env.instance
    assert instance is not None
    assert instance.fixture == fixture_id
    assert observation.result == {
        "fixture": fixture_id,
        "now": INSTANT_ISO,
        "tools": len(instance.tools()),
    }
    assert observation.error is None
    assert observation.done is False
    assert observation.reward is None


def test_a_second_reset_destroys_the_first_instance(env: SeahavenEnv) -> None:
    env.reset()
    first = env.instance
    assert first is not None
    assert first.state_path.exists()
    env.reset()
    assert env.instance is not None
    assert env.instance is not first
    assert not first.dir.exists(), "the first instance's working directory is still there"
    assert first.closed


def test_reset_without_a_fixture_is_a_blank_instance_at_the_wall_clock(env: SeahavenEnv) -> None:
    before = datetime.now(UTC)
    env.reset()
    instance = env.instance
    assert instance is not None
    assert instance.fixture is None
    assert before <= instance.clock.now() <= datetime.now(UTC)
    assert instance.call("rows", sql="SELECT * FROM notes") == []


def test_reset_with_now_puts_a_blank_instance_at_that_time(env: SeahavenEnv) -> None:
    observation = env.reset(now=INSTANT_ISO)
    assert observation.result == {"fixture": None, "now": INSTANT_ISO, "tools": 6}
    assert env.instance is not None
    assert env.instance.clock.iso() == INSTANT_ISO


def test_reset_refuses_now_with_a_fixture(env: SeahavenEnv, world: World) -> None:
    fixture_id = make_fixture(world)
    with pytest.raises(WorldBug, match="now= applies to blank instances only"):
        env.reset(fixture=fixture_id, now=INSTANT_ISO)
    assert env.instance is None


def test_reset_passes_the_seed_through(env: SeahavenEnv) -> None:
    env.reset(seed=7)
    seeded = env.instance
    assert seeded is not None
    minted = seeded.call("mint")
    env.reset(seed=7)
    again = env.instance
    assert again is not None
    assert again.call("mint") == minted


def test_reset_passes_startup_kwargs_to_the_hooks(tmp_path: Path) -> None:
    world = build_world(tmp_path)
    seen: list[str] = []

    @world.instance_startup
    def seed_notes(ctx: Ctx, *, tenant: str = "acme") -> None:
        seen.append(tenant)

    SeahavenEnv(world, include_control_tools=False).reset(tenant="globex")
    assert seen == ["globex"]


def test_an_unknown_startup_kwarg_raises_before_any_directory_exists(
    env: SeahavenEnv, tmp_path: Path
) -> None:
    with pytest.raises(WorldBug, match=r"unknown reset argument\(s\): \['nonsense'\]"):
        env.reset(nonsense=1)
    assert env.instance is None
    work = tmp_path / "work"
    assert not work.exists() or list(work.iterdir()) == []


def test_a_failed_reset_leaves_the_session_as_a_fresh_one(env: SeahavenEnv) -> None:
    """The old instance is destroyed first, so a reset that then fails leaves nothing."""
    env.reset(episode_id="first")
    call(env, "rows", sql="SELECT * FROM notes")
    first = env.instance
    assert first is not None
    with pytest.raises(WorldBug):
        env.reset(nonsense=1)
    assert env.instance is None
    assert not first.dir.exists()
    state = env.state
    assert (state.episode_id, state.step_count, state.fixture, state.now) == (None, 0, None, None)
    # And the session is still usable: another reset is all it takes.
    env.reset(episode_id="second")
    assert env.state.episode_id == "second"


def test_reset_keeps_the_episode_id_it_is_given_and_mints_one_otherwise(env: SeahavenEnv) -> None:
    env.reset(episode_id="ep-1")
    assert env.state.episode_id == "ep-1"
    env.reset()
    minted = env.state.episode_id
    assert minted is not None and minted != "ep-1"
    env.reset()
    assert env.state.episode_id != minted


# --- step: listing ---------------------------------------------------------


def test_list_tools_never_lists_a_control_tool(world: World) -> None:
    for include in (False, True):
        env = SeahavenEnv(world, include_control_tools=include)
        env.reset()
        names = [tool.name for tool in listing(env).tools]
        assert "controller_run_sql" not in names
        assert "controller_changes" not in names
        assert "rows" in names


def test_list_tools_answers_before_a_reset_and_agrees_with_the_instance(env: SeahavenEnv) -> None:
    """Discovery does not need an episode, and the two spellings are one list."""
    before = [tool.model_dump() for tool in listing(env).tools]
    env.reset()
    instance = env.instance
    assert instance is not None
    after = [tool.model_dump() for tool in listing(env).tools]
    assert before == after == instance.tools()
    assert before[0]["description"]


def test_a_listed_tool_carries_its_json_schema(env: SeahavenEnv) -> None:
    env.reset()
    rows = next(tool for tool in listing(env).tools if tool.name == "rows")
    assert rows.input_schema["properties"]["sql"]["type"] == "string"
    assert rows.input_schema["additionalProperties"] is False


# --- step: calling ---------------------------------------------------------


def test_a_call_before_reset_raises(env: SeahavenEnv) -> None:
    with pytest.raises(WorldBug, match="reset first"):
        call(env, "rows", sql="SELECT 1")


def test_a_call_answers_the_tools_result(env: SeahavenEnv) -> None:
    env.reset(now=INSTANT_ISO)
    observation = call(env, "execute", sql="INSERT INTO notes VALUES ('n1', 'body', 0)")
    assert observation.tool_name == "execute"
    assert observation.result == {"rowcount": 1}
    assert observation.error is None
    assert call(env, "rows", sql="SELECT id FROM notes").result == [{"id": "n1"}]


def test_timeout_s_is_accepted_and_ignored(env: SeahavenEnv) -> None:
    env.reset()
    action = CallToolAction(tool_name="rows", arguments={"sql": "SELECT 1 AS n"})
    observation = env.step(action, 0.0)
    assert isinstance(observation, SeahavenObservation)
    assert observation.result == [{"n": 1}]


def test_a_tool_error_is_rendered_onto_the_observation(env: SeahavenEnv) -> None:
    env.reset()
    observation = call(env, "write_then_fail", sql="INSERT INTO notes VALUES ('n1', 'b', 0)")
    assert observation.result is None
    assert observation.error == {"code": "boom", "message": "it did not work out", "details": None}
    assert observation.tool_name == "write_then_fail"
    # The call's transaction rolled back with it, over the wire as in process.
    assert call(env, "rows", sql="SELECT * FROM notes").result == []


def test_an_unknown_tool_is_rendered_like_any_tool_error(env: SeahavenEnv) -> None:
    env.reset()
    observation = call(env, "no_such_tool")
    assert observation.error == {
        "code": "unknown_tool",
        "message": "unknown tool: no_such_tool",
        "details": {"name": "no_such_tool"},
    }


def test_bad_arguments_are_rendered_as_invalid_arguments(env: SeahavenEnv) -> None:
    env.reset()
    observation = call(env, "rows", sql=7)
    assert observation.error is not None
    assert observation.error["code"] == "invalid_arguments"
    assert observation.error["details"]["tool"] == "rows"


# --- step: control tools ---------------------------------------------------


def test_a_control_tool_is_unknown_without_the_flag(env: SeahavenEnv) -> None:
    env.reset()
    observation = call(env, "controller_run_sql", sql=CONTROL_SQL)
    assert observation.error == {
        "code": "unknown_tool",
        "message": "unknown tool: controller_run_sql",
        "details": {"name": "controller_run_sql"},
    }


def test_a_control_tool_is_callable_with_the_flag(world: World) -> None:
    env = SeahavenEnv(world, include_control_tools=True)
    env.reset()
    call(env, "execute", sql="INSERT INTO notes VALUES ('n1', 'body', 0)")
    assert call(env, "controller_run_sql", sql=CONTROL_SQL).result == {
        "columns": ["n"],
        "rows": [[1]],
        "row_count": 1,
        "truncated": False,
    }
    changes = call(env, "controller_changes").result
    assert isinstance(changes, list)
    assert [change["table"] for change in changes] == ["notes"]


def test_the_flag_does_not_reach_a_tool_the_world_does_not_have(world: World) -> None:
    """The flag admits the control tools and nothing else."""
    env = SeahavenEnv(world, include_control_tools=True)
    env.reset()
    assert call(env, "controller_nonsense").error == {
        "code": "unknown_tool",
        "message": "unknown tool: controller_nonsense",
        "details": {"name": "controller_nonsense"},
    }


# --- step: what is not a tool error ---------------------------------------


def test_an_unexpected_exception_becomes_the_generic_error_and_is_logged(
    env: SeahavenEnv, caplog: pytest.LogCaptureFixture
) -> None:
    env.reset()
    with caplog.at_level(logging.ERROR, logger="seahaven.openenv.env"):
        observation = call(env, "crash")
    assert observation.error == {"code": "internal", "message": "internal error", "details": None}
    assert observation.result is None
    record = next(r for r in caplog.records if r.name == "seahaven.openenv.env")
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None
    assert "ValueError" in caplog.text and "a bug in world code" in caplog.text
    # Nothing of the engine's reaches the agent.
    assert "a bug in world code" not in str(observation.error)


def test_a_world_bug_propagates_out_of_step(tmp_path: Path) -> None:
    world = build_world(tmp_path)

    @world.tool
    def misuse(ctx: Ctx) -> None:
        """Fail the way a broken world fails."""
        raise WorldBug("the world is wrong")

    env = SeahavenEnv(world, include_control_tools=False)
    env.reset()
    with pytest.raises(WorldBug, match="the world is wrong"):
        call(env, "misuse")


def test_a_seahaven_error_that_is_neither_propagates(tmp_path: Path) -> None:
    """Caught is `ToolError`; every other `SeahavenError` is the author's, not the agent's."""
    world = build_world(tmp_path)

    @world.tool
    def odd(ctx: Ctx) -> None:
        """Raise the root of the hierarchy, which is neither of the two kinds."""
        raise SeahavenError("neither kind")

    env = SeahavenEnv(world, include_control_tools=False)
    env.reset()
    with pytest.raises(SeahavenError, match="neither kind"):
        call(env, "odd")


def test_a_world_error_subclass_is_rendered_with_its_own_code(tmp_path: Path) -> None:
    world = build_world(tmp_path)

    class NotFound(ToolError):
        def __init__(self, key: str) -> None:
            super().__init__("not_found", f"no note {key}", {"key": key})

    @world.tool
    def fetch(ctx: Ctx, key: str) -> None:
        """Fail the way a world's own error does."""
        raise NotFound(key)

    env = SeahavenEnv(world, include_control_tools=False)
    env.reset()
    assert call(env, "fetch", key="n9").error == {
        "code": "not_found",
        "message": "no note n9",
        "details": {"key": "n9"},
    }


def test_an_action_of_neither_kind_is_a_world_bug(env: SeahavenEnv) -> None:
    env.reset()
    with pytest.raises(WorldBug, match="not on Action"):
        env.step(Action())


# --- state -----------------------------------------------------------------


def test_state_before_reset_names_the_world_and_nothing_else(
    env: SeahavenEnv, world: World
) -> None:
    state = env.state
    assert state.world == world.name
    assert state.fixture is None
    assert state.now is None
    assert state.episode_id is None
    assert state.step_count == 0


def test_state_after_reset_carries_the_fixture_and_the_clock(
    env: SeahavenEnv, world: World
) -> None:
    fixture_id = make_fixture(world)
    env.reset(fixture=fixture_id, episode_id="ep-9")
    state = env.state
    assert (state.world, state.fixture, state.now) == (world.name, fixture_id, INSTANT_ISO)
    assert state.episode_id == "ep-9"


def test_every_step_counts_and_reset_starts_again_from_zero(env: SeahavenEnv) -> None:
    env.reset()
    assert env.state.step_count == 0
    call(env, "rows", sql="SELECT 1")
    listing(env)
    call(env, "no_such_tool")
    assert env.state.step_count == 3
    env.reset()
    assert env.state.step_count == 0


# --- close -----------------------------------------------------------------


def test_close_destroys_the_instance_and_is_idempotent(env: SeahavenEnv) -> None:
    env.reset()
    instance = env.instance
    assert instance is not None
    directory = instance.dir
    env.close()
    assert env.instance is None
    assert instance.closed
    assert not directory.exists()
    env.close()
    assert env.instance is None


def test_close_before_reset_does_nothing(env: SeahavenEnv) -> None:
    env.close()
    assert env.instance is None


def test_a_closed_session_resets_again(env: SeahavenEnv) -> None:
    env.reset()
    env.close()
    env.reset()
    assert env.instance is not None
    assert call(env, "rows", sql="SELECT 1 AS n").result == [{"n": 1}]


# --- what the models publish about themselves ------------------------------

# Every field the two models declare *themselves*, and the description each one
# must carry. Declared, not inherited: OpenEnv describes its own fields, and
# these are the ones Seahaven is answerable for.
DECLARED_DESCRIPTIONS: dict[type[BaseModel], dict[str, str]] = {
    SeahavenObservation: {
        "tool_name": "The tool that was called.",
        "result": "The tool's result. A tool error travels in `error`, never here.",
        "error": (
            "The tool's own error, as `{code, message, details}`, or null when the call "
            "succeeded. A tool error is data the agent reads: it never ends the session."
        ),
    },
    SeahavenState: {
        "fixture": "The fixture the instance was made from, or null for a blank one.",
        "now": "The instance's clock as an ISO-8601 instant, or null before the first reset.",
        "world": "The world this session is connected to. Known before any reset.",
    },
}


@pytest.mark.parametrize("model", list(DECLARED_DESCRIPTIONS))
def test_every_declared_field_publishes_a_description(model: type[BaseModel]) -> None:
    """The rule three review rounds asked for, asserted over the set rather than a field.

    Redeclaring an inherited field *replaces* its schema entry, so a
    redeclaration with no `description` publishes none at all. Round 1 found
    that on `SeahavenObservation.result`, round 2 found the fix had closed that
    one field and left `tool_name` and `error`, and round 3 found
    `SeahavenState`'s three declared fields had never had one either. Three
    findings, one defect, three times "the fix closed the demonstrated case".

    So this asserts over the set: the fields are read off the class, every one
    of them must publish exactly the text above, and a field added to either
    model without a description fails here. `test_server.py` asserts the
    observation's texts really reach a client over `GET /schema`; the state's
    cannot be checked that way, because that endpoint answers
    `State.model_json_schema()` and never sees a subclass (`BACKLOG.md` B13).
    """
    expected = DECLARED_DESCRIPTIONS[model]
    assert set(model.__annotations__) == set(expected)
    published = model.model_json_schema()["properties"]
    assert {name: published[name].get("description") for name in expected} == expected


# --- metadata --------------------------------------------------------------


def test_get_metadata_reads_the_readme(env: SeahavenEnv, world: World, tmp_path: Path) -> None:
    readme = "# Notes\n\nA world of notes, and nothing else.\n\nMore prose.\n"
    (tmp_path / "README.md").write_text(readme, encoding="utf-8")
    metadata = env.get_metadata()
    assert isinstance(metadata, EnvironmentMetadata)
    assert metadata.name == world.name
    assert metadata.version == world.version
    assert metadata.readme_content == readme
    assert metadata.description == "A world of notes, and nothing else."


def test_get_metadata_without_a_readme_falls_back(env: SeahavenEnv, world: World) -> None:
    metadata = env.get_metadata()
    assert metadata.readme_content == ""
    assert metadata.description == f"Seahaven world {world.name}"


def test_get_metadata_falls_back_when_the_readme_has_no_paragraph(
    env: SeahavenEnv, world: World, tmp_path: Path
) -> None:
    (tmp_path / "README.md").write_text("# Notes\n\n## Still a heading\n", encoding="utf-8")
    assert env.get_metadata().description == f"Seahaven world {world.name}"


def test_get_metadata_survives_a_readme_that_is_not_text(
    env: SeahavenEnv, world: World, tmp_path: Path
) -> None:
    """Metadata is not where a world fails: unreadable is the same as absent."""
    (tmp_path / "README.md").write_bytes(b"\xff\xfe not utf-8")
    metadata = env.get_metadata()
    assert metadata.readme_content == ""
    assert metadata.description == f"Seahaven world {world.name}"


def test_get_metadata_reads_a_readme_that_begins_with_a_byte_order_mark(
    env: SeahavenEnv, world: World, tmp_path: Path
) -> None:
    """`_readme` decodes the mark away, which is a separate guard from the string one.

    `_first_paragraph` strips a BOM from whatever string it is handed, but the
    reader is what every real card comes through, and `utf-8` would hand it the
    mark as the first character of the first line. Written here as the bytes an
    editor actually writes, so the codec is what is under test.
    """
    (tmp_path / "README.md").write_bytes(
        "\ufeff---\ntitle: Notes\n---\n\nA card an editor saved with a mark.\n".encode()
    )
    metadata = env.get_metadata()
    assert metadata.description == "A card an editor saved with a mark."
    assert (
        metadata.readme_content == "---\ntitle: Notes\n---\n\nA card an editor saved with a mark.\n"
    )


def test_get_metadata_survives_a_readme_that_is_a_directory(
    env: SeahavenEnv, world: World, tmp_path: Path
) -> None:
    (tmp_path / "README.md").mkdir()
    assert env.get_metadata().readme_content == ""


@pytest.mark.parametrize(
    ("readme", "expected"),
    [
        ("", ""),
        ("Just a line.\n", "Just a line."),
        ("# Title\nStraight after the heading.\n", "Straight after the heading."),
        ("\n\n\n# Title\n\nAfter blank lines.\n", "After blank lines."),
        ("One\nline\nwrapped.\n\nNext.\n", "One line wrapped."),
        ("# A\n# B\n\nAfter two headings.\n", "After two headings."),
        ("Text.\n# Heading right after\n", "Text."),
        (
            "---\ntitle: Notes\nsdk: docker\n---\n\n# Notes\n\nThe card's prose.\n",
            "The card's prose.",
        ),
        (
            "---\ntitle: Notes\n---\nNo blank line after the fence.\n",
            "No blank line after the fence.",
        ),
        # A card whose closing fence was forgotten. Review round 1 found these
        # three published the YAML as the world's description -- and the first
        # of them published it *instead of* the prose below it, because the keys
        # started a paragraph that the blank line then ended. The block ends at
        # the blank line when no fence ends it, so the prose is reached and
        # there is nothing to publish when there is no prose.
        (
            "---\ntitle: Notes\nsdk: docker\n\n# ProjectTracker\n\nReal prose here.\n",
            "Real prose here.",
        ),
        ("---\ntitle: Notes\nsdk: docker\n", ""),
        ("---\ntitle: Notes\n", ""),
        ("---\ntitle: Notes\n\n# Only a heading\n", ""),
        ("---\ntitle: Notes\n---\n", ""),
        # The closing fence on the last line, with nothing after it at all: the
        # block's end is the end of the file and the index must not run past it.
        ("---\ntitle: Notes\n---", ""),
        # Review round 2: this row used to expect `"Text."`. In Markdown a line
        # with a rule under it is a setext heading -- `Text.\n---` is an `<h2>`,
        # not a paragraph -- and a README whose first line is its title spells
        # the title that way about as often as with a `#`. Publishing the title
        # as the description is the bug `# Title` was skipped to avoid, so the
        # expectation changed with the rule rather than being written around.
        ("Text.\n---\n", ""),
        (
            "Title\n=====\n\nThe prose under a setext heading.\n",
            "The prose under a setext heading.",
        ),
        ("Title\n-----\n\nUnder a dashed setext heading.\n", "Under a dashed setext heading."),
        # `___` under a line is a thematic break rather than a setext underline,
        # so CommonMark would call the line above it a paragraph. Seahaven skips
        # it anyway: one rule for all three characters, and a line that someone
        # underlined is a title whichever character they reached for.
        ("Title\n___\n\nUnder underscores.\n", "Under underscores."),
        # The permissive side of the setext rule, and the reason the lookahead
        # is scoped to the paragraph's *first* line: a rule under a later line
        # ends the paragraph, it does not delete it. Widening the lookahead
        # would return `"One"` here, and `""` for a one-line paragraph.
        ("One\nTwo\n===\n", "One Two"),
        ("First line\nSecond line\n---\n", "First line Second line"),
        # A rule with nothing above it is furniture, skipped like a heading.
        ("====\n\nAfter a bare rule.\n", "After a bare rule."),
        ("___\n\nAfter underscores.\n", "After underscores."),
        ("----\n\nAfter four dashes.\n", "After four dashes."),
        # The permissive side of `_is_rule`: a line made of *more* than one
        # repeated rule character is prose. A rule is the whole line or nothing.
        ("- a bullet list item\n", "- a bullet list item"),
        ("--- not a rule ---\n", "--- not a rule ---"),
        ("=> an arrow, not an underline\n", "=> an arrow, not an underline"),
        ("---\n---\n\nEmpty front matter.\n", "Empty front matter."),
        (
            # A horizontal rule further down is not the end of the front matter:
            # the block skip stops at the *closing* fence, not the last one.
            "---\ntitle: Notes\n---\n\nThe card's prose.\n\n---\n\nA later section.\n",
            "The card's prose.",
        ),
        ("   \n\t\nIndented blanks first.\n", "Indented blanks first."),
        # --- a blank line inside a *closed* block --------------------------
        # Review round 2's Major. A blank line is legal YAML and ordinary in a
        # card -- between keys, after the opening fence, inside a list, or as a
        # whitespace-only line -- and the closing fence is what ends the block.
        # None of the rows above has one, which is why a rule that stopped at
        # the first blank line passed the whole suite while publishing YAML for
        # every well-formed card that contained a gap.
        (
            "---\ntitle: Notes\n\nsdk: docker\n---\n\nThe card's prose.\n",
            "The card's prose.",
        ),
        (
            "---\n\ntitle: Notes\n---\n\nA blank right after the opening fence.\n",
            "A blank right after the opening fence.",
        ),
        (
            "---\ntags:\n  - notes\n\n  - sqlite\n---\n\nA tag list with a gap.\n",
            "A tag list with a gap.",
        ),
        (
            "---\ntitle: Notes\n   \n---\n\nA whitespace-only line inside the block.\n",
            "A whitespace-only line inside the block.",
        ),
        (
            "---\n# a yaml comment\n\ntitle: Notes\n---\n\nA comment then a gap.\n",
            "A comment then a gap.",
        ),
        # The same shape with no prose after it: still nothing to publish, and
        # in particular still not `title: Notes sdk: docker`.
        ("---\ntitle: Notes\n\nsdk: docker\n---\n", ""),
        # Unclosed, with a rule further down. The two readings -- a card with a
        # gap closed by that rule, or an unclosed card followed by a section
        # break -- are the same bytes, so this row records which one is taken
        # and what it costs: the description is the prose after the rule rather
        # than the prose before it. Either way it is prose, never YAML, which is
        # the property the block skip exists to guarantee.
        ("---\ntitle: Notes\n\nEarly prose.\n\n---\n\nLater prose.\n", "Later prose."),
        # --- a blank line *before* the card --------------------------------
        # The opening fence is the first non-blank line, not line 0. An editor
        # that leaves a newline at the top of a file must not turn the card's
        # keys into the description.
        (
            "\n---\ntitle: Notes\n---\n\nA blank line before the card.\n",
            "A blank line before the card.",
        ),
        ("\n\n---\ntitle: Notes\nsdk: docker\n\n# Notes\n\nUnclosed too.\n", "Unclosed too."),
        # --- a byte-order mark ---------------------------------------------
        # `str.strip()` leaves a BOM alone: it is not whitespace. Left in place
        # it stops the first line being an opening fence or a heading, so a
        # BOM-prefixed card published `\ufeff---` and then its own YAML.
        (
            "\ufeff---\ntitle: Notes\n---\n\nAfter a byte-order mark.\n",
            "After a byte-order mark.",
        ),
        ("\ufeff# Title\n\nA BOM before a heading.\n", "A BOM before a heading."),
        # The permissive side: a BOM before prose is removed from the prose,
        # not merely tolerated in front of it.
        ("\ufeffJust prose.\n", "Just prose."),
        # --- a card whose last key is not directly above the closing fence --
        # These three are the regression cases for round 2's Major, and the
        # reason they had to be written is worth recording: the six cases added
        # for it in round 2 no longer detect it. The setext lookahead added in
        # round 3 masks them -- with the bound landing on the blank line inside
        # the block, the key after it is skipped as a heading because the
        # closing fence sits directly beneath it, and the fence is then skipped
        # as a rule, so the prose is reached anyway and by accident. A case
        # only discriminates the fused loop when the line after the block's
        # blank line is *not* directly above the fence. Review round 3 found
        # that by reconstructing the mutant and running the table against it,
        # which is the only way a masked test shows up as masked.
        (
            "---\ntitle: Notes\n\nsdk: docker\n\n---\n\nA gap before the closing fence.\n",
            "A gap before the closing fence.",
        ),
        (
            "---\ntitle: Notes\n\ntags:\n  - notes\n---\n\nA tag list behind a gap.\n",
            "A tag list behind a gap.",
        ),
        (
            "---\ntitle: Notes\n\nsdk: docker\napp_file: app.py\n---\n\nTwo keys behind a gap.\n",
            "Two keys behind a gap.",
        ),
        # --- thematic breaks spelled the other three ways -------------------
        # CommonMark builds a thematic break from three or more `-`, `_` or
        # `*`, and allows spaces between them. Review round 3 found `*`
        # missing from the rule set, which published `***` as a world's
        # description -- exactly what the rules exist to prevent.
        ("***\n\nAfter three asterisks.\n", "After three asterisks."),
        ("Title\n***\n\nUnder a starred break.\n", "Under a starred break."),
        ("* * *\n\nAfter a spaced starred break.\n", "After a spaced starred break."),
        ("- - -\n\nAfter a spaced dashed break.\n", "After a spaced dashed break."),
        ("_ _ _\n\nAfter a spaced underscored break.\n", "After a spaced underscored break."),
        # The permissive side of both halves of that rule. A break is three or
        # more characters, so `**` is literal text and `* *` is a bullet list
        # whose item is `*`; and a break is the whole line or nothing, so a
        # starred bullet and a sentence between stars stay prose. Eating any of
        # these would be the same defect as publishing `***`, mirrored.
        ("**\n\nAfter two asterisks.\n", "**"),
        ("* *\n\nAfter two spaced asterisks.\n", "* *"),
        ("* a bullet list item\n", "* a bullet list item"),
        ("*** not a break ***\n", "*** not a break ***"),
        # --- an ATX heading spelled without its space -----------------------
        # The permissive half the heading rule does not have. CommonMark needs
        # a space (or the end of the line) after the run of `#`, so `#1` starts
        # a paragraph there and furniture here, and this README has no
        # description at all rather than the sentence it opens with. Recorded
        # rather than fixed, and pinned rather than left to be discovered:
        # the cost is a fallback description (`get_metadata` answers `Seahaven
        # world <name>`), never a wrong one, and widening the rule is a change
        # to the block that produced every defect this phase's reviews found.
        ("#1 priority is shipping.\n", ""),
    ],
)
def test_first_paragraph(readme: str, expected: str) -> None:
    """A heading is not a paragraph, and a Space card's front matter is not prose."""
    assert _first_paragraph(readme) == expected
