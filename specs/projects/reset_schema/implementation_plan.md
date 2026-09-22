---
status: draft
---

# Implementation Plan: Reset Schema

## Phases

- [ ] Phase 1: Server — `World.state_formats`, `SeahavenResetRequest` / `for_world` / startup model,
  `SeahavenSchemaResponse`, `GET /seahaven/schemas` in `app()`, tests (architecture §5), and the
  bundled docs (§6)
- [ ] Phase 2: Console — shared `ArgsForm.tsx`, `fetchSchema` fallback, reset field helpers, the
  New environment dialog form, Vite proxy, mock server, e2e smoke test, `ui/README.md`, and the
  rebuilt vendored `console/index.html`
