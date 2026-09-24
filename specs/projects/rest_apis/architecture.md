---
status: draft
---

# Architecture: REST APIs

## 1. Shape of the change

A helper, isolated in one package. Every line of code is in `src/seahaven/http/`, and nothing
outside that package imports it: not `seahaven/__init__.py`, not the CLI, not a world. The package
depends on existing modules; the dependency never runs the other way.

| Where | What changes |
|---|---|
| `src/seahaven/http/` | New. The whole feature |
| `src/seahaven/docs/` | New page `http_apis.md`; links from `index.md` and `serving_and_openenv.md`; a section in `reference/api.md` |
| `tests/` | New test modules and one test world module (§8); `tests/test_docs.py` page list |
| Everything else | Unchanged. No edit to `pyproject.toml`, `uv.lock`, `seahaven/cli/`, `seahaven/instances.py` or CI |

No new dependency. The server imports `starlette` and `uvicorn`, which both extras already
install (`openenv` brings them under `serve`, the MCP SDK under `mcp`). `seahaven.openenv.serve`
already imports `uvicorn` on the same footing. Because both environments hold them, the package
sits on neither side of the `ty` split and its tests run in both CI jobs.

## 2. Modules

```
src/seahaven/http/
  __init__.py   public names; app/serve/main as thin wrappers that import the server lazily
  messages.py   HttpRequest, HttpResponse, HttpHandler; stdlib only
  runtime.py    Registry (instances by id), dispatch(), seahaven_error(); no starlette
  server.py     the ASGI app, and serve(); imports starlette and uvicorn
  command.py    main(): argparse, reusing seahaven.cli.mcp's reset-option resolution
```

`messages.py` and `runtime.py` import nothing outside the standard library and `seahaven`, so the
types and all of the instance logic are importable and unit-testable without the server stack.

### 2.1 `__init__.py`

```py
from seahaven.http.messages import HttpHandler, HttpRequest, HttpResponse
from seahaven.http.runtime import DEFAULT_MAX_INSTANCES  # 100

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
MISSING_EXTRA = 'seahaven.http needs the serve extra: pip install "seahaven[serve]"'

def app(world, handler, *, reset_options=None, max_instances=DEFAULT_MAX_INSTANCES) -> ASGIApp
def serve(world, handler, *, host=DEFAULT_HOST, port=DEFAULT_PORT, reset_options=None,
          max_instances=DEFAULT_MAX_INSTANCES) -> None
def main(world, handler, argv: list[str] | None = None) -> None
```

`app` and `serve` each import from `seahaven.http.server` inside the function and turn a
`ModuleNotFoundError` into `ImportError(MISSING_EXTRA)`. `main` imports `seahaven.http.command`,
which imports the server only when it calls `serve`. `ASGIApp` is `starlette.types.ASGIApp`,
imported under `TYPE_CHECKING`. `__all__` lists the names above.

## 3. `messages.py`

```py
type Headers = tuple[tuple[str, str], ...]

@dataclass(frozen=True)
class HttpRequest:
    method: str
    path: str
    query: str = ""
    headers: Headers = ()
    body: bytes = b""

    def __post_init__(self) -> None:
        # object.__setattr__, the dataclass being frozen
        method -> method.upper()
        headers -> tuple((name.lower(), value) for name, value in headers)

    def header(self, name: str) -> str | None   # first match on name.lower(), else None
    def json(self) -> Any                        # json.loads(self.body); JSONDecodeError propagates


@dataclass(frozen=True)
class HttpResponse:
    status: int = 200
    headers: Headers = ()
    body: bytes | str = b""

    def __post_init__(self) -> None:
        # Checked at construction, so a tool meets the mistake as readily as the server does.
        status: an int and not a bool, 100 <= status <= 599         else TypeError / ValueError
        headers: a tuple of (str, str) pairs; no "\r" or "\n" in a name or value, and both
                 encodable as latin-1 (what HTTP/1.1 carries)       else TypeError / ValueError
        body: bytes or str                                          else TypeError

    @classmethod
    def json(cls, data: Any, *, status: int = 200, headers: Headers = ()) -> HttpResponse:
        body = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        adds ("content-type", "application/json") unless a content-type header is present
        (name compared case-insensitively)

    @property
    def body_bytes(self) -> bytes   # str bodies UTF-8 encoded


type HttpHandler = Callable[[Ctx[Any], HttpRequest], HttpResponse]
```

