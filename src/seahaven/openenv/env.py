"""The environment class: one Seahaven instance behind one OpenEnv session.

`SeahavenEnv` is the whole server side of the wire. OpenEnv makes one of these
per connected session, on that session's own single thread, and every `reset`,
`step`, `state` and `close` it receives arrives here. The mapping is one line
long -- a session is an instance -- and everything below is what that costs:

- `reset` makes the instance, destroying the session's previous one first, so a
  session that resets twice never holds two.
- `step` is `Instance.call`, with the errors rendered rather than raised. A
  `ToolError` is data the agent reads; anything else is either the author's bug
  (a `WorldBug`, which propagates and fails the frame loudly) or an accident
  (answered with a fixed generic error). Both of those are logged here with
  their traceback and a correlation id, and what goes out says only "internal
  error" and that id, so no message written for an author reaches the wire.
- `state` is `Instance.state()`: the whole state document, typed by
  `SeahavenState` and carrying OpenEnv's `step_count` beside it. Before the
  first `reset` there is no instance and the world's pinned formatter answers
  with none.
- `close` destroys the instance, which is what makes a dropped connection cost
  nothing.

Nothing here owns a thread, a queue or a timeout. The session's thread is
OpenEnv's, the concurrency gate is the framework's, and `timeout_s` is accepted
and ignored because Seahaven does not bound a call.
"""

import inspect
import logging
import uuid
import warnings
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal

from openenv.core.env_server.interfaces import Environment
from openenv.core.env_server.mcp_types import (
    CallToolAction,
    CallToolObservation,
    ListToolsAction,
    ListToolsObservation,
)
from openenv.core.env_server.mcp_types import ToolError as WireToolError
from openenv.core.env_server.mcp_types import ToolErrorType as WireToolErrorType
from openenv.core.env_server.types import (
    Action,
    EnvironmentMetadata,
    Observation,
    ResetRequest,
    SchemaResponse,
    State,
)
from pydantic import BaseModel, ConfigDict, Field, create_model

from seahaven.call import name_of, serialise
from seahaven.clock import ClockMode
from seahaven.errors import (
    INTERNAL_ERROR_CODE,
    INTERNAL_ERROR_MESSAGE,
    SeahavenError,
    ToolError,
    WorldBug,
)
from seahaven.instances import Instance
from seahaven.state import BUILTIN_FORMATS, document
from seahaven.world import RegisteredStartupHook, World

# Pydantic publishes a PEP 695 alias as a `$ref` to a definition of its own; the
# `Literal` behind it publishes the enum inline, the shape `state_format` has.
# The checker keeps the alias, which it cannot see through `__value__`.
if TYPE_CHECKING:
    _ClockModeField = ClockMode
else:
    _ClockModeField = ClockMode.__value__

__all__ = [
    "FileRef",
    "FixtureRef",
    "NodeRef",
    "SeahavenEnv",
    "SeahavenObservation",
    "SeahavenResetRequest",
    "SeahavenSchemaResponse",
    "SeahavenState",
    "WorldRef",
]

_log = logging.getLogger(__name__)

README_NAME = "README.md"

# Where the Seahaven triple travels. `error` itself carries OpenEnv's two-key
# shape, which forbids extra keys, and `Observation` forbids a sibling field, so
# `metadata` is the only declared free-form space on the frame. The key is
# namespaced because `metadata` is shared: `reset` already writes `fixture`,
# `now`, `clock_mode` and `tools` at its top level, and upstream may add keys of
# its own.
SEAHAVEN_ERROR_KEY = "seahaven_error"

# How much of a UUID a correlation id keeps. Long enough that two ids in one
# server log never collide, short enough to read out of a client-side "internal
# error" and paste into a grep.
CORRELATION_ID_LENGTH = 12


