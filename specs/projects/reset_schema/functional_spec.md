---
status: draft
---

# Functional Spec: Reset Schema

## 1. Purpose

OpenEnv's `GET /schema` publishes the JSON schemas for `action`, `observation` and `state`, but
nothing publishes what `reset()` accepts. The web console therefore asks for reset arguments as a
raw JSON string, while it renders a real form for every tool.

This project:

1. Adds `GET /seahaven/schemas` to `seahaven serve`: everything `/schema` answers, plus a `reset`
   key holding the JSON schema of the reset message.
2. Makes the console render the New environment dialog's reset arguments as a form, with the same
   form code the tool panel uses.

It is built as if a proposal to OpenEnv were already accepted: `create_app` takes a `reset_cls`
next to `state_cls`, and `/schema` answers a `reset` key. Seahaven cannot change upstream, so it
serves the extended answer at its own namespaced path. If upstream accepts the proposal, the
server-side change becomes "pass `reset_cls` to `create_app`" and the console's change becomes
"read `reset` off `/schema`".

## 2. Out of Scope

- Changing what `reset()` accepts, how it validates, or what it answers. The reset wire behaviour is
  unchanged.
- Changing `GET /schema` itself. It answers exactly what OpenEnv answers today.
- Opening the upstream OpenEnv proposal. This project makes the code the proposal would describe.
- Per-fixture descriptions in the schema (fixture ids only; see §4.3).
- The OpenEnv `/web` interface and the MCP server.

## 3. `GET /seahaven/schemas`

### 3.1 Contract

- Method and path: `GET /seahaven/schemas`. No parameters, no body.
- Served by every app `seahaven.openenv.app(...)` builds, whatever `console=` says. `seahaven serve`
  therefore always serves it.
- Kept out of the OpenAPI schema (`include_in_schema=False`), for the same reason as `/console`:
  `openenv push` classifies an app by its path names.
- Response: `200`, `application/json`, an object with exactly four keys:

  | Key           | Value                                                            |
  |---------------|------------------------------------------------------------------|
  | `action`      | Identical to `/schema`'s `action`                                |
  | `observation` | Identical to `/schema`'s `observation`                           |
  | `state`       | Identical to `/schema`'s `state`                                 |
  | `reset`       | `reset_cls.model_json_schema()` — the reset message schema (§4)  |

- The three shared keys come from the same code path as `/schema`, not a copy, so the two endpoints
  cannot disagree. The response model is `SchemaResponse` extended with `reset`.
- The endpoint is read-only and needs no session. It never creates an instance.

### 3.2 Errors

- The schema is built when the app is built (§4.4). A world whose reset schema cannot be built makes
  `app(...)` (and so `seahaven serve`) fail at startup with a `WorldBug`; the endpoint never has to
  answer "cannot build".
- Reading the fixtures directory on a request (§4.3) can still fail (for example, a malformed
  sidecar added while the server runs). That answers `500` like any unhandled server error. The
  console treats it as "no reset schema" (§5.3).

## 4. The Reset Message Schema

### 4.1 The model

The schema is a pydantic model, `SeahavenResetRequest`, a subclass of OpenEnv's `ResetRequest`,
built once per world. `reset` in §3.1 is its `model_json_schema()`. It is the `reset_cls` that the
proposal would pass to `create_app`.

It has `additionalProperties: false`, because `SeahavenEnv.reset` refuses any key it does not name.

### 4.2 Fields

| Field          | Source       | Type in schema                                        | Default | Description |
|----------------|--------------|-------------------------------------------------------|---------|-------------|
| `seed`         | OpenEnv base | integer ≥ 0, or null                                  | null    | OpenEnv's own |
| `episode_id`   | OpenEnv base | string (max 255), or null                             | null    | OpenEnv's own |
| `fixture`      | Seahaven     | enum of the world's fixture ids, or null              | null    | Null makes a blank instance from the world's DDL |
| `now`          | Seahaven     | string (ISO-8601 instant), or null                    | null    | The blank instance's clock; refused together with a fixture |
| `state_format` | Seahaven     | enum of the root world's state formats (built-in and registered), or null | null | Null uses the world's pinned format |
| `startup`      | World        | object (§4.4), or null                                | null    | The world's own startup keywords |

- `control_tools` is not in the schema. `reset()` accepts that key and ignores it; the server's
  `include_control_tools` decides. Publishing it would advertise a control that does nothing.
- The `seed` and `episode_id` entries are inherited from `ResetRequest` unchanged.
- The constraint between `now` and `fixture` is stated in `now`'s description only. JSON Schema
  can express it, but the console's form cannot, and the server already refuses the combination
  with a clear message.
- Descriptions are written in the bundled-docs style (AGENTS.md "Docs style"): short and plain.

### 4.3 Fixtures

