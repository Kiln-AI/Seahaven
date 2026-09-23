"""The clock in each of its modes, and SQL reading it through SQLite's own date functions."""

import io
import itertools
import os
import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import apsw
import apsw.ext
import pytest

from seahaven import clock as clock_module
from seahaven.clock import (
    CANONICAL_FORMAT,
    CLOCK_MODES,
    TRACE_ID,
    Clock,
    ClockMode,
    check_clock_mode,
    register_clock_functions,
)
from seahaven.db import Db, open_instance
from seahaven.errors import WorldBug
from seahaven.sandbox import Authorizer, run_statement
from tests.conftest import INSTANT, INSTANT_ISO

if TYPE_CHECKING:  # a name that lives in apsw's stubs, not in the extension module
    from apsw import SQLiteValue

CANONICAL = f"strftime('{CANONICAL_FORMAT}', ?)"


@contextmanager
def _timezone(name: str) -> Iterator[None]:
    """Run the block with the process timezone set, as SQLite reads it.

    `name` is a POSIX `TZ` string, so nothing here needs the timezone database.
    """
    previous = os.environ.get("TZ")
    os.environ["TZ"] = name
    time.tzset()
    try:
        yield
    finally:
        if previous is None:
            del os.environ["TZ"]
        else:
            os.environ["TZ"] = previous
        time.tzset()


# Each overridden expression, and the expression SQLite itself evaluates on the
# instant to produce what the override must return.
EQUIVALENTS = [
    ("date('now')", "date(?)"),
    ("date()", "date(?)"),
    ("date('NOW')", "date(?)"),
    ("date('now', '+1 day')", "date(?, '+1 day')"),
    ("date('now', 'start of month')", "date(?, 'start of month')"),
    ("date('now', 'weekday 0')", "date(?, 'weekday 0')"),
    ("date('2021-05-06')", "date('2021-05-06')"),
    ("time('now')", "time(?)"),
    ("time()", "time(?)"),
    ("datetime('now')", CANONICAL),
    ("datetime()", CANONICAL),
    ("datetime('now', 'start of month')", f"strftime('{CANONICAL_FORMAT}', ?, 'start of month')"),
    ("datetime('2021-05-06 07:08:09')", "strftime('%Y-%m-%dT%H:%M:%fZ', '2021-05-06 07:08:09')"),
    ("julianday('now')", "julianday(?)"),
    ("julianday()", "julianday(?)"),
    ("unixepoch('now')", "unixepoch(?)"),
    ("timediff('now', '2020-01-01')", "timediff(?, '2020-01-01')"),
    ("timediff('2020-01-01', 'now')", "timediff('2020-01-01', ?)"),
    ("strftime('%Y-%m-%d %H:%M:%f', 'now')", "strftime('%Y-%m-%d %H:%M:%f', ?)"),
    ("strftime('%s')", "strftime('%s', ?)"),
    ("strftime('%Y', 'now', '+2 years')", "strftime('%Y', ?, '+2 years')"),
    ("current_timestamp", CANONICAL),
    ("current_date", "date(?)"),
    ("current_time", "time(?)"),
    # `'now'` as a *format* is the literal text, not a time value.
    ("strftime('now', '2020-01-01')", "strftime('now', '2020-01-01')"),
    # No more permissive than SQLite: padded `now` is not a time value either.
    ("date(' now ')", "date(' now ')"),
]


@pytest.fixture
def overridden(clock: Clock) -> Iterator[apsw.Connection]:
    """A connection reading the clock, closed with the helper it owns."""
    conn = apsw.Connection(":memory:")
    helper = register_clock_functions(conn, clock)
    try:
        yield conn
    finally:
        conn.close()
        helper.close()


@pytest.fixture
def plain() -> Iterator[apsw.Connection]:
    """SQLite with no overrides: where the reference values come from."""
    conn = apsw.Connection(":memory:")
    try:
        yield conn
    finally:
        conn.close()


