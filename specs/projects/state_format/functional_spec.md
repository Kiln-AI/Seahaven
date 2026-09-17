---
status: complete
---

# Functional Spec: State Format

What `state()` returns, how a caller chooses the format it returns, and what a world and a
consumer may rely on for as long as a saved state document exists. Written from
`project_overview.md` and the decisions log in `ideas_for_discussion.md`, which records why each
choice was made; this document records what is built. Where the two disagree, this one is wrong
and should be fixed.

## 1. Purpose

An eval or RL framework runs an episode against a Seahaven world, reads `state()` once at the end,
and saves it as `final_state`. Judges run against that document, possibly hundreds per world, and
possibly years later, against a Seahaven that has moved on. Seahaven has no internal reward, so
this document is the only thing an episode's success is judged on.

Three consequences drive everything below:

- **The document is state, not reason.** It records what the episode left behind in the database,
  and enough provenance to interpret it. It does not record the tool calls that produced it; that
  is the harness's trace, which the harness already keeps, with more context than state could
  carry.
- **Nothing derivable, nothing the consumer already has.** No net diff beside the log, no counts,
  no whole rows, no whole database, no schema. Every one of those is a lookup or a fold away.
- **Versioned and frozen.** A document says which format it is in; a published format never
  changes meaning; a world pins the format it produces so that upgrading Seahaven never changes
  what a running eval saves.

## 2. Concepts

- **State document.** A JSON object, the whole answer to `state()`: a framework-owned
  **envelope** of provenance fields, plus one key, `state`, holding the **formatter's output**.
- **Format.** A name of the form `<family>/<major>`, such as `seahaven.state/1`. It names the
  shape of `state`, not of the envelope. Two are built in (§3, §4). A world may register more
  (§6). A format, once published in a Seahaven release, never changes in a way a reader could
  observe (§10).
- **Change log.** The ordered record of every row the instance changed after startup, one record
  per row per call. It is the only database content in the document. The net diff, what
  `inst.changes()` returns today, is a fold of it (§3.6) and is not in the document.
- **Call ordinal `i`.** The position of a call among every call dispatched to the instance, from 0
  (§7). Log records carry it; it is the join key to the harness's trace.

## 3. The document, and the format `seahaven.state/1`

### 3.1 Root: the envelope and `state`

```jsonc
{
  "format": "seahaven.state/1",
  "seahaven_version": "0.0.1",
  "world": {"name": "projecttracker", "version": "1.0.0"},
  "fixture": {"id": "small_startup", "file_sha256": "…"},     // null for a blank instance
  "episode_id": "…",
  "seed": 7,                                                   // as given to reset; null if none
  "now": "2026-03-04T09:00:00Z",
  "startup": {"user_id": "u_12"},                              // {} when none were given
  "call_count": 3,
  "state": {"db": {"log": [ … ]}}                              // the formatter's output
}
```

Everything above `state` is the **envelope**: written by the framework, identical for every
format, built-in or custom. `state` is the formatter's output and the only thing `format`
describes. For `seahaven.state/1`, `state` is `{"db": {"log": [...]}}`.

| Field | Meaning |
|---|---|
| `format` | The name of the format that produced `state`, always first. A reader checks it before reading `state`. |
| `seahaven_version` | `seahaven.__version__` of the producing process. Informational; a reader keys on `format`, never on this. |
| `world.name`, `world.version` | `World.name` and `World.version`. `world.version` is the world's identity: the docs say a schema, tool or state-format change bumps it. |
| `fixture` | `{id, file_sha256}` from the fixture's sidecar, or `null` for an instance built from the DDL. With the world identity, this is the lookup for the starting state. |
| `episode_id` | Over OpenEnv, the session's episode id (the one given to `reset`, or the generated one). In-process, the instance id. |
| `seed` | The `seed=` the caller gave: an integer, or `null`. The API narrows to `int \| None` (§8). |
| `now` | The instance clock as an ISO-8601 instant, the same string `inst.clock.iso()` answers. |
| `startup` | The reset keywords beyond `fixture`, `seed`, `now` and `state_format`, exactly as the startup hooks received them. An object; empty when there were none. |
| `call_count` | How many calls have been dispatched to the instance so far (§7). Not OpenEnv's `step_count`, which also counts tool listings. |
| `state` | The formatter's output. Under `seahaven.state/1`: `{"db": {"log": [...]}}`, the change log (§3.2). |

