---
status: draft
---

# Implementation Plan: World Composition

Composition is built in the phases below, each one reviewable unit referencing `architecture.md`
(§ numbers) and `functional_spec.md` (FS §). Nothing here restates them.

The framework has no code yet; its own plan (`../seahaven_framework/implementation_plan.md`) is
thirteen phases. Every phase below names the framework phases it needs. See "Order" for where
composition slots into that sequence.

## Phases

- [ ] **Phase 1: the composition model and the seal.** `composition.py`: `AddedWorld`, `NodeKey`,
      `Node`, `Contributed`, `Composition` (§2); `World.add_world` with the five call-site checks
      (§3); resolution with scope propagation, BFS canonical paths, alias edges, key-wise bound
      startup and per-node-key contribution (§4.1 steps 1–5); the registration epoch and
      `World.composition()` (§4.2); the six seal checks and `attached_limit()` (§4.4); the flat
      tool surface, `Instance.tools()` and `World._tools_by_fn` (§5). No chains, no instances yet.
      The composite test world under `tests/worlds/` (§17) with `test_composition.py` and
      `test_add_world.py`. Needs framework phase 2.
- [ ] **Phase 2: contexts, handles, composite instances and dispatch.** `handles.py` (`Frame`,
      `Worlds`, `WorldHandle`); `Ctx` generic with `worlds` and `with_call(worlds=)` (§6.3);
      `Call.node`; `build_route_chain` and per-node chains on `Node` (§4.1 step 6, §7.6);
      `NodeRuntime`, N files named by path, N connections and sessions, `node_seed`, the pinned
      node set (§6.1–6.2); tree startup hooks with merged bound kwargs and N transactions (§6.4);
      `_held()` with the depth counter, the epoch and the `Frame`; the in-call thread-local; node
      dispatch, nested `handle.call`, `bulk` over N transactions (§7); the node path and `internal`
      marker in the log line. Blank composite instances only; fixtures are phase 4.
      `test_composite_instance.py`, `test_composite_dispatch.py`. Needs framework phase 3.
- [ ] **Phase 3: typed access.** `Tool[**P, R]`; the `call` overloads on `Instance` and
      `WorldHandle`; `by_fn` resolution at the root and within a handle's subtree, with the
      ambiguity error (§8.1–8.2); `Ctx[X]` accepted by the registration check and the `Worlds`
      typing base (§8.3); `invoke` returns the tool's original object after proving it serialises,
      and `control.dispatch` serialises its own (§8.4). The `ty` gate on `Concatenate` +
      `ParamSpec` overloads in CI (§16, last row). `test_typed_call.py`. Needs phase 2.
- [ ] **Phase 4: fixtures, inspection, changes, the report.** `NodeMeta`, `FixtureMeta` version 2,
      per-node freeze, verify and `check_composition` (§11); `open_inspection(attachments=)` with
      the attach-before-authorizer order and `Instance.inspect()` over every node (§9);
      `Change.world` and per-node rendering (§10); `Instance.composition()` (§12); control tools
      over the composition. `test_composite_fixtures.py`, `test_composite_inspection.py`,
      `test_composite_changes.py`. Needs phase 2 and framework phase 4.
- [ ] **Phase 5: OpenEnv and `seahaven check`.** `openenv/env.py` serialises the observation's
      result and `SeahavenState.composition` (§8.4, §12); `check` seals first and reports seal
      errors as findings (§4.3); SH206–SH209, SH406, SH502, SH503 and per-node SH401–SH405 (§14),
      `lint/world.py` new. Lint fixture pairs under `tests/worlds/`; the composite OpenEnv
      end-to-end test (§17). Needs phases 3–4 and framework phases 6–7.
- [ ] **Phase 6: docs.** The composition concept page and the FS §14 README section; the
      authoring-docs requirements of FS §13 (prefer an added world's tools over direct SQL;
      prefixes and lists match the client's real surface; never assume sole writership; return
      models, not dicts); the new lint codes on the codes page; `AGENTS.md` scaffold notes. Needs
      phase 5 and framework phase 12.

## Order

Composition is post-V1 (FS §0). Phases 1–5 slot in after framework phase 10 (ProjectTracker in
full), so the single-node path is proven by a real world before it becomes the degenerate case of
one code path (§1). Framework phase 11 (benchmark) then runs after composition phase 4, so it
measures the per-node floor the design assumes (§15); composition phase 6 merges into framework
phase 12. Phases 1 → 2 → 3 and 2 → 4 are strict; 3 and 4 can proceed in parallel; 5 waits for both.