@pytest.mark.parametrize(("overridden_sql", "reference_sql"), EQUIVALENTS)
def test_override_matches_sqlite_on_the_instant(
    overridden: apsw.Connection,
    plain: apsw.Connection,
    clock: Clock,
    overridden_sql: str,
    reference_sql: str,
) -> None:
    expected = plain.execute(
        f"SELECT {reference_sql}", (clock.iso(),) * reference_sql.count("?")
    ).get

    assert overridden.execute(f"SELECT {overridden_sql}").get == expected


def test_the_instant_is_rendered_canonically(overridden: apsw.Connection, clock: Clock) -> None:
    assert overridden.execute("SELECT current_timestamp").get == clock.iso()
    assert overridden.execute("SELECT datetime('now')").get == clock.iso()
    assert clock.iso() == "2024-03-05T12:00:00.123Z"


def test_a_default_clause_reads_the_clock(db: Db) -> None:
    # STRICT, and on a connection with TRUSTED_SCHEMA off: the schema may call
    # the override only because it is registered as innocuous.
    db.execute(
        "CREATE TABLE notes ("
        "  id TEXT NOT NULL PRIMARY KEY,"
        "  created_at TEXT NOT NULL DEFAULT (current_timestamp)"
        ") STRICT"
    )
    db.execute("INSERT INTO notes (id) VALUES ('a')")

    assert db.one("SELECT created_at FROM notes") == {"created_at": Clock(INSTANT).iso()}


def test_a_trigger_reads_the_clock(db: Db) -> None:
    db.execute("CREATE TABLE notes (id TEXT NOT NULL PRIMARY KEY, touched_at TEXT) STRICT")
    db.execute(
        "CREATE TRIGGER stamp AFTER INSERT ON notes BEGIN"
        "  UPDATE notes SET touched_at = datetime('now') WHERE id = NEW.id;"
        " END"
    )
    db.execute("INSERT INTO notes (id) VALUES ('a')")

    assert db.one("SELECT touched_at FROM notes") == {"touched_at": Clock(INSTANT).iso()}


def test_clock_values_compare_against_stored_timestamps(db: Db, clock: Clock) -> None:
    before = Clock(INSTANT - timedelta(seconds=1)).iso()
    after = Clock(INSTANT + timedelta(milliseconds=1)).iso()
    db.execute("CREATE TABLE notes (id TEXT NOT NULL PRIMARY KEY, created_at TEXT NOT NULL) STRICT")
    db.executemany(
        "INSERT INTO notes (id, created_at) VALUES (?, ?)",
        [("before", before), ("at", clock.iso()), ("after", after)],
    )

    later = db.rows("SELECT id FROM notes WHERE created_at > CURRENT_TIMESTAMP ORDER BY id")

    # Exactly the rows after the instant: a row written *at* it is not after it.
    assert later == [{"id": "after"}]
    assert db.rows("SELECT id FROM notes WHERE created_at <= datetime('now') ORDER BY id") == [
        {"id": "at"},
        {"id": "before"},
    ]


def test_localtime_is_not_the_instant(overridden: apsw.Connection, clock: Clock) -> None:
    """The documented wart: `'localtime'` leaves the canonical `Z` on local time."""
    if not hasattr(time, "tzset"):
        pytest.skip("the process timezone cannot be set on this platform")

    # A POSIX offset rather than a zone name: `tzset` parses it without the
    # timezone database, which a slim image may not carry.
    with _timezone("XXX8"):
        local = overridden.execute("SELECT datetime('now', 'localtime')").get

    # Still canonical text, and still stamped `Z` -- on a value that is not UTC.
    # A world's SQL has no business calling it; this pins what it does if it does.
    assert local == "2024-03-05T04:00:00.123Z"
    assert local != clock.iso()


def test_clock_requires_an_aware_instant() -> None:
    with pytest.raises(WorldBug, match="timezone-aware"):
        Clock(datetime(2024, 3, 5, 12, 0))


