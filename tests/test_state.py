"""The state document: the framework's envelope, the two built-in formats, and a world's own.

`test_change_log.py` pins what a record is. What is pinned here is the document
around it: the provenance the framework writes, which format fills `state`, how a
world registers one of its own, and every way of naming a format that is refused
and where.
"""

import json
import threading
from pathlib import Path
from typing import Any

import jsonschema
import pytest

import seahaven
from seahaven import state as state_module
from seahaven.ctx import Ctx
from seahaven.errors import WorldBug
from seahaven.fixtures import load
from seahaven.instances import Instance
from seahaven.world import World
from tests.conftest import INSTANT_ISO, NOTES_SCHEMA, WAIT, Caller, build_world
from tests.test_change_log import counting_statements
from tests.test_changes import add
from tests.test_instances import frozen_fixture

LAST_STEP = "seahaven.state+last_step/1"
SCHEMA = json.loads((Path(__file__).parent / "support" / "state_v1.schema.json").read_text())

# The envelope's fields, in the order `functional_spec.md` §3.1 publishes them,
# with `state` last. A reader is promised the fields; the order is what makes two
# identical episodes serialise byte for byte.
ENVELOPE = [
    "format",
    "seahaven_version",
    "world",
    "fixture",
    "episode_id",
    "seed",
    "now",
    "startup",
    "call_count",
]


def log(document: dict[str, Any]) -> list[dict[str, Any]]:
    return document["state"]["db"]["log"]


@pytest.fixture
def world_with_startup(tmp_path: Path) -> World:
    """A world whose startup hook takes a keyword, so `startup` has something in it."""
    world = build_world(tmp_path)

    @world.instance_startup
    def remember(
        ctx: Ctx, *, tier: str = "free", quota: int = 0, config: dict[str, Any] | None = None
    ) -> None:
        ctx.state["tier"] = tier
        ctx.state["quota"] = quota
        ctx.state["config"] = config

    return world


def test_the_document_is_the_envelope_then_state(instance: Instance) -> None:
    assert list(instance.state()) == [*ENVELOPE, "state"]


def test_the_envelope_of_a_blank_instance(world: World) -> None:
    with world.instance(None, seed=7, now=INSTANT_ISO) as instance:
        document = instance.state()

    assert document["format"] == "seahaven.state/1"
    assert document["seahaven_version"] == seahaven.__version__
    assert document["world"] == {"name": world.name, "version": world.version}
    assert document["fixture"] is None
    assert document["episode_id"] == instance.id
    assert document["seed"] == 7
    assert document["now"] == INSTANT_ISO
    assert document["startup"] == {}
    assert document["call_count"] == 0


def test_the_envelope_of_a_fixture_instance_names_the_fixture_and_its_hash(world: World) -> None:
    fixture_id = frozen_fixture(world)
    frozen = load(world.fixtures_dir / fixture_id)

    with world.instance(fixture_id) as instance:
        document = instance.state()

    assert document["fixture"] == {"id": fixture_id, "file_sha256": frozen.meta.file_sha256}
    assert document["now"] == INSTANT_ISO


def test_no_seed_is_reported_as_none_and_not_as_the_derived_bytes(instance: Instance) -> None:
    """`seed` is what the caller gave; `inst.seed` is what it was hashed into."""
    assert instance.state()["seed"] is None
    assert instance.seed


def test_startup_carries_the_keywords_the_hooks_were_given(world_with_startup: World) -> None:
    with world_with_startup.instance(None, tier="pro", quota=5) as instance:
        assert instance.state()["startup"] == {"tier": "pro", "quota": 5}


def test_startup_is_empty_when_none_were_given(world_with_startup: World) -> None:
    with world_with_startup.instance(None) as instance:
        assert instance.state()["startup"] == {}


