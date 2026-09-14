---
status: open
---

# Backlog: World Composition

Items found while building composition that are out of scope for the phase that found them. Each is
closed or dismissed through the standard phase flow.

## Open

- **`AliasPath` makes the tool list disagree with validation.**
  `Field(validation_alias=AliasPath("p", 0))` publishes the *parameter's own* name in the tool
  schema but validates only the nested shape, so the name the tool list advertises is one
  validation rejects — by reference, by name, and for an agent's own call alike. That is exactly
  what `tool.py`'s module docstring says cannot happen ("the tool list an agent reads and the
  validation a call passes cannot drift apart").

  Pre-existing, and not introduced by composition: `docs/authoring.md` documents only
  `Field(alias=...)`, and phase 3's `Tool.arguments` reads the published schema, so the typed call
  path is exactly as wrong as the by-name path and no more. Closing it means refusing `AliasPath`
  in `tool._field`, which is a change to the registration surface that no phase of this project
  names.

## Closed

_None yet._
