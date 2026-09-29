/**
 * The form generated from a JSON schema: the tool panel's arguments, a
 * non-tool action, and the New environment dialog's reset message.
 */

import { useEffect, useState } from "react"
import { initialValue, type Field } from "../lib/schema"
import { FormField, Input, Switch, Textarea, cx } from "./ui"

export function ArgsForm({
  fields,
  values,
  errors,
  onChange,
}: {
  fields: Field[]
  values: Record<string, string | boolean>
  errors: Record<string, string>
  onChange: (name: string, value: string | boolean) => void
}) {
  if (fields.length === 0) {
    return <p className="text-[13px] text-muted">This call takes no arguments.</p>
  }
  return (
    <div className="flex flex-col gap-4">
      {fields.map((field) => (
        <FormField
          key={field.name}
          label={field.label}
          description={field.description}
          required={field.required}
          typeHint={field.typeHint}
          error={errors[field.name]}
        >
          {(id) => {
            const value = values[field.name]
            if (field.kind === "boolean") {
              return (
                <div className="flex items-center gap-2">
                  <Switch
                    checked={value === true}
                    onChange={(next) => onChange(field.name, next)}
                    label={field.label}
                  />
                  <span className="text-[12.5px] text-muted">
                    {value === true ? "true" : "false"}
                  </span>
                </div>
              )
            }
            if (field.kind === "enum" && field.choices) {
              return (
                <div className="flex flex-wrap gap-1.5">
                  {field.choices.map((choice) => {
                    const text = typeof choice === "string" ? choice : JSON.stringify(choice)
                    const active = value === text
                    return (
                      <button
                        key={text}
                        type="button"
                        onClick={() => onChange(field.name, active ? "" : text)}
                        className={cx(
                          "rounded-md border px-2 py-1 font-mono text-[12.5px] transition-colors",
                          active
                            ? "border-accent bg-accent/10 text-accent"
                            : "border-border text-muted hover:border-faint hover:text-fg",
                        )}
                      >
                        {text}
                      </button>
                    )
                  })}
                </div>
              )
            }
            // An untyped reset field is most often one word, so it gets a one-line input.
            if ((field.kind === "json" && !field.untyped) || field.kind === "text") {
              return (
                <Textarea
                  id={id}
                  rows={field.kind === "json" ? 4 : 3}
                  spellCheck={false}
                  value={String(value ?? "")}
                  placeholder={field.placeholder}
                  onChange={(event) => onChange(field.name, event.target.value)}
                  className={field.kind === "json" ? "font-mono text-[12.5px]" : ""}
                />
              )
            }
            return (
              <Input
                id={id}
                type={field.kind === "number" || field.kind === "integer" ? "number" : "text"}
                value={String(value ?? "")}
                placeholder={field.placeholder}
                onChange={(event) => onChange(field.name, event.target.value)}
              />
            )
          }}
        </FormField>
      ))}
    </div>
  )
}

/**
 * The form's values, reset to the fields' initial values whenever `key` changes.
 *
 * `seed` is merged over the initial values at that moment, which is how a form
 * opens prefilled.
 */
export function useFormValues(fields: Field[], key: string, seed?: Record<string, string | boolean>) {
  const [values, setValues] = useState<Record<string, string | boolean>>({})
  useEffect(() => {
    const next: Record<string, string | boolean> = {}
    for (const field of fields) next[field.name] = initialValue(field)
    setValues({ ...next, ...seed })
    // `key` and not `fields`: a new tool starts a new form, and `fields` is a
    // fresh array on every render, which would reset the form as it is typed
    // into. There is no linter here to tell about that.
  }, [key])
  return [values, setValues] as const
}
