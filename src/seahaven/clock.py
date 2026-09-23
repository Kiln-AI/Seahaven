"""An instance's clock, and the SQLite date and time functions that read it.

The clock has a mode, chosen when the instance is made and kept for its life:
`fixed` reads the start instant for ever, `tick` adds a second per call,
`running` adds the real time elapsed since the clock was made, and `wall` reads
the host's clock. Every reading is computed when it is asked for, so one `Clock`
object shared by reference is the single source of time for every door of an
instance.

Nothing inside a world may read the wall clock directly, and SQL is the door
that is easy to forget: `CURRENT_TIMESTAMP` and friends are parsed as
zero-argument calls into the same function table as `datetime()`, so overriding
that table covers every path, a `DEFAULT` clause and a trigger body included.

The overrides do not reimplement SQLite's date arithmetic. They substitute the
reading for the `'now'` time value and evaluate the *original* function on a
private helper connection that has no overrides, so modifiers, formats and corner
cases stay exactly SQLite's.
"""

import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, Self

import apsw

from seahaven.errors import WorldBug

if TYPE_CHECKING:  # names that live in apsw's stubs, not in the extension module
    from apsw import ScalarProtocol, SQLiteValue

__all__ = [
    "CANONICAL_FORMAT",
    "CLOCK_MODES",
    "DEFAULT_CLOCK_MODE",
    "TRACE_ID",
    "Clock",
    "ClockMode",
    "check_clock_mode",
    "register_clock_functions",
]

type ClockMode = Literal["fixed", "tick", "running", "wall"]
CLOCK_MODES: tuple[ClockMode, ...] = ("fixed", "tick", "running", "wall")
DEFAULT_CLOCK_MODE: ClockMode = "running"
TICK = timedelta(seconds=1)

# The `id` the per-statement trace is registered under. APSW keeps one trace per
# id on a connection, so another trace added under a different id leaves this one
# in place.
TRACE_ID = "seahaven.clock"


def check_clock_mode(value: object) -> ClockMode:
    """`value` if it names a clock mode, else a `WorldBug` listing the modes."""
    for mode in CLOCK_MODES:
        if value == mode:
            return mode
    names = ", ".join(repr(mode) for mode in CLOCK_MODES[:-1])
    raise WorldBug(
        f"unknown clock mode {value!r}; the clock modes are {names} and {CLOCK_MODES[-1]!r}"
    )


# The two sources of real time, and the only reads of it in this module. Names at
# module level so a test replaces them with `monkeypatch` rather than sleeping.
def _wall_now() -> datetime:
    return datetime.now(UTC)


_monotonic_ns = time.monotonic_ns


# The one timestamp format Seahaven writes and compares: `2024-03-05T12:00:00.123Z`.
# Spelled here as SQLite's format string and in `Clock.iso()` as Python's, which
# is a duplication `test_the_instant_is_rendered_canonically` is what holds
# together: it asserts that both doors render the same instant the same way.
CANONICAL_FORMAT = "%Y-%m-%dT%H:%M:%fZ"

# Registered on connections that have SQLITE_DBCONFIG_TRUSTED_SCHEMA off, where
# INNOCUOUS is what keeps them callable from a DEFAULT clause or a trigger.
_FUNCTION_FLAGS = apsw.SQLITE_INNOCUOUS | apsw.SQLITE_DETERMINISTIC

# SQLite accepts `now` only exactly, never padded, and neither does this: an
# override must not be more permissive than the function it replaces.
_NOW = re.compile(r"now", re.IGNORECASE)


@dataclass(frozen=True)
class _DateFunction:
    """How one override evaluates itself on the helper connection.

    `time_values` are the argument positions that hold a time value, the only
    ones `'now'` is substituted in: `strftime`'s first argument is a format
    string, and a format of `'now'` means the literal text. `omitted_at` is where
    an absent time value belongs (`datetime()` means `datetime('now')`), or
    `None` for a function that has no optional one. `prefix` is passed ahead of
    the caller's arguments.
    """

    sql_name: str
    time_values: frozenset[int]
    omitted_at: int | None
    prefix: tuple[str, ...] = ()


