"""Running a caller's `setup_sql` against a new instance, before its first call.

`world.instance(setup_sql=...)` hands this module a string of statements. They
run after the fixture is copied (or the blank schema is built) and before any
node connection is opened, on one writable connection to the root's file with
every added node attached under its schema name, in one transaction. Nothing
else is open on those files yet, so nothing can be waiting on a lock.

The statements run under `SetupAuthorizer`, the sandbox's default-deny
authorizer with every table of every node allowed for reading and writing. So
setup SQL reads and writes rows and does nothing else: no schema change, no
transaction control, no attach, no pragma that sets anything, and only the
functions `run_sql` has. These checks catch an author's mistakes and are not a
boundary: `SetupAuthorizer` says where they stop.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import apsw

from seahaven.clock import Clock, register_clock_functions
from seahaven.db import _READ_ONLY_PRAGMAS, _fold_ascii, _harden
from seahaven.errors import WorldBug
from seahaven.ids import SETUP_STREAM, register_random_functions
from seahaven.sandbox import _ROW_WRITES, Authorizer

__all__ = ["Refused", "SetupAuthorizer", "run_setup_sql", "split_statements"]

_QUOTED_CHARACTERS = 200

_SCHEMA_ACTIONS = frozenset(
    {
        apsw.SQLITE_ALTER_TABLE,
        apsw.SQLITE_ANALYZE,
        apsw.SQLITE_CREATE_INDEX,
        apsw.SQLITE_CREATE_TABLE,
        apsw.SQLITE_CREATE_TEMP_INDEX,
        apsw.SQLITE_CREATE_TEMP_TABLE,
        apsw.SQLITE_CREATE_TEMP_TRIGGER,
        apsw.SQLITE_CREATE_TEMP_VIEW,
        apsw.SQLITE_CREATE_TRIGGER,
        apsw.SQLITE_CREATE_VIEW,
        apsw.SQLITE_CREATE_VTABLE,
        apsw.SQLITE_DROP_INDEX,
        apsw.SQLITE_DROP_TABLE,
        apsw.SQLITE_DROP_TEMP_INDEX,
        apsw.SQLITE_DROP_TEMP_TABLE,
        apsw.SQLITE_DROP_TEMP_TRIGGER,
        apsw.SQLITE_DROP_TEMP_VIEW,
        apsw.SQLITE_DROP_TRIGGER,
        apsw.SQLITE_DROP_VIEW,
        apsw.SQLITE_DROP_VTABLE,
        apsw.SQLITE_REINDEX,
    }
)
# SQLite's own schema tables. A row write to one of these is the schema
# changing; a write to another `sqlite_` table, such as `sqlite_sequence`, is
# refused for the sandbox's own reason.
_SCHEMA_TABLES = frozenset({"sqlite_master", "sqlite_schema", "sqlite_temp_master"})

_SCHEMA_FIX = (
    "setup_sql may only read and write rows (INSERT, UPDATE, DELETE, SELECT). Change the schema "
    "in the world and bump its version."
)


@dataclass(frozen=True)
class Refused:
    """The first action `SetupAuthorizer` refused in one statement."""

    action: int
    third: str | None
    fourth: str | None
    # The base class's own words for it, for an action this module has no reason for.
    description: str


class SetupAuthorizer(Authorizer):
    """The sandbox's authorizer, writable over every table, recording what it refuses.

    It also allows the pragmas the read-only inspection connection allows, which
    report and set nothing, and any read or function call SQLite attributes to
    one of `triggers`, the world's own triggers. That attribution is by name
    only, so it is a guard against mistakes and not a boundary: see `__init__`.
    """

    def __init__(self, tables: Sequence[str], triggers: Iterable[str] = ()) -> None:
        super().__init__(tables, read_only=False)
        # SQLite's auth context names the innermost trigger, view or named FROM
        # item, a CTE included, and it names a view while it resolves the
        # caller's own WHERE clause in DML on a view with an INSTEAD OF trigger.
        # So a name is trusted only as a trigger, and never when a table or a
        # view shares it (SQLite lets a trigger and a view have one name). A CTE
        # the caller names after a world trigger still shares that trigger's
        # trust for function calls and reads; that residual is accepted, because
        # setup SQL comes from the eval's author and these checks catch mistakes.
        shared = {_fold_ascii(name) for name in tables}
        self._triggers = frozenset(_fold_ascii(name) for name in triggers) - shared
        self.refused: Refused | None = None

    def reset(self) -> None:
        super().reset()
        self.refused = None

    def __call__(
        self,
        action: int,
        third: str | None,
        fourth: str | None,
        database: str | None,
        trigger: str | None,
        /,
    ) -> int:
        if (
            action == apsw.SQLITE_PRAGMA
            and third is not None
            and _fold_ascii(third) in _READ_ONLY_PRAGMAS
        ):
            return apsw.SQLITE_OK
        # A world trigger's body is the world's own SQL, run as world code runs it.
        if (
            trigger is not None
            and _fold_ascii(trigger) in self._triggers
            and action in (apsw.SQLITE_FUNCTION, apsw.SQLITE_READ)
        ):
            return apsw.SQLITE_OK
        answer = super().__call__(action, third, fourth, database, trigger)
        if answer == apsw.SQLITE_DENY and self.refused is None:
            self.refused = Refused(action, third, fourth, self.refusals[-1])
        return answer


def run_setup_sql(
    sql: str,
    *,
    root: Path,
    attachments: Sequence[tuple[str, Path]],
    clock: Clock,
    seed: bytes,
) -> None:
    """Run a caller's setup SQL against a new instance's files, in one transaction.

    `attachments` are `(schema name, file)` for every node but the root. `seed`
    is the root node's: `random()` draws from it on `SETUP_STREAM`, whichever
    node a row lands in. Raises `WorldBug` naming the statement that failed,
    with every statement before it rolled back.
    """
    statements = split_statements(sql)
    if not statements:
        return
    conn = apsw.Connection(str(root))
    try:
        _harden(conn)
        conn.set_busy_timeout(0)
        register_random_functions(conn, seed, SETUP_STREAM)
        helper = register_clock_functions(conn, clock)
        try:
            for schema, path in attachments:
                conn.execute("ATTACH DATABASE ? AS ?", (str(path), schema))
            # An instance directory does not survive a crash (the sweep deletes
            # it), so no file of it needs an fsync. Per schema: an attached
            # file keeps SQLite's default otherwise.
            for schema in ("main", *(schema for schema, _ in attachments)):
                conn.pragma("synchronous", "OFF", schema=schema)
            schemas = [schema for schema, _ in attachments]
            authorizer = SetupAuthorizer(
                _every_table(conn), _every_trigger(conn, ["main", *schemas])
            )
            _run(conn, statements, authorizer, schemas)
        finally:
            helper.close()
    finally:
        conn.close()


def _run(
    conn: apsw.Connection,
    statements: Sequence[str],
    authorizer: SetupAuthorizer,
    schemas: Sequence[str],
) -> None:
    """Run every statement in one transaction, committed only if all of them succeed."""
    # `BEGIN` before the authorizer and `COMMIT` after it: it refuses transaction
    # control, the framework's own included.
    conn.execute("BEGIN")
    cursor = conn.cursor()
    try:
        conn.authorizer = authorizer
        try:
            for index, statement in enumerate(statements, 1):
                authorizer.reset()
                try:
                    # Drained, so a statement that returns rows runs to its end.
                    for _ in cursor.execute(statement):
                        pass
                except apsw.Error as error:
                    raise _failure(
                        index, len(statements), statement, authorizer.refused, error, schemas
                    ) from error
        finally:
            conn.authorizer = None
    except BaseException:
        cursor.close(force=True)
        # A failed statement may already have ended the transaction itself (an
        # `ON CONFLICT ROLLBACK`), and a second ROLLBACK would hide the error.
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    cursor.close()
    try:
        conn.execute("COMMIT")
    except apsw.Error as error:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise WorldBug(
            f"setup_sql failed at commit: {error}. A deferred constraint is checked once every "
            "statement has run."
        ) from error


def split_statements(sql: str) -> list[str]:
    """`sql` as its separate statements, in order, without their trailing `;`.

    A `;` inside a string literal, a quoted identifier, a comment or a trigger
    body does not split, because a piece is only a statement once SQLite says
    it is complete. Whitespace between statements is dropped. Text after the last
    complete statement is kept as a statement of its own, so SQLite reports it.
    """
    statements: list[str] = []
    pending = ""
    *pieces, last = sql.split(";")
    for piece in pieces:
        pending += piece + ";"
        if apsw.complete(pending):
            _keep(statements, pending.removesuffix(";"))
            pending = ""
    _keep(statements, pending + last)
    return statements


def _keep(statements: list[str], text: str) -> None:
    if text.strip():
        statements.append(text.strip())


def _every_trigger(conn: apsw.Connection, schemas: Sequence[str]) -> list[str]:
    """The name of every trigger the world's schemas hold."""
    names: list[str] = []
    for schema in schemas:
        quoted = '"' + schema.replace('"', '""') + '"'
        rows = conn.execute(f"SELECT name FROM {quoted}.sqlite_schema WHERE type = 'trigger'")
        names.extend(str(name) for (name,) in rows)
    return names


