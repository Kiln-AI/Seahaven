# Component: Phase 1, low-risk code and test batch

Eight items: 11.M1, 7.M2, 5.M3, 6.M1, 4.M2, 10.M1, 10.M3, 10.M4. Each was prototyped on a scratch
copy of the tree at `38c6f3c`; the framework, projecttracker and xmlrpc suites pass with the
changes below apart from the test changes each section names. Line numbers are at that commit.

## 1. 11.M1 — `synchronous=OFF` before the WAL switch

**Change.** `src/seahaven/db.py::open_instance` (l.262-267):

```py
conn = apsw.Connection(str(path))
_harden(conn)
# An instance directory does not survive a crash: the sweep deletes it. So its
# files need no durability, and at FULL the WAL conversion of a copied fixture
# fsyncs on every creation.
conn.pragma("synchronous", "OFF")
conn.pragma("journal_mode", "WAL")
```

Order matters: OFF must precede `journal_mode=WAL`. Measured: WAL switch 1.25 ms -> 0.11 ms on a
bare copy; `world.instance("agency")` median 5.5 ms -> 2.6 ms. A counting VFS
shows 4 `xSync` calls on open+one write at the shipped order, 0 with the new one.

**Other opens, checked:**
- `open_inspection` — read-only (`mode=ro`), never converts or writes; unaffected, leave it.
- `fixtures.py::_vacuum_into` — runs on the instance connection; SQLite never fsyncs `VACUUM INTO`
  output whatever the setting, so freeze durability is unchanged (it was never fsynced).
- `_build`/`_plain_connection` (`build_blank`) — for a *blank* instance this writes the instance's
  own throwaway file at default FULL with a rollback journal. **Decision: set `synchronous=OFF` on
  the builder too** (same reasoning; a further ~0.8 ms per blank instance, 5.3 -> 4.6 ms). The same
  builder also serves `:memory:` builds (`World.__init__`, lint, conformance), where the pragma is a
  no-op. Add a blank-instance case to the no-fsync test.
- composite nodes: each node goes through `_open_node` -> `open_instance`, so all get it.
- `bench/results/latest.md` §7 ("about 3 ms per node") will be stale; rerun is optional.

**Tests.**
- `tests/test_db.py::test_an_instance_connection_is_hardened`: `synchronous` expectation `1` -> `0`
  (this is the only existing test that breaks; confirmed on the copy).
- New `tests/test_db.py::test_opening_an_instance_file_does_not_fsync` — register a counting
  `apsw.VFS` as default (base `apsw.vfs_names()[0]`, `makedefault=True`, `unregister()` in a
  fixture finaliser; `xOpen` returns an `apsw.VFSFile` subclass that counts `xSync`), build a
  rollback-journal file with a plain connection, then `open_instance` it and run one committed
  `INSERT`. Assert zero syncs. Fails on the shipped order (4 syncs).
- New `tests/test_instances.py::test_every_node_of_a_fixture_instance_opens_without_durability`
  (real entry point): freeze a fixture of a composite (`rooted` + one `add_world`), then
  `with world.instance(fixture) as live, live.bulk() as ctx:` assert
  `ctx.db.conn.pragma("synchronous") == 0` and the same for `ctx.worlds.<name>.db.conn`, and
  `journal_mode == "wal"` for both.

## 2. 7.M2 — `seahaven serve` binds loopback by default

**Change.**
- `src/seahaven/openenv/serve.py`: `DEFAULT_HOST = "127.0.0.1"`; replace the comment with the
  constraint: `/ws` has no authentication, so a server is reachable from the network only when
  the operator says so (`--host 0.0.0.0`); the scaffolded container passes it itself.
- `src/seahaven/cli/serve.py:29`: help text `(default 127.0.0.1)`.
- `console_url` unchanged (still maps `0.0.0.0`/`::` to loopback).

**Dockerfile.** `src/seahaven/cli/templates/hub/Dockerfile.tmpl:13` does not run `seahaven serve`
at all: `CMD ["uv", "run", "uvicorn", "$package.openenv_app:app", "--host", "0.0.0.0", ...]`, so it
is unaffected. It is the only Dockerfile/template in the repo.

**Every place that states the default** (all to change):
- `src/seahaven/docs/serving_and_openenv.md:79` — row becomes `127.0.0.1` | "the address to bind.
  Pass `--host 0.0.0.0` to serve on every interface, as a container does". Line 74's example
  `seahaven serve --host 127.0.0.1 --port 9000` should become `--host 0.0.0.0 --port 9000`.
