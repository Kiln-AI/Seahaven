# Fixtures

A fixture is the data an eval starts from: an immutable directory holding a SQLite file and a
sidecar. Every instance is a copy of one.

```
fixtures/
  small_startup/
    state.sqlite      # checkpointed, vacuumed, sealed read-only
    fixture.yaml      # where this state came from, and what is in it
```

```yaml
format_version: 1
id: small_startup
world: projecttracker
world_version: 1.0.0
schema_hash: eb3bc8f8a9033c1b633508c943d460c8eda52a2baa56b394120e3c7d8cbe33a9
now: '2026-06-01T09:00:00.000Z'
parent_id: null
file_sha256: c721f2d535d64ef52587b15664404294464c73d6b2ab6c1b54688e500b6a6257
created_at: '2026-09-13T08:11:06.580Z'
description: "A three-person startup's tracker: one engineering team, two active projects, ..."
```

`now` is the instant every instance of this fixture is frozen at. `created_at` is real wall-clock
time, and is one of only two wall-clock reads in a world — the other is a blank instance's default
clock.

## The rules

**A fixture is never opened, only copied.** The first time a process copies a fixture it verifies
`file_sha256` and refuses on a mismatch, naming the fixture.

**Freeze is the only way to mint one.** `inst.freeze(id, description)` checks the instance's schema
against the world's, checkpoints, vacuums, copies the file into `fixtures/<id>/`, writes the sidecar
with the instance's clock as `now` and the source fixture as `parent_id`, and seals the file
read-only. It refuses if the directory already exists.

**There is no in-place edit path.** Changing a fixture means forking it: instance from the parent,
change it, freeze the result under a new id.

**A fixture is addressed by id** everywhere: `world.instance("agency")`, `reset(fixture="agency")`,
`@pytest.mark.seahaven(fixture="agency")`, `seahaven fixture fork agency ...`.

## Building one

A blank instance is the authoring path: `world.instance()` with no fixture builds from the DDL
alone. Its clock is the wall time at creation unless `now=` says otherwise, and freezing bakes that
value in for ever.

Fill it through the world's own tools when the point is that the data is reachable the way an agent
would have made it, or through `inst.bulk()` when you are loading thousands of rows and a tool call
per row would spend its time on argument validation. `bulk()` yields the instance's own context —
one transaction, under the instance lock, no call attached — and startup hooks do not run again.

```python
from pathlib import Path

import seahaven

world = seahaven.World(
    name="notes",
    version="1.0.0",
    schema="CREATE TABLE notes (id TEXT PRIMARY KEY, body TEXT NOT NULL) STRICT;",
    fixtures_dir=Path("fixtures"),
    state_format="seahaven.state/1",
)

with world.instance(now="2026-06-01T09:00:00.000Z") as inst:
    with inst.bulk() as ctx:
        ctx.db.executemany(
            "INSERT INTO notes (id, body) VALUES (?, ?)",
            [(f"n{number}", f"note {number}") for number in range(1000)],
        )
    fixture = inst.freeze("seeded", "A thousand notes. Good for list and pagination scenarios.")

assert fixture.now == "2026-06-01T09:00:00.000Z"
assert fixture.parent_id is None

with world.instance("seeded") as inst:
    assert inst.inspect().one("SELECT count(*) AS n FROM notes") == {"n": 1000}
    # A fresh instance of a fixture has changed nothing yet.
    assert inst.changes() == []
```

`freeze` cannot run inside a `bulk()` block: the rows are not committed yet, and a fixture of
uncommitted rows is not what you asked for either. Leave the block first.

## The generator is committed

**Every fixture is committed together with the script that generates it.** A binary artifact with no
source is one nobody can change, and a schema change means regenerating all of them — which has to
be a command rather than a project.

The scaffold puts that script at `fixtures_src/generate.py`: one function per fixture, each taking a
live instance and filling it, which is the shape the CLI calls.

```sh
seahaven fixture freeze empty \
    --now 2026-06-01T09:00:00.000Z \
    --run fixtures_src.generate:empty \
    --description "The schema with no rows. Start here to write a history."

seahaven fixture fork empty startup_with_history \
    --run fixtures_src.generate:startup_with_history \
    --description "The empty tracker, plus one team and two months of issues."

seahaven fixture list
```

**Pass `--now` on every `freeze`, and the same value every time.** `freeze` starts from a blank
instance, and a blank instance with no `--now` takes the wall clock, so leaving the flag off dates
each fixture from the day it was built. Two fixtures of one world would then sit at two instants, and
`seahaven check` cannot tell you, because a wall-clock instant is a perfectly canonical timestamp.
Choose it once, write it in the generator's docstring, and never move it.

