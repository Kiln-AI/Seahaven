"""`SeahavenEnv`, in process and with no server: the whole session contract.

Nothing here starts uvicorn. The environment class is what OpenEnv calls, so
these tests call it the same way -- `reset`, `step`, `state`, `close`, one
environment object standing for one session -- and `test_server.py` proves the
same behaviour arrives over a real socket.
"""

import copy
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import emporium
import pytest

import seahaven
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
    FileRef,
    FixtureRef,
    NodeRef,
    SeahavenEnv,
    SeahavenObservation,
    SeahavenState,
    WorldRef,
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
    # `before` is truncated the way a `Clock` truncates its instant: the clock is
    # read before the instance's files are built, so there is no elapsed time to
    # hide a comparison against a microsecond the clock cannot show.
    assert before.replace(microsecond=before.microsecond // 1000 * 1000) <= instance.clock.now()
    assert instance.clock.now() <= datetime.now(UTC)
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
    assert state.state == {"db": {"log": []}}
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


def test_list_tools_serves_the_composite_surface_before_a_reset(tmp_path: Path) -> None:
    """A world that adds worlds has one flat surface, episode or no episode.

    Lives here rather than in `test_composition.py` because it needs the `serve`
    extra, which this module is the one that skips without.
    """
    host = build_world(tmp_path, name="host")
    host.add_world(
        build_world(tmp_path / "added", name="added"),
        name="payments",
        tool_prefix="pay_",
        tool_allow_list=["mint"],
    )
    env = SeahavenEnv(host, include_control_tools=False)
    before = [tool.model_dump() for tool in listing(env).tools]
    env.reset()
    instance = env.instance
    assert instance is not None
    after = [tool.model_dump() for tool in listing(env).tools]
    assert before[-1]["name"] == "pay_mint"
    assert before == after == instance.tools()


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


def test_a_model_a_tool_returned_is_rendered_onto_the_observation(world: World) -> None:
    """`invoke` answers with the tool's own object; this layer owes the wire its rendering.

    Architecture section 8.4. In process a host tool receiving a model receives
    the model; over OpenEnv the observation carries data, exactly as it did when
    `invoke` rendered it.
    """

    class Note(BaseModel):
        id: str
        at: datetime

    @world.tool
    def note(ctx: Ctx) -> Note:
        """A tool that answers with a model rather than a bare dict."""
        return Note(id="n1", at=datetime(2024, 3, 5, 12, tzinfo=UTC))

    env = SeahavenEnv(world, include_control_tools=False)
    env.reset(now=INSTANT_ISO)

    assert call(env, "note").result == {"id": "n1", "at": "2024-03-05T12:00:00Z"}


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


def leaf_node(world: World) -> NodeRef:
    """The one node a world that adds nothing is, as the document reports it."""
    return NodeRef(
        world=world.name,
        world_version=world.version,
        scope=None,
        aliases=[],
        schema_hash=world.schema_hash,
        frozen_world_version=None,
    )


def test_state_before_reset_is_the_worlds_pinned_format_with_no_instance(
    env: SeahavenEnv, world: World
) -> None:
    """`functional_spec.md` §3.5: the framework's envelope, and the world's pin run with `None`.

    Field by field rather than against a dict, because this is the one document
    nothing in process produces: there is no instance to ask, so every value here
    comes from the world or from the "no instance" arm of the envelope.
    """
    state = env.state
    assert state.format == world.pinned_state_format == "seahaven.state/1"
    assert state.seahaven_version == seahaven.__version__
    assert state.world == WorldRef(name=world.name, version=world.version)
    assert state.composition is None
    assert state.fixture is None
    assert state.episode_id is None
    assert state.seed is None
    assert state.now is None
    assert state.startup is None
    assert state.call_count == 0
    assert state.state == {"db": {"log": []}}
    assert state.step_count == 0


def test_state_before_reset_runs_a_custom_pinned_formatter_with_no_instance(
    tmp_path: Path,
) -> None:
    """A world that pins its own format decides what "no episode yet" looks like.

    The framework has no default `state` and never builds one: this is the only
    thing that answers it, and it is handed the `None` the world's formatter was
    written to expect.
    """
    world = build_world(tmp_path, state_format="acme.state/1")

    @world.state_format("acme.state/1")
    def acme(world: World, instance: Any) -> dict[str, Any]:
        return {"episode": "not started" if instance is None else instance.id}

    state = SeahavenEnv(world, include_control_tools=False).state
    assert state.format == "acme.state/1"
    assert state.state == {"episode": "not started"}
    # The envelope is the framework's under every format, custom or built in.
    assert state.world == WorldRef(name=world.name, version=world.version)
    assert state.call_count == 0


def test_state_after_reset_is_the_instances_document_and_the_step_count(
    env: SeahavenEnv,
) -> None:
    """The wire answers exactly what `inst.state()` answers, and nothing beside it.

    One document, built in one place: a session that wrote its own envelope could
    drift from the in-process one, which is the whole reason `state.document`
    exists.
    """
    env.reset(episode_id="ep-9")
    call(env, "execute", sql="INSERT INTO notes VALUES ('n1', 'a body', 0)")
    listing(env)
    instance = env.instance
    assert instance is not None
    state = env.state
    assert state.model_dump(exclude={"step_count"}) == instance.state()
    # `step_count` is OpenEnv's and counts the listing; `call_count` is the
    # document's and does not (`functional_spec.md` §9).
    assert (state.step_count, state.call_count) == (2, 1)
    assert [record["table"] for record in state.state["db"]["log"]] == ["notes"]


def test_state_after_reset_carries_the_one_node_a_leaf_world_is(
    env: SeahavenEnv, world: World
) -> None:
    """A leaf world is a composition of one node, and `state` says so rather than nothing."""
    env.reset()
    assert env.state.composition == {"main": leaf_node(world)}


def test_state_after_reset_carries_the_fixture_and_the_clock(
    env: SeahavenEnv, world: World
) -> None:
    fixture_id = make_fixture(world)
    env.reset(fixture=fixture_id, episode_id="ep-9", seed=7)
    instance = env.instance
    assert instance is not None
    state = env.state
    assert state.world == WorldRef(name=world.name, version=world.version)
    assert instance.fixture_files is not None
    assert state.fixture == FixtureRef(
        id=fixture_id,
        nodes={"main": FileRef(file_sha256=instance.fixture_files["main"])},
    )
    assert (state.now, state.episode_id, state.seed) == (INSTANT_ISO, "ep-9", 7)
    assert state.startup == {}


def test_reset_selects_a_state_format(env: SeahavenEnv) -> None:
    """`reset(state_format=)` overrides the world's pin for the episode."""
    env.reset(state_format="seahaven.state+last_step/1")
    call(env, "execute", sql="INSERT INTO notes VALUES ('n1', 'a body', 0)")
    call(env, "execute", sql="INSERT INTO notes VALUES ('n2', 'a body', 0)")
    state = env.state
    assert state.format == "seahaven.state+last_step/1"
    assert [record["key"]["id"] for record in state.state["db"]["log"]] == ["n2"]


def test_an_unknown_state_format_refuses_the_reset_and_leaves_the_session_fresh(
    env: SeahavenEnv,
) -> None:
    """Refused like any other reset: before anything is copied, session still open."""
    with pytest.raises(WorldBug, match=r"has no state format 'acme\.state/1'"):
        env.reset(state_format="acme.state/1")
    assert env.instance is None
    env.reset()
    assert env.instance is not None


def test_the_state_format_never_reaches_a_startup_hook(tmp_path: Path) -> None:
    """A reserved reset keyword, like `fixture`, `seed` and `now`."""
    world = build_world(tmp_path)
    seen: list[dict[str, Any]] = []

    @world.instance_startup
    def record(ctx: Ctx, **kwargs: Any) -> None:
        """Take every keyword the reset carried, so a leak would show up here."""
        seen.append(kwargs)

    env = SeahavenEnv(world, include_control_tools=False)
    env.reset(state_format="seahaven.state+last_step/1", tenant="globex")
    assert seen == [{"tenant": "globex"}]
    assert env.state.startup == {"tenant": "globex"}


def test_close_discards_the_log_with_the_instance(env: SeahavenEnv) -> None:
    """A closed session answers §3.5's document again: the log went with the instance."""
    env.reset()
    call(env, "execute", sql="INSERT INTO notes VALUES ('n1', 'a body', 0)")
    assert len(env.state.state["db"]["log"]) == 1
    env.close()
    state = env.state
    assert state.state == {"db": {"log": []}}
    assert (state.episode_id, state.call_count, state.composition) == (None, 0, None)


def test_a_second_reset_starts_a_new_log(env: SeahavenEnv) -> None:
    """The log goes with the instance: a new episode starts from nothing."""
    env.reset()
    call(env, "execute", sql="INSERT INTO notes VALUES ('n1', 'a body', 0)")
    assert len(env.state.state["db"]["log"]) == 1
    env.reset()
    state = env.state
    assert state.state == {"db": {"log": []}}
    assert (state.call_count, state.step_count) == (0, 0)


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
        "format": (
            "The format that produced `state`, as `<family>/<major>`. A reader checks this "
            "before reading `state`, and keys on nothing else for it."
        ),
        "seahaven_version": "The Seahaven version of the producing process. Informational only.",
        "world": "The root world this session is connected to.",
        "composition": (
            "Every node of the instance, keyed by canonical path and root first, or null "
            "before the first reset. Never agent-facing."
        ),
        "fixture": "The fixture the instance was made from, or null for a blank one.",
        "seed": "The seed `reset` was given, or null if it was given none.",
        "now": "The instance's clock as an ISO-8601 instant, or null before the first reset.",
        "startup": (
            "The reset keywords beyond `fixture`, `seed`, `now` and `state_format`, exactly as "
            "the startup hooks received them; empty when there were none, null before any reset."
        ),
        "call_count": (
            "How many calls have been dispatched to the instance. Not `step_count`, which also "
            "counts tool listings. The last call's ordinal is one less than this."
        ),
        "state": (
            "The formatter's output, and the only part of this model `format` describes. Left "
            "untyped because its shape is the format's, not the framework's."
        ),
    },
    WorldRef: {
        "name": "The world's name, as `World(name=...)` gives it.",
        "version": (
            "The root world's version. A judge assumes two documents with the same name and "
            "version came from the same schema and the same tools."
        ),
    },
    NodeRef: {
        "world": "The name of the world this node runs.",
        "world_version": "That world's version, as it was added.",
        "scope": "The node's scope, or null for a node that has none.",
        "aliases": ("Every other path that reaches this same node, so a shared store is visible."),
        "schema_hash": ("The SHA-256 of the node's schema, which is what invalidates a fixture."),
        "frozen_world_version": (
            "The world version recorded in the fixture this node was built from, when it "
            "differs from `world_version`; null otherwise."
        ),
    },
    FileRef: {
        "file_sha256": (
            "The SHA-256 of that node's frozen database file, from the fixture's sidecar."
        ),
    },
    FixtureRef: {
        "id": "The fixture's id, as `reset(fixture=...)` named it.",
        "nodes": (
            "Each node's starting file, keyed by the same path `composition` uses. With "
            "`composition`, this is the lookup for the state the episode started from."
        ),
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
    `State.model_json_schema()` and never sees a subclass.
    """
    expected = DECLARED_DESCRIPTIONS[model]
    assert set(model.__annotations__) == set(expected)
    published = model.model_json_schema()["properties"]
    assert {name: published[name].get("description") for name in expected} == expected


# --- metadata --------------------------------------------------------------


def test_get_metadata_reads_the_readme(env: SeahavenEnv, world: World, tmp_path: Path) -> None:
    """The README is published whole, and nothing is derived from it.

    `readme_content` is the card a hub shows, character for character. The
    one-line description is the world's own `description=` and is unaffected by
    what the README says -- here the world gives none, so the fallback stands
    even though the README opens with a perfectly good sentence.
    """
    readme = "# Notes\n\nA world of notes, and nothing else.\n\nMore prose.\n"
    (tmp_path / "README.md").write_text(readme, encoding="utf-8")
    metadata = env.get_metadata()
    assert isinstance(metadata, EnvironmentMetadata)
    assert metadata.name == world.name
    assert metadata.version == world.version
    assert metadata.readme_content == readme
    assert metadata.description == f"Seahaven world {world.name}"


def test_get_metadata_publishes_the_worlds_description(tmp_path: Path) -> None:
    """`World(description=...)` is the one-line description, verbatim.

    Verbatim includes the spaces around it: the fallback is chosen by looking at
    the stripped string, and what is published is the string itself. A world
    whose description has content in it gets that content on its card exactly as
    it was written, and nothing here tidies it.
    """
    world = build_world(tmp_path, description="A world of notes, and nothing else.")
    env = SeahavenEnv(world, include_control_tools=False)
    assert env.get_metadata().description == "A world of notes, and nothing else."

    spaced = build_world(tmp_path, description="  A world.  ")
    assert SeahavenEnv(spaced, include_control_tools=False).get_metadata().description == (
        "  A world.  "
    )


def test_get_metadata_falls_back_when_the_world_gives_no_description(
    env: SeahavenEnv, world: World
) -> None:
    assert world.description is None
    assert env.get_metadata().description == f"Seahaven world {world.name}"


def test_get_metadata_falls_back_on_a_blank_description(tmp_path: Path) -> None:
    """A blank string falls back exactly as `None` does, and on purpose.

    A world that computed its description from something and got `""` publishes
    the fallback rather than a blank line on its card: the fallback is never a
    wrong sentence, and a blank description is not a description.

    Whitespace is the same case and not a lesser one. `description="   "` is a
    string, so it is truthy, and publishing it puts a line on the card that looks
    empty and says nothing -- the one outcome the fallback exists to prevent. It
    is spelled out here because the plain `or` that used to stand in this place
    caught `""` and let this through.
    """
    for blank in ("", "   ", "\n", " \t\n "):
        world = build_world(tmp_path, description=blank)
        env = SeahavenEnv(world, include_control_tools=False)
        assert env.get_metadata().description == f"Seahaven world {world.name}"


def test_get_metadata_without_a_readme_falls_back(env: SeahavenEnv, world: World) -> None:
    metadata = env.get_metadata()
    assert metadata.readme_content == ""
    assert metadata.description == f"Seahaven world {world.name}"


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
    """`utf-8-sig`, not `utf-8`: the mark must not land on the front of the card.

    Read as plain UTF-8 the mark becomes the first character of
    `readme_content`, in front of the card's opening fence. Written here as the
    bytes an editor actually writes, so the codec is what is under test.
    """
    (tmp_path / "README.md").write_bytes(
        "\ufeff---\ntitle: Notes\n---\n\nA card an editor saved with a mark.\n".encode()
    )
    assert (
        env.get_metadata().readme_content
        == "---\ntitle: Notes\n---\n\nA card an editor saved with a mark.\n"
    )


def test_get_metadata_survives_a_readme_that_is_a_directory(
    env: SeahavenEnv, world: World, tmp_path: Path
) -> None:
    (tmp_path / "README.md").mkdir()
    assert env.get_metadata().readme_content == ""


# --- a composite world, end to end -----------------------------------------

# The committed tree of `tests/worlds/README.md`: `emporium` over two payments
# accounts and a shop that shares one of them.
COMPOSITE_TOOLS = [
    "record_charge_owner",
    "settle_order",
    "pay_create_charge",
    "pay_list_charges",
    "eu_create_charge",
    "eu_list_charges",
    "shop_place_order",
]


def charges(env: SeahavenEnv, tool: str) -> list[dict[str, Any]]:
    """One account's charges, as the wire carries them."""
    listed = call(env, tool).result
    assert isinstance(listed, list)
    return listed


@pytest.fixture
def composite(tmp_path: Path) -> SeahavenEnv:
    """A session on the committed composite world, on its own working directory."""
    world = copy.copy(emporium.world)
    world.work_dir = tmp_path / "work"
    return SeahavenEnv(world, include_control_tools=False)


def test_a_composite_serves_one_flat_tool_list_in_declared_order(
    composite: SeahavenEnv,
) -> None:
    """The host's own tools first, then each added world's, in `add_world` order."""
    composite.reset()
    assert [tool.name for tool in listing(composite).tools] == COMPOSITE_TOOLS


def test_a_composite_never_lists_or_serves_a_control_tool(composite: SeahavenEnv) -> None:
    """The control tools are the root's and cover every node; the agent hears of neither."""
    composite.reset()
    assert not any(tool.name.startswith("controller_") for tool in listing(composite).tools)
    refused = call(composite, "controller_run_sql", sql="SELECT 1")
    assert refused.error is not None and refused.error["code"] == "unknown_tool"


def test_a_call_to_a_contributed_tool_runs_against_the_owning_node(
    composite: SeahavenEnv,
) -> None:
    """`pay_` and `eu_` are two accounts of one world: a charge lands in exactly one."""
    composite.reset()
    charge = call(composite, "pay_create_charge", amount=250)
    assert charge.error is None
    created = charge.result
    assert isinstance(created, dict)
    assert [row["id"] for row in charges(composite, "pay_list_charges")] == [created["id"]]
    assert charges(composite, "eu_list_charges") == []


def test_a_composite_tool_reaching_two_nodes_answers_over_the_wire(
    composite: SeahavenEnv,
) -> None:
    """`settle_order` places an order in the shop and charges the company account."""
    composite.reset()
    settled = call(composite, "settle_order", total=40)
    assert settled.error is None
    result = settled.result
    assert isinstance(result, dict)
    assert set(result) == {"order", "charge", "total"}
    assert [row["id"] for row in charges(composite, "pay_list_charges")] == [result["charge"]]


def test_state_carries_every_node_of_a_composite(composite: SeahavenEnv) -> None:
    """What an eval reads to tell what it is running against (architecture section 12)."""
    composite.reset()
    reported = composite.state.composition
    assert reported is not None
    assert list(reported) == ["main", "payments", "payments_eu", "shop"]
    assert [node.world for node in reported.values()] == [
        "emporium",
        "payments",
        "payments",
        "shop",
    ]
    assert [node.scope for node in reported.values()] == [None, None, "eu", None]
    assert [node.aliases for node in reported.values()] == [[], ["shop/payments"], [], []]


def test_the_whole_document_of_a_composite_is_json(composite: SeahavenEnv) -> None:
    """`state` travels over a wire, so every value in it has to survive the trip."""
    composite.reset()
    call(composite, "pay_create_charge", amount=250)
    document = composite.state.model_dump()
    assert json.loads(json.dumps(document)) == document
    assert [record["world"] for record in document["state"]["db"]["log"]] == ["payments"]


def test_nothing_agent_facing_carries_the_composition(composite: SeahavenEnv) -> None:
    """A contributed tool reveals nothing about its origin, listing included."""
    composite.reset()
    listed = [tool.model_dump() for tool in listing(composite).tools]
    assert not any("payments" in json.dumps(tool) for tool in listed)
    assert call(composite, "shop_place_order", total=5).result is not None


def test_resetting_a_composite_session_destroys_every_nodes_directory(
    composite: SeahavenEnv,
) -> None:
    composite.reset()
    first = composite.instance
    assert first is not None
    directory = first.dir
    assert len(list(directory.glob("state*.sqlite"))) == 4
    composite.reset()
    assert not directory.exists()
    assert composite.instance is not None and composite.instance.dir != directory