- `src/seahaven/docs/reference/cli.md:177` (`0.0.0.0` -> `127.0.0.1`) and the example at l.187
  (same swap as above).
- `tests/test_serve.py:84-85` — the literal pair becomes `("127.0.0.1", 8000)`; l.154 docstring
  ("The default bind is `0.0.0.0` ... names loopback") reword: the default is loopback, and
  `console_url` owns the `0.0.0.0` translation. Add to that test a `serve(world, host="0.0.0.0")`
  case asserting the printed URL is still `http://127.0.0.1:8000/console`.
- `tests/test_cli_serve.py:94` compares with `serving.DEFAULT_HOST` — no change needed.
- `docs/http_apis.md:196` already says `127.0.0.1` (now consistent).

**New test.** `tests/test_cli_new.py` (next to l.255): the scaffolded hub `Dockerfile` CMD contains
`"--host", "0.0.0.0"` — the container's reachability no longer rides on the default, so pin it.

## 3. 5.M3 — `step(ListToolsAction())` parses as `ListToolsObservation`

**Change.** `src/seahaven/openenv/client.py::_parse_result`:

```py
observation = payload.get("observation", {})
# A tool list is the one reply with a `tools` field; a reset carries its tool
# count in `metadata`, never at the top level.
model = ListToolsObservation if "tools" in observation else SeahavenObservation
return StepResult(observation=model.model_validate(observation), ...)
```

Import `ListToolsObservation` from `openenv.core.env_server.mcp_types`. Rewrite the docstring's last
paragraph ("does not come through here") and trim `list_tools`'s docstring (it still reads the frame
directly and answers `list[dict]`; that stays).

**Types.** No annotation changes. The client is `EnvClient[..., Observation, SeahavenState]`,
`_parse_result -> StepResult[Observation]`, and `ListToolsObservation` subclasses `Observation`, so
`step()` already types as `StepResult[Observation]`; a caller narrows with `isinstance`. Overloading
`step` per action type is possible but the base class's dual-mode `step` makes it awkward; not
proposed. Confirmed live: today `ValidationError`; patched, `ListToolsObservation` with
`Tool` models, and a following `CallToolAction` step still parses as `SeahavenObservation`.

**Tests** (`tests/test_client.py`):
- Rewrite `test_the_observation_model_refuses_a_frame_it_does_not_know`: it pins the bug with
  `{"observation": {"tools": []}}`. Use an unknown key instead (`{"observation": {"bogus": 1}}`,
  `match="bogus"`).
- New unit `test_parse_result_answers_a_tool_list_as_a_list_tools_observation`: frame
  `{"observation": {"tools": [{"name": "ping", "description": "d", "input_schema": {}}]}}` ->
  `isinstance(..., ListToolsObservation)`, `tools[0].name == "ping"`.
- New live `test_stepping_a_list_tools_action_answers_the_tool_list(world)`: `serving(world)`,
  `env.reset()`, `result = env.step(ListToolsAction())`, assert `ListToolsObservation`, `"rows"` in
  names and `controller_run_sql` not; then `env.call("rows", sql="SELECT 1 AS n").result` still
  works on the same session.
- Optional doc line in `docs/reference/api.md` near l.611: `step(ListToolsAction())` answers a
  `ListToolsObservation`.

## 4. 6.M1 — an import error names its file, line and the right fix

**Change** in `src/seahaven/cli/__init__.py`:
- `CliError.__init__` gains `line: int | None = None` and `fix: str | None = None` (stored).
- `_import` `except Exception` branch -> `raise _import_failure(module_name, error, root) from error`.
  The `SeahavenError` non-DDL SH501 branch also gets a location (same helper) and its own fix.
- `_no_world` raise passes `fix="give the package a world, or point at the one it has"` — the only
  place that text remains.
- New helpers:

