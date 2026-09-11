"""The attack suite: SQL an agent wrote, against the containment it runs under.

Every test here talks to the sandbox in raw SQLite text, because the sandbox
makes no claim about the text -- only about what SQLite is allowed to do with it.
"""

import time
from collections.abc import Sequence
from contextlib import suppress

import apsw
import pytest

from seahaven.db import Db, SqlValue
from seahaven.errors import DbError, WorldBug
from seahaven.sandbox import (
    MAX_VALUE_BYTES,
    Authorizer,
    SqlResult,
    refusal_kind,
    run_statement,
)

SCHEMA = """
CREATE TABLE notes (id TEXT NOT NULL PRIMARY KEY, body TEXT NOT NULL) STRICT;
CREATE TABLE secrets (id TEXT NOT NULL PRIMARY KEY, token TEXT NOT NULL) STRICT;
CREATE VIRTUAL TABLE notes_fts USING fts5(body);
"""

# Each attack, and the refusal the sandbox records for it. Several read as a
# write to `sqlite_master`, because that is genuinely the first thing SQLite asks
# to do when it runs DDL, and the authorizer answers the question it was asked
# rather than guessing at the statement behind it.
DENIED = [
    (
        "INSERT INTO notes (id, body) VALUES ('z', 'zed')",
        "write of table 'notes' in a read-only query",
    ),
    ("UPDATE notes SET body = 'x'", "write of table 'notes' in a read-only query"),
    ("DELETE FROM notes", "write of table 'notes' in a read-only query"),
    ("CREATE TABLE sneaky (x)", "action INSERT 'sqlite_master'"),
    ("DROP TABLE notes", "action DELETE 'sqlite_master'"),
    ("ALTER TABLE notes RENAME TO diary", "action ALTER TABLE 'notes'"),
    ("CREATE INDEX i ON notes (body)", "action INSERT 'sqlite_master'"),
    ("CREATE VIEW v AS SELECT 1", "action INSERT 'sqlite_master'"),
    ("CREATE VIRTUAL TABLE v2 USING fts5(body)", "action INSERT 'sqlite_master'"),
    ("CREATE TEMP TABLE t (x)", "action INSERT 'sqlite_temp_master'"),
    (
        "CREATE TRIGGER stamp AFTER INSERT ON notes BEGIN SELECT 1; END",
        "action CREATE TRIGGER 'stamp'",
    ),
    ("ATTACH DATABASE ':memory:' AS other", "action ATTACH ':memory:'"),
    ("DETACH DATABASE other", "action DETACH 'other'"),
    ("PRAGMA journal_mode = DELETE", "action PRAGMA 'journal_mode'"),
    ("BEGIN", "action transaction control 'BEGIN'"),
    ("SAVEPOINT s", "action SAVEPOINT 's'"),
    ("REINDEX notes", "action REINDEX 'sqlite_autoindex_notes_1'"),
    ("ANALYZE", "action INSERT 'sqlite_master'"),
    # VACUUM asks for nothing until it runs, so the authorizer never sees it:
    # SQLite's own verdict on the prepared statement is what stops it.
    ("VACUUM", "write in a read-only query"),
    ("SELECT * FROM secrets", "read of table 'secrets'"),
    # The SQLITE_IGNORE regression: ignoring would leave the true count in place,
    # which is the data being refused.
    ("SELECT count(*) FROM secrets", "read of table 'secrets'"),
    ("SELECT random()", "function 'random'"),
    ("SELECT load_extension('libsneaky.so')", "function 'load_extension'"),
    ("SELECT sqlite_version()", "function 'sqlite_version'"),
    ("SELECT last_insert_rowid()", "function 'last_insert_rowid'"),
    # FTS5's shadow tables are the index, not the world.
    ("SELECT * FROM notes_fts_data", "read of table 'notes_fts_data'"),
]


@pytest.fixture
def world(db: Db) -> Db:
    db.execute(SCHEMA)
    db.executemany(
        "INSERT INTO notes (id, body) VALUES (?, ?)",
        [(f"n{index}", f"note {index}") for index in range(5)],
    )
    db.execute("INSERT INTO secrets (id, token) VALUES ('s1', 'hunter2')")
    return db