`HttpRequest` validates nothing beyond the normalisation: the server builds it from a parsed
request, and a tool builds it from literals.

## 4. `runtime.py`

### 4.1 Constants and small helpers

```py
DEFAULT_MAX_INSTANCES = 100
INSTANCE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")      # fullmatch
DEFAULT_CLOCK_MODE: ClockMode = "wall"
_log = logging.getLogger("seahaven.http")

def seahaven_error(status: int, message: str, *, headers: Headers = ()) -> HttpResponse:
    return HttpResponse.json({"seahaven_error": message}, status=status, headers=headers)

class CapacityReached(Exception): ...      # -> 503
class CreationFailed(Exception): ...       # wraps what world.instance(...) raised; .cause
```

### 4.2 Checking reset options

```py
def check_reset_options(world: World, options: Mapping[str, Any], *, source: str) -> None
```

Used by `app()` on the server's options (raising `WorldBug`) and by the server on a `PUT` body
(turned into `400`):

- every key is in `seahaven.cli.mcp.RESET_OPTION_KEYS`, which already leaves out
  `control_tools`. Otherwise the message names the stray keys, the keys taken, and says a world's
  own startup keywords go inside `"startup"`;
- when `source` is the server's options and `fixture` is a non-`None` string, it is one of
  `world.fixtures()` ids. Otherwise the message names the ids there are. A `PUT` body's fixture
  is left to `world.instance(...)`, whose refusal is a `WorldBug` and becomes `400` (§6.3).

`source` is the spelling used in messages: `"reset_options"` or `"the PUT body"`.

### 4.3 `Registry`

Holds the instances of one server. Synchronous and thread-safe. Every blocking operation
(creating, destroying, running a handler) is called by the server on a worker thread.

```py
@dataclass(eq=False)
class _Slot:
    lock: threading.Lock = field(default_factory=threading.Lock)
    instance: Instance | None = None
    retired: bool = False


class Registry:
    def __init__(self, world: World, defaults: Mapping[str, Any], max_instances: int) -> None
    def run[T](self, id: str, fn: Callable[[Instance], T]) -> T
    def put(self, id: str, body: Mapping[str, Any]) -> bool        # True when created
    def delete(self, id: str) -> bool                              # False when absent
    def close(self) -> None
```

State: `_lock` (a `threading.Lock`), `_slots: dict[str, _Slot]`, `_live: int` (reservations).

**Locks.** `_lock` guards `_slots` and `_live`, is held only for dictionary and counter updates,
and is never held while a slot lock is taken. A slot lock serialises everything done to one ID:
creating, replacing, destroying and running requests. Requests to one instance run one at a time
anyway under the instance lock, so holding the slot lock across a request costs nothing, and it is
what makes "a request that arrives during a `PUT` waits, then runs against the new instance" true.
The order is always slot lock, then the instance's own locks, inside `world.instance`, `bulk` and
`destroy`.

**Getting a slot.** `_slot(id)`: under `_lock`, return `_slots[id]`, inserting a new `_Slot` if
absent. Every operation then takes `slot.lock` and first checks `slot.retired`. A retired slot
was removed by another thread while this one waited, so the operation starts again with a fresh
`_slot(id)`. The loop ends because a new slot is never retired before its lock is taken.

**Retiring.** `_retire(id, slot)`: set `slot.retired = True`, then under `_lock` remove
`_slots[id]` only if it is still `slot`. Called with `slot.lock` held.

**Reservations.** `_reserve()`: under `_lock`, raise `CapacityReached` if
`max_instances and _live >= max_instances`, else `_live += 1`. `_release()`: `_live -= 1` under
`_lock`. The invariant is that `_live` counts slots whose instance exists or is being created, so
two slots creating at once cannot both pass the limit.