`fork` needs no `--now`: a fork inherits its parent's clock, which is the point of forking.

The scaffold's `build(fixture_id, *, world=...)` is that same freeze with `--now` and the
description carried for you, and `world=` is the seam a test builds through: a fixture is frozen
into `world.fixtures_dir`, so a test that rebuilds one to compare it with the committed bytes hands
in `copy.copy(world)` pointed at a temporary directory rather than moving the imported world's,
which every other caller in the process would see.

## Descriptions are for eval authors

The `description` is the one field written for a person — or an agent — choosing between fixtures.
It is what `seahaven fixture list` prints. Say what the data contains and what scenarios it
supports:

> A twelve-person agency: three teams, nine projects including a finished one, six hundred issues
> with a six-month history, comments, labels and an audit trail. Good for cross-team queries,
> reporting, bulk operations and search.

Not "the big one".

Worth having, in most worlds: an `empty` fixture holding the schema and no rows. It is the starting
point for setup-flow evals and the obvious thing to fork from, and it makes the first fixture a thing
the generator built like every other. (ProjectTracker ships one, though all three of its fixtures are
frozen from blank rather than forked: a generator that fills a blank instance from scratch is simpler
to read than a chain, and forking is for when the shared history is expensive to rebuild.)

## When the schema changes

The schema hash is in every sidecar, and instance creation refuses a fixture whose hash differs from
the loaded world, with a message saying it needs regenerating. `seahaven check` reports the same
thing as `SH403` before you get that far.

So: change the DDL, delete every fixture directory, re-run the generator for each one in parent
order, commit the new bytes. Nothing migrates a shipped fixture; V1 has no migration path at all,
which is the reason the generator is committed.

An extension that brings a DDL fragment (see [extensions.md](extensions.md)) changes the schema hash
too. That is the cost of the fourth seam, and it is stated plainly there.

## Where fixtures live, and how a world is deployed

`fixtures/` at the project root, beside `src/`, found by walking up from the module that constructed
the `World` to the directory holding `pyproject.toml`. `World(fixtures_dir=...)` names another
directory outright.

**A world is checked out, not installed.** That is the supported deployment shape, and the only one:
a world's directory — `pyproject.toml`, `src/`, `fixtures/` — is what you clone, mount into a
container, or `COPY` into an image, and the framework is installed *into* that checkout's
environment. `pip install <world>` is not a way to deploy a world, and the framework has no path
that makes it one.

The reason is that `fixtures/` sits **outside** the package on purpose. It is data the world ships
rather than code it imports: the generator writes it, `freeze` adds to it, `seahaven check` hashes
it off the filesystem, and a `git diff` on a frozen fixture should be a directory of bytes and not a
package resource. A wheel holds the package and nothing above it, so a built wheel carries the
schema — `schema/*.sql` is inside the package and is read through `importlib.resources` — and
carries no fixtures at all.

The consequence is sharp, and it is quiet until an eval runs:

- `world.instance()` — **blank**, built from the DDL — works from an install. Nothing about a blank
  instance touches the fixtures directory.
- `world.instance("empty")`, and every other **fixture-backed** instance, raises a `WorldBug` naming
  the directory it looked in and the two ways out. `world.fixtures()` is `[]`, and
  `seahaven fixture list` prints nothing.

  ```
  WorldBug: world 'projecttracker' has no fixture 'empty' in .../site-packages/fixtures;
  freeze one, or name the directory with World(fixtures_dir=...)
  ```

So a world that is installed rather than checked out serves blank instances and fails every eval
that starts from data — over a server, that is a `reset(fixture=...)` failing for an agent that can
do nothing about it.

**If you really do have to install one**, `World(fixtures_dir=...)` is the escape hatch: put
`fixtures/` wherever the deployment puts it and name that path. It is a supported argument and it
works. What it does not do is travel in the wheel with everything else, so the deployment owns
getting the directory there — a mounted volume, an image layer, a download at startup — and owns
keeping it in step with the package's schema hash. Read
["Publishing to a hub" in serving.md](serving.md) before taking this route; there is a second thing
`fixtures_dir=` moves with it.

## Verifying them

`seahaven check` has five fixture rules and they are all errors:

| Code | What it catches |
|---|---|
| `SH401` | a sidecar that does not validate |
| `SH402` | a state file that does not match its `file_sha256` — it has been edited since it was frozen |
| `SH403` | a fixture frozen from a different schema than the world declares |
| `SH404` | a `now` that is not a canonical timestamp |
| `SH405` | a `-wal` or `-shm` file beside the state, so it was opened for writing after freezing |

[reference/lints.md](reference/lints.md) has the fix for each.
