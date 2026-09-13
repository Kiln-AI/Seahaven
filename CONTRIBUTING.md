# Contributing to Seahaven

Read this before spending time on a change. It describes how this repository is actually developed,
what to run, and what is broken today. It is deliberately not a welcome mat: the project is early,
several decisions that a contributor would want made have not been made, and one thing you will hit
in the first ten minutes does not work.

Everything stated here was run on the interpreter this repository targets before it was written. The
runs are recorded in `specs/projects/seahaven_framework/phase_plans/phase_13.md`, including the ones
that produced a failure.

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
  taken, not because nobody got to them — B17 below is the clearest case.
- **Close an item by deleting it** in the commit that fixes it. There is no "done" section.

What does not belong there: work the implementation plan already schedules, and anything in the diff
currently under review. Those are the plan's job and the review's job respectively.

## Getting a checkout that runs

Python 3.14 or newer, and [uv](https://docs.astral.sh/uv/). Older interpreters are not supported,
and not in the sense that they are merely untested: `requires-python` is `>=3.14`, and five modules
use PEP 758's unparenthesized `except A, B:`, so they do not parse on 3.13 at all.

**It has to be a final 3.14, not a release candidate.** Against the lock, `import seahaven` fails on
3.14.0rc2, so what you get from the three suites below is not a few failures — it is nothing: every
one of them dies on the first import with an `AssertionError` inside `pydantic`'s
`eval_type_backport`. `uv python install 3.14` installs whatever the index offers, and on the
machine this was written on that was rc2 and nothing else. The detail is two sections down, under
"What is broken today"; check `python3.14 --version` before believing any of the numbers here.

```sh
uv python install 3.14
git clone https://github.com/Kiln-AI/Seahaven
cd Seahaven
uv sync
```

`uv sync` without an extra gives you the framework, the reference world and the example extension as
workspace members, plus pytest, ruff and ty from the `dev` dependency group. **Do not add
`--extra serve` yet** — read the next section first.

Then run the three suites. They are three suites and not one because a world and an extension are
separate packages with their own pytest rootdir, tested the way their authors would test them:

```sh
uv run pytest                              # the framework
uv run pytest worlds/projecttracker        # the reference world
uv run pytest extensions/seahaven-xmlrpc   # the example extension
```

**The counts below were not produced by those command lines.** They were produced by
`uv run --no-sync` inside this repository's own hand-patched `.venv` on 3.14.0rc2, at the commit
that added this file — which is the only environment here in which the suites run at all, for the
reason above. On a final 3.14 the commands as written should give the same counts. That is an
expectation and not a measurement: this sandbox has no final 3.14 to measure on.

| Suite | Without the `serve` extra | With a working `serve` extra |
|---|---|---|
| framework | `960 passed, 5 skipped` in ~20 s | `1101 passed` in ~42 s |
| reference world | `260 passed` in ~16 s | same |
| example extension | `75 passed` in ~1 s | same |

The five skips are the OpenEnv modules: they guard with `pytest.importorskip`, so an absent extra
skips them rather than failing. One test carries the `slow` marker (the 500-session smoke test in
`tests/test_server.py`); `uv run pytest -m "not slow"` deselects it. The saving varies enough
between runs here to not be worth quoting.

## What is broken today: the `serve` extra on 3.14

This is `BACKLOG.md` B17, it is an environment and lockfile problem rather than a defect in
Seahaven's code, and **it is a maintainer decision that is still open — do not "fix" it.** Both
candidate fixes touch `uv.lock`.

`uv.lock` resolves `beartype` 0.22.9, which believes the removal of `collections.abc.ByteString` was
deferred to Python 3.17. It imports the name under `if _IS_PYTHON_AT_MOST_3_16:`, which is true on
3.14, and unguarded by any `try`. Python 3.14 removed the name. Verified directly, on a pristine
install of that version:

```
File ".../beartype/typing/__init__.py", line 306, in <module>
    from collections.abc import ByteString as ByteString  # type: ignore[attr-defined]
ImportError: cannot import name 'ByteString' from 'collections.abc'
```

`fastmcp` catches that `ImportError` and re-raises its own, so what you actually see names the wrong
cause. After `uv sync --locked --extra serve`, `uv run pytest` does not fail four tests — it stops:

```
E   ImportError: FastMCP client support is not installed. Install `fastmcp` or `fastmcp-slim[client]`.
ERROR tests/test_client.py
ERROR tests/test_env.py
ERROR tests/test_serve.py
ERROR tests/test_server.py
!!!!!!!!!!!!!!!!!!! Interrupted: 4 errors during collection !!!!!!!!!!!!!!!!!!!!
1 skipped, 4 errors in 1.29s
```

Collection is interrupted, so none of the other 960 tests run. `import openenv` succeeds (it loads
its subpackages lazily) and `import seahaven.openenv` is what fails, which is why four of the five
modules error rather than skip: they guard on `openenv`. The fifth, `tests/test_cli_serve.py`,
guards on `seahaven.openenv` with `exc_type=ImportError` and skips. B17 was written when all five
errored and has not been renumbered since the fifth was changed; four is what you will see.

**What to do about it:** work without the extra. Everything except `seahaven serve`, the OpenEnv
environment and the client is covered by the 960 tests that run. If your change is in
`src/seahaven/openenv/`, say so when you ask about the change, because you cannot test it against
the lock as it stands.

`.github/workflows/ci.yml` installs `--extra serve`, so CI meets this too. A red CI on that step is
not something your branch caused.

Two related facts you may meet:

- **On a 3.14 release candidate, `import seahaven` itself fails against the lock**, which is the
  warning at the top of the previous section. The locked `pydantic` is 2.13.5, which asserts inside
  `eval_type_backport` on 3.14.0rc2; 2.12.3 does not. This one is not confined to the `serve` extra:
  it takes out every suite, with or without it, before a test runs. Verified here on rc2. It is
  expected to disappear against a final 3.14, which is what CI installs, and it is untested against
  one because this sandbox has no final 3.14.
- **This repository's own environment is hand-patched** so that the suite can run at all: the
  `.venv` here holds `pydantic` 2.12.3 rather than the locked 2.13.5 and a `beartype` edited in
  place. `uv.lock` and the `pyproject.toml` pins are deliberately untouched. A plain `uv sync`
  resyncs the environment to the lock and takes the patch away, which is why work in a patched
  environment runs `uv run --no-sync ...` — the form `bench/README.md` uses throughout. If you patch
  your own environment: uv links packages into a venv from its cache by hard link, so editing a file
  under `.venv/` edits the cached wheel too, and the next venv you build from that cache is patched
  as well. That was observed here, and it makes a patched environment much stickier than it looks.

## The checks

These are the checks `.github/workflows/ci.yml` runs, in its order, and they all have to be clean
before a commit — `AGENTS.md` states that as a rule, not an aspiration:

```sh
uv run ruff format --check
uv run ruff check
uv run ty check
uv run pytest
uv run pytest worlds/projecttracker
uv run pytest extensions/seahaven-xmlrpc
uv run python scripts/check_licences.py
```

Notes on three of them:

- **`ruff format` reaches Markdown.** Python blocks inside `.md` files are formatted like any other
  Python, so a code block in a doc page, in `README.md` or in this file is subject to the format
  check. Line length is 100, and the lint rule set is `E, F, I, UP, B, SIM, RUF`.
- **`ty check` covers `src`, `tests`, `scripts`, `worlds`, `extensions` and `bench`.** Everything is
  typed, tests included.
- **`scripts/check_licences.py` checks the runtime closure only** — what `pip install seahaven`
  pulls in, extras excluded — against a permissive allowlist, and prints `every runtime dependency
  is permissively licensed` when it passes. Development tools are not checked because they are not
  distributed. The extra is not checked either, which is `BACKLOG.md` B15.

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
- **`ty`, `ruff` and the tests are clean before any commit.** All seven commands above.
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
5. **Run all seven checks** before you commit, and say in the pull request which of them you ran and
   on what interpreter. If the `serve` extra is why something did not run, say that too.
6. **Write the commit message for the unit**, the way `git log` already does: what changed and what
   it was for, not a file list.

## Things this repository will not take

- A `LICENSE` file, licence text, or a licence field or classifier in `pyproject.toml`.
- A package publication, a release, a version bump, or a hub publication of ProjectTracker.
- A change to `uv.lock` or to the `pyproject.toml` pins that works around B17.
- A widened diff carrying drive-by fixes to code the change did not touch.
- Real customer data, or a world modelled on a real product's names, schema or error text.
- A new runtime dependency under a non-permissive licence.