**`run(id, fn)`**:

```
loop:
    slot = _slot(id)
    with slot.lock:
        if slot.retired: continue
        if slot.instance is None:
            _create(id, slot, dict(self._defaults))
        return fn(slot.instance)
```

**`_create(id, slot, options)`** (slot lock held, `slot.instance` is `None`):

```
try: _reserve()
except CapacityReached: _retire(id, slot); raise
try:
    slot.instance = _make(options)
except Exception as error:
    _release(); _retire(id, slot)
    raise CreationFailed(error) from error
```

**`_make(options)`**:

```
kwargs = dict(options)
if "seed" not in kwargs: kwargs["seed"] = random.randrange(SEED_CEILING)   # seahaven.cli.mcp
if "clock_mode" not in kwargs: kwargs["clock_mode"] = DEFAULT_CLOCK_MODE
instance = world.instance(**kwargs)
_log.info("created instance %s with seed %s", ...)
return instance
```

A key that is present is used as given, `None` included, so `--reset-options '{"seed": null}'`
gives the framework's constant seed and `{"clock_mode": null}` the world's default mode, as the
same keys do for `seahaven mcp`.

**`put(id, body)`**: the options are the defaults with the body laid over them. A body key whose
value is `None` removes that key; any other value replaces it.

```
options = {**defaults, **body}; drop every key whose value in body is None
loop:
    slot = _slot(id)
    with slot.lock:
        if slot.retired: continue
        existed = slot.instance is not None
        if existed:
            slot.instance.destroy(); slot.instance = None; _release()
        _create(id, slot, options)     # may raise CapacityReached or CreationFailed
        return not existed
```

Releasing before `_create` re-reserves means a replacement can only be refused for capacity when
another ID took the freed place in between. That is a correct refusal, not a leak, and the old
instance is gone either way, as the functional spec says.

**`delete(id)`**: under `_lock`, look up `_slots.get(id)`; `None` means `False`. Then take
`slot.lock`. If it is retired or holds no instance, return `False`. Otherwise destroy, release,
retire and return `True`.

**`close()`**: take a snapshot of `_slots` under `_lock`, then for each slot, with its lock held:
destroy the instance if present (logging, not raising, an exception from `destroy`), release, and
retire. Called once, at shutdown.

### 4.4 `dispatch`

```py
def dispatch(instance: Instance, handler: HttpHandler, request: HttpRequest) -> HttpResponse:
    try:
        with instance.bulk() as ctx:
            instance.clock._call_started()
            response = handler(ctx, request)
            if not isinstance(response, HttpResponse):
                raise WorldBug(
                    f"the HTTP handler returned {type(response).__name__}, not an HttpResponse"
                )
        return response
    except Exception as error:
        _log.exception("the HTTP handler failed on %s %s", request.method, request.path)
        return seahaven_error(500, f"the handler raised {type(error).__name__}: {error}")
```

- `bulk()` holds the instance lock and one transaction per node. It commits when the block exits
  normally, for any status, and rolls back when the block raises. The non-response check raises
  *inside* the block so that it rolls back too. An exception raised by the commit itself (a
  deferred constraint, for example) reaches the same `except` and is a `500`.
- `instance.clock._call_started()` is the one line behind the `tick` feature. It is the private
  method `Instance._next_ordinal` calls, which only moves the counter that `tick` mode reads, so
  calling it on every request is harmless in the other modes. The instance lock is held, as it is
  in `_next_ordinal`. It runs before the handler, so the first request reads one step past the
  start, as the first tool call does. This is the only private member the package touches.
- `ctx.call` is `None`: `bulk()` yields the root context with no call attached.
- `BaseException` other than `Exception` (for example `KeyboardInterrupt`) is not caught.

## 5. `server.py`

### 5.1 `app()`

```py
def app(world, handler, *, reset_options=None, max_instances=DEFAULT_MAX_INSTANCES) -> ASGIApp:
    if not callable(handler): raise WorldBug("handler must be a function (ctx, request) -> ...")
    if max_instances < 0 (or not an int): raise WorldBug(...)
    defaults = dict(reset_options or {})
    check_reset_options(world, defaults, source="reset_options")   # WorldBug
    return _App(handler, Registry(world, defaults, max_instances))
```

