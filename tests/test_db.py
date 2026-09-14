"""The connection wrapper, the two doors onto an instance, and schema reflection."""

import gc
import time
import weakref
from pathlib import Path
from typing import Any

import apsw
import pytest

from seahaven.clock import Clock
from seahaven.db import Db, build_blank, open_inspection, open_instance, shadow_tables, world_tables
from seahaven.errors import DbError, WorldBug
from seahaven.ids import CONTROL_STREAM, INSPECTION_STREAM, instance_seed

NOTES = "CREATE TABLE notes (id TEXT NOT NULL PRIMARY KEY, body TEXT NOT NULL) STRICT"

SEARCHABLE = """
CREATE TABLE notes (id TEXT NOT NULL PRIMARY KEY, body TEXT NOT NULL) STRICT;
CREATE VIRTUAL TABLE notes_fts USING fts5(body, content='notes');
CREATE VIRTUAL TABLE memos_fts USING fts5(body);
"""


@pytest.fixture
def notes(db: Db) -> Db:
    db.execute(NOTES)
    db.executemany("INSERT INTO notes (id, body) VALUES (?, ?)", [("a", "first"), ("b", "second")])
    return db


def test_rows_come_back_as_dicts(notes: Db) -> None:
    assert notes.rows("SELECT id, body FROM notes ORDER BY id") == [
        {"id": "a", "body": "first"},
        {"id": "b", "body": "second"},
    ]


def test_a_single_column_row_is_still_a_dict(notes: Db) -> None:
    # One column is where a row shape would give way if APSW ever handed back a
    # bare value instead of a tuple.
    assert notes.rows("SELECT id FROM notes ORDER BY id") == [{"id": "a"}, {"id": "b"}]
    assert notes.one("SELECT count(*) AS total FROM notes") == {"total": 2}


def test_no_rows_is_an_empty_list_and_none(notes: Db) -> None:
    assert notes.rows("SELECT id FROM notes WHERE id = 'z'") == []
    assert notes.one("SELECT id FROM notes WHERE id = 'z'") is None


def test_duplicate_column_names_keep_the_last(notes: Db) -> None:
    assert notes.one("SELECT 1 AS x, 2 AS x") == {"x": 2}


def test_one_leaves_no_statement_in_flight(notes: Db) -> None:
    assert notes.one("SELECT id FROM notes ORDER BY id") == {"id": "a"}

    # A statement left half-stepped would fail this commit.
    with notes.transaction():
        notes.execute("INSERT INTO notes (id, body) VALUES ('c', 'third')")

    assert len(notes.rows("SELECT id FROM notes")) == 3


def test_execute_reports_what_the_statement_did(db: Db) -> None:
    db.execute("CREATE TABLE events (body TEXT NOT NULL)")

    inserted = db.execute("INSERT INTO events (body) VALUES ('one')")
    assert inserted == (1, 1)

    updated = db.execute("UPDATE events SET body = 'two'")
    assert updated.rowcount == 1


def test_rowid_zero_is_a_rowid(db: Db) -> None:
    db.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, body TEXT NOT NULL) STRICT")

    # SQLite's own value, passed through: 0 is a legal rowid, not "no insert".
    executed = db.execute("INSERT INTO events VALUES (0, 'zero')")

    assert executed == (1, 0)
    # An `int`, so world code can use it without proving it is not `None`.
    assert executed.last_rowid + 1 == 1


def test_execute_discards_returned_rows(notes: Db) -> None:
    assert notes.execute("DELETE FROM notes RETURNING id").rowcount == 2


def test_executemany_counts_every_row_it_changed(db: Db) -> None:
    db.execute(NOTES)

    changed = db.executemany(
        "INSERT INTO notes (id, body) VALUES (?, ?)",
        [("a", "one"), ("b", "two"), ("c", "three")],
    )

    assert changed == 3


def test_params_bind_positionally(notes: Db) -> None:
    assert notes.rows("SELECT id FROM notes WHERE body = ? OR id = ?", "first", "b") == [
        {"id": "a"},
        {"id": "b"},
    ]


