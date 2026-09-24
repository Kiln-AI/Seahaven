"""The server's instances, and one request run against one of them.

No server stack here: the registry and `dispatch` are synchronous and
thread-safe, and the ASGI app in `seahaven.http.server` calls them on worker
threads.
"""

import logging
import random
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from seahaven.cli.mcp import RESET_OPTION_KEYS, SEED_CEILING
from seahaven.clock import ClockMode
from seahaven.errors import WorldBug
from seahaven.http.messages import Headers, HttpHandler, HttpRequest, HttpResponse
from seahaven.instances import Instance
from seahaven.world import World

__all__ = [
    "DEFAULT_CLOCK_MODE",
    "DEFAULT_MAX_INSTANCES",
    "INSTANCE_ID",
    "PUT_BODY",
    "SERVER_OPTIONS",
    "CapacityReached",
    "CreationFailed",
    "Registry",
    "check_reset_options",
    "dispatch",
    "seahaven_error",
]

DEFAULT_MAX_INSTANCES = 100
INSTANCE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
# A test server reads the time the way the real product does.
DEFAULT_CLOCK_MODE: ClockMode = "wall"

# Where a set of reset options came from, as a message names it.
SERVER_OPTIONS = "reset_options"
PUT_BODY = "the PUT body"

_log = logging.getLogger("seahaven.http")


def seahaven_error(status: int, message: str, *, headers: Headers = ()) -> HttpResponse:
    """A response the server makes itself: a client tells it from the world's by its key."""
    return HttpResponse.json({"seahaven_error": message}, status=status, headers=headers)


class CapacityReached(Exception):
    """Creating one more instance would pass the server's limit."""

    def __init__(self, limit: int) -> None:
        super().__init__(
            f"this server holds at most {limit} instances; DELETE /worlds/{{id}} to free one"
        )
        self.limit = limit


class CreationFailed(Exception):
    """`world.instance(...)` raised. `cause` is what it raised."""

    def __init__(self, cause: Exception) -> None:
        super().__init__(str(cause))
        self.cause = cause


def check_reset_options(world: World, options: Mapping[str, Any], *, source: str) -> None:
    """Refuse a key `world.instance(...)` is not called with here, and a missing fixture.

    The fixture is checked only for the server's own options, so a mistake there
    stops the server from starting. A PUT body's fixture is left to
    `world.instance(...)`, whose refusal answers that request alone.
    """
    unknown = sorted(set(options) - RESET_OPTION_KEYS)
    if unknown:
        named = ", ".join(f'"{key}"' for key in unknown)
        taken = ", ".join(f'"{key}"' for key in sorted(RESET_OPTION_KEYS))
        raise WorldBug(
            f"{source} does not take {named}; it takes {taken}, and a world's own startup "
            'keywords go inside "startup"'
        )
    fixture = options.get("fixture")
    if source == SERVER_OPTIONS and isinstance(fixture, str):
        ids = [each.id for each in world.fixtures()]
        if fixture not in ids:
            there = ", ".join(repr(each) for each in ids) if ids else "none"
            raise WorldBug(
                f"{source} names the fixture {fixture!r}, which world {world.name} does not "
                f"have; its fixtures are: {there}"
            )


@dataclass(eq=False)
class _Slot:
    """Everything done to one instance ID, serialised by `lock`.

    `retired` is set when the slot leaves the registry, so a thread that waited
    on `lock` meanwhile knows to start again with the slot now in its place.
    """

    lock: threading.Lock = field(default_factory=threading.Lock)
    instance: Instance | None = None
    retired: bool = False


