"""The changeset: the net difference between the fixture and now, through real calls."""

from pathlib import Path

import pytest

from seahaven.changes import Change
from seahaven.ctx import Ctx
from seahaven.errors import WorldBug
from seahaven.instances import Instance
from seahaven.world import World
from tests.conftest import NOTES_SCHEMA, Boom, build_world

COMPOSITE_SCHEMA = """
CREATE TABLE pairs (a TEXT, b TEXT, v TEXT NOT NULL, PRIMARY KEY (b, a)) STRICT;
"""

FTS_SCHEMA = NOTES_SCHEMA + "CREATE VIRTUAL TABLE docs USING fts5(body);"

BLOB_SCHEMA = "CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB) STRICT;"


def add(instance: Instance, id: str, body: str = "a body", n: int = 0) -> None:
    instance.call("execute", sql=f"INSERT INTO notes VALUES ('{id}', '{body}', {n})")


def test_an_insert_is_one_change(instance: Instance) -> None:
    add(instance, "n1", "hello", 3)

    assert instance.changes() == [
        Change(
            table="notes",
            op="insert",
            key={"id": "n1"},
            before=None,
            after={"id": "n1", "body": "hello", "n": 3},
        )
    ]


def test_a_change_renders_to_a_dict(instance: Instance) -> None:
    add(instance, "n1", "hello", 3)

    (change,) = instance.changes()

    assert change.to_dict() == {
        "table": "notes",
        "op": "insert",
        "key": {"id": "n1"},
        "before": None,
        "after": {"id": "n1", "body": "hello", "n": 3},
    }


def test_an_update_carries_the_changed_columns_and_the_key(world: World) -> None:
    """`before` holds the key and what changed; `after` holds what it changed to."""
    with world.instance(None) as instance:
        add(instance, "n1", "hello", 3)
        instance.freeze("start", "one note")

    with world.instance("start") as instance:
        instance.call("execute", sql="UPDATE notes SET body = 'goodbye' WHERE id = 'n1'")

        assert instance.changes() == [
            Change(
                table="notes",
                op="update",
                key={"id": "n1"},
                before={"id": "n1", "body": "hello"},
                after={"body": "goodbye"},
            )
        ]


def test_a_delete_carries_the_whole_row(world: World) -> None:
    with world.instance(None) as instance:
        add(instance, "n1", "hello", 3)
        instance.freeze("start", "one note")

    with world.instance("start") as instance:
        instance.call("execute", sql="DELETE FROM notes WHERE id = 'n1'")

        assert instance.changes() == [
            Change(
                table="notes",
                op="delete",
                key={"id": "n1"},
                before={"id": "n1", "body": "hello", "n": 3},
                after=None,
            )
        ]


def test_a_composite_key_is_rendered_in_key_order(tmp_path: Path) -> None:
    """`PRIMARY KEY (b, a)`: key order is the key's, not the column list's."""
    world = build_world(tmp_path, COMPOSITE_SCHEMA)
    with world.instance(None) as instance:
        instance.call("execute", sql="INSERT INTO pairs VALUES ('x', 'y', 'v')")

        (change,) = instance.changes()

        assert list(change.key.items()) == [("b", "y"), ("a", "x")]


def test_an_integer_primary_key_is_a_key(tmp_path: Path) -> None:
    world = build_world(tmp_path, "CREATE TABLE things (id INTEGER PRIMARY KEY, name TEXT) STRICT;")
    with world.instance(None) as instance:
        instance.call("execute", sql="INSERT INTO things VALUES (7, 'thing')")

        (change,) = instance.changes()

        assert change.key == {"id": 7}


def test_a_no_op_update_records_nothing(instance: Instance) -> None:
    add(instance, "n1", "hello", 3)
    before = instance.changes()

    instance.call("execute", sql="UPDATE notes SET n = 3 WHERE id = 'n1'")

    assert instance.changes() == before


def test_an_insert_then_an_update_is_one_insert(instance: Instance) -> None:
    add(instance, "n1", "hello", 0)
    instance.call("execute", sql="UPDATE notes SET n = 9 WHERE id = 'n1'")

    (change,) = instance.changes()

    assert change.op == "insert"
    assert change.after == {"id": "n1", "body": "hello", "n": 9}


