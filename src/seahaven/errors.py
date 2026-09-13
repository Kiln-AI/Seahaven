"""Everything Seahaven raises.

Two kinds of failure, and the split is who the failure is for. A `ToolError` is
written for the agent: it has a code, a message and details, it is rendered onto
the observation over OpenEnv, and a world's own errors subclass it. A `WorldBug`
is written for the author: a registration mistake, a call on a destroyed
instance, a result that will not serialise. An agent never sees one.
"""

from typing import Any

__all__ = [
    "ArgumentError",
    "DbError",
    "SeahavenError",
    "ToolError",
    "UnknownTool",
    "WorldBug",
]


class SeahavenError(Exception):
    """The root of the hierarchy: anything Seahaven raises."""


class WorldBug(SeahavenError):
    """Framework misuse, or a bug in world code. Never shown to an agent."""


class ToolError(SeahavenError):
    """A failure the agent is meant to read.

    `code` is the world's vocabulary (`"not_found"`, `"permission_denied"`), free
    apart from the three the framework fixes below. `details` is anything
    JSON-serialisable the agent can act on.
    """

    def __init__(self, code: str, message: str, details: Any = None) -> None:
        # The message, not the triple, so that str(e) and a traceback read as prose.
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(code={self.code!r}, message={self.message!r}, "
            f"details={self.details!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """The wire shape: exactly what the observation carries."""
        return {"code": self.code, "message": self.message, "details": self.details}


class ArgumentError(ToolError):
    """Arguments that did not validate against the tool's argument model."""

    def __init__(self, tool: str, violations: list[dict[str, str]]) -> None:
        super().__init__(
            "invalid_arguments",
            "invalid arguments: " + "; ".join(f"{v['path']}: {v['message']}" for v in violations),
            {"tool": tool, "violations": violations},
        )
        self.tool = tool
        self.violations = violations


class DbError(ToolError):
    """A SQLite failure, wrapped once at `Db` so world code catches one type.

    SQLite's own text is carried but is never the message: engine text reaches an
    agent only when a world's handler or a helper chooses to include it.
    """

    def __init__(
        self,
        sqlite_message: str,
        sqlite_code: int | None = None,
        refusals: tuple[str, ...] = (),
        *,
        message: str | None = None,
    ) -> None:
        # `message=` is how a helper "explicitly does" include engine text
        # (`world_and_dispatch.md` §5): a SQL door mimics a product whose error
        # text *is* SQLite's, so `run_sql` and `controller_run_sql` pass
        # `sqlite_message` here. Nothing else does, and the default is unchanged.
        if message is None:
            message = f"not allowed: {refusals[0]}" if refusals else "database error"
        super().__init__("db_error", message, {"refusals": list(refusals)} if refusals else None)
        self.sqlite_message = sqlite_message
        self.sqlite_code = sqlite_code
        self.refusals = refusals


class UnknownTool(ToolError):
    """A call naming a tool the world does not have."""

    def __init__(self, name: str) -> None:
        super().__init__("unknown_tool", f"unknown tool: {name}", {"name": name})
        self.name = name
