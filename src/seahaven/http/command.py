"""`seahaven.http.main`: the command line of a world's `serve_http.py`.

The reset-option flags, their environment variables and their refusals are
`seahaven mcp`'s own, called from `seahaven.cli.mcp`, so the two commands cannot
drift apart. No seed is added here: each instance gets one of its own from the
registry.
"""

import argparse
import os
import sys
from pathlib import Path

from seahaven.cli import CliError
from seahaven.cli.mcp import add_reset_option_arguments, resolve_reset_options
from seahaven.errors import SeahavenError
from seahaven.http import DEFAULT_HOST, DEFAULT_MAX_INSTANCES, DEFAULT_PORT, HttpHandler, serve
from seahaven.world import World

__all__ = ["WITHHELD_REASON", "main"]

# Ends the refusal of `control_tools`, which `world.instance(...)` takes and this
# server does not offer.
WITHHELD_REASON = "this server serves the world's HTTP handler and nothing else"


def main(world: World, handler: HttpHandler, argv: list[str] | None = None) -> None:
    """Parse `argv` (default `sys.argv[1:]`) and serve until the process is stopped.

    A refused option is one line on stderr and exit 1.
    """
    args = _parser(world).parse_args(argv)
    try:
        reset_options = resolve_reset_options(args, os.environ, withheld_reason=WITHHELD_REASON)
        if args.max_instances < 0:
            raise CliError(
                f"--max-instances takes 0 or more, 0 for no limit, not {args.max_instances}"
            )
        # The wrapper rather than the server module, so a missing server stack
        # is `MISSING_EXTRA`.
        serve(
            world,
            handler,
            host=args.host,
            port=args.port,
            reset_options=reset_options,
            max_instances=args.max_instances,
        )
    except (CliError, SeahavenError, ImportError) as error:
        print(error, file=sys.stderr)
        raise SystemExit(1) from None


def _parser(world: World) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=Path(sys.argv[0]).name,
        description=(
            f"Serve the {world.name} world's HTTP API: one instance per id, under /worlds/{{id}}/."
        ),
    )
    parser.add_argument(
        "--host", default=DEFAULT_HOST, help=f"the address to bind (default {DEFAULT_HOST})"
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help=f"the port to bind (default {DEFAULT_PORT})"
    )
    parser.add_argument(
        "--max-instances",
        metavar="N",
        type=int,
        default=DEFAULT_MAX_INSTANCES,
        help=(
            "how many instances may exist at once, 0 for no limit "
            f"(default {DEFAULT_MAX_INSTANCES})"
        ),
    )
    add_reset_option_arguments(
        parser, seed_default="a random seed for each instance", clock_mode_default="wall"
    )
    return parser