def test_editing_a_document_does_not_edit_the_instance(world_with_startup: World) -> None:
    """The document is the caller's; the instance keeps its own copy, all the way down."""
    with world_with_startup.instance(None, tier="pro") as instance:
        add(instance, "n1")
        document = instance.state()
        document["startup"]["tier"] = "edited"
        log(document)[0]["key"]["id"] = "edited"
        log(document)[0]["after"]["id"] = "edited"

        assert instance.state()["startup"] == {"tier": "pro"}
        assert log(instance.state())[0]["key"] == {"id": "n1"}
        assert log(instance.state())[0]["after"]["id"] == "n1"
        assert instance.change_log()[0].key == {"id": "n1"}
        after = instance.change_log()[0].after
        assert after is not None and after["id"] == "n1"


def test_editing_a_nested_startup_value_does_not_edit_the_instance(
    world_with_startup: World,
) -> None:
    """A startup keyword can nest, so the copy has to reach further than one level."""
    with world_with_startup.instance(None, config={"a": 1}) as instance:
        document = instance.state()
        document["startup"]["config"]["a"] = 99

        assert instance.state()["startup"] == {"config": {"a": 1}}


def test_a_startup_keyword_that_is_not_json_able_is_refused_at_creation(
    world_with_startup: World,
) -> None:
    """A value no document could carry fails now, not at the end of an episode."""
    with pytest.raises(WorldBug, match="startup keyword 'tier' must be JSON-able"):
        world_with_startup.instance(None, tier=b"pro")


def test_the_call_count_is_the_instance_s(instance: Instance) -> None:
    add(instance, "n1")
    add(instance, "n2")

    assert instance.state()["call_count"] == 2


def test_state_v1_is_the_whole_log(instance: Instance) -> None:
    add(instance, "n1")
    instance.call("execute", sql="UPDATE notes SET n = 9 WHERE id = 'n1'")

    assert log(instance.state()) == [record.to_dict() for record in instance.change_log()]
    assert [record["i"] for record in log(instance.state())] == [0, 1]


def test_state_v1_is_empty_before_any_call(instance: Instance) -> None:
    assert instance.state()["state"] == {"db": {"log": []}}


def test_last_step_holds_only_the_last_call(world: World) -> None:
    with world.instance(None, state_format=LAST_STEP) as instance:
        add(instance, "n1")
        add(instance, "n2")

        document = instance.state()

    assert document["format"] == LAST_STEP
    assert [record["key"]["id"] for record in log(document)] == ["n2"]


def test_last_step_documents_concatenate_into_the_whole_log(world: World) -> None:
    """The scope is the call counter, which is what makes the per-step reads add up."""
    with world.instance(None) as instance:
        steps = []
        add(instance, "n1")
        steps.extend(log(instance.state(format=LAST_STEP)))
        instance.call("execute", sql="UPDATE notes SET n = 9 WHERE id = 'n1'")
        steps.extend(log(instance.state(format=LAST_STEP)))
        instance.call("execute", sql="DELETE FROM notes WHERE id = 'n1'")
        steps.extend(log(instance.state(format=LAST_STEP)))

        assert steps == log(instance.state())


def test_last_step_is_empty_before_any_call(world: World) -> None:
    with world.instance(None, state_format=LAST_STEP) as instance:
        assert log(instance.state()) == []


def test_last_step_never_holds_a_bulk_write(world: World) -> None:
    """A `bulk()` write belongs to no call, so no call's document claims it."""
    with world.instance(None, state_format=LAST_STEP) as instance:
        add(instance, "n1")
        with instance.bulk() as ctx:
            ctx.db.execute("INSERT INTO notes VALUES ('b1', 'bulk', 0)")

        assert [record["key"]["id"] for record in log(instance.state())] == ["n1"]


def test_two_reads_between_calls_answer_the_same_document(world: World) -> None:
    with world.instance(None, seed=1, now=INSTANT_ISO, state_format=LAST_STEP) as instance:
        add(instance, "n1")

        assert json.dumps(instance.state()) == json.dumps(instance.state())


