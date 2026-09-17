"""What a session records, and what it is never given to record, through real calls.

`test_change_log.py` covers what the log says *across* calls -- which call a
record belongs to, and what never reaches the log at all. What is pinned here is
one record's shape, and which tables a session is attached to in the first place.
"""

from pathlib import Path

import pytest

from seahaven.changes import LogRecord
from seahaven.errors import WorldBug
from seahaven.instances import Instance
from tests.conftest import NOTES_SCHEMA, build_world

COMPOSITE_SCHEMA = """
CREATE TABLE pairs (a TEXT, b TEXT, v TEXT NOT NULL, PRIMARY KEY (b, a)) STRICT;
"""

FTS_SCHEMA = NOTES_SCHEMA + "CREATE VIRTUAL TABLE docs USING fts5(body);"

BLOB_SCHEMA = "CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB) STRICT;"


def add(instance: Instance, id: str, body: str = "a body", n: int = 0) -> None:
    instance.call("execute", sql=f"INSERT INTO notes VALUES ('{id}', '{body}', {n})")


# Which tables a session is attached to at all: `changes.tracked_tables`, asked
# once when the instance is made.


def test_a_table_the_world_lists_as_untracked_is_not_logged(tmp_path: Path) -> None:
    world = build_world(
        tmp_path,
        NOTES_SCHEMA + "CREATE TABLE audit (id TEXT PRIMARY KEY, what TEXT) STRICT;",
        untracked_tables=["audit"],
    )
    with world.instance(None) as instance:
        instance.call("execute", sql="INSERT INTO audit VALUES ('a1', 'looked')")
        add(instance, "n1")

        assert [record.table for record in instance.change_log()] == ["notes"]


def test_writes_to_an_fts5_table_are_not_logged(tmp_path: Path) -> None:
    """A virtual table cannot be tracked, and its shadow tables are the module's own."""
    world = build_world(tmp_path, FTS_SCHEMA)
    with world.instance(None) as instance:
        instance.call("execute", sql="INSERT INTO docs VALUES ('searchable words')")
        add(instance, "n1")

        assert [record.table for record in instance.change_log()] == ["notes"]


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

        assert instance.change_log() == []


def test_the_log_survives_a_freeze(instance: Instance) -> None:
    """`freeze()` mints a fixture from the instance; it does not reset what it recorded."""
    add(instance, "n1")
    instance.freeze("midway", "one note")
    add(instance, "n2")

    assert [record.key for record in instance.change_log()] == [{"id": "n1"}, {"id": "n2"}]


# One record's shape: the same session extension, read one call at a time.


NULLABLE_SCHEMA = "CREATE TABLE items (id TEXT PRIMARY KEY, label TEXT, n INTEGER) STRICT;"

REAL_SCHEMA = "CREATE TABLE readings (id TEXT PRIMARY KEY, v REAL) STRICT;"

# Not STRICT, on purpose: a column with no affinity is the only way to hold three
# storage classes in one primary key, which is what the sort's ranking is for. A
# linted world cannot have one, and the ranking is what says so cheaply.
MIXED_KEY_SCHEMA = "CREATE TABLE mixed (k BLOB PRIMARY KEY, v TEXT NOT NULL);"

BLOB_KEY_SCHEMA = "CREATE TABLE keyed (k BLOB PRIMARY KEY, v TEXT NOT NULL) STRICT;"

TWO_TABLE_SCHEMA = (
    NOTES_SCHEMA + "CREATE TABLE albums (id TEXT PRIMARY KEY, title TEXT NOT NULL) STRICT;"
)


def test_an_insert_logs_the_whole_row(instance: Instance) -> None:
    add(instance, "n1", "hello", 3)

    assert instance.change_log() == [
        LogRecord(
            i=0,
            subworld=None,
            table="notes",
            op="insert",
            key={"id": "n1"},
            before=None,
            after={"id": "n1", "body": "hello", "n": 3},
        )
    ]


def test_a_delete_logs_the_whole_row(instance: Instance) -> None:
    add(instance, "n1", "hello", 3)
    instance.call("execute", sql="DELETE FROM notes WHERE id = 'n1'")

    assert instance.change_log()[1] == LogRecord(
        i=1,
        subworld=None,
        table="notes",
        op="delete",
        key={"id": "n1"},
        before={"id": "n1", "body": "hello", "n": 3},
        after=None,
    )


def test_an_update_logs_only_the_columns_it_changed(instance: Instance) -> None:
    """The key is in `key` and in neither side: an update is a delta, not a row."""
    add(instance, "n1", "hello", 3)
    instance.call("execute", sql="UPDATE notes SET body = 'goodbye' WHERE id = 'n1'")

    assert instance.change_log()[1] == LogRecord(
        i=1,
        subworld=None,
        table="notes",
        op="update",
        key={"id": "n1"},
        before={"body": "hello"},
        after={"body": "goodbye"},
    )


def test_an_update_of_several_columns_logs_them_all(instance: Instance) -> None:
    add(instance, "n1", "hello", 3)
    instance.call("execute", sql="UPDATE notes SET body = 'goodbye', n = 9 WHERE id = 'n1'")

    record = instance.change_log()[1]

    assert record.before == {"body": "hello", "n": 3}
    assert record.after == {"body": "goodbye", "n": 9}


