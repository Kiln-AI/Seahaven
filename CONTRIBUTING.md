# Contributing to Seahaven

Read this before spending time on a change. It describes how this repository is actually developed
and what to run. It is deliberately not a welcome mat: the project is early, and several decisions a
contributor would want made have not been made — starting with whether there is a licence to
contribute under.

The checkout itself is in good shape, and that is a recent change: everything below was measured on
CPython 3.14.0, in an environment built by the `uv sync` line this file gives you, with nothing
patched in it. The one thing that will waste your afternoon is being on a 3.14 *release candidate*
instead, which is why the setup section makes you check.

## Before anything: the state of the project

- **There is no `LICENSE` file, and no licence has been granted.** `project_overview.md` §3 states
  the intent — open source, MIT — but intent is not a grant, and the file does not exist. Adding it
  is one of the decisions Phase 13 of `implementation_plan.md` is gated on, and the maintainer has
  not taken it. Until it lands there is no licence to redistribute this code under and no stated
  terms a contribution would be accepted under, so **ask before you invest in a change**. This is
  not a formality you can route around: `AGENTS.md` forbids adding the file, and a pull request that
  adds one will be refused.
- **Nothing is published.** The `seahaven` name on PyPI holds a placeholder release that contains
  none of this code, ProjectTracker is not on a hub, and `pip install seahaven` therefore succeeds
  and installs a stub rather than failing. Install from a checkout. `README.md` has the detail, and
  `BACKLOG.md` B22 has the three places that still print the wrong advice.
- **The API moves.** Phases 1–12 built the framework and names moved between them. Nothing is
  deprecated, because nothing is stable enough to deprecate.
- **Do not add a licence file, publish a package, or publish the reference world to a hub.**
  `AGENTS.md` names those three and gates them on explicit maintainer sign-off. A version bump is
  not named there, and is refused here as part of publication rather than on its own authority:
  `version = "0.0.1"` is what the PyPI placeholder holds, and changing it is a release decision.

## How this repository is developed