def test_a_format_can_be_named_for_one_read(instance: Instance) -> None:
    """The instance's own format is unaffected: `format=` answers, it does not switch."""
    add(instance, "n1")
    add(instance, "n2")

    named = instance.state(format=LAST_STEP)

    assert named["format"] == LAST_STEP
    assert [record["key"]["id"] for record in log(named)] == ["n2"]
    assert instance.state()["format"] == "seahaven.state/1"
    assert len(log(instance.state())) == 2


def test_a_custom_formatter_fills_state_and_nothing_else(world: World) -> None:
    @world.state_format("acme.state/1")
    def acme(world: World, instance: Instance | None) -> dict[str, Any]:
        return {"rows": len(instance.change_log()) if instance else 0}

    with world.instance(None, seed=3, now=INSTANT_ISO, state_format="acme.state/1") as instance:
        add(instance, "n1")
        custom = instance.state()
        built_in = instance.state(format="seahaven.state/1")

    assert custom["state"] == {"rows": 1}
    assert custom["format"] == "acme.state/1"
    assert {name: custom[name] for name in ENVELOPE if name != "format"} == {
        name: built_in[name] for name in ENVELOPE if name != "format"
    }


def test_a_custom_formatter_can_edit_a_built_in(world: World) -> None:
    """FS §6's recipe: read the built-in's `state` from inside the formatter and change it."""

    @world.state_format("acme.trimmed/1")
    def trimmed(world: World, instance: Instance | None) -> dict[str, Any]:
        assert instance is not None
        inner = instance.state(format="seahaven.state/1")["state"]
        return {"tables": sorted({record["table"] for record in inner["db"]["log"]})}

    with world.instance(None, state_format="acme.trimmed/1") as instance:
        add(instance, "n1")

        assert instance.state()["state"] == {"tables": ["notes"]}


def test_a_formatter_that_returns_something_else_is_a_world_bug(world: World) -> None:
    world.state_format("acme.state/1")(lambda world, instance: [1, 2])  # ty: ignore[invalid-argument-type]

    with (
        world.instance(None, state_format="acme.state/1") as instance,
        pytest.raises(WorldBug, match="returns the value of 'state'"),
    ):
        instance.state()


def test_a_formatter_that_calls_a_tool_is_a_world_bug(world: World) -> None:
    """The lock is re-entrant, so the guard is what stops a formatter writing."""

    @world.state_format("acme.writes/1")
    def writes(world: World, instance: Instance | None) -> dict[str, Any]:
        assert instance is not None
        add(instance, "written_by_a_formatter")
        return {}

    with world.instance(None, state_format="acme.writes/1") as instance:
        with pytest.raises(WorldBug, match="never writes to it"):
            instance.state()

        assert instance.change_log() == []


def test_a_formatter_that_bulk_writes_is_a_world_bug(world: World) -> None:
    @world.state_format("acme.bulk/1")
    def writes(world: World, instance: Instance | None) -> dict[str, Any]:
        assert instance is not None
        with instance.bulk() as ctx:
            ctx.db.execute("INSERT INTO notes VALUES ('b1', 'bulk', 0)")
        return {}

    with (
        world.instance(None, state_format="acme.bulk/1") as instance,
        pytest.raises(WorldBug, match="never writes to it"),
    ):
        instance.state()


def test_state_cannot_run_inside_bulk(instance: Instance) -> None:
    """A formatter never runs in a transaction: the rows it would read are not committed."""
    with instance.bulk() as ctx:
        ctx.db.execute("INSERT INTO notes VALUES ('b1', 'bulk', 0)")

        with pytest.raises(WorldBug, match="state cannot run inside a transaction"):
            instance.state()

    assert len(log(instance.state())) == 1


