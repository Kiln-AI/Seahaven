---
status: complete
---

# Phase 1: Low-risk code and test batch

## Overview

Eight independent, low-risk fixes from the September 2026 deep review, each designed and
prototyped in [components/phase_1_batch.md](../components/phase_1_batch.md): faster instance
creation (11.M1), a loopback default for `seahaven serve` (7.M2), a typed client that parses a
`ListToolsAction` step (5.M3), import errors placed at their file and line (6.M1), hosts-first
startup-hook order (4.M2), and three test-only items (10.M1, 10.M3, 10.M4). The component doc is the
design; this plan lists the concrete steps.

## Steps

1. **11.M1** `src/seahaven/db.py`: in `open_instance`, set `synchronous=OFF` before
   `journal_mode=WAL` (drop the `NORMAL` line), with a comment giving the constraint (the sweep
   deletes an instance directory after a crash). In `_build`, set `synchronous=OFF` on the
   builder before the DDL runs.
2. **7.M2** `src/seahaven/openenv/serve.py`: `DEFAULT_HOST = "127.0.0.1"`, comment says `/ws` has
   no authentication, so network reach is the operator's choice (`--host 0.0.0.0`); the scaffolded
   container passes it itself. `src/seahaven/cli/serve.py`: help text `(default 127.0.0.1)`.
   Docs: `serving_and_openenv.md` option row and example, `reference/cli.md` row and example.
3. **5.M3** `src/seahaven/openenv/client.py::_parse_result`: pick `ListToolsObservation` when the
   observation has a `tools` field, else `SeahavenObservation`. Update its docstring and
   `list_tools`'s. One line in `reference/api.md` near the client entry.
4. **6.M1** `src/seahaven/cli/__init__.py`:
   - `CliError.__init__(message, *, code=None, path=None, line=None, fix=None, root=None)`;
     `root` is the project the path is shown relative to (defaults to `path`), because `path` now
     names a file for an import failure and `check` renders findings relative to the project.
   - `_import`: the generic `except Exception` branch raises `_import_failure(module_name, error,
     root)`. The non-DDL `SeahavenError` branch also gets a location from `_world_frame` and the fix
     `fix the error at <file:line>`.
   - `_no_world` raise passes `fix="give the package a world, or point at the one it has"`.
   - New helpers `_import_failure`, `_world_frame`, `_authored`, `_is_the_world_module`, `_shown`
     per the component doc.
   - `src/seahaven/cli/check.py`: `collect` uses `error.root`; `_finding_for` passes
     `line=error.line` and `fix=error.fix or <no-world text>`; delete the shared-fix comment.
   - `src/seahaven/pytest_plugin.py`: comment only.
   - `docs/reference/lints.md` SH501 entry: an import that fails for another reason is reported
     at the file and line that raised it.
5. **4.M2** `src/seahaven/composition.py`: new exported `hosts_first(composition) -> list[Node]`
   (DFS from the root in `add_world` order, emitting a node once its last distinct host has been
   emitted; assert every node is emitted). `instances.py::_run_startup_hooks` iterates it; update
   its docstring. `docs/composition.md` hook-order sentence.
6. **10.M1** `tests/test_call_log.py`: replace the argument-mutation test with the two tests of
   the component doc.
7. **10.M3** `tests/test_typed_call.py`: add the diamond-under-a-handle test.
8. **10.M4** `tests/test_docs.py`: replace the index-only link test with a parametrised check
   over every page, `README.md` and `CONTRIBUTING.md` that resolves every relative link and
   every fragment against GitHub heading slugs and explicit anchors; keep the out-of-docs test
   against the new pattern; add a slug unit test.

## Tests

- `test_db.py::test_an_instance_connection_is_hardened`: `synchronous` is `0`.
- `test_db.py::test_opening_an_instance_file_does_not_fsync` (parametrised over opening a copied
  file and building a blank one): a counting VFS sees no `xSync`.
- `test_instances.py::test_every_node_of_a_fixture_instance_opens_without_durability`: both nodes
  of a composite fixture instance are `synchronous=0`, `journal_mode=wal`.
- `test_serve.py`: default bind pair is `("127.0.0.1", 8000)`; `host="0.0.0.0"` still prints the
  loopback console URL.
- `test_cli_new.py`: the hub `Dockerfile` CMD passes `"--host", "0.0.0.0"`.
- `test_client.py`: unknown-key refusal rewritten; `_parse_result` answers a
  `ListToolsObservation` for a tool list; live `step(ListToolsAction())` answers the tool list
  and a later call still works.
- `test_find_world.py`: syntax error, name error and missing dependency in world code are placed
  at their file and line; `--world` naming a missing module has a fix naming `--world`.
- `test_cli_check.py`: an import error is a finding at the line that raised it (through
  `seahaven check`); the no-world finding keeps its fix.
- `test_pytest_plugin.py::test_a_world_that_does_not_import_fails_with_its_file_and_line`.
- `test_composite_instance.py`: a shared node runs after every host; a host seeding a shared node
  seeds it the same composed as alone.
- `test_composition.py::test_hosts_first_is_the_canonical_preorder_without_sharing`.
- `test_call_log.py`: the two argument-copy tests.
- `test_typed_call.py::test_a_handle_reaches_a_function_of_a_node_two_routes_beneath_it_reach`.
- `test_docs.py`: every relative link and anchor resolves, per page; slug unit test.
