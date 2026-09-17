# Lint reference

`seahaven check` runs every rule below over the world found by the convention (the project's
package, attribute `world`; `--world module:attr` overrides it), prints each finding as one line,
and exits 1 if any finding is an `error`. Warnings alone exit 0.

```
SH101 error src/pkg/schema/001_items.sql:8  table 'items' is not STRICT  fix: append STRICT to the CREATE TABLE items
```

Five fields: the code, the severity, where, what, and the edit to make. **Every finding names the
edit** rather than describing the problem, because every rule here exists for a mistake that is
otherwise made silently.

There is no autofix, no configuration file and no suppression comment. The rule set is deliberately
small; a rule an author has to argue with is a rule an author turns off.

Codes are stable and their gaps are deliberate. A retired rule's number is never reused, so a fix
written against `SH203` in a world's history always means the same rule.

## The codes

| Code | Severity | Rule |
|---|---|---|
| SH101 | error | a table that is not `STRICT` |
| SH102 | error | a table with no explicit primary key |
| SH103 | error | a wall-clock expression anywhere in the DDL |
| SH104 | error | DDL that does not execute |
| SH201 | warning | a wall-clock call in world code, outside `middleware/` |
| SH203 | warning | `random` or `uuid.uuid4()` in world code |
| SH205 | warning | a tool with an empty description |
| SH301 | error | a module under `tools/` or `middleware/` that is never imported |
| SH401 | error | a fixture sidecar that does not validate |
| SH402 | error | a fixture's `file_sha256` does not match its state file |
| SH403 | error | a fixture's `schema_hash` does not match the world |
| SH404 | error | a fixture's `now` is not canonical |
| SH405 | error | a fixture's state file has `-wal` or `-shm` companions |
| SH501 | error | the package does not export a `World` named `world` |

The schema rules are asked of a real database rather than of the text: the DDL is built in memory
and interrogated with `PRAGMA table_list`, `PRAGMA table_info` and `sqlite_master`, so a `STRICT`
SQLite did not accept as one, or a primary key spelled in a way nobody thought of, is answered by
the engine. Line numbers go the other way — SQLite keeps a normalised copy of the DDL and knows
nothing about files — so the `*.sql` files are searched for the `CREATE ...` that names the object.
A world whose schema is not on disk still gets every finding, with the package directory and no
line.

---

## SH101 — a table that is not `STRICT`

**Rule.** Every ordinary table in the world's schema is declared `STRICT`. FTS5 virtual tables and
their shadow tables are exempt; a virtual table cannot be `STRICT`.

**Why.** Without `STRICT`, SQLite stores whatever it is handed: a string in an `INTEGER` column, a
float in a `TEXT` one. A world is a mock of a real product's data, and an eval that grades on a
column's value needs the column to hold what the schema says. The failure without this rule is
silent and arrives weeks later as a comparison that does not match. STRICT is also what keeps every
row visible to the change log: a STRICT table refuses `NULL` in a primary-key column, and a row with
a `NULL` key is one SQLite's session extension never records — so without this rule a write could
be missing from a graded document with nothing to say so.

**Fix.** `append STRICT to the CREATE TABLE <name>`. Note that a `STRICT` table's columns must each
be declared with one of the types SQLite allows there — `INT`, `INTEGER`, `REAL`, `TEXT`, `BLOB` or
`ANY` — so a column written `VARCHAR(64)` or `BOOLEAN` has to be spelled `TEXT` or `INTEGER`.

## SH102 — a table with no explicit primary key

**Rule.** Every ordinary table declares a `PRIMARY KEY`, column-level or table-level. SQLite's
implicit `rowid` does not count.

**Why.** Two reasons, and the second is the hard one. A row with no key cannot be identified in a
`LogRecord`, so **a table without a primary key cannot be tracked in the change log** — the
framework refuses to attach one, and every write to that table is invisible to the eval grading the
run. And a list ordered without a unique tiebreak is not deterministic, which is the other thing
this framework is for.

**Fix.** `give <name> a PRIMARY KEY; SQLite's implicit rowid is not one`. A join table takes a
composite key: `PRIMARY KEY (issue_id, label_id)`.

## SH103 — a wall-clock expression anywhere in the DDL

