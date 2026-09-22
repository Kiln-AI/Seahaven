/**
 * JSON Schema to form fields.
 *
 * Every hint a schema carries is rendered: the description under the label, the
 * default in the placeholder, the required marker, the allowed values as a
 * select, the bounds on a number input. Environments publish this material and
 * the person calling a tool should never have to read the source to find it.
 */

import type { JsonSchema } from "./openenv"

export type FieldKind = "string" | "text" | "number" | "integer" | "boolean" | "enum" | "json"

export type Field = {
  name: string
  label: string
  kind: FieldKind
  description: string
  required: boolean
  default: unknown
  choices: unknown[] | null
  minimum?: number
  maximum?: number
  placeholder: string
  /** A shape the form cannot express exactly, so the value is edited as JSON. */
  typeHint: string
  /**
   * The schema names no type at all, and the caller asked for such a field to
   * take plain text (the reset form does; the tool form does not). It is a
   * one-line input, and text that is not JSON is sent as a string, so a plain
   * word does not need quotes.
   */
  untyped: boolean
}

function deref(schema: JsonSchema, root: JsonSchema): JsonSchema {
  if (!schema?.$ref) return schema
  const match = /^#\/\$defs\/(.+)$/.exec(schema.$ref)
  if (!match) return schema
  const target = root.$defs?.[match[1]]
  return target ? { ...target, ...withoutRef(schema) } : schema
}

function withoutRef(schema: JsonSchema): JsonSchema {
  const { $ref: _ignored, ...rest } = schema
  return rest
}

/**
 * `anyOf: [T, null]` is how pydantic spells an optional field. Unwrap it to T.
 *
 * The branch carries the type and nothing else: `title`, `default` and
 * `description` sit on the wrapper, so an argument written `limit: int | None =
 * 20` publishes its default one level above the `integer` it defaults to.
 * Keeping only the branch would drop every hint the field has.
 */
function unwrapNullable(schema: JsonSchema, root: JsonSchema): JsonSchema {
  const branches = schema.anyOf ?? schema.oneOf
  if (!Array.isArray(branches)) return schema
  const real = branches
    .map((branch) => deref(branch, root))
    .filter((branch) => branch.type !== "null")
  if (real.length !== 1) return schema
  const { anyOf: _anyOf, oneOf: _oneOf, ...wrapper } = schema
  return { ...real[0], ...wrapper }
}

function typeOf(schema: JsonSchema): string {
  const type = schema.type
  if (Array.isArray(type)) return type.find((entry) => entry !== "null") ?? "string"
  return type ?? (schema.enum || schema.const !== undefined ? "string" : "")
}

function kindOf(schema: JsonSchema): FieldKind {
  if (schema.enum || schema.const !== undefined) return "enum"
  switch (typeOf(schema)) {
    case "boolean":
      return "boolean"
    case "integer":
      return "integer"
    case "number":
      return "number"
    case "object":
    case "array":
      return "json"
    case "string": {
      const long = (schema.maxLength ?? 0) > 120
      const multiline = schema.format === "textarea" || /sql|body|content|code|text|prompt|notes|query/i.test(String(schema.title ?? ""))
      return long || multiline ? "text" : "string"
    }
    default:
      return "json"
  }
}

function humanize(name: string): string {
  return name
    .replace(/[_-]+/g, " ")
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .replace(/^./, (character) => character.toUpperCase())
}

function describeType(schema: JsonSchema): string {
  const type = typeOf(schema)
  if (type === "array") {
    const items = schema.items ? typeOf(schema.items) : ""
    return items ? `array of ${items}` : "array"
  }
  if (type === "object") {
    const extra = schema.additionalProperties
    if (extra && typeof extra === "object") return `object of ${typeOf(extra)}`
    return "object"
  }
  return type || "any"
}

