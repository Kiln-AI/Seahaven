"""The `World` object every other module in this package registers against.

One `World` per package, built here so that tool and middleware modules import
it without an import cycle: `world.py` imports nothing of this world's, and
everything of this world's imports `world.py`.
"""

import seahaven

world = seahaven.World(
    name="projecttracker",
    version="1.0.0",
    schema=seahaven.sql_files(__package__, "schema"),
)