def test_sqlite_errors_arrive_as_db_errors(notes: Db) -> None:
    with pytest.raises(DbError) as raised:
        notes.rows("SELECT * FROM missing")

    assert "no such table" in raised.value.sqlite_message
    assert raised.value.message == "database error"
    assert isinstance(raised.value.__cause__, apsw.Error)


def test_a_db_error_carries_the_extended_result_code(notes: Db) -> None:
    with pytest.raises(DbError) as raised:
        notes.execute("INSERT INTO notes (id, body) VALUES ('a', 'again')")

    assert raised.value.sqlite_code == apsw.SQLITE_CONSTRAINT_PRIMARYKEY


def test_a_nested_transaction_is_a_savepoint(notes: Db) -> None:
    with notes.transaction():
        notes.execute("INSERT INTO notes (id, body) VALUES ('c', 'third')")
        with pytest.raises(RuntimeError), notes.transaction():
            notes.execute("INSERT INTO notes (id, body) VALUES ('d', 'fourth')")
            raise RuntimeError("the tool gave up")

    assert [row["id"] for row in notes.rows("SELECT id FROM notes ORDER BY id")] == ["a", "b", "c"]


def test_a_failed_transaction_rolls_everything_back(notes: Db) -> None:
    with pytest.raises(RuntimeError), notes.transaction():
        notes.execute("DELETE FROM notes")
        raise RuntimeError("the tool gave up")

    assert len(notes.rows("SELECT id FROM notes")) == 2


def test_a_commit_that_fails_is_a_db_error(db: Db) -> None:
    db.execute("CREATE TABLE parents (id TEXT NOT NULL PRIMARY KEY) STRICT")
    db.execute(
        "CREATE TABLE children ("
        "  id TEXT NOT NULL PRIMARY KEY,"
        "  parent TEXT NOT NULL REFERENCES parents(id) DEFERRABLE INITIALLY DEFERRED"
        ") STRICT"
    )

    # The violation is only noticed at COMMIT, which the wrapper owns.
    with pytest.raises(DbError) as raised, db.transaction():
        db.execute("INSERT INTO children (id, parent) VALUES ('c', 'nobody')")

    assert raised.value.sqlite_code == apsw.SQLITE_CONSTRAINT_FOREIGNKEY
    assert db.rows("SELECT id FROM children") == []


def test_in_transaction_tracks_the_block(notes: Db) -> None:
    assert not notes.in_transaction
    with notes.transaction():
        assert notes.in_transaction
    assert not notes.in_transaction


def test_the_raw_connection_keeps_its_own_errors_inside_a_transaction(notes: Db) -> None:
    with notes.transaction():
        # Not a DbError: the wrapper only wraps what it ran itself.
        with pytest.raises(apsw.Error):
            notes.conn.execute("SELECT * FROM missing")
        notes.execute("INSERT INTO notes (id, body) VALUES ('c', 'third')")

    assert len(notes.rows("SELECT id FROM notes")) == 3


def test_the_raw_connection_is_the_live_one(notes: Db) -> None:
    notes.conn.execute("INSERT INTO notes (id, body) VALUES ('c', 'third')")

    assert notes.one("SELECT body FROM notes WHERE id = 'c'") == {"body": "third"}


def test_an_instance_connection_is_hardened(db: Db) -> None:
    assert db.conn.pragma("journal_mode") == "wal"
    assert db.conn.pragma("synchronous") == 1
    assert db.conn.pragma("foreign_keys") == 1
    assert db.conn.config(apsw.SQLITE_DBCONFIG_DEFENSIVE, -1) == 1
    assert db.conn.config(apsw.SQLITE_DBCONFIG_TRUSTED_SCHEMA, -1) == 0