def test_a_call_from_another_thread_is_not_refused_while_a_formatter_runs(world: World) -> None:
    """The guard is against the formatting thread, not against the instance."""
    running = threading.Event()
    finish = threading.Event()

    @world.state_format("acme.slow/1")
    def slow(world: World, instance: Instance | None) -> dict[str, Any]:
        running.set()
        assert finish.wait(WAIT)
        return {}

    with world.instance(None, state_format="acme.slow/1") as instance:
        reader = Caller(instance.state)
        reader.start()
        assert running.wait(WAIT)
        caller = Caller(lambda: add(instance, "n1"))
        caller.start()
        # The call waits for the instance lock rather than being refused, so it
        # is still unfinished while the formatter holds it.
        caller.join(0.1)
        assert caller.is_alive(), "the call was refused instead of waiting for the lock"
        finish.set()
        reader.finish()
        caller.finish()

        assert len(instance.change_log()) == 1


def test_a_call_is_allowed_again_once_the_formatter_has_finished(world: World) -> None:
    """The guard is saved and restored, so a nested read does not disarm the outer one."""

    @world.state_format("acme.nested/1")
    def nested(world: World, instance: Instance | None) -> dict[str, Any]:
        assert instance is not None
        instance.state(format="seahaven.state/1")
        with pytest.raises(WorldBug, match="never writes to it"):
            add(instance, "written_by_a_formatter")
        return {}

    with world.instance(None, state_format="acme.nested/1") as instance:
        instance.state()

        add(instance, "n1")
        assert len(instance.change_log()) == 1


@pytest.mark.parametrize(
    "bad", ["", "acme.state", "acme.state/", "/1", "acme.state/0", "acme.state/01", "acme state/1"]
)
def test_a_format_name_is_a_family_and_a_major(world: World, bad: str) -> None:
    with pytest.raises(WorldBug, match="not a state format name"):
        world.state_format(bad)


def test_a_world_cannot_register_a_seahaven_name(world: World) -> None:
    with pytest.raises(WorldBug, match="reserved"):
        world.state_format("seahaven.state/2")


def test_a_format_is_registered_once(world: World) -> None:
    world.state_format("acme.state/1")(lambda world, instance: {})

    with pytest.raises(WorldBug, match="registered twice"):
        world.state_format("acme.state/1")(lambda world, instance: {})


def test_a_formatter_must_be_callable(world: World) -> None:
    with pytest.raises(WorldBug, match="a state formatter is a function"):
        world.state_format("acme.state/1")(42)  # ty: ignore[invalid-argument-type]


def test_a_world_must_pin_a_format(tmp_path: Path) -> None:
    with pytest.raises(WorldBug) as raised:
        World("w", "1.0.0", NOTES_SCHEMA, fixtures_dir=tmp_path)

    assert "must pin a state format" in str(raised.value)
    assert "seahaven.state+last_step/1, seahaven.state/1" in str(raised.value)


def test_an_unknown_seahaven_format_is_refused_at_the_world(tmp_path: Path) -> None:
    with pytest.raises(WorldBug, match="which Seahaven does not publish"):
        World("w", "1.0.0", NOTES_SCHEMA, state_format="seahaven.state/9", fixtures_dir=tmp_path)


def test_a_format_name_that_is_not_one_is_refused_at_the_world(tmp_path: Path) -> None:
    with pytest.raises(WorldBug, match="not a state format name"):
        World("w", "1.0.0", NOTES_SCHEMA, state_format="acme.state", fixtures_dir=tmp_path)


def test_a_custom_pin_is_refused_at_the_first_instance(tmp_path: Path) -> None:
    """A custom name cannot be checked at `World(...)`: registration happens after that line."""
    world = build_world(tmp_path, state_format="acme.state/1")

    with pytest.raises(WorldBug) as raised:
        world.instance(None)

    assert "has no state format 'acme.state/1'" in str(raised.value)
    assert "this world registers none" in str(raised.value)


