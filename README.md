# Seahaven

Seahaven is a Python framework for building **synthetic worlds**: faithful, stateful mocks of the
tool surface a real company's agent works against, on SQLite, that agents work against in evals and
in RL.

A world is an ordinary Python package. Its schema is hand-written SQLite DDL, its tools are plain
Python functions, and its data is a set of frozen SQLite files called fixtures. An eval makes an
*instance* — a private copy of one fixture — drives it through tool calls, and grades it on the
state the episode left behind.

```python
import seahaven

world = seahaven.World(
    name="notes",
    version="1.0.0",
    schema="""
    CREATE TABLE notes (
        id TEXT PRIMARY KEY,
        body TEXT NOT NULL,
        created_at TEXT NOT NULL
    ) STRICT;
    """,
)


@world.tool
def add_note(ctx: seahaven.Ctx, body: str) -> dict[str, str]:
    """Write a note down and return it."""
    note = {"id": ctx.ids.uuid(), "body": body, "created_at": ctx.clock.iso()}
    ctx.db.execute(
        "INSERT INTO notes (id, body, created_at) VALUES (?, ?, ?)",
        note["id"],
        note["body"],
        note["created_at"],
    )
    return note


with world.instance(now="2026-06-01T09:00:00.000Z") as inst:
    note = inst.call("add_note", body="buy milk")
    assert note["created_at"] == "2026-06-01T09:00:00.000Z"  # the instance's clock, frozen
    assert [change.op for change in inst.changes()] == ["insert"]  # what an eval grades
```

What the framework is for, in one list:

- **Time is frozen and ids are seeded**, so the same fixture, seed and calls give the same run.
  Every SQLite date function on the instance's connection returns the fixture's instant too.
- **Fixtures are immutable and verified.** They are copied, never opened; the sidecar carries the
  schema hash and the file's SHA-256, and an instance refuses a fixture that does not match.
- **Every tool is validated strictly** against a JSON schema derived from its signature, and every
  violation is reported at once.
- **A changeset**, not a transcript: `inst.changes()` is the net difference between the fixture and
  the state the episode left.
- **Nothing engine-shaped reaches the agent** unless the world chose it — that is the error
  handler's job, and every world has one.
- **Remote is OpenEnv.** `seahaven serve` runs one world, one instance per session.
- **`seahaven check`** is a small lint over the mistakes a world makes silently, each with a named
  fix.

## Status

**Early development. Nothing here is stable, and none of it is published.** The framework, the
reference world, the example extension and the benchmark are complete and tested in this
repository; the `seahaven` name on PyPI holds a placeholder release that contains none of it,
ProjectTracker is not on a hub, and there is no licence file — publication and licensing are a
maintainer decision that has not been taken. `pip install seahaven` therefore gets you a stub rather
than an error: install from a checkout, and expect names to move.

## What is in the repository

| | |
|---|---|
| `src/seahaven/` | the framework: the runtime database layer, world and dispatch, fixtures and instances, the helpers, the CLI and lints, the OpenEnv server and client, the pytest plugin, and the bundled docs |
| `worlds/projecttracker/` | the reference world: a fictional issue tracker, nine tables, 25 tools plus Seahaven's two SQL helpers, three fixtures |
| `extensions/seahaven-xmlrpc/` | the example extension, proving the extension contract carries a protocol the framework knows nothing about |
| `bench/` | the benchmark and its committed results, with the caveats they need |

## Running it from a checkout

