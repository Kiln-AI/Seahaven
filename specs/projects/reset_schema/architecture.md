---
status: complete
---

# Architecture: Reset Schema

Single doc; no component designs. Three pieces: a pydantic model factory (`env.py`), one route
(`openenv/__init__.py`), and a console change (`ui/`). See `functional_spec.md` for behaviour.

## 1. Server: the reset model (`src/seahaven/openenv/env.py`)

### 1.1 Classes

```py
class SeahavenResetRequest(ResetRequest):
    """The reset message: OpenEnv's two fields and the keywords `SeahavenEnv.reset` adds."""

    model_config = ConfigDict(extra="forbid")

    fixture: str | None = Field(default=None, description=..., json_schema_extra=...)  # §1.3
    now: str | None = Field(default=None, description=...)
    state_format: str | None = Field(default=None, description=...)
    startup: dict[str, Any] | None = Field(default=None, description=...)

    @classmethod
    def for_world(cls, world: World) -> type[SeahavenResetRequest]: ...
```

- `SeahavenResetRequest` is world-independent, like `SeahavenState`. `for_world(world)` returns a
  **subclass** built with `pydantic.create_model(f"{...}", __base__=cls, ...)` that narrows
  `state_format` and `startup` for one world and binds the fixture enum to that world. The subclass
  is the app's `reset_cls`: built once in `app()`, passed to the route the way `state_cls` is passed
  to `create_app`.
- The subclass is created with the model name `"SeahavenResetRequest"` too, so the schema's
  `title` is that and no generated name reaches the wire (matching `SeahavenState` on `/schema`).
  `create_model` refuses `__config__` together with `__base__`; the base's `extra="forbid"` is
  inherited, so none is passed. (Prototyped against the locked pydantic/openenv: title,
  `additionalProperties: false`, the filled `fixture` `anyOf` and `$defs/SeahavenStartup` all come
  out as described here.)
- `model_config = ConfigDict(extra="forbid")` overrides `ResetRequest`'s `extra="allow"`, which
  gives `additionalProperties: false`. The inherited `json_schema_extra` examples
  (`{"seed": 42, "episode_id": ...}`) stay; they are valid for this model too.
- The model is used for its schema only. `SeahavenEnv.reset` keeps validating exactly as today.
  Docstring says so in one line.
- Field descriptions: short, docs style. Suggested text:
  - `fixture`: "The fixture to start from, by id. Null starts a blank instance from the world's
    schema."
  - `now`: "The blank instance's clock, as an ISO-8601 instant. Refused with a fixture, which
    carries its own clock."
  - `state_format`: "The format of this episode's state documents. Null uses the world's pinned
    format."
  - `startup`: "The world's own startup keywords, passed to its startup hooks."

### 1.2 `state_format`

`Literal[*names] | None`, where `names` is `sorted(BUILTIN_FORMATS) + sorted(world's registered
formats)`, deduplicated. The registered set is `world._state_formats` — add a public read-only
property `World.state_formats -> frozenset[str]` (names only) rather than reaching into the private
dict from `openenv/`. It is small and has an obvious name; add one line for it in
`docs/reference/api.md`.

### 1.3 `fixture`: enum filled at schema-generation time

The field stays `str | None`. Its `json_schema_extra` is a closure over the world, set by
`for_world` (override the field with `Annotated[str | None, Field(json_schema_extra=fill)]` in
`create_model`). Pydantic calls it on every `model_json_schema()` and does not cache the result
(verified against the locked pydantic: a second call after the list changes shows the new list).

```py
def fill(schema: dict[str, Any]) -> None:
    ids = [fixture.id for fixture in world.fixtures()]
    schema["anyOf"] = [{"type": "string", "enum": ids}, {"type": "null"}] if ids else [{"type": "null"}]
```

- `world.fixtures()` reads the directory on every call and returns `[]` for a world with no
  fixtures directory. An error from it propagates (the route answers 500).
- `ids` is already sorted by `world.fixtures()`.
- Only the root world's fixtures: a reset names a fixture of the root world.

### 1.4 `startup`: the keyword model

A private function `_startup_model(world: World) -> type[BaseModel]` in `env.py`:

