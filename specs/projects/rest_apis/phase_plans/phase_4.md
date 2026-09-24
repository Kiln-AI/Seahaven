---
status: complete
---

# Phase 4: The HTTP APIs docs page and its reference

## Overview

Document `seahaven.http` for world authors, as built in phases 1 to 3: a new guide page,
`http_apis.md`, a `seahaven.http` section in `reference/api.md`, and links from `index.md` and
`serving_and_openenv.md`. The page's executed example runs in the docs suite; the reference's stub
signatures are checked against the code. The docs describe the code as built, so the deviations
recorded in the phase 1 to 3 commit messages apply (for example the 500 messages name the
exception type, `--host`/`--port`/`--max-instances` show their real defaults, a negative
`--max-instances` is refused by `main`, and `HttpResponse` refuses what h11 refuses).

## Steps

1. `src/seahaven/docs/http_apis.md` (new), in the `AGENTS.md` docs style, with a table of contents:
   1. What it is: a world written as one handler function over Seahaven's request and response
      types, tools that call it, and an optional test server.
   2. Writing a handler: `HttpRequest`, `HttpResponse`, `HttpResponse.json`, routing with `match`.
   3. Calling it from a tool: one `python` fence that defines an inline world, a handler, two tools
      over it that turn an error status into a `ToolError`, and drives them through
      `world.instance(...)`. Imports only `seahaven`, `seahaven.http` and `json`.
   4. Serving it: the `serve_http.py` script (a `py` fence), `uv run serve_http.py`, the line it
      prints, the options table (`--host`, `--port`, `--max-instances`, the reset-option flags and
      their variables, linking to `reference/cli.md#seahaven-mcp`), installing the extra (link to
      `serving_and_openenv.md#install-the-serve-extra`), and `seahaven.http.app` for an ASGI host.
   5. The routes: the table, `PUT` options laid over the server's with `null` removing one, `DELETE`,
      the `{"seahaven_error": ...}` shape, a `sh` fence of `curl` calls.
   6. How a request differs from a tool call: one transaction committed on any status and rolled
      back on a raise; no call-log entry and `i` is `None`; `ctx.call` is `None`; a `tick` step per
      request; the `wall` default; a seed per instance; the instance limit; lifetime and
      concurrency; an encoded slash arrives as `/`.
2. `src/seahaven/docs/reference/api.md`: a `seahaven.http` section after `seahaven.openenv`, with
   stubs for `HttpRequest`, `HttpResponse`, `HttpHandler`, `app`, `serve`, `main` and the four
   constants; what `HttpResponse` refuses at construction; the statuses the server answers itself.
   Add it to the page's opening sentence and section table.
3. `src/seahaven/docs/index.md`: a row for `http_apis.md` in the reading-order table, after
   `serving_and_openenv.md`.
4. `src/seahaven/docs/serving_and_openenv.md`: one sentence in the introduction linking to
   `http_apis.md`.
5. `tests/test_docs.py`: add `http_apis.md` to `PAGES`, after `serving_and_openenv.md`.
6. `tests/test_docs_examples.py`: the stub-signature check looks a name up in the module a
   reference section documents when that section is `seahaven.http`, because `seahaven.http.app`
   and `seahaven.openenv.app` share a name and the existing lookup would compare the one against
   the other.

   ```py
   _SECTION_NAMESPACES = {"## `seahaven.http`": "seahaven.http"}

   def _section_of(block: Block) -> str: ...  # the `## ` heading above the block
   ```

## Tests

- `test_docs.py`: the layout tests pick up `http_apis.md` (exists, has a heading, is in the tree,
  is linked from `index.md`, names no control tool).
- `test_docs_examples.py::test_every_python_example_runs[http_apis.md:...]`: the page's example
  runs.
- `test_docs_examples.py::test_every_documented_signature_matches_the_code`: the `seahaven.http`
  stubs are compared with the real callables, and `seahaven.openenv.app` is still compared with
  its own.
- `test_docs_examples.py`: a unit test of the section lookup, that a stub under the
  `seahaven.http` heading resolves to `seahaven.http`'s name and one under `seahaven.openenv`
  does not.
- Name and member checks over the new page and section pass (`seahaven.http.*`, `ctx.*`, `inst.*`).
- Manually: run `tests/http_world.py` and the page's `curl` calls against it, to check the printed
  line, the statuses and the bodies the page shows.