def test_world_code_keeps_sqlites_own_limits(db: Db) -> None:
    plain = apsw.Connection(":memory:")
    limits = (
        apsw.SQLITE_LIMIT_LENGTH,
        apsw.SQLITE_LIMIT_SQL_LENGTH,
        apsw.SQLITE_LIMIT_EXPR_DEPTH,
        apsw.SQLITE_LIMIT_COMPOUND_SELECT,
        apsw.SQLITE_LIMIT_LIKE_PATTERN_LENGTH,
        apsw.SQLITE_LIMIT_VARIABLE_NUMBER,
    )

    try:
        for limit in limits:
            assert db.conn.limit(limit) == plain.limit(limit)
    finally:
        plain.close()


def test_closing_the_database_closes_the_clock_helper(
    db_path: Path, clock: Clock, seed: bytes
) -> None:
    """The clock overrides evaluate on a private connection, and closing the `Db` closes it.

    Not hygiene that the collector would do anyway: every override in
    `_FUNCTIONS` that `register_clock_functions` puts on the world connection is
    a closure over the helper -- the three in `_CONSTANTS` hold a string read at
    registration and are not -- so the helper outlives the `Db` for as long as
    the world connection does. Left unclosed it is a live SQLite connection per
    instance, and an eval run makes thousands of instances.
    """
    database = open_instance(db_path, clock, seed)
    helper = database._helper
    assert helper is not None

    database.close()

    with pytest.raises(apsw.ConnectionClosedError):
        helper.execute("SELECT 1")


def test_a_dropped_database_does_not_leak_its_connection(
    db_path: Path, clock: Clock, seed: bytes
) -> None:
    """No override may capture the connection it is registered on.

    The connection holds its function table and a captured connection closes the
    loop, and it is not a loop the collector can break: APSW's connection does
    not walk that table for it. A `Db` dropped without `close()` would then keep
    a live SQLite connection for the life of the process, and an eval run makes
    thousands of instances. `randomblob` reads the connection's length limit and
    holds a weak reference to do it.
    """
    build_blank(db_path, NOTES).close()
    database = open_instance(db_path, clock, seed)
    # Drawn first: the reference has to be live where the override uses it, so a
    # weak reference that is never resolvable would fail here rather than pass
    # this test by leaking nothing.
    assert database.one("SELECT length(randomblob(8)) AS drawn") == {"drawn": 8}
    connection = weakref.ref(database.conn)

    del database
    gc.collect()

    assert connection() is None


def test_every_door_draws_from_a_stream_of_its_own(
    notes: Db, db_path: Path, clock: Clock, seed: bytes
) -> None:
    """A read-only door gets `random()` and `randomblob()`, as it gets the clock.

    A door that refused them would be a second SQL dialect on the one instance.
    Each carries a stream of its own, so what an eval reads through `inspect()`
    is never the bytes a world just wrote through `ctx.db`, and the control
    tools' door does not echo either of them -- while a door reopened on the
    same seed and label replays value for value.
    """
    dice = "SELECT random() AS roll, hex(randomblob(4)) AS token"
    doors = {
        "inspection": open_inspection(db_path, clock, seed, INSPECTION_STREAM),
        "control": open_inspection(db_path, clock, seed, CONTROL_STREAM),
        "reopened": open_inspection(db_path, clock, seed, INSPECTION_STREAM),
        "elsewhere": open_inspection(db_path, clock, instance_seed("gone"), INSPECTION_STREAM),
    }
    try:
        drawn = {name: door.one(dice) for name, door in doors.items()}
        drawn["instance"] = notes.one(dice)

        assert drawn["reopened"] == drawn["inspection"]
        # The writable door, the two read-only ones and a second seed: four
        # different draws, from one file and one instant.
        distinct = ("instance", "inspection", "control", "elsewhere")
        assert len({str(drawn[name]) for name in distinct}) == len(distinct)
    finally:
        for door in doors.values():
            door.close()