class Registry:
    """The instances of one server, by ID. Every method blocks; call it off the event loop.

    Two kinds of lock: the registry's own, held only for the dictionary and the
    counter, and a slot's, held for everything done to one ID, a request
    included. The registry lock is never held while a slot lock is taken.
    """

    def __init__(self, world: World, defaults: Mapping[str, Any], max_instances: int) -> None:
        self._world = world
        self._defaults = dict(defaults)
        self._max_instances = max_instances
        self._lock = threading.Lock()
        self._slots: dict[str, _Slot] = {}
        # Slots whose instance exists or is being created, so two creations at
        # once cannot both pass the limit.
        self._live = 0

    def run[T](self, id: str, fn: Callable[[Instance], T]) -> T:
        """Call `fn` with instance `id`, creating it from the defaults if it does not exist."""
        while True:
            slot = self._slot(id)
            with slot.lock:
                if slot.retired:
                    continue
                if slot.instance is None:
                    slot.instance = self._create(id, slot, self._defaults)
                return fn(slot.instance)

    def put(self, id: str, body: Mapping[str, Any]) -> bool:
        """Create or replace instance `id` from the defaults with `body` laid over them.

        A body key set to `None` removes that key. Answers whether the instance
        was created rather than replaced. A replaced instance is gone even when
        its replacement fails.
        """
        options = {
            key: value
            for key, value in {**self._defaults, **body}.items()
            if not (key in body and body[key] is None)
        }
        while True:
            slot = self._slot(id)
            with slot.lock:
                if slot.retired:
                    continue
                existed = slot.instance is not None
                if existed:
                    self._discard(slot)
                slot.instance = self._create(id, slot, options)
                return not existed

    def delete(self, id: str) -> bool:
        """Destroy instance `id`, after any request on it finishes. False when there is none."""
        with self._lock:
            slot = self._slots.get(id)
        if slot is None:
            return False
        with slot.lock:
            if slot.retired or slot.instance is None:
                return False
            self._discard(slot)
            self._retire(id, slot)
            return True

    def close(self) -> None:
        """Destroy every instance. Called once, as the server stops."""
        with self._lock:
            slots = list(self._slots.items())
        for id, slot in slots:
            with slot.lock:
                if slot.retired:
                    continue
                if slot.instance is not None:
                    try:
                        self._discard(slot)
                    except Exception:
                        _log.exception("could not destroy instance %s", id)
                self._retire(id, slot)

    def _slot(self, id: str) -> _Slot:
        with self._lock:
            return self._slots.setdefault(id, _Slot())

    def _retire(self, id: str, slot: _Slot) -> None:
        """Take `slot` out of the registry. Its lock is held."""
        slot.retired = True
        with self._lock:
            if self._slots.get(id) is slot:
                del self._slots[id]

    def _reserve(self) -> None:
        with self._lock:
            if self._max_instances and self._live >= self._max_instances:
                raise CapacityReached(self._max_instances)
            self._live += 1

    def _release(self) -> None:
        with self._lock:
            self._live -= 1

    def _create(self, id: str, slot: _Slot, options: Mapping[str, Any]) -> Instance:
        """A new instance for `slot`, whose lock is held and which holds none.

        On a failure the slot is retired, so no empty slot is left behind.
        """
        try:
            self._reserve()
        except CapacityReached:
            self._retire(id, slot)
            raise
        try:
            return self._make(id, options)
        except Exception as error:
            self._release()
            self._retire(id, slot)
            raise CreationFailed(error) from error

    def _make(self, id: str, options: Mapping[str, Any]) -> Instance:
        kwargs = dict(options)
        # A seed of its own, so an ID minted on one instance is not also valid on
        # another. A key that is present is used as given, `None` included.
        kwargs.setdefault("seed", random.randrange(SEED_CEILING))
        kwargs.setdefault("clock_mode", DEFAULT_CLOCK_MODE)
        instance = self._world.instance(**kwargs)
        _log.info("created instance %s with seed %s", id, instance.caller_seed)
        return instance

    def _discard(self, slot: _Slot) -> None:
        """Destroy the slot's instance and free its place. The slot lock is held."""
        instance, slot.instance = slot.instance, None
        try:
            if instance is not None:
                instance.destroy()
        finally:
            self._release()


class _NotAResponse(WorldBug):
    """The handler returned something other than an `HttpResponse`."""


def dispatch(instance: Instance, handler: HttpHandler, request: HttpRequest) -> HttpResponse:
    """Run one request as one transaction: committed on any response, rolled back on a raise."""
    try:
        with instance.bulk() as ctx:
            # The one private member this package touches. It is what a tool call
            # moves as it takes its ordinal, and only the `tick` mode reads it, so
            # a request reads the next tick as a tool call would.
            instance.clock._call_started()
            response = handler(ctx, request)
            # Raised inside the block, so the handler's writes roll back.
            if not isinstance(response, HttpResponse):
                raise _NotAResponse(
                    f"the handler returned {type(response).__name__}, not an HttpResponse"
                )
        return response
    except Exception as error:
        _log.exception("the HTTP handler failed on %s %s", request.method, request.path)
        if isinstance(error, _NotAResponse):
            return seahaven_error(500, str(error))
        return seahaven_error(500, f"the handler raised {type(error).__name__}: {error}")
