"""The change log across calls: what belongs to which call, and what never reaches it.

One record per row per call, in call order. The shape of a single record is
`test_changes.py`'s; what is pinned here is the boundary of a call -- net inside
it, never folded across it -- the ordinal that joins the log to a harness's
trace, and the two things the log is not: the startup hooks' writes, and a
database read.
"""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import apsw
import pytest

from seahaven.ctx import Ctx
from seahaven.errors import UnknownTool, WorldBug
from seahaven.instances import Instance
from seahaven.world import World
from tests.conftest import INSTANT_ISO, Boom, build_world
from tests.test_changes import add


def keys(instance: Instance) -> list[tuple[int | None, str]]:
    """Each record as the call it belongs to and the row it changed."""
    return [(record.i, str(record.key["id"])) for record in instance.change_log()]


@contextmanager
def counting_statements(conn: apsw.Connection) -> Iterator[list[str]]:
    """Every statement run on `conn` for the length of the block."""
    statements: list[str] = []

    def trace(_cursor: apsw.Cursor, sql: str, _bindings: object) -> bool:
        statements.append(sql)
        return True

    conn.exec_trace = trace
    try:
        yield statements
    finally:
        conn.exec_trace = None


def test_the_log_is_empty_before_any_call(instance: Instance) -> None:
    assert instance.change_log() == []
    assert instance.call_count == 0


def test_a_read_only_call_logs_nothing(instance: Instance) -> None:
    add(instance, "n1")

    instance.call("rows", sql="SELECT * FROM notes")

    assert keys(instance) == [(0, "n1")]
    assert instance.call_count == 2


def test_a_call_that_changed_nothing_logs_nothing(instance: Instance) -> None:
    add(instance, "n1", "hello", 3)

    instance.call("execute", sql="UPDATE notes SET n = 3 WHERE id = 'n1'")

    assert keys(instance) == [(0, "n1")]


def test_a_rolled_back_call_logs_nothing(instance: Instance) -> None:
    with pytest.raises(Boom):
        instance.call("write_then_fail", sql="INSERT INTO notes VALUES ('n1', 'body', 0)")

    assert instance.change_log() == []


def test_a_rolled_back_bulk_logs_nothing(instance: Instance) -> None:
    """The recording is outside the transaction, so it reads a changeset that rolled back."""
    with pytest.raises(Boom), instance.bulk() as ctx:
        ctx.db.execute("INSERT INTO notes VALUES ('b1', 'bulk', 0)")
        raise Boom("it did not work out")

    assert instance.change_log() == []


def test_rows_a_startup_hook_wrote_are_not_in_the_log(tmp_path: Path) -> None:
    """The hooks run before any session exists: seed rows are starting state."""
    world = build_world(tmp_path)

    @world.instance_startup
    def seed(ctx: Ctx) -> None:
        ctx.db.execute("INSERT INTO notes VALUES ('seeded', 'from a hook', 0)")

    with world.instance(None) as instance:
        assert instance.change_log() == []


def test_an_insert_then_an_update_in_one_call_is_one_insert(instance: Instance) -> None:
    instance.call(
        "execute",
        sql="INSERT INTO notes VALUES ('n1', 'hello', 0); UPDATE notes SET n = 9 WHERE id = 'n1';",
    )

    (record,) = instance.change_log()

    assert record.op == "insert"
    assert record.after == {"id": "n1", "body": "hello", "n": 9}


def test_an_insert_then_a_delete_in_one_call_logs_nothing(instance: Instance) -> None:
    instance.call(
        "execute",
        sql="INSERT INTO notes VALUES ('n1', 'hello', 0); DELETE FROM notes WHERE id = 'n1';",
    )

    assert instance.change_log() == []


def test_an_update_back_to_the_original_in_one_call_logs_nothing(instance: Instance) -> None:
    add(instance, "n1", "hello", 3)

    instance.call(
        "execute",
        sql="UPDATE notes SET n = 9 WHERE id = 'n1'; UPDATE notes SET n = 3 WHERE id = 'n1';",
    )

    assert keys(instance) == [(0, "n1")]


