# Releasing Seahaven

This is the maintainer's checklist for publishing `seahaven` to PyPI. Only the framework is
published. The reference world and the example extension stay in this repository.

A version on PyPI is permanent. PyPI refuses a second upload of a version, even after the first
upload is deleted, so test the built wheel in step 3 before you publish it in step 4.

1. [Bump the version](#1-bump-the-version)
2. [Build](#2-build)
3. [Test the wheel](#3-test-the-wheel)
4. [Publish](#4-publish)
5. [Test the release from PyPI](#5-test-the-release-from-pypi)
6. [Tag the release](#6-tag-the-release)

## 1. Bump the version

On a branch, set `version` in `pyproject.toml`, then update the lock file:

```sh
uv lock     # prints "Updated seahaven vA -> vB"
```

Run every check in [AGENTS.md](../AGENTS.md#automated-checks), in both environments. Open a pull
request and merge it.

`seahaven new` writes `seahaven~=<major>.<minor>` of the installed version into each new world, so
a new minor version changes what new worlds depend on. The bundled docs in `src/seahaven/docs/` ship
inside the wheel: if the release changes how Seahaven is installed or run, update them in the same
pull request.

## 2. Build

Build from a clean, up-to-date `main`:

```sh
git checkout main && git pull
git status                 # must be clean
uv run python -V           # 3.14.0 or newer, with no "rc"
rm -rf dist
uv build --no-sources
uvx twine check dist/*     # the README renders on PyPI
```

At the repository root, `uv build` builds only `seahaven`. `--no-sources` ignores
`[tool.uv.sources]`, so the build resolves the way a user's install does.

The remaining steps use two shell variables. Set them in the repository root:

```sh
V=0.5.0       # the version from step 1
D=$PWD/dist
```

## 3. Test the wheel

Until the version is on PyPI, uv can find it only in `dist/`. Pass `--find-links $D` to every
`uv sync`, and `--no-sync` to every `uv run`. Without them, uv resolves `seahaven` from PyPI and
fails, because the new version is not there yet.

Scaffold a world from the wheel and run its tests:

```sh
cd "$(mktemp -d)"
uvx --from $D/seahaven-$V-py3-none-any.whl seahaven new crm_world
cd crm_world
uv sync --find-links $D
uv run --no-sync pytest
uv run --no-sync seahaven check
```

Call the world's tools in process:

```sh
uv run --no-sync python -c '
from crm_world import world
with world.instance() as i:
    item = i.call("create_item", name="Acme")
    print(item, i.call("get_item", item_id=item["id"]))'
```

Serve the world over OpenEnv. The server takes about 10 seconds to start:

```sh
uv sync --find-links $D --extra serve
uv run --no-sync seahaven serve
```

Open http://127.0.0.1:8000/console to see the web console. Then, in a second terminal in the same
directory, call the server with the client:

```sh
uv run --no-sync python -c '
from seahaven.openenv import SeahavenClient
with SeahavenClient(base_url="http://127.0.0.1:8000") as env:
    env.reset(seed=7)
    print([t["name"] for t in env.list_tools()])
    item = env.call("create_item", name="Acme").result
    print(env.call("get_item", item_id=item["id"]).result)
    print(env.call("get_item", item_id="nope").seahaven_error["code"])'
```

It prints `['create_item', 'get_item']`, then the item, then `NOT_FOUND`. Stop the server.

Serve the world over MCP. The `serve` and `mcp` extras cannot be installed together, so this sync
replaces the `serve` extra:

```sh
uv sync --find-links $D --extra mcp
claude mcp add crm_world -- uv run --no-sync seahaven mcp
```

Start `claude` in the same directory and ask it to create an item called Acme, then fetch it. Remove
the server when you are done:

```sh
claude mcp remove crm_world
```

## 4. Publish

Upload with a PyPI API token for the `seahaven` project:

```sh
uv publish $D/seahaven-$V*     # the token from --token or UV_PUBLISH_TOKEN
```

## 5. Test the release from PyPI

This follows the README quickstart, so it tests what a new user does. The flags from step 3 are not
needed now:

```sh
cd "$(mktemp -d)"
uvx --refresh seahaven@$V new crm_world
cd crm_world
uv sync
uv run pytest
uv run seahaven check
uv run python -c 'import seahaven; print(seahaven.__version__)'   # prints the new version
```

`--refresh` stops uvx from using a cached older release. If uv does not find the new version yet,
wait a minute for PyPI's CDN and try again.

Then run the servers from the published package. Use the client command from step 3 against the
first. Each command runs until you press Ctrl+C:

```sh
uv run --extra serve seahaven serve
uv run --extra mcp seahaven mcp
```

## 6. Tag the release

From the repository root:

```sh
git tag v$V
git push origin v$V
```

On GitHub, create a release from the tag, under Releases, then Draft a new release. Generate the
release notes, and edit them.