### 5.2 `_App`: a plain ASGI callable

The app is a small class implementing ASGI directly, rather than a Starlette `Router`. There is one
catch-all, and Starlette's routing would add its own `404`/`405` bodies, `GET`-only defaults for
function endpoints and slash redirects, which each need turning off. From Starlette the app uses
only `Request` (to read the body) and `Response` (to send), plus
`starlette.concurrency.run_in_threadpool`.

```py
class _App:
    async def __call__(self, scope, receive, send) -> None:
        match scope["type"]:
            case "lifespan": await self._lifespan(receive, send)
            case "http":
                response = await self._respond(scope, Request(scope, receive))
                await _to_starlette(response)(scope, receive, send)
            case _:  # websocket: refuse the handshake
                await send({"type": "websocket.close", "code": 1000})
```

`_lifespan`: loop on `receive()`. Answer `lifespan.startup` with `lifespan.startup.complete`. On
`lifespan.shutdown`, `await run_in_threadpool(registry.close)`, answer
`lifespan.shutdown.complete`, and return.

### 5.3 Routing: `_respond(scope, request) -> HttpResponse`

```
path = scope["path"], with scope.get("root_path", "") removed from its front when present
m = re.fullmatch(r"/worlds/([^/]*)(/.*)?", path, re.DOTALL)
no match               -> 404 "no route <path>: the world's API is under /worlds/{id}/, and
                              PUT or DELETE /worlds/{id} manages an instance"
id fails INSTANCE_ID   -> 400 "instance ids are 1 to 64 letters, digits, '-' or '_', not <id!r>"
m[2] is None:
    PUT    -> _put(id, request)
    DELETE -> _delete(id)
    other  -> 405, header ("allow", "PUT, DELETE"),
              "/worlds/{id} takes PUT or DELETE; the world's API is under /worlds/{id}/"
else       -> _forward(id, m[2], scope, request)
```

`scope["path"]` is already percent-decoded by the server, so `HttpRequest.path` is decoded. One
consequence goes in the docs' limits: an encoded slash (`%2F`) in a path segment arrives as `/`.

### 5.4 The three operations

**`_forward`**:

```
http_request = HttpRequest(
    method=scope["method"], path=rest,
    query=scope["query_string"].decode("latin-1"),
    headers=tuple((k.decode("latin-1"), v.decode("latin-1")) for k, v in scope["headers"]),
    body=await request.body(),
)
try: return await run_in_threadpool(registry.run, id, partial(dispatch, handler=..., request=...))
except CapacityReached: 503 _capacity_message()
except CreationFailed as e: log with traceback; 500 f"instance {id} could not be created: {e.cause}"
```

(`dispatch` takes `instance` positionally, so the `partial` binds the other two by keyword.)

**`_put`**:

```
raw = await request.body()
body = {} if raw.strip() == b"" else json.loads(raw)     # JSONDecodeError/UnicodeDecodeError -> 400
not a dict                                               -> 400
check_reset_options(world, body, source="the PUT body")  # WorldBug -> 400
try: created = await run_in_threadpool(registry.put, id, body)
except CapacityReached: 503
except CreationFailed as e:
    SeahavenError cause -> 400 with str(cause)
    anything else       -> log with traceback; 500 f"instance {id} could not be created: ..."
201 if created else 200, HttpResponse.json({"id": id})
```

**`_delete`**: `204` with an empty body if `registry.delete` returns true, else `404`,
`"no instance <id>"`.

`_capacity_message()`: `"this server holds at most {n} instances; DELETE /worlds/{id} to free
one"`.

### 5.5 `_to_starlette(response)`

```
out = starlette.responses.Response(content=response.body_bytes, status_code=response.status)
out.raw_headers += [(n.lower().encode("latin-1"), v.encode("latin-1"))
                    for n, v in response.headers if n.lower() != "content-length"]
```

