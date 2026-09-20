---
status: complete
---

# Phase 1: The extra, and the world's instructions

## Overview

The gate on the whole project, and the one change to core it needs.

`pyproject.toml` gains the `mcp` extra (`architecture.md` §10, `functional_spec.md` §9) and
`uv.lock` is regenerated. The licence gate passes — every distribution the extra brings is MIT,
BSD-3-Clause, Apache-2.0, MIT-0 or PSF-2.0 — but the extra cannot be installed beside `serve`:
`openenv` requires `fastmcp` 3.x, which requires `mcp` 1.x, and this project is written against 2.x.
The maintainer's answer is to declare the two extras conflicting, so this phase also splits the
environment CI and a contributor work in: two syncs, two pytest runs, and a licence audit named the
extra its environment holds.

`World.__init__` gains `mcp_server_instructions: str | None = None` (`functional_spec.md` §3): a
free string, unvalidated, stored beside `description` and read by nothing in core. Phase 2's
`instructions_for(world)` is its only reader.

The docs record the argument in `reference/api.md` and tell an author cloning a real MCP server to
set it in `authoring.md`. Nothing imports the SDK in this phase.

## Steps

1. `pyproject.toml`: add `mcp = ["mcp>=2.2,<3"]` under `[project.optional-dependencies]`, and a
   `[tool.uv]` table declaring the conflict, with a comment naming the pin that causes it and the
   `openenv` release that would dissolve it:

   ```toml
   [tool.uv]
   conflicts = [[{ extra = "serve" }, { extra = "mcp" }]]
   ```

2. `uv lock`, which carries both majors of `mcp`, then a sync of each extra in turn, then
   `scripts/check_licences.py` against each. A copyleft identifier anywhere in the new tree stops
   the phase.
3. `scripts/check_licences.py`: the extras to audit come from the command line, because one
   environment can no longer read both trees.
   - `extras_to_audit(named, declared)`: the extras named, or every declared extra when none is
     named. An extra the project does not declare raises, because a typo that audited nothing
     quietly would be a green run proving nothing.
   - `unmet_requirements(root, extra)`: what this environment is missing before the extra can be
     audited — a requirement that is not installed, or one installed at a version the extra does
     not ask for. The version matters: `serve` brings a distribution called `mcp`, and a name-only
     check would clear 1.30 and report the `mcp` extra as audited.
   - `main(argv)`: named extras are strict and an unmet one fails with the sync to run; a bare run
     audits what is installed and names what it could not read.
   - The module docstring says all of this, because it is the file's own instructions.
4. `.github/workflows/ci.yml`: the `check` job's licence step becomes `check_licences.py serve`, and
   a second job, `mcp`, syncs `--extra mcp` and runs the framework suite and `check_licences.py
   mcp`. Phase 2 adds the `import seahaven.mcp` assertion to that job and phase 3 the MCP tests.
5. `AGENTS.md` and `CONTRIBUTING.md`: the automated checks are now two environments. Both lists get
   the second sync, the second `uv run pytest`, and the named licence audits.
6. `src/seahaven/world.py`: `World.__init__` gains the keyword argument, after `description`:

   ```py
   mcp_server_instructions: str | None = None,
   ```

   assigned as `self.mcp_server_instructions = mcp_server_instructions`, with a comment saying what
   reads it (the MCP server's `instructions` string, nothing else) and why it is unvalidated.
7. `src/seahaven/docs/reference/api.md`: the argument in the `World.__init__` stub — the stub is
   compared parameter by parameter against the real callable — a sentence on it beside the
   `description` sentence, and `world.mcp_server_instructions` in the attribute row of the member
   table.
8. `src/seahaven/docs/authoring.md`: a short section on the argument after "Pinning a state
   format", with its row in the page's table of contents. It says what the string is, that a world
   which never serves MCP does not set it, and that an author cloning a real MCP server reads that
   server's instructions and matches them.
9. The spec, because phases 2 and 3 are built from it: `functional_spec.md` §9 records the conflict
   and what dissolves it, and `architecture.md` §10 replaces the single-environment CI line with the
   two-job table and specifies the version-aware skip guard those phases use in place of
   `pytest.importorskip("mcp")`.

## Tests

- `tests/test_world.py::test_the_mcp_server_instructions_are_kept_as_given_and_carried_by_a_copy` —
  the string is stored verbatim, blank included, `None` when it is not given, and `copy.copy(world)`
  carries it, the way the `description` test beside it is written.
- `tests/test_licence_check.py` runs in both environments, so a test that needs an extra's tree
  skips where that tree is absent, and the file says so at the top.
  - `test_the_project_declares_both_extras` — `declared_extras` answers `["mcp", "serve"]`.
  - `test_the_closure_of_every_declared_extra_is_allowed`, parametrized over the declared extras.
  - `test_the_mcp_closure_is_the_sdk_and_the_types_package_it_splits_into` — `mcp` and `mcp-types`
    are MIT, and `openenv` is not in that closure.
  - `test_the_serve_closure_is_the_runtime_tree_and_not_the_tooling`, and the existing base-closure
    and licence-spelling tests, unchanged except for the extras they name.
  - `test_the_extras_a_run_covers_are_the_ones_named_or_all_of_them` and
    `test_an_extra_the_project_does_not_declare_is_a_typo_and_not_an_empty_audit`.
  - Three tests of `unmet_requirements` against a fake project, so the answers do not depend on
    which environment the suite is in: requirements met, a version the extra does not ask for, and
    nothing installed at all.
  - `main` through its exit codes: a named extra audits that one and says which, a bare run names
    what it could not read, an extra this environment lacks fails rather than passing quietly, and
    an unknown extra is refused before anything is audited.
  - `test_every_declared_extra_is_audited_by_some_ci_job` — read out of the workflow file, because
    an extra added without a job that audits it would be shipped to users and read by nobody.
