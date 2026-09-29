# Component: Phase 3, middleware per contributing route (4.M1)

Prototyped on a scratch copy of the tree at `38c6f3c` (4 files, +63/-58 in `src/`); line numbers
are at that commit. Full framework suite, projecttracker and xmlrpc suites, ruff and ty (serve side)
are green on the prototype. Ten scratch tests: seven fail
on today's code and pass on the prototype. The other three guard behaviour that must not change,
and pass on both.

## 1. Today

- `_walk` records `parents[child]` (the first edge that reaches it). `_nodes` builds, per node,
  `agent_chain = build_route_chain(_route_layers(key, parents), key)` (the canonical route) and
  `internal_chain = build_route_chain(_own_layers(key), key)`.
- `_contribute` folds `_Candidate(entry=Contributed(name, tool, node), route, prefixes, shadowed)`
  bottom-up. `_Candidate.route` is the `/`-joined edge *names* (diagnostics only). It holds no node
  keys, so the chain cannot be read from it.
- Consumers of the chains (all of them):
  - `instances.Instance._dispatch` (agent calls, by name and by function) runs
    `target.node.agent_chain`.
  - `world.World.chain` runs `root.agent_chain`. It is used by `tests/test_world.py` 390/734/743.
  - `handles.WorldHandle.call` (host code, `ctx.worlds.x.call`) runs `owner.internal_chain`.
  - Control tools go to `control.dispatch` and never touch a chain. Startup hooks run no
    middleware. openenv, mcp and lint read `composition.tools` for names, tools and nodes only.
    No chain is read there.

## 2. Change

### Data model (`src/seahaven/composition.py`)

- `Contributed` gains `chain: Handler`. It is what an agent's call to *this entry* descends: every
  middleware of every node on the route that contributed it, outermost first, then `invoke` on
  `node`. The docstring says the chain belongs to the entry because one node reached by two routes
  contributes an entry through each.
- `Node.agent_chain` is **removed**. Keeping a canonical-route chain invites the same bug back.
  `Node.internal_chain` is **renamed `own_chain`** (decided): this node's middlewares, then `invoke` on this
  node. It serves two callers: a handle call, and an agent call to one of the root's own tools
  (for the root, the canonical route is the root alone)." It stops `World.chain` reading
  `root.internal_chain`. `Node` is not exported from `seahaven`.
- `_Candidate.entry` is replaced by `name: str, tool: Tool, node: Node, hosts: tuple[NodeKey, ...]`.
  `hosts` holds the node keys on the contributing route, from the node whose list this is down to
  the owner (`hosts[-1] == node.key`). A `Contributed` is built only at the end, because it now
  needs a chain, and building a chain for every candidate at every level would be wasted work.
- `_walk` drops `parents` (it now has no reader). It returns four values instead of five.

### Where it is computed (the seal)

- `_contribute`: a node's own tool starts with `hosts=(key,)`. `_through(host, added, inner)` takes
  the host key from `contribution(key)` and sets `hosts=(host, *inner.hosts)`. `_fold` keys on
  `candidate.name`. The standing candidate keeps its own `hosts`, and a repeat that survives to the
  root is a collision anyway (`_raise_clashes`), so every served entry has exactly one route.
- `resolve`: `tools = _entries(root_node, surface.candidates)` in place of
  `{name: c.entry ...}`. Chains are built only for entries that reach the root, one per distinct
  route:

```py
def _route_layers(route: Sequence[NodeKey]) -> list[tuple[NodeKey, Middleware]]:
    return [(key, middleware) for key in route for middleware in key[0].middlewares]


def _entries(root: Node, candidates: Mapping[str, _Candidate]) -> dict[str, Contributed]:
    chains: dict[tuple[NodeKey, ...], Handler] = {(root.key,): root.own_chain}
    tools: dict[str, Contributed] = {}
    for name, candidate in candidates.items():
        route = candidate.hosts
        if route not in chains:
            chains[route] = build_route_chain(_route_layers(route), route[-1])
        tools[name] = Contributed(name, candidate.tool, candidate.node, chains[route])
    return tools
```