class SeahavenObservation(CallToolObservation):
    """What a tool call answers with: exactly one of `result` and `error`.

    This is the shape of a `step` on a `CallToolAction`, and of nothing else.
    `reset` answers a plain `Observation` with `fixture`, `now`, `clock_mode`
    and `tools` in its `metadata`: no tool was called, so nothing there is a
    tool's result.

    It subclasses OpenEnv's `CallToolObservation` rather than `Observation` so
    that upstream's MCP `tools/call` path, which checks for that type, keeps
    working. `/mcp` itself is refused, but a `{"type": "mcp"}` frame on a `/ws`
    connection reaches the same check (`http_server.py` line 1020 in openenv
    0.5.0), which answers an internal error to anything that is not one. That
    check is on the step path only; `reset` has its own handler and no check.

    `error` is a tool's own error: data the agent reads, which never closes the
    session. OpenEnv's docstrings say the field is only for transport failures,
    but its own `MCPEnvironment` answers a failed tool call with
    `ToolErrorType.EXECUTION_ERROR` ("tool ran but failed"), so this follows the
    convention their code establishes rather than the one their docstrings
    describe.

    The field is upstream's `ToolError` itself -- `{error_type, message}`, and
    nothing else, because that model forbids extra keys. Every Seahaven error is
    therefore a frame upstream's own client parses: `CallToolObservation(**...)`
    validates, and `MCPToolClient.call_tool` raises a legible `RuntimeError`
    naming the message and the type rather than a pydantic `ValidationError`.

    The world's own `{"code", "message", "details"}` -- the same triple
    `ToolError.to_dict` gives in process -- travels in
    `metadata["seahaven_error"]`, and `seahaven_error` below reads it. It cannot
    travel as a field of its own: `Observation` forbids extra keys too, so a
    sibling field would break a strict parse exactly as the old shape did.
    `metadata` is a declared `Dict[str, Any]` and is the only free-form space on
    the frame.
    """

    # All three carry descriptions of their own rather than the inherited ones,
    # because `GET /schema` publishes them and the inherited text describes
    # OpenEnv's convention rather than Seahaven's: `result` arrives documented
    # as "Tool-specific result (may include tool errors)" and `error` as
    # "Transport/framework error if call failed", which are precisely the two
    # things this class swaps. Redeclaring a field *replaces* its schema entry,
    # so a redeclaration without a `description` publishes none at all: review
    # round 1 found that on `result`, and round 2 found it still standing one
    # field over, on `tool_name` and `error`. Every redeclared field now says
    # what it means, and `test_server.py` asserts all three off a live
    # `GET /schema` so the next redeclaration cannot drop one quietly.
    tool_name: str = Field(default="", description="The tool that was called.")
    result: Any | None = Field(
        default=None, description="The tool's result. A tool error travels in `error`, never here."
    )
    error: WireToolError | None = Field(
        default=None,
        description=(
            "The tool's own error, as OpenEnv's `{error_type, message}`, or null when the "
            "call succeeded. The world's own code and details travel in "
            '`metadata["seahaven_error"]`. A tool error is data the agent reads: it never '
            "ends the session."
        ),
    )
    # Inherited from `Observation`: `done` (always `False` here), `reward`
    # (always `None`) and `metadata`.

    @property
    def seahaven_error(self) -> dict[str, Any] | None:
        """The world's `{"code", "message", "details"}`, or `None` on a call that worked.

        Read from this observation's own `metadata`, which is the copy that
        travels inside the observation. OpenEnv's serializer copies a non-empty
        `metadata` to the top level of the wire envelope as well
        (`serialization.py` lines 183-184 in openenv 0.5.0, deliberately, for a
        client that reads the generic format), so the triple is on an error frame
        twice and a client should pick one. This is the one Seahaven's own client
        reads.
        """
        error = self.metadata.get(SEAHAVEN_ERROR_KEY)
        return error if isinstance(error, dict) else None


class WorldRef(BaseModel):
    """The root world a document came from: `functional_spec.md` §3.1's `world`."""

    name: str = Field(description="The world's name, as `World(name=...)` gives it.")
    version: str = Field(
        description=(
            "The root world's version. A judge assumes two documents with the same name and "
            "version came from the same schema and the same tools."
        )
    )


class NodeRef(BaseModel):
    """One node of an instance's composition: `functional_spec.md` §3.1's `composition[path]`.

    `NodeReport`'s fields less `path`, which is the key this is stored under.
    """

    world: str = Field(description="The name of the world this node runs.")
    world_version: str = Field(description="That world's version, as it was added.")
    scope: str | None = Field(description="The node's scope, or null for a node that has none.")
    aliases: list[str] = Field(
        description="Every other path that reaches this same node, so a shared store is visible."
    )
    schema_hash: str = Field(
        description="The SHA-256 of the node's schema, which is what invalidates a fixture."
    )
    frozen_world_version: str | None = Field(
        description=(
            "The world version recorded in the fixture this node was built from, when it "
            "differs from `world_version`; null otherwise."
        )
    )


class FileRef(BaseModel):
    """One node's starting file: `functional_spec.md` §3.1's `fixture.nodes[path]`."""

    file_sha256: str = Field(
        description="The SHA-256 of that node's frozen database file, from the fixture's sidecar."
    )


class FixtureRef(BaseModel):
    """The fixture an episode started from: `functional_spec.md` §3.1's `fixture`."""

    id: str = Field(description="The fixture's id, as `reset(fixture=...)` named it.")
    nodes: dict[str, FileRef] = Field(
        description=(
            "Each node's starting file, keyed by the same path `composition` uses. With "
            "`composition`, this is the lookup for the state the episode started from."
        )
    )


