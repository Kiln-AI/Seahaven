"""A live world: a private copy of a fixture, and everything a call into it needs.

An instance is a file in a working directory, a connection on it, a frozen clock,
a seeded id stream, a change log and a lock. `world.instance(...)` makes one,
`inst.call(...)` runs a tool on it, and `inst.destroy()` (or leaving its
`with` block) takes the files away again. Nothing is shared between two
instances: two instances of one fixture are two copies of one file.

Three rules hold the concurrency together.

*One lock per instance.* `call`, `change_log`, `state`, `freeze`, `bulk`, `destroy`
and the opens inside `inspect()` and `_control_db()` take it, so calls into one instance
serialise and a destroy waits for the call in flight. Reads through the `inspect()` handle
afterwards do not take it: that handle is the caller's, to read from whatever
thread it likes. The lock is an `RLock` because a control tool is called with it
already held and then asks the instance for something -- its control handle --
that takes it again on the same thread.

*The gate before the lock.* The concurrency gate bounds how many tool calls run
at once across the process. It is taken before the instance lock, so a call
queued behind it holds nothing and can never delay a `destroy` or a `freeze`.

*The manager's lock is never held while an instance lock is.* The registry is
touched only in short moments that take nothing else.
"""

import atexit
import errno
import logging
import os
import shutil
import stat
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, closing, contextmanager, suppress
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self

from seahaven.call import Call, serialise
from seahaven.changes import LogRecord, open_session, render_log, tracked_tables
from seahaven.clock import Clock
from seahaven.ctx import Ctx, InstanceInfo
from seahaven.db import Db, build_blank, open_inspection, open_instance
from seahaven.errors import ToolError, UnknownTool, WorldBug
from seahaven.fixtures import STATE_NAME, Fixture, check_id, freeze, load, verify
from seahaven.ids import CONTROL_STREAM, INSPECTION_STREAM, Ids, instance_seed
from seahaven.state import Formatter, document

if TYPE_CHECKING:  # `world.py` imports this module; the annotation is all that is needed here
    from seahaven.world import World

__all__ = [
    "WORK_DIR_PREFIX",
    "Instance",
    "InstanceManager",
    "concurrency",
    "default_concurrency",
    "gate",
    "set_concurrency",
]

# The start of the working root's name, not the whole of it: the root is
# `<tempdir>/seahaven-<uid>/` and a working directory is
# `<tempdir>/seahaven-<uid>/<pid namespace>-<pid>/<world>/`. See
# `_default_work_root` for why the root is per user and `_process_dirname` for
# why the pid carries a namespace.
WORK_DIR_PREFIX = "seahaven"

# The default working root is POSIX: it is named after a user id, and it is made
# safe with `O_NOFOLLOW`, `fchmod` and `mkdirat`. Where any of that is missing
# there is no default working directory at all and `World(work_dir=...)` is the
# way to name one -- a refusal that says so, rather than an `AttributeError` or a
# `NotImplementedError` out of `world.instance()`.
_POSIX_WORK_ROOT = (
    hasattr(os, "getuid")
    and hasattr(os, "O_NOFOLLOW")
    and hasattr(os, "fchmod")
    and os.mkdir in os.supports_dir_fd
)

_log = logging.getLogger(__name__)


def default_concurrency() -> int:
    """How many tool calls run at once unless an operator says otherwise.

    `process_cpu_count` follows a container's CPU affinity rather than the host's
    core count, and is `None` on a platform that cannot say. The cap keeps a very
    large host from over-subscribing.

    **This is not the throughput optimum, and it was never measured to be.** The
    sweep in `bench/results/latest.md` found no optimum above 1 on a build with
    the GIL: most of a call is Python, so a second runnable thread buys contention
    rather than parallelism, and `n = 1` ran 22% to 37% more calls a second than
    this default on every workload, cache state and offered load measured.

    **Nor is it fair.** The same sweep found that a gate starves a waiting caller
    whenever it binds -- at this size exactly as at any other, because the cause is
    the semaphore and not the number (`BACKLOG.md` B20).

    One measured reason is left: no value the sweep tried was better than this one
    on every axis at once, everywhere it was measured. (`n = 1` is better on every
    axis at 32 sessions; at four, and with one slow call in the process, it is the
    value that waits worst.) Following cpu affinity is *not* a measured advantage --
    this build has the GIL, and the sweep says nothing about what a free-threaded
    one would do -- it is the shape that keeps that door open, which is a design
    intent and is recorded here as one. An operator with a measurement of their own
    world should reach for `serve --concurrency`.
    """
    return min(os.process_cpu_count() or 4, 16)


class _Gate(threading.BoundedSemaphore):
    """The process-wide gate: a bounded semaphore that publishes its own size.

    `BoundedSemaphore` records the value it was built with and offers no way to
    read it, so `concurrency()` first kept the size in a module-level variable
    beside the gate. Two variables holding one fact is a fact that can drift:
    `set_concurrency` wrote both, and anything else that put a gate in place --
    a test substituting an instrumented one, say -- moved one and left the
    other. This class does not copy the size, it *derives* it: `size` reads the
    bound the semaphore itself is holding, so there is no second value to keep
    in step and none to assign -- a read-only property cannot go stale and
    `gate.size = 99` raises.

    The cost is one standard-library private, read in one place. That is the
    trade the class exists to make: the alternative was every caller reaching
    for `_gate._initial_value`, which is what `concurrency()` was added to stop.
    `ty` needs telling because typeshed does not declare the attribute, not
    because it is absent -- `BoundedSemaphore.__init__` has set it since 3.3 --
    and if a future CPython renames it, `concurrency()` raises `AttributeError`
    and the suite says so in nine tests rather than reporting a wrong size.
    """

    @property
    def size(self) -> int:
        return self._initial_value  # ty: ignore[unresolved-attribute]