def test_a_column_set_to_null_is_a_change(tmp_path: Path) -> None:
    """`NULL` is a value, not an absent column: the two look the same only to a careless reader."""
    world = build_world(tmp_path, NULLABLE_SCHEMA)
    with world.instance(None) as instance:
        instance.call("execute", sql="INSERT INTO items VALUES ('i1', 'a label', 1)")
        instance.call("execute", sql="UPDATE items SET label = NULL WHERE id = 'i1'")

        record = instance.change_log()[1]

        assert record.before == {"label": "a label"}
        assert record.after == {"label": None}


def test_a_composite_key_is_logged_in_key_order(tmp_path: Path) -> None:
    world = build_world(tmp_path, COMPOSITE_SCHEMA)
    with world.instance(None) as instance:
        instance.call("execute", sql="INSERT INTO pairs VALUES ('x', 'y', 'v')")

        (record,) = instance.change_log()

        assert list(record.key.items()) == [("b", "y"), ("a", "x")]


def test_an_integer_primary_key_is_logged_as_a_key(tmp_path: Path) -> None:
    world = build_world(tmp_path, "CREATE TABLE things (id INTEGER PRIMARY KEY, name TEXT) STRICT;")
    with world.instance(None) as instance:
        instance.call("execute", sql="INSERT INTO things VALUES (7, 'thing')")

        (record,) = instance.change_log()

        assert record.key == {"id": 7}


def test_a_blob_is_logged_as_base64(tmp_path: Path) -> None:
    world = build_world(tmp_path, BLOB_SCHEMA)
    with world.instance(None) as instance:
        instance.call("execute", sql="INSERT INTO blobs VALUES ('b1', x'00ff')")

        (record,) = instance.change_log()

        assert record.after == {"id": "b1", "data": "AP8="}


def test_an_infinite_float_is_logged_as_null(tmp_path: Path) -> None:
    """JSON carries no infinity, and SQLite already stores NaN as NULL."""
    world = build_world(tmp_path, REAL_SCHEMA)
    with world.instance(None) as instance:
        instance.call("execute", sql="INSERT INTO readings VALUES ('r1', 1e999), ('r2', 1.5)")

        after = {record.key["id"]: record.after for record in instance.change_log()}

        assert after["r1"] == {"id": "r1", "v": None}
        assert after["r2"] == {"id": "r2", "v": 1.5}


def test_a_log_record_renders_to_a_dict(instance: Instance) -> None:
    add(instance, "n1", "hello", 3)

    (record,) = instance.change_log()

    assert record.to_dict() == {
        "i": 0,
        "subworld": None,
        "table": "notes",
        "op": "insert",
        "key": {"id": "n1"},
        "before": None,
        "after": {"id": "n1", "body": "hello", "n": 3},
    }
    # The published field order, which a saved document is read in.
    assert list(record.to_dict()) == ["i", "subworld", "table", "op", "key", "before", "after"]


def test_records_of_one_call_are_sorted_by_table_then_key(tmp_path: Path) -> None:
    """Written in neither order: the changeset's own is by table and then by rowid."""
    world = build_world(tmp_path, TWO_TABLE_SCHEMA)
    with world.instance(None) as instance:
        instance.call(
            "execute",
            sql=(
                "INSERT INTO notes VALUES ('n2', 'second', 0);"
                "INSERT INTO albums VALUES ('a2', 'second');"
                "INSERT INTO notes VALUES ('n1', 'first', 0);"
                "INSERT INTO albums VALUES ('a1', 'first');"
            ),
        )

        assert [(record.table, record.key["id"]) for record in instance.change_log()] == [
            ("albums", "a1"),
            ("albums", "a2"),
            ("notes", "n1"),
            ("notes", "n2"),
        ]


def test_a_mixed_type_key_sorts_by_type_and_then_by_its_published_value(tmp_path: Path) -> None:
    """Numbers before text, and a blob by the base64 it is published as, not by its bytes.

    A reader of a saved document has the base64 and never the bytes, so the
    published order has to be the one the base64 gives -- which is not the bytes'
    order: `x'01'` publishes as `'AQ=='`, which sorts before `'b'`.
    """
    world = build_world(tmp_path, MIXED_KEY_SCHEMA)
    with world.instance(None) as instance:
        instance.call(
            "execute",
            sql="INSERT INTO mixed VALUES (x'01', 'blob'), ('b', 'text'), (2, 'number')",
        )

        rows = [(record.key["k"], (record.after or {})["v"]) for record in instance.change_log()]

        assert rows == [(2, "number"), ("AQ==", "blob"), ("b", "text")]


def test_a_blob_key_sorts_by_its_base64_and_not_by_its_bytes(tmp_path: Path) -> None:
    """Base64 is not order-preserving, so the two orders genuinely invert here."""
    world = build_world(tmp_path, BLOB_KEY_SCHEMA)
    with world.instance(None) as instance:
        # x'00' publishes as 'AA==' and x'fb' as '+w==': by bytes the zero comes
        # first, by text the '+' (ASCII 43) does.
        instance.call("execute", sql="INSERT INTO keyed VALUES (x'00', 'zero'), (x'fb', 'high')")

        assert [record.key["k"] for record in instance.change_log()] == ["+w==", "AA=="]
