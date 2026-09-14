"""The typed client: one client for every Seahaven world.

Every world speaks the same wire shape -- `CallToolAction` in, an observation
carrying `result` or `error` out -- so there is one client rather than one per
world. That is why a world's `client.py` in a hub push is a single re-export
(`components/openenv.md` §6) and not generated code.

`SeahavenClient` is OpenEnv's `EnvClient` with the three parsers filled in and
two conveniences on top. Both conveniences go through `_dispatch`, which is how
the base client answers a result in synchronous code and an awaitable in
asynchronous code from the same method; writing them in terms of the public
`step()` would have answered `step`'s dual-mode wrapper instead of the
observation, so the sketch in the component document is spelled out here rather
than copied.
"""

from typing import Any, Self

from openenv.core.client_types import StepResult
from openenv.core.env_client import EnvClient
from openenv.core.env_server.mcp_types import CallToolAction, ListToolsAction

from seahaven.openenv.env import SeahavenObservation, SeahavenState

__all__ = ["SeahavenClient"]


class SeahavenClient(
    EnvClient[CallToolAction | ListToolsAction, SeahavenObservation, SeahavenState]
):
    """A connected session on a `seahaven serve` server.

    ```python
    with SeahavenClient(base_url="http://127.0.0.1:8000") as env:
        env.reset(fixture="empty", seed=7)
        tools = env.list_tools()
        obs = env.call("ping", message="hello")
        obs.result, obs.error
        env.state().now
    ```

    A tool error arrives on the observation and never raises: `obs.error` is the
    `{"code", "message", "details"}` the world produced, and exactly one of
    `obs.result` and `obs.error` is set. Only a framework or protocol failure --
    a `WorldBug` out of a world's own code, a malformed frame, a closed
    connection -- raises, as `RuntimeError`, which is the stock client's
    behaviour too.
    """

    def __enter__(self) -> Self:
        """The base client's `__enter__`, narrowed to this client's own type.

        `EnvClient.__enter__` is annotated as returning `EnvClient`, so `with
        SeahavenClient(...) as env` would hand back something with no `call` and
        no `list_tools` as far as a type checker is concerned -- and a world
        author would be told the two verbs this client exists for do not exist.
        """
        super().__enter__()
        return self

    async def __aenter__(self) -> Self:
        """The same narrowing for `async with`."""
        await super().__aenter__()
        return self

    def _step_payload(self, action: CallToolAction | ListToolsAction) -> dict[str, Any]:
        return action.model_dump()

    def _parse_result(self, payload: dict[str, Any]) -> StepResult[SeahavenObservation]:
        """A step or reset frame as a `StepResult` carrying a typed observation.

        Used for `reset` and for `CallToolAction` results, which are the frames
        that carry a `SeahavenObservation`. A `ListToolsAction` answers a
        `ListToolsObservation`, which has a `tools` list and no `result`; it does
        not come through here, and `list_tools` below says why.
        """
        return StepResult(
            observation=SeahavenObservation.model_validate(payload.get("observation", {})),
            reward=payload.get("reward"),
            done=payload.get("done", False),
            metadata=payload.get("metadata"),
        )

    def _parse_state(self, payload: dict[str, Any]) -> SeahavenState:
        return SeahavenState.model_validate(payload)

    def call(self, tool: str, /, **arguments: Any) -> Any:
        """Call a tool and answer the observation. Awaitable in asynchronous code.

        The observation, not the `StepResult` around it: a Seahaven step is never
        done and never rewarded, so the wrapper carries nothing a caller of this
        client wants. Read `.result` or `.error`.

        The tool name is positional-only, as `Instance.call`'s is, and for the
        same reason: a world is free to declare a tool argument called `tool` --
        or `self` -- and `**arguments` must be able to carry it. Without the `/`
        such a tool lists, works through `step(CallToolAction(...))`, and cannot
        be called through this client at all.
        """
        return self._dispatch(lambda: self._call_async(tool, **arguments))

    def list_tools(self) -> Any:
        """The world's tool list, as OpenEnv spells it. Awaitable in asynchronous code.

        Each entry is `{"name", "description", "input_schema"}`. Control tools are
        never listed, whatever the server was started with.

        This is the one verb that reads the frame itself instead of parsing it
        into `SeahavenObservation`: that model forbids extra fields and has no
        `tools`, because it is the shape of a tool *call*. The list is read
        straight off the step payload as `list[dict]`.
        """
        return self._dispatch(self._list_tools_async)

    async def _call_async(self, tool: str, /, **arguments: Any) -> SeahavenObservation:
        result = await self._step_async(CallToolAction(tool_name=tool, arguments=arguments))
        return result.observation

    async def _list_tools_async(self) -> list[dict[str, Any]]:
        response = await self._send_and_receive(
            {"type": "step", "data": ListToolsAction().model_dump()}
        )
        observation = response.get("data", {}).get("observation", {})
        return list(observation.get("tools", []))