# Enhancement: a FIFO gate hands slots out in arrival order; this one starves (`BACKLOG.md` B20).
_gate: _Gate | None = _Gate(default_concurrency())


def set_concurrency(size: int) -> None:
    """Resize the process-wide gate. `0` removes it; this is `serve --concurrency`.

    Calls already running are unaffected: each releases the gate it took.
    """
    global _gate
    if size < 0:
        raise WorldBug(f"concurrency must not be negative: {size}")
    _gate = _Gate(size) if size else None


def concurrency() -> int:
    """The gate's size as it was last set, or `0` when there is no gate.

    The counterpart of `set_concurrency`, kept because there was no way to read
    the size back: a caller that wanted it had to reach for
    `instances._gate._initial_value`, which is one module's private name and one
    standard-library class's private attribute in a single expression. `_Gate`
    publishes that size as a read-only property derived from the bound the
    semaphore is holding, so this answers the gate that is actually in place
    rather than a copy of it that a resize has to remember to update.
    """
    # Read once, as `gate()` does: a `set_concurrency` between the test and the
    # attribute must not turn this into an `AttributeError` on `None`.
    current = _gate
    return current.size if current is not None else 0


@contextmanager
def gate(*, bypass: bool = False) -> Iterator[None]:
    """Hold one of the gate's slots for the block, queueing for it if need be.

    Nothing is ever rejected: the gate bounds how many calls execute at once, not
    how many are admitted.
    """
    # Read once: a `set_concurrency` between the acquire and the release must not
    # let this call release a slot on a semaphore it never took.
    semaphore = _gate
    if bypass or semaphore is None:
        yield
        return
    semaphore.acquire()
    try:
        yield
    finally:
        semaphore.release()


