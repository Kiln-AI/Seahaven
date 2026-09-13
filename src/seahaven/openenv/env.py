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
from pydantic import Field

from seahaven.errors import SeahavenError, ToolError, UnknownTool, WorldBug
from seahaven.instances import Instance
from seahaven.world import CONTROL_TOOL_NAMES, World

__all__ = [
    "SeahavenEnv",
    "SeahavenObservation",
    "SeahavenState",
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
    that the `/mcp` `tools/call` path, which checks for that type, keeps working.

    `error` is a tool's own error, which diverges from OpenEnv's convention that
    `error` is a transport failure and a tool's error travels inside `result`.
    Seahaven takes the divergence deliberately: a tool error is data the agent
    reads, it must never close the session, and one shape for it across every
    world -- `{"code", "message", "details"}`, the same dict `ToolError.to_dict`
    gives in-process -- is worth more than the convention.
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


class SeahavenState(State):
    """The session's state: which instance, from what, at what time, of which world.

    `episode_id` and `step_count` are the base's. Before the first `reset` there
    is no instance, so `fixture` and `now` are both `None` and `world` is still
    answered: the world a session is connected to is known before it is reset.
    """

    # Descriptions for the same reason the observation's fields have them: the
    # two fields this class *inherits* arrive described by OpenEnv, and three
    # declared here with nothing said about them would be the round-1 and
    # round-2 finding a third time, one class over. They do not reach a client:
    # `GET /schema` answers `State.model_json_schema()` and `GET /state` is
    # annotated `response_model=State`, so OpenEnv never publishes a subclass's
    # fields either way -- both filed as `BACKLOG.md` B13. `SeahavenState`
    # still describes itself, because the model is the thing that is wrong or
    # right about its own fields, and the day upstream publishes the real state
    # model these are already correct.
    fixture: str | None = Field(
        default=None, description="The fixture the instance was made from, or null for a blank one."
    )
    now: str | None = Field(
        default=None,
        description="The instance's clock as an ISO-8601 instant, or null before the first reset.",
    )
    world: str = Field(
        description="The world this session is connected to. Known before any reset."
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
        self._episode_id: str | None = None
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
        **startup_kwargs: Any,
    ) -> SeahavenObservation:
        """Start an episode: a fresh instance, and the session's previous one gone.

        `fixture=None` is a blank instance built from the world's DDL, whose clock
        is the wall time unless `now=` says otherwise; a fixture carries its own
        clock and `now=` with one is refused. That is `world.instance(...)`'s rule
        and not a second one: everything here is passed straight to it, including
        any startup keyword, and everything it refuses it refuses before it has
        copied anything.

        The old instance is destroyed *before* the new one is made, so the
        session never holds two at once. A creation that then fails leaves the
        session exactly as a fresh one -- no instance, no episode, no steps -- and
        open for another `reset`: the exception propagates, and OpenEnv answers
        the frame with `EXECUTION_ERROR` rather than closing the connection.
        """
        self._forget()
        instance = self.world.instance(fixture, seed=seed, now=now, **startup_kwargs)
        self._instance = instance
        self._episode_id = episode_id or str(uuid.uuid4())
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
        """Where the session is: its episode, its step count, and its instance."""
        instance = self._instance
        return SeahavenState(
            episode_id=self._episode_id,
            step_count=self._steps,
            fixture=instance.fixture if instance is not None else None,
            now=instance.clock.iso() if instance is not None else None,
            world=self.world.name,
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
            result = instance.call(name, **action.arguments)
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

        `Instance.tools()` is a view over `world.tools` with the control tools
        filtered out, so the two answers are the same list; a test pins that they
        are, because the instance is the spelling `components/openenv.md` gives
        and the world is the only thing there is to ask before a `reset`.
        """
        instance = self._instance
        if instance is not None:
            return instance.tools()
        return [tool.listing() for tool in self.world.tools.values() if not tool.control]

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
        """Destroy the instance and clear the episode. The only way a session ends one."""
        instance = self._instance
        self._instance = None
        self._episode_id = None
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