1. `composition = world.composition()` (seals; a composition error is already a `WorldBug`).
2. Walk `composition.nodes` (root first, breadth-first — the framework's tree order), and each
   node's `world.startup_hooks` in registration order. For each `hook`:
   - `sig = inspect.signature(hook.fn, eval_str=True)`. A `NameError` → `WorldBug` naming the hook
     (same wording pattern as `tool._signature`).
   - Every `KEYWORD_ONLY` parameter `p` → a candidate `(annotation, default)`:
     annotation is `Any` if `p.annotation is empty`, else `p.annotation`; default is `...` if
     `p.default is empty`, else `p.default`.
   - If `hook.takes_var_kwargs`: remember `open_ended = True`.
   - If the name is already collected with a **different annotation** (`!=`), raise
     `WorldBug(f"startup keyword {name!r} is annotated {a} by {hook_a} and {b} by {hook_b}; one
     keyword reaches both hooks, so give them one type")`. Hook names via
     `seahaven.call.name_of(fn)` (the helper `composition.py` already uses). Same annotation: keep
     the first (its default wins).
3. `create_model("SeahavenStartup", __config__=ConfigDict(extra="allow" if open_ended else
   "forbid"), **fields)`.
4. Call `model.model_json_schema(mode="validation")` once, inside `try`. On failure, find the
   culprit by building each field alone (as `tool._blame` does) and raise
   `WorldBug(f"startup hook {hook}: keyword {name!r}: no JSON schema: {error}")`. Keep a
   `name -> hook` map from step 2 for the message. Do not share `tool._blame` (its messages say
   "tool"); a ten-line local loop is fine.

`for_world` then sets `startup: model | None = None` on the subclass. Pydantic emits the model under
`$defs/SeahavenStartup` with `startup: {anyOf: [{$ref}, {type: null}]}`; the console resolves it
(§3.3).

A hook's `ctx` parameter is positional and is skipped by taking keyword-only parameters only —
exactly the set `RegisteredStartupHook.accepts` holds, so the schema's names equal
`composition.accepted_startup_kwargs` (asserted in a test).

### 1.5 Response model

```py
class SeahavenSchemaResponse(SchemaResponse):
    """`GET /schema`'s answer plus the reset message, as OpenEnv would answer with a `reset_cls`."""

    reset: dict[str, Any] = Field(description="JSON schema for the reset message")
```

In `env.py` next to the other models, exported from `seahaven.openenv` alongside
`SeahavenState` if that module has an `__all__` listing models.

## 2. Server: the route (`src/seahaven/openenv/__init__.py`)

```py
SCHEMAS_PATH = "/seahaven/schemas"

def _serve_schemas(served: FastAPI, reset_cls: type[SeahavenResetRequest]) -> None:
```

- Finds upstream's `GET /schema` route in `served.router.routes` (an `APIRoute` with
  `path == "/schema"` and `"GET" in methods`) and keeps its `endpoint`. If there is none, raise
  `RuntimeError` at app build: upstream changed, and a silent second implementation is exactly the
  drift the spec forbids. This is how "the shared keys are shared code" is met: our handler awaits
  upstream's own `get_schemas()` and extends its result.
- Registers `@served.get(SCHEMAS_PATH, include_in_schema=False, response_model=SeahavenSchemaResponse)`:

  ```py
  async def schemas() -> SeahavenSchemaResponse:
      base = await upstream()
      return SeahavenSchemaResponse(**base.model_dump(), reset=reset_cls.model_json_schema())
  ```

  `upstream` is `async def get_schemas()` in openenv 0.5.x. Call it as `result = upstream()`, then
  `await` it if `inspect.isawaitable(result)`, so a sync upstream handler still works.
- Comment at the function: this is what `create_app(reset_cls=...)` plus a `reset` key on
  `/schema` would give, and it goes away (replaced by passing `reset_cls`) if OpenEnv accepts that.
- In `app()`: `reset_cls = SeahavenResetRequest.for_world(world)` **before** `create_app`, so a
  `WorldBug` fails `app()` before anything else is built. Then `_serve_schemas(served, reset_cls)`
  after `_refuse_mcp_transport` and before the console. Always, not gated on `console`.
- `app()` docstring gains one paragraph on the route.

`GET /seahaven/schemas` must be registered before any catch-all; there is none today. The
`_refuse_http_episode_control` set does not include it.

## 3. Console (`ui/`)

### 3.1 Fetch (`src/lib/openenv.ts`)

```ts
export type EnvSchema = { action?: JsonSchema; observation?: JsonSchema; state?: JsonSchema; reset?: JsonSchema }

export async function fetchSchema(root: string): Promise<EnvSchema | null> {
  // Seahaven-specific: `/seahaven/schemas` is `/schema` plus a `reset` key ...
  const extended = await fetchJson<EnvSchema>(root, "seahaven/schemas")
  if (extended && isObject(extended.reset)) return extended
  return fetchJson<EnvSchema>(root, "schema")
}
```

