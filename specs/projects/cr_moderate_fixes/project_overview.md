---
status: complete
---

# CR Moderate Fixes

Fix the moderate findings from the full-repo deep code review (September 2026) that the maintainer
decided to fix. The review's 56 moderate findings were triaged one by one: 28 are won't-fix, 9 are
pre-publish decisions, 1 was already fixed, and 18 are fixed by this project. The triage, with the
reason for every decision, is in [triage.md](triage.md).

Not a ton of phases. A good clustering of related items, with the low-risk ones grouped together:

- A batch of low-risk code and test fixes.
- The `bulk()` deadlock fix (2.M1) in its own phase and commit, so it can be reviewed and rolled
  back on its own. No ergonomic change for default callers.
- Middleware per contributing route (4.M1) in its own phase, so its complexity and tests can be
  judged on their own.
- A docs phase with every docs-only fix, including the history and spec-citation cleanup (9.M6).

Out of scope: the pre-publish items, the `seahaven new --hub` fix (on another branch), and the
review's mild findings.
