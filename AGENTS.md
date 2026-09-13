# Seahaven

Seahaven is a Python framework for building synthetic worlds: faithful, stateful mocks of a
company's tool surface, on SQLite, that agents work against in evals.

The spec is the source of truth and lives in `specs/projects/seahaven_framework/`: read
`project_overview.md`, then `functional_spec.md`, then `architecture.md` and `components/`. When
code and spec disagree, the spec wins unless a phase plan records why not. A change in behaviour
updates the spec in the same commit.

We develop with the `/spec` skill (https://github.com/scosman/vibe-crafting); the phases are in
`specs/projects/seahaven_framework/implementation_plan.md`. The skill owns the process.

`BACKLOG.md` holds real issues in already-committed code that are out of scope for the phase that
found them. Add to it rather than widening the diff under review; do not pick from it without
asking. Close an item by deleting it in the commit that fixes it.

`CONTRIBUTING.md` is the short guide for outside contributors.

## Environment

This project runs on a final release of CPython 3.14 or newer, never a release candidate: two
locked dependencies break on the 3.14 rcs, and uv will sync onto one without complaint. Check
before anything else:

```sh
uv run python -V        # must print 3.14.0 or higher, with no "rc" in it
```

If it is missing or prints a release candidate:

```sh
uv self update
uv python install 3.14
uv sync --extra serve
```

## Automated checks

These are what CI runs. All of them must be clean before any commit:

```sh
uv run python -c "import seahaven.openenv"
uv run ruff format --check
uv run ruff check
uv run ty check
uv run pytest                              # the framework
uv run pytest worlds/projecttracker        # the reference world
uv run pytest extensions/seahaven-xmlrpc   # the example extension
uv run python scripts/check_licences.py
```

The three suites are separate because a world and an extension are separate packages with their
own pytest rootdir. `ruff format` also formats Python blocks inside Markdown, and
`tests/test_docs_examples.py` executes every example in the docs, `README.md` and
`CONTRIBUTING.md`, so an example that is added anywhere it reaches will be run.

## Rules

- Python 3.14+, fully typed, tests included. `ty` is the checker.
- Tests are written with the code and catch real breakage; a test that still passes when the line
  under it is deleted is not a test.
- Comments carry external constraints, not a description of the code beneath them.
- No `LICENSE` file, no package publication, no version bump, no hub publication of the reference
  world without explicit maintainer sign-off.
- No real customer data, ever. The reference world is fictional: no real product's names, schema or
  error text.
- No copyleft in anything Seahaven ships. Runtime dependencies and every extra must be permissively
  licensed; `scripts/check_licences.py` is the rule and CI runs it. The bar for adding a runtime
  dependency at all is high, because Seahaven is vendored into other people's products.
