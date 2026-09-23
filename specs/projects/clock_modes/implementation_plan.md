---
status: complete
---

# Implementation Plan: Clock Modes

## Phases

- [ ] Phase 1: Clock core. `clock.py` modes, time sources and the per-statement SQL reading
  (architecture §2–3); `World(default_clock_mode=...)`, `world.instance(clock_mode=...)`, instance
  creation and the `tick` advance (§4); the state envelope field (§5.1, `state.py` only); the
  `ClockMode` export; the projecttracker generator pin (§6); every existing test that asserts an
  exact timestamp pinned at its call site; the new tests for these files (§8).
- [ ] Phase 2: Doors. OpenEnv reset option, schema order, observation and `State` field, client
  docstrings (§5.1–5.2); MCP `--clock-mode` and `SEAHAVEN_CLOCK_MODE` (§5.3); the pytest plugin
  marker signature (§5.4); their tests (§8).
- [ ] Phase 3: Docs. The bundled docs listed in functional spec §7, and `reference/api.md` for
  `Clock`, `ClockMode`, `World(default_clock_mode=...)` and `world.instance(clock_mode=...)`.
  Examples that show a moving clock use `tick`.
- [ ] Phase 4: Risk report. A written assessment, `specs/projects/clock_modes/risk_report.md`, of
  whether the per-statement SQL reading (architecture §3, `trace_v2` with `SQLITE_TRACE_STMT`) is
  safe and robust. It covers at least: SQLite's documented guarantees for `SQLITE_TRACE_STMT` and
  where they are loose ("possibly at other times"); nested statements a user function runs on the
  same connection; virtual tables reported as trigger activity; statements that are stepped
  partly, reset and re-run; APSW versions and the `id` keyword; the sandbox and any other tracer;
  thread use of the `inspect()` handle; what fails, and how visibly, if the trace is removed or
  never fires; the measured cost. Each risk gets a verdict and, where one is warranted, a test
  added to the suite or a follow-up proposed.