def test_a_custom_pin_registered_after_the_world_line_works(tmp_path: Path) -> None:
    world = build_world(tmp_path, state_format="acme.state/1")
    world.state_format("acme.state/1")(lambda world, instance: {"ok": True})

    with world.instance(None) as instance:
        assert instance.state()["state"] == {"ok": True}


def test_an_unknown_format_leaves_no_directory_behind(tmp_path: Path) -> None:
    world = build_world(tmp_path)
    work = tmp_path / "work"
    work.mkdir(parents=True, exist_ok=True)

    with pytest.raises(WorldBug, match="has no state format"):
        world.instance(None, state_format="acme.state/1")

    assert list(work.iterdir()) == []


def test_an_unknown_format_named_for_one_read_is_refused(instance: Instance) -> None:
    with pytest.raises(WorldBug, match="has no state format"):
        instance.state(format="acme.state/1")


def test_state_on_a_destroyed_instance_is_refused(world: World) -> None:
    instance = world.instance(None)
    instance.destroy()

    with pytest.raises(WorldBug, match="has been destroyed"):
        instance.state()


@pytest.mark.parametrize("name", ["seahaven.state/1", LAST_STEP])
def test_a_built_in_answers_a_document_with_no_instance(world: World, name: str) -> None:
    """Before the first `reset` over OpenEnv there is no instance, and the world is still known."""
    document = state_module.document(world, None, name, world.resolve_state_format(name))

    assert list(document) == [*ENVELOPE, "state"]
    assert document["format"] == name
    assert document["world"] == {"name": world.name, "version": world.version}
    assert [document[field] for field in ("fixture", "episode_id", "seed", "now", "startup")] == [
        None
    ] * 5
    assert document["call_count"] == 0
    assert document["state"] == {"db": {"log": []}}


def test_a_formatter_that_refuses_no_instance_says_so(world: World) -> None:
    """The author's contract to keep: a formatter that raises on `None` raises here."""

    @world.state_format("acme.strict/1")
    def strict(world: World, instance: Instance | None) -> dict[str, Any]:
        if instance is None:
            raise WorldBug("acme.strict/1 needs an episode")
        return {}

    with pytest.raises(WorldBug, match="needs an episode"):
        state_module.document(world, None, "acme.strict/1", strict)


def test_state_does_no_database_work(instance: Instance) -> None:
    """The log was rendered as each call committed; the document is serialisation only."""
    add(instance, "n1")
    conn = instance.db.conn

    with counting_statements(conn) as watched:
        assert conn.execute("SELECT 1").get == 1
    assert watched  # the counter sees a statement when there is one

    with counting_statements(conn) as statements:
        assert instance.state()

    assert statements == []


@pytest.mark.parametrize("name", ["seahaven.state/1", LAST_STEP])
def test_a_built_in_document_is_the_published_shape(world: World, name: str) -> None:
    fixture_id = frozen_fixture(world)
    with world.instance(fixture_id, seed=11, state_format=name) as instance:
        add(instance, "n1")
        instance.call("execute", sql="UPDATE notes SET n = 4 WHERE id = 'n1'")
        instance.call("execute", sql="DELETE FROM notes WHERE id = 'n1'")
        with instance.bulk() as ctx:
            ctx.db.execute("INSERT INTO notes VALUES ('b1', 'bulk', 0)")
        document = instance.state()

    jsonschema.validate(json.loads(json.dumps(document)), SCHEMA)
    assert log(document)


def test_a_blank_instance_document_is_the_published_shape(world: World) -> None:
    with world.instance(None, now=INSTANT_ISO) as instance:
        jsonschema.validate(json.loads(json.dumps(instance.state())), SCHEMA)


def test_the_schema_refuses_a_document_with_a_field_nobody_published(instance: Instance) -> None:
    """The schema is only a guard if it fails on the thing it is guarding against."""
    document = instance.state()
    document["reward"] = 1.0

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(document, SCHEMA)
