"""A live world: a private copy of a fixture, and everything a call into it needs.

An instance is a working directory holding one file per node of its world's
composition, a connection on each, a frozen clock they share, and per node a
seeded id stream, a state dict and a changeset session -- plus one lock over the
whole of it. A world that adds nothing has one node, so its instance is the one
file it has always been. `world.instance(...)` makes one, `inst.call(...)` runs a
tool on whichever node owns it, and `inst.destroy()` (or leaving its `with`
block) takes the files away again. Nothing is shared between two instances: two
instances of one fixture are two copies of one file.

*The activation.* Taking the lock from depth 0 opens a `Frame` (`handles.py`) and
returning to depth 0 closes it. That is the lifetime of every `ctx.worlds` handle
host code makes: the outermost call, everything nested inside it, and not one
moment more.

Three rules hold the concurrency together.

*One lock per instance.* `call`, `changes`, `freeze`, `bulk`, `destroy`, a
nested call through a handle and the opens inside `inspect()` and `_control_db()`
take it, so calls into one instance serialise and a destroy waits for the call in
flight. Reads through the `inspect()` handle afterwards do not take it: that
handle is the caller's, to read from whatever thread it likes. The lock is an
`RLock` because a control tool is called with it already held and then asks the
instance for something -- its changeset, its control handle -- that takes it
again on the same thread.

*The gate before the lock.* The concurrency gate bounds how many tool calls run
at once across the process. It is taken before the instance lock, so a call
queued behind it holds nothing and can never delay a `destroy` or a `freeze`. A
call host code makes into an added world does not take it at all: the outermost
call is already holding it, and one instance runs one call at a time however many
nodes that call touches.

*The manager's lock is never held while an instance lock is.* The registry is
touched only in short moments that take nothing else.
"""

import atexit
import errno
import hashlib
import logging
import os
import shutil
import stat
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, ExitStack, contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Concatenate, Self, overload

import apsw

from seahaven.call import Call, arguments_of, name_of
from seahaven.changes import Change, render, start_session
from seahaven.clock import Clock
from seahaven.composition import (
    ROOT_PATH,
    Composition,
    Node,
    NodeKey,
    NodeReport,
    canonical_tree,
)
from seahaven.ctx import Ctx, InstanceInfo
from seahaven.db import Db, build_blank, open_inspection, open_instance
from seahaven.errors import ToolError, UnknownTool, WorldBug
from seahaven.fixtures import Fixture, check_composition, check_id, freeze, load, verify
from seahaven.handles import Frame, unbound
from seahaven.ids import CONTROL_STREAM, INSPECTION_STREAM, Ids, instance_seed
from seahaven.tool import Tool

if TYPE_CHECKING:  # `world.py` imports this module; the annotation is all that is needed here
    from seahaven.world import RegisteredStartupHook, World

