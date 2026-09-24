---
status: complete
---

# Functional Spec: REST APIs

## 1. Purpose

Many worlds mock a product whose real interface is an HTTP API. When a world implements that API
directly, it can model the product faithfully (status codes, headers, error envelopes), and its
tools become thin wrappers over it.

This project adds two things to Seahaven:

1. **A handler contract.** A world writes its API as one plain function,
   `handler(ctx, request) -> response`, using Seahaven's own request and response types. The
   world's tools call that function, in the tool's own transaction.
2. **A server helper.** `seahaven.http` serves one world's handler over HTTP, with one instance per
   client-chosen ID. This makes any such world usable as a local test server, like a payment
   provider's sandbox.

The primary surface is still the world's tools. HTTP serving is a secondary benefit, and it is
opt-in: a world that never imports `seahaven.http` is unchanged.

## 2. Scope

### In scope

- `HttpRequest` and `HttpResponse` types and the `HttpHandler` type (§3).
- `seahaven.http.app(...)`: the ASGI application (§4, §5).
- `seahaven.http.serve(...)`: runs the application with uvicorn (§6).
- `seahaven.http.main(...)`: parses a command line and calls `serve` (§6).
- A docs page for authors, and an example on the reference world (§9).

### Out of scope for this version

- Registering a handler on the world (`@world.http_api`), and a `seahaven` subcommand. A world
  with two APIs writes two scripts.
- Routing helpers. The world brings its own router, or dispatches by hand.
- Tool middleware on HTTP requests.
- Info and state endpoints (`GET /worlds/{id}`).
- Rolling back a request that returns an error status. This may come later as an opt-in (§7).
- An idle timeout for instances. Persisting instances across a server restart.
- Authentication.
- Serving the APIs of worlds added through composition.
- Streaming responses, server-sent events and WebSockets.
- HTTP requests counting as tool calls (§7.4).

## 3. The handler contract

### 3.1 Types

The types are importable without any extra (`from seahaven.http import HttpRequest`), so a world
can declare and test its API without the server's dependencies installed.

```py
@dataclass(frozen=True)
class HttpRequest:
    method: str                                  # upper case: "GET", "POST", ...
    path: str                                    # "/v1/customers/cus_1": percent-decoded, below the instance prefix
    query: str = ""                              # raw query string without "?": "limit=3&expand[]=x"
    headers: tuple[tuple[str, str], ...] = ()    # in order; names lower case; repeats allowed
    body: bytes = b""

    def header(self, name: str) -> str | None: ...   # first value, name matched case-insensitively
    def json(self) -> Any: ...                       # json.loads(body); raises on invalid JSON


@dataclass(frozen=True)
class HttpResponse:
    status: int = 200
    headers: tuple[tuple[str, str], ...] = ()
    body: bytes | str = b""                      # a str is sent UTF-8 encoded

    @classmethod
    def json(cls, data: Any, *, status: int = 200,
             headers: tuple[tuple[str, str], ...] = ()) -> HttpResponse: ...
             # body is JSON; adds content-type: application/json unless headers set one


type HttpHandler = Callable[[Ctx[Any], HttpRequest], HttpResponse]
```

The constructors take their values as given. `HttpRequest` upper-cases `method` and lower-cases
header names, so a tool that builds a request by hand gets the same shape the server builds.

### 3.2 A handler

A handler is a synchronous function. It reads and writes the database through `ctx`, as a tool
does, and returns an `HttpResponse`. The world does its own routing inside the function.

```py
def handle(ctx: seahaven.Ctx, request: HttpRequest) -> HttpResponse:
    match request.method, request.path.split("/")[1:]:
        case "GET", ["v1", "customers", customer_id]:
            row = ctx.db.one("SELECT * FROM customers WHERE id = ?", customer_id)
            if row is None:
                return HttpResponse.json({"error": {"type": "not_found"}}, status=404)
            return HttpResponse.json(dict(row))
        case _:
            return HttpResponse.json({"error": {"type": "no_route"}}, status=404)
```

### 3.3 Calling the handler from a tool

A tool calls the handler directly with its own `ctx`. The handler runs inside the tool's call:
the tool's transaction, the change log entry and `ctx.call` all belong to that tool call. The tool
decides what an error status means to an agent, typically by raising a `ToolError`, which also
rolls back the call.

```py
@world.tool
def get_customer(ctx: seahaven.Ctx, customer_id: str) -> dict[str, object]:
    """Fetch one customer by id."""
    response = handle(ctx, HttpRequest("GET", f"/v1/customers/{customer_id}"))
    if response.status != 200:
        raise CustomerError(...)
    return json.loads(response.body)
```

Seahaven adds nothing for this: it is a plain function call.

## 4. The server's routes

The server serves one world and one handler. An instance is addressed by an ID the client chooses.
A client points its SDK's base URL at `http://HOST:PORT/worlds/{id}`.