```py
def _import_failure(module_name, error, root) -> CliError:
    summary = _last_traceback_line(error)          # "SyntaxError: invalid syntax"
    if isinstance(error, ModuleNotFoundError) and _is_the_world_module(module_name, error.name):
        return CliError(f"cannot import {module_name!r}: {summary}", code="SH501", path=root,
            fix=f"make {module_name!r} importable (the package the [project] name names, under "
                "src/), or point at the world with --world module:attr")
    place = _world_frame(error, root)
    command = f"python -c 'import {module_name}'"
    if place is None:
        return CliError(f"cannot import {module_name!r}: {summary}; {command} prints the "
            "whole traceback", code="SH501", path=root, fix=f"fix the error {command} ends in")
    file, line = place
    where = f"{_shown(file, root)}:{line}"
    return CliError(f"cannot import {module_name!r}: {summary}, at {where}; {command} prints the "
        "whole traceback", code="SH501", path=file, line=line, fix=f"fix the error at {where}")

def _world_frame(error, root) -> tuple[Path, int] | None:
    if isinstance(error, SyntaxError) and error.filename and error.lineno \
            and Path(error.filename).is_file():
        return Path(error.filename).resolve(), error.lineno   # not in the tb frames
    frames = [f for f in reversed(traceback.extract_tb(error.__traceback__)) if _authored(f)]
    under_root = [f for f in frames if Path(f.filename).resolve().is_relative_to(root)]
    chosen = (under_root or frames or [None])[0]
    return None if chosen is None else (Path(chosen.filename).resolve(), chosen.lineno)
```

`_authored(frame)`: the file exists (drops `<frozen importlib...>`), and is not under the `seahaven`
package directory, `sys.prefix` or `sys.base_prefix` (drops a project-local `.venv`, the stdlib and
installed libraries). `_is_the_world_module`: `name == module_name or
module_name.startswith(name + ".")`. `_shown` is the path relative to `root` when under it.

`src/seahaven/cli/check.py::_finding_for`: `Finding(..., path=path, line=error.line,
fix=error.fix or <no-world text>)` and delete the comment about the shared fix.
`src/seahaven/pytest_plugin.py`: no code change — it prints `str(error)`, which now carries the
location and the traceback hint; update its comment. Same for `main()` (serve, fixture, mcp).

Prototype on a copy of `tests/worlds/tidy`: a `def broken(:` in `tools/notes.py` gives
`src/tidy/tools/notes.py:23`; a bare `undefined_name` gives the same; a bad `World(...)` call in
`__init__.py` gives `src/tidy/__init__.py:9`; a missing top-level module gives no frame.

**Tests.** The broken worlds are written into `tmp_path` by the test (a committed file with a
syntax error would break `ruff format --check`, `ruff check` and `ty`): copy `WORLDS / "tidy"` with
`shutil.copytree(..., ignore=ignore_patterns("__pycache__"))` and append the bad line. Modules use
`isolated_imports`, which already purges worlds under a temp dir.
- `tests/test_find_world.py::test_a_syntax_error_in_world_code_names_its_file_and_line` — `discover`
  raises `CliError` with `code == "SH501"`, `path == .../src/tidy/tools/notes.py`,
  `line == <appended line>`, `"SyntaxError" in message`, `"src/tidy/tools/notes.py:" in message`,
  `"world = seahaven.World" not in (fix + message)`.
- `...::test_a_name_error_in_world_code_names_its_file_and_line` — same for `NameError`.
- `...::test_a_missing_dependency_is_placed_at_the_import_that_needs_it` — `import not_installed_x`
  in `tools/notes.py`: location is that line (distinguishes it from the missing-world case).
- Keep `test_a_world_option_naming_a_module_that_does_not_import_is_sh501`; add
  `assert "--world" in raised.value.fix`.
- `tests/test_cli_check.py::test_an_import_error_is_a_finding_at_the_line_that_raised` — via
  `check(tmp_world, monkeypatch, capsys)` (the real `seahaven check` path): exit 1, one line
  matching `FINDING_LINE`, starting `SH501 error src/tidy/tools/notes.py:<n>`, `fix: fix the error
  at src/tidy/tools/notes.py:<n>`. Rename/extend `test_an_import_error_is_sh501_with_the_last_
  traceback_line` (still asserts no `\n`).
- `tests/test_cli_check.py::test_a_package_with_no_world_is_sh501` — add
  `"fix: give the package a world" in line` (pins that the old fix stays there only).
- `tests/test_pytest_plugin.py::test_a_world_that_does_not_import_fails_with_its_file_and_line` —
  `write_world(pytester)`, append `"\nundefined_name\n"` to `src/tinyworld/__init__.py`, a test
  using `instance` with a marker; `runpytest()` -> `assert_outcomes(errors=1)`, and
  `result.stdout.fnmatch_lines(["*NameError*src/tinyworld/__init__.py:*"])`.