- `_nodes`: `own_chain=build_route_chain(_route_layers((key,)), key)`. `_own_layers` goes away.
- The cache is keyed by node keys, not edge names. Two edges from one host to one node
  (`name="a"` and `name="b"`, same store) are the same nodes, so they give the same chain. That is
  correct: middleware belongs to worlds, not to edge names. Seeding `(root.key,)` makes every root
  tool's `chain is world.chain`, so a leaf world still has exactly one chain object.
- Cost: `hosts` prepends O(depth) per `_through`. The seal stays O(nodes x children x tools x
  depth), with depth at most the attach bound. The diamond tests take 0.02s both before and after.

### Dispatch (`src/seahaven/instances.py`)

- `_Target` gains `chain: Handler`. `_target` fills it from `entry.chain` on both the by-name and
  the by-function branch. `entry_for` returns the same `Contributed`, so a typed call runs the
  same chain as the name. The control-tool branch passes `composition.root.own_chain` (unused,
  because `control.dispatch` returns first).
- `_dispatch` runs `target.chain(ctx, call)`. Reword the comment: the chain comes from this
  call's resolution, which reseals after a registration, so a middleware registered after the
  instance was made still applies.
- Add `Handler` to the `seahaven.call` import.
- `world.py`: `World.chain` returns `self.composition().root.own_chain`. The docstring is still
  true. `handles.py`: `owner.own_chain(...)`.

### Host code through handles: unchanged, deliberately

`ctx.worlds.x.call` runs the owning node's own chain today and after the change. A handle names a
store, not a route, and the host's chain (and the chain of every world above it on *its* route) is
already wrapped around the host tool making the call. So a nested call runs exactly the layers of
the route by which the agent reached the host, plus the owner's own layers. Using the handle's
route would run the host's layers twice.

### Behaviour is identical when no node is shared

For an entry, the contributing route is a path of edges from the root to the owner, and the
canonical route is the `parents` path. If every node on the contributing route (the root
excepted) is reached by exactly one edge, then that node's only incoming edge is its `parents`
edge. So the two paths are equal, `_route_layers` returns the same list and `build_route_chain`
builds the same chain. The root's own tools have route `(root,)`, which is the old
`root.agent_chain`. Scoped second accounts are separate nodes, so the graph is still a tree.
Behaviour changes **only** for an entry whose contributing route crosses an alias edge. Among
the committed worlds, only emporium's `shop/payments` crosses one, and it contributes nothing
(`tool_allow_list=[]`). No world there has middleware, and the suites pass unchanged.

### Cases

- **Two names, two routes** (`m_leaf_write` through `middle`, `l_leaf_write` direct, one store):
  host+middle+leaf versus host+leaf. `call.node` is the canonical path (`leaf`) for both, and
  `ctx` for each layer is its own node's.
- **Diamond** (host→left→leaf and host→right→leaf, prefixes `l_`/`r_`): each side runs its own
  middle. Today `r_` runs `left`'s.
- **Allow/block lists**: a route filtered out never reaches the root, so it cannot choose the
  chain. With `host.add_world(leaf, tool_allow_list=[])` first (canonical) and `middle` adding
  `leaf` second, `leaf_write` runs host+middle+leaf. Today it runs host+leaf.
- **Three deep** (host→a→b→leaf, plus host→leaf `x_`): `leaf_write` runs host, a, b, leaf.
- **Intermediate error handler**: `middle`'s handler maps the leaf's error on `m_refuse`, and
  `l_refuse` raises the leaf's own error.
- **Composite fixtures**: chains are not frozen. Sidecar, paths, file and schema names, change-log
  `world`, `NodeReport` and edges are unchanged, because paths still come from `_walk`.
- **SH207** (one tool under two names): still a warning. The two names can now also differ in
  middleware; `reference/lints.md` says so (§3).

## 3. Docs

`src/seahaven/docs/composition.md`, the first sentence of `## Middleware` (line 378). Replace
"along the canonical route from the root to the owning node" so it reads:

> An agent call to a contributed tool descends the middleware of every world on the route that
> contributed that tool, from the root to the owning world, outermost first: the host's, then any
> world in between, then the owning world's, and then the tool. When a shared node's tool reaches
> the surface under two names by two routes, each name runs its own route's middleware.

Nothing else on the page names the canonical route for middleware.