| Route | Method | Behaviour |
|---|---|---|
| `/worlds/{id}` | `PUT` | Create or replace the instance `id` (§5.2) |
| `/worlds/{id}` | `DELETE` | Destroy the instance `id` (§5.3) |
| `/worlds/{id}` | any other | `405` |
| `/worlds/{id}/{path...}` | any | Pass the request to the handler (§5.1) |
| anything else | any | `404` |

The path `/worlds/{id}` without a trailing part belongs to Seahaven. Every path below it belongs to
the world: `/worlds/acme/` reaches the handler with `path="/"`, and `/worlds/acme/v1/customers`
with `path="/v1/customers"`.

**Instance IDs** match `[A-Za-z0-9_-]{1,64}`. Any other ID is answered `400`.

### 4.1 Seahaven's own responses

Every response the server makes itself, rather than the handler, is JSON with the content type
`application/json` and the shape `{"seahaven_error": "<message>"}`. The message says what to do,
in the style of Seahaven's other errors. A client can tell the server's errors from the world's by
that key.

## 5. Instances

### 5.1 Requests to the handler

For a request to `/worlds/{id}/{path...}`:

1. If no instance `id` exists, the server creates one from the server's reset options (§6.2). This
   is automatic, and it is the only way most clients ever create an instance.
2. The server builds an `HttpRequest` from the HTTP request.
3. The server runs the handler inside `with instance.bulk() as ctx:` (§7).
4. The server sends the handler's `HttpResponse` as the HTTP response.

The server passes every method (including `HEAD` and `OPTIONS`) and every header to the handler
unchanged. The server adds no CORS headers.

### 5.2 `PUT /worlds/{id}`

The body is a JSON object of reset options, the same keys `seahaven mcp --reset-options` takes
(`fixture`, `seed`, `now`, `clock_mode`, `state_format`, `startup`). An empty body is the same as
`{}`.

- The body's keys are laid over the server's reset options, key by key. A key set to `null`
  removes the server's value for that key, so `{"fixture": null}` makes a blank instance on a
  server whose default is a fixture.
- If instance `id` exists, it is destroyed first (after any request in flight on it finishes), and
  the answer is `200`. Otherwise the answer is `201`. The response body is `{"id": "<id>"}`.
- `PUT` is how a test gets a fresh instance: the same ID, replaced.

Refusals:

| Condition | Status |
|---|---|
| Body is not JSON, or not a JSON object | `400` |
| A key `--reset-options` does not take, or `control_tools` | `400`, naming the key and the keys that are taken |
| `world.instance(...)` refuses the options (unknown fixture, unknown clock mode, `now` earlier than the fixture, unknown startup keyword) | `400`, with Seahaven's message |
| The limit would be exceeded (§5.4) | `503` |
| Anything else raised while creating the instance, such as a startup hook failing | `500`, and the traceback is logged |

When a `PUT` that replaces an instance fails, the old instance is already gone and the ID holds no
instance.

### 5.3 `DELETE /worlds/{id}`

Destroys the instance, after any request in flight on it finishes. Answers `204` with no body, or
`404` if no instance `id` exists. A later request to `/worlds/{id}/...` creates the instance again
automatically.

### 5.4 The instance limit

The server holds at most `max_instances` instances, 100 by default; `0` means no limit. Creating
one more, automatically or by `PUT`, answers `503` with a message naming the limit and saying that
`DELETE /worlds/{id}` frees one. Replacing an existing instance with `PUT` does not count as
creating one.

### 5.5 Lifetime and concurrency

- An instance lives until it is deleted, replaced or the server stops. Stopping the server destroys
  every instance. Nothing survives a restart.
- Two first requests to the same ID at the same moment create exactly one instance.
- Requests to one instance run one at a time, under the instance's lock. Requests to different
  instances run in parallel.
- A request that arrives while a `PUT` is replacing its instance waits, then runs against the new
  instance.

### 5.6 Seeds and the clock

- **Seed.** An instance created without a `seed` in its options gets a fresh random seed of its own.
  Two instances on one server therefore do not mint the same IDs, so an ID taken from one instance
  is not accidentally valid in another. A `seed` in the server's options or in a `PUT` body is used
  as given.
- **Clock mode.** An instance created without a `clock_mode` in its options runs in `wall` mode,
  in place of the world's default. A test server should read the time the way the real product
  does. A `clock_mode` in the options is used as given.

## 6. Running the server

### 6.1 The script

A world following the convention adds a `serve_http.py` at its project root:

```py
import seahaven.http

from myworld import world
from myworld.api import handle

if __name__ == "__main__":
    seahaven.http.main(world, handle)
```

and runs it with `uv run serve_http.py --port 9000 --fixture demo`. On start, it prints one line
with the base URL a client uses, for example
`Serving myworld at http://127.0.0.1:9000/worlds/{id}`.

### 6.2 The Python API

