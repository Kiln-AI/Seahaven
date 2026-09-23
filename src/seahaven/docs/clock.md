# Clock

Every instance has a clock of its own, and everything in the world reads it: world code through
`ctx.clock`, and SQL through SQLite's own date and time functions. This page explains how the clock
starts, the four modes that say how it moves, and what they mean for the timestamps a world writes.

## Modes

An instance's clock starts at a **start instant**: the fixture's `now`, or for a blank instance the
`now=` it was given, or else the wall time at creation. The instance's **clock mode** says how the
clock moves from there:

| Mode | What the clock reads | Replays |
|---|---|---|
| `fixed` | The start instant, for the instance's whole life | Yes |
| `tick` | The start instant plus one second per tool call dispatched so far | Yes |
| `running` | The start instant plus the real time elapsed since the instance was created | No |
| `wall` | The host's current UTC time. The start instant is not used | No |

## Choosing a mode

`running` is the default. A world sets its own default with `World(default_clock_mode=...)`. One
instance takes another mode with `world.instance(clock_mode=...)`, which is also a key of `reset`
over a server and of the pytest marker. The mode does not change for the life of the instance.

## How `tick` counts

Under `tick`, every reading inside call `i`, counting from 0, is the start instant plus `i + 1`
seconds, however many readings the call makes. Startup hooks, and anything that reads the clock
before the first call, see the start instant. Reading the clock never moves it, in any mode.

```python
import seahaven

world = seahaven.World(
    name="notes",
    version="1.0.0",
    schema="CREATE TABLE notes (id TEXT PRIMARY KEY) STRICT;",
    state_format="seahaven.state/1",
)


@world.tool
def what_time_is_it(ctx: seahaven.Ctx) -> dict[str, object]:
    """Read the clock once in Python and once in SQL."""
    return {"python": ctx.clock.iso(), "sql": ctx.db.one("SELECT CURRENT_TIMESTAMP AS now")}


with world.instance(now="2026-06-01T09:00:00.000Z", clock_mode="tick") as inst:
    assert inst.clock.mode == "tick"
    assert inst.clock.iso() == "2026-06-01T09:00:00.000Z"
    first = inst.call("what_time_is_it")
    assert first == {
        "python": "2026-06-01T09:00:01.000Z",
        "sql": {"now": "2026-06-01T09:00:01.000Z"},
    }
    assert inst.call("what_time_is_it")["python"] == "2026-06-01T09:00:02.000Z"
    assert inst.clock.iso() == "2026-06-01T09:00:02.000Z"
```

## SQL

SQL reads the same clock as world code. Every connection overrides SQLite's own date and time
functions, such as `CURRENT_TIMESTAMP`, `datetime('now')`, `strftime` and `julianday`, to return
the clock's reading. Under `running` and `wall` the reading is taken once per SQL statement, as
SQLite does for its own functions, so two `CURRENT_TIMESTAMP` columns of one `INSERT` get the same
value.

## Where the wall clock is read

Under `fixed`, `tick` and `running`, a world reads the wall clock in two places only: the start
instant of a blank instance that was given no `now=`, and the `created_at` stamped on a fixture when
you freeze it. `running` measures elapsed time on a monotonic clock, so a change to the host's
clock does not move it. `wall` reads the host's clock at every reading.

## Blank instances and fixtures

The clock is made *before* the blank database is built, not after, so a schema file that seeds
reference rows of its own, such as `INSERT INTO plans VALUES ('free', ...)`, stamps them from the
instance's clock as well. Freezing records the clock's reading at that moment as the fixture's
`now`, and every instance of the fixture starts from there.

## Timestamps are not a complete order

Design around one consequence: **a timestamp is not a complete order.** Under `fixed`, every row
one run writes carries the same timestamp. Under `tick`, each call has its own instant, but the rows
one call writes share it. See
["Things that go wrong quietly"](authoring.md#things-that-go-wrong-quietly) in authoring.md.