Starlette computes `content-length` from the body (and omits it for `1xx`, `204` and `304`), so a
handler's own `content-length` is dropped rather than risk a mismatch. Repeated headers, such as
two `set-cookie`, survive because `raw_headers` is a list. uvicorn sends no body for `HEAD`.

### 5.6 `serve()`

```py
def serve(world, handler, *, host, port, reset_options, max_instances) -> None:
    served = app(world, handler, reset_options=reset_options, max_instances=max_instances)
    print(f"Serving {world.name} at {base_url(host, port)}", flush=True)
    uvicorn.run(served, host=host, port=port, workers=1, log_level="info")
```

`base_url` gives `http://<reachable>:<port>/worlds/{id}`, where `reachable` is `127.0.0.1` for
`0.0.0.0`, `::` or `""`, and an IPv6 literal is bracketed. The same five lines are in
`seahaven.openenv.serve.console_url`. They are repeated here rather than imported, because
importing that module imports `openenv`. `workers=1` for the reason `seahaven.openenv.serve` gives:
instances are in-process state. `print` rather than logging, for the reason given in the same
place.

## 6. `command.py`: `main`

```py
def main(world, handler, argv=None) -> None:
    parser = argparse.ArgumentParser(
        prog=Path(sys.argv[0]).name,
        description=f"Serve the {world.name} world's HTTP API: one instance per id, under /worlds/{{id}}/.",
    )
    --host (default None -> DEFAULT_HOST), --port (int, default None -> DEFAULT_PORT),
    --max-instances (int, default None -> DEFAULT_MAX_INSTANCES; negative -> refusal)
    --fixture, --seed, --now, --clock-mode, --reset-options   # same dests, metavars and help as
                                                                # seahaven mcp's, written out here
    args = parser.parse_args(argv)
    try:
        options = _reset_options(args)
        from seahaven.http import serve  # the wrapper, so a missing extra is MISSING_EXTRA
        serve(world, handler, host=..., port=..., reset_options=options, max_instances=...)
    except (CliError, SeahavenError, ImportError) as error:
        print(error, file=sys.stderr)
        raise SystemExit(1) from None
```

**Reusing `seahaven mcp`'s option logic without changing it.** `seahaven.cli.mcp.resolve_options`
reads exactly the attributes `fixture`, `seed`, `now`, `clock_mode` and `reset_options` from the
namespace, which the five arguments above set up. It already applies the environment variables,
the mixing refusal, JSON parsing, the key checks and the integer check on `--seed`. Two
differences are handled around it, not inside it:

1. **Seed.** When nobody gave a seed, `resolve_options` picks one for the whole run and reports it
   as `Options.random_seed`. `_reset_options` removes the `"seed"` key when `random_seed is not
   None`, which restores "nobody gave a seed", and the registry then picks one per instance.
2. **`control_tools`.** `resolve_options` refuses it with a message that names `seahaven mcp`.
   `_reset_options` first looks for `control_tools` in the general source: the `--reset-options`
   value, else a non-blank `SEAHAVEN_RESET_OPTIONS`. If that value parses as a JSON object holding
   the key, it raises its own `CliError`:
   `'--reset-options does not take "control_tools": this server serves the world's HTTP handler
   and nothing else'`. The spelling is the variable's name when that was the source. Anything
   that does not parse is left for `resolve_options` to refuse in its own words.

The only names used from `seahaven.cli.mcp` are `resolve_options`, `RESET_OPTION_KEYS`,
`SEED_CEILING`, `CONVENIENCE` and `GENERAL_VARIABLE`. The five `add_argument` calls are
repeated, not shared, because `seahaven mcp` builds them inside `add_parser` and this package
changes nothing outside itself. A test pins that the two parsers agree (§8.5).

## 7. Error handling summary