def test_an_instant_is_kept_at_the_precision_it_is_rendered_at() -> None:
    finer = Clock(INSTANT.replace(microsecond=123_456))

    # Two instants nothing in the framework can tell apart read the same.
    assert finer.now() == Clock(INSTANT).now()
    assert finer.iso() == Clock(INSTANT).iso()
    assert Clock.from_iso(finer.iso()).now() == finer.now()
    assert finer.now().microsecond == 123_000


def test_clock_normalises_to_utc() -> None:
    elsewhere = Clock(datetime(2024, 3, 5, 14, 0, 0, 123000, tzinfo=timezone(timedelta(hours=2))))

    assert elsewhere.now() == INSTANT
    assert elsewhere.now().tzinfo == UTC
    assert elsewhere.iso() == INSTANT_ISO


def test_clock_round_trips_through_canonical_text() -> None:
    assert Clock.from_iso(Clock(INSTANT).iso()).now() == INSTANT


def test_from_iso_refuses_text_that_is_not_a_timestamp() -> None:
    with pytest.raises(WorldBug, match="not a timestamp"):
        Clock.from_iso("the fifth of March")


def test_wall_truncates_to_milliseconds() -> None:
    wall = Clock.wall()

    assert wall.now().microsecond % 1000 == 0
    assert wall.now().tzinfo == UTC
    assert abs((wall.now() - datetime.now(UTC)).total_seconds()) < 5


# ----------------------------------------------------------------- the modes

# How far a stepping monotonic source moves on every read: enough to change every
# field a date function renders, the date and the milliseconds included.
STEP = timedelta(days=1, seconds=1, milliseconds=1)

UNKNOWN_MODE = "unknown clock mode 'tik'; the clock modes are 'fixed', 'tick', 'running' and 'wall'"


def _after(steps: int) -> str:
    """The canonical reading of a stepping `running` clock `steps` reads after it was made.

    Rendered by hand rather than through a `Clock`, because making a clock reads
    the monotonic source, and that would be a step of its own.
    """
    return (INSTANT + steps * STEP).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@pytest.fixture
