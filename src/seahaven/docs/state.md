# The state document

An eval grades on what the episode left behind. `inst.state()` is where it reads it: one plain
dict, JSON-serialisable with the standard library, carrying both the provenance of the episode and
what the agent changed.

```python
import json

import projecttracker

with projecttracker.world.instance("small_startup", seed=7) as inst:
    issue = inst.call("get_issue", key="ENG-3")
    inst.call("transition_issue", issue_id=issue["id"], status="done")
    document = inst.state()

assert document["format"] == "seahaven.state/1"
assert document["world"]["name"] == "projecttracker"
assert document["call_count"] == 2
assert json.dumps(document)  # nothing in it needs an encoder of yours
```

Over OpenEnv the same document arrives on the `state` message; see [serving.md](serving.md).

**Read it between calls, never inside one.** `state()` raises `WorldBug` inside a transaction —
inside `inst.bulk()`, or inside a tool call — because the rows written there are not committed yet
and no document could describe them: the log would be missing rows the same block can already read
through `ctx.db`, with nothing to say so. Leave the block, or let the call return, and then read.
The same rule is why a formatter never runs in a transaction.

## The envelope and `state`

A document has two halves, and they have different owners.

```jsonc
{
  "format": "seahaven.state/1",
  "seahaven_version": "0.0.1",
  "world": {"name": "projecttracker", "version": "1.0.0"},
  "fixture": {"id": "small_startup", "file_sha256": "…"},   // null for a blank instance
  "episode_id": "…",
  "seed": 7,                                                 // as given; null if none
  "now": "2026-06-01T09:00:00.000Z",
  "startup": {"user_id": "u_12"},                            // {} when none were given
  "call_count": 2,
  "state": {"db": {"log": []}}                               // the formatter's output
}
```

Everything above `state` is the **envelope**. The framework writes it, and it is identical under
every format, built-in or your own.

| Field | What it is |
|---|---|
| `format` | The name of the format that produced `state`, always first. A reader checks this before reading `state`, and keys on nothing else for it |
| `seahaven_version` | `seahaven.__version__` of the producing process. Informational; never key on it |
| `world` | `{name, version}`. `world.version` is the world's contract: two documents with the same name and version were produced by the same schema and the same tools |
| `fixture` | `{id, file_sha256}` from the fixture's sidecar, or `null` for an instance built from the DDL |
| `episode_id` | Over OpenEnv, the session's episode id — the one `reset` was given, or the one it minted. In process an instance *is* an episode, so it is the instance id |
| `seed` | The `seed=` the caller gave: an integer, or `null`. Not the derived instance seed |
| `now` | The instance's frozen clock as an ISO-8601 instant — the same string `inst.clock.iso()` answers |
| `startup` | The reset keywords beyond `fixture`, `seed`, `now` and `state_format`, exactly as the startup hooks received them. An object, empty when there were none |
| `call_count` | How many calls have been dispatched. The last call's ordinal is one less. Over OpenEnv this is **not** `step_count`, which also counts tool listings |
| `state` | The formatter's output, and the only part of the document `format` describes |

`state` is the **format's**. A formatter answers the value of `state` and nothing else, so it can
neither omit provenance nor misspell it — that is the compatibility contract below made structural
rather than promised.

## Choosing a format

Three cases, in the order you are likely to meet them.

### Read it once at the end: `seahaven.state/1`

The usual case. A harness runs an episode, reads `state()` when it is over, and saves the document
as its `final_state`. `state` is `{"db": {"log": [...]}}` — the whole change log, every row the
episode changed.

```python
import projecttracker

with projecttracker.world.instance("small_startup") as inst:
    issue = inst.call("get_issue", key="ENG-3")
    inst.call("transition_issue", issue_id=issue["id"], status="done")
    log = inst.state()["state"]["db"]["log"]

assert [record["op"] for record in log if record["table"] == "issues"] == ["update"]
```

### Read it after every step: `seahaven.state+last_step/1`

A rollout harness that embeds the state in each step's record — OpenEnv's does — would save the
whole log once per step under `seahaven.state/1`, which is quadratic over an episode. This format
is the same envelope and the same `state` shape with one difference: `state.db.log` holds only the
records of the most recent call.

