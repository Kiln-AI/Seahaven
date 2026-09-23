/**
 * The New environment flow.
 *
 * `reset` lives here and nowhere else. It is not an action you take against a
 * running environment, it is the thing that creates one: an environment that
 * has been reset twice is two different runs sharing one row in the list, which
 * is exactly the confusion this dialog exists to remove.
 *
 * The reset form is Seahaven-specific: it is built from the `reset` schema that
 * `GET /seahaven/schemas` publishes, and any environment that publishes none
 * gets the JSON box instead.
 */

import { useEffect, useMemo, useRef, useState } from "react"
import { fetchMetadata, fetchSchema, normalizeRoot, type EnvMetadata, type JsonSchema } from "../lib/openenv"
import {
  coerce,
  flattenResetArgs,
  initialValue,
  nestResetArgs,
  parseArgsObject,
  resetFieldsOf,
} from "../lib/schema"
import { ArgsForm, useFormValues } from "./ArgsForm"
import { Badge, Button, Disclosure, FormField, Input, Modal, Switch, Textarea } from "./ui"

/** How long the dialog waits for a first reset schema before it shows the JSON box. */
const SCHEMA_PATIENCE_MS = 2000

export type NewEnvRequest = {
  root: string
  label: string
  resetArgs: Record<string, unknown>
}

