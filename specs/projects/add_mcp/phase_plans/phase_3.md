---
status: complete
---

# Phase 3: The command

## Overview

`seahaven mcp`: the parser, the option resolution of `functional_spec.md` §2.1–§2.4, the random
seed of §4, the missing-extra refusal of §9, and the registration that puts the subcommand on the
command tree. `src/seahaven/cli/mcp.py` imports no SDK, so it is importable and testable in both of
the repository's two environments, and `tests/test_cli_mcp.py` runs in both.

The phase also writes `tests/test_mcp_process.py`, which drives the real command as a subprocess:
`AGENTS.md` asks for the real entry point, and every rule this module resolves is only worth
anything if it reaches `world.instance()` through `main()`. That file imports the SDK, so it joins
the `mcp` side of the type-check split — `.github/workflows/ci.yml` already names it in both lists,
and the two command lists in `AGENTS.md` and `CONTRIBUTING.md` gain it, which
`tests/test_ci_workflow.py` holds them to.

Phase 2's review left one item open, and it closes here: `functional_spec.md` §12 test 5 names a
world whose startup hook *and* whose tool both print to stdout, and phase 2 covered only a print
while the world was being resolved. The world these process tests run against prints from all
three places, and the assertion is the one phase 2 used — every line of the process's stdout parses
as a protocol frame.

## Steps

1. `src/seahaven/cli/__init__.py`.

   - `build_parser` imports `mcp` and adds it to the tuple of subcommand modules.
   - The module docstring says six subcommands, not five.
   - `check_world_option(explicit: str | None) -> None`, which is the `module:attr` spelling rule
     `discover` already carries, lifted out so `discover` and `seahaven mcp` share one message.
     `functional_spec.md` §2.4 makes a malformed `--world` a refusal *before* the protocol starts,
     and `seahaven mcp` resolves the world late, inside the server, so the spelling has to be
     asked about early and separately.

2. `src/seahaven/cli/mcp.py`. Argument parsing, resolution and nothing else.

   ```py
   MISSING_EXTRA = 'seahaven mcp needs the mcp extra: pip install "seahaven[mcp]"'
   SEED_CEILING = 2**31

   @dataclass(frozen=True)
   class Options:
       reset_options: dict[str, Any]
       random_seed: int | None

   def add_parser(subcommands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None: ...
   def run(args: argparse.Namespace) -> int: ...
   def resolve_options(args: argparse.Namespace, environ: Mapping[str, str]) -> Options: ...
   ```

   `add_parser` registers `mcp` with `add_world_option` and four options: `--fixture`, `--seed`,
   `--now`, `--reset-options`. `--seed` is parsed as a string and converted in `resolve_options`,
   so a non-integer is the one-line refusal of §2.4 and exit 1 rather than argparse's exit 2.

   `resolve_options` is a pure function of the namespace and an environment mapping: no world, no
   filesystem, no SDK. In order, per `architecture.md` §3:

   - Read each of the four options: the flag when it is not `None`, else the variable when it is
     set, recording the source in the spelling the user used (`--fixture`, `SEAHAVEN_FIXTURE`).
   - Refuse a general source together with any convenience source, naming every conflicting source
     in the order `fixture, seed, now`.
   - Parse `--reset-options` through `json.loads`, refusing a document that is not an object, and
     the seed through `int`. Both messages name the source.
   - Answer the general object as it stands, or the convenience keywords that were given.
   - When no seed came through either door, put `seed=random.randrange(0, SEED_CEILING)` in the
     answer and report it as `random_seed`, so `run` can write it to stderr. A `"seed"` key inside
     a `--reset-options` object counts as given, whatever its value.

   `run` resolves the options, checks the `--world` spelling, imports `seahaven.mcp` (raising
   `CliError(MISSING_EXTRA)` on `ImportError`, before the world is touched), writes the seed line
   to stderr when the seed is the one it picked, and returns
   `serve(lambda: find_world(args.world), reset_options=options.reset_options)`.