- `fixture` is published as a plain `enum` of fixture ids, sorted as `world.fixtures()` sorts them,
  wrapped so that null is also allowed (pydantic's nullable form).
- The list is read from the fixtures directory **on each request**, so a fixture written while the
  server runs appears without a restart. The rest of the schema is fixed when the app is built.
- A world with no fixtures directory, or an empty one, publishes `fixture` as null-only: the only
  value it accepts is null. It does not publish an empty `enum`, which is not valid JSON Schema.
- Fixture descriptions are not published. A plain `enum` cannot carry them, and `enum` is what
  pydantic emits for a `Literal`, which makes it the form most likely to be accepted upstream.

### 4.4 Startup

`startup` is an object whose properties are the startup keywords of every hook in the world's
composition tree. These are the same keywords the instance already accepts
(`composition.accepted_startup_kwargs`).

- Each property is built from the hook parameter, the same way a tool argument is built from a tool
  parameter:
  - The type annotation becomes the type. An unannotated parameter is "any" (no `type`).
  - The default becomes `default`. A keyword with no default is `required` inside `startup`.
  - `Annotated[T, Field(description=...)]` becomes the description.
- If any hook in the tree takes `**kwargs`, the instance accepts any startup keyword, so `startup`
  has `additionalProperties: true`. Otherwise it is `additionalProperties: false`.
- A world with no startup keywords publishes `startup` as an object with no properties (and
  `additionalProperties` as above). It is still nullable.
- If a startup keyword has no default, `startup` itself still stays optional at the top level.
  Omitting it is the same as `{}`, and the world's hook refuses the missing keyword when the
  instance is made. The form marks the keyword as required (§5.2).

**Errors, raised as `WorldBug` when the app is built:**

- Two hooks in the tree name the same keyword with different annotations (compared as annotation
  objects; different defaults alone are not a conflict — the first hook in tree order gives the
  default). The message names the keyword and the two hooks. This is one check, not a merge system.
- A hook parameter's annotation cannot be turned into a JSON schema. The message names the hook and
  the parameter, like the tool error does.

These are new errors: a world with such a hook that `seahaven serve` accepts today will fail at
startup after this change. `world.instance(...)` in process is not affected.

### 4.5 Drift protection

A test pins that the set of `SeahavenResetRequest` fields equals the set of named parameters of
`SeahavenEnv.reset`, minus `control_tools`. Adding a reset parameter without a schema field (or the
reverse) fails that test.

## 5. The Console

### 5.1 Fetching the schema

- The console fetches `GET /seahaven/schemas` first. If that fails for any reason (404, non-200,
  network error, a body that is not JSON, or no usable `reset` object), it fetches `GET /schema` as
  it does today.
- The result is used everywhere the console uses `/schema` today. The rest of the console behaves
  exactly as before; only the reset form reads `reset`.
- The New environment dialog fetches the schema for the typed URL, with the same debounce and
  stale-answer guard as its `/metadata` probe. The schema used when the environment is opened is the
  same kind of fetch.
- This is Seahaven-specific code in a console that is otherwise generic. A comment at the fetch
  says so, and says it goes away if OpenEnv accepts `reset` in `/schema`.
- The Vite dev proxy and the mock server gain `/seahaven/schemas`.

### 5.2 The reset form (a reset schema is available)

- The "Advanced: reset arguments" disclosure and its JSON textarea are replaced by one form
  rendering the reset schema, built with the same `fieldsOf` / `coerce` / `ArgsForm` code as the
  tool panel. No second form implementation.
- **One flat form.** The top-level fields (`seed`, `episode_id`, `fixture`, `now`, `state_format`)
  and the `startup` keywords appear in one list. A `startup` keyword is labelled so a reader can tell
  it is a world keyword (for example, prefixed or under a small "Startup" label within the same form).
  On submit, the startup values are put back under `startup` in the reset message. The `startup`
  object itself is not a field.
- Enum fields (`fixture`, `state_format`) render as selects. Leaving one empty sends nothing, so the
  server default applies (blank instance, pinned format).
- An empty optional field is omitted from the message, as in the tool form. If no startup keyword
  has a value, `startup` is omitted.
- If `startup` allows any keyword (`additionalProperties: true`), the named keywords are still
  fields. Keywords the schema does not name are sent with the raw JSON toggle (below); there is no
  separate control for them.
- A **raw JSON toggle**, as the tool form has, switches between the form and editing the whole reset
  message as JSON, with the same parse error display as today's textarea.
- Validation errors from `coerce` are shown per field, and the Open button is disabled while there
  are any, as the tool form does.
- **Replay / prefill:** when the dialog opens with `defaultArgs`, the form is filled from them
  (top-level keys into their fields, `startup` keys into the startup fields). If a default key has
  no field in the schema, the dialog opens in raw JSON mode with the defaults, so nothing is lost.

### 5.3 Fallback (no reset schema)

If neither fetch yields a `reset` schema (a generic OpenEnv environment, a cross-origin page, an
older Seahaven server, or an error), the dialog is exactly what it is today: the "Advanced: reset
arguments" disclosure with the JSON textarea and its explanatory text.

## 6. Documentation and Tests

- **Bundled docs:** `serving_and_openenv.md` gains a short section on `/seahaven/schemas`, and the
  reference list of served routes includes it. Docs examples follow the tested-examples rules.
- **Server tests:** the endpoint through a real app (`TestClient`) against the reference world:
  the three shared keys equal `/schema`'s; `reset` lists the fixtures, the state formats and
  projecttracker's `user_id` startup keyword; a fixture added to the directory appears on the next
  request; a world with no fixtures; a `**kwargs` hook; the two build-time `WorldBug`s; the drift
  test (§4.5).
- **Console:** unit coverage where the console has it for the fetch fallback and the nested
  `startup` round trip. The mock server serves `/seahaven/schemas`, and the e2e smoke test opens
  an environment through the generated form. The plain mock (`:8001`) keeps exercising the fallback.