export function NewEnvDialog({
  defaultRoot,
  defaultArgs,
  busy,
  error,
  onCancel,
  onCreate,
}: {
  defaultRoot: string
  defaultArgs?: Record<string, unknown>
  busy: boolean
  error: string | null
  onCancel: () => void
  onCreate: (request: NewEnvRequest) => void
}) {
  const [root, setRoot] = useState(defaultRoot)
  const [label, setLabel] = useState("")
  const [argsText, setArgsText] = useState(
    defaultArgs && Object.keys(defaultArgs).length > 0 ? JSON.stringify(defaultArgs, null, 2) : "",
  )
  const [probe, setProbe] = useState<{ state: "idle" | "looking" | "found" | "quiet"; meta: EnvMetadata | null }>({
    state: "idle",
    meta: null,
  })
  const probeToken = useRef(0)
  // `undefined` until the first answer, or until `SCHEMA_PATIENCE_MS` passes
  // without one: neither the form nor the JSON box is shown yet, so one does not
  // flash in and swap for the other.
  const [resetSchema, setResetSchema] = useState<JsonSchema | null | undefined>(undefined)
  const shownSchema = useRef<string | undefined>(undefined)
  const [raw, setRaw] = useState(false)
  const [rawNote, setRawNote] = useState<string | null>(null)
  const [formErrors, setFormErrors] = useState<Record<string, string>>({})

  const { fields, startupOpen } = useMemo(
    () => (resetSchema ? resetFieldsOf(resetSchema) : { fields: [], startupOpen: false }),
    [resetSchema],
  )
  // The form restarts when the target world changes, not on every render: the
  // field names and their choices are what tell two worlds' forms apart.
  const formKey = fields.map((field) => `${field.name}=${(field.choices ?? []).join(",")}`).join("|")
  // What the form opens with: the defaults, or what was typed as JSON before the schema arrived.
  const [seedArgs, setSeedArgs] = useState(defaultArgs)
  const seed = seedArgs ? (flattenResetArgs(seedArgs, fields) ?? undefined) : undefined
  const [values, setValues] = useFormValues(fields, formKey, seed)

  const parsed = parseArgsObject(argsText)
  const useForm = Boolean(resetSchema) && !raw
  const target = normalizeRoot(root)
  // The JSON text is what the user is editing when the JSON box or raw mode is
  // shown; in the form it is stale. Read from the effect, which outlives renders.
  const typedText = useRef<string | null>(null)
  typedText.current = useForm ? null : argsText

  const showSchema = (reset: JsonSchema | null) => {
    const text = JSON.stringify(reset)
    if (text === shownSchema.current) return
    shownSchema.current = text
    setResetSchema(reset)
    setFormErrors({})
    setRawNote(null)
    if (reset === null) return
    const typed = typedText.current !== null ? parseArgsObject(typedText.current) : null
    if (typed?.error) {
      // Text that does not parse cannot fill a form, so it stays as it is, in raw mode.
      setRaw(true)
      return
    }
    const source = typed ? typed.value : defaultArgs
    setSeedArgs(source)
    // A key the form has no field for opens as raw JSON, so it is not lost.
    const unfit = source !== undefined && flattenResetArgs(source, resetFieldsOf(reset).fields) === null
    if (unfit && !typed) setArgsText(JSON.stringify(source, null, 2))
    setRaw(unfit)
  }

  // Name the environment before connecting, when the browser is allowed to read
  // `/metadata`. A quiet answer is not an error: it usually means this page is
  // on a different origin than the environment. The reset schema is read beside
  // it, on its own; the form stays as it is until a different schema arrives.
  useEffect(() => {
    const token = ++probeToken.current
    const current = () => probeToken.current === token
    setProbe({ state: "looking", meta: null })
    const timer = window.setTimeout(() => {
      void fetchMetadata(target).then((meta) => {
        if (current()) setProbe({ state: meta ? "found" : "quiet", meta })
      })
      void fetchSchema(target).then((schema) => {
        if (current()) showSchema(schema?.reset ?? null)
      })
    }, 350)
    // A server that never answers would otherwise leave no reset arguments at
    // all: show the JSON box, and let a late schema replace it with the form.
    const patience = window.setTimeout(() => {
      if (current() && shownSchema.current === undefined) showSchema(null)
    }, SCHEMA_PATIENCE_MS)
    return () => {
      window.clearTimeout(timer)
      window.clearTimeout(patience)
    }
  }, [target])

  const submit = () => {
    if (!useForm) {
      if (parsed.error) return
      onCreate({ root: target, label: label.trim(), resetArgs: parsed.value })
      return
    }
    const { values: args, errors } = coerce(fields, values)
    setFormErrors(errors)
    if (Object.keys(errors).length > 0) return
    onCreate({ root: target, label: label.trim(), resetArgs: nestResetArgs(args) })
  }

  const switchRaw = (next: boolean) => {
    if (next) {
      setArgsText(JSON.stringify(nestResetArgs(coerce(fields, values).values), null, 2))
      setRaw(true)
      return
    }
    // Back to the form only with everything that was typed: a key the form has
    // no field for would be dropped, so the dialog stays in raw JSON instead.
    const flat = parsed.error ? null : flattenResetArgs(parsed.value, fields)
    if (!flat) {
      setRawNote(
        parsed.error
          ? "Fix the JSON to go back to the form."
          : "The form has no field for some of these keys, so they stay as raw JSON.",
      )
      return
    }
    setValues(Object.fromEntries(fields.map((field) => [field.name, flat[field.name] ?? initialValue(field)])))
    setRawNote(null)
    setFormErrors({})
    setRaw(false)
  }

  const rawBox = (
    <>
      <Textarea
        rows={5}
        spellCheck={false}
        value={argsText}
        placeholder={'{\n  "fixture": "small_startup",\n  "seed": 7\n}'}
        onChange={(event) => {
          setArgsText(event.target.value)
          setRawNote(null)
        }}
        className="font-mono text-[12.5px]"
      />
      {parsed.error ? <p className="text-[12.5px] font-medium text-danger">{parsed.error}</p> : null}
      {rawNote ? <p className="text-[12.5px] text-muted">{rawNote}</p> : null}
    </>
  )

  return (
    <Modal
      title="New environment"
      subtitle="Opens a socket, then resets it. One environment is one session holding one private instance."
      onClose={onCancel}
      footer={
        <>
          <Button variant="primary" onClick={submit} disabled={busy || (!useForm && Boolean(parsed.error))}>
            {busy ? "Opening…" : "Open environment"}
          </Button>
          <Button variant="ghost" onClick={onCancel} disabled={busy}>
            Cancel
          </Button>
          {error ? <span className="ml-auto text-[12.5px] text-danger">{error}</span> : null}
        </>
      }
    >
      <div className="flex flex-col gap-4">
        <FormField
          label="Environment URL"
          description="The root the server is on. The socket is this plus /ws."
          required
        >
          {(id) => (
            <Input
              id={id}
              value={root}
              spellCheck={false}
              placeholder="http://127.0.0.1:8000"
              onChange={(event) => setRoot(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") submit()
              }}
            />
          )}
        </FormField>

        <div className="flex min-h-[20px] items-center gap-2 text-[12.5px]">
          {probe.state === "looking" ? <span className="text-faint">Looking…</span> : null}
          {probe.state === "found" && probe.meta ? (
            <>
              <Badge tone="ok">{probe.meta.name ?? "environment"}</Badge>
              <span className="truncate text-muted">{probe.meta.description}</span>
            </>
          ) : null}
          {probe.state === "quiet" ? (
            <span className="text-muted">
              No metadata from this origin. That is normal cross-origin, and the socket still works.
            </span>
          ) : null}
        </div>

        <FormField label="Label" description="What to call this run in the list. Optional.">
          {(id) => (
            <Input
              id={id}
              value={label}
              placeholder={probe.meta?.name ? `${probe.meta.name} run` : "run 1"}
              onChange={(event) => setLabel(event.target.value)}
            />
          )}
        </FormField>

        {resetSchema === undefined ? null : resetSchema ? (
          <div className="flex flex-col gap-3">
            <div className="flex items-center gap-2">
              <h3 className="text-[13px] font-semibold text-fg">Reset arguments</h3>
              <span className="text-[12px] text-faint">passed to the environment's reset()</span>
              <div className="ml-auto flex items-center gap-2">
                <span className="text-[12px] text-muted">Raw JSON</span>
                <Switch checked={raw} onChange={switchRaw} label="Edit reset arguments as raw JSON" />
              </div>
            </div>
            {raw ? (
              <div className="flex flex-col gap-2">{rawBox}</div>
            ) : (
              <ArgsForm
                fields={fields}
                values={values}
                errors={formErrors}
                onChange={(name, value) => setValues((current) => ({ ...current, [name]: value }))}
              />
            )}
            {startupOpen && !raw ? (
              <p className="text-[12.5px] text-muted">
                This world also accepts startup keywords it does not list. Add them with Raw JSON.
              </p>
            ) : null}
          </div>
        ) : (
          <Disclosure
            title="Advanced: reset arguments"
            hint="passed to the environment's reset()"
            defaultOpen={Boolean(defaultArgs && Object.keys(defaultArgs).length > 0)}
          >
            <div className="flex flex-col gap-2">
              <p className="text-[12.5px] leading-snug text-muted">
                OpenEnv publishes no schema for these, so this is the one box the UI cannot generate a
                form for. Whatever the environment's `reset()` accepts goes here, as a JSON object:
                commonly <code className="font-mono text-fg">seed</code> and{" "}
                <code className="font-mono text-fg">episode_id</code>, plus whatever that environment
                adds. A key the environment does not accept is the server's to judge: some servers
                drop it silently, and others answer with an error that names it.
              </p>
              {rawBox}
            </div>
          </Disclosure>
        )}
      </div>
    </Modal>
  )
}