- One function, same name, so `App.tsx` needs no change for the environment panels.
- The comment: Seahaven-specific code in a generic console; `/seahaven/schemas` is `/schema` plus
  `reset`; if OpenEnv accepts `reset` in `/schema`, this becomes a plain `/schema` read.
- `isObject`: non-null, non-array object with `properties` being an object. Anything else counts as
  "no reset schema" and triggers the fallback.

### 3.2 Shared form (`src/components/ArgsForm.tsx`, new)

Move `ArgsForm` and `useFormValues` out of `panels.tsx` unchanged into their own module and export
them. `panels.tsx` imports them. No behaviour change for the tool panel.

`useFormValues(fields, key)` gains an optional third argument `seed?: Record<string, string |
boolean>`, merged over the initial values when `key` changes. That is how prefill works (§3.4).

### 3.3 Reset fields (`src/lib/schema.ts`)

```ts
export const STARTUP_PREFIX = "startup."

/** The reset schema as one flat field list: top-level fields, then the startup keywords. */
export function resetFieldsOf(reset: JsonSchema): { fields: Field[]; startupOpen: boolean }

/** Coerced flat values back to a reset message, with startup keywords nested under `startup`. */
export function nestResetArgs(values: Record<string, unknown>): Record<string, unknown>

/** A reset message to flat form values; `null` when a key has no field (dialog opens raw). */
export function flattenResetArgs(args: Record<string, unknown>, fields: Field[]): Record<string, string | boolean> | null
```

- `resetFieldsOf`:
  - `top = fieldsOf(reset, ["startup"])`.
  - Resolve `reset.properties.startup`: unwrap `anyOf [X, null]`, then `$ref` against `reset.$defs`.
    Reuse the module's `deref`/`unwrapNullable` (export nothing new beyond these three functions).
  - `inner = fieldsOf({ ...startupSchema, $defs: reset.$defs })` — passing `$defs` so nested refs
    resolve. Each inner field gets `name = STARTUP_PREFIX + name` and `label = "startup · " +
    label` (the "tell it is a world keyword" labelling from spec §5.2).
  - `startupOpen = startupSchema.additionalProperties === true` (unused by the form beyond a hint
    line; kept so the dialog can say "this world accepts other startup keywords — use Raw JSON").
  - Returns `[...top, ...inner]`.
- `nestResetArgs`: moves every `startup.`-prefixed key into a `startup` object (prefix stripped);
  omits `startup` when empty.
- `flattenResetArgs`: inverse, producing form strings via the same formatting `initialValue` uses
  (export a small `toFormValue(field, value)` from the existing `initialValue` logic rather than
  duplicating it). Returns `null` if any top-level key other than `startup`, or any `startup` key,
  has no matching field.
- A field name that itself contains `.` at top level cannot occur (the model's fields are fixed and
  known); startup keywords are Python identifiers, so no collision with the prefix.

### 3.4 Dialog (`src/components/NewEnvDialog.tsx`)

- New state: `resetSchema: JsonSchema | null`, fetched with `fetchSchema(root)` inside the existing
  debounced probe effect (`Promise.all` with `fetchMetadata`), guarded by the same `probeToken`.
  `null` while looking and on any failure.
- If `resetSchema` is null: render exactly today's disclosure + textarea (unchanged code path).
- If present:
  - `fields = useMemo(() => resetFieldsOf(resetSchema).fields, [resetSchema])`.
  - Form key: `JSON.stringify(resetSchema)` hashed cheaply — use the schema's property names plus
    fixture enum joined as the `useFormValues` key, so the form resets when the target world changes
    but not on every render.
  - Prefill: `seed = defaultArgs ? flattenResetArgs(defaultArgs, fields) : undefined`. If
    `flattenResetArgs` returns `null`, start in raw mode with `JSON.stringify(defaultArgs, null, 2)`.
  - Render a "Reset arguments" heading row with the same Raw JSON `Switch` as the tool panel
    (switching to raw serializes `nestResetArgs(coerce(fields, values).values)`), then `ArgsForm`
    or the raw `Textarea` with `parseArgsObject` errors. Not inside a collapsed disclosure: it is
    one flat form, always visible.
  - If `startupOpen`, one muted line under the form: "This world also accepts startup keywords it
    does not list. Add them with Raw JSON."
  - Submit: raw → `parseArgsObject(text)`; form → `coerce(fields, values)`; any errors → show and
    stop; else `resetArgs = nestResetArgs(values)`. The Open button is disabled while the raw text
    fails to parse (as today); coerce errors appear on submit (as in the tool panel).
- The dialog header comment gains one sentence: the form is Seahaven-specific and falls back to
  the JSON box for any environment that publishes no reset schema.