```py
seahaven.http.app(world, handler, *, reset_options=None, max_instances=100) -> ASGI app
seahaven.http.serve(world, handler, *, host="127.0.0.1", port=8000, reset_options=None,
                    max_instances=100) -> None
seahaven.http.main(world, handler, argv=None) -> None
```

`reset_options` is the dictionary each instance is created with, before any `PUT` body is laid
over it. Its keys are checked when the app is built: an unknown key, `control_tools`, or a
`fixture` the world does not have raises there, not on the first request.

### 6.3 The command line `main` parses

| Option | Default | What it does |
|---|---|---|
| `--host` | `127.0.0.1` | the address to bind |
| `--port` | `8000` | the port to bind |
| `--max-instances` | `100` | how many instances may exist at once; `0` for no limit |
| `--fixture`, `--seed`, `--now`, `--clock-mode` | none | reset options, as `seahaven mcp` takes them |
| `--reset-options JSON` | none | all reset options as one JSON object, as `seahaven mcp` takes it |

The reset-option flags behave exactly as in `seahaven mcp`: the same environment variables
(`SEAHAVEN_FIXTURE`, `SEAHAVEN_SEED`, `SEAHAVEN_NOW`, `SEAHAVEN_CLOCK_MODE`,
`SEAHAVEN_RESET_OPTIONS`), the same refusal to mix the convenience flags with `--reset-options`,
and the same key checks. One difference: `main` does not pick one seed for the whole run, because
each instance gets its own (§5.6).

A refused option prints one line to stderr and exits `1`, as the `seahaven` command does.

### 6.4 Dependencies

`seahaven.http`'s server parts need Starlette and uvicorn, which the `serve` extra already installs.
No new dependency is added. Importing `seahaven.http.app`, `serve` or `main` without them raises an
`ImportError` that says to install the `serve` extra. The request and response types (§3.1) need
neither.

## 7. Transactions and the change log

### 7.1 One request, one transaction

The server runs the handler inside `instance.bulk()`. The request is one transaction:

- The handler returns an `HttpResponse`, of any status: the transaction commits.
- The handler raises: the transaction rolls back and the server answers `500`. The body names the
  exception type and message, and the traceback is logged.
- The handler returns something other than an `HttpResponse`: the transaction rolls back and the
  server answers `500`, saying what was returned.

A handler that must leave nothing behind on a failure checks before it writes, as a real API does,
or uses `ctx.db.transaction()` to undo part of its work.

### 7.2 What the handler sees

Under the server, `ctx` is the context `bulk()` yields: `ctx.db`, `ctx.clock`, `ctx.ids`,
`ctx.state` and `ctx.instance` work as in a tool. `ctx.call` is `None`, because a request is not a
tool call. A handler shared with tools must not depend on `ctx.call`.

### 7.3 The `tick` clock

Each request advances a `tick` clock by one step before the handler runs, as a tool call does. A
test server run with `--clock-mode tick` therefore gives each request its own, replayable
timestamp. (Priority: low. The server's default is `wall`, §5.6.)

### 7.4 A request is not a tool call

Consequences an author needs to know, stated in the docs page:

- A request takes no call ordinal and makes no call-log entry. Its change-log records have `i` set
  to `None`.
- The process-wide concurrency gate does not apply. The server's thread pool bounds how many
  requests run at once.

## 8. Errors in the handler's path

| Condition | Answer |
|---|---|
| Invalid instance ID | `400` |
| Instance limit reached on automatic creation | `503` |
| Automatic creation fails | `500`; the traceback is logged |
| Handler raises, or returns a non-`HttpResponse` | `500` (§7.1) |

## 9. Documentation and example

- A new docs page, `http_apis.md`: what the handler is and why a world would write one; writing a
  handler; calling it from a tool; the `serve_http.py` script and its options; the routes; how a
  request differs from a tool call (§7.4). It is linked from `index.md` and
  `serving_and_openenv.md`, and added to the page list in `tests/test_docs.py`. The helper's names
  are added to `reference/api.md`.
- The reference world, ProjectTracker, is not changed: it is not shaped like an HTTP API. The docs
  page's examples use a small world defined inline on the page, in the way `index.md` does, so
  they run in the test suite. The server script is shown as a `py` fragment.

## 10. Testing

- Unit tests for the types (§3.1).
- Tests that drive `seahaven.http.app` over real HTTP requests against a small test world with a
  handler: automatic creation, `PUT` create and replace with laid-over options and `null`,
  `DELETE`, the limit, every refusal in §5.2 and §8, commit on any status and rollback on raise,
  the seed and clock defaults, two concurrent first requests creating one instance, a `tick`
  clock advancing once per request.
- A test that calls the handler from a tool through `world.instance(...)`.
- A test that runs a server script as a process and makes one request to it.
- Tests for `main`'s option parsing, sharing `seahaven mcp`'s cases where the behaviour is shared.
