---
status: complete
---

# Phase 3: `seahaven.http.main`, and the `seahaven mcp` split it uses

## Overview

Give a world's `serve_http.py` a command line. `seahaven.http.main(world, handler, argv)` parses
`--host`, `--port`, `--max-instances` and the reset-option flags that `seahaven mcp` takes, and calls
`serve`. The reset-option flags, their environment variables and every refusal are shared with
`seahaven mcp`, so two functions are split out of `seahaven/cli/mcp.py` for `main` to call. The
split leaves `seahaven mcp`'s behaviour, help and messages unchanged, which `tests/test_cli_mcp.py`
proves by passing unedited. The test world gains the `__main__` block phase 1 deferred, and a
process test runs it as a real server.

## Steps

1. `src/seahaven/cli/mcp.py` (architecture §6.1):
   - `WITHHELD_REASON`: the text after the colon of today's `control_tools` refusal.
   - `add_reset_option_arguments(parser, *, seed_default=..., clock_mode_default=...)`: the five
     `add_argument` calls `add_parser` makes today, moved verbatim with their comments. The two
     keywords are the "the default is ..." ends of the `--seed` and `--clock-mode` help, whose
     defaults are `seahaven mcp`'s own text; `main` passes its own, because its defaults differ (a
     random seed per instance, `wall`).
   - `resolve_reset_options(args, environ, *, withheld_reason=WITHHELD_REASON) -> dict[str, Any]`:
     the body of `resolve_options` without `_with_a_seed`.
   - `resolve_options` becomes `_with_a_seed(resolve_reset_options(args, environ))`.
   - `_object` and `_check_keys` take `withheld_reason` and build the same message from it.
   - `__all__` gains the two functions and `WITHHELD_REASON`.
2. `src/seahaven/http/command.py` (architecture §6.2):

   ```py
   WITHHELD_REASON = "this server serves the world's HTTP handler and nothing else"

   def main(world: World, handler: HttpHandler, argv: list[str] | None = None) -> None
   ```

   - The parser: `prog=Path(sys.argv[0]).name`, a description naming the world, `--host` (default
     `DEFAULT_HOST`), `--port` (`int`, default `DEFAULT_PORT`), `--max-instances` (`int`, default
     `DEFAULT_MAX_INSTANCES`), then `add_reset_option_arguments`.
   - Resolve the options with this package's `WITHHELD_REASON`; refuse a negative
     `--max-instances` with a `CliError` naming the flag; call the `seahaven.http.serve` wrapper,
     imported inside the function so a missing stack is `MISSING_EXTRA`.
   - `CliError`, `SeahavenError` or `ImportError`: print the message to stderr, `SystemExit(1)`.
3. `src/seahaven/http/__init__.py`: `main(world, handler, argv=None)`, which imports
   `seahaven.http.command` and calls its `main`; `"main"` in `__all__`.
4. `tests/http_world.py`: `if __name__ == "__main__": seahaven.http.main(build(), handle)`.

## Tests

`tests/test_http_command.py`. `seahaven.http.server.serve` is wrapped by a recorder that records its
keyword arguments and calls the real `serve` with `uvicorn.run` replaced, so the whole path from
`argv` to the built app runs.

- `test_the_defaults_reach_serve`: `127.0.0.1`, `8000`, `100`, `{}`, and the `Serving ...` line.
- `test_host_port_and_max_instances_reach_serve`.
- `test_each_flag_reaches_the_reset_options` / `test_each_variable_reaches_the_reset_options`.
- `test_reset_options_are_passed_whole`: `--reset-options` with `"startup"`.
- `test_no_seed_is_left_to_each_instance`: no `"seed"` key and nothing on stderr; `--seed 7` is `7`.
- `test_mixing_the_two_doors_is_refused_with_the_shared_message`.
- `test_control_tools_are_refused_with_this_servers_reason`: by `--reset-options` and by
  `SEAHAVEN_RESET_OPTIONS`.
- `test_a_seed_that_is_not_an_integer_is_refused`.
- `test_a_negative_max_instances_is_refused`, `test_an_unknown_fixture_is_refused`: exit 1, one
  line on stderr, `uvicorn.run` never called.
- `test_help_names_this_servers_defaults`: the `--seed` and `--clock-mode` help name the per
  instance seed and `wall`.
- `test_without_the_server_stack_main_says_to_install_the_extra`.
- `test_the_script_serves_over_http` (process): `python tests/http_world.py --port 0`, read the
  `Serving notes_api at` line on stdout and the port from uvicorn's `Uvicorn running on` line on
  stderr, `POST` a note and `GET` it back with `urllib`, then `SIGTERM` and a clean exit.

`tests/test_cli_mcp.py` is not edited, and passes.
