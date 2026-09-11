"""Running SQL an agent wrote, without letting it out of the world.

This module is the enforcement, and it is the only enforcement: nothing parses,
rewrites or pre-screens the text before SQLite sees it. The text goes to SQLite
as written, and SQLite is asked, action by action, whether it may. So an agent's
SQL cannot write a row it was not offered, read a table it was not offered,
attach a file, or call a function the allowlist does not name -- not because a
filter caught the phrasing, but because there is no phrasing that gets past the
authorizer. That is what makes this a sandbox rather than a filter, and it is why
the tests attack it with raw SQLite text.

It is public API: an extension serving another dialect builds its own tool on
`Authorizer` and `run_statement`, and classifies on the refusal text.

Four things bound a statement:

* an authorizer that default-denies. SQLite asks about every action it is about
  to take, and anything not explicitly allowed is refused.
* one statement per call, enforced by SQLite's own execution tracer: a payload
  that carries a second statement is aborted before that statement steps. The
  tracer only ever sees a statement SQLite is about to run, so a payload whose
  *first* statement hits a row or byte cap ends there and comes back
  `truncated` rather than refused -- the statements after it are discarded
  unrun with the cursor. Containment holds either way; the caller is simply not
  told there was more.
* `sqlite3_stmt_readonly`, SQLite's own verdict on whether the statement writes,
  checked before it steps.
* a cap on how large one value may get, because nothing can interrupt SQLite
  *inside* a single opcode. See `MAX_VALUE_BYTES`.
"""

import base64
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast, get_args

import apsw

from seahaven.db import Db, SqlValue, _fold_ascii
from seahaven.errors import DbError, WorldBug

__all__ = [
    "ALLOWED_FUNCTIONS",
    "MAX_VALUE_BYTES",
    "REFUSALS",
    "Authorizer",
    "Refusal",
    "SqlResult",
    "refusal_kind",
    "run_statement",
]

# SQLite functions an agent's SQL may call. An allowlist rather than a denylist,
# because the interesting ones to refuse are the ones nobody thought of.
#
# The three groups left out on purpose:
#   * `random`, `randomblob` -- an instance is meant to be reproducible.
#   * `load_extension` -- loads native code. Denied here and disabled on the
#     connection; either alone would do.
#   * `sqlite_version`, `sqlite_source_id`, `changes`, `last_insert_rowid`,
#     `total_changes`, `sqlite_offset` -- these describe the host and the
#     instance's write history, neither of which is part of the world.
#
# FTS5's auxiliary functions (`bm25`, `snippet`, `highlight`) are absent because
# the shadow tables they need are denied. A world that exposes search through SQL
# passes its own `functions` and allows those tables explicitly.
ALLOWED_FUNCTIONS = frozenset(
    {
        # aggregates
        "avg", "count", "group_concat", "max", "min", "string_agg", "sum", "total",
        # window functions
        "cume_dist", "dense_rank", "first_value", "lag", "last_value", "lead",
        "nth_value", "ntile", "percent_rank", "rank", "row_number",
        # text
        "char", "concat", "concat_ws", "format", "glob", "hex", "instr", "length",
        "like", "lower", "ltrim", "printf", "quote", "replace", "rtrim", "substr",
        "substring", "trim", "unhex", "unicode", "upper",
        # values and numbers
        "abs", "coalesce", "iif", "ifnull", "likelihood", "likely", "nullif",
        "round", "sign", "typeof", "unlikely",
        # math (present when SQLite is built with SQLITE_ENABLE_MATH_FUNCTIONS)
        "acos", "acosh", "asin", "asinh", "atan", "atan2", "atanh", "ceil",
        "ceiling", "cos", "cosh", "degrees", "exp", "floor", "ln", "log", "log10",
        "log2", "mod", "pi", "pow", "power", "radians", "sin", "sinh", "sqrt",
        "tan", "tanh", "trunc",
        # json, including the `->` and `->>` operators, which SQLite asks about
        # under those names
        "->", "->>", "json", "json_array", "json_array_length", "json_error_position",
        "json_extract", "json_group_array", "json_group_object", "json_insert",
        "json_object", "json_patch", "json_pretty", "json_quote", "json_remove",
        "json_replace", "json_set", "json_type", "json_valid",
        # the clock. These names are `clock.py`'s overrides, not SQLite's originals.
        "current_date", "current_time", "current_timestamp", "date", "datetime",
        "julianday", "strftime", "time", "timediff", "unixepoch",
    }
)  # fmt: skip

