# Lint reference

`seahaven check` runs every rule below over the world found by the functional spec's §2.2
convention, prints each finding as

```
SH101 error src/pkg/schema/001_items.sql:12  table 'issues' is not STRICT  fix: append STRICT to the CREATE TABLE
```

and exits 1 if any finding is an `error`. Warnings alone exit 0. There is no autofix, no
configuration file and no suppression comment: the rule set is deliberately small, and every rule
here exists because it is a mistake an authoring agent makes silently.

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

**The why and the fix for each code are written in the documentation phase.** Until then, every
finding carries its own `fix:` clause, which names the edit rather than describing the problem, and
the rule modules under `seahaven/lint/` say in their docstrings what each rule is protecting.
