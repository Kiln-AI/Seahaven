---
status: draft
---

# Backlog: Clock Modes

Items found while building clock modes that are out of scope for the phase that found them. Each is
closed or dismissed through the standard phase flow.

## Open

- **Choose and implement a fix for the per-statement reading.** Decide between F1 and F2 in
  `risk_report.md`, or neither. Implement the chosen fix, and turn the strict `xfail` tests it
  resolves into passing tests.
- **Qualify "one SQL statement takes one reading" in the docs.** The bundled docs
  (`concepts.md`, `reference/api.md`) state it without qualification. Today R4 (comment-led
  statements) and R6 (interleaved cursors) are exceptions. Update the docs to match whatever the
  F1/F2 decision leaves true.

## Closed

_None yet._