No other root fields. A reader must ignore envelope fields it does not know (§10), so a later
addition does not break it; `state`'s contents are exactly what `format` defines.

### 3.2 The change log

`state.db.log` is one flat list. Each record is one row changed by one call:

```jsonc
{"i": 1, "subworld": null, "table": "issues", "op": "update",
 "key": {"id": "iss_3"},
 "before": {"status": "open", "updated_at": "2026-03-01T14:22:10Z"},
 "after":  {"status": "done", "updated_at": "2026-03-04T09:00:00Z"}}
```

| Field | Meaning |
|---|---|
| `i` | The call ordinal (§7) of the call that made the change, or `null` for a write made with no call in flight (`inst.bulk()`). |
| `subworld` | The sub-world the table belongs to, `null` for the root. Always `null` in this release; the field exists so a composed world's log stays one flat list. |
| `table` | The table name. |
| `op` | `"insert"`, `"update"` or `"delete"`. |
| `key` | The row's primary key, as `{column: value}` in primary-key column order. |
| `before` | `null` for an insert. For a delete, the whole row as it was. For an update, exactly the non-key columns the call changed, with their old values. |
| `after` | `null` for a delete. For an insert, the whole row. For an update, exactly the non-key columns the call changed, with their new values. |

Rules:

- **Net per call.** A call is one transaction. Its records are the net difference between the
  instance before the call and after it, per row: a row inserted and deleted in the same call
  leaves no record, a row updated back to its original values leaves none, an insert followed by
  an update in one call is one insert with the final values. That is SQLite's changeset semantics
  applied to one call.
- **Not net across calls.** A row touched in two calls appears twice, once per call. The docs say
  so in the two places it bites: counting rows straight off the log overcounts, so fold or
  deduplicate by key first; and two episodes with the same end state can have different logs, so
  "same end state" compares folds (§3.6), never logs.
- **The key is never repeated** inside `before` or `after` on an update. On an insert and a
  delete, the whole row includes the key columns, because that is the row.
- **A primary-key rewrite is a delete plus an insert**, never an update. Documented beside `op`.
- **Empty is empty.** A call that changed nothing has no records. A call that raised and rolled
  back has none. A read-only tool has none.
- **What is tracked** is what today's changeset tracks: every world table except those in
  `World(untracked_tables=...)`, FTS5 shadow tables, and virtual tables. A table with no explicit
  primary key is refused at instance creation, as today. A row with `NULL` in any primary-key
  column is never recorded, which is SQLite's rule; it cannot arise in a linted world, because a
  STRICT table refuses `NULL` in a primary-key column and SH101 requires STRICT (measured 2026-09-17
  on SQLite 3.45.1).
- **Startup is not in the log.** The session attaches after the startup hooks have run, as today.
  Every write after that is in the log, including `inst.bulk()` writes, which carry `i: null`.
- **No cap.** The log is as long as the episode made it. A consumer that wants less filters after
  the fact or registers a custom formatter (§6); the framework truncates nothing.

### 3.3 Order

Records are ordered by call: every record of call 0 before every record of call 1. Records with
`i: null` are ordered by when their transaction committed relative to the calls around them.
Within one call, records are sorted by `subworld` (`null` first), then `table`, then the key's
values in primary-key column order, ascending. Every tracked table is STRICT, so a key column has
one type and the comparison is well defined. Two identical episodes produce byte-identical logs.

### 3.4 Values

Every value is a JSON value, mapped from SQLite's storage classes:

| SQLite | JSON |
|---|---|
| INTEGER | number. SQLite integers are 64-bit; a JavaScript reader loses precision above 2^53. The docs say so; the format does not work around it. |
| REAL | number. An infinity renders as `null`: JSON carries neither infinities nor NaN, and SQLite itself stores NaN as NULL, so this is SQLite's own coercion one step further. Less precise, not wrong; a reader cannot tell a NULL column from an infinite one. The conversion carries a one-line comment stating that trade. |
| TEXT | string |
| BLOB | string, base64 |
| NULL | `null` |