def _every_table(conn: apsw.Connection) -> list[str]:
    """Every table and view on every schema of `conn`, FTS5's shadow tables included.

    FTS5 reads and writes its shadow tables through this connection while a
    caller's statement runs, so they are allowed here. A caller's own write to
    one is still refused, by SQLite's defensive mode (`db._harden`).
    """
    rows = conn.execute(
        "SELECT name FROM pragma_table_list WHERE type IN ('table', 'view', 'virtual', 'shadow') "
        "AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\'"
    ).fetchall()
    return [str(name) for (name,) in rows]


def _failure(
    index: int,
    count: int,
    statement: str,
    refused: Refused | None,
    error: apsw.Error,
    schemas: Sequence[str],
) -> WorldBug:
    reason, fix = _explain(refused, error, schemas)
    outcome = "failed" if refused is None else "was refused"
    explanation = f"{reason} {fix}" if fix else reason
    return WorldBug(
        f"setup_sql statement {index} of {count} {outcome}: {explanation}\n  {_quoted(statement)}"
    )


def _explain(refused: Refused | None, error: apsw.Error, schemas: Sequence[str]) -> tuple[str, str]:
    """Why a statement failed, and what to do instead. The fix may be empty."""
    if refused is None:
        message = str(error)
        if message.startswith("no such table") and schemas:
            return (
                f"{message}.",
                f"An added node's tables are named <schema>.<table>{_named(schemas)}",
            )
        return f"{message}.", ""
    action = refused.action
    if action in _SCHEMA_ACTIONS or (
        action in _ROW_WRITES and _fold_ascii(refused.third or "") in _SCHEMA_TABLES
    ):
        return "it changes the schema.", _SCHEMA_FIX
    if action in (apsw.SQLITE_TRANSACTION, apsw.SQLITE_SAVEPOINT):
        return (
            "it controls the transaction.",
            "setup_sql already runs in one transaction that Seahaven opens and commits; remove "
            "BEGIN, COMMIT and SAVEPOINT.",
        )
    if action in (apsw.SQLITE_ATTACH, apsw.SQLITE_DETACH):
        return (
            "it attaches or detaches a database.",
            "Every node is already attached: name the root's tables unqualified and an added "
            f"node's as <schema>.<table>{_named(schemas)}",
        )
    if action == apsw.SQLITE_PRAGMA:
        return (
            f"PRAGMA {refused.third} can change the connection.",
            "Only read-only pragmas such as table_info are allowed.",
        )
    if action == apsw.SQLITE_FUNCTION:
        return (
            f"function {refused.fourth}() is not allowed.",
            "setup_sql has the functions run_sql has.",
        )
    return (
        f"{refused.description} is not allowed.",
        "setup_sql may only read and write the world's own tables.",
    )


def _named(schemas: Sequence[str]) -> str:
    """The schema names this world's added nodes have, as the end of a sentence."""
    if not schemas:
        return " (this world adds none)."
    return f" (this world: {', '.join(schemas)})."


def _quoted(statement: str) -> str:
    collapsed = re.sub(r"\s+", " ", statement).strip()
    if len(collapsed) <= _QUOTED_CHARACTERS:
        return collapsed
    return collapsed[:_QUOTED_CHARACTERS] + "..."