```python
import projecttracker

world = projecttracker.world

with world.instance("small_startup", state_format="seahaven.state+last_step/1") as inst:
    first = inst.call("get_issue", key="ENG-3")
    inst.call("transition_issue", issue_id=first["id"], status="done")
    after_the_first_write = inst.state()["state"]["db"]["log"]

    second = inst.call("get_issue", key="ENG-4")
    inst.call("transition_issue", issue_id=second["id"], status="canceled")
    after_the_second_write = inst.state()["state"]["db"]["log"]

assert {record["i"] for record in after_the_first_write} == {1}
assert {record["i"] for record in after_the_second_write} == {3}
```

It is scoped by the call counter and not by when `state()` was last read, so two reads between
calls answer the same document, and an episode's per-step documents concatenate into the whole
log. A step you skipped reading shows up as a jump in `call_count`. Writes made with no call in
flight — `inst.bulk()` — are never in it, and neither is anything before the first call.

The trade is the obvious one: a harness that keeps only the last document has only the last call's
changes. Keep every step's, or pin `seahaven.state/1`.

### Neither fits: register your own

A world may publish formats of its own, for a caller whose needs differ from the two built-ins.

```python
from typing import Any

import seahaven

world = seahaven.World(
    name="notes",
    version="1.0.0",
    schema="CREATE TABLE notes (id TEXT PRIMARY KEY, body TEXT NOT NULL) STRICT;",
    state_format="notes.touched/1",
)


@world.tool
def add_note(ctx: seahaven.Ctx, body: str) -> dict[str, str]:
    """Write a note down and return it."""
    note = {"id": ctx.ids.uuid(), "body": body}
    ctx.db.execute("INSERT INTO notes (id, body) VALUES (?, ?)", note["id"], note["body"])
    return note


@world.state_format("notes.touched/1")
def touched(world: seahaven.World, instance: seahaven.Instance | None) -> dict[str, Any]:
    """Which rows the episode touched, and nothing about how."""
    if instance is None:
        return {"touched": []}
    rows = {(record.table, str(record.key["id"])) for record in instance.change_log()}
    return {"touched": [{"table": table, "id": row_id} for table, row_id in sorted(rows)]}


with world.instance() as inst:
    inst.call("add_note", body="buy milk")
    document = inst.state()

assert document["format"] == "notes.touched/1"
assert [row["table"] for row in document["state"]["touched"]] == ["notes"]
assert document["seahaven_version"] == seahaven.__version__
```

The rules:

- The name is `<family>/<major>`: one `/`, then a positive integer. `seahaven.` is reserved for
  the framework's own formats.
- The function takes the world and the instance and returns a JSON-serialisable dict — **the value
  of `state`, and nothing else**. The framework writes the envelope around it.
- The instance is `None` for the state read before the first `reset` over OpenEnv. Decide what your
  format's "no episode yet" value is; a formatter that raises on `None` makes that read a
  `WorldBug`, which is your contract to keep, not the framework's.
- To publish a variation of a built-in, read one and edit it: `instance.state(format=
  "seahaven.state/1")["state"]` is a copy of the built-in's output, and the records in it are
  copies too.
- Registration is open for the life of the world, as it is for tools and middleware, and an
  extension may register one. A duplicate name, or a `seahaven.` name, is refused.
- A formatter runs under the instance lock and never inside a transaction. It reads; it does not
  write. One that writes raises `WorldBug`.
- A future composed world's log is one flat list with `subworld` filled in, so write a formatter
  that tolerates a `subworld` that is not always `null`. The built-ins do.

## Where the format is chosen

| Written | What it does |
|---|---|
| `World(state_format="seahaven.state/1")` | **required**; the world's pin, and what every instance of it answers in |
| `world.instance(..., state_format="…")` | overrides the pin for that instance alone |
| `reset(state_format="…")` | the same, over OpenEnv, for that episode |
| `inst.state(format="…")` | answers in another **registered** format for the same instance, without changing the instance's own. In process only: the OpenEnv `state` message carries no arguments |
| `inst.state_format` | the name this instance answers in, fixed for its life |

There is no default and no fallback. A `World` built without `state_format` raises, and the
message names the built-ins. `world.pinned_state_format` is the string it was given.