**Rule.** No `CURRENT_TIMESTAMP`, `CURRENT_DATE`, `CURRENT_TIME`, `datetime('now')` or any relative
of them, in a column default, a trigger body, a view, a generated column or a partial index. The
whole schema is searched through `sqlite_master`, with comments and string literals excluded.

**Why.** Not the instant — an instance's clock overrides make a DDL default read the frozen one
anyway — but **the text**. What SQLite writes is `2026-06-01 09:00:00`, and every other door of a
world writes `2026-06-01T09:00:00.000Z`. One format across every door is the rule, and two formats
in one column is a comparison that fails for a reason nobody will find quickly.

**Fix.** `write the timestamp from world code with ctx.clock.iso(), so every door of the world uses
one format`. Make the column `NOT NULL` with no default and pass the value in.

One known false positive: `'now'` is matched as text, so a `CHECK` constraint that allows the
literal string `'now'` is reported. `'now'` is a time value to every date function SQLite has and
there is no way to tell the two apart in `sqlite_master.sql`; a world with a status called `now` is
rarer than a world with `datetime('now')` in a trigger.

## SH104 — DDL that does not execute

**Rule.** The whole schema, applied in filename order to a blank database, runs. The message is
SQLite's own.

**Why.** A world whose DDL does not execute cannot be constructed at all — `World.__init__` builds
the schema in memory to compute the schema hash. This rule exists so that `seahaven check` answers
with a line and a fix rather than a traceback out of an import, which is what an authoring agent can
act on.

**Fix.** `fix the statement SQLite names; every *.sql file under schema/ is run in filename order,
against a blank database`. Remember the order is *filename* order, so a table referenced by a
foreign key in `001_` and created in `002_` is a common cause.

## SH201 — a wall-clock call in world code, outside `middleware/`

**Rule.** `datetime.now`, `datetime.utcnow`, `date.today`, `time.time`, `time.monotonic` and
`time.perf_counter`, anywhere under the world's package except `middleware/`. The call's dotted name
is resolved through the module's own imports, so `datetime.now()`, `datetime.datetime.now()` and
`from datetime import datetime as dt; dt.now()` are one rule and not three.

**Why.** An instance's time is its fixture's, everywhere. A timestamp from the machine's clock in
fixture-relative data is nearly always a mistake: the row is dated years after everything around it,
and the run does not replay.

**A warning and not an error**, because it is sometimes deliberate — and `middleware/` is exempt
outright, since a real clock timing a real call in a logging layer is exactly right.

**Fix.** `take the instance's time from ctx.clock.iso() or ctx.clock.now()`.

## SH203 — `random` or `uuid.uuid4()` in world code

**Rule.** Importing the standard library's `random`, calling anything on it, or calling
`uuid.uuid4()` or `uuid.uuid1()`. Importing `uuid` on its own is fine: `uuid.UUID` is how
`ctx.ids.uuid()`'s own output is parsed.

**Why.** Both read a source of entropy that is not the instance's, so the same fixture and seed stop
replaying — which is the one guarantee a world gets for free and the one an eval author relies on
when comparing two runs.

**Fix.** `draw from ctx.ids: ctx.ids.uuid() for an identifier, ctx.ids.random for anything else`.
`ctx.ids.random` is a real `random.Random`, so `choice`, `shuffle` and `randint` all work — seeded
per instance.

## SH205 — a tool with an empty description

**Rule.** A registered tool whose description is empty or whitespace. Asked of the `World` rather
than of the source, because a description is a docstring *or* a `description=` *or* whatever a
factory put on the `Tool`, and only the registry knows which. The framework's own control tool is
exempt: it is never listed and never reaches an agent.

**Why.** The description is the whole of what an agent reads to decide whether to call the tool. A
tool without one is a tool that will not be called, or will be called wrongly.

**Fix.** `give the function a docstring, which becomes the whole description, or pass description=
to @world.tool`.

**A warning**, because a world under development has tools that do not have one yet.

## SH301 — a module under `tools/` or `middleware/` that is never imported

**Rule.** Every module under those two directories is in `sys.modules` after the world's package has
been imported. The two directories and not the whole package: a world may put helpers anywhere, and
these are the two the layout gives a meaning to.