class SeahavenState(State):
    """The state document, plus OpenEnv's `step_count`.

    Everything but `state` and `step_count` is the framework's envelope, written
    identically under every format (`functional_spec.md` §3.1); `state` is the
    formatter's output, and `format` names its shape and nothing else. So
    `state().state` is what the format produced, and
    `state().model_dump(exclude={"step_count"})` is the document as
    `inst.state()` answers it in process.

    `episode_id` and `step_count` are the base's: `step_count` counts everything
    the session asked for, tool listings included, and is *not* the document's
    `call_count`. Before the first `reset` there is no instance, so the fields an
    instance would have answered are `null` and the world's pinned formatter runs
    with none (`functional_spec.md` §3.5).

    The base's `extra="allow"` is kept, so a newer server can talk to an older
    client: a reader ignores the envelope fields it does not know.

    `path` is the join key across the whole document: `composition[p]` describes
    a node, `fixture.nodes[p]` the file it started from, and a log record's
    `world` names one.
    """

    # Descriptions for the same reason the observation's fields have them: the
    # two fields this class *inherits* arrive described by OpenEnv, and the ones
    # declared here with nothing said about them would be the round-1 and
    # round-2 finding a third time, one class over. They reach a client because
    # `app()` passes this class to `create_app` as `state_cls`, which openenv
    # 0.5.0 added and which is what closed
    # https://github.com/huggingface/OpenEnv/issues/1155; under 0.4.2 `GET
    # /schema` answered `State.model_json_schema()` for every environment and no
    # subclass's fields were published at all.
    #
    # Declared in `functional_spec.md` §3.1's order, less `episode_id`, which the
    # base carries. A field the document always writes is required here; the six
    # it answers `null` for default to `None`, so reading one off a frame that
    # left it out answers `None` rather than raising.
    format: str = Field(
        description=(
            "The format that produced `state`, as `<family>/<major>`. A reader checks this "
            "before reading `state`, and keys on nothing else for it."
        )
    )
    seahaven_version: str = Field(
        description="The Seahaven version of the producing process. Informational only."
    )
    world: WorldRef = Field(description="The root world this session is connected to.")
    composition: dict[str, NodeRef] | None = Field(
        default=None,
        description=(
            "Every node of the instance, keyed by canonical path and root first, or null "
            "before the first reset. Never agent-facing."
        ),
    )
    fixture: FixtureRef | None = Field(
        default=None,
        description="The fixture the instance was made from, or null for a blank one.",
    )
    seed: int | None = Field(
        default=None, description="The seed `reset` was given, or null if it was given none."
    )
    now: str | None = Field(
        default=None,
        description=(
            "The instance's clock when this document was produced, as an ISO-8601 instant, or "
            "null before the first reset."
        ),
    )
    clock_mode: _ClockModeField | None = Field(
        default=None,
        description=(
            "How the instance's clock moves: fixed, tick, running or wall; null before the "
            "first reset."
        ),
    )
    startup: dict[str, Any] | None = Field(
        default=None,
        description=(
            "The world's own startup keywords -- reset's `startup` -- rendered as JSON when "
            "the instance was created: a hook receives the caller's value and this carries its "
            "JSON rendering. Empty when there were none, null before any reset."
        ),
    )
    call_count: int = Field(
        description=(
            "How many calls have been dispatched to the instance. Not `step_count`, which also "
            "counts tool listings. The last call's ordinal is one less than this."
        )
    )
    state: dict[str, Any] = Field(
        description=(
            "The formatter's output, and the only part of this model `format` describes. Left "
            "untyped because its shape is the format's, not the framework's."
        )
    )


# The console lays its reset form out in `properties` order, and pydantic puts
# the inherited OpenEnv fields first. The fields not named here keep their own
# order, after these.
RESET_ORDER = ("fixture", "startup", "now", "clock_mode", "state_format")


def _order_properties(schema: dict[str, Any], leading: tuple[str, ...]) -> None:
    properties = schema["properties"]
    first = {name: properties[name] for name in leading if name in properties}
    schema["properties"] = first | {
        name: value for name, value in properties.items() if name not in first
    }


