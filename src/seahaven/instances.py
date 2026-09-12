"""A live world: a private copy of a fixture, and everything a call into it needs.

An instance is a file in a working directory, a connection on it, a frozen clock,
a seeded id stream, a changeset session and a lock. `world.instance(...)` makes
one, `inst.call(...)` runs a tool on it, and `inst.destroy()` (or leaving its
`with` block) takes the files away again. Nothing is shared between two
instances: two instances of one fixture are two copies of one file.

Three rules hold the concurrency together.

*One lock per instance.* `call`, `changes`, `freeze`, `bulk`, `destroy` and the
opens inside `inspect()` and `_control_db()` take it, so calls into one instance
serialise and a destroy waits for the call in flight. Reads through the `inspect()` handle
afterwards do not take it: that handle is the caller's, to read from whatever
thread it likes. The lock is an `RLock` because a control tool is called with it
already held and then asks the instance for something -- its changeset, its
control handle -- that takes it again on the same thread.

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
from contextlib import AbstractContextManager, contextmanager, suppress
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self

import apsw

from seahaven.call import Call
from seahaven.changes import Change, render, start_session
from seahaven.clock import Clock
from seahaven.ctx import Ctx, InstanceInfo
from seahaven.db import Db, build_blank, open_inspection, open_instance
from seahaven.errors import ToolError, UnknownTool, WorldBug
from seahaven.fixtures import STATE_NAME, Fixture, check_id, freeze, load, verify
from seahaven.ids import Ids, instance_seed

if TYPE_CHECKING:  # `world.py` imports this module; the annotation is all that is needed here
    from seahaven.world import World

__all__ = [
    "WORK_DIR_PREFIX",
    "Instance",
    "InstanceManager",
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
    core count, and is `None` on a platform that cannot say. A call is mostly
    Python and SQLite on a warm page cache, so the core count is where throughput
    tops out; the cap keeps a very large host from over-subscribing.
    """
    return min(os.process_cpu_count() or 4, 16)


_gate: threading.BoundedSemaphore | None = threading.BoundedSemaphore(default_concurrency())


def set_concurrency(concurrency: int) -> None:
    """Resize the process-wide gate. `0` removes it; this is `serve --concurrency`.

    Calls already running are unaffected: each releases the gate it took.
    """
    global _gate
    if concurrency < 0:
        raise WorldBug(f"concurrency must not be negative: {concurrency}")
    _gate = threading.BoundedSemaphore(concurrency) if concurrency else None


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
        session: apsw.Session,
        dir: Path,
        world: World,
        manager: InstanceManager,
    ) -> None:
        self.id = id
        self.fixture = fixture
        self.clock = clock
        # The derived instance seed -- what actually drove `ctx.ids` -- and not
        # the `seed=` the caller passed, which is one of its two inputs.
        self.seed = ctx.instance.seed
        self.ctx = ctx
        self.db = db
        self.dir = dir
        self.world = world
        self.closed = False
        # Re-entrant: a control tool holds this lock and then asks the instance
        # for its changeset or its control handle, which take it again.
        self.lock = threading.RLock()
        self._session = session
        self._manager = manager
        self._inspection: Db | None = None
        self._control: Db | None = None

    @property
    def state_path(self) -> Path:
        """The instance's own database file."""
        return self.dir / STATE_NAME

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
                self._inspection = open_inspection(self.state_path, self.clock)
            return self._inspection

    def changes(self) -> list[Change]:
        """Every row this instance has changed since it was created."""
        with self._held():
            return render(self._session.changeset(), self.db.conn)

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
        tool = world.tools.get(name)
        if tool is None:
            # An agent naming a tool that does not exist reads the answer: it is
            # a tool error, not a framework one.
            raise UnknownTool(name)
        # The gate first and the lock second, so a queued call holds nothing.
        with gate(bypass=tool.control), self._held():
            call = Call(name, arguments, tool)
            ctx = self.ctx.with_call(call)
            if tool.control:
                # Imported here, not at the top: `control.py` is written against
                # `Instance`, so the dependency runs that way round and this is
                # the one place the call path needs it back.
                from seahaven import control

                return control.dispatch(self, ctx)
            # The chain is read from the world here rather than held, so a
            # middleware registered after this instance was made applies to it.
            return world.chain(ctx, call)

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
                self._control = open_inspection(self.state_path, self.clock)
            return self._control

    @contextmanager
    def _bulk(self) -> Iterator[Ctx]:
        with self._held(), self.db.transaction():
            yield self.ctx

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

        The session goes before the connection it records on. APSW tolerates the
        other order (it finalises a session with its connection), but a session
        is a growing buffer of every row the instance changed, and releasing it
        first is what makes a destroyed instance cost nothing.

        Both read-only handles -- the caller's, from `inspect()`, and the control
        tools' own -- are closed here as well; either may never have been opened.
        """
        for handle in (self._inspection, self._control):
            if handle is not None:
                handle.close()
        self._inspection = None
        self._control = None
        self._session.close()
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
        seed: int | bytes | None = None,
        now: str | datetime | None = None,
        startup_kwargs: Mapping[str, Any] | None = None,
    ) -> Instance:
        """Materialise an instance from a fixture, or from the world's DDL."""
        world = self._world
        kwargs = startup_kwargs or {}
        # Everything that can be refused is refused here, before a directory
        # exists: an unknown startup argument, an id that is not an id, a fixture
        # that is missing, modified or frozen from another schema, and `now=`
        # where the fixture already carries the clock. A creation that cannot
        # succeed copies nothing and leaves nothing behind.
        _check_startup_kwargs(world, kwargs)
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
            db = open_instance(state, clock)
            # The fixture id, or the world's name for a blank instance, so one
            # caller seed against two fixtures gives two streams.
            seed_bytes = instance_seed(fixture_id if fixture_id is not None else world.name, seed)
            ctx = Ctx(
                db=db,
                clock=clock,
                ids=Ids(seed_bytes),
                state={},
                instance=InstanceInfo(id=instance_id, fixture=fixture_id, seed=seed_bytes),
            )
            _run_startup_hooks(world, ctx, kwargs)
            instance = Instance(
                id=instance_id,
                fixture=fixture_id,
                clock=clock,
                ctx=ctx,
                db=db,
                # Attached after the hooks, so what they wrote is starting state
                # rather than a change the agent made.
                session=start_session(db.conn, world),
                dir=directory,
                world=world,
                manager=self,
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
        `BACKLOG.md` B3 records the leak.
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
        not. `fchmod` on a descriptor, and not `mkdir(mode=...)` on a path, for
        three reasons: `mkdir`'s mode is masked by the process umask, so it is a
        ceiling and not a setting; `exist_ok` says nothing about the mode of a
        directory that is already there, so the mode is re-asserted on every
        call and a root left by an earlier run is repaired; and a path can be
        redirected between the two calls.
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
            root.mkdir(parents=True, exist_ok=True)
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