**Why.** Registration happens at import. A tool module nobody imports registers nothing, and the
world silently has one tool fewer than its author believes. The symptom — an `unknown_tool` from an
eval, weeks later — says nothing about the missing import line, which is why this is an error.

**Fix.** `import it from <package>/__init__.py, or from <package>.tools`. A module that is genuinely
shared and registers nothing belongs beside the tool modules with a leading underscore *and* an
import from the module that uses it, which is what puts it in `sys.modules`.

## SH401 — a fixture sidecar that does not validate

**Rule.** `fixture.yaml` parses as YAML, carries `format_version: 1`, and validates against the
sidecar model. Checked first and alone: the other fixture rules read fields a broken sidecar does not
have, so a fixture that fails this is reported once and left.

**Why.** The sidecar is what says where the state came from. Without it, nothing can say whether the
file matches the world.

**Fix.** `regenerate it with seahaven fixture freeze or seahaven fixture fork`. A sidecar is never
hand-edited: everything in it is derived from the instance that was frozen.

## SH402 — a fixture's `file_sha256` does not match its state file

**Rule.** `state.sqlite` exists and hashes to what the sidecar says.

**Why.** A fixture is immutable, and this is how that is enforced against the file rather than
against a convention. A mismatch means the file was changed after it was frozen — usually by opening
it and writing to it — and every eval that used it since started from state nobody meant. The
framework makes the same check the first time it copies a fixture in a process; this rule finds it
before a commit rather than in a run.

**Fix.** `fixtures are immutable: fork it, change the fork, and freeze that`. If the file is simply
missing, the fix is to regenerate it.

## SH403 — a fixture's `schema_hash` does not match the world

**Rule.** The hash of the normalised DDL in the sidecar equals the loaded world's.

**Why.** The fixture was frozen from a different schema, so a tool of the current world may query a
column it does not have. Instance creation refuses such a fixture outright; this is the same check,
before a commit.

**Fix.** `regenerate it with seahaven fixture freeze or seahaven fixture fork`. **Every** fixture of
the world, in parent order, because they all conform to one schema. This is the cost a schema change
carries, and it is why the generator script is committed.

## SH404 — a fixture's `now` is not canonical

**Rule.** `now` round-trips through the clock's own formatter unchanged: UTC, milliseconds, trailing
`Z` — `2026-06-01T09:00:00.000Z`.

**Why.** Every instance of the fixture takes its clock from this string, and every timestamp
comparison in the world is a text comparison. A value in another format sorts wrongly against every
row in the fixture.

**Fix.** `canonical is 2026-06-01T09:00:00.000Z: UTC, milliseconds, trailing Z`. In practice: pass
`--now` in that format to `seahaven fixture freeze`.

## SH405 — a fixture's state file has `-wal` or `-shm` companions

**Rule.** No `state.sqlite-wal` or `state.sqlite-shm` beside the state file.

**Why.** A fixture is checkpointed and vacuumed before it is sealed, so either file means the
database was opened for writing after it was frozen — and whatever the fixture's hash covers, it does
not cover what is in them.

**Fix.** `regenerate it with seahaven fixture freeze or seahaven fixture fork`.

**Not the file mode.** `freeze` does seal `state.sqlite` at `0444`, and this rule does not check it:
git records only the executable bit, so every committed fixture comes back from a clone at `0644`
and a mode check would fire on every correct world after every clone. What the seal guards against
is the file changing, and that is SH402, over a hash version control does preserve. Sealing the file
is still what stops a live instance writing a fixture in place, so `freeze` still does it; it is
only the *check* that cannot ask.

## SH501 — the package does not export a `World` named `world`

**Rule.** The project's package — from `[project] name` in the nearest `pyproject.toml`, normalised
— imports, and has an attribute `world` that is a `seahaven.World`. This is the one finding
discovery raises rather than a rule module: there is no world to lint.

**Why.** It is the convention every part of the tooling relies on: the CLI, the pytest plugin and
`serve` all find a world this way. A package that does not follow it works in process and nowhere
else.

**Fix.** `give the package a world, or point at the one it has`. The message names the module and
the attribute it looked for, and `--world module:attr` is the override for a layout the convention
misses.

An import that fails for another reason is reported here too, with the exception's own last line:
what `check` will not do is answer with a traceback.