### 3.5 The document before any call, and before `reset`

- After `reset` and before any call: the full envelope, `call_count: 0`, `state.db.log: []`.
- Over OpenEnv, before the first `reset`: there is no instance, and the **world's pinned**
  formatter runs with no instance (§6). The envelope is the framework's regardless of format:
  `format`, `seahaven_version` and `world` answered; `fixture`, `episode_id`, `seed`, `now` and
  `startup` `null`; `call_count` 0. For the built-ins `state` is `{"db": {"log": []}}`. A blank
  instance is distinguishable from no instance because a blank instance has a `now`. After
  `reset`, the instance's formatter runs. Never a default.

### 3.6 The fold, defined but not shipped

The net diff of an episode is the fold of its log, and the format defines the fold so that any
reader, in any language, computes the same one:

1. Group records by `(subworld, table, key)`; fold each group in log order, starting from
   "untouched".
2. Composition: untouched + insert = insert; insert + update = insert with the update's `after`
   overlaid; insert + delete = untouched; update + update = update with `before` from the first
   and `after` from the last, dropping any column whose folded `after` equals its folded `before`;
   update + delete = delete, with `before` being the delete's row with the update's `before`
   values overlaid; delete + insert = update if the rows differ (`before` the deleted row, `after`
   the inserted row, reduced to the differing non-key columns), else untouched.
3. Drop groups that folded to untouched. Sort by `(subworld, table, key)` as §3.3.

This is SQLite's changeset semantics over the whole episode. `inst.changes()` (unchanged in this
project) is the reference implementation: a test asserts
`fold(inst.state()["state"]["db"]["log"]) == inst.changes()` on every episode shape the suite
exercises (§13). The fold function that test uses
is test code, not public API, in this release.

## 4. The format `seahaven.state+last_step/1`

The same envelope as §3 and the same `state` shape, with one difference: `state.db.log` holds
only the records whose `i` equals `call_count - 1`, the most recent call. Records with `i: null`
are never in it.

It is idempotent: the scope is the call counter, not when `state()` was last read, so two reads
between calls answer the same document. It exists for a caller that reads `state()` after every
step, such as OpenEnv's rollout harness, which embeds the state in each step's record: under
`seahaven.state/1` that is the whole log per step, quadratic over an episode. Under this format
the per-step documents concatenate into the full log; a skipped read shows as a jump in
`call_count`; before any call the log is empty; and a harness that saves only the last document
as `final_state` has only the last call's changes, which is the trade the caller chose.

## 5. Choosing a format