| Where | Raised | Becomes |
|---|---|---|
| `app()` / `serve()` arguments | `WorldBug` | raised to the caller; `main` prints it and exits 1 |
| `main` options | `CliError` | one line on stderr, exit 1 |
| Route, ID, method | none | `404` / `400` / `405` from `_respond` |
| `PUT` body | JSON error, `WorldBug` from `check_reset_options` | `400` |
| Creation | `CapacityReached` | `503` |
| Creation on `PUT` | `CreationFailed(SeahavenError)` / other | `400` / `500` + traceback |
| Creation on first request | `CreationFailed` | `500` + traceback |
| Handler raises, returns a non-response, commit fails | any `Exception` | `500` + traceback, rolled back |

Tracebacks go to the `seahaven.http` logger with `_log.exception`. With no handler configured,
Python's last-resort handler prints them to stderr, which is where a developer running
`serve_http.py` is looking.

## 8. Testing

Test modules live in `tests/`. Server tests start with `pytest.importorskip("starlette")`, so a
bare environment skips them. Every environment CI runs has Starlette.

### 8.1 The test world: `tests/http_world.py`

A module, not a package under `tests/worlds/`, because these tests need a `World` object and a
script, not discovery. It has no committed fixtures.

```py
def build(fixtures_dir: Path | None = None) -> seahaven.World   # a fresh world each call
def handle(ctx, request) -> HttpResponse                        # the routes below

if __name__ == "__main__":
    seahaven.http.main(build(), handle)
```

- The schema is one `STRICT` table: `notes(id TEXT PRIMARY KEY, body TEXT NOT NULL,
  created_at TEXT NOT NULL)`.
- The world has a startup hook taking `explode: bool = False`, which raises `RuntimeError` when
  it is true.
- Tools: `get_note(ctx, note_id)` and `create_note(ctx, body)`, each a wrapper over `handle`, and
  each raising a `ToolError` subclass on a non-2xx status.

| Route | Behaviour |
|---|---|
| `POST /notes` | inserts `{"body": ...}` from the JSON body, with `ctx.ids.uuid()` and `ctx.clock.iso()`; `201` with the note |
| `POST /notes?reject=1` | inserts, then returns `400` (commit on any status) |
| `GET /notes/{id}` | `200` with the note, or `404` |
| `GET /notes` | `200` with every note, ordered by id |
| `POST /explode` | inserts, then raises `RuntimeError` (rollback) |
| `GET /wrong` | returns a `dict` (a non-response) |
| `ANY /echo...` | `200` with method, path, query, headers and body as text; response headers include two `set-cookie` and a wrong `content-length` |
| `GET /context` | `{"call_is_none": ctx.call is None, "now": ctx.clock.iso()}` |
| anything else | `404` JSON |

### 8.2 `tests/test_http_messages.py`

Normalisation of method and header names; `header()` is case-insensitive and returns the first
value or `None`; `json()` on a request; `HttpResponse.json` sets the content type, keeps one given
in any case, and encodes non-ASCII as UTF-8; `body_bytes`; each refusal in `__post_init__`
(status out of range, `True` as a status, a non-pair header, a CR/LF in a value, a non-latin-1
value, a bad body type); frozen.

### 8.3 `tests/test_http_runtime.py`: `Registry` and `dispatch`, no HTTP

The world is built with `fixtures_dir=tmp_path`. A fixture is made by freezing an instance.

- `run` creates on first use, and uses the same instance after.
- Concurrency: 8 threads behind a `threading.Barrier` call `run` on one ID. Exactly one instance
  exists, and `world.instance` was called once (counted by wrapping it with `monkeypatch`).
- Limit: the `max_instances + 1`-th ID raises `CapacityReached`, and no slot is left behind;
  replacing with `put` does not count; `delete` frees a place; a failed creation frees its
  reservation (proved by then creating up to the limit); `0` means no limit.
- `put`: `True` then `False`; the replacement is a new instance and the old one's directory is
  gone; the body is laid over the defaults, and a `null` removes the default (fixture default,
  then `{"fixture": null}` gives a blank instance).
- `delete`: `True`, then `False`; a `run` after it creates a new instance.
- `close` destroys every instance: each directory is removed.
- Seeds: two instances without a seed mint different `ctx.ids.uuid()`; an explicit seed
  replays; `{"seed": None}` in the defaults is passed through as `None`.
