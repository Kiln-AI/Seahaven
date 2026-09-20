# Contributing to Seahaven

Thanks for your interest. Seahaven is early and the API is still moving, so open an issue before
starting anything larger than a bug fix.

## Setup

You need [uv](https://docs.astral.sh/uv/) and a final release of Python 3.14 or newer. Not a
release candidate: two locked dependencies break on the 3.14 rcs, and uv will not stop you from
syncing onto one.

```sh
uv python install 3.14
uv sync --extra serve
uv run python -V    # must be 3.14.0 or newer, with no "rc"
```

## Running the checks

CI runs these, and all of them must pass before you open a pull request:

```sh
uv run ruff format --check
uv run ruff check
uv run ty check -c 'src.exclude=["src/seahaven/mcp", "tests/test_mcp_server.py", "tests/test_mcp_process.py"]'
uv run pytest                              # the framework
uv run pytest worlds/projecttracker        # the reference world
uv run pytest extensions/seahaven-xmlrpc   # the example extension
uv run python scripts/check_licences.py serve
```

`serve` and `mcp` are conflicting extras, so the second environment is a second run:

```sh
uv sync --extra mcp
uv run pytest
uv run python scripts/check_licences.py mcp
uv sync --extra serve                      # back to the everyday one
```

The type check is split between the two environments, because `ty` resolves imports against the one
it runs in. The line above walks everything but the MCP paths, and CI's `mcp` job checks those.

## Guidelines

- Everything is typed, tests included. `ty` is the checker.
- Write tests with the code. Examples in the docs are executed by the test suite.
- Keep a pull request to one change. Unrelated fixes go in their own.
- Runtime dependencies must be permissively licensed (MIT, Apache-2.0, BSD). No copyleft.
- No real customer data anywhere, including in the reference world.

## Licence

Seahaven is intended to be released under the MIT licence, but the `LICENSE` file is not in the
repository yet. Ask before investing in a large change.
