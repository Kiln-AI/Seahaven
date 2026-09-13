"""What a tool is handed, and everything it is allowed to reach for.

One `Ctx` exists per instance and carries the instance's database, clock and
seeded randomness. A call gets a shallow copy of it with `call` set, so a tool
sees its own `Call` and never another's, while `db` and `state` stay the one
object the whole instance shares.
"""

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Self

from seahaven.clock import Clock
from seahaven.db import Db
from seahaven.ids import Ids

if TYPE_CHECKING:  # `call.py` sits above this module; the annotation is all that is needed here
    from seahaven.call import Call

__all__ = ["Ctx", "InstanceInfo"]


@dataclass(frozen=True)
class InstanceInfo:
    """Which instance this is, for a tool that wants to say so.

    `seed` is the derived instance seed, not the `seed=` the caller passed: the
    caller's value is one of two inputs to it, and the derived bytes are what
    actually drove `ctx.ids`.
    """

    id: str
    fixture: str | None
    seed: bytes


@dataclass(frozen=True)
class Ctx:
    """The instance context: `ctx` in every tool, middleware and startup hook."""

    db: Db
    clock: Clock
    ids: Ids
    # A plain dict on purpose. What a world keeps in it -- a principal, a
    # compiled schema, a counter -- and how it types that is the world's
    # business, and a framework model here would only be in the way.
    state: dict[str, Any]
    instance: InstanceInfo
    call: Call | None = None

    def with_call(self, call: Call) -> Self:
        """A copy of this context bound to one call.

        Shallow: `db`, `state` and `ids` are the same objects, so a tool writing
        to `ctx.state` writes to the instance's state.
        """
        return replace(self, call=call)
