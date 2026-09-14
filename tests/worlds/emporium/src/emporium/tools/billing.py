"""The one tool this world contributes of its own."""

from typing import Any

import seahaven
from emporium.world import world

__all__ = ["record_charge_owner"]


@world.tool
def record_charge_owner(ctx: seahaven.Ctx, charge_id: str, owner_id: str) -> dict[str, Any]:
    """Note which of this company's people owns a charge."""
    ctx.db.execute(
        "INSERT INTO charge_owners (charge_id, owner_id) VALUES (?, ?)", charge_id, owner_id
    )
    return {"charge_id": charge_id, "owner_id": owner_id}