- `World(state_format="seahaven.state/1")` is a **required** constructor argument. There is no
  default. A `World` constructed without it raises at construction, and the message names the
  current built-in formats. Every existing world in the repo (ProjectTracker, the example
  extension's world, every `World(...)` in tests, docs and `bench/`) gains the argument; accepted
  as a breaking change, since the framework is private and pre-alpha and every world is ours. The scaffold (`seahaven new`) writes `state_format="seahaven.state/1"` into the generated
  `world.py`, so a pin is chosen once, at the world's creation.
- `reset(state_format=...)` over OpenEnv and `world.instance(state_format=...)` in-process
  override the world's pin for that instance. `state_format` joins `fixture`, `seed` and `now` as a
  reserved reset keyword: a startup hook that names a parameter `state_format` fails at
  registration, as one naming `fixture` does today.
- An unknown format name is an error before any instance is created. A `seahaven.` name that is
  not a built-in fails at `World(...)`. A custom name can only be checked at instance creation,
  because a world registers its formatters after its `World(...)` line runs; so `World(state_format=
  "acme.state/1")` is accepted at construction and fails at the first `instance`/`reset` if nothing
  registered it by then. Over OpenEnv a refused reset surfaces as `EXECUTION_ERROR` and the
  session stays open, as any refused reset does.
- The format is fixed for the life of the instance. `inst.state()` answers it; `inst.state(format=
  "...")` answers another registered format for the same instance, in-process only. Over OpenEnv
  the state message carries no arguments, so the instance's format is the only one reachable.
- The docs recommend a world change its pin only with a `World.version` bump. Not enforced.

## 6. Custom formatters

A world may register formats of its own, for a caller whose needs differ from the two built-ins:

```python
@world.state_format("acme.state/1")
def acme_state(world: seahaven.World, instance: seahaven.Instance | None) -> dict[str, Any]: ...
```

- The name must contain exactly one `/` followed by a positive integer, and must not begin with
  `seahaven.`, which is reserved for built-ins.
- The function receives the world and the instance, or `None` for the instance before the first
  `reset` over OpenEnv (§3.5, §9), and returns a JSON-serialisable dict: **the value of `state`,
  and nothing else.** The framework writes the envelope around it, so a custom format can neither
  omit nor misspell provenance. With an instance it reads `instance.change_log()` and
  `instance.call_count` (§8), and anything else public on the instance; with `None` it defines
  its own "no instance yet" value, and one that raises on `None` makes pre-reset `state` a
  `WorldBug`, which is the formatter author's contract to keep. A formatter that wants a
  variation of a built-in reads `instance.state(format="seahaven.state/1")["state"]` and edits
  it.
- Registration is open for the life of the world, like tools and middleware, and an extension
  may register one. Registering a name twice, or a built-in name, fails.
- Formats resolve against the instance's world. In a composition (a later project; `subworld` is
  reserved for it) that is the root world: a formatter registered on the root sees the whole
  composed log, `subworld` values included, and formats all of it. A sub-world's own registrations
  serve it when it runs standalone and are not inherited by a root that composes it, unless the
  composition design says otherwise. Every formatter, the built-ins included, must therefore accept
  a log whose `subworld` is not always `null`.
- Formatters run under the instance lock and never in a transaction; they read, they do not
  write. One that writes raises `WorldBug`.

## 7. The call ordinal

`i` counts every call dispatched to the instance, from 0, in dispatch order: every `inst.call` in
process and every `CallToolAction` over OpenEnv, including a call that raised a `ToolError` and a
call refused as `UnknownTool`. It excludes control tools and tool listing (`ListToolsAction`,
`inst.tools()`), neither of which is a call to the world. Over OpenEnv it therefore matches the
harness's Nth `CallToolAction`, which the harness issued in order; `call_count` is the check that
the two agree before a join.

`call_count` is the number of calls dispatched so far, so the last call's ordinal is
`call_count - 1`.

## 8. The in-process API

```python
inst.state()                       # the document (envelope + state), in the instance's format, as a dict
inst.state(format="seahaven.state+last_step/1")
inst.change_log()                  # list[LogRecord], the §3.2 records, in §3.3 order
inst.call_count                    # int
inst.changes()                     # unchanged: the cumulative net diff, list[Change]
world.instance("agency", seed=7, state_format="seahaven.state/1", user_id="u_12")
```

- `inst.state()` returns a plain dict, JSON-serialisable with the standard library, so a caller
  saves it with `json.dump` and nothing else. It costs serialisation only: the log is kept in
  memory and appended to as each call commits, and `state()` does no database work.
- `inst.change_log()` returns the records as frozen dataclasses with a `to_dict()` of the §3.2
  shape, the way `Change` does today.
- `inst.changes()` keeps its contract. It remains the reference for the fold (§3.6).
- **`seed` is `int | None` everywhere.** `world.instance(seed=)`, the pytest marker and the seed
  derivation drop `bytes`, which nothing needed: the caller's value is hashed with the fixture id
  into the derived instance seed either way, and OpenEnv's `reset(seed=)` is already `int | None`.
  A `bytes` seed raises a `WorldBug`, as any other wrong type does today. Accepted as a breaking
  change on the same grounds as §5's.

## 9. Over OpenEnv