class Instance:
    """One live world instance. Made by `world.instance(...)`, never by hand."""

    def __init__(
        self,
        *,
        id: str,
        fixture: str | None,
        clock: Clock,
        ctx: Ctx,
        db: Db,
        tracked: tuple[str, ...],
        dir: Path,
        world: World,
        manager: InstanceManager,
        state_format: str,
        formatter: Formatter,
        episode_id: str,
        caller_seed: int | None,
        fixture_sha256: str | None,
        startup: dict[str, Any],
    ) -> None:
        self.id = id
        self.fixture = fixture
        self.clock = clock
        # The derived instance seed -- what actually drove `ctx.ids` -- and not
        # the `seed=` the caller passed, which is one of its two inputs.
        self.seed = ctx.instance.seed
        # The `seed=` the caller gave, which is the one a state document reports:
        # `self.seed` above is what it was hashed into.
        self.caller_seed = caller_seed
        self.ctx = ctx
        self.db = db
        self.dir = dir
        self.world = world
        # Over OpenEnv the session's episode id, and in process the instance id:
        # in process an instance is an episode (`functional_spec.md` §3.1).
        self.episode_id = episode_id
        # The fixture's identity, from its sidecar; `None` for a blank instance.
        # With the world's name and version, this is a reader's lookup for the
        # state the episode started from.
        self.fixture_sha256 = fixture_sha256
        # The reset keywords beyond `fixture`, `seed`, `now` and `state_format`,
        # already JSON-able: serialised at creation so a keyword that could never
        # reach a document is refused there rather than at `state()` time.
        self.startup = startup
        # The format this instance answers in, fixed for its life, and the
        # formatter it resolved to when the instance was made.
        self.state_format = state_format
        self._formatter = formatter
        # The thread running a formatter on this instance, or `None`. A thread
        # id rather than a flag because `_call` reads it without the lock: only
        # the formatting thread itself is refused, so a call from another thread
        # is never caught by a formatter it has nothing to do with.
        self._formatting: int | None = None
        self.closed = False
        # Re-entrant: a control tool holds this lock and then asks the instance
        # for its control handle, which takes it again.
        self.lock = threading.RLock()
        self._manager = manager
        self._inspection: Db | None = None
        self._control: Db | None = None
        # The tables every per-call session attaches, settled once at creation:
        # a world's schema does not change while an instance is alive.
        self._tracked = tracked
        # The change log, appended to in commit order as each call's recording
        # ends, and the per-table column cache `render_log` fills as it goes.
        self._records: list[LogRecord] = []
        self._columns: dict[str, tuple[list[str], list[int]]] = {}
        self._call_count = 0

    @property
    def state_path(self) -> Path:
        """The instance's own database file."""
        return self.dir / STATE_NAME

    @property
    def call_count(self) -> int:
        """How many calls have been dispatched to this instance.

        Every world tool `call` reached, including one that raised and one whose
        name the world does not have; never a control tool and never `tools()`.
        The last call's ordinal is one less than this.
        """
        return self._call_count

    def call(self, name: str, /, **arguments: Any) -> Any:
        """Run one tool, with its arguments validated, on the calling thread.

        Raises the world's `ToolError` subclasses to the caller; over OpenEnv the
        same error is rendered onto the observation instead.
        """
        started = time.perf_counter()
        try:
            result = self._call(name, arguments)
        except ToolError as error:
            self._log_call(name, started, error.code)
            raise
        except BaseException as error:
            # Not a failure with a code of its own; the class name is what there
            # is to say about it, and `invoke` has already logged the traceback.
            self._log_call(name, started, type(error).__name__)
            raise
        self._log_call(name, started, "ok")
        return result

    def tools(self) -> list[dict[str, Any]]:
        """The tool list, with JSON schemas. Control tools are never in it."""
        return [tool.listing() for tool in self.world.tools.values() if not tool.control]

    def inspect(self) -> Db:
        """A read-only handle on this instance, opened once and kept.

        Every table, the instance's clock, no authorizer beyond the connection's
        permanent write denial. Reads through it do not take the instance lock: a
        read-only connection on a WAL database sees a consistent snapshot per
        statement. Reading through it concurrently with `destroy()` is the one
        ordering the caller owns.
        """
        with self._held():
            if self._inspection is None:
                self._inspection = open_inspection(
                    self.state_path, self.clock, self.ctx.instance.seed, INSPECTION_STREAM
                )
            return self._inspection

    def change_log(self) -> list[LogRecord]:
        """Every row this instance has changed, one record per row per call, in call order.

        Costs no database work: each call's records were rendered when that call
        committed, and this hands back what is already in memory.

        The list is the caller's, but the records in it are the instance's: a
        `LogRecord` is frozen and its `key`, `before` and `after` dicts are the
        ones the log holds, handed out rather than copied because copying every
        record on every read would cost an episode's worth of dicts per call.
        Read them; editing one in place edits the log itself.
        """
        with self._held():
            return list(self._records)

    def state(self, format: str | None = None) -> dict[str, Any]:
        """The state document: this instance's provenance, and `state` from its format.

        A plain dict, JSON-serialisable with the standard library, so a caller
        saves an episode with `json.dump` and nothing else. It costs
        serialisation only: the change log is in memory and was rendered as each
        call committed, so this does no database work.

        `format` answers in another of this world's registered formats instead,
        for the same instance. It is in-process only -- the OpenEnv `state`
        message carries no arguments -- and the instance's own format is
        unaffected.

        Refused inside a transaction -- `bulk()`, or a tool call -- where the
        rows written are not committed and no document could describe them.
        """
        with self._held():
            if self.db.in_transaction:
                # The lock is an `RLock`, so a `state()` inside `bulk()` gets this
                # far and would then build a document from a transaction that has
                # not committed: the log would be missing the rows the same block
                # can already read through `ctx.db`, with nothing to say so. A
                # formatter never runs in a transaction (`functional_spec.md` §6).
                raise WorldBug(
                    "state cannot run inside a transaction: a formatter reads what the instance "
                    "holds, and inside bulk() or a tool call the rows written are not committed "
                    "yet. Read it after the block or the call returns"
                )
            name = self.state_format if format is None else format
            formatter = (
                self._formatter if format is None else self.world.resolve_state_format(format)
            )
            # Saved and restored rather than cleared: a formatter may read
            # `instance.state(format=...)` to build a variation of a built-in
            # (`functional_spec.md` §6), and the inner read must not leave the
            # outer one unguarded.
            formatting = self._formatting
            self._formatting = threading.get_ident()
            try:
                return document(self.world, self, name, formatter)
            finally:
                self._formatting = formatting

    def freeze(self, id: str, description: str) -> Fixture:
        """Mint a fixture from this instance's current state."""
        with self._held():
            if self.db.in_transaction:
                # The lock is an `RLock`, so a `freeze` inside `bulk()` gets this
                # far and then reaches a `VACUUM` SQLite will not run inside a
                # transaction. Said here instead, where what to do about it is
                # obvious: the rows are not committed yet, and a fixture of
                # uncommitted rows is not what the caller asked for either.
                raise WorldBug(
                    "freeze cannot run inside bulk(): leave the bulk() block first, so the "
                    "rows it wrote are committed and the fixture is what the instance holds"
                )
            return freeze(self, id, description, fixtures_dir=self.world.fixtures_dir)

    def bulk(self) -> AbstractContextManager[Ctx]:
        """Write straight into the instance, under its lock and one transaction.

        The authoring path: a tool call per row would spend its time on argument
        validation and transaction boundaries for tens of thousands of rows. What
        is yielded is the instance's own context, with no call attached; nothing
        is disabled and nothing is wrapped. Startup hooks do not run again.
        """
        return self._bulk()

    def destroy(self) -> None:
        """Close everything and remove the working directory. Idempotent.

        Takes the lock, so a call in flight finishes first.
        """
        # Unregistered first, and outside the instance lock: the manager's lock is
        # never taken while an instance lock is held.
        self._manager.unregister(self)
        with self.lock:
            if not self.closed:
                self.closed = True
                self._close()
                _log.info("destroyed instance %s of world %s", self.id, self.world.name)
        shutil.rmtree(self.dir, ignore_errors=True)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exception: object) -> None:
        self.destroy()

    def __repr__(self) -> str:
        return f"<Instance {self.id} of {self.world.name} from {self.fixture or 'blank'}>"

    def _call(self, name: str, arguments: Mapping[str, Any]) -> Any:
        world = self.world
        # Looked up before the gate because whether this is a control tool is
        # what decides the gate is bypassed; whether it *exists* is answered
        # under the lock below, where the ordinal it consumes is issued.
        tool = world.tools.get(name)
        # Before the gate, not after it: a formatter reaching this holds the
        # instance lock, and queueing for a slot whose holders may be waiting on
        # that lock would hang the process rather than raise.
        self._refuse_if_formatting()
        # The gate first and the lock second, so a queued call holds nothing.
        with gate(bypass=tool is not None and tool.control), self._held():
            if tool is None:
                # An agent naming a tool that does not exist reads the answer: it
                # is a tool error, not a framework one. It is still a call, so it
                # consumes its ordinal before it is refused, and the harness's
                # Nth call is this instance's Nth (functional_spec.md §7).
                self._next_ordinal()
                raise UnknownTool(name)
            call = Call(name, arguments, tool)
            ctx = self.ctx.with_call(call)
            if tool.control:
                # Imported here, not at the top: `control.py` is written against
                # `Instance`, so the dependency runs that way round and this is
                # the one place the call path needs it back. Not a call to the
                # world: no ordinal, and nothing recorded.
                from seahaven import control

                return control.dispatch(self, ctx)
            # The chain is read from the world here rather than held, so a
            # middleware registered after this instance was made applies to it.
            with self._recording(self._next_ordinal()):
                return world.chain(ctx, call)

    def _next_ordinal(self) -> int:
        """The ordinal of the call about to run, counting from 0. The lock is held."""
        self._call_count += 1
        return self._call_count - 1

    @contextmanager
    def _recording(self, i: int | None) -> Iterator[None]:
        """Log every row the block changes, as one call's worth of records.

        A session per call rather than per-call deltas off one long-lived
        session: diffing consecutive cumulative changesets is O(everything the
        episode changed) per call, which is the cost the change log exists to
        keep off a harness. This is O(what the call changed).
        """
        session = open_session(self.db.conn, self._tracked)
        try:
            yield
        finally:
            # After the block's transaction has committed or rolled back and
            # before any other write: `changeset()` joins what the session
            # recorded against the live table, so a rolled-back row, or one
            # written back to its original values, contributes nothing.
            with closing(session):
                changeset = session.changeset()
            if changeset:
                self._records.extend(render_log(changeset, self.db.conn, self._columns, i=i))

    def _control_db(self) -> Db:
        """A second read-only handle, opened once, for the control tools alone.

        Not the `inspect()` handle, though it is opened the same way. A control
        read runs through `sandbox.run_statement`, which sets the connection's
        authorizer and its value limit for the length of one statement; the
        `inspect()` handle is the one a caller reads through *without* taking the
        instance lock. Two threads on one connection, one of them changing its
        authorizer while the other steps a cursor, is a deadlock inside SQLite --
        and one that takes the interpreter with it, because the thread waiting on
        the connection is holding the GIL.

        Reached only from the control tools, which `Instance.call` runs with the
        instance lock held, so this connection has one statement on it at a time
        and the state `run_statement` borrows is state nobody else can see.
        """
        with self._held():
            if self._control is None:
                self._control = open_inspection(
                    self.state_path, self.clock, self.ctx.instance.seed, CONTROL_STREAM
                )
            return self._control

    @contextmanager
    def _bulk(self) -> Iterator[Ctx]:
        with self._held():
            self._refuse_if_formatting()
            # `i=None`: authoring writes happen after creation and count, but
            # there is no call in flight for them to belong to.
            with self._recording(None), self.db.transaction():
                yield self.ctx

    def _refuse_if_formatting(self) -> None:
        """Refuse a write from inside a formatter.

        A formatter runs with the instance lock held, and the lock is an `RLock`,
        so a formatter calling `inst.call` or `inst.bulk` on its own instance
        would be let straight through to write. It lands here instead.

        The test is against *this* thread: `_call` asks before taking the gate,
        where the instance lock is not held, and another thread's legitimate call
        must not be refused because this instance happens to be formatting
        somewhere else.
        """
        if self._formatting == threading.get_ident():
            raise WorldBug("a state formatter reads an instance and never writes to it")

    @contextmanager
    def _held(self) -> Iterator[None]:
        """Hold the lock, refusing an instance `destroy` got to first.

        Everything that touches the instance goes through here, so a caller
        either wins the race with `destroy` or is told the instance is gone --
        never half of each.
        """
        with self.lock:
            if self.closed:
                raise WorldBug(f"instance {self.id} has been destroyed")
            yield

    def _close(self) -> None:
        """Release every handle the instance holds.

        No session outlives a call -- each one is opened and closed inside
        `_recording` -- so what is left here are the connections. Both read-only
        handles, the caller's from `inspect()` and the control tool's own, may
        never have been opened.
        """
        for handle in (self._inspection, self._control):
            if handle is not None:
                handle.close()
        self._inspection = None
        self._control = None
        self.db.close()

    def _log_call(self, name: str, started: float, outcome: str) -> None:
        """The one operational line per call: which instance, which tool, how long, how it went.

        The duration is wall time from entry, so a call that queued behind the
        gate reports the latency its caller saw.
        """
        _log.info(
            "call %s on instance %s of world %s: %s in %.1f ms",
            name,
            self.id,
            self.world.name,
            outcome,
            (time.perf_counter() - started) * 1000,
        )


