---
status: complete
---

# Phase 1: The handler contract, the instance registry and dispatch

## Overview

Build the part of `seahaven.http` that needs no server stack: the request and response types a
world's handler is written against, and the runtime the server will drive (a thread-safe registry of
instances by ID, and `dispatch`, which runs one request as one `bulk()` transaction). Add the test
world the whole project is tested against, and test the types, the registry, `dispatch` and the
tool path through `world.instance(...)`. The ASGI server, the `app`/`serve` wrappers and `main` are
later phases.

## Steps

1. `src/seahaven/http/messages.py` (stdlib and `seahaven.ctx` only), per architecture §3:
   - `type Headers = tuple[tuple[str, str], ...]`
   - `HttpRequest(method, path, query="", headers=(), body=b"")`, frozen; `__post_init__`
     upper-cases `method` and lower-cases header names (`object.__setattr__`);
     `header(name) -> str | None` (first match, case-insensitive); `json() -> Any`.
   - `HttpResponse(status=200, headers=(), body=b"")`, frozen; `__post_init__` refuses a status that
     is not an `int` or is a `bool` (`TypeError`), outside 100..599 (`ValueError`); headers that are
     not a tuple of `(str, str)` pairs (`TypeError`), a CR or LF in a name or value, or a name or
     value not encodable as latin-1 (`ValueError`); a body that is not `bytes` or `str`
     (`TypeError`). `HttpResponse.json(data, *, status=200, headers=())` with compact,
     `ensure_ascii=False` JSON and a `content-type: application/json` unless one is given in any
     case; `body_bytes` property.
   - `type HttpHandler = Callable[[Ctx[Any], HttpRequest], HttpResponse]`.
2. `src/seahaven/http/runtime.py`, per architecture §4:
   - `DEFAULT_MAX_INSTANCES = 100`, `INSTANCE_ID`, `DEFAULT_CLOCK_MODE: ClockMode = "wall"`,
     `SERVER_OPTIONS = "reset_options"`, `PUT_BODY = "the PUT body"`, logger `seahaven.http`.
   - `seahaven_error(status, message, *, headers=()) -> HttpResponse`.
   - `CapacityReached(limit)` whose message names the limit and `DELETE /worlds/{id}`;
     `CreationFailed(cause)` with `.cause`.
   - `check_reset_options(world, options, *, source)`: keys against
     `seahaven.cli.mcp.RESET_OPTION_KEYS`; the fixture against `world.fixtures()` only when
     `source == SERVER_OPTIONS`; raises `WorldBug`.
   - `Registry(world, defaults, max_instances)` with `run`, `put`, `delete`, `close`, and the
     private `_slot`, `_retire`, `_reserve`, `_release`, `_create`, `_make`, `_discard`, following
     the lock rules in §4.3 (the registry lock never held while a slot lock is taken; a retired slot
     restarts the operation).
   - `dispatch(instance, handler, request) -> HttpResponse`: `bulk()`, one `tick` step via
     `instance.clock._call_started()`, the non-response check inside the block, every `Exception`
     logged and answered `500`.
3. `src/seahaven/http/__init__.py`: the three message names and `DEFAULT_MAX_INSTANCES`, with
   `__all__`. The `app`/`serve`/`main` wrappers come in phase 2 and 3.
4. `tests/http_world.py`: `build(fixtures_dir=None, work_dir=None) -> World` (world `notes_api`,
   one `STRICT` `notes` table, a startup hook taking `explode: bool = False`, tools `get_note` and
   `create_note(body, reject=False)` over `handle`, raising `NoteError` on a non-2xx status) and
   `handle(ctx, request)` with the routes of architecture §8.1 (`/wrong` inserts before returning a
   non-response, so its rollback is observable). The `__main__` block that calls
   `seahaven.http.main` is added in phase 3, when `main` exists.

## Tests

`tests/test_http_messages.py`:
- method upper-cased and header names lower-cased, repeats and order kept; a list of headers
  becomes a tuple
- `header()` is case-insensitive, returns the first value, or `None`
- `request.json()` parses the body and raises on invalid JSON
- `HttpResponse.json` sets `content-type`, keeps a given one in any case, encodes non-ASCII as
  UTF-8 in `body_bytes`, compact separators, status and headers passed through
- `body_bytes` of a `str` and a `bytes` body
- refusals: status 99 and 600, `True`, a float status, a non-tuple headers value, a non-pair
  header, a non-str name, CR/LF in a name and in a value, a non-latin-1 value, a bad body type
- both types are frozen

`tests/test_http_runtime.py`:
- `check_reset_options`: an unknown key and `control_tools` are refused naming the keys taken and
  `"startup"`; an unknown fixture is refused for the server's options, naming the ids there are;
  the same fixture in a PUT body is not checked here; a `None` fixture and a known one pass
- `run` creates on first use and returns the same instance after
- eight threads behind a barrier create exactly one instance (`world.instance` called once)
- the limit: the next ID raises `CapacityReached` and leaves no slot; replacing with `put` does not
  count; `delete` frees a place; a failed creation frees its reservation; `0` means no limit
- `put` answers `True` then `False`, replaces with a new instance and removes the old directory;
  the body is laid over the defaults; `{"fixture": None}` removes a fixture default
- a failed `put` raises `CreationFailed` carrying the cause, and leaves the ID empty
- `delete` answers `True` then `False`; a later `run` creates a new instance
- `close` destroys every instance and removes each directory
- seeds: two instances with no seed mint different ids; an explicit seed replays; a `None` seed in
  the defaults reaches `world.instance` as `None`
- clock: the default mode is `wall`; an explicit mode is kept; under `tick` three requests read
  three successive ticks, the first one step past the start
- `dispatch`: a `400` commits its write; a raise rolls back and answers `500` naming the exception
  as a `seahaven_error`; a non-response rolls back and answers `500` naming the type returned;
  `ctx.call` is `None`; a request's change-log records have `i` of `None`
- through `world.instance(...)`: `create_note` then `get_note` succeed; `get_note` of a missing ID
  raises `NoteError`; the handler's insert is logged under the tool call's `i`; `create_note` with
  `reject=True` raises and rolls back the handler's write