def run(
    db: Db,
    sql: str,
    params: Sequence[SqlValue] = (),
    *,
    tables: Sequence[str] = ("notes",),
    read_only: bool = True,
    functions: frozenset[str] | None = None,
    max_rows: int | None = None,
    max_bytes: int | None = None,
) -> SqlResult:
    authorizer = (
        Authorizer(tables, read_only=read_only)
        if functions is None
        else Authorizer(tables, read_only=read_only, functions=functions)
    )
    return run_statement(
        db, sql, params, authorizer=authorizer, max_rows=max_rows, max_bytes=max_bytes
    )


def refusals(
    db: Db,
    sql: str,
    *,
    tables: Sequence[str] = ("notes",),
    read_only: bool = True,
    functions: frozenset[str] | None = None,
) -> tuple[str, ...]:
    """What the sandbox turned `sql` down for. Fails if it ran."""
    with pytest.raises(DbError) as raised:
        run(db, sql, tables=tables, read_only=read_only, functions=functions)
    assert raised.value.refusals, f"expected a refusal, got {raised.value.sqlite_message}"
    return raised.value.refusals


def test_an_allowed_query_returns_rows_and_columns(world: Db) -> None:
    result = run(world, "SELECT id, body FROM notes ORDER BY id LIMIT 2")

    assert result.columns == ["id", "body"]
    assert result.rows == [["n0", "note 0"], ["n1", "note 1"]]
    assert result.row_count == 2
    assert not result.truncated


def test_columns_are_named_even_with_no_rows(world: Db) -> None:
    result = run(world, "SELECT id, body FROM notes WHERE id = 'nope'")

    assert result.columns == ["id", "body"]
    assert result.rows == []


def test_params_bind_positionally(world: Db) -> None:
    result = run(world, "SELECT id FROM notes WHERE body = ?", ("note 3",))

    assert result.rows == [["n3"]]


@pytest.mark.parametrize(("sql", "refusal"), DENIED)
def test_the_sandbox_refuses(world: Db, sql: str, refusal: str) -> None:
    # The first refusal is the one the agent reads, and some statements ask
    # several questions before SQLite gives up on them.
    assert refusals(world, sql, tables=("notes", "notes_fts"))[0] == refusal


def test_a_refusal_is_what_the_agent_reads(world: Db) -> None:
    with pytest.raises(DbError) as raised:
        run(world, "SELECT * FROM secrets")

    assert raised.value.message == "not allowed: read of table 'secrets'"
    assert raised.value.code == "db_error"
    assert refusal_kind(raised.value.refusals[0]) == "read"


def test_every_refusal_is_classifiable(world: Db) -> None:
    for sql, _ in DENIED:
        for refusal in refusals(world, sql, tables=("notes", "notes_fts")):
            # Raises unless the refusal begins with one of the stable names.
            refusal_kind(refusal)


def test_a_refused_write_says_it_was_a_write(world: Db) -> None:
    # The classification an extension reads: this tool cannot write, which is a
    # different answer from "that statement is not a tool".
    write = refusals(world, "DELETE FROM notes")[0]
    ddl = refusals(world, "DROP TABLE notes")[0]

    assert refusal_kind(write) == "write"
    assert refusal_kind(ddl) == "action"


def test_refusal_kind_refuses_text_that_is_not_a_refusal() -> None:
    with pytest.raises(WorldBug, match="not a refusal"):
        refusal_kind("something else entirely")


def test_table_names_fold_the_way_sqlite_folds_them(world: Db) -> None:
    # A column read arrives canonicalised and a bare row read arrives as the
    # agent spelled it; both have to be allowed.
    assert run(world, "SELECT id FROM NOTES ORDER BY id LIMIT 1").rows == [["n0"]]
    assert run(world, "SELECT count(*) FROM NOTES").rows == [[5]]


def test_the_fold_is_ascii_only() -> None:
    authorizer = Authorizer(["İnbox"])

    assert authorizer(apsw.SQLITE_READ, "İNBOX", "id", "main", None) == apsw.SQLITE_OK
    # `"İnbox".lower()` is `"i̇nbox"`; SQLite treats them as different tables and
    # so must the allowlist.
    assert authorizer(apsw.SQLITE_READ, "i̇nbox", "id", "main", None) == apsw.SQLITE_DENY