When a bad name is caught depends on what kind it is. A `seahaven.` name the framework does not
publish is refused at `World(...)`. A name of your own can only be checked when an instance is
made, because a world registers its formatters after its `World(...)` line has run — so
`World(state_format="acme.state/1")` is accepted and the first `instance` or `reset` fails if
nothing registered it by then. Either way the refusal comes before anything is copied; over
OpenEnv it surfaces as `EXECUTION_ERROR` and the session stays open for another `reset`.

The format is fixed for the life of an instance. Changing a world's pin changes what every eval
built on it saves, so bump `World.version` when you do — the docs recommend it; nothing enforces
it.

## The change log

Both built-in formats fill `state.db.log` with the same kind of record: one row changed by one
call.

```jsonc
{"i": 1, "subworld": null, "table": "issues", "op": "update",
 "key": {"id": "iss_3"},
 "before": {"status": "open", "updated_at": "2026-03-01T14:22:10.000Z"},
 "after":  {"status": "done", "updated_at": "2026-06-01T09:00:00.000Z"}}
```

| Field | What it is |
|---|---|
| `i` | The ordinal of the call that made the change, or `null` for a write made with no call in flight (`inst.bulk()`) |
| `subworld` | Always `null` in this release. The field exists so that a composed world's log stays one flat list |
| `table` | The table name |
| `op` | `"insert"`, `"update"` or `"delete"` |
| `key` | The row's primary key, as `{column: value}`, in primary-key column order |
| `before` | `null` for an insert. The whole row for a delete. For an update, exactly the non-key columns that call changed, with their old values |
| `after` | `null` for a delete. The whole row for an insert. For an update, the same columns as `before`, with their new values |

In process the same records are objects rather than dicts: `inst.change_log()` answers
`list[LogRecord]`, and `record.to_dict()` is the shape above. The list is yours; the records in it
are the instance's, so read them rather than editing them in place.

**A record is the net of its call.** A call is one transaction, and its records are the difference
between the instance before it and after it, per row: a write that leaves a value unchanged records
nothing, an insert followed by an update of the same row is one insert with the final values, a row
inserted and deleted in the same call leaves nothing, and a call that raised and rolled back leaves
nothing. A read-only call has no records.

**The log is not net across calls.** A row touched by two calls appears twice, once per call. That
is the source of both traps below.

Two more rules worth knowing:

- **The key is never repeated inside an update.** It is in `key`, which is where you join on it. An
  insert and a delete carry whole rows, key columns included, because that is the row.
- **A primary-key rewrite is a delete plus an insert**, never an update.

What is in the log is every world table except those a world named in `World(untracked_tables=...)`,
FTS5's shadow tables and virtual tables. Rows written by startup hooks are not in it: hooks run at
instance creation, before the first call opens a session, so what they wrote is starting state
rather than the agent's work. Nothing is capped or truncated — the log is as long as the episode
made it.

### Order

Records are ordered by call: every record of call 0 before every record of call 1. A record with
`i: null` sits where its transaction committed relative to the calls around it. Within one call,
records are sorted by `subworld` (`null` first), then `table`, then the key's values in
primary-key column order. Two identical episodes produce byte-identical logs.

## The fold, and the two traps

The net diff of a whole episode is the **fold** of its log. The format defines it so that any
reader, in any language, computes the same one:

1. Group records by `(subworld, table, key)` and fold each group in log order, starting from
   "untouched".
2. Compose. **Untouched + any record is that record**, whatever its `op` — a row the fixture
   already held opens its group with an `update` or a `delete`, not an insert. From there:
   insert + update = the insert with the update's `after` overlaid; insert + delete = untouched;
   update + update = an update with `before` from the first and `after` from the last, dropping any
   column whose folded `after` equals its folded `before`; update + delete = a delete whose
   `before` is the deleted row with the update's `before` values overlaid; delete + insert = an
   update if the rows differ (`before` the deleted row, `after` the inserted one, reduced to the
   differing non-key columns), else untouched.
3. Drop the groups that folded to untouched, and sort what is left as records are sorted within a
   call.

The framework does not ship this. It is a page of code wherever you grade, and Seahaven's own
suite carries one, checked against SQLite's cumulative changeset over the same episode.

**Trap one: counting straight off the log overcounts.** Two calls that touch the same row leave two
records, so a count of records is not a count of rows.

