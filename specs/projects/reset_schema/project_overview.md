---
status: draft
---

# Reset Schema

Add a new API endpoint to the `seahaven serve` FastAPI server: `/seahaven/_reset_schema` (namespaced to avoid OpenEnv collisions).

The model is like the `/schema` endpoint, in that it returns the expected schema, but for the `reset()` call (the `/schema` endpoint covers state and others). The schema should include:

- The OpenEnv base class fields (episode ID, etc.)
- Seahaven base additions (fixture, etc.)
- The schema for the startup dict from the implementing world

The goal is for our reset UI in `/ui` to no longer take a raw JSON string, but to render a nice form like it does for tools.

## Other Notes

- Fixtures should be an enum of the actual fixture names (or a string with limited values, whichever is more common) — a set the UI can render as choices.
- The UI should render the schema nicely, like it does for tool endpoints. Same UI toolkit/approach — not a whole new code path.
- The API should be modeled like `/schema`. The intent is to propose adding it to the OpenEnv API with its maintainers, so that if accepted, the UI change is small.