# The largest single value, text or binary, one agent-authored statement may build.
#
# Nothing acts inside one VDBE opcode, so nothing else can stop SQLite part-way
# through building a single value. At SQLite's own default of 1 GB,
# `SELECT length(printf('%1000000000d', 1))` runs for seconds and peaks near a
# gigabyte of resident memory before failing. `s || s` in a recursive CTE does the
# same, so this is a property of one opcode and not of one function.
#
# Set on the connection for the length of the statement and put back afterwards,
# the way the authorizer is. It deliberately is not one of the connection's own
# limits: a connection-wide cap would bind the world's writes too. This one
# belongs to the authorizer's scope.
#
# It fixes the allocation completely and the *time* only partly. `instr` and
# `replace` are quadratic in their arguments and run entirely inside one
# `OP_Function`, so two operands that both fit under this cap still take seconds.
# The memory stays flat and the overrun is bounded at roughly one opcode, but it
# is a residual rather than an oversight: no per-function denylist closes it, for
# the same reason that taking `printf` off the allowlist would not have closed
# the allocation. `test_sandbox.py` pins it so the residual stays visible.
MAX_VALUE_BYTES = 1_000_000

# What each action code is called when it is refused. The agent reads this, so it
# says what SQLite was about to do rather than quoting an integer back.
_ACTIONS = {
    apsw.SQLITE_ALTER_TABLE: "ALTER TABLE",
    apsw.SQLITE_ANALYZE: "ANALYZE",
    apsw.SQLITE_ATTACH: "ATTACH",
    apsw.SQLITE_CREATE_INDEX: "CREATE INDEX",
    apsw.SQLITE_CREATE_TABLE: "CREATE TABLE",
    apsw.SQLITE_CREATE_TEMP_INDEX: "CREATE TEMP INDEX",
    apsw.SQLITE_CREATE_TEMP_TABLE: "CREATE TEMP TABLE",
    apsw.SQLITE_CREATE_TEMP_TRIGGER: "CREATE TEMP TRIGGER",
    apsw.SQLITE_CREATE_TEMP_VIEW: "CREATE TEMP VIEW",
    apsw.SQLITE_CREATE_TRIGGER: "CREATE TRIGGER",
    apsw.SQLITE_CREATE_VIEW: "CREATE VIEW",
    apsw.SQLITE_CREATE_VTABLE: "CREATE VIRTUAL TABLE",
    apsw.SQLITE_DELETE: "DELETE",
    apsw.SQLITE_DETACH: "DETACH",
    apsw.SQLITE_DROP_INDEX: "DROP INDEX",
    apsw.SQLITE_DROP_TABLE: "DROP TABLE",
    apsw.SQLITE_DROP_TEMP_INDEX: "DROP TEMP INDEX",
    apsw.SQLITE_DROP_TEMP_TABLE: "DROP TEMP TABLE",
    apsw.SQLITE_DROP_TEMP_TRIGGER: "DROP TEMP TRIGGER",
    apsw.SQLITE_DROP_TEMP_VIEW: "DROP TEMP VIEW",
    apsw.SQLITE_DROP_TRIGGER: "DROP TRIGGER",
    apsw.SQLITE_DROP_VIEW: "DROP VIEW",
    apsw.SQLITE_DROP_VTABLE: "DROP VIRTUAL TABLE",
    apsw.SQLITE_INSERT: "INSERT",
    apsw.SQLITE_PRAGMA: "PRAGMA",
    apsw.SQLITE_REINDEX: "REINDEX",
    apsw.SQLITE_SAVEPOINT: "SAVEPOINT",
    apsw.SQLITE_TRANSACTION: "transaction control",
    apsw.SQLITE_UPDATE: "UPDATE",
}

# The three actions that write rows; everything else on a connection is DDL or
# a connection-level verb, and is refused as an action.
_ROW_WRITES = frozenset({apsw.SQLITE_DELETE, apsw.SQLITE_INSERT, apsw.SQLITE_UPDATE})

# The stable vocabulary of refusals. Every string in `Authorizer.refusals` and in
# `DbError.refusals` begins with one of these words, so an extension serving
# another dialect can classify what the sandbox turned down without reading the
# rest of the sentence. The names are API: they do not change.
type Refusal = Literal["read", "write", "function", "action", "statement", "value"]


REFUSALS: frozenset[str] = frozenset(get_args(Refusal.__value__))


def refusal_kind(refusal: str) -> Refusal:
    """Which `Refusal` a refusal string belongs to: the classification seam."""
    kind = refusal.split(" ", 1)[0]
    if kind not in REFUSALS:
        raise WorldBug(f"not a refusal: {refusal!r}")
    return cast(Refusal, kind)


