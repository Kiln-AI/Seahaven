"""What an instance changed, as data an eval can grade on.

The **change log** is the ordered record of every row the instance changed after
startup, one record per row per call: a session is opened for each call, and what
it recorded is rendered and appended when the call's transaction is done.

The session extension is what speaks, and its rule is what makes a record the net
of its call: a write that leaves a value unchanged records nothing, an insert
followed by an update of the same row is one insert, and a call that rolled back
leaves no trace.

No session sees the startup hooks. They run at instance creation, before the
first call opens a session of its own, so seed rows are starting state rather
than agent changes.
"""

import base64
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

import apsw

from seahaven.db import world_tables
from seahaven.errors import WorldBug

if TYPE_CHECKING:  # `world.py` imports this module's callers; the annotation is all that is needed
    from seahaven.world import World

__all__ = [
    "LogRecord",
    "open_session",
    "render_log",
    "tracked_tables",
]

# SQLite's own names for what a row change is, as this framework spells them.
_OPS: dict[str, Literal["insert", "update", "delete"]] = {
    "INSERT": "insert",
    "UPDATE": "update",
    "DELETE": "delete",
}


@dataclass(frozen=True)
class LogRecord:
    """One row one call changed: the unit of the change log.

    The whole row on an insert and on a delete, because that is the row; on an
    update, exactly the non-key columns the call changed, old values on one side
    and new on the other. The key is never repeated inside an update's sides: it
    is in `key`, which is where a reader joins on it.

    `i` is the ordinal of the call that made the change, or `None` for a write
    made with no call in flight (`inst.bulk()`). `subworld` is `None` in this
    release; the field exists so that a composed world's log stays one flat list.
    """

    i: int | None
    subworld: str | None
    table: str
    op: Literal["insert", "update", "delete"]
    key: dict[str, Any]
    before: dict[str, Any] | None
    after: dict[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        """The wire shape, in the field order `functional_spec.md` §3.2 publishes.

        The three dicts are copied: the document is the caller's to edit, and
        `functional_spec.md` §6's recipe for a custom formatter is to read a
        built-in's `state` and change it. A shallow copy is the whole of it
        because every value is a JSON scalar by the time `_jsonable` is done.
        """
        return {
            "i": self.i,
            "subworld": self.subworld,
            "table": self.table,
            "op": self.op,
            "key": dict(self.key),
            "before": None if self.before is None else dict(self.before),
            "after": None if self.after is None else dict(self.after),
        }


def tracked_tables(conn: apsw.Connection, world: World) -> tuple[str, ...]:
    """Every table a session of this instance records, in name order.

    Virtual tables are left out because the session extension cannot track one,
    and the tables a world names in `World(untracked_tables=...)` because it said
    to. A table with no explicit primary key is refused here rather than attached
    quietly, which is why this is asked once at instance creation: the refusal is
    a `WorldBug` about the world, and every session the instance opens afterwards
    attaches the list this answered.
    """
    untracked = frozenset(world.untracked_tables)
    virtual = _virtual_tables(conn)
    tables = []
    for table in world_tables(conn):
        if table in untracked or table in virtual:
            continue
        _refuse_a_table_with_no_primary_key(conn, table)
        tables.append(table)
    return tuple(tables)


def open_session(conn: apsw.Connection, tracked: Sequence[str]) -> apsw.Session:
    """Record every change to those tables, from now on.

    One of these is opened per call, so it is deliberately no more than a
    `Session` and an `attach` each: both are C, and the list they are given was
    computed once when the instance was made.
    """
    session = apsw.Session(conn, "main")
    for table in tracked:
        session.attach(table)
    return session


def render_log(
    changeset: bytes,
    conn: apsw.Connection,
    columns: dict[str, tuple[list[str], list[int]]],
    *,
    i: int | None,
) -> list[LogRecord]:
    """One call's changeset as log records, sorted as `functional_spec.md` §3.3 says.

    `columns` is the caller's cache of `_columns`, kept for the life of the
    instance: a world's schema does not change while one is alive, so a table is
    looked up once however many calls touch it.
    """
    rendered = []
    for change in apsw.Changeset.iter(changeset):
        table = change.name
        if table not in columns:
            columns[table] = _columns(conn, table)
        names, key_positions = columns[table]
        op = _OPS[change.op]
        # The key is in whichever side of the change has it: an insert has only
        # `new`, everything else carries the old row's key in `old`.
        source = change.new if op == "insert" else change.old
        before, after = _sides(names, key_positions, op, change.old, change.new)
        rendered.append(
            LogRecord(
                i=i,
                subworld=None,
                table=table,
                op=op,
                key=_row(names, source, key_positions) or {},
                before=before,
                after=after,
            )
        )
    rendered.sort(key=_sort_key)
    return rendered


def _sides(
    names: list[str],
    key_positions: list[int],
    op: Literal["insert", "update", "delete"],
    old: tuple[Any, ...] | None,
    new: tuple[Any, ...] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """A record's `before` and `after`: whole rows at the ends of a row's life, deltas between.

    An update carries exactly the columns it changed -- a changeset marks the
    rest `apsw.no_change`, which is not the same as `NULL`, and `_row` drops
    them, so asking it for the non-key columns leaves exactly the changed ones --
    and carries them without the key, which `key` already holds. An insert and a
    delete carry the whole row, key columns included, because that is the row.
    """
    if op != "update":
        return _row(names, old), _row(names, new)
    keys = frozenset(key_positions)
    non_key = [position for position in range(len(names)) if position not in keys]
    return _row(names, old, non_key), _row(names, new, non_key)


def _sort_key(record: LogRecord) -> tuple[Any, ...]:
    """Where a record sorts inside its call: sub-world, then table, then key values."""
    return (
        record.subworld or "",
        record.table,
        tuple(_sqlite_rank(value) for value in record.key.values()),
    )


def _sqlite_rank(value: Any) -> tuple[int, Any]:
    """A published key value in SQLite's own order: NULL, then numbers, then text.

    The *rendered* value, not the raw one. The within-call order is part of the
    format (`functional_spec.md` §3.3) and the fold a consumer computes re-sorts
    by it (§3.6), and a consumer reading a saved document has a blob's base64
    text and never its bytes -- so a blob key sorts by that text, which is the
    only order a reader can reproduce. The leading rank is what keeps the sort
    total: a key column declared ANY can hold two storage classes, which Python
    would refuse to compare. Every tracked table is STRICT in a linted world, so
    in practice a column's rank never varies.
    """
    match value:
        case None:
            return (0, None)
        case int() | float():
            return (1, value)
        case _:
            return (2, value)


def _row(
    names: list[str], values: tuple[Any, ...] | None, positions: list[int] | None = None
) -> dict[str, Any] | None:
    """The columns a change carries, named. `None` where the change has no such side."""
    if values is None:
        return None
    wanted = range(len(names)) if positions is None else positions
    return {
        names[position]: _jsonable(values[position])
        for position in wanted
        if values[position] is not apsw.no_change
    }


def _jsonable(value: Any) -> Any:
    """A SQLite value as something JSON can carry: a blob becomes base64 text."""
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if isinstance(value, float) and math.isinf(value):
        # JSON has no infinities; SQLite already stores NaN as NULL, so this is
        # its rule one step further (functional_spec.md §3.4).
        return None
    return value


def _columns(conn: apsw.Connection, table: str) -> tuple[list[str], list[int]]:
    """A table's column names in storage order, and the positions of its key, in key order."""
    # The cast says what the pragma returns; APSW types every column as any
    # SQLite value.
    rows = cast(
        list[tuple[str, int]],
        conn.execute("SELECT name, pk FROM pragma_table_info(?) ORDER BY cid", (table,)).fetchall(),
    )
    key = sorted((pk, position) for position, (_name, pk) in enumerate(rows) if pk > 0)
    return [name for name, _pk in rows], [position for _pk, position in key]


def _refuse_a_table_with_no_primary_key(conn: apsw.Connection, table: str) -> None:
    """A table with no explicit primary key cannot be tracked, so it is not attached quietly.

    The session extension attaches one happily and then records nothing for it:
    every write to it would be missing from the change log with nothing to say so.
    """
    _names, key = _columns(conn, table)
    if not key:
        raise WorldBug(
            f"table {table!r} has no explicit primary key, so the change log could not record its "
            f"writes; give it one, or name it in World(untracked_tables=...)"
        )


def _virtual_tables(conn: apsw.Connection) -> frozenset[str]:
    rows = cast(
        list[tuple[str]],
        conn.execute(
            "SELECT name FROM pragma_table_list WHERE schema = 'main' AND type = 'virtual'"
        ).fetchall(),
    )
    return frozenset(name for (name,) in rows)