def test_the_same_row_in_two_calls_is_two_records(instance: Instance) -> None:
    """Not net across calls: counting rows straight off the log overcounts."""
    add(instance, "n1", "hello", 0)
    instance.call("execute", sql="UPDATE notes SET n = 9 WHERE id = 'n1'")

    assert [(record.i, record.op) for record in instance.change_log()] == [
        (0, "insert"),
        (1, "update"),
    ]


def test_an_update_back_to_the_original_across_two_calls_is_two_records(
    instance: Instance,
) -> None:
    add(instance, "n1", "hello", 3)
    instance.call("execute", sql="UPDATE notes SET n = 9 WHERE id = 'n1'")
    instance.call("execute", sql="UPDATE notes SET n = 3 WHERE id = 'n1'")

    assert [(record.i, record.before, record.after) for record in instance.change_log()[1:]] == [
        (1, {"n": 3}, {"n": 9}),
        (2, {"n": 9}, {"n": 3}),
    ]


def test_records_are_ordered_by_call(instance: Instance) -> None:
    add(instance, "n9")
    add(instance, "n1")
    add(instance, "n5")

    assert keys(instance) == [(0, "n9"), (1, "n1"), (2, "n5")]


def test_bulk_writes_carry_no_ordinal(instance: Instance) -> None:
    """`bulk` is authoring, not a call: it happens after creation, and it counts."""
    add(instance, "n1")
    with instance.bulk() as ctx:
        ctx.db.execute("INSERT INTO notes VALUES ('b1', 'bulk', 0)")
    add(instance, "n2")

    assert keys(instance) == [(0, "n1"), (None, "b1"), (1, "n2")]
    assert instance.call_count == 2


def test_every_dispatched_call_consumes_an_ordinal(instance: Instance) -> None:
    """A refusal and a failure are calls the harness made, so they are calls here."""
    with pytest.raises(UnknownTool):
        instance.call("no_such_tool")
    with pytest.raises(Boom):
        instance.call("write_then_fail", sql="INSERT INTO notes VALUES ('n1', 'body', 0)")
    add(instance, "n2")

    assert instance.call_count == 3
    assert keys(instance) == [(2, "n2")]


def test_control_tools_and_tool_listing_are_not_calls(instance: Instance) -> None:
    instance.tools()
    instance.call("controller_run_sql", sql="SELECT 1")
    instance.call("controller_changes")

    assert instance.call_count == 0
    assert instance.change_log() == []


def test_two_identical_episodes_serialise_byte_for_byte(world: World) -> None:
    def episode() -> str:
        with world.instance(None, seed=7, now=INSTANT_ISO) as instance:
            add(instance, "n2", "second", 1)
            add(instance, "n1", "first", 2)
            instance.call("execute", sql="UPDATE notes SET n = 9 WHERE id = 'n2'")
            with pytest.raises(Boom):
                instance.call("write_then_fail", sql="DELETE FROM notes")
            return json.dumps([record.to_dict() for record in instance.change_log()])

    assert episode() == episode()


def test_the_change_log_does_no_database_work(instance: Instance) -> None:
    """The records were rendered when their call committed; reading them is memory."""
    add(instance, "n1")
    conn = instance.db.conn

    with counting_statements(conn) as watched:
        assert conn.execute("SELECT 1").get == 1
    assert watched  # the counter sees a statement when there is one

    with counting_statements(conn) as statements:
        assert instance.change_log()

    assert statements == []


def test_a_destroyed_instance_has_no_change_log(instance: Instance) -> None:
    """The log goes with the instance, as everything the lock guards does."""
    add(instance, "n1")
    instance.destroy()

    with pytest.raises(WorldBug, match="has been destroyed"):
        instance.change_log()