Seahaven is spec-driven and built phase by phase, mostly by agents, using the `/spec` skill
(<https://github.com/scosman/vibe-crafting>). `AGENTS.md` at the root is the standing brief and the
first thing to read; this section is the longer version of it.

**The spec is the source of truth.** It lives in `specs/projects/seahaven_framework/`: read
`project_overview.md`, then `functional_spec.md`, then `architecture.md` and `components/`. When
code and spec disagree the spec wins, unless a phase plan records why not. A change in behaviour
therefore usually means changing a spec artifact in the same diff — an undocumented divergence is a
defect even when the code is better.

**Work is organised in phases, not in features.** `implementation_plan.md` lists them. Each phase is
one coding round, one code review, and one commit, and the commit is named for it — `git log` reads
`Phase 12: the docs, written and executed`, `Phase 11: the benchmark, and what the gate sweep
found`. A phase is a reviewable unit, which is where most of the process pressure in this repository
comes from.

**Each phase writes its plan before its code**, to `specs/projects/seahaven_framework/phase_plans/`:
a `status` frontmatter, an overview, numbered steps that name the files and the signatures, and the
tests it owes. Recent plans add a "What the code review changed" section afterwards, recording what
the review found; the early ones do not, so read a late plan for the shape rather than
`phase_plans/phase_1.md`. `phase_plans/phase_12.md` is the one to read, including its own summary
of the failure mode four of its five review rounds turned up: a plausible sentence written from the
code's shape rather than from a run.

**Review is adversarial and runs in rounds until a round is clean.** Findings are rated Critical,
Moderate and Mild, and a reviewer reproduces a claim rather than reading it. Tests are mutation-swept
— `BACKLOG.md` B1 is the residue of a sweep of 912 statements across three phases — so a test that
still passes when the line beneath it is deleted counts as no test.

If you are an agent working in this repository, `AGENTS.md` is your brief and this file is context
for it, not a replacement.

## `BACKLOG.md`, and not widening a diff

`BACKLOG.md` holds real defects, test gaps and spec inaccuracies in code that is **already
committed** and out of scope for the phase that found them. It exists to protect the reviewable
unit, and it has three rules worth stating on their own:

- **Do not widen a diff that is under review.** A defect you notice in older code while working on
  something else goes in `BACKLOG.md`, with enough detail to act on without re-deriving it and a
  note of where it was found. It does not go into the diff in front of the reviewer.
- **Do not pick an item without asking.** Several of them are open because a decision has not been
  taken, not because nobody got to them — B15 and B16 both say in so many words that what they
  need next is a maintainer's call, not a patch.
- **Close an item by deleting it** in the commit that fixes it. There is no "done" section.

What does not belong there: work the implementation plan already schedules, and anything in the diff
currently under review. Those are the plan's job and the review's job respectively.

## Getting a checkout that runs

A **final** Python 3.14 or newer, and [uv](https://docs.astral.sh/uv/). Older interpreters are not
supported, and not in the sense that they are merely untested: `requires-python` is `>=3.14`, and
five modules use PEP 758's unparenthesized `except A, B:`, so they do not parse on 3.13 at all.

```sh
uv self update                 # or upgrade uv however you installed it: pip, Homebrew, a distro
uv python install 3.14
git clone https://github.com/Kiln-AI/Seahaven
cd Seahaven
uv sync --extra serve
uv run python -V
```

That last line has to print `3.14.0` or higher with no `rc` in it, and it is a check you make by
hand because **nothing in the project metadata makes it for you**: uv matches an interpreter against
`requires-python` ignoring the pre-release segment, so `uv sync --frozen --extra serve` onto
`3.14.0rc2` succeeds here with no warning and exit 0. The constraint is live — the same uv refuses
3.14.0 for a project asking `>=3.15` — it just does not read an rc as out of range.

`uv self update` is the first line for a related reason: uv installs only a Python it knows about,
and an older one settles for the newest release candidate in its list. uv 0.8.17's download list
has `cpython-3.14.0rc2` and no final 3.14 at all; 0.9.7's has `cpython-3.14.0`. The next section is
what an rc costs you.

`uv sync --extra serve` gives you the framework, the reference world and the example extension as
workspace members, pytest, ruff and ty from the `dev` dependency group, and OpenEnv, which
`src/seahaven/openenv/` and its tests need. `.github/workflows/ci.yml` installs the same thing with
`--locked`, and on a final 3.14 it simply works: the locked closure imports and the whole suite
runs, with nothing patched anywhere. Sync without the extra and you still get a checkout that
runs — the OpenEnv test modules guard with `pytest.importorskip` and skip rather than fail, at
`962 passed, 5 skipped` for the framework suite and `252 passed, 1 skipped` for the world — but
`seahaven serve`, the environment and the client are then tested nowhere, so use the extra. That
skip is quiet, which is why CI asserts the import on its own line; see "The checks".

Then run the three suites. They are three suites and not one because a world and an extension are
separate packages with their own pytest rootdir, tested the way their authors would test them:

```sh
uv run pytest                              # the framework
uv run pytest worlds/projecttracker        # the reference world
uv run pytest extensions/seahaven-xmlrpc   # the example extension
```

Those command lines, in a checkout synced as above, are what produced these:

| Suite | Result |
|---|---|
| framework | `1093 passed` in ~45 s |
| reference world | `261 passed` in ~16 s |
| example extension | `75 passed` in ~1 s |

One test carries the `slow` marker (the 500-session smoke test in `tests/test_server.py`);
`uv run pytest -m "not slow"` deselects it and gives `1092 passed, 1 deselected`. The saving varies
enough between runs to not be worth quoting.

## Why the interpreter check is worth the minute

A 3.14 release candidate fails in ways that look like library bugs and are not, which is an
expensive afternoon if you meet them without knowing. Both of the following were reproduced on
`cpython-3.14.0rc2` and neither happens on 3.14.0 final.

**The failure you will actually meet is `pydantic`.** The locked 2.13.5 cannot evaluate this
project's forward references on rc2, so `import seahaven` itself raises — and that is before any
extra, so every suite gives you nothing at all rather than a few failures:

```
File ".../pydantic/_internal/_typing_extra.py", line 481, in eval_type_backport
    assert isinstance(value, typing.ForwardRef)
AssertionError
```

**Underneath it there is a second one, which you have to go looking for.**
`collections.abc.ByteString` was removed in 3.14.0rc2 and restored before 3.14.0 final —
`hasattr(collections.abc, "ByteString")` is `False` on the first and `True` on the second — and
the locked `beartype` 0.22.9 imports that name unguarded. `import beartype.typing` is what
produces it, and `beartype.typing` is on the `serve` extra's import path, reached from
`from fastmcp import Client` in `openenv/core/env_server/mcp_environment.py`. Importing the extra
on an rc does not get you this traceback, though, because pydantic stops you first:

```
File ".../beartype/typing/__init__.py", line 306, in <module>
    from collections.abc import ByteString as ByteString  # type: ignore[attr-defined]
ImportError: cannot import name 'ByteString' from 'collections.abc'
```

Neither is a defect in Seahaven or in `uv.lock`, and neither wants a workaround here: both of those
versions are the ones the lock already holds, and both are green on 3.14.0. What they want is a
final interpreter, which nothing but the check at the top of the previous section will get you.

## The checks

These are the checks `.github/workflows/ci.yml` runs, in its order, and they all have to be clean
before a commit — `AGENTS.md` states that as a rule, not an aspiration:

```sh
uv run python -c "import seahaven.openenv"
uv run ruff format --check
uv run ruff check
uv run ty check
uv run pytest
uv run pytest worlds/projecttracker
uv run pytest extensions/seahaven-xmlrpc
uv run python scripts/check_licences.py
```

Notes on four of them:

- **`uv run python -c "import seahaven.openenv"` is there because a skip is quiet.** The OpenEnv
  test modules guard with `pytest.importorskip`, which is right when the extra is not installed and
  wrong when it is installed and does not import: the suite would pass, exit 0, and have tested
  none of `seahaven serve`, the environment or the client. This line is the one place that says so
  out loud.
- **`ruff format` reaches Markdown.** Python blocks inside `.md` files are formatted like any other
  Python, so a code block in a doc page, in `README.md` or in this file is subject to the format
  check. Line length is 100, and the lint rule set is `E, F, I, UP, B, SIM, RUF`.
- **`ty check` covers `src`, `tests`, `scripts`, `worlds`, `extensions` and `bench`.** Everything is
  typed, tests included.
- **`scripts/check_licences.py` checks everything Seahaven ships** — what `pip install seahaven`
  pulls in, plus every extra the project declares, because `serve` is a runtime extra — against an
  allowlist, and prints `every shipped dependency is allowed (base closure plus: serve)` when it
  passes. The rule is no copyleft: GPL, AGPL and LGPL are refused in any spelling, permissive
  licences pass, and so do MPL-2.0 and CC0-1.0. An unrecognised identifier fails; so does a declared
  extra that is not installed, because a licence that cannot be read cannot be cleared — run it in
  an environment synced with `--extra serve`. Development tools are a dependency group rather than
  an extra and are not checked, because they are distributed with nothing.

One check is not in CI and is worth running anyway if you touched the lints or the reference world:

```sh
uv run seahaven check --world projecttracker:world
```

It is silent and exits 0 when a world is clean. Run from `worlds/projecttracker/`, plain `seahaven
check` finds the world by the convention (the project's package, attribute `world`) and does the
same thing. Run from the repository root without `--world` it fails with `SH501`, correctly:
Seahaven is not a world.

There is also a benchmark in `bench/`, run by hand and never a gate. `bench/README.md` has the
commands and `bench/results/latest.md` has the caveats to read before quoting any number from it.

## Standards

From `AGENTS.md`, with what each one means in practice:

- **Python 3.14+, fully typed.** Annotations on every definition, in the tests too. `ty` is the
  checker.
- **`ty`, `ruff` and the tests are clean before any commit.** All eight commands above.
- **Runtime dependencies are permissive only** (MIT, Apache-2.0, BSD-class), and CI checks. Adding
  one means the licence gate has to still pass, and the bar for adding one at all is high: Seahaven
  is vendored into other people's products.
- **No real customer data, ever.** The reference world is fictional, and deliberately so: no real
  product's names, schema or error text, in code, tests, fixtures or docs.
- **Examples are executed, not proofread.** `tests/test_docs_examples.py` runs every fenced Python
  block in the bundled docs, in `README.md` and in this file, parses every fragment, resolves every
  `seahaven.*` name a block mentions against the real package, resolves every documented member
  against a live object, and parses every `seahaven ...` command line in a shell block with the real
  argument parser. If you add an example anywhere it reaches, it will be run.
- **Comments carry external constraints**, not a description of what the code beneath them does. The
  code is expected to explain itself by naming and structure.
- **Tests catch real breakage.** There is no coverage gate and no coverage tool configured; the
  standard is enforced by review, which mutates the code under a test to see whether the test
  notices.

## Making a change

1. **Ask first**, for the licence reason at the top and because the phase plan may already own the
   work. A change nobody asked for, against an unlicensed repository, in a codebase whose spec is
   the source of truth, is the shape of contribution most likely to be wasted.
2. **Work on a branch**, and keep the diff to one reviewable unit. Anything you notice that is not
   that unit goes in `BACKLOG.md`.
3. **Change the spec with the code** when behaviour changes. If you disagree with a spec artifact,
   change it in the same diff and say why, rather than leaving code and spec disagreeing.
4. **Write the tests with the code**, and reuse the helpers that are there — `tests/conftest.py`,
   `tests/worlds/` and each package's own conftest.
5. **Run all eight checks** before you commit, and say in the pull request which of them you ran and
   on what interpreter — the exact `uv run python -V`, not "3.14". If anything was skipped rather
   than run, say what and why.
6. **Write the commit message for the unit**, the way `git log` already does: what changed and what
   it was for, not a file list.

## Things this repository will not take

- A `LICENSE` file, licence text, or a licence field or classifier in `pyproject.toml`.
- A package publication, a release, a version bump, or a hub publication of ProjectTracker.
- A change to `uv.lock` or to the `pyproject.toml` dependency bounds that has not been run against
  all three suites. Those bounds are the versions this project is known green on, which is what the
  comment above them says; raising one is a measurement, not an edit.
- A widened diff carrying drive-by fixes to code the change did not touch.
- Real customer data, or a world modelled on a real product's names, schema or error text.
- A new runtime dependency under a non-permissive licence.
