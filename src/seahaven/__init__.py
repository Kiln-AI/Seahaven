"""Seahaven: a framework for building synthetic worlds.

The public API is exactly the names below, plus the `sandbox` module. Everything
else is internal.
"""

from importlib.metadata import PackageNotFoundError, version

from seahaven import sandbox
from seahaven.clock import Clock
from seahaven.db import Db
from seahaven.errors import (
    ArgumentError,
    DbError,
    SeahavenError,
    ToolError,
    UnknownTool,
    WorldBug,
)
from seahaven.ids import Ids

try:
    __version__ = version("seahaven")
except PackageNotFoundError:  # imported from a source tree that was never installed
    __version__ = "0.0.0+unknown"

__all__ = [
    "ArgumentError",
    "Clock",
    "Db",
    "DbError",
    "Ids",
    "SeahavenError",
    "ToolError",
    "UnknownTool",
    "WorldBug",
    "sandbox",
]
