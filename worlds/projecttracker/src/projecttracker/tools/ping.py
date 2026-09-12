"""`ping`: the tool that proves an instance of this world is alive.

The placeholder's one tool, and deliberately a thin one -- it reads the clock,
reads the database and returns a small JSON object, which is every layer of a
call (validation, the chain, the transaction, serialisation) and none of this
product's behaviour. The tools that are this product's behaviour arrive with the
rest of the schema.

It is a read, so it stays true whatever a fixture holds, which is what makes it
the thing to call from a smoke test, a transport test or a fresh scaffold.
"""

from typing import Annotated, Any

from pydantic import Field

import seahaven
from projecttracker.world import world

__all__ = ["ping"]


@world.tool
def ping(
    ctx: seahaven.Ctx,
    message: Annotated[str, Field(max_length=200)] = "pong",
) -> dict[str, Any]:
    """Check that the tracker is reachable.

    Returns the message it was given, the tracker's current time, and how many
    users the workspace has.
    """
    row = ctx.db.one("SELECT count(*) AS users FROM users")
    # `Db.one` is `None` only when the query returns no row, and `count(*)`
    # always returns one; the assertion is for the type checker and is a
    # statement of that, not a check on the data.
    assert row is not None
    return {
        "message": message,
        # The instance's clock, in the canonical text every timestamp this world
        # stores or shows is in -- never `datetime.now()`.
        "now": ctx.clock.iso(),
        "users": row["users"],
    }