def test_an_insert_then_a_delete_records_nothing(instance: Instance) -> None:
    add(instance, "n1")
    instance.call("execute", sql="DELETE FROM notes WHERE id = 'n1'")

    assert instance.changes() == []


def test_a_call_that_rolled_back_leaves_no_trace(instance: Instance) -> None:
    with pytest.raises(Boom):
        instance.call("write_then_fail", sql="INSERT INTO notes VALUES ('n1', 'body', 0)")

    assert instance.changes() == []
    assert instance.inspect().rows("SELECT * FROM notes") == []


def test_rows_a_startup_hook_wrote_are_not_agent_changes(tmp_path: Path) -> None:
    """The session is attached after the hooks: seed rows are starting state."""
    world = build_world(tmp_path)

    @world.instance_startup
    def seed(ctx: Ctx) -> None:
        ctx.db.execute("INSERT INTO notes VALUES ('seeded', 'from a hook', 0)")

    with world.instance(None) as instance:
        assert instance.inspect().rows("SELECT id FROM notes") == [{"id": "seeded"}]
        assert instance.changes() == []


def test_rows_bulk_wrote_are_changes(instance: Instance) -> None:
    """`bulk` is the authoring path, not a hook: it happens after creation, and it counts."""
    with instance.bulk() as ctx:
        ctx.db.execute("INSERT INTO notes VALUES ('b1', 'bulk', 0)")

    assert [change.key for change in instance.changes()] == [{"id": "b1"}]


def test_a_table_the_world_lists_as_untracked_is_absent(tmp_path: Path) -> None:
    world = build_world(
        tmp_path,
        NOTES_SCHEMA + "CREATE TABLE audit (id TEXT PRIMARY KEY, what TEXT) STRICT;",
        untracked_tables=["audit"],
    )
    with world.instance(None) as instance:
        instance.call("execute", sql="INSERT INTO audit VALUES ('a1', 'looked')")
        add(instance, "n1")

        assert [change.table for change in instance.changes()] == ["notes"]


def test_a_table_with_no_explicit_primary_key_is_refused(tmp_path: Path) -> None:
    """It would attach happily and record nothing, which is worse than not starting."""
    world = build_world(tmp_path, NOTES_SCHEMA + "CREATE TABLE loose (x TEXT) STRICT;")

    with pytest.raises(WorldBug) as raised:
        world.instance(None)

    assert "loose" in str(raised.value)
    assert "primary key" in str(raised.value)


def test_a_table_with_no_primary_key_can_be_untracked_instead(tmp_path: Path) -> None:
    world = build_world(
        tmp_path,
        NOTES_SCHEMA + "CREATE TABLE loose (x TEXT) STRICT;",
        untracked_tables=["loose"],
    )
    with world.instance(None) as instance:
        instance.call("execute", sql="INSERT INTO loose VALUES ('x')")

        assert instance.changes() == []


def test_writes_to_an_fts5_table_do_not_appear(tmp_path: Path) -> None:
    """A virtual table cannot be tracked, and its shadow tables are the module's own."""
    world = build_world(tmp_path, FTS_SCHEMA)
    with world.instance(None) as instance:
        instance.call("execute", sql="INSERT INTO docs VALUES ('searchable words')")
        add(instance, "n1")

        assert [change.table for change in instance.changes()] == ["notes"]


def test_changes_are_cumulative_and_survive_a_freeze(instance: Instance) -> None:
    add(instance, "n1")
    instance.freeze("midway", "one note")
    add(instance, "n2")

    assert [change.key for change in instance.changes()] == [{"id": "n1"}, {"id": "n2"}]


def test_a_blob_comes_back_as_base64(tmp_path: Path) -> None:
    world = build_world(tmp_path, BLOB_SCHEMA)
    with world.instance(None) as instance:
        instance.call("execute", sql="INSERT INTO blobs VALUES ('b1', x'00ff')")

        (change,) = instance.changes()

        assert change.after == {"id": "b1", "data": "AP8="}
