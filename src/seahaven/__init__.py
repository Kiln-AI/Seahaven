"""Seahaven: a framework for building synthetic worlds.

The public API is exactly the names below, plus the `helpers` and `sandbox`
modules. Everything else is internal.
"""

from importlib.metadata import PackageNotFoundError, version

from seahaven import helpers, sandbox
from seahaven.call import Call
from seahaven.changes import Change
from seahaven.clock import Clock
from seahaven.ctx import Ctx
from seahaven.db import Db
from seahaven.errors import (
    ArgumentError,
    DbError,
    SeahavenError,
    ToolError,
    UnknownTool,
    WorldBug,
)
from seahaven.fixtures import Fixture
from seahaven.ids import Ids
from seahaven.instances import Instance
from seahaven.tool import Tool
from seahaven.world import World

try:
    __version__ = version("seahaven")
except PackageNotFoundError:  # imported from a source tree that was never installed
    __version__ = "0.0.0+unknown"

__all__ = [
    "ArgumentError",
    "Call",
    "Change",
    "Clock",
    "Ctx",
    "Db",
    "DbError",
    "Fixture",
    "Ids",
    "Instance",
    "SeahavenError",
    "Tool",
    "ToolError",
    "UnknownTool",
    "World",
    "WorldBug",
    "helpers",
    "sandbox",
]