class InstanceManager:
    """Every live instance of one world, in this process. Created lazily by `World`."""

    def __init__(self, world: World) -> None:
        self._world = world
        self._lock = threading.Lock()
        self._instances: dict[str, Instance] = {}
        # One manager is made per world, and only when that world is about to
        # have its first instance, so this registers once: a process that exits
        # without destroying its instances still takes their files with it.
        #
        # Nothing unregisters it, and that is deliberate. The registration is
        # what makes the cleanup unconditional -- it must survive a caller
        # dropping every reference to the world while its instances are still on
        # disk, which is exactly the case that needs it -- and there is no point
        # in a manager's life at which it is known to be finished: `close()` is
        # idempotent and a closed manager can still make instances. The bound is
        # one entry per world that has ever made an instance, which is a handful
        # of objects for the life of a process.
        atexit.register(self.close)

    def create(
        self,
        fixture_id: str | None = None,
        *,
        seed: int | None = None,
        now: str | datetime | None = None,
        state_format: str | None = None,
        episode_id: str | None = None,
        startup_kwargs: Mapping[str, Any] | None = None,
    ) -> Instance:
        """Materialise an instance from a fixture, or from the world's DDL."""
        world = self._world
        kwargs = startup_kwargs or {}
        # Everything that can be refused is refused here, before a directory
        # exists: an unknown startup argument, a startup value a state document
        # could not carry, a format nothing registered, an id that is not an id,
        # a fixture that is missing, modified or frozen from another schema, and
        # `now=` where the fixture already carries the clock. A creation that
        # cannot succeed copies nothing and leaves nothing behind.
        _check_startup_kwargs(world, kwargs)
        # The hooks below still receive the raw values; this is the copy the
        # document reports, rendered now so that a keyword it could never carry
        # is a refusal at creation rather than at the end of an episode.
        startup: dict[str, Any] = {}
        for keyword, value in kwargs.items():
            try:
                startup[keyword] = serialise(value)
            except WorldBug as error:
                # `serialise` speaks about tool results, which is what it is for
                # everywhere else; this is the one caller that is not one, and it
                # is the one that can say which keyword was the problem.
                raise WorldBug(
                    f"startup keyword {keyword!r} must be JSON-able data, because the state "
                    f"document reports it: {error}"
                ) from error
        format_name = state_format if state_format is not None else world.pinned_state_format
        formatter = world.resolve_state_format(format_name)
        fixture = self._fixture(fixture_id, now) if fixture_id is not None else None
        _sweep_once(self)

        instance_id = str(uuid.uuid4())
        directory = self._make_instance_dir(instance_id)
        state = directory / STATE_NAME
        db: Db | None = None
        try:
            if fixture is None:
                build_blank(state, world.schema).close()
                clock = _clock_from(now) if now is not None else Clock.wall()
            else:
                # `copyfile` and not `copy`: the instance must not inherit the
                # fixture's read-only mode, and its timestamps are its own. On
                # Linux this is `copy_file_range`, so a reflink filesystem makes
                # the copy nearly free.
                shutil.copyfile(fixture.state_path, state)
                clock = Clock.from_iso(fixture.now)
            # The fixture id, or the world's name for a blank instance, so one
            # caller seed against two fixtures gives two streams. Derived before
            # the connection is opened, because the connection carries it too:
            # `random()` and `randomblob()` are registered on it from this seed.
            seed_bytes = instance_seed(fixture_id if fixture_id is not None else world.name, seed)
            db = open_instance(state, clock, seed_bytes)
            ctx = Ctx(
                db=db,
                clock=clock,
                ids=Ids(seed_bytes),
                state={},
                instance=InstanceInfo(id=instance_id, fixture=fixture_id, seed=seed_bytes),
            )
            _run_startup_hooks(world, ctx, kwargs)
            # Asked once, here, so that the refusal of a table with no primary
            # key is still a refusal at instance creation and the per-call
            # sessions have nothing to work out.
            tracked = tracked_tables(db.conn, world)
            instance = Instance(
                id=instance_id,
                fixture=fixture_id,
                clock=clock,
                ctx=ctx,
                db=db,
                tracked=tracked,
                dir=directory,
                world=world,
                manager=self,
                state_format=format_name,
                formatter=formatter,
                episode_id=episode_id or instance_id,
                caller_seed=seed,
                fixture_sha256=fixture.meta.file_sha256 if fixture is not None else None,
                startup=startup,
            )
        except BaseException:
            if db is not None:
                db.close()
            shutil.rmtree(directory, ignore_errors=True)
            raise
        with self._lock:
            self._instances[instance_id] = instance
        return instance

    def destroy(self, instance: Instance) -> None:
        """Destroy one instance. The same thing as `instance.destroy()`."""
        instance.destroy()

    def unregister(self, instance: Instance) -> None:
        """Forget an instance. Called by `destroy` before it closes anything."""
        with self._lock:
            self._instances.pop(instance.id, None)

    def sweep_stale_processes(self) -> int:
        """Remove the working directories of processes that are no longer running.

        A crashed process leaves its instances on disk and nothing else will ever
        clean them up. Only the default location is swept, and only sibling
        directories carrying this pid namespace's prefix and naming a pid of it
        that is not alive: a live process's directory is never touched, so two
        processes that can see each other's pids cannot sweep each other, a
        directory being built is never removed mid-creation, and a name from a
        namespace this process cannot ask about is never judged. A `work_dir` the
        caller configured is theirs and is never swept.

        The root is read through a checked descriptor (`_open_root`), so a
        symlink planted at its path is not followed and another user's directory
        at that name is not walked. An entry that is itself a symlink is not a
        directory and is never removed: `rmtree` would refuse it anyway, and the
        `lstat` here means it is not even offered. Judging and removing happen
        through that one descriptor, so the entry that was checked and the entry
        that is removed are one inode rather than one name looked up twice.

        Two processes may sweep the same root at the same moment -- each one's
        first `create` does -- so an entry can be listed and then be gone before
        it is judged. That is the other process doing this one's work, not a
        failure: the entry is stepped over, the rest of the root is still swept,
        and the count reports what this process itself removed.

        A directory from the layout before this one -- a bare `<pid>`, with no
        namespace prefix -- is not swept, and nothing else will remove it either.
        Deliberately: the condition that would make it safe to sweep is the
        cross-namespace hazard the pid prefix was added to remove.
        """
        if self._world.work_dir is not None or not _POSIX_WORK_ROOT:
            return 0
        root = _default_work_root()
        swept = 0
        try:
            with _open_root(root) as fd:
                prefix = _dirname_prefix()
                for name in os.listdir(fd):
                    pid = _pid_of(name, prefix)
                    if pid is None or _is_alive(pid):
                        continue
                    try:
                        entry = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    except OSError:
                        # Removed by another sweep between the listing and here,
                        # or unanswerable. Either way it is not this process's to
                        # judge, and it is not the rest of the root's problem.
                        continue
                    if not stat.S_ISDIR(entry.st_mode):
                        continue
                    shutil.rmtree(name, dir_fd=fd, ignore_errors=True)
                    # Counted by what is gone, not by what was attempted:
                    # `rmtree` is asked to ignore errors, and a directory it
                    # could not remove has not been swept.
                    if _is_gone(name, fd):
                        swept += 1
        except OSError:
            # No root yet, or one this process must not touch. `create` raises
            # the loud refusal about the second a moment later; the sweep's job is
            # to remove what it is sure about, so it says nothing here -- and what
            # it had already removed before the refusal is still what it removed.
            pass
        if swept:
            _log.info("swept %d working directories of processes that are gone", swept)
        return swept

    def close(self) -> None:
        """Destroy every live instance. Registered with `atexit`; safe to call twice."""
        with self._lock:
            instances = list(self._instances.values())
        for instance in instances:
            instance.destroy()

    def _fixture(self, fixture_id: str, now: str | datetime | None) -> Fixture:
        """The fixture an instance is about to be copied from, and every refusal it earns.

        Found by directory name rather than by scanning: `freeze` is the only
        thing that mints a fixture and it always names the directory after the
        id, so creating an instance does not have to parse every other sidecar.
        """
        # Applied before the filesystem is touched, so an id off the wire cannot
        # become a path.
        check_id(fixture_id)
        directory = self._world.fixtures_dir / fixture_id
        if not directory.is_dir():
            raise WorldBug(
                f"world {self._world.name!r} has no fixture {fixture_id!r} in "
                f"{self._world.fixtures_dir}; freeze one, or name the directory with "
                f"World(fixtures_dir=...)"
            )
        fixture = load(directory)
        verify(fixture)
        if fixture.meta.schema_hash != self._world.schema_hash:
            raise WorldBug(
                f"fixture {fixture_id!r} was frozen from a different schema; regenerate it"
            )
        if now is not None:
            raise WorldBug("now= applies to blank instances only: a fixture carries its own clock")
        return fixture

    def _make_instance_dir(self, instance_id: str) -> Path:
        """Make this instance's own directory, and answer where it is.

        Two paths. A `work_dir` the caller named is theirs: it is made with
        `parents=True` and nothing is checked, because a directory the caller
        chose is a directory the caller is responsible for.

        The default one is built a descriptor at a time --
        `<tempdir>/seahaven-<uid>/` , `<pid namespace>-<pid>/`, `<world>/` --
        because every one of those names sits under a directory that somebody
        else may be able to write to, and a name is a lookup while a descriptor
        is an inode. `_open_root` checks the root and `_open_child` checks each
        level below it, so a symlink planted at any of them is refused rather
        than followed, and the instance directory is created relative to the
        checked `<world>` descriptor rather than composed as a string and
        resolved again. What comes back is a path, because that is what SQLite
        and the rest of this module take; by then every component of it is an
        inode this user made or owns.

        The root is kept at `0o700` and each level is made `0o700` as well
        rather than relying on the root's mode, because the root's mode protects
        what is under it only as far as the root itself is trustworthy, and
        `_open_root` exists precisely because a root found already in place is
        not. Both are used, and neither replaces the other. `mkdir(mode=0o700)`
        is a ceiling and not a setting, because the process umask masks it, so it
        cannot be relied on to leave `0o700` behind -- which is the whole reason
        the `fchmod` exists. What it does do is make the ceiling `0o700` rather
        than `mkdir`'s default `0o777` for the window between the two calls: the
        argument that the mode is only a ceiling is a reason to follow it with an
        `fchmod`, not a reason to leave the ceiling wide, and under `umask 000`
        that is the difference between a root that is briefly world-writable and
        one that never is. The `fchmod` settles the mode, and it is on a
        descriptor and not on a path for two further reasons of its own:
        `exist_ok` says nothing about the mode of a directory that is already
        there, so the mode is re-asserted on every call and a root left by an
        earlier run is repaired; and a path can be redirected between the two
        calls, while an inode cannot.
        """
        configured = self._world.work_dir
        if configured is not None:
            directory = configured / instance_id
            try:
                directory.mkdir(parents=True)
            except OSError as error:
                # A configured `work_dir` whose parent does not exist, or a
                # filesystem that is full or read-only. A caller who named the
                # directory is the one who can do something about it.
                raise WorldBug(
                    f"cannot make the working directory {directory}: {error}. Name a directory "
                    f"this process can write with World(work_dir=...)"
                ) from error
            return directory

        root = _default_work_root()
        process = _process_dirname(os.getpid())
        try:
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            with _open_root(root) as root_fd:
                os.fchmod(root_fd, 0o700)
                # The per-process directory is one per process and not one per
                # world, so a second world in this process finds it already
                # there; the world's own is the same the second time round.
                with (
                    _open_child(root_fd, process) as process_fd,
                    _open_child(process_fd, self._world.name) as world_fd,
                ):
                    os.mkdir(instance_id, 0o700, dir_fd=world_fd)
        except OSError as error:
            # The refusals that come from outside the framework: a read-only or
            # full `<tempdir>`, a directory planted by somebody else at one of
            # these names, or a symlink standing where one of them should be.
            # They read like every other refusal here, and name the way out.
            raise WorldBug(
                f"cannot make a working directory under {root}: {error}. Name a directory this "
                f"process owns with World(work_dir=...)"
            ) from error
        return root / process / self._world.name / instance_id


