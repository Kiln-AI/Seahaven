---
status: draft
---

# Functional Spec: Startup SQL

## Summary

`setup_sql` is a new parameter of `world.instance(...)`, and therefore of OpenEnv `reset`, of the
HTTP API's instance creation and of `seahaven mcp --reset-options`. It holds SQL that the framework
runs against the new instance after the fixture is copied (or the blank schema is built) and before
any startup hook. An eval uses it to adjust the starting state of a served world without writing
code.

It reuses the startup step of instance creation. It is not a startup keyword and not an
`instance_startup` hook: `startup` stays the world's own keyword namespace.

## The interface

```py
world.instance(
    fixture=None, *, seed=None, now=None, clock_mode=None, state_format=None,
    control_tools=False, startup=None,
    setup_sql: str | Mapping[str, str] | None = None,
)
```

Over OpenEnv:

```jsonc
{"type": "reset", "data": {
  "fixture": "agency",
  "seed": 7,
  "setup_sql": "UPDATE issues SET due_at = '2026-10-01T00:00:00.000Z' WHERE key = 'ENG-12';
                DELETE FROM comments WHERE issue_id = (SELECT id FROM issues WHERE key = 'ENG-12');",
  "startup": {"user_id": "a0b45f75-0917-49b9-9efd-d0279f2dd73d"}
}}
```

### Value forms

| Form | Meaning |
|---|---|
| omitted or `null` | no setup SQL |
| a string | SQL for the root node (`main`) |
| an object | SQL per node, keyed by canonical path: `{"main": "...", "payments": "...", "payments/ledger": "..."}`. A node with no key gets no SQL |

The string form is shorthand for `{"main": "..."}`. A world that adds no other world only ever needs
the string.

A string may hold several statements separated by `;`. They run in order, on the node's own
connection, so a statement names that node's tables unqualified (`issues`, not `main.issues` or
`payments.charges`). An empty or whitespace-only string is accepted and runs nothing.

### Composition keys

Keys are canonical paths, the same keys as `composition` and `fixture["nodes"]` in the state
document: `main` for the root, `<name>` for a world the root adds, `<parent path>/<name>` deeper.
A key that is not a canonical path of this world's composition is refused. A key that is an alias
of a node is refused, and the message names the node's canonical path. This keeps one spelling per
node in the request and in the state document.

## Behaviour

### When it runs

Instance creation, in order:

1. Copy the fixture, or build the blank schema.
2. Open every node's transaction (as today, before startup hooks).
3. **Run `setup_sql`**, every node that has some, root first then hosts before what they add (the
   order startup hooks already use). Statements within one node run in the order written.
4. Run the startup hooks, as today. A hook sees the rows `setup_sql` wrote: for example, SQL that
   inserts a user and `startup={"user_id": <that id>}` together work.
5. Commit, and the instance is ready for its first call.

### The change log and the state document

- Rows `setup_sql` writes are **not** in the change log. No change-log session is open during
  creation; they are part of the starting state, as rows a startup hook writes are today. A
  grader folding the log sees only what the agent's calls changed. `call_count` is unchanged.
- The state document's envelope gains a field, `setup_sql`, beside `startup`: always the object
  form, keyed by canonical path, holding the SQL exactly as given. `{}` when there was none. The
  string form is reported as `{"main": "..."}`. Before the first `reset` over a server it is `null`,
  as `startup` is.
- `fixture.nodes[path].file_sha256` keeps naming the fixture file the node was copied from. A
  reader reconstructs the starting state from `fixture` plus `setup_sql` plus `startup`.

### What SQL is allowed

The docs tell an author to use `INSERT`, `UPDATE` and `DELETE`, and to change the schema with a new
world version, not with setup SQL.

Runtime enforcement (priority P3 unless the architecture step finds it cheap, which it likely is,
because `seahaven.sandbox.Authorizer` already default-denies everything but row reads, row writes
and allowlisted functions): refuse schema changes (`CREATE`, `DROP`, `ALTER`), `ATTACH`/`DETACH`,
`PRAGMA` writes, transaction control (`BEGIN`, `COMMIT`, `SAVEPOINT`) and functions outside the
`run_sql` allowlist. Read and write access covers every table of the node's schema.

Without enforcement, SQL that commits or changes the schema can leave the instance inconsistent.
Transaction control in particular would end the creation transaction early, so it must be refused
even if the rest is P3.

`random()` and `randomblob()` draw from the node's seeded stream, as everywhere else in an
instance, so the same `seed` and the same `setup_sql` give the same starting state.

### Errors

A failure aborts instance creation with `seahaven.WorldBug`, every node's transaction is rolled
back, and nothing is left behind, exactly as when a startup hook raises. Over OpenEnv the session is
left fresh (no instance, no episode) and open for another `reset`; the frame is `EXECUTION_ERROR`.

Refused before anything is copied (the checks `world.instance` already runs first):

- `setup_sql` is not a string, an object of strings, or `null`.
- An object key that is not a canonical path, or is an alias (message names the canonical path).

Refused while running:

- A statement SQLite rejects (syntax, constraint, missing table). The message names the node path,
  the statement's position in the string (1-based) and SQLite's own text.
- A statement the enforcement refuses, named the same way.

## Every entry point

`setup_sql` is a `world.instance` parameter, so each place that forwards instance options takes it:

| Entry point | How it is passed |
|---|---|
| `world.instance(...)` | `setup_sql=` |
| OpenEnv `reset` (WebSocket and `SeahavenEnv.reset`) | `"setup_sql"` key; published in the reset schema from `GET /schema` |
| HTTP API instance creation (`PUT` body) and `seahaven serve` reset options | `"setup_sql"` key |
| `seahaven mcp --reset-options` | `"setup_sql"` key |
| `/ui` reset form | rendered from the reset schema; the object form may be entered as JSON |

It is always available. There is no operator flag to turn it off.

## Out of scope

- Bound parameters (`:now`, `:seed`). Statements are plain literal SQL.
- Cross-node statements (one statement touching two nodes' tables).
- Setup SQL on `add_world(...)` (a host fixing a child's SQL at composition time).
- A per-world opt-out.
- Turning setup SQL into a fixture (freezing). Authors who reuse the same setup across many evals
  should make a fixture instead; the docs say so.

## Documentation

- `serving_and_openenv.md`: a row in the reset argument table and one example.
- `db_schema_and_fixtures.md` or `authoring.md`: when to use `setup_sql` versus a fixture or a
  startup hook, and the advice to write rows only.
- `state.md`: the new envelope field.
- `reference/api.md`: the `world.instance` signature.
- `composition.md`: the object form and its keys.