class SeahavenResetRequest(ResetRequest):
    """The reset message: OpenEnv's two fields and the keywords `SeahavenEnv.reset` adds.

    Used for its JSON schema only; `SeahavenEnv.reset` validates a reset exactly as it did.
    """

    # `forbid` over the base's `allow`, because `SeahavenEnv.reset` refuses every
    # key it does not name. `control_tools` is one it names and ignores, so it is
    # not a field: publishing it would advertise a control that does nothing.
    model_config = ConfigDict(
        extra="forbid", json_schema_extra=lambda schema: _order_properties(schema, RESET_ORDER)
    )

    fixture: str | None = Field(
        default=None,
        description=(
            "The fixture to start from, by id. Null starts a blank instance from the world's "
            "schema."
        ),
    )
    now: str | None = Field(
        default=None,
        description=(
            "Where the instance's clock starts, as an ISO-8601 instant, e.g. 2024-03-05T12:00:00Z."
        ),
    )
    clock_mode: _ClockModeField | None = Field(
        default=None,
        description=(
            "How the instance's clock moves: fixed, tick, running or wall. Null uses the "
            "world's default."
        ),
    )
    state_format: str | None = Field(
        default=None,
        description=(
            "The format of this episode's state documents. Null uses the world's pinned format."
        ),
    )
    startup: dict[str, Any] | None = Field(
        default=None, description="The world's own startup keywords, passed to its startup hooks."
    )

    @classmethod
    def for_world(cls, world: World) -> type[SeahavenResetRequest]:
        """This model narrowed to one world: its fixtures, its formats, its startup keywords.

        The fixture ids are read from the fixtures directory on every
        `model_json_schema()`, so a fixture frozen while a server runs is
        published without a restart. Everything else is fixed here, and a
        startup hook the schema cannot describe is a `WorldBug` here.
        """
        base = cls.model_fields
        # A world cannot register a built-in's name, so the two lists never overlap.
        formats = (*sorted(BUILTIN_FORMATS), *sorted(world.state_formats))

        def fill_fixture_ids(schema: dict[str, Any]) -> None:
            ids = [fixture.id for fixture in world.fixtures()]
            # Null-only rather than an empty `enum`, which is not valid JSON Schema.
            schema["anyOf"] = [{"type": "string", "enum": ids}] if ids else []
            schema["anyOf"].append({"type": "null"})

        # The subclass takes the base's own name, so the schema's `title` is
        # `SeahavenResetRequest` and no generated name reaches the wire. No
        # `__config__`: `create_model` refuses one beside `__base__`, and the base's
        # `extra="forbid"` is inherited.
        return create_model(
            "SeahavenResetRequest",
            __base__=cls,
            fixture=(
                Annotated[
                    str | None,
                    Field(
                        description=base["fixture"].description, json_schema_extra=fill_fixture_ids
                    ),
                ],
                None,
            ),
            state_format=(
                Literal[formats] | None,  # ty: ignore[invalid-type-form]
                Field(default=None, description=base["state_format"].description),
            ),
            startup=(
                _startup_model(world) | None,
                Field(default=None, description=base["startup"].description),
            ),
        )


class SeahavenSchemaResponse(SchemaResponse):
    """`GET /schema`'s answer plus the reset message, as OpenEnv would answer with a `reset_cls`."""

    reset: dict[str, Any] = Field(description="JSON schema for the reset message")