def _clock_from(now: str | datetime) -> Clock:
    return Clock.from_iso(now) if isinstance(now, str) else Clock(now)


def _check_startup_kwargs(world: World, startup_kwargs: Mapping[str, Any]) -> None:
    """Refuse a `reset` argument no startup hook asked for.

    A hook taking `**kwargs` accepts everything, which switches the check off for
    the whole world; the docs say to spell the parameters out.
    """
    unknown = set(startup_kwargs) - world.accepted_startup_kwargs
    if unknown and not any(hook.takes_var_kwargs for hook in world.startup_hooks):
        raise WorldBug(f"unknown reset argument(s): {sorted(unknown)}")


def _run_startup_hooks(world: World, ctx: Ctx, startup_kwargs: Mapping[str, Any]) -> None:
    """Run every hook, in registration order, in one transaction.

    One transaction for all of them, so seed rows a later hook writes are atomic
    with an earlier hook's; a hook that raises aborts creation, which removes the
    instance entirely.
    """
    with ctx.db.transaction():
        for hook in world.startup_hooks:
            hook(
                ctx,
                **{
                    name: value
                    for name, value in startup_kwargs.items()
                    if hook.takes_var_kwargs or name in hook.accepts
                },
            )


_sweep_lock = threading.Lock()
_swept = False