3. `tests/test_cli.py`: `mcp` joins the parametrised list of subcommands the parser must have.

4. `AGENTS.md` and `CONTRIBUTING.md`: `tests/test_mcp_process.py` joins the `mcp` half of the
   type-check line in both command lists.

## Tests

**`tests/test_cli_mcp.py`** — no SDK, runs in both environments. `run_cli` drives the parser, and
`seahaven.mcp` is a recording stub put in `sys.modules`, the way `tests/test_cli_serve.py` replaces
`openenv.serve`.

- `test_each_flag_reaches_the_reset_options` — `--fixture`, `--seed`, `--now`.
- `test_each_variable_reaches_the_reset_options` — the three `SEAHAVEN_*` spellings.
- `test_a_flag_beats_the_matching_variable_silently` — and nothing is said about it on stderr.
- `test_reset_options_are_passed_whole` — the JSON object reaches `serve` as keyword arguments,
  including a key no convenience flag has.
- `test_the_general_door_refuses_every_convenience_source` — parametrised over the flag and the
  variable spellings; the message names the sources actually given and the general source's own
  spelling.
- `test_the_refusal_names_every_conflicting_source_in_order` — fixture, seed and now at once.
- `test_reset_options_that_are_not_json_are_refused` — and the message names the source.
- `test_reset_options_that_are_not_an_object_are_refused` — a list, a string and a number.
- `test_a_seed_that_is_not_an_integer_is_refused` — flag and variable.
- `test_no_seed_is_a_random_seed_written_to_stderr` — the seed reaches `serve`, is in range, and
  is on the line stderr carries.
- `test_two_launches_without_a_seed_differ` — the deviation from `ids.DEFAULT_CALLER_SEED`.
- `test_a_seed_that_was_given_is_not_announced` — flag, variable, and inside `--reset-options`.
- `test_the_world_option_reaches_the_world_serve_resolves` — the callable answers that world.
- `test_a_malformed_world_option_is_refused_before_serving` — exit 1, `serve` never called.
- `test_a_missing_mcp_extra_names_the_extra` — one line on stderr, exit 1.

**`tests/test_mcp_process.py`** — the real command as a subprocess, behind `mcp_sdk()`. The world
is a small package the test writes into `tmp_path`: it prints from its module body, from its
startup hook and from a tool, it binds a startup keyword, and its `work_dir` is the test's, so the
parent can see the instance directory appear and go.

- `test_the_command_serves_the_world` — parametrised over the handshake and modern eras: the
  server's identity, the world's instructions, and the tool list equal to what the world publishes.
- `test_a_write_is_seen_by_a_later_read_in_the_same_process`.
- `test_a_tool_error_is_an_is_error_result_carrying_the_triple`.
- `test_a_control_tool_is_answered_as_an_unknown_tool` — §12 test 4, through a real client against
  a real process.
- `test_the_convenience_flags_reach_the_instance` — `--now` and `--seed` are visible in what the
  tools answer, and the same seed twice gives the same ids.
- `test_the_environment_variables_reach_the_instance` — the same through `SEAHAVEN_NOW` and
  `SEAHAVEN_SEED`.
- `test_reset_options_reach_a_startup_hook` — a keyword no flag spells.
- `test_two_calls_in_flight_serialise` — neither update is lost.
- `test_a_world_that_prints_cannot_corrupt_the_stream` — §12 test 5 in full: the module body, the
  startup hook and the tool all print, every line of stdout parses as a frame, and all three
  strings are on stderr.
- `test_a_bad_fixture_is_answered_on_the_wire_and_exits_one`.
- `test_eof_destroys_the_instance_and_removes_its_working_directory` — one directory while the
  process is alive, none after, exit 0.
- `test_a_refusal_is_one_line_on_stderr_and_exit_one` — the mixing refusal through the real
  command, with nothing on stdout.
- `test_the_seed_it_picked_is_on_stderr` — and the process still serves.
