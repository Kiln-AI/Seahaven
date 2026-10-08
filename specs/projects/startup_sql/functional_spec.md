---
status: complete
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

The single string below is the preferred design. It depends on a new writable connection that
attaches every node. If the architecture step finds that connection adds disproportionate
complexity or risk (locking, triggers, seeding, attach limits), it may push back to the fallback:
`setup_sql: str | Mapping[str, str] | None`, where a string is the root's SQL and an object maps a
canonical node path to that node's SQL, run on each node's own connection. The fallback needs a
per-world reset schema and a console change to render well; the architecture must cover both if
it is chosen.

```py
world.instance(
    fixture=None, *, seed=None, now=None, clock_mode=None, state_format=None,
    control_tools=False, startup=None,
    setup_sql: str | None = None,
)
```

Over OpenEnv:

```jsonc
{"type": "reset", "data": {
  "fixture": "agency",
  "seed": 7,
  "setup_sql": "UPDATE issues SET due_at = '2026-10-01T00:00:00.000Z' WHERE key = 'ENG-12'; DELETE FROM comments WHERE issue_id = (SELECT id FROM issues WHERE key = 'ENG-12');",
  "startup": {"user_id": "a0b45f75-0917-49b9-9efd-d0279f2dd73d"}
}}
```

`setup_sql` is one string. It may hold several statements separated by `;`, and they run in the
order written. `null` or omitted means no setup SQL. An empty or whitespace-only string is accepted
and runs nothing.

### Table names in a composed world

The SQL runs on one connection that sees every node, named as `inst.inspect()` names them:

- the root's tables unqualified (`issues`), or as `main.issues`;
- an added node's tables under a schema named by its canonical path, with `/` replaced by `__`:
  `payments.charges`, `payments__tax.rates`.

A statement may read and write across nodes, for example
`INSERT INTO payments.charges (...) SELECT ... FROM orders`. A world that adds no other world only
ever uses unqualified names.

## Behaviour

### When it runs

Instance creation, in order:

1. Copy the fixture, or build the blank schema.
2. **Run `setup_sql`** in its own transaction, on a writable connection to the root's file with
   every other node attached writable under its schema name. Commit, then close the connection.
3. Open every node's transaction and run the startup hooks, as today. A hook sees the rows
   `setup_sql` wrote: for example, SQL that inserts a user and `startup={"user_id": <that id>}`
   together work.
4. Commit, and the instance is ready for its first call.

Step 2 commits before step 3 so that its write locks on the attached files cannot block a hook.
Creation is still all or nothing: a failure at any step removes the whole instance directory, as
a failing startup hook does today.

### The change log and the state document

- Rows `setup_sql` writes are **not** in the change log. Change-log sessions are opened only per
  tool call; creation runs before any exists. They are part of the starting state, as rows a startup
  hook writes are today. A grader folding the log sees only what the agent's calls changed.
  `call_count` is unchanged.
- The state document's envelope gains a field, `setup_sql`, beside `startup`: the string exactly as
  given, or `null` when there was none (and before the first `reset` over a server).
- `fixture.nodes[path].file_sha256` keeps naming the fixture file each node was copied from. A
  reader reconstructs the starting state from `fixture` plus `setup_sql` plus `startup`.

### What SQL is allowed

The docs tell an author to use `INSERT`, `UPDATE` and `DELETE`, and to change the schema with a new
world version, not with setup SQL.

Runtime enforcement is part of v1, through `seahaven.sandbox.Authorizer`, which already
default-denies everything but row reads, row writes and allowlisted functions. It refuses schema
changes (`CREATE`, `DROP`, `ALTER`), `ATTACH`/`DETACH`, `PRAGMA` writes, transaction control
(`BEGIN`, `COMMIT`, `SAVEPOINT`) and functions outside the `run_sql` allowlist. Read and write
access covers every table of every node.

A refusal fails loudly: instance creation aborts with a `WorldBug` whose message names the
statement's position, what was refused, and what to do instead. For example: "setup_sql statement
2 was refused: CREATE TABLE is a schema change. setup_sql may only read and write rows (INSERT,
UPDATE, DELETE); change the schema in the world and bump its version."

Transaction control would end the setup transaction early, and `ATTACH` and `DETACH` would change
the setup connection's attachments, which are the framework's.

`random()` and `randomblob()` draw from the root node's seeded stream, whichever node the row lands
in, so the same `seed` and the same `setup_sql` give the same starting state.

### Errors

A failure aborts instance creation with `seahaven.WorldBug` and nothing is left behind, exactly as
when a startup hook raises. Over OpenEnv the session is
left fresh (no instance, no episode) and open for another `reset`; the frame is `EXECUTION_ERROR`.

Refused before anything is copied (the checks `world.instance` already runs first):

- `setup_sql` is not a string or `null`.

Refused while running:

- A statement SQLite rejects (syntax, constraint, missing table). The message names the
  statement's position in the string (1-based) and SQLite's own text.
- A statement the enforcement refuses, named the same way.

## Every entry point

`setup_sql` is a `world.instance` parameter, so each place that forwards instance options takes it:

| Entry point | How it is passed |
|---|---|
| `world.instance(...)` | `setup_sql=` |
| OpenEnv `reset` (WebSocket and `SeahavenEnv.reset`) | `"setup_sql"` key; published in the reset schema from `GET /schema` |
| HTTP API instance creation (`PUT` body) and `seahaven serve` reset options | `"setup_sql"` key |
| `seahaven mcp --reset-options` | `"setup_sql"` key |
| `/ui` reset form | rendered from the reset schema as a multi-line text box; no console change |

It is always available. There is no operator flag to turn it off.

## Out of scope

- Bound parameters (`:now`, `:seed`). Statements are plain literal SQL.
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
- `composition.md`: the schema names an added node's tables have in `setup_sql`.