class Authorizer:
    """SQLite's permission callback, written as default-deny.

    It also remembers what it refused. That matters because SQLite reports its own
    refusals inconsistently -- a denied table read raises `apsw.AuthError` while a
    denied function raises `apsw.SQLError`, and the two are siblings in APSW's
    flat hierarchy rather than parent and child. Classifying by exception type
    would report half of these refusals as world bugs, and classifying by message
    text would miss the next wording, so `run_statement` asks the authorizer
    instead: a statement that failed while this object was refusing things failed
    *because* of the refusal.

    One per call: `refusals` is per-statement state, so an `Authorizer` is never
    shared between calls or instances. The two allowlists are decided once, by
    whatever builds the tool, and passed in; both are folded here, so a caller may
    spell a table or a function in any case SQLite would accept.
    """

    def __init__(
        self,
        tables: Iterable[str],
        *,
        read_only: bool = True,
        functions: frozenset[str] = ALLOWED_FUNCTIONS,
    ) -> None:
        self._tables = frozenset(_fold_ascii(name) for name in tables)
        self._functions = frozenset(_fold_ascii(name) for name in functions)
        self.read_only = read_only
        self._refusals: list[str] = []

    @property
    def refusals(self) -> tuple[str, ...]:
        """What this authorizer turned down since the last `reset`."""
        return tuple(self._refusals)

    def reset(self) -> None:
        self._refusals.clear()

    def __call__(
        self,
        action: int,
        third: str | None,
        fourth: str | None,
        database: str | None,
        trigger: str | None,
        /,
    ) -> int:
        # Positional-only: SQLite calls this by position, and an extension
        # subclassing it should not have to copy five parameter names.
        if action == apsw.SQLITE_READ:
            return self._table("read", third)
        if action in (apsw.SQLITE_SELECT, apsw.SQLITE_RECURSIVE):
            return apsw.SQLITE_OK
        if action == apsw.SQLITE_FUNCTION:
            if fourth is not None and _fold_ascii(fourth) in self._functions:
                return apsw.SQLITE_OK
            return self._refuse(f"function {fourth!r}")
        if action in _ROW_WRITES and not _is_schema_table(third):
            if self.read_only:
                return self._refuse(f"write of table {third!r} in a read-only query")
            return self._table("write", third)
        return self._refuse(_describe(action, third, fourth))

    def _table(self, what: Literal["read", "write"], name: str | None) -> int:
        # Folded, because SQLite canonicalises the table name for a *column* read
        # and hands back the agent's own spelling for a bare row read:
        # `SELECT id FROM ISSUES` arrives as `issues` and `SELECT count(*) FROM
        # ISSUES` as `ISSUES`. Comparing raw would refuse the second one -- a
        # false deny on the commonest query shape there is.
        if name is not None and _fold_ascii(name) in self._tables:
            return apsw.SQLITE_OK
        # SQLITE_IGNORE would let the statement run with NULLs in place of the
        # values -- and `count(*)` would still return the true row count, which is
        # the data being refused. Denying is the only answer that is a refusal.
        return self._refuse(f"{what} of table {name!r}")

    def _refuse(self, what: str) -> int:
        self._refusals.append(what)
        return apsw.SQLITE_DENY


# Actions whose *fourth* argument is the thing being acted on: SQLite passes the
# database name third for `ALTER TABLE` and the savepoint's operation third for
# `SAVEPOINT`. The agent reads these strings, so they name the right object.
_NAMED_BY_FOURTH = frozenset({apsw.SQLITE_ALTER_TABLE, apsw.SQLITE_SAVEPOINT})


def _is_schema_table(name: str | None) -> bool:
    """Whether a row write is really DDL.

    SQLite asks about `CREATE TABLE` as an INSERT into `sqlite_master`, so a row
    write to one of its own tables is the schema changing, not the agent writing
    a row, and is classified as the action it is.
    """
    return name is not None and _fold_ascii(name).startswith("sqlite_")


def _describe(action: int, third: str | None, fourth: str | None) -> str:
    named = _ACTIONS.get(action, str(action))
    subject = fourth if action in _NAMED_BY_FOURTH else third
    return f"action {named} {subject!r}" if subject else f"action {named}"


@dataclass(frozen=True)
class SqlResult:
    """One statement's rows, as far as the caps allowed them.

    `row_count` counts the rows actually returned. Nothing here knows how many
    rows the statement would have produced: finding that out means running it to
    the end, which is the cost the caps exist to avoid.

    Frozen, but not hashable: `columns` and `rows` are lists, which is the shape
    `runtime_db.md` §4 fixes, and `hash()` of one raises as it would for a list.
    """

    columns: list[str]
    rows: list[list[Any]]
    truncated: bool

    @property
    def row_count(self) -> int:
        return len(self.rows)


