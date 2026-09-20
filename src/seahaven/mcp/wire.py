"""Translation: a tool listing, a result and an error, in the shapes MCP carries.

Nothing here knows about sessions, handlers or the server. It is the same
taxonomy `seahaven/openenv/env.py` renders onto an OpenEnv observation, on
another wire: a `ToolError` is data the agent reads, and anything else is
scrubbed to a fixed message and a correlation id that leads to the traceback on
stderr.
"""

import json
import logging
import uuid
from typing import Any

from mcp import types

from seahaven.call import serialise
from seahaven.errors import INTERNAL_ERROR_CODE, INTERNAL_ERROR_MESSAGE, ToolError

__all__ = ["failure", "internal_error", "listing", "log_failure", "success"]

_log = logging.getLogger(__name__)

# How much of a UUID a correlation id keeps, as `seahaven/openenv/env.py` keeps
# it: long enough that two ids in one server log never collide, short enough to
# read out of a client-side "internal error" and paste into a grep.
CORRELATION_ID_LENGTH = 12

# What a result's text block is indented by. A model reads these, and an object
# printed on one line is the same JSON with the structure hidden.
INDENT = 2


def listing(entries: list[dict[str, Any]]) -> list[types.Tool]:
    """`Instance.tools()` as MCP tools.

    The rename functional spec §5.3 asks for is the model's own: `types.Tool`
    names the field `input_schema` and serialises it as `inputSchema`. Name and
    description are passed through unchanged, and no `output_schema` is set --
    Seahaven tools do not declare one.
    """
    return [
        types.Tool(
            name=entry["name"],
            description=entry["description"],
            input_schema=entry["input_schema"],
        )
        for entry in entries
    ]


def success(result: Any) -> types.CallToolResult:
    """A tool's return value: JSON text, and the same object again when it is one.

    `structured_content` is set only for an object, because `structuredContent`
    is defined as one. The SDK validates it against an output schema only when
    the tool declares one, and these declare none, so the pairing is safe.
    """
    value = serialise(result)
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=_dump(value))],
        structured_content=value if isinstance(value, dict) else None,
    )


def failure(error: ToolError) -> types.CallToolResult:
    """A tool failure as a result and not as a protocol error.

    The agent is meant to read it and recover, which a JSON-RPC error does not
    let it do, so the triple goes in the text block and `isError` says what it
    is. This is the split `SeahavenClient.call` already makes.
    """
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=_dump(error.to_dict()))],
        is_error=True,
    )


def _dump(value: Any) -> str:
    """One JSON rendering for every text block this module writes.

    `ensure_ascii=False`, so a world's own text reaches the model as the author
    wrote it rather than as escapes; the transport is UTF-8.
    """
    return json.dumps(value, indent=INDENT, ensure_ascii=False)


def internal_error(correlation: str) -> ToolError:
    """The generic error, built fresh each time so no caller can edit the next one's.

    The code and the message are fixed, so a client that matches on error text is
    not perturbed by an id that changes every call; the id rides in `details`,
    where it is the one thing that leads from this result to the traceback on
    stderr.
    """
    return ToolError(INTERNAL_ERROR_CODE, INTERNAL_ERROR_MESSAGE, {"id": correlation})


def log_failure(tool: str, what: str) -> str:
    """Log the exception being handled, with its traceback, and answer its id.

    The id is what makes a client-side "internal error" greppable: it is on the
    result or the error frame and on this line, and nowhere else.

    `seahaven/openenv/env.py` has a `_log_failure` of its own that this
    deliberately does not share: that one logs an episode id and a call ordinal
    from the OpenEnv session, neither of which exists here, and a helper general
    enough for both would say less than either.
    """
    correlation = uuid.uuid4().hex[:CORRELATION_ID_LENGTH]
    _log.exception("[%s] tool %s %s", correlation, tool, what)
    return correlation
