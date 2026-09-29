---
status: complete
---

# Implementation Plan: CR Moderate Fixes

One commit per phase. Details are in [architecture.md](architecture.md) and the component doc each
phase names.

## Phases

- [x] **Phase 1: Low-risk code and test batch.** 11.M1 (no fsync on instance creation), 7.M2
  (`serve` binds `127.0.0.1`), 5.M3 (`step(ListToolsAction())`), 6.M1 (import errors name their
  file and line), 4.M2 (hosts-first startup-hook order), 10.M1, 10.M3 and 10.M4 (tests, including
  the link check over every docs page). See
  [components/phase_1_batch.md](components/phase_1_batch.md).
- [x] **Phase 2: The `bulk()` deadlock (2.M1).** A per-thread re-entrant gate; `bulk()` marks the
  thread and takes no slot. Its own commit. See
  [components/phase_2_bulk_gate.md](components/phase_2_bulk_gate.md).
- [x] **Phase 3: Middleware per contributing route (4.M1).** A chain per contributed entry, built
  from the route that contributed it. Its own commit. See
  [components/phase_3_route_middleware.md](components/phase_3_route_middleware.md).
- [ ] **Phase 4: Docs.** 9.M1 (SH103's rationale), 9.M7, 9.M8, 8.M4 (projecttracker audit-trail
  claims), 2.M2/4.M3 (composite `bulk()` is not all-or-nothing when a commit fails), and 9.M6 (the
  history and spec-citation sweep). See [components/phase_4_docs.md](components/phase_4_docs.md).
