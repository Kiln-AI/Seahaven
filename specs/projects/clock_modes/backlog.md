---
status: complete
---

# Backlog: Clock Modes

Items found while building clock modes that are out of scope for the phase that found them. Each is
closed or dismissed through the standard phase flow.

## Open

_None._

## Closed

- **Choose and implement a fix for the per-statement reading.** Dismissed: the maintainer chose
  neither F1 nor F2. R4 and R6 are rare and narrow, and the strict `xfail` tests stay as the record
  of them.
- **Qualify "one SQL statement takes one reading" in the docs.** Closed with one sentence in
  `reference/api.md` that names the exceptions as rare and links to `risk_report.md`. The
  maintainer asked for no more than that.