class SeahavenEnv(Environment[Action, Observation, SeahavenState]):
    """One world, one session, one instance.

    The instance lives on the environment object and OpenEnv makes one
    environment object per session, so instance-per-session is true by
    construction rather than by bookkeeping.

    The observation parameter is the base `Observation` because the class
    answers three shapes: a plain `Observation` from `reset`, and a
    `SeahavenObservation` or a `ListToolsObservation` from `step`, each of which
    subclasses it. `GET /schema` does not read this parameter: `app()` in
    `seahaven.openenv` passes `SeahavenObservation` to `create_app` as the
    published observation class, which is the shape of a tool call.
    """

    # Every instance has its own directory, its own connection and its own lock,
    # and the framework holds no per-world mutable state outside the instance
    # manager, which is under its own lock. Sessions are independent.
    SUPPORTS_CONCURRENT_SESSIONS = True

    def __init__(self, world: World, *, include_control_tools: bool) -> None:
        super().__init__()
        self.world = world
        self.include_control_tools = include_control_tools
        self._instance: Instance | None = None
        self._steps = 0

    @property
    def instance(self) -> Instance | None:
        """The session's live instance, or `None` before the first `reset`."""
        return self._instance

    def reset(
        self,
        seed: int | None = None,
        episode_id: str | None = None,
        *,
        fixture: str | None = None,
        now: str | None = None,
        # `ClockMode` and not `str`, which a wire value may be: OpenEnv binds by
        # name only, and `create` refuses a value that is not a mode at runtime.
        clock_mode: ClockMode | None = None,
        state_format: str | None = None,
        control_tools: Any = None,
        startup: Mapping[str, Any] | None = None,
        **unknown: Any,
    ) -> Observation:
        """Start an episode: a fresh instance, and the session's previous one gone.

        Answers a plain `Observation` whose `metadata` carries `fixture` (the
        fixture the instance was made from, or `None` for a blank one), `now`
        (the instance's clock as an ISO-8601 instant), `clock_mode` (how that
        clock moves) and `tools` (how many the instance lists). It is `metadata`
        and not a tool result because no tool was called, and because OpenEnv's
        serializer copies a non-empty `metadata` to the top level of the wire
        envelope, where a client that knows nothing of Seahaven's observation
        classes still finds it.
        `done` is `False` and `reward` is `None`, as on every observation here.

        `fixture=None` is a blank instance built from the world's DDL, whose clock
        starts at the wall time unless `now=` says otherwise; a fixture's clock
        starts at the fixture's own `now`, or at a later `now=`. `clock_mode=`
        is the mode the clock runs in, the world's default when it is not given.
        `state_format=` answers this episode's `state` message in another of the
        root world's formats, in place of the world's pin. An unknown mode or an
        unregistered format is refused before anything is copied. Those are the
        instance's own rules and not a second set: everything here is passed
        straight through, including `startup`, the world's own keyword
        namespace.

        This signature is the reset wire schema. OpenEnv has no schema of its
        own for a reset message: its server introspects this method to bind the
        client's body, so a parameter added here is a key a client can send, and
        `tests/test_env.py` pins the set for that reason. `**unknown` takes
        every other key the client sent and refuses it, because the wire is
        where a caller is furthest from the code and least able to guess: a
        startup keyword travels inside `startup`, and one sent at the top level
        is an error rather than a key that quietly does nothing. The refusal
        names the stray keys and the names this method does take, so a
        misspelled `fixure` is answered with `fixture`. Nothing but the client's
        own body reaches this method, so a key in `**unknown` is always one the
        client wrote.

        `control_tools` is named so that a reset message carrying it binds here,
        where it is read by nothing, rather than reaching a hook. The instance
        takes the server's own `include_control_tools` and never the client's
        word.

        The refusal is the one check `reset` can make from the message alone, so
        it runs before `_forget()` and the session keeps the episode it had,
        which is what the same call does in process: an unknown keyword fails at
        argument binding, before the body. Everything the world judges -- an
        unknown startup keyword, a `now=` before the fixture's -- is judged
        while the instance is being made, after the old one is gone.

        The old instance is destroyed *before* the new one is made, so the
        session never holds two at once. A creation that then fails leaves the
        session exactly as a fresh one -- no instance, no episode, no steps -- and
        open for another `reset`: the exception propagates, and OpenEnv answers
        the frame with `EXECUTION_ERROR` rather than closing the connection.
        """
        # Three key names never reach this method: `self`, `session_id` and
        # `func` are the arguments of OpenEnv's own call frame
        # (`_run_in_session_executor` in `openenv/core/env_server/http_server.py`,
        # verified against 0.5.0, which now receives them because a `**kwargs`
        # signature makes it forward every key). A client sending one gets
        # upstream's `TypeError` as the error frame instead of the refusal below,
        # and the session survives it either way.
        if unknown:
            # The names it does take, read off the signature so the message cannot
            # drift from it: the answer to a misspelled `fixure` is `fixture`, and
            # a list of the real names is where the caller finds it.
            takes = [
                name
                for name, parameter in inspect.signature(self.reset).parameters.items()
                if parameter.kind is not inspect.Parameter.VAR_KEYWORD
            ]
            raise WorldBug(
                f"reset does not take {', '.join(sorted(unknown))}. It takes "
                f"{', '.join(takes)}; a world's own keywords go inside startup={{...}}"
            )
        self._forget()
        # Minted before the instance, because the instance keeps it: every
        # document this episode produces reports the episode id the harness gave
        # (`functional_spec.md` §3.1), which is the join key back to its trace.
        # `world.instance(...)` deliberately has no `episode_id` parameter -- in
        # process an instance *is* an episode and its id is the episode's
        # (`architecture.md` §6) -- so this one caller, the only place a session
        # id exists, goes to the manager `world.instance` itself calls. All the
        # public verb adds is its refusal to build an instance from inside a tool
        # call, and no OpenEnv frame can reach this method from inside one. The
        # session keeps no copy of the id either; the instance holds it and the
        # document reports it from there.
        instance = self.world._instances().create(
            fixture,
            seed=seed,
            now=now,
            clock_mode=clock_mode,
            state_format=state_format,
            episode_id=episode_id or str(uuid.uuid4()),
            control_tools=self.include_control_tools,
            startup=startup,
        )
        self._instance = instance
        return Observation(
            metadata={
                "fixture": instance.fixture,
                "now": instance.clock.iso(),
                "clock_mode": instance.clock.mode,
                "tools": len(instance.tools()),
            }
        )

    def step(
        self,
        action: Action,
        timeout_s: float | None = None,
        **kwargs: Any,
    ) -> SeahavenObservation | ListToolsObservation:
        """Run one action. Two kinds arrive, whatever `action_cls` says.

        `ListToolsAction` is answered first and without an instance: a tool list
        is the world's, not an episode's, and OpenEnv's own MCP contract is that
        discovery does not require a `reset`. Control tools are never in it,
        whatever `include_control_tools` says -- the flag makes them *callable*,
        never advertised.

        A `CallToolAction` needs an instance, and naming a control tool on a
        session the flag did not start is refused by the instance as
        `UnknownTool`, in the same words an unregistered name earns: an agent
        must not be able to tell the two apart.

        `timeout_s` is accepted and ignored. Seahaven does not bound a call, and a
        bound that silently did nothing would be worse than none.
        """
        # Every step counts, including one that is refused: the count is of what
        # the session asked for, which is what a caller reconciling a trajectory
        # against a transcript needs it to be.
        self._steps += 1
        if isinstance(action, ListToolsAction):
            return ListToolsObservation(tools=self._listing())
        if isinstance(action, CallToolAction):
            return self._call(action)
        raise WorldBug(
            f"a Seahaven environment steps on a CallToolAction or a ListToolsAction, "
            f"not on {type(action).__name__}"
        )

    @property
    def state(self) -> SeahavenState:
        """The state document, plus the session's step count.

        With an instance this is exactly `inst.state()`: the framework's envelope
        and the instance's format, built once and in one place, so the wire and
        an in-process caller cannot answer differently. `composition` is what the
        instance is running against, node by node, so an eval over the wire can
        tell a tree from a leaf and a shared store from two. It is on `state` and
        nowhere else: `state` is not an observation, and no agent reads one.

        Before the first `reset` there is no instance, and the *world's* pinned
        formatter runs with `None` (`functional_spec.md` §3.5). There is no
        default and no framework-built `state`: a world that pins a format of its
        own decides what "no episode yet" looks like, and one whose formatter
        refuses `None` makes this a `WorldBug`, which is the formatter author's
        contract to keep.
        """
        instance = self._instance
        if instance is not None:
            return SeahavenState(step_count=self._steps, **instance.state())
        # `pinned_state_format`, and not `state_format`: the latter is the
        # world's registration decorator, because one name cannot be both a
        # string and a decorator (`functional_spec.md` §6).
        pin = self.world.pinned_state_format
        return SeahavenState(
            step_count=self._steps,
            **document(self.world, None, pin, self.world.resolve_state_format(pin)),
        )

    def close(self) -> None:
        """Destroy the instance, if there is one. Idempotent.

        OpenEnv calls this when the session ends, on the session's own thread, so
        a dropped client releases its fixture copy and its connections rather than
        holding them until the process exits.
        """
        self._forget()

    def get_metadata(self) -> EnvironmentMetadata:
        """The world's identity and its README, which is the card a hub shows.

        The one-line description is the world's own `description=`, an explicit
        argument rather than anything read out of the README. A world that gives
        none -- or gives a blank string -- publishes `Seahaven world <name>`,
        which is a fallback and never a wrong sentence.
        """
        readme = self._readme()
        # Blank, not just empty: a description of nothing but spaces would put a
        # blank-looking line on the card, which is the one thing the fallback is
        # here to prevent. The test is on the stripped string and the published
        # value is the unstripped one, so a description with content in it
        # reaches the card exactly as its author wrote it.
        given = self.world.description or ""
        return EnvironmentMetadata(
            name=self.world.name,
            version=self.world.version,
            description=given if given.strip() else f"Seahaven world {self.world.name}",
            readme_content=readme,
        )

    def _call(self, action: CallToolAction) -> SeahavenObservation:
        """One tool call, with every failure rendered rather than raised.

        Three outcomes and one rule each. A `ToolError` -- which includes the
        framework's `UnknownTool`, `ArgumentError` and `DbError`, and every error
        a world defines -- is what the agent is meant to read, and goes on the
        observation. Any other `SeahavenError`, a `WorldBug` above all, is the
        author's and propagates: an eval that graded against "internal error"
        observations while the world was broken is the failure this class exists
        to prevent (`functional_spec.md` §5.3). The catch is `SeahavenError` and
        not `WorldBug` on purpose, and the rule is that a framework error nobody
        classified is a framework bug: a new error class that reaches an agent as
        data because it was never sorted is the worse of the two failures.
        Anything else is an accident: it is logged with its traceback and answered
        with the fixed generic error, so that a world with no error handler still
        cannot leak engine text.

        The second and third outcomes are scrubbed here, because this is the wire
        boundary and nothing deeper is one. A propagating `SeahavenError` is
        re-raised with a message that says nothing: OpenEnv renders a failed frame
        as `WSErrorResponse(data={"message": str(e)})` (`http_server.py` lines
        1720-1726 in openenv 0.5.0), so whatever the author wrote for themselves
        would otherwise go out verbatim. It still hard-fails the frame, which is
        the point -- turning it into an observation would let an eval quietly
        score a broken world. In process, `Instance.call` still raises the real
        error, which is what a world's own tests read.
        """
        instance = self._instance
        if instance is None:
            raise WorldBug("reset first")
        name = action.tool_name
        # Read before the call so the log can say which entry of the state
        # document's call log this failure is: the instance takes an ordinal as it
        # dispatches, so a count that moved means this call has a record and a
        # count that did not means it failed before it reached the world.
        dispatched_before = instance.call_count
        try:
            # `Instance.call` answers with the object the tool returned, so that
            # a host tool handed a model is handed a model (architecture section
            # 8.4); this is the layer that owes the wire its rendering. A world's
            # tool has already been through `serialise` once, inside the call's
            # own transaction, so that a result no wire carries rolled the call
            # back rather than reaching here; a control tool bypasses `invoke`
            # altogether and `control.dispatch` renders its own. Either way this
            # renders an already-proved value, which is the redundancy section
            # 8.4 chose over changing `Handler`.
            result = serialise(instance.call(name, **action.arguments))
            return SeahavenObservation(tool_name=name, result=result)
        except ToolError as error:
            return _error_observation(name, error)
        except SeahavenError as error:
            # Logged here and nowhere else: before this, a framework error
            # propagated unlogged and the only copy of its text was the one on the
            # wire. Scrubbing that copy takes the last one away with it.
            correlation = self._log_failure(
                name, instance, dispatched_before, "raised a framework error"
            )
            raise WorldBug(f"{INTERNAL_ERROR_MESSAGE} ({correlation})") from error
        except Exception:
            correlation = self._log_failure(
                name, instance, dispatched_before, "failed with an unhandled exception"
            )
            return _error_observation(name, _internal_error(correlation))

    def _log_failure(self, name: str, instance: Instance, dispatched_before: int, what: str) -> str:
        """Log a failure with its traceback, and answer the correlation id.

        The id is what makes a client-side "internal error" greppable: it is on
        the observation or the error frame and on this line, and nowhere else. The
        rest of the line is the join back to the state document -- the world, the
        instance, the episode the harness named, and which entry of the call log
        this was.

        `seahaven/mcp/wire.py` has a `log_failure` of its own that deliberately
        does not share this one: an MCP session has no episode id and no call
        ordinal to log, and a helper general enough for both would say less than
        either.
        """
        correlation = uuid.uuid4().hex[:CORRELATION_ID_LENGTH]
        _log.exception(
            "[%s] tool %s %s on instance %s of world %s (episode %s, call %s)",
            correlation,
            name,
            what,
            instance.id,
            self.world.name,
            instance.episode_id,
            instance.call_count - 1
            if instance.call_count > dispatched_before
            else "not dispatched",
        )
        return correlation

    def _listing(self) -> list[dict[str, Any]]:
        """The tool list: the instance's when there is one, the world's when there is not.

        `Instance.tools()` is the world's sealed composition, so the two answers
        are the same list; a test pins that they are, because the instance is the
        spelling `components/openenv.md` gives and the world is the only thing
        there is to ask before a `reset`. Read from the composition and not from
        `world.tools`, which is the root's own registry and, for a world that adds
        worlds, is not the surface an agent sees.
        """
        instance = self._instance
        if instance is not None:
            return instance.tools()
        return [
            entry.tool.listing() | {"name": entry.name}
            for entry in self.world.composition().tools.values()
        ]

    def _readme(self) -> str:
        """The world package's top-level `README.md`, or empty when there is none.

        Found beside the fixtures directory, which is the same root derivation
        `World` uses for that directory: the world's project root in a checkout,
        and the package's own directory in an installed wheel. Unreadable is the
        same as absent -- metadata is not where a world should fail.
        """
        path = Path(self.world.fixtures_dir).parent / README_NAME
        try:
            # `utf-8-sig`, not `utf-8`: a README written by a Windows editor
            # begins with a byte-order mark, and read as plain UTF-8 that mark
            # becomes the first character of `readme_content` -- an invisible
            # character at the head of the published card, in front of its
            # opening heading or fence. This codec drops it, which is the only
            # place it can be dropped: `readme_content` is published exactly as
            # it is read here.
            return path.read_text(encoding="utf-8-sig")
        # Unparenthesized on the formatter's insistence, not as a flourish:
        # PEP 758 allows it where nothing is bound with `as`, and `ruff format`
        # -- a gate this project runs before every commit -- removes the
        # parentheses again if they are written. `world.py` catches this same
        # pair with parentheses because it binds the error; where the binding
        # goes, so do the brackets.
        except OSError, UnicodeDecodeError:
            return ""

    def _forget(self) -> None:
        """Destroy the instance, which is the episode. The only way a session ends one."""
        instance = self._instance
        self._instance = None
        self._steps = 0
        if instance is not None:
            instance.destroy()


