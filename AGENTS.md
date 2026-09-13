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
plus the state of it: nothing published, no licence file, and the interpreter check below spelled
out for a first checkout.

## Environment

**This project must run on CPython 3.14.0 or newer, and never on a release candidate.** Check the
interpreter before anything else:

```sh
uv run python -V        # must print 3.14.0 or higher, with no "rc" in it
```

If it is missing, or if it prints a release candidate, install a final release and re-sync:

```sh
uv self update          # or upgrade uv however you installed it: pip, Homebrew, a distro package
uv python install 3.14
uv sync --extra serve
```

**Nothing enforces this for you.** `requires-python = ">=3.14"` does not keep you off a release
candidate: uv matches an interpreter against it ignoring the pre-release segment, and syncs this
project onto `3.14.0rc2` with no warning and exit 0. The check is active -- the same uv refuses
3.14.0 for a project asking `>=3.15` -- it simply does not read an rc as one. What you get is
whatever your uv knows how to install: uv 0.8.17's download list has `cpython-3.14.0rc2` and no
final 3.14 at all, and 0.9.7's has `cpython-3.14.0`. Hence `uv run python -V`, by hand.

The check is worth the minute because a release candidate fails in ways that look like library bugs
and are not. On 3.14.0rc2 the pinned `pydantic` cannot evaluate this project's forward references,
so `import seahaven` itself raises and every suite gives nothing rather than a few failures -- that
is the one you meet first, with or without the extra. Under it, `collections.abc.ByteString` was
removed in rc2 and **restored before 3.14.0 final**, so `import beartype.typing`, which the `serve`
extra needs, raises `ImportError` there as well. Neither library is at fault and neither has a
version to move to: CPython moved and moved back. All of it is green on 3.14.0 final with no
patching.

Everything else (ruff, ty, pytest) is configured in `pyproject.toml` and runs through `uv run`.

## Rules

- Python 3.14+, fully typed. `ty`, `ruff` and the tests are clean before any commit.
- No `LICENSE` file, no package publication, no hub publication without explicit maintainer sign-off
  (implementation plan, Phase 13).
- No real customer data, ever. The reference world is fictional: no real product's names, schema or
  error text.
- Runtime dependencies are permissive only (MIT, Apache-2.0, BSD-class); CI checks.