- The `state` message answers the document. `SeahavenState` is the document plus OpenEnv's
  `step_count`: every envelope field of §3.1 is a typed field on the model (`world` and `fixture`
  as nested models), `state` is `dict[str, Any]` because its shape is the format's, and the base
  class's `extra="allow"` is kept so a newer server can talk to an older client. `step_count` is
  OpenEnv's and not part of the document; a reader that wants the number of calls uses
  `call_count`. The existing `world: str`, `fixture: str | None` and `now: str | None` fields are
  replaced by the envelope's, which is a breaking change to `SeahavenState` and to
  `SeahavenClient.state()`'s return type. Accepted (2026-09-17): the old state was a placeholder
  and no consumer outside this repo exists. `SeahavenClient.state().state` is the formatter's
  output; `.model_dump(exclude={"step_count"})` is the document.
- `reset(state_format=...)` is passed through to `world.instance` like `fixture`, `seed` and `now`
  and never reaches a startup hook.
- Before the first `reset`, the document is §3.5's. `close` and a second `reset` discard the log
  with the instance.
- **Confirmation step.** The plan carries a test, against a real server over the WebSocket, that
  a client receives the whole document from the `state` message, every field of §3.1 included.
  An earlier session established that subclass fields travel over the WebSocket state message
  while the HTTP `GET /state` route strips them (`BACKLOG.md` B13); nothing here works unless that
  holds, so it is tested rather than assumed.
- `SeahavenClient.state()` returns the `SeahavenState` model.

## 10. The compatibility contract

For every format Seahaven publishes:

- The document has two contracts. The **envelope** is the framework's: a field is only ever
  added, never removed, renamed, retyped or rebuilt, so a reader keys on field presence and on
  `seahaven_version` only for information. **`state`** is the format's: `format` is the only thing
  a reader keys on for it.
- Within a format: no field of `state` is removed or renamed; none changes type; and the
  construction of an existing field does not change, even where the type would not, so a reader
  written against `seahaven.state/1` on the day it shipped reads every `seahaven.state/1`
  document ever produced.
- A field may be added within a format only if a reader that ignores it loses nothing. Readers
  must ignore unknown fields, in the envelope and in `state`. Anything else is a new major,
  `seahaven.state/2`, a new formatter, and the old one stays callable and frozen.
- Removing a published format is a breaking Seahaven release and is not planned.
- `world.version` is the world's contract: a judge assumes that two documents with the same
  `world.name` and `world.version` were produced by the same schema and tools.

## 11. Control tools

`controller_run_sql` and `controller_changes` are unchanged in behaviour and remain behind
`--include-control-tools` over the wire and always reachable in-process. Both are marked
deprecated: a `DeprecationWarning` on each call, and docstrings that name `state()` as the
replacement. They leave the docs entirely (§12). Removal is not scheduled in this project.

## 12. Documentation and lint

- **`state()` is the primary surface in every doc that grades or inspects an episode.**
  `serving.md` (the control-tools section is replaced by a state section), `testing.md` (the
  changeset example becomes a state example reading `inst.state()["state"]["db"]["log"]`),
  `concepts.md` (the changeset concept gains "the change log" and the document), the README's `grade(world_instance.changes())` example, and
  `reference/api.md`. `reference/cli.md` keeps `--include-control-tools` with one line saying it
  is deprecated.
- **A new `state.md`** in the bundled docs: the document (the envelope and `state`), both
  built-in formats and custom registration, presented as three cases in this order, each with its reason: `final_state` read
  once at the end uses `seahaven.state/1`; a per-step reader like OpenEnv's episode harness uses
  `seahaven.state+last_step/1`; a caller whose needs differ registers its own. Then: the fold and
  the two traps (overcounting, comparing logs), the value mapping, the lookup for the starting
  state (the fixture by world, version, id and hash; for a blank instance the DDL plus what the
  startup hooks wrote, which the format does not promise to make reproducible), the compatibility
  contract, and why nothing derivable is in the document.
- **`openenv.md`**, arriving from another branch, is updated to the new state surface once it
  lands; this project's docs phase owns that edit.
- **`index.md`** lists `state.md`. `authoring.md` documents `World(state_format=...)` as required
  and the reserved reset keyword.
- **Lint:** no new rule. SH101's entry in `reference/lints.md` gains one sentence: STRICT is also
  what keeps every row visible to the change log, because a STRICT table refuses `NULL` in a
  primary-key column and such a row would otherwise never be recorded.
