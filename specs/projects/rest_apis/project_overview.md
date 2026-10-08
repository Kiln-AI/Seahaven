---
status: complete
---

# REST APIs

Not always, but often a Seahaven world mocks a real REST API. For example, the Stripe world
implemented Stripe's REST API, then added the MCP tool wrapper functions it needed. Many worlds
follow that pattern. This project adds framework-level support for it.

## What to build

- **An HTTP API protocol.** A "call_api" protocol or similar, defining a FastAPI-compatible HTTP
  handler. Roughly: it takes method, headers and body, and returns status, headers and body. The
  usual. A world registers one with `@seahaven.http_api` or `@seahaven.http_api(name="xyz")`.
  Registering two with the same name raises, including two with the default (no) name.
- **A new CLI command**, `seahaven serve_http` or similar, which starts a server. It takes an
  optional port, an optional host, and optional reset options (the reset dict passed to each
  instance it creates). It should share a lot of its argument code with `seahaven serve`. An
  optional `--name` selects the API registered with `http_api(name="xyz")`.

## The server

- Starts a FastAPI server.
- Registers a wildcard handler that takes care of instance management, then dispatches to the
  correct `http_api` handler.
  - `/worlds/{id}/*` is routed to a specific instance, by ID.
  - On the first call to an ID, the instance is created if it does not already exist. Creation is
    automatic.
- `DELETE /worlds/{id}` deletes the instance. Instances are kept until they are deleted.

A `@seahaven.tool` can call the API handler, and that is often all a tool wrapper does anyway.

## Why

- The first two worlds have use cases where they also make excellent test servers: like Stripe's
  sandbox, but for every service. If we are building all the infrastructure, we should expose it.
- When the backing technology is an HTTP API, the world should mock that directly, including
  headers, status codes and so on. Modelling the real product is always better; tool wrappers are
  lossy.

## Constraints

- It should be implemented fairly isolated: a nearly completely separate, opt-in feature, plus CLI
  hooks.

## Revised scope (from discussion)

Tool first; HTTP is a side benefit. For this version Seahaven ships a server helper only:

- Define the handler contract: `(ctx, request) -> response`, with Seahaven's own plain request and
  response types rather than FastAPI objects, so a world never imports a server stack to declare
  its API and a tool can call the handler directly.
- Write the server helper. It runs each request through `inst.bulk()`, so it needs no change to
  core. Instances default to the `wall` clock mode.
- `PUT /worlds/{id}` with reset options creates or replaces an instance. `--max-instances`
  defaults to 100. No idle TTL.
- Write a doc page for authors.
- No `world.http_api` registration and no `seahaven` subcommand. A world following the convention
  writes a few-line `serve_http.py` and runs `uv run serve_http.py`. The helper parses the
  command line itself, sharing `seahaven mcp`'s reset-option code.
- No tool middleware on HTTP requests. Worlds bring their own router; no routing helpers.
- Not in this version: info and state endpoints, rollback on an error status (a later opt-in),
  added worlds' APIs.
