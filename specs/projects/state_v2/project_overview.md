---
status: draft
---

# State Format (v2: restarted on top of composition)

Design what `state()` returns: a really high-quality state system. This is what a client uses to
judge whether an episode was successful or not (we don't have internal rewards), so a lot rests on
this.

## What state carries

- **Database state**
  - A diff of all database state changes.
  - In a format that's excellent for consumers (see below).
  - P2: allow multiple formats.
- **Other environment state**
  - Tool counts: total, and total by tool name.
  - Tool calls: a list of tool calls and their params.
  - Tool runtime? No: we're mocked.
  - Step count.
  - Tool error counts.
- **Filtering**
  - Does `state` take options to filter, or always return everything? Things like tool counts are
    derivable if we return everything, so options only earn their place if they are needed for
    something else.
  - The tool-call list or the DB diff could be huge.
  - That said, filtering fights "stable and reusable" (below), so maybe we drop it.

## Stable and reusable

We want to design state to be pretty stable. Frameworks will run an episode and save `final_state`.
They may create new judges later that need to run against `final_state`, long after the episode
ran.

### Setup for versioning

This output is essential. It also will have to improve over time.

Can we call `state(format="v1")`, and set ourselves up to evolve the state? You can still keep
running existing evals against it using an older format key, but adopt newer, better ones for new
evals. Basically have a `StateFormatter`: a pluggable interface. Each World can specify its default
state format, for data consistency across Seahaven upgrades. Callers can request whatever they
want.

## Control tools vs state

We previously spec'ed control tools as the way an external judge can judge final state. We gave it
read-only SQL access (`controller_run_sql`) and the changeset (`controller_changes`).

This fails the "stable and reusable" bar above. We want `state` to replace them. We don't need to
pull them out right away, but we should update the docs to push `state()` as the primary surface.

Note: the OpenEnv surface, including `state`, is documented in
`src/seahaven/docs/serving_and_openenv.md`. Updating it for this design is part of this project's
docs work.

## DB format

This is a key design decision. Our design is that one world/fixture might have hundreds of
goals/judges in an external RL/eval system. Read the OpenEnv doc for some insights.

We could write a code judge for each, but that's expensive and error-prone. We could use an LLM
judge for each, but that's also expensive and error-prone.

We should ideate on a judge system that can work well in this world. Even though it's not part of
Seahaven, it needs to work on the `state()` output, so its design is coupled.

**Current thinking:** a judge that uses something like Jinja2 on the final state, paired with an
expected result.

- A list of expectations, each with a Jinja expression to evaluate on `final_state`, an expected
  value, and a comparison.
  - Example 1
    - jinja: select the tool count for the tool `update_issue_state`
    - expected value: 12
    - comparison: less_than
  - Example 2
    - jinja: select the number of changed records from the database, filtered to table `issues`
    - expected value: 24
    - comparison: equal

## Start wide

Write the project overview now. But before a functional spec that says the actual format, discuss
what the right format should be, thinking through the consumers/judges. JSON and Jinja? Does that
scale to DB diffs?

Prior art: a research phase before the functional spec, then bring back some ideas to discuss
before we build the functional spec.

## Attempted and restarted

This project was built once already, as `state_format`, on branch `claude/happy-allen-inkfcc`
(tip `07fc86a`, five phases, all committed). It was never merged. The spec was written and every
phase implemented against a base sixty-seven commits behind `main`, and in that window `main`
landed **world composition**: an instance stopped being one file, one connection, one id stream and
one changeset session, and became a tree of nodes with one of each per node. The design below was
written for the old shape. Three of its decisions are wrong on the new one, not merely conflicted:

- **One session per call, on the root connection.** A call on a composite world writes to several
  node connections, and a nested call through `ctx.worlds.<name>.call(...)` never enters
  `Instance.call`. The change log would silently miss both.
- **`subworld`, always `null`.** `main` already ships the concept as `Change.world`: populated, a
  `str`, `"main"` for the root and the node's canonical path otherwise. A `seahaven.state/1` that
  publishes `null` for rows `main` can name would cost a `/2` to correct.
- **An envelope that names one world and one fixture file.** `main` ships
  `Instance.composition()` (a `NodeReport` per node: path, world, version, scope, aliases, schema
  hash) and `SeahavenState.composition`. Provenance for an eval was solved twice, in two shapes.

Two more collisions of intent: `main` kept and extended `Instance.changes()` for composition in the
same window the old branch deleted it, and `World(state_format=...)` being required on every
`World` gives every leaf of a tree a pin that is never consulted. The bundled docs were also
rewritten and renamed under the old branch, so its documentation phase is a write-off in full.

This project, `state_v2`, restarts from `main`. The published format is still `seahaven.state/1`;
`v2` is the project, not the format. The old branch is **reference, not a base**: coding agents may
read it for how a decision was implemented and why, and its review rounds settled several details
worth keeping. Do not merge from it, and do not trust any part of it that assumes an instance is
one store.

### What comes over, and what restarts

The split is decided here in the spec, not left to a coding phase. The lists below are a first cut;
`implementation_plan.md` carries the final, per-phase version once the questions in this restart
are answered.

**Bring over** (replay onto `main`, adapted where composition demands it):

- `state.py` whole: `envelope`, `document`, `check_format_name`, the two built-in formats, and the
  name rules.
- The record shape and its rendering: `LogRecord` with `to_dict()` in field order, `render_log`,
  the SQLite-to-JSON value mapping, sorting on rendered key values, `_jsonable`'s infinity rule.
- The per-call session design, extended to one session per node per call.
- `Instance.state()` and its guards: the formatting guard keyed on the thread, the refusal inside a
  transaction, refusal after `destroy()`.
- `world.state_format(...)` as the registration decorator, `world.pinned_state_format` as the pin,
  the registry copied by `World.copy()`, `RESET_ARGUMENTS` gaining `state_format`.
- The OpenEnv surface: `SeahavenState` as the document plus `step_count`, `WorldRef` and
  `FixtureRef`, `reset(state_format=...)`, the episode id minted before the instance, and the gate
  tests that prove the whole document arrives over the WebSocket.
- The deprecation of `controller_run_sql` attributed to the caller's line with `skip_file_prefixes`.
- Test support: the fold as test code, the oracle over SQLite's own cumulative changeset, the
  published JSON schema with `additionalProperties: false`.
- Seed narrowing to `int | None`.
- The `bench/recording.py` probe and its report section, retargeted at per-node runtimes.

**Restart** (re-decided or re-authored against `main`):

- The `subworld` field: renamed, typed and populated to match `Change.world`.
- The envelope's provenance: composition and per-node fixture identity.
- Where the format pin is required and which pin governs a tree.
- Call ordinals on `main`'s two-stage dispatch (`call` → `_target` → `_dispatch`), and nested calls.
- The removal of `Instance.changes()`, `Change`, `render()` and `controller_changes`, re-planned
  against the callers `main` added for composition.
- Everything in `src/seahaven/docs/`: new page set, new house style (`AGENTS.md` "Docs style"),
  and `serving_and_openenv.md` in place of the deleted `serving.md` and `openenv.md`.
- The `BACKLOG.md` edits, which target a file `main` deleted on purpose. Deferred findings now go to
  `specs/projects/<project>/backlog.md` and are closed before merge.
