# CLI reference

The whole command tree, as a synopsis. Every command is spelled out below it.

```text
seahaven new <name> [--dir <path>] [--hub]
seahaven check [--world module:attr]
seahaven docs
seahaven fixture list [--world module:attr]
seahaven fixture freeze <id> --run module:function --description <text> [--now <ts>] [--world module:attr]
seahaven fixture fork <parent> <id> --run module:function --description <text> [--world module:attr]
seahaven serve [--host HOST] [--port PORT] [--max_concurrent_envs N] [--concurrency N]
               [--session-timeout SECONDS] [--include-control-tools] [--no-console]
               [--world module:attr]
seahaven mcp [--fixture NAME] [--seed N] [--now ISO] [--reset-options JSON] [--world module:attr]
```

Every command exits `0` on success and `1` on failure, and a failure is one line on stderr with the
fix in it, never a traceback. `seahaven check` also exits 1 when it found an error-severity finding,
which is what makes it usable in CI. An option that does not exist, or a missing argument, is
argparse's own usage error and exits 2.

| Command | What it does |
|---|---|
| [`seahaven new`](#seahaven-new-name) | scaffold a world |
| [`seahaven check`](#seahaven-check) | run every lint |
| [`seahaven docs`](#seahaven-docs) | print the bundled docs directory |
| [`seahaven fixture list`](#seahaven-fixture-list) | every fixture of this world |
| [`seahaven fixture freeze`](#seahaven-fixture-freeze-id) | fill a blank instance and freeze it |
| [`seahaven fixture fork`](#seahaven-fixture-fork-parent-id) | fill an instance of a fixture and freeze the result |
| [`seahaven serve`](#seahaven-serve) | run this world's OpenEnv server |
| [`seahaven mcp`](#seahaven-mcp) | serve this world to one MCP client over stdio |

## Finding the world

Every command except `new` and `docs` acts on a world, and finds it the same way: the nearest
`pyproject.toml` from the working directory, the package its `[project] name` normalises to, and the
attribute `world` on that package. A package that is not importable, or whose `world` is missing or
is not a `seahaven.World`, is an error naming the package and the fix.

`--world module:attr` overrides the convention for a layout it misses. `--world mypkg.world:world`
names the module the `World` is built in, and the lints climb out of it to the package around it,
which is where `schema/`, `tools/` and `middleware/` are. The pytest plugin has the same override,
spelled `--seahaven-world`, because pytest's option namespace is shared with every other plugin.

Paths in output are printed relative to the *project*, not to wherever the shell happened to be. So
`seahaven check` run from `src/mypkg/middleware/` and from the project root describe the same file
the same way.

## `seahaven new <name>`

Renders a complete world and stops. One table, two tools, an error handler, `errors.py`, `world.py`,
`openenv_app.py`, one passing test, an empty `fixtures/`, `README.md`, `AGENTS.md`, `.gitignore` and
the generator script. No fixture is built and nothing is imported.

| Option | What it does |
|---|---|
| `--dir <path>` | the directory to create the world in; the default is here |
| `--hub` | also write the five files `openenv push` requires: `openenv.yaml`, a root `Dockerfile`, a root `__init__.py`, `client.py` and `models.py` |

The package name is the world's name, normalised. `AGENTS.md` is written once and never touched
again. It is an ordinary file the world owns, and it points an authoring agent at these docs.

```sh
seahaven new notes
seahaven new notes --dir ~/projects --hub
```

The command refuses an existing directory rather than merging into one. It also refuses a name that
does not make a Python package name, and a name `World(name=...)` would not accept — the name
reaches the scaffolded `world.py` verbatim, so a world it cannot import is refused before the
directory is written.

The command finishes by printing what to do next. While Seahaven is unpublished, the `uv sync` in
that list is a trap: the scaffold's `seahaven~=0.0` resolves to a placeholder release that contains
none of the framework, so the sync succeeds and the world fails at import with `ModuleNotFoundError:
No module named 'seahaven.world'`. `seahaven check` itself cannot start either, because the
placeholder ships no console script. Install the framework from a checkout until publication. The
same applies to `--hub`: the `Dockerfile` it writes runs `uv sync --extra serve`, which today builds
an image whose container cannot start.

## `seahaven check`

Runs every lint over the world and prints each finding, sorted by code, then path, then line.

```
SH101 error src/badworld/schema/001_items.sql:8  table 'items' is not STRICT  fix: append STRICT to the CREATE TABLE items
SH201 warning src/badworld/tools/items.py:26  datetime.datetime.now() reads the wall clock, not the instance's  fix: take the instance's time from ctx.clock.iso() or ctx.clock.now()
SH301 error src/badworld/tools/orphan.py  module 'badworld.tools.orphan' is never imported, so it registers nothing  fix: import it from badworld/__init__.py, or from badworld.tools
```

It exits 1 if any finding is an `error`. Warnings alone exit 0. Run it before every commit. Every
rule is in [lints.md](lints.md).

## `seahaven docs`

Prints the directory holding the bundled docs for the installed version, and nothing else, so it
composes:

```sh
seahaven docs
```

```
cat "$(seahaven docs)/index.md"
```

The docs ship inside the installed package, so what this prints always matches the version
installed. That is why a scaffolded world's `AGENTS.md` says to run this rather than to search the
web for a framework that is not in an agent's training data.

## `seahaven fixture list`

Every fixture of the world, one per line, tab-separated: id, parent (`-` when there is none), `now`,
and the description.

```
agency	-	2026-06-01T09:00:00.000Z	A twelve-person agency: three teams, nine projects ...
empty	-	2026-06-01T09:00:00.000Z	The tracker's schema with no rows. Start here to write a history ...
small_startup	-	2026-06-01T09:00:00.000Z	A three-person startup's tracker: one engineering team ...
```

A world with no fixtures prints nothing and exits 0.

## `seahaven fixture freeze <id>`

Creates a blank instance from the world's schema, runs the generator against it, and freezes the
result to `fixtures/<id>/`.

| Option | What it does |
|---|---|
| `--run module:function` | **required.** The generator: a function taking the live instance and filling it |
| `--description <text>` | **required.** What the fixture holds and what scenarios it supports, for eval authors |
| `--now <ts>` | the instant to freeze the clock at; the default is the wall clock at creation |

```sh
seahaven fixture freeze empty \
    --now 2026-06-01T09:00:00.000Z \
    --run fixtures_src.generate:empty \
    --description "The schema with no rows. Start here to write a history."
```

**Pass `--now`, and pass the same value every time.** Without it the fixture is dated from the day
it was built, and nothing will tell you, because a wall-clock instant is a perfectly valid
timestamp. See [../db_schema_and_fixtures.md](../db_schema_and_fixtures.md).

The command refuses an id whose directory already exists.

## `seahaven fixture fork <parent> <id>`

Creates an instance of `parent`, runs the generator against it, and freezes the result with
`parent_id` set. It takes the same `--run` and `--description`, and no `--now`: a fork inherits its
parent's clock, which is the point of forking.

```sh
seahaven fixture fork empty small_startup \
    --run fixtures_src.generate:small_startup \
    --description "A three-person startup's tracker: one team, two projects, forty issues."
```

## `seahaven serve`

Runs the OpenEnv server for this world: one world, many sessions, one worker process. It needs the
`serve` extra; without it the command says so and exits 1.

| Option | Default | What it does |
|---|---|---|
| `--host HOST` | `0.0.0.0` | the address to bind |
| `--port PORT` | `8000` | the port to bind |
| `--max_concurrent_envs N` | `500` | how many sessions may be open at once |
| `--concurrency N` | `min(cpus, 16)` | how many tool calls run at once; `0` for no gate |
| `--session-timeout SECONDS` | `3600` | seconds of idleness before a session is reaped; `0` disables the reaper |
| `--include-control-tools` | off | make each session's instance one that can call `controller_run_sql`; without the flag the name is an unknown tool. It is never listed either way, and it is deprecated in favour of the state document -- run Python with `-W default::DeprecationWarning` to see the warning |
| `--no-console` | off | do not serve the web console at `/console`; the address is otherwise printed when the server starts |

```sh
seahaven serve
seahaven serve --host 127.0.0.1 --port 9000
seahaven serve --include-control-tools --concurrency 0
```

`--max_concurrent_envs` is spelled with underscores because it is OpenEnv's own option name, and a
second spelling here would be one more thing to translate.

Read [../serving_and_openenv.md](../serving_and_openenv.md) before choosing `--concurrency`. The
gate is unfair whenever it binds, and the default is not exempt.

## `seahaven mcp`

Serves this world to one MCP client over stdio: one world, one instance, one episode, for the life
of the process. It needs the `mcp` extra; without it the command says so and exits 1. The `mcp` and
`serve` extras cannot be installed together, so an environment holds one or the other.

| Option | Default | What it does |
|---|---|---|
| `--fixture NAME` | a blank instance | the fixture the instance starts from |
| `--seed N` | a random seed, written to stderr | the caller seed, an integer; the framework refuses a negative one, and one that does not fit in 8 bytes |
| `--now ISO` | wall time at creation | the clock a blank instance starts at; the framework refuses it together with a fixture |
| `--reset-options JSON` | none | a JSON object of the keyword arguments `world.instance()` is called with; not allowed with `--fixture`, `--seed` or `--now` |
| `--world module:attr` | the convention | which world to serve |

```sh
seahaven mcp
seahaven mcp --fixture small_startup --seed 7
seahaven mcp --reset-options '{"fixture": "agency", "seed": 7, "startup": {"reviewer": "ada"}}'
```

Every option except `--world` also answers to an environment variable, because an MCP client
configuration passes `env` more comfortably than `args`. A flag beats the matching variable,
silently: the flag is the more specific of the two, and a warning about a variable somebody set in a
client configuration months ago would be noise on every launch. `--world` has no variable, because
it names the code to import, which belongs beside the command in the configuration file.

| Variable | The same as |
|---|---|
| `SEAHAVEN_FIXTURE` | `--fixture` |
| `SEAHAVEN_SEED` | `--seed` |
| `SEAHAVEN_NOW` | `--now` |
| `SEAHAVEN_RESET_OPTIONS` | `--reset-options` |

A variable set to the empty string counts as unset. A client configuration is a JSON file people
copy and edit, and an `env` entry left as `""` there means "not this one".

**`--reset-options` is the general door**, and the only one that reaches a world's own startup
keywords: it spells the keyword arguments `world.instance()` is called with, as JSON. `--fixture`,
`--seed` and `--now` are convenience spellings of three of those keyword arguments, because JSON
inside an `.mcp.json` `args` array is painful to quote. Giving both is refused rather than merged,
and the refusal names the sources actually given, in the spelling they were given in. The message is
one line:

```
--fixture cannot be combined with --reset-options; put "fixture" inside the --reset-options JSON instead: --reset-options '{"fixture": "small_startup", "seed": 7}'
```

**A world's own startup keywords go inside `"startup"`**, which is the namespace `world.instance()`
gives them ([../serving_and_openenv.md](../serving_and_openenv.md#sessions-and-instances) covers
that namespace):

```sh
seahaven mcp --reset-options '{"fixture": "small_startup", "startup": {"user_id": "u_12"}}'
```

The document takes five keys and no others: `"fixture"`, `"seed"`, `"now"`, `"state_format"` and
`"startup"`. A key `world.instance()` does not take is refused on the command line, before anything
is served, rather than reaching the client as a `TypeError` once it has connected:

```
--reset-options does not take "user_id"; world.instance() takes "fixture", "now", "seed", "startup" and "state_format", and a world's own startup keywords go inside "startup": --reset-options '{"fixture": "small_startup", "startup": {"user_id": "u_12"}}'
```

There is no `--state-format`. The state document is not published over MCP, so the format it would
be rendered in has no reader; a world that wants a different one for some other reason passes
`state_format` through `--reset-options`. There is no `--include-control-tools` either: `serve` has
that flag because a harness drives the server it started, and nothing reaching an MCP client may run
SQL against the world. `world.instance()` takes a `control_tools` keyword, and `--reset-options` is
the one key of its own that it refuses rather than passes:

```
--reset-options does not take "control_tools": seahaven mcp publishes the world's own tools and nothing else, and nothing reaching an MCP client may run SQL against the world
```

An instance this command makes is therefore made without control tools, so a control tool's name is
an unknown tool on it, in the words any name the world does not have earns.

**No `--seed` means a random seed**, not the constant `world.instance()` falls back to. The command
picks an integer, uses it, and writes it to stderr, so a run can be asked for again:

```
no seed was given, so this run uses --seed 1481765302
```

These are refused before anything is served, as one line on stderr and exit 1: a `--reset-options`
document that is not valid JSON, one that is valid JSON but not an object, one that names a key
`world.instance()` does not take or the `"control_tools"` it withholds, a `--seed` that is not an
integer, a `--world` that is not `module:attr`, and the mixing above.

Everything that needs the world is checked once the client has connected, and reaches it as a
JSON-RPC error: an import that fails, a fixture that does not exist, a startup keyword no startup
hook names, a startup hook that raises, and the two values `world.instance()` refuses itself:
`--now` given together with a fixture, and a `--seed` that is negative or does not fit in 8 bytes. A
process that could not make its instance answers that error and then exits 1.

Read [../serving_and_openenv.md](../serving_and_openenv.md#serving-one-world-to-an-mcp-client) for
the `.mcp.json` that launches the command, and for what a client is and is not given.

## Using it from Python

Nothing in the CLI is a hidden entry point. `seahaven.cli.main(argv)` is the whole command, and
`seahaven.cli.find_world(explicit, start=...)` is the discovery. For a harness that wants a server
inside its own process without going through `argv`, `seahaven.openenv.serve.serve(world, ...)` is
the three lines `serve` runs, and `seahaven.mcp.serve(world, reset_options=...)` is what `mcp` runs.