def test_a_second_statement_never_runs(world: Db) -> None:
    assert refusals(world, "SELECT 1; SELECT 2") == ("statement after the first in one call",)

    assert refusals(world, "SELECT 1; DROP TABLE notes") == ("action DELETE 'sqlite_master'",)
    assert world.one("SELECT count(*) AS n FROM notes") == {"n": 5}


def test_a_cap_reached_first_truncates_rather_than_refusing(world: Db) -> None:
    payload = "SELECT id FROM notes; INSERT INTO notes (id, body) VALUES ('evil', 'x')"

    # The tracer only sees a statement SQLite is about to run, so a first
    # statement that stops at a cap ends the payload there: the caller is told
    # `truncated` instead of being refused, and what followed never ran.
    result = run(world, payload, tables=("notes",), read_only=False, max_rows=1)

    assert result.truncated
    assert world.one("SELECT count(*) AS n FROM notes") == {"n": 5}
    # The same text with no cap is refused.
    assert refusals(world, payload, read_only=False) == ("statement after the first in one call",)


def test_a_write_the_authorizer_let_through_is_still_refused(world: Db) -> None:
    class Permissive(Authorizer):
        """What an extension must not be able to open up by accident."""

        def __call__(
            self,
            action: int,
            third: str | None,
            fourth: str | None,
            database: str | None,
            trigger: str | None,
            /,
        ) -> int:
            return apsw.SQLITE_OK

    with pytest.raises(DbError) as raised:
        run_statement(
            world,
            "INSERT INTO notes (id, body) VALUES ('z', 'zed')",
            authorizer=Permissive(["notes"]),
        )

    assert raised.value.refusals == ("write in a read-only query",)
    assert world.one("SELECT count(*) AS n FROM notes") == {"n": 5}


def test_writes_go_only_to_the_tables_that_were_listed(world: Db) -> None:
    result = run(
        world,
        "INSERT INTO notes (id, body) VALUES ('z', 'zed')",
        tables=("notes",),
        read_only=False,
    )

    assert result.rows == []
    assert world.one("SELECT body FROM notes WHERE id = 'z'") == {"body": "zed"}
    assert refusals(
        world, "UPDATE secrets SET token = 'x'", tables=("notes",), read_only=False
    ) == ("write of table 'secrets'",)
    assert refusals(world, "DROP TABLE notes", tables=("notes",), read_only=False) == (
        "action DELETE 'sqlite_master'",
    )


def test_the_function_allowlist_is_the_whole_vocabulary(world: Db) -> None:
    assert run(world, "SELECT upper(body) FROM notes ORDER BY id LIMIT 1").rows == [["NOTE 0"]]
    assert run(world, "SELECT json_extract('{\"a\": 1}', '$.a')").rows == [[1]]
    assert run(world, "SELECT current_timestamp").rows[0][0].endswith("Z")


def test_a_caller_can_replace_the_function_allowlist(world: Db) -> None:
    counting_only = frozenset({"count"})

    assert run(world, "SELECT count(*) FROM notes", functions=counting_only).rows == [[5]]
    assert refusals(world, "SELECT upper(body) FROM notes", functions=counting_only) == (
        "function 'upper'",
    )


def test_the_function_allowlist_folds_like_sqlite(world: Db) -> None:
    shouting = frozenset({"UPPER"})

    assert run(world, "SELECT upper(body) FROM notes LIMIT 1", functions=shouting).row_count == 1
    assert refusals(world, "SELECT lower(body) FROM notes", functions=shouting) == (
        "function 'lower'",
    )


def test_shadow_tables_can_be_opened_deliberately(world: Db) -> None:
    result = run(world, "SELECT count(*) FROM notes_fts_data", tables=("notes_fts_data",))

    assert result.row_count == 1


def test_one_value_cannot_grow_without_bound(world: Db) -> None:
    started = time.monotonic()

    assert refusals(world, "SELECT printf('%1000000000d', 1)") == (
        f"value larger than {MAX_VALUE_BYTES} bytes",
    )

    # Refused where SQLite's own 1 GB limit would have spent seconds and a
    # gigabyte of memory reaching the same answer.
    assert time.monotonic() - started < 1.0