def _sweep_once(manager: InstanceManager) -> None:
    """The sweep is per process, not per world: the directories it removes are a process's."""
    global _swept
    if manager._world.work_dir is not None:
        # This world sweeps nothing, so it must not spend the one sweep the
        # process gets: a world with its own working directory made first would
        # otherwise leave every default-location world unswept for the run.
        return
    with _sweep_lock:
        if _swept:
            return
        _swept = True
    manager.sweep_stale_processes()


def _default_work_root() -> Path:
    """`<tempdir>/seahaven-<uid>/`: the parent of every working directory this user makes.

    Per user, and not the shared `<tempdir>/seahaven/` of
    `components/fixtures_instances.md` §2.1, because the system temporary
    directory is shared and a root inside it can be exactly one of private and
    usable by a second user. The phase plan records the reasoning. Both the
    creation path and the sweep read the root from here, so they are the same
    directory by construction.
    """
    if not _POSIX_WORK_ROOT:
        raise WorldBug(
            "there is no default working directory on this platform: it needs a user id, "
            "O_NOFOLLOW, fchmod and mkdirat. Name one with World(work_dir=...)"
        )
    return Path(tempfile.gettempdir()) / f"{WORK_DIR_PREFIX}-{os.getuid()}"


@contextmanager
def _open_root(root: Path) -> Iterator[int]:
    """The working root as a descriptor: a directory, not a link, belonging to this user.

    Everything the framework does to the root is done to this descriptor, because
    the root's *path* cannot be trusted. `<tempdir>` is world-writable with the
    sticky bit, and the sticky bit stops deleting and renaming -- not creating a
    name nobody has claimed yet -- while `seahaven-<uid>` is entirely predictable.
    So a local user can plant either of two things there before Seahaven first
    runs: a symlink, which a path-based `chmod` and a path-based `rmtree` follow
    to wherever it points, or a plain directory of their own, which a path-based
    `chmod` succeeds on whenever the victim is uid 0 -- how this suite, a CI job
    and many container harnesses run.

    `O_NOFOLLOW` refuses the first -- `ENOTDIR` on Linux, where `O_DIRECTORY` is
    judged first, `ELOOP` elsewhere -- and the owner check refuses the second
    with `EPERM`. The caller reads either as one `WorldBug` naming
    `World(work_dir=...)`.

    What this does *not* cover, so that the claim matches the code: `O_NOFOLLOW`
    is about the last component, and a symlinked `<tempdir>` above it (macOS's
    `/tmp` is one) is both legitimate and out of reach -- anyone who can redirect
    `TMPDIR` already owns the process's environment. And an entry removed through
    the root's path after it has been checked is a narrow race rather than the
    standing exposure a planted name is.
    """
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if os.fstat(fd).st_uid != os.geteuid():
            # `geteuid`, not `getuid`: what may be written is the effective id's
            # business. A directory somebody else made at our name is theirs,
            # even when this process is root and could chmod it anyway.
            raise PermissionError(errno.EPERM, "owned by another user", str(root))
        yield fd
    finally:
        os.close(fd)


