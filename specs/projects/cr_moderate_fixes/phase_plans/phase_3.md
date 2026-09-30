---
status: complete
---

# Phase 3: Middleware per contributing route (4.M1)

## Overview

An agent call to a contributed tool runs the middleware of the node's canonical route today. When
a node becomes reachable by a second route, a tool contributed through the first route silently
loses the middleware of the worlds on that route. This phase moves the chain from the node to the
contributed entry: each `Contributed` carries the chain of the route that contributed it, built
once per distinct route at the seal. Per
[components/phase_3_route_middleware.md](../components/phase_3_route_middleware.md). Behaviour is
unchanged when no node is shared, and handle calls still run only the owner's own chain.

## Steps

1. `src/seahaven/composition.py`, data model:
   - `Contributed` gains `chain: Handler`, with a docstring saying the chain belongs to the entry
     because one node reached by two routes contributes an entry through each.
   - `Node.agent_chain` is removed; `Node.internal_chain` is renamed `own_chain`, and the field
     comment says it serves a handle call and an agent call to one of the root's own tools.
   - `_Candidate.entry` becomes `name: str`, `tool: Tool`, `node: Node`,
     `hosts: tuple[NodeKey, ...]` (the node keys on the contributing route, from the node whose
     list this is down to the owner). The docstring describes `hosts`.
   - Module docstring: "the chains their calls descend" becomes "the chain each tool on the
     surface descends".
2. `_walk` drops `parents` and returns four values; `resolve` and `_nodes` follow.
3. `_route_layers(route: Sequence[NodeKey])` returns every middleware of every node on a route,
   outermost first; `_own_layers` is removed. `_nodes` builds
   `own_chain=build_route_chain(_route_layers((key,)), key)`.
4. `_contribute`: a node's own tool starts with `hosts=(key,)`. `_through(host, added, inner)`
   prepends `host`. `_fold` keys on `candidate.name`.
5. `resolve`: `tools = _entries(nodes[order[0]], surface.candidates)`, where `_entries` builds one
   chain per distinct `hosts` route, seeded with `{(root.key,): root.own_chain}`.
6. `src/seahaven/instances.py`: `_Target` gains `chain: Handler`; `_target` fills it from
   `entry.chain` on the by-name and by-function branches, and from `composition.root.own_chain`
   for a control tool. `_dispatch` runs `target.chain(ctx, call)`, with the comment reworded (the
   chain comes from this call's resolution, which reseals after a registration). Import `Handler`.
7. `src/seahaven/world.py`: `World.chain` returns `root.own_chain`; the docstring's "on the node
   that owns it" becomes "on its entry in the tool list". `src/seahaven/handles.py`:
   `owner.own_chain(...)`.
8. Docs: `src/seahaven/docs/composition.md` `## Middleware` first sentence replaced per the
   component doc §3; `src/seahaven/docs/reference/lints.md` SH207 **Why** gains "Each name also
   runs the middleware of its own route."

## Tests

`tests/test_composite_dispatch.py`, "the chain" section, with a helper
`shared_under_a_middle(tmp_path, trace)` (host adds traced `middle`, which adds traced `leaf`,
with `m_`; host then adds `leaf` with `l_`):

- `test_the_agent_chain_is_the_whole_contributing_route`: renamed from `..._canonical_route`,
  body unchanged.
- `test_sharing_a_node_keeps_the_middle_worlds_middleware_on_what_it_contributes`: company adds
  shop (`shop_`, recording middleware, adds payments); shop's middleware runs on
  `shop_payments_write` before and after `company.add_world(payments, tool_prefix="pay_")`, and
  never on `pay_payments_write`.
- `test_each_name_of_a_shared_tool_runs_its_own_routes_middleware`: `m_leaf_write` traces
  host/middle/leaf, `l_leaf_write` host/leaf; both reads see both rows.
- `test_both_sides_of_a_diamond_run_their_own_middle`: `l_` gives host/left/leaf, `r_` gives
  host/right/leaf.
- `test_a_route_the_lists_filter_out_does_not_choose_the_chain`: canonical edge with
  `tool_allow_list=[]`, middle's route contributes; `leaf_write` runs host/middle/leaf, by name
  and by function reference.
- `test_a_shared_leaf_three_deep_keeps_every_world_between`: `leaf_write` gives host/a/b/leaf,
  `x_leaf_write` gives host/leaf.
- `test_a_middle_worlds_error_handler_shapes_the_errors_it_contributes`: `m_refuse` raises
  middle's error, `l_refuse` raises `Boom`.
- `test_a_middleware_registered_later_reaches_a_route_through_a_shared_node`: on a live instance,
  a middleware registered on `middle` runs on `m_leaf_write`, not on `l_leaf_write`.
- `test_a_nested_call_into_a_shared_node_runs_only_the_owners_layer` (guard): a host tool calling
  `ctx.worlds.middle.worlds.leaf.call("leaf_write")` traces host/leaf.

`tests/test_composition.py`, "contribution" section (seal only):

- `test_each_entry_carries_the_chain_of_the_route_that_contributed_it`: `m_leaf_write` and
  `l_leaf_write` share `.node` and differ in `.chain`.
- `test_entries_of_one_route_share_one_chain`: `m_leaf_write.chain is m_leaf_read.chain`, and a
  root tool's `.chain is host.chain`.