def test_a_quadratic_builtin_is_a_documented_residual(world: Db) -> None:
    # Nothing acts inside one opcode, so the value cap bounds the memory a single
    # function call takes but only partly bounds its time: at the cap, this shape
    # runs for about eight seconds. A tenth of the size is used here to pin the
    # residual rather than to pay for it.
    haystack = _letters(MAX_VALUE_BYTES // 5)
    needle = _letters(MAX_VALUE_BYTES // 10)

    result = run(world, f"SELECT instr({haystack}, {needle} || 'b')")

    # It ran to the end and was not refused: nothing here bounds the time.
    assert result.rows == [[0]]


def _letters(count: int) -> str:
    """SQL for a string of `count` letters, built inside SQLite."""
    return f"replace(printf('%*c', {count}, 'a'), ' ', 'a')"


def test_max_rows_truncates(world: Db) -> None:
    result = run(world, "SELECT id FROM notes ORDER BY id", max_rows=2)

    assert result.rows == [["n0"], ["n1"]]
    assert result.truncated


def test_max_bytes_truncates(world: Db) -> None:
    result = run(world, "SELECT id, body FROM notes ORDER BY id", max_bytes=40)

    assert 0 < result.row_count < 5
    assert result.truncated


def test_max_bytes_counts_bytes_not_characters(world: Db) -> None:
    world.execute("INSERT INTO notes (id, body) VALUES ('jp', ?)", "日本語のテキストです")

    # 14 characters of JSON, 34 bytes of UTF-8: a cap between the two has to
    # count the bytes the caller asked about.
    result = run(world, "SELECT body FROM notes WHERE id = 'jp'", max_bytes=20)

    assert result.rows == []
    assert result.truncated
    assert run(world, "SELECT body FROM notes WHERE id = 'jp'", max_bytes=40).row_count == 1


def test_a_negative_cap_is_a_mistake(world: Db) -> None:
    with pytest.raises(WorldBug, match="must not be negative"):
        run(world, "SELECT id FROM notes", max_rows=-1)

    with pytest.raises(WorldBug, match="must not be negative"):
        run(world, "SELECT id FROM notes", max_bytes=-1)


def test_unset_caps_mean_the_whole_result(world: Db) -> None:
    result = run(world, "SELECT id FROM notes ORDER BY id")

    assert result.row_count == 5
    assert not result.truncated


def test_bytes_come_back_as_base64(world: Db) -> None:
    result = run(world, "SELECT x'6869' AS blob")

    assert result.rows == [["aGk="]]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM notes ORDER BY id",
        "SELECT * FROM secrets",
        "SELECT printf('%1000000000d', 1)",
    ],
)
def test_the_connection_is_left_as_it_was_found(world: Db, sql: str) -> None:
    before = world.conn.limit(apsw.SQLITE_LIMIT_LENGTH)

    # Whether the statement succeeded, truncated or was refused, the shared
    # connection has to come back exactly as it was.
    with suppress(DbError):
        run(world, sql, max_rows=1)

    assert world.conn.authorizer is None
    assert world.conn.limit(apsw.SQLITE_LIMIT_LENGTH) == before
    # And world code, which is not sandboxed, still works.
    assert world.one("SELECT count(*) AS n FROM notes") == {"n": 5}


def test_one_authorizer_forgets_between_statements(world: Db) -> None:
    authorizer = Authorizer(["notes"])

    with pytest.raises(DbError):
        run_statement(world, "SELECT * FROM secrets", authorizer=authorizer)
    assert authorizer.refusals == ("read of table 'secrets'",)

    result = run_statement(world, "SELECT count(*) FROM notes", authorizer=authorizer)

    assert result.rows == [[5]]
    assert authorizer.refusals == ()


def test_a_plain_sql_mistake_is_not_a_refusal(world: Db) -> None:
    with pytest.raises(DbError) as raised:
        run(world, "SELECT missing_column FROM notes")

    assert raised.value.refusals == ()
    assert raised.value.message == "database error"
    assert raised.value.sqlite_message == "no such column: missing_column"
