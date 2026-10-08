---
status: complete
---

# Architecture: Startup SQL

`setup_sql: str | None` is a new `world.instance(...)` parameter. Instance creation runs it on one
writable connection to the root's file with every added node attached, after the files are in place
and before any node connection is opened. Everything else is plumbing the value through the entry
points and reporting it in the state document.

The single-string design from the functional spec stands. A prototype against two copies of the
`agency` fixture (root plus one attached node, both with FTS5 and its triggers) confirmed every
risk the spec named:

- Cross-node writes in one string work, FTS5 triggers fire in the attached file, and the search
  index is correct in both files when reopened through `open_instance`.
- `Authorizer(read_only=False)` plus the `_harden` settings refuse every statement the spec refuses
  (results under [Refusals](#refusals)).
- Running before `_open_node` means no second connection exists, so there is no locking question.

No pushback to the fallback (per-node object) is needed.

## Contents

- [Flow in `InstanceManager.create`](#flow-in-instancemanagercreate)
- [New module `seahaven/setup_sql.py`](#new-module-seahavensetup_sqlpy)
- [Refusals](#refusals)
- [Error messages](#error-messages)
- [Plumbing](#plumbing)
- [State document](#state-document)
- [Docs](#docs)
- [Testing](#testing)

## Flow in `InstanceManager.create`

`src/seahaven/instances.py` `create(...)` gains `setup_sql: str | None = None`, keyword-only, after
`startup`. Changes, in the order the method runs:

1. **Pre-copy checks** (beside `_check_startup_keywords`, ~:1125): `_check_setup_sql(setup_sql)`
   refuses anything that is not `str` or `None` with a `WorldBug`:
   `setup_sql takes a string of SQL statements separated by ';', not {type name}`.
   Nothing is parsed here.
2. Clock, seed, fixture copy or blank build: unchanged.
3. **New step, directly after the copy/build and before the `_open_node` loop (~:1167):**

   ```py
   if setup_sql is not None:
       run_setup_sql(
           setup_sql,
           root=directory / composition.root.file_name,
           attachments=[
               (node.schema_name, directory / node.file_name)
               for node in composition.nodes
               if node.depth > 0
           ],
           clock=clock,
           seed=node_seed(base, ROOT_PATH),
       )
   ```

   (`Composition.root` is the root `Node`. The attachment list
   mirrors `Instance._attachments()` at :898, built from the composition because the runtime does
   not exist yet. Factor a shared `_attachment_pairs(nodes, directory)` helper so both read the
   same rule.)
4. `Instance(...)` gains `setup_sql=setup_sql`.
5. Startup hooks, `tracked_tables`: unchanged. Hooks see the committed setup rows.

A failure in step 3 raises inside the existing `try`, so the existing `except BaseException` closes
nothing (the runtime is still empty) and removes the directory. No new cleanup code.

`World.instance` (`src/seahaven/world.py:515`) gains the same parameter and forwards it.

## New module `seahaven/setup_sql.py`

One public function, two private helpers. Lives beside `sandbox.py` and reuses its `Authorizer`.

```py
def run_setup_sql(
    sql: str,
    *,
    root: Path,
    attachments: Sequence[tuple[str, Path]],
    clock: Clock,
    seed: bytes,
) -> None:
    """Run a caller's setup SQL against a new instance's files, in one transaction."""
```

Steps:

1. `statements = split_statements(sql)`. Empty list: return without opening anything.
2. Open `apsw.Connection(str(root))`. Apply the same settings `open_instance` applies, minus WAL
   (the WAL switch belongs to `open_instance`, which runs next):
   `_harden(conn)`, `pragma synchronous OFF`, `set_busy_timeout(0)`,
   `register_random_functions(conn, seed, SETUP_STREAM)`,
   `helper = register_clock_functions(conn, clock)`.
3. For each `(schema, path)`: `conn.execute("ATTACH DATABASE ? AS ?", (str(path), schema))`. Bound
   parameters, as `open_inspection` does. Plain path, not the read-only URI.
4. `tables = _every_table(conn)`:
   `SELECT name FROM pragma_table_list WHERE type IN ('table', 'virtual', 'shadow') AND name NOT LIKE 'sqlite\_%' ESCAPE '\'`.
   Shadow tables must be listed: FTS5 reads and writes them through this connection, and its
   virtual-table constructor reads `<name>_config` while the caller's statement is prepared. A
   direct write to a shadow table by the caller is still refused, by `SQLITE_DBCONFIG_DEFENSIVE`
   (on via `_harden`): "table issues_fts_data may not be modified".
5. `conn.execute("BEGIN")`, *then* `conn.authorizer = authorizer`. The order matters: the
   authorizer refuses transaction control, including the framework's own `BEGIN` and `COMMIT`.
6. For `index, statement` in `enumerate(statements, 1)`: `authorizer.reset()`, then
   `for _ in conn.execute(statement): pass` (drain, so a `SELECT` runs to completion and its rows
   are discarded).
7. On any exception: `conn.authorizer = None`, `ROLLBACK`, raise the `WorldBug` from
   [Error messages](#error-messages) `from` the original.
8. On success: `conn.authorizer = None`, `COMMIT`.
9. `finally`: `conn.close()`, `helper.close()`.

`SETUP_STREAM = b"setup"` is added to `src/seahaven/ids.py` beside the other streams. A stream of
its own keeps the instance's `INSTANCE_STREAM` draws identical whether or not setup SQL ran, and the
root's node seed makes it deterministic per `seed`.

### `split_statements(sql: str) -> list[str]`

Splits on `;` and joins pieces until `apsw.complete(piece)` is true, so a `;` inside a string
literal, a quoted identifier or a comment does not split. A trailing incomplete fragment is kept as
the last statement and fails with SQLite's own "incomplete input". Pieces that are only whitespace
are dropped; a piece that is only a comment runs as nothing. Verified in the prototype on
`"UPDATE a SET t='x;y'; -- c;\n CREATE TRIGGER ... BEGIN SELECT 1; SELECT 2; END; SELECT 3"` → three
statements.

Splitting ourselves, rather than one `execute` of the whole string, is what makes the statement
number in an error exact: with a single `execute`, a refusal at prepare time and a failure at step
time are indistinguishable.

### `SetupAuthorizer(Authorizer)`

A subclass that records the first refused action as `(action, third, fourth)` on top of what the
base class does, so the error message can be written from the action code rather than by parsing
the base class's refusal text. `__call__` calls `super().__call__(...)`, and when the answer is
`SQLITE_DENY` and nothing is recorded yet, records it. `reset()` clears it. Constructed with
`tables` and `read_only=False`; functions default to `ALLOWED_FUNCTIONS`, the `run_sql` allowlist
(includes `random`, `randomblob`, `datetime`).

`MAX_VALUE_BYTES` is not applied: it bounds an agent's statement, and setup SQL comes from the eval.

## Refusals

Prototype results with this design (refused action → what SQLite or the authorizer reported):

| Statement | Refused as |
|---|---|
| `CREATE TABLE`, `DROP TABLE`, `CREATE INDEX` | write to `sqlite_master` |
| `ALTER TABLE` | `SQLITE_ALTER_TABLE` |
| `CREATE TRIGGER`, `CREATE VIEW` | `SQLITE_CREATE_TRIGGER`, `SQLITE_CREATE_VIEW` |
| `BEGIN`, `COMMIT`, `ROLLBACK` | `SQLITE_TRANSACTION` |
| `SAVEPOINT`, `RELEASE` | `SQLITE_SAVEPOINT` |
| `ATTACH`, `DETACH` | `SQLITE_ATTACH`, `SQLITE_DETACH` |
| `PRAGMA journal_mode = ...`, `PRAGMA foreign_keys = OFF` | `SQLITE_PRAGMA` not on the read-only list |
| `SELECT load_extension(...)` | `SQLITE_FUNCTION` not on the allowlist |
| `INSERT INTO issues_fts_data ...` | SQLite (defensive mode): "may not be modified" |
| `UPDATE sqlite_master ...` | SQLite: "may not be modified" |

Allowed: `INSERT`, `UPDATE`, `DELETE`, `SELECT`, `INSERT ... SELECT` across nodes, FTS5 commands
such as `INSERT INTO issues_fts(issues_fts) VALUES('rebuild')`, read-only pragmas. Foreign keys are
on, so `DELETE FROM users` in `agency` fails with SQLite's constraint error.

## Error messages

Every failure is one `WorldBug`, raised from the original exception, with this shape:

```
setup_sql statement {i} of {n} {was refused|failed}: {reason} {fix}
  {statement, whitespace collapsed, cut at 200 characters with "..."}
```

`_explain(recorded, error, schemas)` writes `reason` and `fix`:

| Case | reason / fix |
|---|---|
| write to `sqlite_master`, or a `CREATE_*`, `DROP_*`, `ALTER_TABLE`, `REINDEX`, `ANALYZE` action | "it changes the schema." / "setup_sql may only read and write rows (INSERT, UPDATE, DELETE, SELECT). Change the schema in the world and bump its version." |
| `TRANSACTION`, `SAVEPOINT` | "it controls the transaction." / "setup_sql already runs in one transaction that Seahaven opens and commits; remove BEGIN, COMMIT and SAVEPOINT." |
| `ATTACH`, `DETACH` | "it attaches or detaches a database." / "Every node is already attached: name the root's tables unqualified and an added node's as <schema>.<table>" + `" (this world: payments, payments__tax)."` or `" (this world adds none)."` |
| `PRAGMA` | "PRAGMA {name} can change the connection." / "Only read-only pragmas such as table_info are allowed." |
| `FUNCTION` | "function {name}() is not allowed." / "setup_sql has the functions run_sql has." |
| any other recorded action | "{the base class's description} is not allowed." / "setup_sql may only read and write rows." |
| nothing recorded (a plain SQLite error) | SQLite's own message. / If it starts with "no such table" and the world adds nodes: "An added node's tables are named <schema>.<table> (this world: ...)." Otherwise no fix. |

"was refused" for a recorded refusal, "failed" for a plain SQLite error.

## Plumbing

From the touchpoint survey; each item is a one-line change unless noted.

**In process**
- `world.py:515` `World.instance`: parameter (after `startup`), docstring line, forward.
- `instances.py:1074` `InstanceManager.create`: as above.
- `instances.py:290` `Instance.__init__`: `setup_sql: str | None`; store `self.setup_sql` beside
  `self.startup` (:334). Public read-only attribute, listed in `reference/api.md`'s `inst.*` table.
- `pytest_plugin.py:47` `_MARKER_SIGNATURE`: add `setup_sql=None` in `World.instance` order.

**OpenEnv** (`src/seahaven/openenv/env.py`)
- `RESET_ORDER` (:342): add `"setup_sql"` after `"startup"`. This also orders the console form.
- `SeahavenResetRequest` (:353): `setup_sql: str | None = Field(default=None, description=...)`.
  `for_world` needs no change.
- `SeahavenEnv.reset` (:480): parameter before `**unknown`; forward at :581; docstring.
- `SeahavenState` (:317): `setup_sql: str | None = Field(default=None, description=...)` before
  `call_count`; update the count in the comment at :275 ("the six it answers null for").

**HTTP and MCP**: no code change. `RESET_OPTION_KEYS` (`cli/mcp.py:90`) is derived from
`inspect.signature(World.instance)`, and `check_reset_options`, `_make` and the PUT handler all use
it or pass `**kwargs`. Update the stale comment at `cli/mcp.py:88` ("pins the five names").

**Console**: no code change. A `str | None` field titled "Setup Sql" unwraps to a string, and
`kindOf` maps a title matching `/sql/` to a multi-line text box. Add the field to the mock reset
schema in `tools/console/mock/server.mjs` and the form order in `tools/console/e2e/smoke.mjs`.

## State document

`src/seahaven/state.py` `envelope()` (:80): add
`"setup_sql": instance.setup_sql if instance is not None else None` directly after `startup`. The
value is the string exactly as given (an empty string stays `""`), `None` when none was given and
before the first `reset`. It is not run through `serialise`: it is already a string.

The change log needs no code: sessions open per call (`instances.py:745`), and setup runs before the
instance exists. A test pins it (below).

## Docs

Per the AGENTS.md docs style, short and checked against the code:

- `serving_and_openenv.md`: a row in the reset table (:127); a row in the `SeahavenState` table
  (:318); add `setup_sql` to the pre-reset null list (:339); one example reset message with
  `setup_sql`.
- `db_schema_and_fixtures.md`: a short section, "Adjusting a fixture per episode": what
  `setup_sql` is for, rows only, the composed-world table names, and when a new fixture or a startup
  hook is the better tool. One `python` example that runs (projecttracker-free, on a tiny inline
  world, so `test_docs_examples.py` executes it).
- `state.md`: example JSON (:61) and envelope table row (:79).
- `composition.md`: one sentence and a fragment showing `payments.charges` in `setup_sql`.
- `reference/api.md`: `world.instance` row (:123), marker signature (:628), `inst.setup_sql` row.
- `reference/cli.md`: mention under `--reset-options` (:204).

## Testing

New file `tests/test_setup_sql.py`. Each test drives `world.instance(...)` unless it says otherwise,
per the AGENTS.md rule to cover the real entry point. Worlds are small inline worlds, one leaf and
one composed (host plus an added `payments` world, plus one with an FTS5 table and sync triggers),
in the style of `tests/test_composite_instance.py`.

Behaviour:
- rows written by `setup_sql` are visible to the first tool call and to `inst.inspect()`;
- several statements run in order (a later statement sees an earlier one's row);
- `;` inside a string literal does not split;
- a composed world: an unqualified name writes the root, `payments.charges` writes the node, and
  `INSERT INTO payments.charges ... SELECT ... FROM <root table>` works;
- FTS5: an `UPDATE` through `setup_sql` is found by an FTS `MATCH` in the first tool call;
- a startup hook sees setup rows (hook reads a row the SQL inserted);
- same `seed` + same `setup_sql` → identical `random()` results; and an instance's own
  `random()` draws in a tool call are the same with and without `setup_sql` (the separate stream);
- `setup_sql` rows are not in `inst.change_log()`, and `call_count` is 0 after creation;
- `inst.state()["setup_sql"]` is the string as given; `None` when omitted;
- `""`, whitespace and a comment-only string create the instance and change nothing.

Refusals and errors (each asserts the `WorldBug` message: statement number, reason, fix):
- one test per row of the [Refusals](#refusals) table, parametrized;
- a refusal in statement 2 rolls back statement 1, and no instance directory is left behind;
- syntax error, constraint failure, and "no such table" with the composed-world hint;
- a non-string `setup_sql` is refused before anything is copied (no directory created).

Unit tests: `split_statements` cases (literal `;`, quoted identifier, comment, trigger body,
trailing fragment, empty); `_explain` per row of the error table.

Entry points:
- OpenEnv: `SeahavenEnv.reset(fixture=..., setup_sql=...)` in process, and one WebSocket round trip
  through the served app (as `tests/test_server.py` does), asserting the row and the state field;
  a failing `setup_sql` leaves the session fresh and open for another `reset`.
- HTTP: a `PUT` body with `setup_sql` creates an instance with the row.
- MCP: `--reset-options '{"setup_sql": "..."}'` accepted (in the mcp environment's tests).
- projecttracker: one test in `worlds/projecttracker/tests` resets `agency` with `setup_sql`
  inserting a user and `startup={"user_id": <that user>}`, then `create_issue` is attributed to them.

Pins to update (from the survey): `tests/test_env.py` `RESET_PARAMETERS` (:256) and
`DECLARED_DESCRIPTIONS` (:1010); `tests/test_reset_schema.py` property order (:86);
`tests/test_cli_mcp.py` key list (:285) and messages (:316); `tests/test_mcp_process.py:587`;
`tests/state_v1.schema.json` (`required` and `properties`, `["string", "null"]`);
`tests/test_state.py` key order (:66) and no-instance nulls (:346); `tests/test_server.py`
`expected_document` (:445); `tests/test_client.py` `DOCUMENT_FRAME` (:112);
`tests/test_pytest_plugin.py:629` (automatic once the marker signature is updated);
`tests/test_world.py:791` framework-parameter list.
