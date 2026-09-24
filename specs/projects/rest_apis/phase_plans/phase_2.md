---
status: complete
---

# Phase 2: The ASGI server, `app` and `serve`

## Overview

Serve a world's handler over HTTP. `seahaven.http.server` is a plain ASGI app over phase 1's
`Registry` and `dispatch`, with the routes of functional spec §4 and the answers of §5 and §8, and
`serve()` runs it with uvicorn. `seahaven.http.app` and `seahaven.http.serve` are thin wrappers that
import the server lazily and turn a missing server stack into `ImportError(MISSING_EXTRA)`. The
tests drive the app over real HTTP.

Two items from phase 1's review are closed here, because the server makes them observable:
`HttpResponse` accepts things uvicorn's h11 layer refuses at send time, and a raise from
`destroy()` in `Registry.put` escapes unwrapped and leaves an orphan slot.

## Steps

1. `src/seahaven/http/messages.py`: `HttpResponse` refuses what h11 cannot send.
   - status 200 to 599 (a 1xx is an interim response, not an answer);
   - a header name matches the token regex `[!#$%&'*+.^_`|~0-9A-Za-z-]+`;
   - a header value has no control character other than tab (`\x00-\x08`, `\x0a-\x1f`, `\x7f`)
     and no space or tab at either end (h11 refuses both), and is latin-1 as before.
2. `src/seahaven/http/runtime.py`: `DestroyFailed(Exception)` with `.cause`. `_discard` wraps a
   raise from `destroy()` in it (its `finally` still releases the place). `put` retires the slot and
   re-raises when discarding the old instance fails; `delete` retires in a `finally`; `close` catches
   `DestroyFailed` and logs. The ID holds no instance afterwards in every case.
3. `src/seahaven/http/server.py`, per architecture §5:
   - `app(world, handler, *, reset_options, max_instances) -> ASGIApp`: refuses a non-callable
     handler and a `max_instances` that is not an `int >= 0` (`WorldBug`), checks the options with
     `check_reset_options(..., source=SERVER_OPTIONS)`, returns `_App`.
   - `_App.__call__`: lifespan (close the registry on shutdown, in the thread pool), http, and a
     websocket closed before accept.
   - `_respond`: strip `root_path`; `404` outside `/worlds/`; `400` for a bad ID; `PUT`/`DELETE`/`405`
     with `allow` on `/worlds/{id}`; otherwise `_forward`.
   - `_forward`, `_put`, `_delete` as architecture §5.4, plus `DestroyFailed` -> `500` on `PUT` and
     `DELETE`. `_failed(id, done, cause)` logs the traceback and answers
     `instance {id} could not be {done}: {Type}: {message}`.
   - `_to_starlette`: drops the handler's `content-length` and `transfer-encoding`, keeps repeats.
   - `base_url(host, port)` and `serve(...)` (print the line, `uvicorn.run(..., workers=1)`).
4. `src/seahaven/http/__init__.py`: `DEFAULT_HOST`, `DEFAULT_PORT`, `MISSING_EXTRA`; `app` and
   `serve` wrappers with defaults, importing `seahaven.http.server` inside a context manager that
   turns a `ModuleNotFoundError` for `starlette` or `uvicorn` into `ImportError(MISSING_EXTRA)`.
   `ASGIApp` under `TYPE_CHECKING`.
5. `tests/serving.py`: split out `serving_app(app, **config)`, which serves any ASGI app; `serving`
   imports `seahaven.openenv` when called and delegates to it.
6. `tests/http_world.py`: a `GET /respond` route that builds the response its query describes; the
   fallback 404 names the path it got; the echo route also sends a `transfer-encoding` header.

## Tests

- `test_http_messages.py`: the new refusals (1xx, empty/space/colon/non-ASCII names, NUL, VT, DEL,
  CR, leading and trailing whitespace) and the accepted edges (tab inside, empty value, every token
  character, 200 and 599).
- `test_http_runtime.py`: a failed destroy on `put` and on `delete` raises `DestroyFailed`, leaves
  no slot and frees the place; `close` logs a failed destroy and goes on.
- `test_http_server.py`, over real HTTP:
  - routes: `404` outside `/worlds/`; `400` for bad IDs (including 65 characters); `405` with
    `allow`; a websocket refused with `403`; a `root_path` taken off.
  - forwarding: `/worlds/a/` reaches the handler as `/`; method, decoded path, query, repeated
    headers and body arrive unchanged; two `set-cookie` come back; the handler's framing headers
    are replaced; `HEAD` has headers and no body.
  - the handler: commit on a `400`, rollback and `500` on a raise and on a non-response; each
    response h11 would refuse is a `500` `seahaven_error`, and the accepted edge is sent intact; a
    first request whose creation fails is `500` and logged.
  - `PUT`: `201` then `200` with a fresh instance; an empty body takes the server's options; a body
    is laid over them, `null` included; every refusal is `400` and creates nothing; a failing
    startup is `500` and leaves the ID empty; the limit is `503` and replacing does not count; a
    failing destroy on `PUT` and `DELETE` is `500` and leaves the ID empty.
  - `DELETE`: `204`, then `404`, and a later request creates again.
  - instances: concurrent first requests create one; different IDs mint different IDs; a `tick`
    clock moves once per request; stopping the server destroys every instance.
  - `app`/`serve`: each argument refusal; a missing `uvicorn` or `starlette` is `MISSING_EXTRA`,
    another missing module is not; `serve` prints the base URL and hands uvicorn one worker and the
    options; a bad option raises before printing; `base_url` for each host shape.
