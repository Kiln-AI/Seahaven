# Component: Phase 4, docs (9.M1, 9.M7, 9.M8, 8.M4, 2.M2/4.M3, 9.M6)

Line numbers are at `38c6f3c`; phases 1-3 move some of them, so find the quoted text. Docs paths are under `src/seahaven/docs/`. Wrap all prose at 100
columns when applying. Nothing below renames a heading, so no anchor or `tests/test_docs.py` change.

## 1. 9.M1: SH103's stated reason

**Verified** on a fixed clock at `2026-06-01T09:00:00.000Z`, with the clock functions registered
as they are (`SQLITE_DETERMINISTIC | SQLITE_INNOCUOUS`; 3.M1, removing the flag, is won't-fix):
- `DEFAULT CURRENT_TIMESTAMP`, `DEFAULT (datetime('now'))`, a trigger's `datetime('now')` and a
  view's `datetime('now')` all give `2026-06-01T09:00:00.000Z`: canonical text.
- `DEFAULT CURRENT_DATE` gives `2026-06-01`; `DEFAULT (julianday('now'))` gives `2461192.875`.
  Not a timestamp in the canonical format.
- `'now'` in a generated column, an index expression or a partial-index `WHERE` is accepted,
  because the overrides are registered deterministic. The stored or indexed value is computed once
  and goes stale as the clock moves.
- A plain SQLite connection (Python `sqlite3`) on the same schema writes host wall-clock time in
  SQLite's format (`2026-09-29 19:44:50`). The overrides exist only on connections Seahaven opens
  (`db.open_instance`, `open_inspection`, `_build`).

**What SH103 is for:**
(a) `CURRENT_DATE`, `CURRENT_TIME`, `date/time/julianday/unixepoch('now')` write a date, a time or a
number. (b) A trigger that stamps a time overwrites the timestamps a fixture generator writes in
`bulk()` with the instance's clock, so loaded history is lost. (c) `'now'` in a generated column,
an index or a `CHECK` is evaluated once and not again as the clock moves. (d) A default, trigger or
view is part of the file, and any connection Seahaven did not open (a grader using `sqlite3`, the
`sqlite3` shell) evaluates it with the host's wall clock in SQLite's format. (e) One rule: world
code owns every timestamp.

**Edit 1a. `db_schema_and_fixtures.md:79-82`.** Current: "one built from `CURRENT_TIMESTAMP` reads
the instance's clock. The wall-clock rule below is unchanged and still refuses `CURRENT_TIMESTAMP`
inside a `CREATE` — a column default, a trigger body — so that one timestamp format is written
everywhere; what is sanctioned here is the `INSERT` the file runs itself." Replace from "The
wall-clock rule" with (drops "unchanged and still", a history phrase, and the false reason):
> The wall-clock rule below covers the objects a schema file creates, such as a column default or a
> trigger body. It does not cover the `INSERT` statements the file runs.

(True: SH103 reads `sqlite_master` only, `lint/ddl.py:166-198`.)

**Edit 1b. `db_schema_and_fixtures.md:98-103`.** Current: "…The problem is the text as much as the
instant: SQLite writes `2026-06-01 09:00:00`, and the rest of the world writes
`2026-06-01T09:00:00.000Z`. Two formats in one column make a comparison fail for a reason nobody
finds quickly. Declare the column…". Replace the three sentences from "The problem is" with:
> The tool that makes a row decides its time. A trigger that stamps `updated_at` overwrites the
> history a fixture generator writes, and `CURRENT_DATE` or `julianday('now')` writes a date or a
> number where every other timestamp is `2026-06-01T09:00:00.000Z`.

**Edit 1c. `reference/lints.md:94-98` (the SH103 "Why").** Current: "The problem is the text rather
than the instant. An instance's clock overrides make a default in the schema read the instance's
clock anyway, but what SQLite *writes* is `2026-06-01 09:00:00`, and every other door of a world
writes `2026-06-01T09:00:00.000Z`. One format across every door is the rule, and two formats in one
column is a comparison that fails for a reason nobody will find quickly." Replace with:
> **Why.** On Seahaven's own connections, `CURRENT_TIMESTAMP` and `datetime('now')` read the
> instance's clock and write canonical text. The rest of the family does not: `CURRENT_DATE`,
> `CURRENT_TIME`, `date('now')`, `julianday('now')` and `unixepoch('now')` write a date, a time or a
> number. A trigger that stamps a time also overwrites the timestamps a fixture generator writes in
> `inst.bulk()`. And a schema object is part of the database file: a connection Seahaven did not
> open, such as a grader reading `state.sqlite` with Python's `sqlite3`, evaluates it with SQLite's
> own functions, which read the host's wall clock and write `2026-06-01 09:00:00`. In a generated
> column, an index or a `CHECK`, the time is read once and the stored value does not move with the
> clock. A timestamp that world code writes from `ctx.clock.iso()` has none of these problems.

Keep the "Rule", "Fix" and false-positive paragraphs (`:90-92`, `:100-106`) unchanged. The Fix
text is the lint's own message (`lint/ddl.py:193-196`), so do not reword it here.

**Edit 1d. `reference/lints.md:90-92` (the SH103 "Rule").** The regex already searches the whole
schema; name what it covers: "…in a column default, a trigger body, a view, a generated column, a
`CHECK` constraint, an index expression or a partial index."

## 2. 9.M7: `composition.md:717-721`, the benchmark sentence

Current (`:716-721`): "**An idle composite instance is one file and one connection per node.** A
session is opened per node per call and closed with the call, so an idle instance holds none. Cost
scales with the tree, and the design target — hundreds of concurrent instances with minute-long
lifetimes — holds for a small number of nodes. The benchmark measures one node per instance and
reads as a per-node floor."

What `bench/` measures now: `uv run python -m bench composite` (`bench/composite.py`) stands up the
1-, 2- and 4-node trees of `tests/worlds/` and reports open/destroy time, disk per node, and
one-row read and write per call at 1 and 4 nodes (`bench/results/latest.md` §7: about 3 ms and
44 KiB per added node, call overhead 1-5% for 3 added nodes). Bench only runs over the repo's own
worlds, so it is no help to an author, and figures are out under 9.M6. Replace the last two
sentences with:
> Cost scales with the number of nodes: each node adds a file and a connection when an instance
> is made, and a session on every call.

(Verified: `NodeRuntime` per node, `state.md:563` says a session per node per call.)

## 3. 9.M8: `projecttracker.md:208-211`

Current fence: `uv run pytest worlds/projecttracker` / `uv run seahaven check`. From the repo root
`uv run seahaven check` exits 1 with `SH501 ... seahaven has a module and not a World for its
'world'` (reproduced). Replacement, both run from the repo root and verified clean (pytest exit 0,
check exit 0; `cd worlds/projecttracker && uv run seahaven check` is also clean):
```sh
uv run pytest worlds/projecttracker
uv run seahaven check --world projecttracker:world
```
Optionally add above the fence: "Run both from the root of the Seahaven repository."

## 4. 8.M4: audit trail and `actor_id` claims (ProjectTracker)

**Facts (`worlds/projecttracker/src/projecttracker/tools/`).** 13 write tools. Five take `actor_id`
and resolve an actor (`_rows.resolve_actor`): `create_issue`, `update_issue`, `assign_issue`,
`transition_issue`, `add_comment`. Eight take none: `create_user`, `create_team`,
`add_team_member`, `create_project`, `update_project`, `create_label`, `set_issue_labels`,
`archive_issue`. Event kinds are `created`, `status`, `assignee`, `comment` (`_types.py:57`).
Rows are written by `create_issue` (`created`), `add_comment` (`comment`), and `_apply` for
`update_issue`/`assign_issue`/`transition_issue` (`status` and `assignee` changes only). No event
for: `title`, `description`, `priority`, `due_at` changes, `set_issue_labels`, `archive_issue`.

| Where | Current | Replacement |
|---|---|---|
| `worlds/projecttracker/README.md:6-7` | "issues carry labels and comments, and every write to an issue appends to an audit trail." | "issues carry labels and comments, and an audit trail records who created each issue, changed its status or assignee, or commented on it." |
| `src/seahaven/docs/projecttracker.md:63-64` | same sentence as the README | same replacement |
| `worlds/projecttracker/AGENTS.md:37-39` | "Every write takes an optional `actor_id`, which wins, and a write with neither is `InvalidInput("actor_id", "no actor")` — which is…" | "The five issue writes the trail records (`create_issue`, `update_issue`, `assign_issue`, `transition_issue` and `add_comment`) take an optional `actor_id`, which wins, and one of them with neither is `InvalidInput("actor_id", "no actor")` — which is…" (keep the rest) |
| `src/seahaven/docs/projecttracker.md:173-175` | "Every write takes an optional `actor_id` which wins, so one run can have two people writing without two instances. A write with neither, …, is `INVALID_INPUT`." | "The five issue writes the audit trail records take an optional `actor_id`, which wins, so one run can have two people writing without two instances. One of them with neither, …, is `INVALID_INPUT`." |
| `worlds/projecttracker/src/projecttracker/tools/_events.py:3-4` | "Every write to an issue leaves a row here -- created, status changed, assignee changed, comment added -- and every one of them goes through `record_event`." | "Creating an issue, changing its status or its assignee, and adding a comment each leave a row here, and every one of them goes through `record_event`. Other field changes, labels and archiving leave none." |

Add to AGENTS.md (the world's product-rules list, next to the viewer bullet) one line an eval
author needs: "The trail does not record `title`, `description`, `priority` or `due_at` changes,
labels, or archiving; grade those on the row." Do not touch the closed-issue wording (`dc29e4b`).

**Leave `schema/001_core.sql:105`** ("The audit trail every write to an issue appends to") as it is:
`world._schema_hash` collapses whitespace only, so a comment edit changes `schema_hash` and every
fixture then fails SH403 (decided; no fixture regeneration in this project).

## 5. 2.M2 / 4.M3: `bulk()` is not atomic across nodes when a commit fails (accepted)

**Real behaviour (reproduced: root 0 rows, child row kept and in the change log, `DbError`
raised).** `_bulk` enters one transaction per node
in `_runtime` order (root first) on an `ExitStack`, so they exit in reverse: added nodes commit
first, the root last. If the block raises, every node rolls back. If a commit fails (a
`DEFERRABLE INITIALLY DEFERRED` foreign key, a full disk), `bulk()` raises; that node and the nodes
not yet committed roll back; nodes already committed keep their rows.

Every place that makes or implies the claim:
- **`src/seahaven/instances.py:598-600`** (`bulk()` docstring): "Every node's transaction is
  committed on the way out, and all of them are rolled back together if the block raised." This
  sentence is true as written (raising rolls back all), but reads as all-or-nothing. Replace with:
  "If the block raises, every node's transaction is rolled back. Otherwise they commit one at a
  time, the root last, and a commit that fails leaves the nodes committed before it committed."
- **`src/seahaven/instances.py:921-923`** (comment in `_bulk`): "Every node's transaction open
  before the block runs and committed in sequence on the way out, so a bulk write that reaches two
  stores through `ctx.worlds` either lands in both or in neither." False. Replace with a constraint
  comment: "One transaction per node, because each node is its own file and SQLite cannot commit
  two files atomically. The stack commits them in reverse, the root last: a block that raises rolls
  every node back, but a commit that fails leaves the nodes before it committed."
- **`composition.md:489-490`**: "`inst.bulk()` yields the root's context with a live `ctx.worlds`,
  and opens one transaction per node and commits them in sequence, so a fixture generator fills
  every store in one block." Keep, then add:
  > If the block raises, every node rolls back. If a node's commit fails, for example on a deferred
  > foreign key, `bulk()` raises and the nodes that committed before it keep their rows. Do not
  > freeze an instance after `bulk()` raised; build it again.
- **`reference/api.md:198`**: "a context manager yielding the instance's own `Ctx`, in one
  transaction, for loading rows fast". Wrong for a composite. Replace: "a context manager yielding
  the root node's `Ctx`, with one transaction per node, for loading rows fast. See
  [composition.md](../composition.md#one-instance-many-stores) for a composite".
- `db_schema_and_fixtures.md:204-205` ("in one transaction per node") and `composition.md:323-326`
  (per-call "no cross-world atomicity") are accurate; no change.

## 6. 9.M6: history, bench figures, version stories, spec citations

Scope: every `*.md` under `src/seahaven/docs/` except `http_apis.md`. Grep terms from the brief plus
`§`, `as before`, `always`, `carried`, `meanwhile`, `design target`, `rc2`, `sign-off`. Hits for
"was"/"were" and `version` in examples were read and are ordinary present-tense uses; not listed.

| # | file:line | Quote | Action |
|---|---|---|---|
| H1 | state.md:563-566 | "The Seahaven repository carries a probe that measures what that costs, under `bench/`. Run the probe with `uv run python -m bench recording ...`" | Delete the bench sentences; keep the first sentence ("Recording is not free … when the call commits.") |
| H2 | state.md:568-569 | "The figures below are approximate. … Read the figures as the size of the cost, not as the cost." | Delete |
| H3 | state.md:571-577 | "costs about 47% more than the single long-lived session per node that Seahaven kept before this release … used to do once an episode … between about 5% and 18%" | Delete. Keep one present fact: "A read-only call records nothing, so it costs less than a write." |
| H4 | state.md:579-582 | "The four-node `emporium` world … 45% to 67% … does not grow in proportion to the node count" | Delete (node cost is covered by §2 above) |
| H5 | serving_and_openenv.md:91-95 | "If the command fails on a release candidate of CPython 3.14 … 3.14.0rc2 … pydantic … `beartype` … 3.14.0 final" | Delete; `:44-45` already says "a final CPython 3.14 or newer, not a release candidate" |
| H6 | serving_and_openenv.md:502-507 | "In the framework's own benchmark, with five threads … 12,874 at a gate of 1, and 5,351 and 4,011 at 2 and 4 … a server with 500 sessions and a gate of 16 is the ordinary case" | Rewrite as fact: "Under load, one session can be served once while another is served thousands of times. Every gate size that binds behaves this way." |
| H7 | serving_and_openenv.md:511-512 | "The fix is a gate that hands slots out in arrival order, not a different number." | Delete (roadmap) |
| H8 | serving_and_openenv.md:514-520 | "What to do meanwhile … `--concurrency 0` was the one setting measured … The measurements … are in `bench/results/latest.md` … no number from them should be quoted" | Rewrite: "If a saturated workload cares about its slowest session, run with `--concurrency 0`. Without the gate every session gets an even share, and median and 95th-percentile latency go up." (`--concurrency` at `cli/serve.py:40`) |
| H9 | serving_and_openenv.md:547-549 | "Seahaven carried a client-side close handshake and an ASGI middleware to get that under openenv 0.4.2, and carries neither now, because openenv 0.5 logs no error for a normal close." | Delete. `:7` already pins OpenEnv v0.5.x |
| H10 | serving_and_openenv.md:594-595 | "Seahaven's own reference world is not published anywhere. That step is gated on a maintainer's sign-off and has not happened." | Delete (maintainer note; AGENTS.md already holds the rule) |
| H11 | composition.md:91-92 | "gains a node dimension it did not have before." | Rewrite: "gains a node dimension." |
| H12 | composition.md:470 | "The root's file is `state.sqlite`, as it has always been, and" | Delete ", as it has always been" |
| H13 | composition.md:482-483 | "The root's stream is the instance seed untouched, which is why a world that adds nothing mints exactly what it always did." | Delete ", which is why … always did"; keep "The root's stream is the instance seed untouched." |
| H14 | composition.md:495 | "It carries the version-1 fields describing the root exactly as before, plus a `nodes` list" | Rewrite: "It carries the version-1 fields, which describe the root, and a `nodes` list" |
| H15 | composition.md:521-523 | "so its fixture directory is byte for byte what it was before composition existed, and every fixture already on disk keeps loading." | Rewrite: "A world that adds nothing writes `format_version: 1` with no `nodes` key, and Seahaven reads both versions." (`fixtures.py:153` `Literal[1, 2]`) |
| H16 | composition.md:589 | "is the read-only connection it always was, with every added node attached" | Rewrite: "is the eval's read-only connection, with every added node attached" |
| H17 | composition.md:592 | "Writes, `ATTACH` and `DETACH` are denied on it, as they always were." | Delete ", as they always were" |
| H18 | composition.md:718-721 | "the design target — hundreds of concurrent instances … The benchmark measures one node per instance" | See §2 |
| H19 | reference/api.md:29-44 | "Ten names … `components/world_and_dispatch.md` §1 … `architecture.md` §1 … declares them public on its own authority … That goes further than the specification" | Rewrite, no specs: "Some names on this page are not exported from `seahaven/__init__.py`. They are public all the same: `seahaven.world.Handler`, `Middleware` and `StartupHook`; `seahaven.fixtures.load`, `load_all`, `verify` and `freeze`; and the concurrency gate, `seahaven.instances.default_concurrency`, `concurrency` and `set_concurrency`." Drop the count "Ten": 9.Mild notes `NodeReport`, `Exec` and the `seahaven.openenv` names are also on the page and not root exports, so say "for example" or list them too |
| H20 | reference/api.md:318-319 | "Two rare cases are exceptions: see the [clock modes risk report](…/specs/projects/clock_modes/risk_report.md)." | Rewrite with the cases (risk report R4, R6; strict xfails `tests/test_clock.py:642,654`): "Two rare cases take another statement's reading: a statement whose text starts with a `-- ` comment, and a statement whose rows are read while another statement runs on the same connection. Neither happens under `fixed`, and under `tick` only on `inst.inspect()` and the control tool." |
| H21 | db_schema_and_fixtures.md:190-191 | "A fixture frozen under an id this rule now refuses still appears in `world.fixtures()` but no longer opens." | Rewrite present tense: "A fixture directory whose id breaks this rule appears in `world.fixtures()` and does not open." |
| H22 | db_schema_and_fixtures.md:326-327 | "Nothing migrates a fixture that has already shipped. Version 1 of Seahaven has no migration path at all, which is why the generator is committed." | Rewrite: "Nothing migrates a fixture, which is why the generator is committed." |
| H23 | db_schema_and_fixtures.md:80-81 | "The wall-clock rule below is unchanged and still refuses" | See §1a |
| H24 | projecttracker.md:198 | "`agency` is also the fixture the framework's benchmark runs against." | Delete (repo-internal; nothing for an author) |

Kept on purpose (current fact, not history): the PyPI placeholder warnings (`authoring.md:52`,
`serving_and_openenv.md:47-51`, `reference/cli.md:77-81`), "does not build a working image today"
(`serving_and_openenv.md:573-577`), OpenEnv "(v0.5.x)" and "needs openenv 0.5 or newer"
(`serving_and_openenv.md:7`, `:468`), "`seahaven_version": "0.0.1"` (`state.md:47`, an example),
"none is planned" (`state.md:547`), "Deliberately absent, and not planned" (`composition.md:730`).

**AGENTS.md banned words.** A grep over the same pages for "load bearing"/"load-bearing", "seam(s)",
"the whole point", "surface" as a verb (surfaces/surfaced/surfacing/to surface) and "lives in" finds
**no hits**. "surface" appears only as a noun (`composition.md:124`, `reference/api.md:138`). Near
misses in the same register, for the writer's judgement: `composition.md:90` "That example is the
whole feature." (aphoristic opener; delete), `serving_and_openenv.md:599-600` "which is the whole
integration" (rewrite: "Point Kiln at a served world and it drives the sessions."),
`extensions.md:126` "whole contract", `projecttracker.md:58` "That is the whole reason".

## Decisions

- 3.M1 is won't-fix, so SH103's rationale is written for the clock functions as they are (§1), and
  the rule list names every place the regex already covers (Edit 1d).
- `schema/001_core.sql` is not edited (§4).
- H19: the concurrency gate stays on the public API page; only the spec citations go.