# `datetime` renders through `strftime` with the canonical format rather than
# through SQLite's `datetime`, whose `2024-03-05 12:00:00` sorts *below* every
# canonical timestamp of the same day (a space is below `T`) and carries no
# milliseconds. A world stores canonical text, so a clock value that did not use
# it would make `created_at > CURRENT_TIMESTAMP` nonsense. SQLite still does all
# the parsing and the arithmetic; only the rendering is ours. `date` and `time`
# are unambiguous already, and the rest return numbers or durations.
#
# The one place the canonical rendering says more than it knows is the
# `'localtime'` modifier: `datetime('now', 'localtime')` is the instant shifted
# into the host's zone, and a `Z` is then stamped on a value that is not UTC.
# Plain SQLite is already host-dependent there (it reads the process timezone,
# which no fixture pins), so the modifier has no place in a world's SQL at all;
# what the canonical rendering adds is that the value now also *claims* to be
# UTC and will sort against stored timestamps as though it were.
# `test_localtime_is_not_the_instant` pins the behaviour, and a later phase's
# lint is where a world using it should be told so.
_FUNCTIONS = {
    "date": _DateFunction("date", frozenset({0}), 0),
    "time": _DateFunction("time", frozenset({0}), 0),
    "datetime": _DateFunction("strftime", frozenset({0}), 0, (CANONICAL_FORMAT,)),
    "julianday": _DateFunction("julianday", frozenset({0}), 0),
    "unixepoch": _DateFunction("unixepoch", frozenset({0}), 0),
    "timediff": _DateFunction("timediff", frozenset({0, 1}), None),
    "strftime": _DateFunction("strftime", frozenset({1}), 1),
}

# The zero-argument keywords, as the expression each evaluates to on the instant.
_CONSTANTS = {
    "current_timestamp": f"strftime('{CANONICAL_FORMAT}', ?)",
    "current_date": "date(?)",
    "current_time": "time(?)",
}


class Clock:
    """An instance's clock, which world code reads through `ctx.clock`.

    The framework makes one when it creates an instance, in the instance's mode,
    and every connection and context of the instance reads that one object.
    `Clock(datetime)` built directly is a `fixed` clock at that instant, which is
    also the way to render a datetime as canonical text. Building a `Clock` does
    not choose or change any instance's mode: that is the `clock_mode` reset
    option, and `World(default_clock_mode=...)` for the world's default.
    """

    def __init__(self, now: datetime, mode: ClockMode = "fixed") -> None:
        self._start = _truncated(now)
        self._mode = check_clock_mode(mode)
        self._calls = 0
        self._origin_ns = _monotonic_ns()

    @classmethod
    def from_iso(cls, text: str, mode: ClockMode = "fixed") -> Self:
        """Parse a timestamp, canonical or any other ISO 8601 form Python reads."""
        try:
            instant = datetime.fromisoformat(text)
        except ValueError as error:
            raise WorldBug(f"not a timestamp: {text!r}") from error
        return cls(instant, mode)

    @classmethod
    def wall(cls) -> Self:
        """A `fixed` clock at the wall clock's current time."""
        return cls(_wall_now())

    @property
    def mode(self) -> ClockMode:
        return self._mode

    def now(self) -> datetime:
        """The current reading. Reading the clock never moves it."""
        match self._mode:
            case "fixed":
                return self._start
            case "tick":
                return self._start + self._calls * TICK
            case "running":
                elapsed_ms = (_monotonic_ns() - self._origin_ns) // 1_000_000
                return self._start + timedelta(milliseconds=elapsed_ms)
            case "wall":
                return _truncated(_wall_now())

    def iso(self) -> str:
        """The current reading as canonical text: what world code writes to the database."""
        now = self.now()
        return f"{now:%Y-%m-%dT%H:%M:%S}.{now.microsecond // 1000:03d}Z"

    def _call_started(self) -> None:
        """Count a call. The instance calls this as a call takes its ordinal."""
        self._calls += 1

    def __repr__(self) -> str:
        return f"Clock({self.iso()}, {self._mode})"


