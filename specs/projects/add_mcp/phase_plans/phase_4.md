---
status: complete
---

# Phase 4: The docs

## Overview

Phases 1-3 shipped `seahaven mcp` and left the docs describing a framework that has no MCP server.
`authoring.md` and `reference/api.md` already document `mcp_server_instructions` (phase 1), so what
is left is the command itself, and one sentence in `serving_and_openenv.md` that has been false
since phase 1: "Seahaven will not add MCP support until the standard supports stateful servers."

This phase writes the `seahaven mcp` section of `serving_and_openenv.md`, rewrites that sentence,
adds the subcommand to `reference/cli.md` with every flag and every environment variable, and puts
MCP into `README.md`'s description of what a world can do. No new page, so `tests/test_docs.py`'s
`PAGES` is unchanged.

Every command, flag, path and name written down is checked against the shipped code
(`src/seahaven/cli/mcp.py`, `src/seahaven/mcp/`), not against the specs: phase 2 deviated from
`architecture.md` in seven ways and phase 3 in four.

## Steps

1. `src/seahaven/docs/serving_and_openenv.md`, the opening: the page says a world is driven in
   process or over `/ws` and that "there is no third way". Rewrite that paragraph so the two ways a
   harness drives a world stay what they are, and `seahaven mcp` is named as the third door with a
   different job, linking to the new section.

2. `src/seahaven/docs/serving_and_openenv.md`, the table of contents: one row for the new section,
   placed where the section is.

3. `src/seahaven/docs/serving_and_openenv.md`, the new section `## Serving one world to an MCP
   client`, after `## Evaluating with Kiln` and before `## Known problems in OpenEnv`. It covers,
   in this order:
   - what the command is: one process, one client, one instance for the life of the process, the
     world's tools and the world's own instruction string, and nothing about Seahaven;
   - that it is not the road for an eval or an RL run, which is `/ws` and `SeahavenClient`;
   - the `mcp` extra, and that `mcp` and `serve` cannot be installed together;
   - the command and an `.mcp.json` that launches it (a `json` fence, which the example harness
     does not execute), with `--world` beside the command in the file;
   - the options, briefly, pointing at `seahaven mcp -h` and `reference/cli.md` for the full set;
   - the random seed: no `--seed` means a random one, written to stderr, which is deliberately not
     `world.instance()`'s constant default;
   - one episode per process: no reset, restart the client for a fresh world, and destruction takes
     the instance's directory and its copy of the fixture with it;
   - what the client never sees: no state document, no call log, no control tool, no resources, no
     prompts, and why;
   - what goes wrong: a failed start is a JSON-RPC error and exit 1, a tool failure is a result the
     model can read, a framework error is scrubbed to a correlation id, and stdout belongs to the
     protocol so a world's `print` goes to stderr.

4. `src/seahaven/docs/serving_and_openenv.md`, `### POST /mcp and ws /mcp are refused with a
   JSON-RPC error`: rewrite the "Seahaven will not add MCP support" paragraph. The refusals and
   their reasoning stay; what changes is that a reader who wants an MCP client is sent to
   `seahaven mcp`, and that the sentence about stateful servers becomes what it was actually about
   — the OpenEnv app's `/mcp` dialect, not MCP itself.

5. `src/seahaven/docs/reference/cli.md`: the synopsis block gains the `mcp` line; the command table
   gains its row; a `## seahaven mcp` section after `## seahaven serve` carries the option table,
   the environment-variable table, the exit codes, and the three refusals that happen before
   anything is served. "Using it from Python" names `seahaven.mcp.serve`.

6. `src/seahaven/docs/index.md`: the `serving_and_openenv.md` row names `seahaven mcp` too, because
   the page now covers it.

7. `README.md`: MCP joins the feature list, and the serving section says a world is reachable from
   an MCP client as well as from an OpenEnv one.

8. Check the whole change against `AGENTS.md`'s docs style list: plain sentences, disclosure order,
   100-column wrap, and none of the named phrases.

## Tests

No new test file. The docs harness already covers this change, and the work is to keep it green:

- `tests/test_docs.py::test_every_page_in_the_layout_exists` and the link test: `PAGES` is
  unchanged, and every new link resolves to a page that exists.
- `tests/test_docs_examples.py`, the `sh` tier: every `seahaven mcp ...` command line added to any
  page is parsed by the real argument parser, so a flag that does not exist fails the suite.
- `tests/test_docs_examples.py`, the `seahaven.a.b` check: `seahaven.mcp.serve` named in
  `reference/cli.md` must resolve on the real package. It resolves only where the `mcp` extra is
  installed, so confirm the name in the `mcp` environment as well as running the suite in both.
- Both environments: `uv sync --locked --extra serve` and `uv sync --locked --extra mcp`, with the
  full check list of `AGENTS.md` under each.
