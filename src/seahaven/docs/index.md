# Seahaven

Seahaven is a framework for building **synthetic worlds**: faithful, stateful mocks of the tool
surface a real company's agent works against, on SQLite, that agents work against in evals.

`seahaven docs` prints the directory this page is in. It ships inside the installed `seahaven`
package, so it always matches the version you have.

## These pages are not written yet

This is a stub. The authoring documentation is written in a later phase of the implementation plan,
and until then the specifications under `specs/projects/seahaven_framework/` in the Seahaven
repository are the source of truth — `project_overview.md`, then `functional_spec.md`, then
`architecture.md` and `components/`.

The pages that will live here:

| Page | What it covers |
|---|---|
| `index.md` | What Seahaven is, the reading order, the commands |
| `concepts.md` | World, fixture, instance, tool, clock, reproducibility, changesets |
| `authoring.md` | Writing tools, errors and the error handler, middleware, startup hooks, schema rules |
| `fixtures.md` | Freezing, forking, generators, descriptions for eval authors |
| `testing.md` | The pytest plugin, what to test in a world |
| `serving.md` | `seahaven serve`, the OpenEnv client, control tools, publishing to a hub |
| `extensions.md` | The extension contract, the XML-RPC example |
| `reference/api.md` | The public API |
| `reference/lints.md` | Every `SHnnn` code: rule, why, fix |
| `reference/cli.md` | Every subcommand and option |
| `projecttracker.md` | A walkthrough of the reference world |

## The commands, meanwhile

```
seahaven new <name> [--hub]   # scaffold a world
seahaven check                # run every lint; do this before a commit
seahaven fixture list         # every fixture: id, parent, now, description
seahaven serve                # run this world's OpenEnv server
seahaven docs                 # print this directory
```
