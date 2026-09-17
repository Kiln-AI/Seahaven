"""Seahaven over OpenEnv: the ASGI app, the environment, the models, the client.

This subpackage is the only part of Seahaven that needs the `serve` extra, and
it imports `openenv` at module top: `import seahaven` never reaches here, so a
world used in-process -- in pytest, in a script, in a notebook -- never pays for
a dependency whose wheel brings gradio, openai, fastmcp and pandas with it.

```python
from projecttracker import world
import seahaven.openenv

app = seahaven.openenv.app(world)
```

That file, `openenv_app.py`, is the whole of a world's server. One world per
app, mounted at `/`, which is the shape a hub expects.
"""

import functools

from fastapi import FastAPI
from openenv.core.env_server.http_server import create_app
from openenv.core.env_server.mcp_types import (
    CallToolAction,
    ListToolsAction,
    ListToolsObservation,
)
from openenv.core.env_server.types import ConcurrencyConfig

from seahaven.openenv.client import SeahavenClient
from seahaven.openenv.env import (
    FixtureRef,
    SeahavenEnv,
    SeahavenObservation,
    SeahavenState,
    WorldRef,
)
from seahaven.world import World

__all__ = [
    "CallToolAction",
    "FixtureRef",
    "ListToolsAction",
    "ListToolsObservation",
    "SeahavenClient",
    "SeahavenEnv",
    "SeahavenObservation",
    "SeahavenState",
    "WorldRef",
    "app",
]

# The operator's budget, not a design limit. Seahaven refuses no session of its
# own accord; over capacity OpenEnv answers `CAPACITY_REACHED` and closes the
# connection, and `seahaven serve --max_concurrent_envs` moves the number.
DEFAULT_MAX_CONCURRENT_ENVS = 500

# A held session costs its fixture copy on disk and about a megabyte of memory,
# and a client that drops without closing holds one for ever. An hour idle is
# long enough that no live eval is reaped and short enough that a crashed
# harness does not accumulate; `--session-timeout 0` turns the reaper off.
DEFAULT_SESSION_TIMEOUT = 3600.0


def app(
    world: World,
    *,
    include_control_tools: bool = False,
    max_concurrent_envs: int = DEFAULT_MAX_CONCURRENT_ENVS,
    session_timeout: float | None = DEFAULT_SESSION_TIMEOUT,
) -> FastAPI:
    """The ASGI app serving one world: one session per instance, many sessions.

    `include_control_tools` makes `controller_run_sql` and `controller_changes`
    callable over the wire. They are never listed either way; the flag is for a
    harness that drives the world itself, and a server an agent talks to should
    not have it.

    `session_timeout` is seconds of inactivity before OpenEnv reaps a session,
    or `None` for no reaper. It is passed as part of a `ConcurrencyConfig`
    because OpenEnv refuses both that and `max_concurrent_envs` together, and
    `env_name` is passed explicitly because the factory is a `functools.partial`
    and OpenEnv cannot read a name off one.
    """
    return create_app(
        functools.partial(SeahavenEnv, world, include_control_tools=include_control_tools),
        CallToolAction,
        SeahavenObservation,
        env_name=world.name,
        concurrency_config=ConcurrencyConfig(
            max_concurrent_envs=max_concurrent_envs,
            session_timeout=session_timeout,
        ),
    )
