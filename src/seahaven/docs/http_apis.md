# HTTP APIs

Many worlds mock a REST API that has an MCP or tool wrapper around it. When this is the case:

- It is often helpful to implement the REST API inside the Seahaven world. REST documentation, such
  as an OpenAPI description, is often much more descriptive than the MCP equivalent, and easier to
  mock correctly. This leads to fewer bugs in your world. You still need tools that call the mock
  REST API, but these tools can be quite small.
- You may want to reuse the world as a mock for testing features that call the REST API. The design
  principles behind Seahaven, such as many isolated and realistic instances, help in software
  testing as well. The `seahaven.http` module lets you serve a Seahaven world as a REST API.

`seahaven.http` is a convention for implementing worlds that are really REST API wrappers. It
includes:

- A convention for the HTTP handler, the type [`HttpHandler`](#writing-a-handler), which is
  `Callable[[Ctx[Any], HttpRequest], HttpResponse]`.
- A [web server](#serving-the-handler) that you run against a Seahaven world that implements an
  `HttpHandler`. The server creates, destroys and calls world instances.

**Important:** `seahaven.http` is a completely optional module. You do not need it to use
Seahaven. Use it only if it helps, and if the world you model is based on a REST API.

| Section | What it covers |
|---|---|
| [Writing a handler](#writing-a-handler) | The request and response types, and a worked example |
| [Calling the handler from a tool](#calling-the-handler-from-a-tool) | Transactions, and turning a status into a `ToolError` |
| [Serving the handler](#serving-the-handler) | `serve_http.py`, its options, and the base URL |
| [The routes](#the-routes) | Reaching the handler, and `PUT` and `DELETE` on an instance |
| [How a request differs from a tool call](#how-a-request-differs-from-a-tool-call) | Transactions, the call log, the clock, seeds and limits |
| [Server responses](#server-responses) | The statuses the server answers itself |

## Writing a handler

A handler is a synchronous `HttpHandler` function, `(ctx, request) -> response`. The handler
reads and writes the database through `ctx`, as a tool does, and returns an `HttpResponse`. The
world does its own routing inside the function. The example below uses `match`, and a world can use
a router of its choice instead.

```python
import json

import seahaven
from seahaven.http import HttpRequest, HttpResponse

world = seahaven.World(
    name="crm",
    version="1.0.0",
    schema="""
    CREATE TABLE contacts (
        id TEXT PRIMARY KEY,
        email TEXT NOT NULL,
        created_at TEXT NOT NULL
    ) STRICT;
    """,
    state_format="seahaven.state/1",
)


def api_error(status: int, kind: str, message: str) -> HttpResponse:
    return HttpResponse.json({"error": {"type": kind, "message": message}}, status=status)


def handle(ctx: seahaven.Ctx, request: HttpRequest) -> HttpResponse:
    """The CRM's REST API."""
    match request.method, request.path.split("/")[1:]:
        case "POST", ["v1", "contacts"]:
            email = request.json().get("email")
            if not isinstance(email, str):
                return api_error(400, "invalid_request", "email is required")
            contact = {"id": ctx.ids.uuid(), "email": email, "created_at": ctx.clock.iso()}
            ctx.db.execute(
                "INSERT INTO contacts (id, email, created_at) VALUES (?, ?, ?)",
                contact["id"],
                contact["email"],
                contact["created_at"],
            )
            return HttpResponse.json(contact, status=201)
        case "GET", ["v1", "contacts", contact_id]:
            row = ctx.db.one("SELECT * FROM contacts WHERE id = ?", contact_id)
            if row is None:
                return api_error(404, "not_found", f"no contact {contact_id}")
            return HttpResponse.json(row)
        case _:
            return api_error(404, "no_route", f"no route {request.method} {request.path}")


class CrmError(seahaven.ToolError):
    """The API answered a tool with an error status."""

    def __init__(self, response: HttpResponse) -> None:
        error = json.loads(response.body_bytes)["error"]
        super().__init__(error["type"], error["message"])


def call_api(ctx: seahaven.Ctx, request: HttpRequest) -> dict[str, object]:
    response = handle(ctx, request)
    if response.status >= 400:
        raise CrmError(response)
    return json.loads(response.body_bytes)


@world.tool
def create_contact(ctx: seahaven.Ctx, email: str) -> dict[str, object]:
    """Create a contact."""
    body = json.dumps({"email": email}, ensure_ascii=False).encode()
    return call_api(ctx, HttpRequest("POST", "/v1/contacts", body=body))


@world.tool
def get_contact(ctx: seahaven.Ctx, contact_id: str) -> dict[str, object]:
    """Fetch one contact by id."""
    return call_api(ctx, HttpRequest("GET", f"/v1/contacts/{contact_id}"))


with world.instance() as inst:
    contact = inst.call("create_contact", email="ada@example.com")
    assert inst.call("get_contact", contact_id=contact["id"]) == contact
    assert [record.i for record in inst.change_log()] == [0]
    try:
        inst.call("get_contact", contact_id="nope")
    except CrmError as error:
        assert error.code == "not_found"
    else:
        raise AssertionError("get_contact did not raise")
```

The types are in `seahaven.http` and need no extra, so a world declares and tests its handler
without the server installed.

`HttpRequest(method, path, query="", headers=(), body=b"")` is one request. `path` is relative to
the instance, such as `/v1/contacts`, and `query` is the raw query string without the `?`. The
method is stored in upper case and header names in lower case, so a request a tool builds has the
same shape as one the server builds. `request.header(name)` answers the first value of a header,
and `request.json()` parses the body.

`HttpResponse(status=200, headers=(), body=b"")` is one response. Headers are `(name, value)` pairs
and a name can repeat, as `set-cookie` does. A `str` body is sent UTF-8 encoded, and
`response.body_bytes` is the body as sent. `HttpResponse.json(data, status=..., headers=...)` makes
a JSON body and adds `content-type: application/json` unless `headers` sets one. A response that
HTTP cannot carry, such as a header value with a line break in it, raises when it is made, in the
handler that made it.

## Calling the handler from a tool

A tool calls the handler with its own `ctx`, as `call_api` does above. The handler then runs inside
the tool call. Its writes are in the tool's transaction, and in the change log under the tool call's
`i`.

The handler answers an error with a status, as the real product does. The tool decides what that
status means to an agent. Raise a `ToolError`, as `CrmError` does: the agent reads its code and
message, and the tool call rolls back. [authoring.md](authoring.md#errors) covers error classes.

Seahaven adds nothing for this. It is a plain function call.

## Serving the handler

### Install the server

The server needs Starlette and uvicorn, which the `serve` extra installs.
[Install the extra](serving_and_openenv.md#install-the-serve-extra). Without it,
`seahaven.http.app` and `serve` raise an `ImportError` that says so, and `serve_http.py` prints the
same message and exits 1.

### The script

A world adds a `serve_http.py` at its project root:

```py
import seahaven.http

from crm import world
from crm.api import handle

if __name__ == "__main__":
    seahaven.http.main(world, handle)
```

Run it with `uv run`. It prints the base URL for a client and serves until you stop it:

```sh
uv run serve_http.py --port 9000 --fixture demo
```

```
Serving crm at http://127.0.0.1:9000/worlds/{id}
```

Set a client's base URL to that address, with `{id}` replaced by an instance ID you choose, such as
`http://127.0.0.1:9000/worlds/test1`. The first request to the ID creates the instance.

### Options

| Option | Env Var | Default | What it does |
|---|---|---|---|
| `--host` | -- | `127.0.0.1` | the address to bind |
| `--port` | -- | `8000` | the port to bind |
| `--max-instances N` | -- | `100` | how many instances may exist at once; `0` for no limit |
| `--fixture NAME` | `SEAHAVEN_FIXTURE` | a blank instance | the fixture each instance starts from |
| `--seed N` | `SEAHAVEN_SEED` | a random seed for each instance | the caller seed of each instance |
| `--now ISO` | `SEAHAVEN_NOW` | the fixture's `now`, or wall time for a blank instance | where each instance's clock starts |
| `--clock-mode MODE` | `SEAHAVEN_CLOCK_MODE` | `wall` | how each instance's clock moves |
| `--reset-options JSON` | `SEAHAVEN_RESET_OPTIONS` | none | every reset option as one JSON object; not allowed with the four flags above |

The last five options are the **reset options**: the keyword arguments each instance is created
with. They take the same values and variables as in `seahaven mcp`, and
[reference/cli.md](reference/cli.md#seahaven-mcp) describes them. A world's own startup keywords go
inside `"startup"` in `--reset-options`. Two defaults differ from `seahaven mcp`: each instance gets
a random seed of its own, and the clock runs in `wall` mode, so the world reads the time as the real
product does.

To serve from Python rather than from a script, call `seahaven.http.serve(world, handle, ...)`.
`serve` takes the keywords `host`, `port`, `max_instances` and `reset_options`, a dict with the keys
`--reset-options` takes. For an ASGI server of your own, `seahaven.http.app(world, handle, ...)`
builds the application, and takes `max_instances` and `reset_options`. Run one worker, because the
instances are state in one process.

## The routes

| Route | Method | What it does |
|---|---|---|
| `/worlds/{id}/...` | any | passes the request to the handler, and creates instance `id` first if it does not exist |
| `/worlds/{id}` | `PUT` | creates or replaces instance `id` |
| `/worlds/{id}` | `DELETE` | destroys instance `id` |

An instance ID is 1 to 64 letters, digits, `-` and `_`. Every path below `/worlds/{id}/` belongs to
the world: `/worlds/test1/v1/contacts` reaches the handler with the path `/v1/contacts`, and
`/worlds/test1/` with `/`. The server passes the method, the query string, every header and the
body to the handler unchanged. It adds no CORS headers.

Most clients never manage an instance, because the first request to an ID creates it. Use `PUT` to
start an instance with other reset options, or to replace an instance with a fresh one.

**`PUT /worlds/{id}`** takes a JSON object of reset options, the keys `--reset-options` takes. The
object is laid over the server's reset options key by key. A key set to `null` removes the server's
value, so `{"fixture": null}` makes a blank instance on a server started with `--fixture`. An empty
body is the same as `{}`. The answer is `201` for a new instance and `200` for a replaced one, with
the body `{"id": "<id>"}`. A request that arrives during a `PUT` waits, then runs against the new
instance.

**`DELETE /worlds/{id}`** answers `204`, or `404` when there is no instance `id`. A later request to
the ID creates the instance again.

```sh
curl -X PUT http://127.0.0.1:9000/worlds/test1 -d '{"seed": 7, "clock_mode": "tick"}'
curl -X POST http://127.0.0.1:9000/worlds/test1/v1/contacts -d '{"email": "ada@example.com"}'
curl -X DELETE http://127.0.0.1:9000/worlds/test1
```

A response that the server makes itself, rather than the handler, is JSON with the shape
`{"seahaven_error": "<message>"}`, and the message says what to fix. A client tells the server's
errors from the world's by that key. [Server responses](#server-responses) lists the statuses.

## How a request differs from a tool call

- **A request is one transaction.** The server runs the handler inside `inst.bulk()`. A response of
  any status commits, so a `400` that wrote a row keeps the row. Check before you write, as a real
  API does, or use `ctx.db.transaction()` to undo part of the work. A handler that raises, or that
  returns something other than an `HttpResponse`, rolls back. The server then answers `500` and
  logs the traceback.
- **A request is not a tool call.** A request makes no call-log entry, and its change-log records
  have `i` set to `None`. `ctx.call` is `None`, so a handler that tools also call must not depend on
  it. Tool middleware does not run, and the
  [concurrency gate](serving_and_openenv.md#the-concurrency-gate) does not apply: the server's
  thread pool limits how many requests run at once.
- **The clock.** An instance runs in `wall` mode unless its reset options name a clock mode. Under
  `tick`, each request moves the clock one step before the handler runs, as a tool call does
  ([clock.md](clock.md)).
- **The seed.** An instance created without a `seed` gets a random seed of its own, so an ID that
  `ctx.ids` makes in one instance is not valid in another. A `seed` in the server's options or in a
  `PUT` body is used as given. Give one to replay a run.
- **The instance limit.** The server holds at most `--max-instances` instances. A request that
  would create one more gets `503`, and `DELETE` frees a place. Replacing an instance with `PUT`
  does not count as creating one.
- **Lifetime and concurrency.** An instance lives until it is deleted or replaced, or until the
  server stops. Stopping the server destroys every instance, and nothing survives a restart.
  Requests to one instance run one at a time, and requests to different instances run in parallel.
- **The path is decoded.** `request.path` is percent-decoded, so an encoded slash (`%2F`) in a path
  segment arrives as `/`.

## Server responses

The server answers these statuses itself, each with the body `{"seahaven_error": "<message>"}`:

| Status | When |
|---|---|
| `400` | the instance ID is not 1 to 64 letters, digits, `-` and `_`; a `PUT` body is not a JSON object, names a key `--reset-options` does not take or `control_tools`, or has options `world.instance(...)` refuses |
| `404` | the path is not under `/worlds/`; a `DELETE` names an ID that has no instance |
| `405` | a method other than `PUT` or `DELETE` on `/worlds/{id}`, with an `allow` header |
| `500` | the handler raised or did not return an `HttpResponse`; an instance could not be created, replaced or destroyed. The traceback goes to the `seahaven.http` logger |
| `503` | creating an instance would pass the instance limit |

The server sets `content-length` from the response body, and drops a `content-length` or
`transfer-encoding` header that the handler sets.
