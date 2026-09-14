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

It also closes more politely than the base client does, which is a fix and not a
convenience: `_disconnect_async` below says why, and what it costs to override a
private method to get it.
"""

import asyncio
import json
from contextlib import suppress
from typing import Any, Self

from openenv.core.client_types import StepResult
from openenv.core.env_client import EnvClient
from openenv.core.env_server.mcp_types import CallToolAction, ListToolsAction

from seahaven.openenv.env import SeahavenObservation, SeahavenState

__all__ = ["SeahavenClient"]

# How long a disconnect waits for the server to hang up before closing this end
# anyway. A healthy server answers in about a millisecond; the bound is here so
# that one which has stopped answering cannot hold an eval's shutdown open.
CLOSE_TIMEOUT = 5.0


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

    async def _disconnect_async(self) -> None:
        """Ask the server to close, and wait for it to, before closing this end.

        The base client sends `{"type": "close"}` and closes the socket in the
        same breath. The server does not get to finish: its `/ws` handler
        destroys the session first -- an `await`, so it yields -- and by the time
        it calls `close()` uvicorn has already seen the client's CLOSE frame and
        torn the transport down. That `close()` raises `WebSocketDisconnect`,
        which the handler's `except RuntimeError` does not catch, so every
        session that ends normally leaves an `ERROR: Exception in ASGI
        application` traceback in the server's log -- the log an operator greps
        when something is actually wrong. Waiting for the server to hang up
        first makes it a 1000/1000 handshake and no log line at all.

        This overrides a private method, verified against **openenv 0.4.2**. The
        name, the shape of the close frame, and `close()`/`__exit__`/`__aexit__`
        all reaching `_disconnect_async` are OpenEnv's internals and not its API.
        An upgrade that renames the method, or stops routing disconnects through
        it, would leave this override quietly unused -- so `test_client.py`
        asserts the base class still has the method this one replaces, and fails
        loudly when it does not. What is at stake if it ever slips through is
        noise in a log, never a disconnect that does not happen: `super()` below
        closes the socket either way.
        """
        ws = self._ws
        if ws is not None and self._ws_loop is asyncio.get_running_loop():
            # The loop check is the base client's own `same_loop` guard: a client
            # disconnected from a loop other than the one it connected on must
            # not touch the socket, which belongs to a loop that may be dead.
            with suppress(Exception):
                await ws.send(json.dumps({"type": "close"}))
                await asyncio.wait_for(ws.wait_closed(), timeout=CLOSE_TIMEOUT)
        # `super()` still runs, and still sends a close frame of its own: against
        # a server that has already closed, that send fails and is swallowed
        # there, and against one that never answered the socket is closed exactly
        # as it was before. It is also what clears `_ws` and `_ws_loop`, so the
        # teardown itself stays OpenEnv's.
        await super()._disconnect_async()

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