@contextmanager
def _open_child(parent: int, name: str) -> Iterator[int]:
    """`name` under an open directory, made `0o700` if it is not there, as a descriptor.

    `_open_root`'s rule one level down, and for the same reason. The names below
    the root are as predictable as the root's own -- a pid, a world name -- and
    an entry found already there is not this process's work: it is a second
    world in this process, or it is somebody else's. `mkdir` says which by
    raising `FileExistsError`, and the open that follows settles it: `O_NOFOLLOW`
    refuses a symlink planted at the name, and the owner check refuses a
    directory another user made. What comes back is an inode this user owns, so
    the names created below it cannot be redirected between the check and the
    use.
    """
    with suppress(FileExistsError):
        os.mkdir(name, 0o700, dir_fd=parent)
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        if os.fstat(fd).st_uid != os.geteuid():
            raise PermissionError(errno.EPERM, "owned by another user", name)
        yield fd
    finally:
        os.close(fd)


def _is_gone(name: str, parent: int) -> bool:
    """Is `name` no longer under this directory? Anything unanswerable is still there."""
    try:
        os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def _pid_namespace() -> int | None:
    """The inode of this process's pid namespace, or `None` where there is no procfs."""
    try:
        return os.stat("/proc/self/ns/pid").st_ino
    except OSError:
        return None