- Every example in the docs runs under the docs test, as today.

## 13. Test plan

- **Log shape.** For each of insert, update (one column, several columns, a column set to
  `NULL`), delete, composite key, blob, integer key: the record matches §3.2 exactly. Update
  `before`/`after` never contain key columns. An insert and a delete carry whole rows.
- **Net per call, not across calls.** Insert then update in one call is one insert; in two calls
  it is an insert record then an update record. Update back to the original in one call is no
  record; across two calls it is two update records. Rolled-back call: no records. Read-only call:
  no records.
- **Order.** Two calls' records are in call order; within a call, sorted per §3.3; two identical
  episodes give byte-identical `json.dumps` output.
- **`i` and `call_count`.** A refused unknown tool and a `ToolError` each consume an ordinal. Tool
  listing and control tools do not. `bulk()` writes carry `i: null`. `call_count` matches.
- **The fold.** `fold(log) == inst.changes()` on every case above, plus a primary-key rewrite
  (delete plus insert in the log, delete plus insert in `changes()`).
- **Provenance.** Every envelope field against a fixture instance and a blank one; `startup`
  carries the keywords given and is `{}` otherwise; `fixture` is `null` for blank; the envelope is
  identical under a custom format.
- **Formats.** `seahaven.state+last_step/1` holds only the last call's records; the concatenation
  over an episode equals the `seahaven.state/1` log; before any call it is empty. A custom
  formatter registers, is selectable by `instance(state_format=)` and `reset(state_format=)`, and
  a name beginning with `seahaven.`, a duplicate, or a non-dict output is refused; its output
  lands under `state` with the envelope untouched.
- **Choosing.** `World(...)` without `state_format` raises with the built-in names in the message;
  an unknown `seahaven.` name raises at `World`, an unregistered custom name at `instance` and
  `reset`, both before any instance exists; a custom pin registered after `World(...)` works; a hook
  parameter named `state_format` is refused at registration; the scaffold writes the pin and the
  scaffolded world passes `seahaven check`.
- **OpenEnv.** In process: `state` before `reset` is §3.5, the envelope from the framework and
  `state` from the world's pinned formatter called with `None`; after `reset` it is the document; a
  second `reset` starts a new log. Against a real server over the WebSocket: the whole document
  arrives, `SeahavenClient.state().model_dump(exclude={"step_count"})` round-trips it, and
  `reset(state_format=...)`
  selects the format.
- **Performance.** `state()` on an instance with 1,000 log records does no database work
  (asserted through the authorizer or a statement counter) and completes within a stated bound;
  a call's cost with logging on is within a stated fraction of today's.
- **Deprecation.** Each control tool warns once per call, observed in process (the server's
  warning is raised in its own process and is not visible to a client).
- **Docs.** Every new example executes; no doc mentions `controller_` except `cli.md`'s one line.

## 14. Open questions

Numbered so they can be answered by number.

1. *Answered 2026-09-17:* `seed` is `int | None`; `bytes` is dropped from the API (§8).
2. *Answered 2026-09-17:* infinities render as `null` (§3.4).
3. *Answered 2026-09-17:* `episode_id` is the OpenEnv episode id over the wire and the instance id
   in process (§3.1); no separate `instance_id` field.
4. *Answered 2026-09-17:* `inst.change_log()` (§8).
5. *Answered 2026-09-17:* on the world, resolved against the instance's (root) world (§6).
6. *Decided 2026-09-17, from the architecture review:* the document is a framework-owned envelope
   plus `state`, the formatter's output, one level deep (§2, §3.1, §6, §9, §10). Custom formatters
   produce `state` only. Non-JSON-able startup keywords are refused at `reset` (architecture §6).
   The control tools' deprecation warning is tested in process only.

## 15. Non-goals

A net diff, a summary or counts in the document; an ignore list; an `indirect` flag; tool calls or
results in the document; a whole-database or whole-row format; a schema block or fingerprint;
filtering options on `state()` or a cap on the log; a judge language, a DSL, or a
loader/helper shipped by Seahaven; removing the control tools; composition itself (`subworld` is
reserved, always `null`); changing `inst.changes()`.