Python 3.14+ and [uv](https://docs.astral.sh/uv/). It has to be a **final** 3.14, not a release
candidate — see the note below the examples.

```sh
uv self update                             # or however you installed uv: pip, Homebrew, a distro
uv python install 3.14
git clone https://github.com/Kiln-AI/Seahaven
cd Seahaven
uv sync --extra serve
uv run pytest                              # the framework's own suite
uv run pytest worlds/projecttracker        # the reference world
uv run pytest extensions/seahaven-xmlrpc   # the example extension
uv run ruff check && uv run ty check
```

Try the reference world:

```sh
cd worlds/projecttracker
uv run seahaven fixture list
uv run seahaven check
uv run python - <<'PY'
import projecttracker

with projecttracker.world.instance("small_startup") as tracker:
    print(tracker.call("get_issue", key="ENG-12")["title"])
PY
```

One environment note, and it is the only one: **the interpreter has to be a final 3.14, not a
release candidate.** `uv run python -V` should print `3.14.0` or higher with no `rc` in it. On
3.14.0rc2 the locked `pydantic` cannot evaluate this project's forward references, so `import
seahaven` itself raises `AssertionError` inside `eval_type_backport` and every suite gives nothing
before a test runs; `collections.abc.ByteString` is absent there too, so `import beartype.typing`,
which the `serve` extra needs, raises `ImportError` there as well. Neither needed a change here:
`ByteString` is back in 3.14.0 final, and the locked closure imports and runs green on it with no
patching.

Nothing enforces the interpreter for you, which is why the check is by hand: uv matches an
interpreter against `requires-python` ignoring the pre-release segment, and syncs this project onto
`3.14.0rc2` with no warning. What you get is whatever your uv knows how to install — uv 0.8.17's
download list has `cpython-3.14.0rc2` and no final 3.14 at all, 0.9.7's has `cpython-3.14.0` —
which is why `uv self update` comes first above.

## Building a world

From the repository root, staying inside this repository's environment — which is the only
environment that has the framework until Seahaven is published:

```sh
uv run seahaven new mytracker
uv pip install -e ./mytracker --no-deps
uv run pytest mytracker
uv run seahaven check --world mytracker:world
```

That scaffold is a working world: one table, two tools, an error handler, three passing tests and
the generator script every fixture will be built by. Fill in the schema, write the tools, freeze a
fixture, and serve it.

Two of those lines are shaped by the framework being unpublished, and both go away when it is.
`--no-deps` stops uv resolving the scaffold's `seahaven~=0.0` dependency at all: it resolves — to the
placeholder release described above — and while the editable install in this environment happens to
satisfy it too, relying on that coincidence is not advice. `--world` is needed because the world is
not this repository's own project, so the convention that finds a world from the nearest
`pyproject.toml` finds Seahaven instead.

**Do not follow the next steps `seahaven new` prints** (`cd mytracker`, `uv sync`, `uv run pytest`,
`uv run seahaven check`) until then. Leaving this directory leaves the environment that has the
framework, and `uv sync` there succeeds — installing the scaffold and the placeholder, whose whole
`__init__.py` says *"This release reserves the package name. The API is not implemented yet."* and
exports `__version__`.

What follows is not one clean error, which is the point. The scaffold declares no test dependency,
so `uv run pytest` runs whatever pytest it can find: here that was a system Python 3.11 outside the
new environment, reporting `ModuleNotFoundError: No module named 'seahaven'` from the test's own
`import seahaven`. Put a pytest inside that environment (`uv run --with pytest pytest`) and
collection succeeds instead — and all three tests error with `fixture 'instance' not found` and
`fixture 'world' not found`, under a `PytestUnknownMarkWarning: Unknown pytest.mark.seahaven`,
because the placeholder ships no pytest plugin. `uv run seahaven check` never starts at all:
`error: Failed to spawn: seahaven`, the placeholder having no console script. The world's own
failure — `ModuleNotFoundError: No module named 'seahaven.world'`, from the error handler's import —
waits until something imports the package, which the scaffold's tests never do.

The alternative, if you want the world in an environment of its own, is to put the framework there
from this checkout with `uv pip install -e /path/to/Seahaven`.

## Contributing

Read [`CONTRIBUTING.md`](CONTRIBUTING.md) first: setup, the checks that have to pass, and the
guidelines.

## Documentation

The framework's docs ship **inside the installed package**, so they always match the version
installed, and `seahaven docs` prints the directory. In this repository they are
[`src/seahaven/docs/`](src/seahaven/docs/):

- [`index.md`](src/seahaven/docs/index.md) — what Seahaven is, the reading order, the commands
- [`concepts.md`](src/seahaven/docs/concepts.md) — world, fixture, instance, tool, clock, changesets
- [`authoring.md`](src/seahaven/docs/authoring.md) — writing tools, errors, middleware, startup hooks, the schema
- [`fixtures.md`](src/seahaven/docs/fixtures.md) — freezing, forking, generators
- [`testing.md`](src/seahaven/docs/testing.md) — the pytest plugin, what to test
- [`serving.md`](src/seahaven/docs/serving.md) — `seahaven serve`, the client, control tools
- [`extensions.md`](src/seahaven/docs/extensions.md) — the extension contract
- [`projecttracker.md`](src/seahaven/docs/projecttracker.md) — a walkthrough of the reference world
- [`reference/`](src/seahaven/docs/reference/) — the API, every lint code, every CLI option

Every example on those pages is executed by the test suite (`tests/test_docs_examples.py`), because
an example that does not work is worse than no example.

## Performance

There is a benchmark, in `bench/`, with its results committed at `bench/results/latest.md`. Read the
caveats at the top of that file before quoting anything from it: the numbers are one machine, one
afternoon, in a shared sandbox, from a closed loop with no think time. They are useful for the order
of magnitude of a call and for comparison against another run of the same harness on the same
machine, which is how a regression is found. They are not a capacity model, not a service-level
objective, and not a comparison with anyone else's framework. Nothing in CI fails on a number.