__all__ = [
    "WORK_DIR_PREFIX",
    "Instance",
    "InstanceManager",
    "NodeRuntime",
    "calling",
    "concurrency",
    "default_concurrency",
    "gate",
    "in_call",
    "node_seed",
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


@dataclass
class NodeRuntime:
    """One node of one instance: its file, its connection, and what a call on it runs with.

    A leaf world's instance has exactly one of these, for the root, and it holds
    what the instance itself held before composition existed.

    `ctx` is the node's *template* context: its `db`, `ids` and `state` and the
    instance's clock, with no call and an unbound `worlds`. Every context a layer
    or a hook actually runs with is made from it by `Frame.ctx`, which is what
    binds it to one activation.
    """

    node: Node
    db: Db
    ids: Ids
    state: dict[str, Any]
    ctx: Ctx[Any]
    # Attached after the startup hooks have run, so what they wrote is starting
    # state rather than a change the agent made.
    session: apsw.Session | None = None


@dataclass(frozen=True)
class _Target:
    """What a call resolved to: the name as it was asked for, and who owns it."""

    name: str
    tool: Tool
    node: Node


class Instance:
    """One live world instance. Made by `world.instance(...)`, never by hand."""

    def __init__(
        self,
        *,
        id: str,
        fixture: str | None,
        clock: Clock,
        runtime: Mapping[NodeKey, NodeRuntime],
        node_keys: frozenset[NodeKey],
        frozen_versions: Mapping[str, str],
        dir: Path,
        world: World,
        manager: InstanceManager,
    ) -> None:
        self.id = id
        self.fixture = fixture
        self.clock = clock
        # In the composition's own order: breadth-first, the root first.
        self._runtime = dict(runtime)
        self._root_key = next(iter(self._runtime))
        # The derived instance seed -- what actually drove the root's `ctx.ids` --
        # and not the `seed=` the caller passed, which is one of its two inputs.
        self.seed = self.ctx.instance.seed
        self.dir = dir
        self.world = world
        self.closed = False
        # Re-entrant: a control tool holds this lock and then asks the instance
        # for its changeset or its control handle, which take it again, and a
        # tool calling into an added world re-enters it from the same thread.
        self.lock = threading.RLock()
        # The node set this instance was created with, and the seal it was last
        # compared against. A tool or a middleware registered later reaches this
        # instance; an `add_world` cannot, because no live instance holds the file
        # it asks for.
        self._node_keys = node_keys
        # Per node path, the world version the fixture recorded where it is not
        # the one installed. Empty for a blank instance. `composition()` is where
        # an eval reads it (architecture 11.3).
        self._frozen_versions = dict(frozen_versions)
        self._checked: Composition | None = None
        self._manager = manager
        self._inspection: Db | None = None
        self._control: Db | None = None
        # The activation: `_held` opens a `Frame` on the way from depth 0 to 1 and
        # drops it on the way back, and every handle made during it is anchored to
        # the epoch it carries.
        self._depth = 0
        self._epoch = 0
        self._frame: Frame | None = None

    @property
    def ctx(self) -> Ctx[Any]:
        """The root node's template context: no call, and a `worlds` that is not bound."""
        return self._runtime[self._root_key].ctx

    @property
    def db(self) -> Db:
        """The root node's connection. An added node's is `ctx.worlds.<name>.db`."""
        return self._runtime[self._root_key].db

    @property
    def state_path(self) -> Path:
        """The root node's database file."""
        return self.dir / self._runtime[self._root_key].node.file_name

    @overload
    def call[**P, R](
        self,
        tool: Callable[Concatenate[Ctx[Any], P], R],
        /,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> R: ...
    @overload
    def call(self, tool: str, /, **arguments: Any) -> Any: ...

    def call(self, tool: str | Callable[..., Any], /, *args: Any, **arguments: Any) -> Any:
        """Run one tool, with its arguments validated, on the calling thread.

        By the name an agent would use, or by the function itself -- which is the
        typed way in, with the arguments checked and the result the tool's own
        object rather than its rendering (architecture section 8). A function
        resolves against the composite surface, so it names a tool of this world
        or of one it adds, and unambiguously: a world that is a node of this tree
        twice does not name one store, and the answer says which handle does.

        The tool may belong to this world or to any world it adds: the composite
        surface is flat, and the call runs against the store of whichever node
        owns it. Raises the owning world's `ToolError` subclasses to the caller;
        over OpenEnv the same error is rendered onto the observation instead.
        """
        started = time.perf_counter()
        # The exposed name once there is one, so an eval grouping the log on it
        # cannot tell the two ways in apart; the function's qualified name until
        # then, which is all a resolution failure has to name.
        name = name_of(tool)
        node: str | None = None
        try:
            target = self._target(tool)
            name, node = target.name, target.node.path
            result = self._dispatch(target, arguments_of(target.tool, tool, args, arguments))
        except BaseException as error:
            self._log_failure(name, started, error, node)
            raise
        self._log_call(name, started, "ok", node)
        return result

    def tools(self) -> list[dict[str, Any]]:
        """The tool list, with JSON schemas. Control tools are never in it.

        The composite list: this world's own tools in registration order, then
        each added world's contribution in `add_world` order. Every entry is the
        owning tool's own listing with only the name substituted, so a
        contributed tool's description and input schema are byte-identical to the
        added world's and nothing in it reveals where it came from. A world that
        adds nothing seals to one node and gets its own registry back.
        """
        return [
            entry.tool.listing() | {"name": entry.name}
            for entry in self._current_composition().tools.values()
        ]

    def inspect(self) -> Db:
        """A read-only handle on this instance, opened once and kept.

        Every table of every node: the root's store is `main` and each added
        node's file is attached under the schema its path derives, so "was the
        invoice created and was the message posted" is one statement over
        `main.invoices` and `messaging.posts`. The instance's clock, and no
        authorizer beyond the connection's permanent write denial -- which was
        installed after the attaches and therefore refuses a later `ATTACH` as
        well as every write (`db.open_inspection`).

        Reads through it do not take the instance lock: a read-only connection on
        a WAL database sees a consistent snapshot per statement. Reading through
        it concurrently with `destroy()` is the one ordering the caller owns.
        """
        with self._held():
            if self._inspection is None:
                self._inspection = open_inspection(
                    self.state_path,
                    self.clock,
                    self.ctx.instance.seed,
                    INSPECTION_STREAM,
                    self._attachments(),
                )
            return self._inspection

    def composition(self) -> tuple[NodeReport, ...]:
        """What this instance is running against: every node, root first.

        Paths, world names and versions, the scope each node resolved into and
        the alias routes that reach it. The set the instance was created with, so
        it describes the files on disk rather than whatever the world's seal says
        now. Nothing agent-facing carries any of it.

        A node whose fixture was frozen from another version of its world, with
        the schema unchanged, also carries `frozen_world_version`: that is
        reported and never refused, and this is where an eval reads it.
        """
        return tuple(
            NodeReport.of(runtime.node, self._frozen_versions.get(runtime.node.path))
            for runtime in self._runtime.values()
        )

    def changes(self) -> list[Change]:
        """Every row this instance has changed since it was created, in every node.

        One list: each node's changeset rendered against its own connection and
        stamped with its own path, concatenated in the composition's canonical
        order, root first. A world that adds nothing has one node, so its list is
        what it always was with `main` on every record.
        """
        with self._held():
            return [
                change
                for runtime in self._runtime.values()
                for change in render(
                    _session_of(runtime).changeset(), runtime.db.conn, runtime.node.path
                )
            ]

    def freeze(self, id: str, description: str) -> Fixture:
        """Mint a fixture from this instance's current state: every node's store, together.

        One frozen file per node and one sidecar describing all of them, at the
        one clock this instance runs on. All or nothing: a node whose schema has
        drifted mints nothing (`fixtures.freeze`).
        """
        with self._held():
            if any(runtime.db.in_transaction for runtime in self._runtime.values()):
                # The lock is an `RLock`, so a `freeze` inside `bulk()` gets this
                # far and then reaches a `VACUUM` SQLite will not run inside a
                # transaction. Said here instead, where what to do about it is
                # obvious: the rows are not committed yet, and a fixture of
                # uncommitted rows is not what the caller asked for either. Any
                # node, because `bulk()` opens a transaction on every one of them.
                raise WorldBug(
                    "freeze cannot run inside bulk(): leave the bulk() block first, so the "
                    "rows it wrote are committed and the fixture is what the instance holds"
                )
            return freeze(self, id, description, fixtures_dir=self.world.fixtures_dir)

    def bulk(self) -> AbstractContextManager[Ctx[Any]]:
        """Write straight into the instance, under its lock and one transaction per node.

        The authoring path: a tool call per row would spend its time on argument
        validation and transaction boundaries for tens of thousands of rows. What
        is yielded is the root node's context, with no call attached and a live
        `ctx.worlds`, so an added world's store is reached through
        `ctx.worlds.<name>.db`; nothing is disabled and nothing is wrapped. Every
        node's transaction is committed on the way out, and all of them are rolled
        back together if the block raised. Startup hooks do not run again.
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

    def _target(self, tool: str | Callable[..., Any]) -> _Target:
        """Which node owns the tool a caller named, in the tree as it stands now."""
        composition = self._current_composition()
        if not isinstance(tool, str):
            # A function names the entry, and the entry names the node: the two
            # ways in meet here, and everything below this line is one path.
            entry = composition.entry_for(tool)
            return _Target(entry.name, entry.tool, entry.node)
        name = tool
        entry = composition.tools.get(name)
        if entry is not None:
            return _Target(name, entry.tool, entry.node)
        # Control tools are never contributed -- they are the framework's, not a
        # world's surface -- so the composite list cannot hold them and the root's
        # own registry is where they are. Every other name the root registers is
        # in that list already.
        registered = self.world.tools.get(name)
        if registered is None or not registered.control:
            # An agent naming a tool that does not exist reads the answer: it is
            # a tool error, not a framework one.
            raise UnknownTool(name)
        return _Target(name, registered, composition.root)

    def _dispatch(self, target: _Target, arguments: Mapping[str, Any]) -> Any:
        tool = target.tool
        # The gate first and the lock second, so a queued call holds nothing.
        with gate(bypass=tool.control), self._held() as frame:
            call = Call(target.name, arguments, tool, node=target.node.path)
            ctx = frame.ctx(target.node.key, call)
            if tool.control:
                # Imported here, not at the top: `control.py` is written against
                # `Instance`, so the dependency runs that way round and this is
                # the one place the call path needs it back.
                from seahaven import control

                return control.dispatch(self, ctx)
            # The chain is read from the node here rather than held, so a
            # middleware registered after this instance was made applies to it.
            with in_call():
                return target.node.agent_chain(ctx, call)

    def _current_composition(self) -> Composition:
        """The world's tree, refusing one that has grown or lost a node since creation.

        A tool or a middleware registered after an instance exists reaches it on
        its next call, which is the framework's promise and what the seal
        delivers. An `add_world` cannot: it changes how many files an instance is
        supposed to have, and no live instance can honour that.

        Memoised on the composition's identity, so the set comparison runs once
        per seal rather than once per call.
        """
        composition = self.world.composition()
        if composition is not self._checked:
            if frozenset(composition.by_key) != self._node_keys:
                raise WorldBug(
                    f"the composition of world {self.world.name!r} changed after instance "
                    f"{self.id} was created; create a new instance"
                )
            self._checked = composition
        return composition

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
                    self.state_path,
                    self.clock,
                    self.ctx.instance.seed,
                    CONTROL_STREAM,
                    self._attachments(),
                )
            return self._control

    def _attachments(self) -> list[tuple[str, Path]]:
        """Every node but the root, as `open_inspection` attaches them.

        Built from the instance's own runtime, which is the set of files that
        exist on disk, and not from the world's current seal. By depth rather
        than by position, so the invariant is read off the node itself.
        """
        return [
            (runtime.node.schema_name, self.dir / runtime.node.file_name)
            for runtime in self._runtime.values()
            if runtime.node.depth > 0
        ]

    def _nodes(self) -> tuple[NodeRuntime, ...]:
        """Every node's runtime, root first, in the composition's canonical order.

        Reached by `fixtures.freeze`, which writes down what they hold. Private
        for the reason `_control_db` is: a node runtime is the framework's own
        internals and no part of what a world or an eval is offered.
        """
        return tuple(self._runtime.values())

    @contextmanager
    def _bulk(self) -> Iterator[Ctx[Any]]:
        with self._held() as frame, ExitStack() as stack:
            # Every node's transaction open before the block runs and committed in
            # sequence on the way out, so a bulk write that reaches two stores
            # through `ctx.worlds` either lands in both or in neither.
            for runtime in self._runtime.values():
                stack.enter_context(runtime.db.transaction())
            yield frame.ctx(self._root_key, None)

    @contextmanager
    def _held(self) -> Iterator[Frame]:
        """Hold the lock, refusing an instance `destroy` got to first, and open the activation.

        Everything that touches the instance goes through here, so a caller
        either wins the race with `destroy` or is told the instance is gone --
        never half of each.

        The `Frame` is opened on the way from depth 0 to 1 and dropped on the way
        back, and every re-entry on the same thread yields that same frame. That
        is what makes a handle live exactly as long as the activation that made
        it: across a nested `handle.call`, after it returns, and across an
        `inst.call(...)` from inside a `bulk()` block -- but not one moment past
        the outermost block, because the epoch moves again on the way out.
        """
        with self.lock:
            if self.closed:
                raise WorldBug(f"instance {self.id} has been destroyed")
            frame = self._frame
            if frame is None:
                self._epoch += 1
                frame = Frame(self, self._epoch)
                self._frame = frame
            self._depth += 1
            try:
                yield frame
            finally:
                self._depth -= 1
                if self._depth == 0:
                    self._frame = None
                    self._epoch += 1

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
        for runtime in self._runtime.values():
            if runtime.session is not None:
                runtime.session.close()
            runtime.db.close()

    def _log_failure(
        self,
        name: str,
        started: float,
        error: BaseException,
        node: str | None = None,
        *,
        internal: bool = False,
    ) -> None:
        """The line for a call that raised, in the vocabulary an eval groups on.

        A `ToolError` has a code; anything else has only its class name to give,
        and `invoke` has already put the traceback on record.
        """
        outcome = error.code if isinstance(error, ToolError) else type(error).__name__
        self._log_call(name, started, outcome, node, internal=internal)

    def _log_call(
        self,
        name: str,
        started: float,
        outcome: str,
        node: str | None = None,
        *,
        internal: bool = False,
    ) -> None:
        """The one operational line per call: which instance, which tool, how long, how it went.

        The duration is wall time from entry, so a call that queued behind the
        gate reports the latency its caller saw.

        `node` is the path of the node that owns the tool, absent only when no
        node does -- an agent naming a tool that is not there. A call host code
        made through a handle marks itself internal, so an eval can tell an
        agent's calls from the ones a composite made on its behalf.
        """
        _log.info(
            "call %s on instance %s of world %s: %s in %.1f ms%s",
            name,
            self.id,
            self.world.name,
            outcome,
            (time.perf_counter() - started) * 1000,
            _where(node, internal),
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
        """Materialise an instance from a fixture, or from the world's DDL.

        One SQLite file, one connection, one `Ids` stream and one changeset
        session per node of the world's composition -- which for a world that
        adds nothing is one of each, in the directory it has today. A composite
        fixture carries one frozen file per node and every one of them is copied;
        a blank instance builds every node from its own world's DDL.
        """
        world = self._world
        kwargs = startup_kwargs or {}
        # The seal first: every whole-tree failure surfaces from the first use of
        # the tree, and this is one.
        composition = world.composition()
        # Everything else that can be refused is refused here, before a directory
        # exists: an unknown startup argument, an id that is not an id, a fixture
        # that is missing, modified or frozen from another schema, and `now=`
        # where the fixture already carries the clock. A creation that cannot
        # succeed copies nothing and leaves nothing behind.
        _check_startup_kwargs(composition, kwargs)
        fixture = self._fixture(composition, fixture_id, now) if fixture_id is not None else None
        _sweep_once(self)

        instance_id = str(uuid.uuid4())
        directory = self._make_instance_dir(instance_id)
        runtime: dict[NodeKey, NodeRuntime] = {}
        try:
            if fixture is None:
                for node in composition.nodes:
                    build_blank(directory / node.file_name, node.world.schema).close()
                clock = _clock_from(now) if now is not None else Clock.wall()
            else:
                _copy_fixture(fixture, composition, directory)
                clock = Clock.from_iso(fixture.now)
            # The fixture id, or the world's name for a blank instance, so one
            # caller seed against two fixtures gives two streams. Derived before
            # any connection is opened, because every node's connection carries a
            # stream of it too: `random()` and `randomblob()` are registered on
            # each one from that node's own seed.
            base = instance_seed(fixture_id if fixture_id is not None else world.name, seed)
            info = InstanceInfo(id=instance_id, fixture=fixture_id, seed=base)
            for node in composition.nodes:
                runtime[node.key] = _open_node(node, directory, clock, base, info)
            instance = Instance(
                id=instance_id,
                fixture=fixture_id,
                clock=clock,
                runtime=runtime,
                node_keys=frozenset(composition.by_key),
                frozen_versions=_frozen_versions(fixture, composition),
                dir=directory,
                world=world,
                manager=self,
            )
            # One activation for the whole of creation, so a root hook's handles
            # stay live across every hook that runs after it.
            with instance._held() as frame:
                _run_startup_hooks(composition, frame, kwargs)
                for node_runtime in runtime.values():
                    node_runtime.session = start_session(
                        node_runtime.db.conn, node_runtime.node.world
                    )
        except BaseException:
            for node_runtime in runtime.values():
                # The session before the connection it records on, as `_close`
                # does and for the same reason.
                if node_runtime.session is not None:
                    node_runtime.session.close()
                node_runtime.db.close()
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

    def _fixture(
        self, composition: Composition, fixture_id: str, now: str | datetime | None
    ) -> Fixture:
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
        # Every node's schema and the shape of the tree itself, after the root's
        # own two checks and before anything is copied. What comes back is the
        # differences that are reported rather than refused: an added world
        # installed at another version whose schema is unchanged still loads, and
        # one package cannot be installed at two versions in one environment, so
        # there is nothing here to act on beyond saying so.
        for difference in check_composition(fixture.meta, composition):
            _log.info("fixture %s of world %s: %s", fixture_id, self._world.name, difference)
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


def node_seed(base: bytes, path: str) -> bytes:
    """The seed one node's id stream is drawn from.

    The root's is the instance seed itself, untouched, so a world that adds
    nothing draws exactly the identifiers it drew before composition existed. An
    added node's is a function of its own canonical path alone, so adding or
    removing a node perturbs no other node's stream -- which is why the path is
    the salt rather than a position in the tree.

    The `node` tag is domain separation against `ids._stream_seed`, which derives
    a connection's `random()` stream as `sha256(seed + b"\0" + label)` over the
    same base. A child may be named anything `^[a-z][a-z0-9_]*$` matches, `control`,
    `inspection` and `instance` included, so without the tag a node of that name
    would draw its identifiers from the very stream one of the root's three doors
    hands to SQL -- and an agent's `SELECT random()` would read out the ids that
    node is about to mint. Pinned by
    `test_composite_instance.py::test_a_node_named_after_a_sql_door_does_not_draw_that_doors_stream`.
    """
    if path == ROOT_PATH:
        return base
    return hashlib.sha256(base + b"\0node\0" + path.encode("utf-8")).digest()


def _open_node(
    node: Node, directory: Path, clock: Clock, base: bytes, info: InstanceInfo
) -> NodeRuntime:
    """Open one node's file and build the context every call on it starts from.

    The connection is opened on the node's own seed, so `random()` and
    `randomblob()` in one node's SQL are a stream of that node's -- two nodes of
    one instance share neither each other's nor their own `ctx.ids`. The root's
    node seed is the instance seed itself, so a leaf world's writable connection
    draws exactly what it drew before composition existed.
    """
    seed = node_seed(base, node.path)
    db = open_instance(directory / node.file_name, clock, seed)
    ids = Ids(seed)
    state: dict[str, Any] = {}
    return NodeRuntime(
        node=node,
        db=db,
        ids=ids,
        state=state,
        # Unbound: this context belongs to the instance and to no activation, so
        # one that escapes the framework raises where it reaches for another
        # world rather than addressing whatever call happens to be running.
        ctx=Ctx(db=db, clock=clock, ids=ids, state=state, instance=info, worlds=unbound()),
    )


def _session_of(runtime: NodeRuntime) -> apsw.Session:
    """A node's changeset session, which exists for as long as its instance does.

    The field is optional because the session is attached after the startup hooks
    have run, and that is inside creation -- before any caller holds the instance.
    """
    if runtime.session is None:
        raise WorldBug(f"the changeset session of node {runtime.node.path!r} is not attached")
    return runtime.session


def _frozen_versions(fixture: Fixture | None, composition: Composition) -> dict[str, str]:
    """Per node path, the world version its fixture recorded, where it is not the installed one.

    Empty for a blank instance, and empty for a fixture whose every node is at
    the version it was frozen at, so a report only carries the field when there
    is something to say. `check_composition` has already refused every node whose
    *schema* moved, so what is left here is the difference that is reported and
    never refused (architecture 11.3).

    Version only, deliberately: `NodeReport.frozen_world_version` is the field
    §12 gives the report, so a node frozen from a differently-named world with
    the same version reads as unchanged here. The other half of that difference
    is `fixtures._version_differences`, which compares world name and version
    both and puts the name in the INFO log at create.
    """
    if fixture is None:
        return {}
    recorded = {composition.root.path: fixture.meta.world_version}
    recorded |= {node.path: node.world_version for node in fixture.meta.nodes}
    return {
        node.path: recorded[node.path]
        for node in composition.nodes
        if node.path in recorded and recorded[node.path] != node.world.version
    }


def _copy_fixture(fixture: Fixture, composition: Composition, directory: Path) -> None:
    """Copy one frozen file per node into the new instance's directory.

    `copyfile` and not `copy`: the instance must not inherit the fixture's
    read-only mode, and its timestamps are its own. On Linux this is
    `copy_file_range`, so a reflink filesystem makes the copy nearly free.

    The source name is the one the sidecar recorded and the destination is the
    one this composition derives. They agree -- both come from the node's path --
    and reading the source from the sidecar is what keeps the fixture, rather
    than a rule repeated here, the description of what is in the directory.
    `check_composition` has already established that the two sets of paths match.

    Every source here is a file of the fixture's own directory rather than a link
    out of it, because `_fixture` runs `verify` first and `fixtures._verify_file`
    refuses a symlink by name. The rule is stated once, where the file is hashed,
    rather than twice.
    """
    shutil.copyfile(fixture.state_path, directory / composition.root.file_name)
    by_path = {node.path: node for node in composition.nodes}
    for node in fixture.nodes:
        shutil.copyfile(fixture.file_of(node), directory / by_path[node.path].file_name)


def _check_startup_kwargs(composition: Composition, startup_kwargs: Mapping[str, Any]) -> None:
    """Refuse a `reset` argument no startup hook in the tree asked for.

    The union across the tree, because a keyword is broadcast: every hook in the
    tree that names it receives it. A hook taking `**kwargs` anywhere accepts
    everything, which switches the check off for the whole tree; the docs say to
    spell the parameters out.
    """
    accepted = composition.accepted_startup_kwargs
    if accepted is None:
        return
    unknown = set(startup_kwargs) - accepted
    if unknown:
        raise WorldBug(f"unknown reset argument(s): {sorted(unknown)}")


def _run_startup_hooks(
    composition: Composition, frame: Frame, startup_kwargs: Mapping[str, Any]
) -> None:
    """Run every node's hooks once, root first, with every node's transaction already open.

    All the transactions before the first hook, because the root's hooks are
    specified to write into a child's store through `ctx.worlds.<name>.db` before
    that child's own hooks run, which is only coherent if the child's transaction
    is already open. A hook that raises rolls every one of them back, and creation
    removes the instance entirely.

    Depth-first preorder over the canonical tree, so a node whose hooks seed a
    child runs before it, and a node reached by two routes runs once.
    """
    runtimes = frame.instance._runtime
    with ExitStack() as stack:
        for node in composition.nodes:
            stack.enter_context(runtimes[node.key].db.transaction())
        for node in canonical_tree(composition.root):
            ctx = frame.ctx(node.key, None)
            for hook in node.world.startup_hooks:
                hook(ctx, **_hook_arguments(hook, node, startup_kwargs))


def _hook_arguments(
    hook: RegisteredStartupHook, node: Node, startup_kwargs: Mapping[str, Any]
) -> dict[str, Any]:
    """What one hook is called with: the `reset` keywords it named, then the bound ones.

    Bound last and therefore final. A bound keyword is part of the composition,
    and an eval must not be able to reconfigure one node by passing a `reset()`
    keyword that happens to share its name -- while that keyword still reaches
    every other hook in the tree that names it.
    """

    def wanted(name: str) -> bool:
        return hook.takes_var_kwargs or name in hook.accepts

    return {
        **{name: value for name, value in startup_kwargs.items() if wanted(name)},
        **{name: value for name, value in node.bound_startup.items() if wanted(name)},
    }


# The per-thread "in a call" flag. In-process calls run on the caller's thread and
# OpenEnv runs each session on its own, so a thread-local is exactly the scope
# this question has: a tool that is running must not create an instance, and a
# tool that is not is ordinary authoring code.
_in_call = threading.local()


@contextmanager
def in_call() -> Iterator[None]:
    """Mark this thread as inside a tool call for the length of the block."""
    previous = calling()
    _in_call.active = True
    try:
        yield
    finally:
        _in_call.active = previous


def calling() -> bool:
    """Is this thread inside a tool call? What `World.instance` refuses on."""
    return getattr(_in_call, "active", False)


def _where(node: str | None, internal: bool) -> str:
    """The node the call ran on, and whether host code made it, for the log line."""
    if node is None:
        return ""
    return f" (node={node}, internal=true)" if internal else f" (node={node})"


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
