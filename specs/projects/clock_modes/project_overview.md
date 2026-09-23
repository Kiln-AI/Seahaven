---
status: complete
---

# Clock Modes

Add clock modes to Seahaven. Today an instance's clock is fixed: it holds the fixture's `now` for
the instance's whole life. Add two more modes:

- **Progressing** (real time progressing): store `start_time = now()` at instance start. The
  clock's time at any moment is `now() - start_time + initial_world_time`.
- **Tick** (ticking): each read adds 1s.

The existing behaviour stays available as **fixed**.

## API Surface

- New Seahaven reset option: `clock_mode`, an enum of `fixed`, `tick` and `progressing`. Optional;
  uses the world's default if not set.
- New world creation option: `default_clock_mode`. Optional; defaults to `progressing`.

## Notes

- Seahaven is pre-v1, so changing the default from a fixed clock to `progressing` for every
  existing world is accepted (decided).