def _truncated(instant: datetime) -> datetime:
    """`instant` in UTC, cut to the millisecond precision `iso()` renders and SQL sees.

    Microseconds a clock cannot show would make two instants that are identical
    at every door of the framework compare unequal.
    """
    if instant.tzinfo is None:
        raise WorldBug("a clock instant must be timezone-aware")
    utc = instant.astimezone(UTC)
    return utc.replace(microsecond=utc.microsecond // 1000 * 1000)


class _StatementReading:
    """One clock reading per top-level SQL statement on a connection.

    SQLite evaluates `CURRENT_TIMESTAMP` once per use, so a statement with two
    such defaults, or one whose trigger stamps a row, would otherwise take several
    readings of a moving clock. SQLite promises its own functions one instant per
    statement, and the overrides keep that promise: the reading is taken at the
    first date function a statement evaluates and dropped when the next statement
    starts. A trigger program runs inside its statement and shares its reading.
    """

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._iso: str | None = None

    def iso(self) -> str:
        if self._iso is None:
            self._iso = self._clock.iso()
        return self._iso

    def statement_started(self, event: dict[str, Any]) -> None:
        if not event["trigger"]:
            self._iso = None


def register_clock_functions(conn: apsw.Connection, clock: Clock) -> apsw.Connection:
    """Point every SQLite date and time function on `conn` at `clock`.

    Returns the private helper connection the overrides evaluate on. The caller
    owns it and closes it with the connection it serves.
    """
    helper = apsw.Connection(":memory:")
    reading = _StatementReading(clock)
    # A connection-level trace rather than `Connection.exec_trace`: the sandbox
    # sets a cursor's `exec_trace`, which APSW calls *instead of* the
    # connection's, while `trace_v2` fires for every statement either way.
    conn.trace_v2(apsw.SQLITE_TRACE_STMT, reading.statement_started, id=TRACE_ID)

    for name, function in _FUNCTIONS.items():
        conn.create_scalar_function(
            name, _override(helper, function, reading), -1, flags=_FUNCTION_FLAGS
        )
    for name, expression in _CONSTANTS.items():
        conn.create_scalar_function(
            name, _constant(helper, expression, reading), 0, flags=_FUNCTION_FLAGS
        )

    return helper


def _constant(
    helper: apsw.Connection, expression: str, reading: _StatementReading
) -> ScalarProtocol:
    """A zero-argument keyword, evaluated once per distinct reading.

    A statement that uses one on every row takes one reading, so the helper query
    runs once for it rather than once a row.
    """
    cached: tuple[str, SQLiteValue] | None = None

    def evaluate(*_args: SQLiteValue) -> SQLiteValue:
        nonlocal cached
        instant = reading.iso()
        if cached is None or cached[0] != instant:
            cached = (instant, helper.execute(f"SELECT {expression}", (instant,)).get)
        return cached[1]

    return evaluate


def _override(
    helper: apsw.Connection, function: _DateFunction, reading: _StatementReading
) -> ScalarProtocol:
    """Wrap SQLite's own function so that its time value resolves to the statement's reading."""

    def is_now(position: int, value: SQLiteValue) -> bool:
        return (
            position in function.time_values
            and isinstance(value, str)
            and _NOW.fullmatch(value) is not None
        )

    def evaluate(*args: SQLiteValue) -> SQLiteValue:
        instant = reading.iso()
        resolved: list[SQLiteValue] = [
            instant if is_now(position, value) else value for position, value in enumerate(args)
        ]
        if function.omitted_at is not None and len(resolved) <= function.omitted_at:
            resolved.append(instant)
        arguments = [*function.prefix, *resolved]
        placeholders = ", ".join("?" * len(arguments))
        return helper.execute(f"SELECT {function.sql_name}({placeholders})", arguments).get

    return evaluate