- Docs: `docs/reference/lints.md:357-358` — an import that fails for another reason is reported with
  the exception's type and message at the file and line in the world's code that raised it, and the
  fix names that place; `python -c "import <package>"` prints the whole traceback.

## 5. 4.M2 — startup hooks in a hosts-first order

**Algorithm** (new `composition.py::hosts_first(composition) -> list[Node]`, exported; replaces
`canonical_tree` in `instances.py::_run_startup_hooks`):

```py
remaining = {node: 0 for node in composition.nodes}
for node in composition.nodes:
    for child in dict.fromkeys(node.added.values()):   # distinct children, add_world order
        remaining[child] += 1                          # = number of distinct hosts
order = []
def visit(node):
    order.append(node)
    for child in dict.fromkeys(node.added.values()):
        remaining[child] -= 1
        if remaining[child] == 0:                     # its last host has just run
            visit(child)
visit(composition.root)
```

A depth-first preorder from the root in `add_world` order, where a node is emitted only when the
last of its hosts has been emitted (Kahn's algorithm with a DFS stack). `node.added` holds every
edge, alias edges included. `dict.fromkeys` dedups a host that adds one node twice under two names.

**Correctness.** (a) Every node is emitted: the graph is a DAG, and a node whose hosts are all
emitted reaches `remaining == 0` during the last host's visit. (b) Hosts first: a node is emitted
only after its last host's `visit` began, and each host was emitted when it began. (c) Once each: a
node is visited only on the transition to 0. (d) **Same as today without sharing:** with no node
reached by two distinct hosts, every non-root node has `remaining == 1`, so `visit(child)` runs as
soon as its only host reaches that edge, in `add_world` order — exactly `canonical_tree`, which in
that case descends every edge because every route equals the child's path. By induction on depth,
the two sequences are identical. A node added twice by *one* host keeps today's order too, because
hosts are counted distinctly. Add an assert `len(order) == len(composition.nodes)`.

Examples: company adds payments, shop; shop adds payments -> `company, shop, payments` (today
`company, payments, shop`). `test_a_node_two_hosts_reach_runs_its_hooks_once` (host adds middle,
shared; middle adds shared) stays `host, middle, shared`.

Keep `canonical_tree` (still exported; only this call site used it). Update `_run_startup_hooks`'s
docstring and `docs/composition.md:401-405`: "Hooks run once per node, from the root: a node's own
hooks, then its added worlds in `add_world` order, and a world that more than one world adds runs
after every world that adds it."

**Tests** (`tests/test_composite_instance.py`; the prototype passes these and all suites):
- `test_a_shared_node_runs_after_every_world_that_adds_it` — the company/payments/shop shape above
  with `recording`, `tool_prefix` on one route; assert `["company", "shop", "payments"]`. Fails on
  today's code (verified).
- `test_a_host_that_seeds_a_shared_node_seeds_it_composed_as_alone` — shop's hook writes
  `ctx.worlds.payments.state["plan"] = "merchant"`; payments' hook does
  `ctx.state.setdefault("plan", "free")` and records it; assert `"merchant"` both for
  `shop.instance(None)` and for `company.instance(None)`.
- Unit, `tests/test_composition.py::test_hosts_first_is_the_canonical_preorder_without_sharing`:
  for a three-level tree with no sharing, `hosts_first(resolve(root)) ==
  list(canonical_tree(resolve(root).root))`.

## 6. 10.M1 — the call-log copy test that can fail

Replace `tests/test_call_log.py::test_a_tool_that_mutates_its_arguments_does_not_alter_the_record`
with two tests, both through `world.instance(None)`:

```python
def test_a_caller_that_changes_its_list_after_the_call_does_not_alter_the_record(tmp_path):
    world = build_world(tmp_path)
    @world.tool
    def count(ctx: Ctx, tags: list[str]) -> int:
        """Count the tags."""
        return len(tags)
    with world.instance(None) as instance:
        given = ["a", "b"]
        assert instance.call("count", tags=given) == 2
        given.append("later")
        (record,) = instance.call_log()
        assert record.arguments == {"tags": ["a", "b"]}

def test_a_middleware_that_changes_an_argument_in_place_does_not_alter_the_record(tmp_path):
    ...  # same tool, plus:
    @world.middleware
    def extend(ctx: Ctx, call: Call, next_: Handler) -> object:
        call.arguments["tags"].append("added")   # the caller's list, before validation
        return next_(ctx, call)
    # instance.call("count", tags=["a", "b"]) == 3; record.arguments == {"tags": ["a", "b"]}
```

Verified on the copy: both pass as shipped; with `given = arguments` at `instances.py:767` both fail
(and every existing test in the module still passes, confirming the finding). A shallow
`dict(arguments)` also fails both, so they pin the deep copy.

## 7. 10.M3 — typed call by reference through a handle, diamond

Add to `tests/test_typed_call.py` after `test_a_handle_reaches_a_function_of_a_world_in_its_subtree`:

```python
def test_a_handle_reaches_a_function_of_a_node_two_routes_beneath_it_reach(tmp_path):
    """One store reached twice under one scope is one account: the reference is not ambiguous."""
    host, _w, _r = rooted("host", tmp_path)
    middle, _mw, _mr = rowed("middle")
    left, _lw, _lr = rowed("left")
    right, _rw, _rr = rowed("right")
    leaf, leaf_write, leaf_read = rowed("leaf")
    left.add_world(leaf, name="leaf")
    right.add_world(leaf, name="leaf", tool_prefix="r_")   # two routes, one contribution each
    middle.add_world(left, name="left")
    middle.add_world(right, name="right")
    host.add_world(middle, name="middle")

    @host.tool
    def reach(ctx: Ctx, value: str) -> list[str]:
        """Reach the shared grandchild's store through the child, by reference."""
        ctx.worlds.middle.call(leaf_write, value=value)
        return ctx.worlds.middle.call(leaf_read)

    with host.instance(None) as live:
        assert live.call("reach", value="once") == ["once"]
        assert live.call("leaf_read") == ["once"]
        assert live.call("r_leaf_read") == ["once"]   # both routes read one store
```

The diamond has to sit *under* the handle (`middle`), since `_subtree` starts at the handle's node.
Verified: passes as shipped; with the `seen` check removed it raises `names more than one tool ...
(leaf_write at middle/left/leaf, leaf_write at middle/left/leaf)`.

## 8. 10.M4 — every relative link in the docs resolves

In `tests/test_docs.py`, replace `test_every_page_index_md_links_to_is_a_page_of_the_layout` with a
parametrised check over `PAGES` plus `README.md` and `CONTRIBUTING.md` (repo root via
`Path(__file__).resolve().parents[1]`; the suite always runs from a checkout, so no skip is needed).

- Strip fenced blocks (`^(```|~~~).*?^\1`, `re.S|re.M`) and inline code spans before matching, so
  code is never read as a link. Link pattern `\]\(([^)\s]+)(?:\s+"[^"]*")?\)`; skip `scheme:`
  targets (`https:`, `mailto:`).