function isUntyped(schema: JsonSchema): boolean {
  return (
    schema.type === undefined &&
    schema.enum === undefined &&
    schema.const === undefined &&
    schema.anyOf === undefined &&
    schema.oneOf === undefined &&
    schema.$ref === undefined &&
    schema.properties === undefined &&
    schema.items === undefined
  )
}

export function fieldsOf(
  schema: JsonSchema | undefined,
  skip: string[] = [],
  { untypedAsText = false }: { untypedAsText?: boolean } = {},
): Field[] {
  if (!schema?.properties) return []
  const required = new Set(schema.required ?? [])
  return Object.entries(schema.properties)
    .filter(([name]) => !skip.includes(name))
    .map(([name, raw]) => {
      const resolved = unwrapNullable(deref(raw, schema), schema)
      const choices = resolved.enum ?? (resolved.const !== undefined ? [resolved.const] : null)
      const hasDefault = resolved.default !== undefined
      return {
        name,
        label: resolved.title || humanize(name),
        kind: kindOf(resolved),
        description: resolved.description ?? "",
        required: required.has(name),
        default: resolved.default,
        choices,
        minimum: resolved.minimum,
        maximum: resolved.maximum,
        // A `null` default means "not given", and a placeholder reading "null" says otherwise.
        placeholder: hasDefault && resolved.default !== null ? `${format(resolved.default)}` : "",
        typeHint: describeType(resolved),
        untyped: untypedAsText && isUntyped(resolved),
      }
    })
}

function format(value: unknown): string {
  if (typeof value === "string") return value
  return JSON.stringify(value)
}

/** The value a field starts at: its default, or empty. */
export function initialValue(field: Field): string | boolean {
  return toFormValue(field, field.default)
}

/** A payload value as the form holds it, so that `coerce` gives the value back. */
export function toFormValue(field: Field, value: unknown): string | boolean {
  if (field.kind === "boolean") return value === true
  if (value === undefined || value === null) return ""
  if (field.untyped && typeof value === "string" && !parsesAsJson(value)) return value
  if (field.kind === "json") return field.untyped ? JSON.stringify(value) : JSON.stringify(value, null, 2)
  return format(value)
}

function parsesAsJson(text: string): boolean {
  try {
    JSON.parse(text)
    return true
  } catch {
    return false
  }
}

export type Coerced = {
  values: Record<string, unknown>
  errors: Record<string, string>
}

/**
 * Form values to a call payload.
 *
 * An untouched optional field is left out rather than sent as an empty string,
 * so the environment applies its own default. A required field that is empty is
 * an error here rather than a validation failure three frames later.
 */
export function coerce(fields: Field[], raw: Record<string, string | boolean>): Coerced {
  const values: Record<string, unknown> = {}
  const errors: Record<string, string> = {}

  for (const field of fields) {
    const value = raw[field.name]

    if (field.kind === "boolean") {
      if (value === true || field.required || field.default !== undefined) {
        values[field.name] = value === true
      }
      continue
    }

    const text = typeof value === "string" ? value.trim() : ""
    if (!text) {
      if (field.required) errors[field.name] = "required"
      continue
    }

    switch (field.kind) {
      case "integer":
      case "number": {
        const parsed = Number(text)
        if (!Number.isFinite(parsed)) {
          errors[field.name] = "not a number"
        } else if (field.kind === "integer" && !Number.isInteger(parsed)) {
          errors[field.name] = "must be a whole number"
        } else if (field.minimum !== undefined && parsed < field.minimum) {
          errors[field.name] = `must be at least ${field.minimum}`
        } else if (field.maximum !== undefined && parsed > field.maximum) {
          errors[field.name] = `must be at most ${field.maximum}`
        } else {
          values[field.name] = parsed
        }
        break
      }
      case "json": {
        try {
          values[field.name] = JSON.parse(text)
        } catch (error) {
          if (field.untyped) values[field.name] = text
          else errors[field.name] = `not valid JSON: ${(error as Error).message}`
        }
        break
      }
      case "enum": {
        const match = field.choices?.find((choice) => format(choice) === text)
        values[field.name] = match !== undefined ? match : text
        break
      }
      default:
        values[field.name] = text
    }
  }

  return { values, errors }
}

