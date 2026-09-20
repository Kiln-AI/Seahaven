---
status: complete
---

# Implementation Plan: Add MCP

Four phases. Each is a reviewable unit that leaves the repository green.

## Phases

- [x] **Phase 1: The extra, and the world's instructions.** The `mcp` extra in `pyproject.toml` and
      a regenerated `uv.lock`; `World(..., mcp_server_instructions=...)`; `reference/api.md` and the
      `authoring.md` line telling an author cloning a real MCP server to set it. Nothing imports the
      SDK yet. `scripts/check_licences.py` passes on the new tree, or the phase stops and says so
      (`architecture.md` §10).

- [ ] **Phase 2: The server.** `seahaven/mcp/` — `serve()`, the session map, the `initialize`
      middleware that creates the instance, the two handlers, the wire translation and the error
      taxonomy, shutdown on EOF and on signals, and the interim control tool refusal.
      `tests/test_mcp_server.py` drives it in process through the SDK's own client. CI gains the
      `import seahaven.mcp` assertion beside the one for `seahaven.openenv`.

- [ ] **Phase 3: The command.** `seahaven/cli/mcp.py` — the parser, `resolve_options` and every
      refusal, the environment variables, the random seed and its stderr line, the missing-extra
      message; registration in `build_parser` and the module docstring that counts the subcommands.
      `tests/test_cli_mcp.py` for the resolution, `tests/test_mcp_process.py` for the real entry
      point as a subprocess.

- [ ] **Phase 4: The docs.** The `seahaven mcp` section in `serving_and_openenv.md`, the rewrite of
      "Seahaven will not add MCP support until the standard supports stateful servers" and the note
      that this command is not the road for an eval, `reference/cli.md` for the subcommand and every
      flag and variable, and `README.md`. No new page, so `tests/test_docs.py`'s `PAGES` is
      unchanged.

## Notes

- `functional_spec.md` §15 is a dependency, not a phase: control tools becoming opt-in in core is
  expected to land separately, and `control_tools_task.md` is the prompt that hands it over. Phase 2
  ships the interim refusal, and deleting it is a four-line change whenever §15 arrives.
- Phase 1 is the gate on the whole project. If the `mcp` dependency tree does not pass
  `check_licences.py`, nothing after it is worth building as specified.
