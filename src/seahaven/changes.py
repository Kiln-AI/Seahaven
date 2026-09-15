"""What an instance changed, as data an eval can grade on.

The changeset is the net difference between the fixture and the current state,
not a log of calls. A write that leaves a value unchanged records nothing; an
insert followed by an update of the same row is one insert; a call that rolled
back leaves no trace. That is SQLite's session extension speaking, and it is the
property that makes a changeset worth grading: two runs that reached the same
state agree, whatever route they took.

The session is attached to the world's tables at instance creation, after the
startup hooks have run, so seed rows are starting state rather than agent
changes. A composite instance has one session per node, each over its own world's
tables and its own `untracked_tables`, and every `Change` says which node it came
from.
"""

import base64
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

import apsw

from seahaven.db import world_tables
from seahaven.errors import WorldBug

if TYPE_CHECKING:  # `world.py` imports this module's callers; the annotation is all that is needed
    from seahaven.world import World

__all__ = ["Change", "render", "start_session"]

# SQLite's own names for what a row change is, as this framework spells them.
_OPS: dict[str, Literal["insert", "update", "delete"]] = {
    "INSERT": "insert",
    "UPDATE": "update",
    "DELETE": "delete",
}


@dataclass(frozen=True)
class Change:
    """One row the instance changed, with its columns named and its node said.

    `world` is the path of the node the row belongs to -- `main` for the root,
    and the added node's canonical path otherwise -- which is what tells two
    tables of the same name in two stores apart. A world that adds nothing has
    one node, so every change it makes says `main`.

    `before` and `after` hold only the columns the change carries: a changeset
    marks the rest `apsw.no_change`, which is not the same as `NULL`, and
    flattening the two would turn "changed the assignee" into "rewrote the row".
    An update's `before` therefore holds the key columns and the old values of
    what changed, its `after` the new values of what changed, and `key` the
    primary key either way.
    """

    world: str
    table: str
    op: Literal["insert", "update", "delete"]
    key: dict[str, Any]
    before: dict[str, Any] | None
    after: dict[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        """The wire shape: what `controller_changes` returns and an eval reads."""
        return {
            "world": self.world,
            "table": self.table,
            "op": self.op,
            "key": self.key,
            "before": self.before,
            "after": self.after,
        }


def start_session(conn: apsw.Connection, world: World) -> apsw.Session:
    """Record every change to the world's own tables, from now on.

    Virtual tables are left out because the session extension cannot track one,
    and the tables a world names in `World(untracked_tables=...)` because it said
    to.
    """
    untracked = frozenset(world.untracked_tables)
    virtual = _virtual_tables(conn)
    session = apsw.Session(conn, "main")
    for table in world_tables(conn):
        if table in untracked or table in virtual:
            continue
        _refuse_a_table_with_no_primary_key(conn, table)
        session.attach(table)
    return session


def render(changeset: bytes, conn: apsw.Connection, world: str) -> list[Change]:
    """One node's changeset as `Change` records, in the changeset's own order.

    That order is by table and then by rowid, which is deterministic for a given
    sequence of writes, so two identical runs render identically.

    `world` is the path of the node `conn` belongs to, stamped on every record.
    It is a parameter and not a default because every caller is rendering one
    node of a tree and knows which: a default would be right for the root and
    quietly wrong for the caller that forgot.
    """
    columns: dict[str, tuple[list[str], list[int]]] = {}
    changes = []
    for change in apsw.Changeset.iter(changeset):
        table = change.name
        if table not in columns:
            columns[table] = _columns(conn, table)
        names, key_positions = columns[table]
        op = _OPS[change.op]
        # The key is in whichever side of the change has it: an insert has only
        # `new`, everything else carries the old row's key in `old`.
        source = change.new if op == "insert" else change.old
        changes.append(
            Change(
                world=world,
                table=table,
                op=op,
                key=_row(names, source, key_positions) or {},
                before=_row(names, change.old),
                after=_row(names, change.new),
            )
        )
    return changes


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
    every write to it would be missing from the changeset with nothing to say so.
    """
    _names, key = _columns(conn, table)
    if not key:
        raise WorldBug(
            f"table {table!r} has no explicit primary key, so the changeset could not record its "
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
