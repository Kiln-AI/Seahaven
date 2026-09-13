# Seahaven

Seahaven is a Python framework for building synthetic worlds: faithful, stateful mocks of a
company's tool surface, on SQLite, that agents work against in evals. The spec is the source of
truth and lives in `specs/projects/seahaven_framework/`: read `project_overview.md`, then
`functional_spec.md`, then `architecture.md` and `components/`. When code and spec disagree, the
spec wins unless a phase plan records why not.

## How we work

We use the `/spec` skill for agentic development: https://github.com/scosman/vibe-crafting. The
phases are in `specs/projects/seahaven_framework/implementation_plan.md`; each phase is one coding
round, one code review, one commit.

`BACKLOG.md` holds real issues in already-committed code or artifacts that are out of scope for the
phase that found them. Add to it rather than widening the diff under review; do not pick from it
without asking.

`CONTRIBUTING.md` is the same ground written for someone who has not seen the repository before,
plus the state of it: nothing published, no licence file, and the `serve` extra broken on 3.14.

## Environment

If you are running in a VM or a fresh container, check the interpreter before anything else:
`python3.14 --version`. If it is missing or a pre-release, run `uv python install 3.14`, then
`uv sync`. Everything else (ruff, ty, pytest) is configured in `pyproject.toml` and runs through
`uv run`.

## Rules

- Python 3.14+, fully typed. `ty`, `ruff` and the tests are clean before any commit.
- No `LICENSE` file, no package publication, no hub publication without explicit maintainer sign-off
  (implementation plan, Phase 13).
- No real customer data, ever. The reference world is fictional: no real product's names, schema or
  error text.
- Runtime dependencies are permissive only (MIT, Apache-2.0, BSD-class); CI checks.
