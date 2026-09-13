# Seahaven

Seahaven is a framework for building **synthetic worlds**: faithful, stateful mocks of the tool
surface a real company's agent works against, on SQLite, that agents work against in evals.

`seahaven docs` prints the directory this page is in. It ships inside the installed `seahaven`
package, so it always matches the version you have.

## The pages are still being written

The layout below is fixed and every page exists, but the prose arrives with the documentation phase
of Seahaven's own implementation plan. Until it does, the specifications under
`specs/projects/seahaven_framework/` in the Seahaven repository are the source of truth —
`project_overview.md`, then `functional_spec.md`, then `architecture.md` and `components/` — and
`reference/lints.md` here is complete.

## Reading order

| Page | What it covers |
|---|---|
| [concepts.md](concepts.md) | World, fixture, instance, tool, clock, reproducibility, changesets |
| [authoring.md](authoring.md) | Writing tools, errors and the error handler, middleware, startup hooks, schema rules |
| [fixtures.md](fixtures.md) | Freezing, forking, generators, descriptions for eval authors |
| [testing.md](testing.md) | The pytest plugin, what to test in a world |
| [serving.md](serving.md) | `seahaven serve`, the OpenEnv client, control tools, publishing to a hub |
| [extensions.md](extensions.md) | The extension contract, the XML-RPC example |
| [projecttracker.md](projecttracker.md) | A walkthrough of the reference world |

Reference:

| Page | What it covers |
|---|---|
| [reference/api.md](reference/api.md) | The public API |
| [reference/lints.md](reference/lints.md) | Every `SHnnn` code: rule, why, fix |
| [reference/cli.md](reference/cli.md) | Every subcommand and option |

## The commands

```
seahaven new <name> [--hub]   # scaffold a world
seahaven check                # run every lint; do this before a commit
seahaven fixture list         # every fixture: id, parent, now, description
seahaven serve                # run this world's OpenEnv server
seahaven docs                 # print this directory
```