def test_an_inspection_connection_is_hardened_too(
    notes: Db, db_path: Path, clock: Clock, seed: bytes
) -> None:
    """Read-only is not hardened: `_harden` runs on this connection as well.

    Asked through `config`, which is a C-API call rather than a pragma, because
    this connection's authorizer refuses every pragma that is not on its
    allowlist -- `PRAGMA foreign_keys` included. Each answer is checked against a
    connection SQLite opened for itself, so a default that ever comes to agree
    with the hardened value fails here instead of quietly making the test vacuous.
    """
    inspection = open_inspection(db_path, clock, seed, INSPECTION_STREAM)
    plain = apsw.Connection(":memory:")
    try:
        for setting, hardened in (
            (apsw.SQLITE_DBCONFIG_ENABLE_FKEY, 1),
            (apsw.SQLITE_DBCONFIG_DEFENSIVE, 1),
            (apsw.SQLITE_DBCONFIG_TRUSTED_SCHEMA, 0),
        ):
            assert inspection.conn.config(setting, -1) == hardened
            assert plain.config(setting, -1) != hardened, "SQLite's own default; nothing is proved"
    finally:
        plain.close()
        inspection.close()


def test_an_inspection_connection_refuses_a_pragma_that_is_not_on_the_list(
    notes: Db, db_path: Path, clock: Clock, seed: bytes
) -> None:
    """A pragma is judged by name, and nothing underneath the authorizer refuses these.

    The read-only open refuses a pragma that writes the *database*; it does not
    refuse one that only changes the *connection*, and `PRAGMA cache_size` takes
    effect on a read-only connection like any other. Nor does anything refuse a
    pragma that merely reports and is not on the allowlist. Both are denied by the
    `SQLITE_PRAGMA` clause and by nothing else, which is what separates this from
    the writes `test_inspection_reads_but_does_not_write` covers.
    """
    inspection = open_inspection(db_path, clock, seed, INSPECTION_STREAM)
    try:
        for refused in ("PRAGMA cache_size = 100", "PRAGMA secure_delete = ON", "PRAGMA page_size"):
            with pytest.raises(DbError) as raised:
                inspection.execute(refused)
            assert "not authorized" in raised.value.sqlite_message
        # The allowlist is what lets anything through at all.
        assert inspection.rows("PRAGMA table_info('notes')")
    finally:
        inspection.close()


def test_extensions_cannot_be_loaded(db: Db) -> None:
    with pytest.raises(DbError):
        db.rows("SELECT load_extension('anything')")


