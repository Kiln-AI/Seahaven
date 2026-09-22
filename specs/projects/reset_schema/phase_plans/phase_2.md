---
status: complete
---

# Phase 2: Console — the reset form

## Overview

The New environment dialog renders the reset message as a form built from the `reset` schema that
`GET /seahaven/schemas` publishes (phase 1), with the same `fieldsOf` / `coerce` / `ArgsForm` code
as the tool panel. Any environment that publishes no reset schema keeps today's JSON box. Dev
tooling (Vite proxy, mock, e2e), `ui/README.md` and the vendored build follow.

## Steps

1. `ui/src/lib/openenv.ts`: `EnvSchema` gains `reset?: JsonSchema`. `fetchSchema(root)` tries
   `seahaven/schemas` first and returns it when `reset` is an object with object `properties`;
   otherwise `fetchJson(root, "schema")`. Comment: Seahaven-specific, goes away if OpenEnv accepts
   `reset` in `/schema`.
2. `ui/src/components/ArgsForm.tsx` (new): move `ArgsForm` and `useFormValues` from `panels.tsx`
   unchanged and export them; `useFormValues(fields, key, seed?)` merges `seed` over the initial
   values when `key` changes. `panels.tsx` imports them.
3. `ui/src/lib/schema.ts`:
   - `Field.untyped: boolean` — the property has no type at all (phase 1 publishes a keyword only
     bound hooks name as `{"title": ...}`). It still renders as the JSON box, but `coerce` sends
     text that is not JSON as a string, so a plain word needs no quotes. `toFormValue` shows a
     string that would not re-parse as JSON unquoted, so the round trip is exact.
   - `toFormValue(field, value)` extracted from `initialValue` (which calls it with the default).
   - `STARTUP_PREFIX`, `resetFieldsOf(reset) -> { fields, startupOpen }`,
     `nestResetArgs(values)`, `flattenResetArgs(args, fields) -> values | null`, per architecture
     §3.3. Startup fields are named `startup.<kw>` and labelled `startup · <label>`.
4. `ui/src/components/NewEnvDialog.tsx`:
   - `resetSchema` state, fetched with `fetchSchema` in the debounced probe (`Promise.all` with
     `fetchMetadata`, same token guard); `null` while looking and on failure.
   - No schema: today's disclosure and textarea, unchanged.
   - Schema: a "Reset arguments" heading with the Raw JSON switch, then `ArgsForm` or the raw
     textarea (sharing `argsText` with the fallback box, same parse error). Prefill from
     `defaultArgs` via `flattenResetArgs`; a key with no field opens raw. Switching to raw
     serializes `nestResetArgs(coerce(...).values)`; switching back fills the form from the raw
     text when it parses and every key has a field. `startupOpen` adds one muted hint line.
   - Submit: raw → parsed text; form → `coerce`, errors shown per field and stop, else
     `nestResetArgs(values)`. Open is disabled while raw text fails to parse.
5. `ui/vite.config.ts`: proxy `/seahaven`.
6. `ui/mock/server.mjs`: `GET /seahaven/schemas` in default mode only (`--plain` answers 404):
   `SCHEMA` plus a reset schema shaped like the real one — fixture enum from `FIXTURES`,
   `state_format` enum, `startup` `$ref` to `SeahavenStartup` with `user_id` (string or null) and
   an untyped `team` keyword.
7. `ui/e2e/smoke.mjs`: on `:8000`, fill the generated form (fixture chip, seed,
   `startup · User Id`, untyped `startup · Team`), check the raw toggle shows the nested message,
   open, and assert the `reset` frame the browser sent (Playwright `framesent`) is
   `{fixture, seed, startup: {user_id, team}}`. On the plain page, assert the
   "Advanced: reset arguments" fallback is present. Screenshots of both.
8. `ui/README.md`: fetch paragraph, dev-proxy line, the "Known limits" reset entry.
9. Rebuild and copy `dist/index.html` to `src/seahaven/openenv/console/index.html`.

## Tests

- e2e `reset form`: the generated form renders for the Seahaven-shaped mock, with the fixture as
  chips and the startup keyword labelled as one.
- e2e `reset frame`: the frame on the socket nests startup keywords under `startup` and omits
  untouched fields; the untyped keyword arrives as a plain string.
- e2e `raw toggle`: raw mode shows the nested message.
- e2e `fallback`: the plain mock (no `/seahaven/schemas`) shows the Advanced JSON box.
- `npm run build` (tsc) and the vendored-build `cmp`.
