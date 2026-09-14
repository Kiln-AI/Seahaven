"""The `World` every other module here registers against."""

import seahaven

world = seahaven.World(
    name="payments",
    version="1.4.0",
    schema=seahaven.sql_files(__package__, "schema"),
)
