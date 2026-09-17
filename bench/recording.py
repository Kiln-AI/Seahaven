"""What the change log costs a call, against the long-lived session it replaced.

`architecture.md` §16 asks for this number. Until this release an instance kept a
single `apsw.Session` for its whole life, and `Instance.changes()` read it out
when an eval asked; now every call opens a session of its own, attaches each
tracked table to it, reads its changeset when the call's transaction is done, and
renders that into the log records the state document carries.

The alternative that was rejected -- keeping the long-lived session and diffing
it per call -- is O(everything the episode changed) per call rather than O(what
the call changed), which is the cost the change log exists to keep off a harness.
What this measures is what the shape that was chosen costs instead.

Three legs per workload, on one instance, because two would say how much and not
where:

1. **A session per call**, as Seahaven runs it.
2. **A session per call, never read**, opened and attached and closed with
   nothing taken out of it. The difference from the leg above is `changeset()`
   and `render_log`: the work that moved from an eval's `changes()` call, once an
   episode, to every call.
3. **One long-lived session**, opened once for the whole pass and never read:
   the shape the framework had before this release. The difference from the leg
   above is everything a *fresh* session does that a warmed-up one does not --
   opening it, attaching its tables, its first sighting of each table it records
   (the `PRAGMA table_xinfo` `sandbox.py`'s authorizer has to allow), and freeing
   a populated change buffer at `close()`. Which of those dominates depends on
   how much the call wrote, so the two workloads answer differently. This leg is
   the noisiest of the three and should not be read to a point.

Legs 2 and 3 are not configurations Seahaven offers. They exist to split the
number in leg 1.

Both workloads, because they answer differently: the write mix pays all of it,
and a read changes no rows, so its session has nothing to render and nothing to
free.
"""

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass

from bench.harness import Run, closed_loop, summarise
from bench.runner import calls_per_worker, sessions
from bench.workloads import WORKLOADS
from seahaven import Instance, World
from seahaven.changes import open_session

__all__ = [
    "HasRecorded",
    "Recorder",
    "Recording",
    "long_lived_session",
    "recording",
    "unread_sessions",
]

# What `Instance._recording` is: a context manager over one call's writes, taking
# that call's ordinal.
type Recorder = Callable[[int | None], AbstractContextManager[None]]

# What a comparison leg reports to a test: whether its session has a changeset to
# give. `sqlite3session_isempty` and not the changeset's size, which SQLite only
# tracks when a session is configured to -- configuring one would put work into
# the leg that the leg is there to leave out. Read in tens of nanoseconds,
# against a call of hundreds of microseconds, so keeping it up to date per call
# measures nothing.
type HasRecorded = Callable[[], bool]


@dataclass(frozen=True)
class Recording:
    """One workload, one instance, recorded three ways. Seconds are per call."""

    workload: str
    calls: int
    per_call_seconds: float
    unread_seconds: float
    long_lived_seconds: float
    # Whether any call of the pass gave its session something to record, asked of
    # the sessions rather than assumed from the workload's name. A workload that
    # writes nothing has nothing to render and nothing to free, so the split
    # between its legs is noise and the report says so instead of reading it.
    wrote_rows: bool

    @property
    def overhead(self) -> float:
        """What the change log adds to a call, against one long-lived session."""
        return self._share(self.per_call_seconds - self.long_lived_seconds)

    @property
    def rendering(self) -> float:
        """The part of that which is `changeset()` and `render_log`."""
        return self._share(self.per_call_seconds - self.unread_seconds)

    def _share(self, seconds: float) -> float:
        """One leg's difference from the long-lived leg, as a fraction of that leg."""
        if not self.long_lived_seconds:
            return 0.0
        return seconds / self.long_lived_seconds


def recording(world: World, *, workload: str, calls: int, repeats: int) -> Recording:
    """Time one workload down all three legs, on one instance.

    The legs alternate rather than running as three blocks. An instance's write
    cost climbs slowly as its tables grow, and three blocks would hand that climb
    to whichever leg ran last, which is the same size of effect this is trying to
    measure.
    """
    driven = WORKLOADS[workload]
    per_pass = calls_per_worker(driven, calls)
    per_call: list[Run] = []
    unread: list[Run] = []
    long_lived: list[Run] = []
    wrote_rows = False
    with sessions(world, driven, 1) as (only,):
        for index in range(per_pass):  # warm the path before any leg is timed
            only.caller(index)
        for _ in range(repeats):
            per_call.append(closed_loop([only.caller], per_pass))
            with unread_sessions(only.instance) as has_recorded:
                unread.append(closed_loop([only.caller], per_pass))
                # After the pass, so reading it is no part of what was timed.
                wrote_rows = wrote_rows or has_recorded()
            with long_lived_session(only.instance):
                long_lived.append(closed_loop([only.caller], per_pass))
    return Recording(
        workload=driven.name,
        calls=per_pass * repeats,
        per_call_seconds=_median_seconds_per_call(per_call),
        unread_seconds=_median_seconds_per_call(unread),
        long_lived_seconds=_median_seconds_per_call(long_lived),
        wrote_rows=wrote_rows,
    )


@contextmanager
def unread_sessions(instance: Instance) -> Iterator[HasRecorded]:
    """A session per call, opened and attached and closed, with nothing read out of it.

    Yields whether any call's session has had anything to give, asked of each
    just before it was closed: what the leg records has to be observable, or a
    leg that quietly stopped opening a session at all would still measure
    something and still look right.
    """
    recorded = [False]

    @contextmanager
    def opened(i: int | None) -> Iterator[None]:
        session = open_session(instance.db.conn, instance._tracked)
        try:
            yield
        finally:
            recorded[0] = recorded[0] or not session.is_empty
            session.close()

    with _recorded_by(instance, opened):
        yield lambda: recorded[0]


@contextmanager
def long_lived_session(instance: Instance) -> Iterator[HasRecorded]:
    """Record this instance's writes the way Seahaven did before the change log.

    One session for the length of the block, attached to the tables every
    per-call session attaches, and nothing read out of it -- which is also how
    the long-lived session behaved between calls: it was rendered when an eval
    asked for it, not once per call. Yields whether it has anything to give, for
    the reason above.
    """
    session = open_session(instance.db.conn, instance._tracked)
    try:
        with _recorded_by(instance, _nothing):
            yield lambda: not session.is_empty
    finally:
        session.close()


@contextmanager
def _recorded_by(instance: Instance, recorder: Recorder) -> Iterator[None]:
    """Run this instance's calls through another recorder for the length of the block.

    Shadowed on the instance and not on the class, so the measurement cannot
    escape into another instance, and deleted afterwards so the method the class
    defines is what answers again.
    """
    instance._recording = recorder  # ty: ignore[invalid-assignment]
    try:
        yield
    finally:
        del instance._recording


@contextmanager
def _nothing(i: int | None) -> Iterator[None]:
    yield


def _median_seconds_per_call(runs: list[Run]) -> float:
    return 1 / summarise(runs).rate_median
