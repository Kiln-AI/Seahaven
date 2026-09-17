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
  (logged with its traceback and answered with a fixed generic error, so engine
  text never reaches an agent).
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

import logging
import uuid
from pathlib import Path
from typing import Any

from openenv.core.env_server.interfaces import Environment
from openenv.core.env_server.mcp_types import (
    CallToolAction,
    CallToolObservation,
    ListToolsAction,
    ListToolsObservation,
)
from openenv.core.env_server.types import Action, EnvironmentMetadata, State
from pydantic import BaseModel, Field

from seahaven.call import serialise
from seahaven.errors import SeahavenError, ToolError, UnknownTool, WorldBug
from seahaven.instances import Instance
from seahaven.state import document
from seahaven.world import CONTROL_TOOL_NAMES, World

__all__ = [
    "FileRef",
    "FixtureRef",
    "NodeRef",
    "SeahavenEnv",
    "SeahavenObservation",
    "SeahavenState",
    "WorldRef",
]

_log = logging.getLogger(__name__)

README_NAME = "README.md"

# What an agent is told when a tool failed in a way nobody wrote down. The code
# and the message are fixed and say nothing: the real failure is in the server's
# log, with its traceback, and an eval that grades on error text must not be able
# to read a stack frame out of one.
INTERNAL_ERROR_CODE = "internal"
INTERNAL_ERROR_MESSAGE = "internal error"


class SeahavenObservation(CallToolObservation):
    """What a tool call answers with: exactly one of `result` and `error`.

    It subclasses OpenEnv's `CallToolObservation` rather than `Observation` so
    that upstream's MCP `tools/call` path, which checks for that type, keeps
    working. `/mcp` itself is refused, but a `{"type": "mcp"}` frame on a `/ws`
    connection reaches the same check (`http_server.py` line 985 in openenv
    0.4.2), which answers an internal error to anything that is not one.

    `error` is a tool's own error: data the agent reads, which never closes the
    session, in one shape across every world -- `{"code", "message", "details"}`,
    the same dict `ToolError.to_dict` gives in-process. OpenEnv's docstrings say
    the field is only for transport failures, but its own `MCPEnvironment`
    answers a failed tool call with `ToolErrorType.EXECUTION_ERROR` ("tool ran
    but failed"), so this follows the convention their code establishes rather
    than the one their docstrings describe. The shape differs: their `ToolError`
    is `{error_type, message}` and forbids extra keys, so a strict parse of
    `error` against it rejects a tool error -- `SeahavenClient` types the field
    as this dict, and any lenient client reads it fine.
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
    error: dict[str, Any] | None = Field(
        default=None,
        description=(
            "The tool's own error, as `{code, message, details}`, or null when the call "
            "succeeded. A tool error is data the agent reads: it never ends the session."
        ),
    )
    # Inherited from `Observation`: `done` (always `False` here), `reward`
    # (always `None`) and `metadata`.


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
    # round-2 finding a third time, one class over. They do not reach a client:
    # `GET /schema` answers `State.model_json_schema()` and `GET /state` is
    # annotated `response_model=State`, so OpenEnv never publishes a subclass's
    # fields either way -- filed upstream as
    # https://github.com/huggingface/OpenEnv/issues/1155 (open against 0.4.2;
    # `main` is unchanged). TODO: when a release carrying that fix is the floor,
    # these descriptions reach a client and this note can go. `SeahavenState`
    # still describes itself, because the model is the thing that is wrong or
    # right about its own fields, and the day upstream publishes the real state
    # model these are already correct.
    #
    # Declared in `functional_spec.md` §3.1's order, less `episode_id`, which the
    # base carries. A field the document always writes is required here; the five
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
        description="The instance's clock as an ISO-8601 instant, or null before the first reset.",
    )
    startup: dict[str, Any] | None = Field(
        default=None,
        description=(
            "The reset keywords beyond `fixture`, `seed`, `now` and `state_format`, exactly as "
            "the startup hooks received them; empty when there were none, null before any reset."
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


class SeahavenEnv(Environment[Action, SeahavenObservation | ListToolsObservation, SeahavenState]):
    """One world, one session, one instance.

    The instance lives on the environment object and OpenEnv makes one
    environment object per session, so instance-per-session is true by
    construction rather than by bookkeeping.
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
        state_format: str | None = None,
        **startup_kwargs: Any,
    ) -> SeahavenObservation:
        """Start an episode: a fresh instance, and the session's previous one gone.

        `fixture=None` is a blank instance built from the world's DDL, whose clock
        is the wall time unless `now=` says otherwise; a fixture carries its own
        clock and `now=` with one is refused. `state_format=` answers this
        episode's `state` message in another of the root world's formats, in
        place of the world's pin, and an unregistered one is refused before
        anything is copied. Those are the instance's own rules and not a second
        set: everything here is passed straight through, including any startup
        keyword. `state_format` is keyword-only and named, like `fixture`, `seed`
        and `now`, so it can never fall into `**startup_kwargs` and reach a hook.

        The old instance is destroyed *before* the new one is made, so the
        session never holds two at once. A creation that then fails leaves the
        session exactly as a fresh one -- no instance, no episode, no steps -- and
        open for another `reset`: the exception propagates, and OpenEnv answers
        the frame with `EXECUTION_ERROR` rather than closing the connection.
        """
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
            state_format=state_format,
            episode_id=episode_id or str(uuid.uuid4()),
            startup_kwargs=startup_kwargs,
        )
        self._instance = instance
        return SeahavenObservation(
            result={
                "fixture": instance.fixture,
                "now": instance.clock.iso(),
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

        A `CallToolAction` needs an instance, and naming a control tool without
        the flag is refused as `UnknownTool` before dispatch, in the same words an
        unregistered name earns: an agent must not be able to tell the two apart.

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
        to prevent (`functional_spec.md` §5.3). Anything else is an accident: it
        is logged with its traceback and answered with the fixed generic error, so
        that a world with no error handler still cannot leak engine text.
        """
        instance = self._instance
        if instance is None:
            raise WorldBug("reset first")
        name = action.tool_name
        try:
            if name in CONTROL_TOOL_NAMES and not self.include_control_tools:
                # Before dispatch, and in the same words as a name the world does
                # not have: whether this server was started with the flag is not
                # something an agent gets to learn by calling.
                raise UnknownTool(name)
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
            return SeahavenObservation(tool_name=name, error=error.to_dict())
        except SeahavenError:
            raise
        except Exception:
            _log.exception(
                "tool %s on instance %s of world %s failed with an unhandled exception",
                name,
                instance.id,
                self.world.name,
            )
            return SeahavenObservation(tool_name=name, error=_internal_error())

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


def _internal_error() -> dict[str, Any]:
    """The generic error, built fresh each time so no caller can edit the next one's.

    Built through `ToolError.to_dict` rather than written out, so the generic
    error carries exactly the keys every other error on the wire carries --
    `architecture.md` §6's `{"code", "message", "details"}` -- and cannot drift
    from them.
    """
    return ToolError(INTERNAL_ERROR_CODE, INTERNAL_ERROR_MESSAGE).to_dict()