def test_hardening_turns_extension_loading_off_where_it_was_on(
    db_path: Path, clock: Clock, seed: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every door this module opens refuses an extension, on purpose and not by default.

    SQLite's own default is off as well, so `enable_load_extension(False)` has
    nothing to show on a connection that arrives in the default state: the test
    above passes whether or not `_harden` calls it. Here the connections are
    handed to `_harden` with extension loading already on, which is the one state
    that tells the call from the default -- without it SQLite gets as far as
    looking for the file, and says so in place of refusing the call.
    """
    build_blank(db_path, NOTES).close()
    real_connection = apsw.Connection

    def already_loading(*args: Any, **kwargs: Any) -> apsw.Connection:
        conn = real_connection(*args, **kwargs)
        conn.enable_load_extension(True)
        return conn

    monkeypatch.setattr(apsw, "Connection", already_loading)

    for database in (
        open_instance(db_path, clock, seed),
        open_inspection(db_path, clock, seed, INSPECTION_STREAM),
    ):
        try:
            with pytest.raises(DbError) as raised:
                database.rows("SELECT load_extension('anything')")
            assert "not authorized" in raised.value.sqlite_message
        finally:
            database.close()


def test_a_second_writer_fails_instead_of_waiting(notes: Db, db_path: Path) -> None:
    other = apsw.Connection(str(db_path))
    try:
        other.execute("BEGIN IMMEDIATE")

        started = time.monotonic()
        with pytest.raises(DbError):
            notes.execute("INSERT INTO notes (id, body) VALUES ('c', 'third')")

        assert time.monotonic() - started < 0.5
    finally:
        other.close()


def test_inspection_reads_but_does_not_write(
    notes: Db, db_path: Path, clock: Clock, seed: bytes
) -> None:
    inspection = open_inspection(db_path, clock, seed, INSPECTION_STREAM)
    try:
        assert inspection.one("SELECT body FROM notes WHERE id = 'a'") == {"body": "first"}
        assert inspection.one("SELECT current_timestamp AS now") == {"now": clock.iso()}
        assert inspection.rows("PRAGMA table_info('notes')")
        # Pragma names fold the way every other SQL name does.
        assert inspection.rows("PRAGMA TABLE_INFO('notes')")
        assert inspection.rows("SELECT name FROM pragma_table_xinfo('notes')")

        for forbidden in (
            "INSERT INTO notes (id, body) VALUES ('c', 'third')",
            "PRAGMA journal_mode = DELETE",
            "PRAGMA user_version = 3",
            "PRAGMA optimize",
            "ATTACH DATABASE ':memory:' AS other",
            "CREATE TABLE sneaky (x)",
            "DROP TABLE notes",
        ):
            with pytest.raises(DbError):
                inspection.execute(forbidden)
    finally:
        inspection.close()

    assert len(notes.rows("SELECT id FROM notes")) == 2


def test_build_blank_writes_the_ddl_and_nothing_else(tmp_path: Path) -> None:
    path = tmp_path / "blank.sqlite"

    conn = build_blank(path, NOTES)

    assert world_tables(conn) == ["notes"]
    assert conn.pragma("foreign_keys") == 1
    conn.close()
    assert path.exists()


def test_build_blank_runs_every_statement(tmp_path: Path) -> None:
    # APSW runs a multi-statement string only as far as the first statement that
    # returns a row, unless the cursor is iterated. A world's schema file may
    # hold such a statement, and everything below it has to run.
    ddl = f"SELECT 1;\n{NOTES};\nCREATE TABLE later (x TEXT NOT NULL PRIMARY KEY) STRICT"

    conn = build_blank(tmp_path / "blank.sqlite", ddl)

    assert world_tables(conn) == ["later", "notes"]
    conn.close()


def test_build_blank_in_memory_needs_no_file(tmp_path: Path) -> None:
    conn = build_blank(":memory:", NOTES)

    assert world_tables(conn) == ["notes"]
    assert list(tmp_path.iterdir()) == []
    conn.close()


def test_build_blank_refuses_to_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "blank.sqlite"
    path.write_bytes(b"")

    with pytest.raises(WorldBug, match="existing file"):
        build_blank(path, NOTES)


def test_build_blank_reports_ddl_that_does_not_execute(tmp_path: Path) -> None:
    path = tmp_path / "blank.sqlite"

    with pytest.raises(apsw.SQLError, match="syntax error"):
        build_blank(path, "CREATE TABLE (")

    # Nothing left behind: a retry at the same path reports the DDL's error
    # again rather than "refusing to build over an existing file".
    assert not path.exists()
    build_blank(path, NOTES).close()


def test_fts5_shadow_tables_are_found_by_prefix(tmp_path: Path) -> None:
    conn = build_blank(tmp_path / "blank.sqlite", SEARCHABLE)

    shadow = shadow_tables(conn)

    assert {"notes_fts_data", "notes_fts_idx", "notes_fts_config"} <= shadow
    # `content=` changes which shadow tables exist, which is why they are derived
    # rather than listed.
    assert "memos_fts_content" in shadow
    assert "notes_fts_content" not in shadow
    assert {"notes", "notes_fts", "memos_fts"} & shadow == set()
    conn.close()


def test_world_tables_keep_the_virtual_table_and_drop_the_rest(
    tmp_path: Path, clock: Clock, seed: bytes
) -> None:
    path = tmp_path / "state.sqlite"
    build_blank(path, SEARCHABLE).close()
    db = open_instance(path, clock, seed)
    try:
        assert world_tables(db.conn) == ["memos_fts", "notes", "notes_fts"]
    finally:
        db.close()