def _error_observation(name: str, error: ToolError) -> SeahavenObservation:
    """One error as the two things a frame carries: upstream's shape, and the triple.

    `error_type` is the class's and never the world's, and `message` is the
    world's own text unchanged. The world's code is not folded into that message:
    it reaches the agent whole in `metadata["seahaven_error"]`, which upstream's
    own client preserves, and the framework's errors already say in prose what
    their code says -- "unknown_tool: unknown tool: rows" is what folding them
    together reads like.
    """
    return SeahavenObservation(
        tool_name=name,
        error=WireToolError(
            error_type=WireToolErrorType(type(error).error_type.value), message=error.message
        ),
        metadata={SEAHAVEN_ERROR_KEY: error.to_dict()},
    )


def _internal_error(correlation: str) -> ToolError:
    """The generic error, built fresh each time so no caller can edit the next one's.

    The code and the message are fixed, so an eval that matches on error text is
    not perturbed by an id that changes every call; the id rides in `details`,
    where it is the one thing that leads from this observation to the traceback in
    the server's log.
    """
    return ToolError(INTERNAL_ERROR_CODE, INTERNAL_ERROR_MESSAGE, {"id": correlation})


def _startup_model(world: World) -> type[BaseModel]:
    """The startup keywords of every hook in the world's tree, as one model.

    A keyword is built from its hook parameter the way a tool argument is: the
    annotation is the type (`Any` when there is none), the default is the default,
    and one with no default is required. Keywords come from every node, because a
    keyword in `reset(startup=...)` reaches every hook in the tree that names it,
    so the names are exactly `composition.accepted_startup_kwargs`.

    A keyword bound on a hook's node (`add_world(..., startup={...})`) never
    reaches that hook from the caller: the bound value is applied last. So that
    hook's parameter neither makes the keyword required nor conflicts with
    another hook's annotation. A keyword that only bound hooks name is still
    accepted by the server, and ignored, so it is published as optional, with
    no type and no default.
    """
    fields: dict[str, tuple[Any, Any]] = {}
    owners: dict[str, RegisteredStartupHook] = {}
    bound_only: dict[str, RegisteredStartupHook] = {}
    open_ended = False
    for node in world.composition().nodes:
        for hook in node.world.startup_hooks:
            open_ended = open_ended or hook.takes_var_kwargs
            for parameter in _signature(hook).parameters.values():
                if parameter.kind is not inspect.Parameter.KEYWORD_ONLY:
                    continue
                name = parameter.name
                if name in node.bound_startup:
                    bound_only.setdefault(name, hook)
                    continue
                annotation = (
                    Any if parameter.annotation is inspect.Parameter.empty else parameter.annotation
                )
                default = ... if parameter.default is inspect.Parameter.empty else parameter.default
                if name not in fields:
                    fields[name] = (annotation, default)
                    owners[name] = hook
                elif fields[name][0] != annotation:
                    raise WorldBug(
                        f"startup keyword {name!r} is annotated {fields[name][0]} by "
                        f"{name_of(owners[name].fn)} and {annotation} by {name_of(hook.fn)}; one "
                        f"keyword reaches both hooks, so give them one type -- one shared "
                        f"object, such as a module-level alias, because two `Annotated[..., "
                        f"Field(...)]` written out separately are never equal"
                    )
    for name, hook in bound_only.items():
        if name not in fields:
            fields[name] = (Any, Field(default=None, json_schema_extra=_drop_default))
            owners[name] = hook
    for name, hook in owners.items():
        if name.startswith("_") or name == "model_config":
            raise WorldBug(
                f"startup hook {name_of(hook.fn)}: keyword {name!r} cannot be published in the "
                f"reset schema, because pydantic reserves the name; rename it"
            )
    config = ConfigDict(extra="allow" if open_ended else "forbid")
    try:
        model = _create_startup_model(config, fields)
        model.model_json_schema(mode="validation")
    except Exception as error:
        name = _unschemable_keyword(fields, config)
        where = f"startup hook {name_of(owners[name].fn)}: keyword {name!r}" if name else "startup"
        raise WorldBug(f"{where}: no JSON schema: {error}") from error
    missing = [name for name in fields if name not in model.model_fields]
    if missing:
        name = missing[0]
        raise WorldBug(
            f"startup hook {name_of(owners[name].fn)}: keyword {name!r} cannot be published in "
            f"the reset schema; rename it"
        )
    return model