```python
import projecttracker

with projecttracker.world.instance("small_startup") as inst:
    issue = inst.call("get_issue", key="ENG-3")
    inst.call("update_issue", issue_id=issue["id"], title="First")
    inst.call("update_issue", issue_id=issue["id"], title="Second")
    log = inst.state()["state"]["db"]["log"]

updates = [record for record in log if record["table"] == "issues"]
assert len(updates) == 2
assert len({record["key"]["id"] for record in updates}) == 1  # one row, twice
```

Fold or deduplicate by `(table, key)` before you count.

**Trap two: two episodes with the same end state can have different logs.** One agent closes an
issue; another closes it, reopens it and closes it again. The end states match and the logs do not.
"Same end state" compares folds, never logs.

## Values

Every value in a record is a JSON value, mapped from SQLite's storage classes:

| SQLite | JSON |
|---|---|
| `INTEGER` | number. SQLite integers are 64-bit, so a JavaScript reader loses precision above 2^53. The format does not work around it |
| `REAL` | number, except that an infinity becomes `null` |
| `TEXT` | string |
| `BLOB` | string, base64 |
| `NULL` | `null` |

The infinity rule is worth stating plainly: JSON carries neither infinities nor NaN, SQLite itself
already stores NaN as `NULL`, and this is that coercion one step further. A reader cannot tell a
`NULL` column from an infinite one. Less precise, not wrong — and a column a world stores
infinities in is a column to grade on some other way.

## The state the episode started from

The document says what changed, not what the instance began with. That is deliberate (see below),
and the envelope carries the lookup instead.

For an instance made from a fixture, it is `world.name`, `world.version`, the fixture's `id` and
its `file_sha256` — four fields that name one immutable directory, hash included, so a reader
years later can open exactly the file the episode ran against.

```python
import projecttracker

with projecttracker.world.instance("small_startup") as inst:
    document = inst.state()

assert document["fixture"]["id"] == "small_startup"
assert len(document["fixture"]["file_sha256"]) == 64

with projecttracker.world.instance() as blank:
    assert blank.state()["fixture"] is None  # built from the DDL
```

For a blank instance, `fixture` is `null` and the starting state is the world's DDL plus whatever
the startup hooks wrote, which `startup` records the keywords for. **The format does not promise
that is reproducible.** A hook is ordinary world code and may read anything; a blank instance's
clock defaults to wall time unless `now=` was given. Grade against a frozen fixture when the
starting state has to be pinned down.

## The compatibility contract

Two contracts, one per half of the document.

**The envelope is the framework's.** A field is only ever added — never removed, renamed, retyped
or rebuilt. Key on a field's presence, and on `seahaven_version` for information only.

**`state` is the format's**, and `format` is the only thing to key on for it. Within a published
format: no field is removed or renamed, none changes type, and the construction of an existing
field does not change even where its type would not — so a reader written against
`seahaven.state/1` on the day it shipped reads every `seahaven.state/1` document ever produced. A
field may be added only if a reader that ignores it loses nothing.

**So: ignore fields you do not know**, in the envelope and in `state`. Anything the rules above
forbid is a new major — `seahaven.state/2`, a new formatter — and the old one stays callable and
frozen. Removing a published format would be a breaking Seahaven release, and none is planned.

## Why nothing derivable is in the document

No fold, no counts, no end-state rows, no diff against the fixture. Each of those is computable
from what is already there, and every one of them would be a second thing to keep true: a field a
future release could get subtly wrong while the log beside it stayed right, and a reader with two
sources to reconcile. The log and the provenance are the primitives; a grader computes what it
needs from them and the framework does not guess which.

That is also why the document costs no database work to produce. Each call's records were rendered
when it committed, so `state()` is serialisation and nothing else, however long the episode ran.

The recording is not free at the other end, though — a session is opened and rendered per call.
Seahaven's own probe is `uv run python -m bench recording --calls 400 --repeats 5` — quote the
flags with any number from it, because a shorter pass is dominated by its own noise. On one Linux
machine that command put the cost at about **+40%** on a write-heavy call (+32% to +49% across
runs) and about **+10%** on a read-only one (+3% to +14%), measured against the single long-lived
session the framework kept before the change log existed. Those are one machine's numbers and not a
property of the framework; run the probe on yours before planning around them. `bench/README.md` in
the Seahaven repository has the caveats that go with it.