def monotonic(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """The clock's monotonic source, as a value the test sets. Starts at 0."""
    now = [0]
    monkeypatch.setattr(clock_module, "_monotonic_ns", lambda: now[0])
    return now


@pytest.fixture
def wall(monkeypatch: pytest.MonkeyPatch) -> list[datetime]:
    """The clock's wall-clock source, as a value the test sets."""
    now = [datetime(2031, 12, 25, 6, 30, 0, 500_999, tzinfo=UTC)]
    monkeypatch.setattr(clock_module, "_wall_now", lambda: now[0])
    return now


@pytest.fixture
def stepping(monkeypatch: pytest.MonkeyPatch) -> None:
    """A monotonic source that moves `STEP` on every read, so no two readings agree."""
    reads = itertools.count(0, STEP // timedelta(microseconds=1) * 1000)
    monkeypatch.setattr(clock_module, "_monotonic_ns", lambda: next(reads))


def test_fixed_reads_the_start_on_every_reading(monotonic: list[int], wall: list[datetime]) -> None:
    fixed = Clock(INSTANT)
    monotonic[0] += 5_000_000_000
    wall[0] += timedelta(days=3)
    fixed._call_started()

    assert fixed.mode == "fixed"
    assert fixed.now() == INSTANT
    assert fixed.iso() == INSTANT_ISO


def test_tick_counts_calls_started(monotonic: list[int]) -> None:
    tick = Clock(INSTANT, "tick")
    monotonic[0] += 5_000_000_000

    assert tick.iso() == INSTANT_ISO
    tick._call_started()
    assert tick.now() == INSTANT + timedelta(seconds=1)
    # Reading never moves it.
    assert tick.now() == INSTANT + timedelta(seconds=1)
    tick._call_started()
    assert tick.iso() == "2024-03-05T12:00:02.123Z"


def test_running_adds_elapsed_monotonic_time_truncated_to_milliseconds(
    monotonic: list[int],
) -> None:
    running = Clock(INSTANT, "running")
    assert running.now() == INSTANT

    monotonic[0] = 1_999_999
    assert running.now() == INSTANT + timedelta(milliseconds=1)
    monotonic[0] = 1_000_000
    # Truncated, never rounded, so a later read can equal an earlier one.
    assert running.now() == INSTANT + timedelta(milliseconds=1)
    monotonic[0] = 40 * 60 * 1_000_000_000
    assert running.iso() == "2024-03-05T12:40:00.123Z"
    running._call_started()
    assert running.iso() == "2024-03-05T12:40:00.123Z"


def test_running_counts_from_when_the_clock_was_made(monotonic: list[int]) -> None:
    monotonic[0] = 7_000_000_000
    running = Clock(INSTANT, "running")
    monotonic[0] += 250_000_000

    assert running.now() == INSTANT + timedelta(milliseconds=250)


def test_running_ignores_the_wall_clock(monotonic: list[int], wall: list[datetime]) -> None:
    running = Clock(INSTANT, "running")
    monotonic[0] += 3_000_000_000
    wall[0] -= timedelta(days=365)

    assert running.now() == INSTANT + timedelta(seconds=3)


def test_wall_reads_the_wall_clock_and_ignores_the_start(
    monotonic: list[int], wall: list[datetime]
) -> None:
    on_the_wall = Clock(INSTANT, "wall")

    assert on_the_wall.iso() == "2031-12-25T06:30:00.500Z"
    assert on_the_wall.now().microsecond == 500_000
    # It follows the host's clock backwards too, which `running` never does.
    wall[0] = datetime(2020, 1, 1, tzinfo=timezone(timedelta(hours=2)))
    assert on_the_wall.iso() == "2019-12-31T22:00:00.000Z"
    monotonic[0] += 3_000_000_000
    assert on_the_wall.iso() == "2019-12-31T22:00:00.000Z"


def test_the_wall_constructor_is_a_fixed_clock_at_the_wall_clock(wall: list[datetime]) -> None:
    clock = Clock.wall()
    wall[0] += timedelta(hours=1)

    assert clock.mode == "fixed"
    assert clock.iso() == "2031-12-25T06:30:00.500Z"


def test_a_clock_built_directly_is_fixed() -> None:
    assert Clock(INSTANT).mode == "fixed"
    assert Clock.from_iso(INSTANT_ISO).mode == "fixed"
    assert Clock.from_iso(INSTANT_ISO, "tick").mode == "tick"


@pytest.mark.parametrize("mode", CLOCK_MODES)
def test_every_mode_is_accepted(mode: ClockMode) -> None:
    assert check_clock_mode(mode) == mode
    assert Clock(INSTANT, mode).mode == mode


@pytest.mark.parametrize("bad", ["tik", "Fixed", "", None, 1])
def test_an_unknown_mode_is_refused_naming_the_four(bad: Any) -> None:
    with pytest.raises(WorldBug, match="the clock modes are 'fixed', 'tick', 'running' and 'wall'"):
        check_clock_mode(bad)
    with pytest.raises(WorldBug, match=re.escape(f"unknown clock mode {bad!r}")):
        Clock(INSTANT, bad)


def test_the_refusal_names_the_value_it_was_given() -> None:
    with pytest.raises(WorldBug) as raised:
        check_clock_mode("tik")

    assert str(raised.value) == UNKNOWN_MODE


def test_repr_names_the_reading_and_the_mode() -> None:
    assert repr(Clock(INSTANT)) == "Clock(2024-03-05T12:00:00.123Z, fixed)"
    assert repr(Clock(INSTANT, "tick")) == "Clock(2024-03-05T12:00:00.123Z, tick)"


def test_clocks_compare_by_identity() -> None:
    """A moving clock cannot compare, or hash, by a reading that changes."""
    clock = Clock(INSTANT)

    assert clock == clock
    assert Clock(INSTANT) != Clock(INSTANT)


# ------------------------------------------------- one reading per SQL statement


@pytest.fixture
def running(tmp_path: Path, seed: bytes, stepping: None) -> Iterator[Db]:
    """A database on a `running` clock whose every reading is `STEP` later than the last."""
    database = open_instance(tmp_path / "running.sqlite", Clock(INSTANT, "running"), seed)
    try:
        yield database
    finally:
        database.close()


def _stamped(db: Db) -> list[dict[str, Any]]:
    return db.rows("SELECT * FROM stamps ORDER BY rowid")


def test_two_default_columns_share_one_reading(running: Db) -> None:
    running.execute(
        "CREATE TABLE stamps ("
        "  id TEXT NOT NULL PRIMARY KEY,"
        "  a TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),"
        "  b TEXT NOT NULL DEFAULT (datetime('now'))"
        ") STRICT"
    )
    running.execute("INSERT INTO stamps (id) VALUES ('x')")

    assert _stamped(running) == [{"id": "x", "a": _after(1), "b": _after(1)}]


def test_a_trigger_shares_its_statements_reading(running: Db) -> None:
    running.execute(
        "CREATE TABLE stamps ("
        "  id TEXT NOT NULL PRIMARY KEY,"
        "  created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),"
        "  touched_at TEXT"
        ") STRICT"
    )
    running.execute(
        "CREATE TRIGGER touch AFTER INSERT ON stamps BEGIN"
        "  UPDATE stamps SET touched_at = datetime('now') WHERE id = NEW.id;"
        " END"
    )
    running.execute("INSERT INTO stamps (id) VALUES ('x')")

    assert _stamped(running) == [{"id": "x", "created_at": _after(1), "touched_at": _after(1)}]


def test_every_row_of_a_multi_row_insert_shares_one_reading(running: Db) -> None:
    running.execute("CREATE TABLE stamps (id TEXT NOT NULL PRIMARY KEY, at TEXT NOT NULL) STRICT")
    running.execute(
        "INSERT INTO stamps (id, at) VALUES ('a', CURRENT_TIMESTAMP), ('b', datetime()),"
        " ('c', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
    )

    assert {row["at"] for row in _stamped(running)} == {_after(1)}


def test_the_same_statement_run_again_takes_a_later_reading(running: Db) -> None:
    """The same text, so the second run reuses the cached prepared statement."""
    first = running.one("SELECT CURRENT_TIMESTAMP AS at, datetime('now') AS again")
    second = running.one("SELECT CURRENT_TIMESTAMP AS at, datetime('now') AS again")

    assert first == {"at": _after(1), "again": _after(1)}
    assert second == {"at": _after(2), "again": _after(2)}


def test_each_executemany_binding_takes_its_own_reading(running: Db) -> None:
    running.execute(
        "CREATE TABLE stamps ("
        "  id TEXT NOT NULL PRIMARY KEY,"
        "  at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)"
        ") STRICT"
    )
    running.executemany("INSERT INTO stamps (id) VALUES (?)", [("a",), ("b",), ("c",)])

    assert [row["at"] for row in _stamped(running)] == [_after(1), _after(2), _after(3)]


def test_a_statement_that_reads_no_time_takes_no_reading(running: Db) -> None:
    running.one("SELECT 1 AS one")

    assert running.one("SELECT CURRENT_TIMESTAMP AS at") == {"at": _after(1)}


@pytest.mark.parametrize(("overridden_sql", "reference_sql"), EQUIVALENTS)
def test_every_override_follows_a_moving_clock(
    running: Db, plain: apsw.Connection, overridden_sql: str, reference_sql: str
) -> None:
    def expected(steps: int) -> Any:
        return plain.execute(
            f"SELECT {reference_sql}", (_after(steps),) * reference_sql.count("?")
        ).get

    assert running.conn.execute(f"SELECT {overridden_sql}").get == expected(1)
    assert running.conn.execute(f"SELECT {overridden_sql}").get == expected(2)


def test_a_sandboxed_statement_takes_a_fresh_reading(running: Db) -> None:
    """The sandbox sets a cursor's `exec_trace`, which APSW runs in place of the connection's."""
    running.execute("CREATE TABLE notes (id TEXT NOT NULL PRIMARY KEY) STRICT")

    def sandboxed() -> Any:
        result = run_statement(
            running, "SELECT datetime('now') AS at", authorizer=Authorizer(("notes",))
        )
        return result.rows

    assert sandboxed() == [[_after(1)]]
    assert sandboxed() == [[_after(2)]]


def test_another_trace_does_not_displace_the_clocks(running: Db) -> None:
    seen: list[str] = []
    running.conn.trace_v2(
        apsw.SQLITE_TRACE_STMT, lambda event: seen.append(event["sql"]), id="someone else"
    )

    assert running.one("SELECT CURRENT_TIMESTAMP AS at") == {"at": _after(1)}
    assert running.one("SELECT CURRENT_TIMESTAMP AS at") == {"at": _after(2)}
    assert seen == ["SELECT CURRENT_TIMESTAMP AS at"] * 2


def test_the_clock_trace_is_registered_under_its_id(running: Db) -> None:
    """Removing it by that id is what `Db.conn` tells world code not to do."""
    running.one("SELECT CURRENT_TIMESTAMP AS at")
    running.conn.trace_v2(0, None, id=TRACE_ID)

    assert running.one("SELECT CURRENT_TIMESTAMP AS at") == {"at": _after(1)}


def test_a_failed_statements_reading_is_not_reused(running: Db) -> None:
    """A statement that took a reading and then failed does not hand it to the next one."""
    with pytest.raises(apsw.SQLError):
        running.conn.execute("SELECT CURRENT_TIMESTAMP, abs(-9223372036854775807 - 1)").fetchall()

    assert running.one("SELECT CURRENT_TIMESTAMP AS at") == {"at": _after(2)}


def test_a_statement_abandoned_after_its_first_row_takes_a_new_reading_when_run_again(
    running: Db,
) -> None:
    sql = "SELECT CURRENT_TIMESTAMP AS at FROM (VALUES (1), (2))"
    cursor = running.conn.execute(sql)
    assert next(cursor) == (_after(1),)
    cursor.close()

    assert running.conn.execute(sql).fetchall() == [(_after(2),), (_after(2),)]


# ------------------------------------------- what the per-statement reading relies on
#
# `specs/projects/clock_modes/risk_report.md` is the assessment these belong to.
# SQLite reports a statement that starts while another is executing with its text
# prefixed `-- `, and APSW reports such an event as trigger activity: that is what
# keeps a nested statement and a virtual table's own queries inside their caller's
# reading.


def test_a_nested_statement_shares_its_callers_reading(running: Db) -> None:
    conn = running.conn
    conn.execute(
        "CREATE TABLE stamps ("
        "  id TEXT NOT NULL PRIMARY KEY,"
        "  a TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),"
        "  b TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)"
        ") STRICT"
    )

    def stamp_in_python(*_args: SQLiteValue) -> SQLiteValue:
        return conn.execute("SELECT CURRENT_TIMESTAMP").get

    conn.create_scalar_function("stamp_in_python", stamp_in_python, 0)
    conn.execute("INSERT INTO stamps (id) VALUES (stamp_in_python())")

    assert _stamped(running) == [{"id": _after(1), "a": _after(1), "b": _after(1)}]


def test_a_virtual_tables_own_queries_share_the_statements_reading(running: Db) -> None:
    """FTS5 runs statements of its own on the connection, in the middle of the caller's."""
    running.conn.execute(
        "CREATE TABLE stamps ("
        "  id INTEGER PRIMARY KEY,"
        "  body TEXT NOT NULL,"
        "  created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),"
        "  indexed_at TEXT"
        ") STRICT;"
        "CREATE VIRTUAL TABLE stamps_fts USING fts5(body, content='stamps', content_rowid='id');"
        "CREATE TRIGGER index_stamp AFTER INSERT ON stamps BEGIN"
        "  INSERT INTO stamps_fts (rowid, body) VALUES (NEW.id, NEW.body);"
        "  UPDATE stamps SET indexed_at = CURRENT_TIMESTAMP WHERE id = NEW.id;"
        " END"
    )
    running.execute("INSERT INTO stamps (body) VALUES ('hello world')")

    assert running.one("SELECT created_at, indexed_at FROM stamps") == {
        "created_at": _after(1),
        "indexed_at": _after(1),
    }


def test_a_statement_re_prepared_after_a_schema_change_takes_a_fresh_reading(
    running: Db, tmp_path: Path
) -> None:
    """A second connection changes the schema, and the cached statement is re-prepared."""
    running.conn.execute(
        "CREATE TABLE anchor (id INTEGER PRIMARY KEY) STRICT; INSERT INTO anchor VALUES (1)"
    )
    sql = "SELECT CURRENT_TIMESTAMP AS at, datetime('now') AS again FROM anchor"
    assert running.one(sql) == {"at": _after(1), "again": _after(1)}

    other = apsw.Connection(str(tmp_path / "running.sqlite"))
    try:
        other.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY) STRICT")
    finally:
        other.close()

    assert running.one(sql) == {"at": _after(2), "again": _after(2)}


@pytest.mark.parametrize(
    "tracing",
    [
        pytest.param(lambda conn: conn.set_profile(lambda *_: None), id="set_profile"),
        pytest.param(
            lambda conn: conn.trace_v2(apsw.SQLITE_TRACE_STMT, lambda _: None), id="no-id"
        ),
        pytest.param(lambda conn: conn.trace_v2(0, None), id="no-id-removed"),
    ],
)
def test_other_trace_apis_leave_the_clocks_trace_in_place(
    running: Db, tracing: Callable[[apsw.Connection], None]
) -> None:
    tracing(running.conn)

    assert running.one("SELECT CURRENT_TIMESTAMP AS at") == {"at": _after(1)}
    assert running.one("SELECT CURRENT_TIMESTAMP AS at") == {"at": _after(2)}


def test_apsws_own_tracer_leaves_the_clocks_trace_in_place(running: Db) -> None:
    with apsw.ext.Trace(io.StringIO(), db=running.conn):
        assert running.one("SELECT CURRENT_TIMESTAMP AS at") == {"at": _after(1)}

    assert running.one("SELECT CURRENT_TIMESTAMP AS at") == {"at": _after(2)}


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="risk_report.md R4: APSW reports a statement whose text starts with '-- ' as"
    " trigger activity, so it keeps the previous statement's reading",
)
def test_a_statement_led_by_a_line_comment_takes_a_fresh_reading(running: Db) -> None:
    assert running.one("SELECT CURRENT_TIMESTAMP AS at") == {"at": _after(1)}

    assert running.one("-- a note\nSELECT CURRENT_TIMESTAMP AS at") == {"at": _after(2)}


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="risk_report.md R6: a statement started between another's rows drops that"
    " statement's reading",
)
def test_a_statement_keeps_its_reading_while_another_runs_between_its_rows(
    running: Db, plain: apsw.Connection
) -> None:
    """Only a date function with an argument that changes per row is evaluated per row."""
    conn = running.conn
    conn.execute(
        "CREATE TABLE due (at TEXT NOT NULL) STRICT;"
        "INSERT INTO due VALUES ('2024-01-01'), ('2024-01-02'), ('2024-01-03')"
    )
    ages = []
    for (age,) in conn.execute("SELECT timediff('now', at) FROM due"):
        ages.append(age)
        running.one("SELECT CURRENT_TIMESTAMP AS at")

    reference = [
        plain.execute("SELECT timediff(?, ?)", (_after(1), day)).get
        for day in ("2024-01-01", "2024-01-02", "2024-01-03")
    ]
    assert ages == reference