def _drop_default(schema: dict[str, Any]) -> None:
    schema.pop("default", None)


def _create_startup_model(
    config: ConfigDict, fields: dict[str, tuple[Any, Any]]
) -> type[BaseModel]:
    with warnings.catch_warnings():
        # A keyword named `schema`, `copy` or `json` is a fine field that shadows a
        # `BaseModel` method; the model is used for its schema only, so the
        # shadowing is harmless and the warning would print on every `app()`.
        warnings.filterwarnings(
            "ignore", message=".*shadows an attribute in parent", category=UserWarning
        )
        return create_model(  # ty: ignore[no-matching-overload]
            "SeahavenStartup", __config__=config, **fields
        )


def _signature(hook: RegisteredStartupHook) -> inspect.Signature:
    try:
        # Evaluated, so a `Literal` or a model reaches pydantic as the object it names.
        return inspect.signature(hook.fn, eval_str=True)
    except NameError as error:
        raise WorldBug(
            f"startup hook {name_of(hook.fn)} is annotated with {error.name!r}, which does not "
            f"exist at runtime: an annotation imported only under TYPE_CHECKING cannot describe "
            f"a startup keyword"
        ) from error


def _unschemable_keyword(fields: dict[str, tuple[Any, Any]], config: ConfigDict) -> str | None:
    """The keyword pydantic refused, found by building each one alone."""
    for name, definition in fields.items():
        try:
            _create_startup_model(config, {name: definition}).model_json_schema(mode="validation")
        except Exception:
            return name
    return None