- Split `path#fragment`. Empty path means the page itself. Resolve `(page.parent / path).resolve()`;
  assert it exists. For a bundled page, also assert the target is inside `DOCS` (the docs ship in
  the wheel, so a link out of `docs/` is dead there) and, for `.md`, is in `PAGES`.
- A fragment must be an anchor of the target: the GitHub slug of each heading outside fences
  (strip backticks and link markup, lowercase, drop characters that are not word, space or hyphen,
  spaces to hyphens, `-1`, `-2` suffixes for duplicates), plus any `<a id|name="...">`.
- Report every broken link in one assertion message (`page: target (missing|no anchor)`).
- Keep `test_a_link_out_of_the_docs_is_not_read_as_a_page_of_the_layout` against the new pattern
  and add a sample-based unit test for the slug (`"SH103 — a wall-clock ..."` ->
  `"sh103--a-wall-clock-..."`, a duplicate heading gets `-1`), so a slug bug cannot pass silently.
- Update the module docstring and the comment above `_PAGE_LINK` (it says in-page anchors are not
  handled).

Prototype: 249 relative links (140 in-page anchors, 88 page links, 21 page#anchor) and
15 external across the 15 pages + README + CONTRIBUTING; all resolve today, and no bundled page links
out of `docs/`.

## Decisions (settled during planning)

- **11.M1:** the blank-instance builder also gets `synchronous=OFF` (see §1).
- **6.M1:** a non-schema `SeahavenError` raised at import (for example a duplicate tool name) gets
  the same file:line lookup, and the fix text `fix the error at <file:line>`. The traceback hint
  stays `python -c 'import <package>'`.
- **7.M2:** no WebSocket `Origin` check. It is not part of the approved fix.
- **3.M1** (clock functions registered deterministic) is won't-fix; it is not in this phase.