### 3.5 Dev tooling

- `vite.config.ts`: add `"/seahaven": { target: TARGET, changeOrigin: true }` to the proxy.
- `mock/server.mjs`: answer `GET /seahaven/schemas` with `/schema`'s body plus a `reset` schema
  shaped like the real one (fixture enum with the mock's fixture names, `state_format` enum,
  `startup` `$ref` with one `user_id` keyword), **only in the default mode**. `--plain` answers 404
  for it, so the plain mock exercises the fallback.
- `e2e/smoke.mjs`: against the default mock, open the dialog, pick a fixture chip and fill
  `startup · User id`, open, and assert the reset frame the mock received carries
  `{"fixture": ..., "startup": {"user_id": ...}}` (the mock already records frames, or add that).
  Against the plain mock, assert the "Advanced: reset arguments" textarea is present. Screenshot
  both.
- Rebuild and replace `src/seahaven/openenv/console/index.html` (CI compares it to the build).
- `ui/README.md`: the fetch paragraph and the dev-proxy line mention `/seahaven/schemas`.

## 4. Error handling summary

| Where | Failure | Result |
|---|---|---|
| `app()` | hook annotation unresolvable / unschemable / conflicting | `WorldBug`, serve does not start |
| `app()` | upstream `/schema` route missing | `RuntimeError`, serve does not start |
| request | fixtures directory unreadable / bad sidecar | exception → 500 |
| console | endpoint 404/500/network/bad body | fall back to `/schema`; no reset form |

## 5. Tests

`tests/test_server.py` style (a real server via `serving(world)`) for the route; direct calls for
the model where a server adds nothing. New file `tests/test_reset_schema.py` unless an existing
module is the obvious home.

1. **Route, reference world** (projecttracker): `GET /seahaven/schemas` → `200`; `action`,
   `observation`, `state` equal `GET /schema`'s; `reset.properties.fixture` enum equals
   `["agency", "empty", "small_startup"]`; `state_format` enum contains `seahaven.state/1`;
   `$defs.SeahavenStartup.properties` has `user_id`; `additionalProperties` is `false` at top level
   and in startup; no `control_tools` property.
2. **Route not in OpenAPI**: `/seahaven/schemas` absent from `/openapi.json` paths.
3. **Served with `console=False`** too.
4. **Fixtures per request**: world copy (`copy.copy(world)`) with `fixtures_dir` a temp dir; first
   request shows none (`fixture` null-only); write a fixture (reuse the fixture-building helper
   the fixture tests use); second request lists it.
5. **No fixtures dir**: `fixture` is `{"anyOf": [{"type": "null"}], ...}`.
6. **Startup shapes** (`build_world` from conftest): no hooks → empty properties; a hook with a
   required keyword → `required` lists it inside startup; `Annotated[..., Field(description=)]` →
   description published; unannotated → no `type`; a `**kwargs` hook → startup
   `additionalProperties: true`.
7. **Composition**: a child world's hook keyword appears; same keyword same type in two hooks → one
   property, first default; different types → `WorldBug` from `app()` naming both hooks.
8. **Unschemable annotation** (e.g. a plain class) → `WorldBug` from `app()` naming hook and keyword.
9. **Drift**: `set(SeahavenResetRequest.model_fields) == set(inspect.signature(SeahavenEnv.reset)
   .parameters) - {"self", "control_tools", "unknown"}`; and for a world, startup property names
   == `world.composition().accepted_startup_kwargs`.
10. **Schema validates the real call**: `jsonschema.validate` a reset message built from the schema
    (fixture from the enum, `startup={"user_id": ...}`), then send it over `/ws` and get a live
    episode. This is the "real entry point" test AGENTS.md asks for.
11. **Upstream route missing**: monkeypatch the router to drop `/schema` before `_serve_schemas` →
    `RuntimeError`.

Console: covered by the e2e smoke test (§3.5) and `npm run check`. There is no unit-test runner in
`ui/`; do not add one.

## 6. Docs

- `serving_and_openenv.md`: under "The wire protocol", after "`GET /schema` publishes the three
  models", a short `### GET /seahaven/schemas adds the reset message` section: what it answers,
  that fixtures are read per request, that it is not in OpenAPI. One `json` example, trimmed.
  "The web console" section: one sentence that the New environment dialog builds its reset form from
  it.
- `docs/reference/api.md`: `World.state_formats`.
- Follow AGENTS.md docs style; wrap at 100.

## 7. Environment note for the coding agent

The session's default `uv` may resolve a 3.14 release candidate. Check `uv run python -V` prints a
final 3.14 before running checks (AGENTS.md "Environment").