- Clock: the default mode is `wall`; an explicit `clock_mode` is kept; under `tick`, three
  requests read three successive ticks.
- `dispatch`: a `400` response commits its write; a raise rolls back and gives `500` with the
  exception named; a non-response rolls back and gives `500`; `ctx.call` is `None`.

### 8.4 `tests/test_http_server.py`: over real HTTP

A real uvicorn server on port `0` in a thread, with `httpx`, in the way `tests/serving.py` does it
for OpenEnv (that helper imports `openenv`, so this module has a small helper of its own). One case
per row of the functional spec's route table and §5.2 and §8:

- routes: unknown path `404`; bad ID `400`; `GET /worlds/a` `405` with `allow`; websocket
  refused;
- forwarding: `/worlds/a/` reaches the handler as `/`; method, path, query, repeated request
  headers and body arrive unchanged; two `set-cookie` come back; the handler's wrong
  `content-length` is replaced; `HEAD` gets headers and no body;
- `PUT`: `201` then `200`; bad JSON, a non-object and an unknown key are `400`; `control_tools` is
  `400`; an unknown fixture is `400`; `{"startup": {"explode": true}}` is `500`; the limit is
  `503`; an empty body is `{}`;
- `DELETE`: `204` then `404`;
- the handler: `500` bodies carry `seahaven_error`; a first request whose creation fails is `500`;
- shutdown: stopping the server removes the instances' directories.

### 8.5 `tests/test_http_command.py`

`main` with `seahaven.http.server.serve` replaced by a recorder (`monkeypatch`):

- the defaults reach `serve` (`127.0.0.1`, `8000`, `100`, `{}`);
- each flag and each environment variable reaches `reset_options`;
- no seed given: no `"seed"` key; `--seed 7`: `7`;
- mixing a convenience flag with `--reset-options` exits 1 with `seahaven mcp`'s message;
- `control_tools` by flag and by variable exits 1 with this package's message;
- `--max-instances -1` and an unknown fixture exit 1 with one line;
- the reset-option arguments of `main`'s parser and of `seahaven mcp`'s have the same option
  strings, dests and metavars (built from `seahaven.cli.build_parser()`).

One process test: run `python tests/http_world.py --port 0`, read uvicorn's `Uvicorn running on
http://127.0.0.1:<port>` line from its stderr to learn the port, check the `Serving notes_api at
...` line on stdout, make one `POST` and one `GET`, then terminate the process.

### 8.6 The real entry point through `world.instance(...)`

In `test_http_runtime.py`: `inst.call("create_note", body="x")` and then
`inst.call("get_note", ...)` succeed; `get_note` of a missing ID raises the tool's `ToolError`;
the handler's insert appears in `inst.change_log()` under the tool call's `i`; a tool that writes
and then gets a `4xx` from the handler rolls back its own write by raising.

### 8.7 Docs

`http_apis.md` examples: one `python` fence defines an inline world, a handler, a tool that wraps
it, and drives the tool through `world.instance(...)`. It imports only `seahaven` and
`seahaven.http`'s types, so it runs in every environment. The server script is a `py` fence.
`tests/test_docs.py`'s `PAGES` gains `http_apis.md`.

## 9. Documentation

`src/seahaven/docs/http_apis.md`, in the docs style of `AGENTS.md`:

1. What it is: a world written as an HTTP handler, with tools over it, and served as a test server.
2. Writing a handler: the types, and routing is the world's own.
3. Calling it from a tool, and turning a status into a `ToolError`.
4. Serving it: `serve_http.py`, `uv run serve_http.py`, the options table, the base URL for an SDK.
5. The routes, and `PUT` options, including laying options over the defaults and `null`.
6. How a request differs from a tool call: one transaction that commits on any status, no call
   log entry, `ctx.call` is `None`, a `tick` step per request, the default `wall` clock, a random
   seed per instance, the instance limit, nothing survives a restart, encoded slashes.

`reference/api.md` gains a `seahaven.http` section listing every public name. `index.md` and
`serving_and_openenv.md` each gain a one-line link.