def run_statement(
    db: Db,
    sql: str,
    params: Sequence[SqlValue] = (),
    *,
    authorizer: Authorizer,
    max_rows: int | None = None,
    max_bytes: int | None = None,
) -> SqlResult:
    """Run one statement under `authorizer` and return its rows.

    Raises `DbError` for everything: a refusal carries what was refused in
    `refusals`, and anything else carries SQLite's own message. `max_rows` and
    `max_bytes` are the caller's truncation policy; unset means the whole result.
    """
    if (max_rows is not None and max_rows < 0) or (max_bytes is not None and max_bytes < 0):
        raise WorldBug(f"caps must not be negative: max_rows={max_rows}, max_bytes={max_bytes}")

    authorizer.reset()
    tracer = _StatementTracer(authorizer.read_only)
    conn = db.conn
    cursor = conn.cursor()
    cursor.exec_trace = tracer
    # Both of these only read. Every change to the shared connection happens
    # inside the `try`, so there is no window in which the sandbox's authorizer or
    # its tighter limit is left behind on the instance's long-lived connection
    # with no `finally` to put it back.
    previous_authorizer = conn.authorizer
    previous_length = conn.limit(apsw.SQLITE_LIMIT_LENGTH)
    try:
        conn.limit(apsw.SQLITE_LIMIT_LENGTH, MAX_VALUE_BYTES)
        conn.authorizer = authorizer
        rows, truncated = _collect(cursor, sql, params, max_rows, max_bytes)
    except apsw.Error as error:
        raise _failure(error, authorizer, tracer) from error
    finally:
        # Restored first, so that nothing after this line can leave the sandbox's
        # authorizer or its tighter limit behind on the world's connection.
        conn.authorizer = previous_authorizer
        conn.limit(apsw.SQLITE_LIMIT_LENGTH, previous_length)
        # Truncation leaves the statement half-stepped. Retire it before the
        # caller's transaction tries to commit over the top of it.
        cursor.close(force=True)
    return SqlResult(columns=tracer.columns, rows=rows, truncated=truncated)


def _failure(error: apsw.Error, authorizer: Authorizer, tracer: _StatementTracer) -> DbError:
    """Say why a statement failed, in the sandbox's words rather than SQLite's."""
    message = str(error)
    code = getattr(error, "extendedresult", None)
    if tracer.abort_reason is not None:
        return DbError(message, code, (tracer.abort_reason,))
    if authorizer.refusals:
        return DbError(message, code, authorizer.refusals)
    if isinstance(error, apsw.TooBigError):
        # SQLITE_TOOBIG here can only be the cap this function just set, and a
        # result code is an API where the message text is not.
        return DbError(message, code, (f"value larger than {MAX_VALUE_BYTES} bytes",))
    return DbError(message, code)


class _StatementTracer:
    """Runs once per statement, before it steps.

    APSW's execution tracer is the only place three things are reachable at once:
    the column names (`get_description()` raises once execution has completed, so
    a query returning no rows has no other way to name its columns),
    `sqlite3_stmt_readonly`, and a veto -- returning False aborts before the
    statement does anything.
    """

    def __init__(self, read_only: bool) -> None:
        self._read_only = read_only
        self.columns: list[str] = []
        self.statements = 0
        self.abort_reason: str | None = None

    def __call__(self, cursor: apsw.Cursor, _sql: str, _bindings: Any) -> bool:
        self.statements += 1
        if self.statements > 1:
            # The payload carries more than one statement. Abort before this one
            # steps: whatever ran first was inside the caller's allowance, and
            # what follows a `;` is exactly where an injected write would sit.
            return self._abort("statement after the first in one call")
        if self._read_only and not cursor.is_readonly:
            return self._abort("write in a read-only query")
        self.columns = [name for name, _declared_type in cursor.get_description()]
        return True

    def _abort(self, reason: str) -> bool:
        self.abort_reason = reason
        return False


def _collect(
    cursor: apsw.Cursor,
    sql: str,
    params: Sequence[SqlValue],
    max_rows: int | None,
    max_bytes: int | None,
) -> tuple[list[list[Any]], bool]:
    """Read rows until a cap is reached. Returns them, and whether it stopped early."""
    rows: list[list[Any]] = []
    total_bytes = 0
    for values in cursor.execute(sql, params):
        if max_rows is not None and len(rows) >= max_rows:
            return rows, True
        row = [_jsonable(value) for value in values]
        if max_bytes is not None:
            # Measured as the UTF-8 the caller would actually send, and
            # unescaped (`ensure_ascii=False`), so that the same query truncates
            # at the same row whatever alphabet the fixture is written in.
            total_bytes += len(json.dumps(row, ensure_ascii=False).encode("utf-8"))
            if total_bytes > max_bytes:
                return rows, True
        rows.append(row)
    return rows, False


def _jsonable(value: Any) -> Any:
    """SQLite gives back None, int, float, str or bytes. Only bytes need help."""
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    return value