`src/seahaven/docs/reference/lints.md`, SH207 **Why**: add one sentence, "Each name also runs the
middleware of its own route."
 "Paths and aliases" (paths,
files, ids, change log) stays correct. The `authoring.md:509` note stays correct.

Code comments: update the module docstring line 7-8 ("the chains their calls descend" becomes
"the chain each tool on the surface descends") and the `_route_layers` docstring (a route, not
the canonical route). Update the `Node` field comment and the `_Candidate` docstring to describe
`hosts`.

## 4. Tests (all through `world.instance(...)` unless marked seal)

`tests/test_composite_dispatch.py`, in the "the chain" section. They reuse `rooted`, `traced` and
`composable_world`. Add a helper `shared_under_a_middle(tmp_path, trace)`: host adds traced
`middle` (which adds traced `leaf`) with `m_`, then adds `leaf` with `l_`.

1. Rename `test_the_agent_chain_is_the_whole_canonical_route` to
   `test_the_agent_chain_is_the_whole_contributing_route`. The body is unchanged. This is the only
   existing test that changes.
2. `test_sharing_a_node_keeps_the_middle_worlds_middleware_on_what_it_contributes`: the reviewer's
   reproduction. `company` adds `shop` (`shop_`, with a recording middleware, adds `payments`).
   Call `shop_payments_write`. Then `company.add_world(payments, tool_prefix="pay_")`, make a new
   instance and call both names. Shop's middleware ran for `shop_payments_write` both times and
   never for `pay_payments_write`.
3. `test_each_name_of_a_shared_tool_runs_its_own_routes_middleware`: in `shared_under_a_middle`,
   the trace for `m_leaf_write` is host/middle/leaf and for `l_leaf_write` is host/leaf. Both reads
   return both rows, which proves it is one store.
4. `test_both_sides_of_a_diamond_run_their_own_middle`: `r_leaf_write` gives
   `[host, right, leaf]` and `l_leaf_write` gives `[host, left, leaf]`.
5. `test_a_route_the_lists_filter_out_does_not_choose_the_chain`: the canonical edge has
   `tool_allow_list=[]` and `middle`'s route contributes, so `leaf_write` runs host/middle/leaf.
   The same holds by function reference: `live.call(leaf.tools["leaf_write"].fn, ...)`.
6. `test_a_shared_leaf_three_deep_keeps_every_world_between`: host→a→b→leaf plus host→leaf `x_`.
   `leaf_write` gives `[host, a, b, leaf]` and `x_leaf_write` gives `[host, leaf]`.
7. `test_a_middle_worlds_error_handler_shapes_the_errors_it_contributes`: `leaf.refuse` raises
   `Boom`, and `middle`'s middleware re-raises it as its own `ToolError` subclass. `m_refuse`
   raises middle's error and `l_refuse` raises `Boom`.
8. `test_a_middleware_registered_later_reaches_a_route_through_a_shared_node`: on a live instance,
   register a middleware on `middle`. It runs on `m_leaf_write` and not on `l_leaf_write`.
9. Guard: `test_a_nested_call_into_a_shared_node_runs_only_the_owners_layer`. A host tool calls
   `ctx.worlds.middle.worlds.leaf.call("leaf_write")`, and the trace is `[host, leaf]`. It passes
   today too, and pins the rule that a handle call does not take a route.

`tests/test_composition.py`, in the "contribution" section (seal, no instance):

10. `test_each_entry_carries_the_chain_of_the_route_that_contributed_it`: `m_leaf_write` and
    `l_leaf_write` have the same `.node` but different `.chain`.
11. `test_entries_of_one_route_share_one_chain`: `m_leaf_write.chain is m_leaf_read.chain`, and a
    root tool's `.chain is host.chain`.

Each of tests 2-8 and 10-11 fails on today's code (confirmed on the prototype's scratch copies).

## 5. Decisions

- `internal_chain` is renamed `own_chain`.
- SH207's **Why** in `reference/lints.md` gains the middleware sentence (§3).
- This design removes `parents` from `_walk`. Phase 1's hook order (4.M2, `hosts_first`) walks
  `Node.added` and does not read `parents`, so the two changes do not conflict; phase 1 lands
  first.