/** Parse the Advanced JSON box: an object, or a message saying why it is not one. */
export function parseArgsObject(text: string): { value: Record<string, unknown>; error: string | null } {
  const trimmed = text.trim()
  if (!trimmed) return { value: {}, error: null }
  try {
    const parsed = JSON.parse(trimmed)
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
      return { value: {}, error: "must be a JSON object, like {\"seed\": 7}" }
    }
    return { value: parsed as Record<string, unknown>, error: null }
  } catch (error) {
    return { value: {}, error: (error as Error).message }
  }
}

// --- the reset message -----------------------------------------------------

/** The name prefix that marks a startup keyword in the flat reset form. */
export const STARTUP_PREFIX = "startup."

/**
 * The reset schema as one flat field list, in the schema's property order, with
 * `startup` expanded in place into the world's startup keywords, named
 * `startup.<keyword>`.
 *
 * `startupOpen` is true when the world accepts startup keywords the schema does
 * not name; those can only be sent as raw JSON.
 */
export function resetFieldsOf(reset: JsonSchema): { fields: Field[]; startupOpen: boolean } {
  // A property that only accepts null offers nothing to fill in: Seahaven
  // publishes `fixture` that way for a world with no fixtures.
  const nullOnly = Object.entries(reset.properties ?? {})
    .filter(([, property]) => isNullOnly(property))
    .map(([name]) => name)
  const top = fieldsOf(reset, nullOnly, { untypedAsText: true })
  const raw = reset.properties?.startup
  if (!raw) return { fields: top, startupOpen: false }
  const startup = unwrapNullable(deref(raw, reset), reset)
  const inner = fieldsOf({ ...startup, $defs: reset.$defs }, [], { untypedAsText: true }).map((field) => ({
    ...field,
    name: STARTUP_PREFIX + field.name,
    label: `startup · ${field.label}`,
  }))
  const fields = top.flatMap((field) => (field.name === "startup" ? inner : [field]))
  return { fields, startupOpen: startup.additionalProperties === true }
}

function isNullOnly(schema: JsonSchema): boolean {
  if (schema.type === "null") return true
  const branches = schema.anyOf ?? schema.oneOf
  return Array.isArray(branches) && branches.length > 0 && branches.every((branch) => branch.type === "null")
}

/** Coerced flat values back to a reset message, with the startup keywords nested under `startup`. */
export function nestResetArgs(values: Record<string, unknown>): Record<string, unknown> {
  const message: Record<string, unknown> = {}
  const startup: Record<string, unknown> = {}
  for (const [name, value] of Object.entries(values)) {
    if (name.startsWith(STARTUP_PREFIX)) startup[name.slice(STARTUP_PREFIX.length)] = value
    else message[name] = value
  }
  if (Object.keys(startup).length > 0) message.startup = startup
  return message
}

/**
 * A reset message as flat form values, or `null` when a key has no field, so
 * the caller can edit it as raw JSON rather than drop the key.
 */
export function flattenResetArgs(
  args: Record<string, unknown>,
  fields: Field[],
): Record<string, string | boolean> | null {
  const byName = new Map(fields.map((field) => [field.name, field]))
  const flat: Record<string, string | boolean> = {}
  const put = (name: string, value: unknown) => {
    const field = byName.get(name)
    if (!field) return false
    flat[name] = toFormValue(field, value)
    return true
  }
  for (const [name, value] of Object.entries(args)) {
    if (name !== "startup") {
      if (!put(name, value)) return null
      continue
    }
    if (value === null || value === undefined) continue
    if (typeof value !== "object" || Array.isArray(value)) return null
    for (const [keyword, inner] of Object.entries(value as Record<string, unknown>)) {
      if (!put(STARTUP_PREFIX + keyword, inner)) return null
    }
  }
  return flat
}