def _dirname_prefix() -> str:
    """What every working directory name of this pid namespace starts with."""
    namespace = _pid_namespace()
    return "" if namespace is None else f"{namespace}-"


def _process_dirname(pid: int) -> str:
    """The working directory name of a process: its pid, scoped to its pid namespace.

    §2.1 keys the directory on the bare pid, and a pid means nothing outside the
    namespace that issued it. Two containers of one image with `<tempdir>`
    bind-mounted from the host share a root and have unrelated pid spaces -- and
    both are uid 0, so the per-user root does not separate them. `os.kill(pid, 0)`
    answers in the caller's namespace, so with bare pids one container reads the
    other's live pid as dead and sweeps a running eval's database away. The
    namespace's inode in the name means the sweep can tell its own pids from
    names it must not judge at all, and it ends pid reuse between namespaces too.

    Where there is no procfs to ask -- macOS, and anything else without
    `/proc/self/ns/pid` -- the name is the bare pid, which is §2.1's own layout
    and what this framework did before the prefix existed. A machine whose
    processes all share one namespace loses nothing by it; a machine that does
    not have procfs cannot be asked the question in the first place.
    """
    return f"{_dirname_prefix()}{pid}"


def _pid_of(name: str, prefix: str) -> int | None:
    """The pid a working directory name carries, or `None` if this process cannot judge it.

    A name without this namespace's prefix belongs to another namespace (or to a
    host that has no procfs), where the same number is a different process or no
    process at all. The sweep leaves those alone for ever: a directory that is
    never reclaimed costs disk, and one that is reclaimed while it is in use
    costs somebody's eval.

    Only the digits this code writes are a pid: ASCII `0`-`9`. `str.isdigit` is
    not that test. It is true of superscript digits, which `int` then refuses
    with a `ValueError` -- not an `OSError`, so neither handler in the sweep
    catches it and `world.instance()` itself fails -- and true of the Arabic-
    Indic digits, which `int` reads as an ordinary number, so a name nobody here
    wrote would be judged as some live process's pid and swept when that process
    died. Neither can be planted by a stranger, since the root is `0o700` and
    owner-checked before it is read; but a name this code did not write is not a
    pid, and `None` is the same answer it gives a foreign namespace.
    """
    if not name.startswith(prefix):
        return None
    rest = name[len(prefix) :]
    return int(rest) if rest.isascii() and rest.isdecimal() else None


def _is_alive(pid: int) -> bool:
    """Is a process with this id running?

    A pid we may not signal is alive, and so is anything else that cannot be
    answered for: the cost of being wrong is deleting a running process's
    instances. `0` is not a pid at all -- to `kill` it means this process's whole
    group -- so it is never asked about.
    """
    if pid <= 0:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True
