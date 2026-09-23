---
status: complete
---

# Clock Modes

Add clock modes to Seahaven. Today an instance's clock is fixed: it holds the fixture's `now` for
the instance's whole life. Add three more modes:

- **Running** (real time progressing): store `start_time = now()` at instance start. The clock's
  time at any moment is `now() - start_time + initial_world_time`.
- **Tick** (ticking): each tool call adds 1s. Every read inside one call sees the same time, and
  the first call sees the start time plus 1s.
- **Wall**: the host's true wall-clock time.

The existing behaviour stays available as **fixed**.

## API Surface

- New Seahaven reset option: `clock_mode`, an enum of `fixed`, `tick`, `running` and `wall`.
  Optional; uses the world's default if not set.
- New world creation option: `default_clock_mode`. Optional; defaults to `running`.

Fixtures do not have a clock mode. A fixture has a timestamp: the instance's clock reading when it
was frozen. Instances have a mode, and worlds have a default.

## Notes

- Seahaven is pre-v1, so changing the default from a fixed clock to `running` for every existing
  world is accepted (decided).
