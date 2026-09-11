"""The world object: construction, the three registration verbs, and what each of them refuses."""

import importlib.util
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from seahaven.call import Call, Handler
from seahaven.ctx import Ctx
from seahaven.errors import WorldBug
from seahaven.tool import Tool
from seahaven.world import World

SCHEMA = "CREATE TABLE notes (id TEXT PRIMARY KEY, body TEXT NOT NULL) STRICT;"

MODULE_SOURCE = f"""
from seahaven import World

world = World("generated", "1.0.0", {SCHEMA!r})
"""


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World("testworld", "1.0.0", SCHEMA, fixtures_dir=tmp_path / "fixtures")


def echo(ctx: Ctx, word: str) -> dict[str, str]:
    """Echo one word."""
    return {"word": word}


def world_built_in(module_path: Path) -> World:
    """Build a world from a module on disk, so the derivation has a real file to walk up from."""
    module_path.parent.mkdir(parents=True, exist_ok=True)
    module_path.write_text(MODULE_SOURCE)
    spec = importlib.util.spec_from_file_location(
        f"generated_{module_path.parent.name}", module_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.world


def test_a_world_is_its_name_its_version_and_its_schema(tmp_path: Path) -> None:
    """`name` and `version` are informational, and go into the sidecar and the metadata."""
    world = World("projecttracker", "1.2.0", SCHEMA, fixtures_dir=tmp_path)

    assert (world.name, world.version, world.schema) == ("projecttracker", "1.2.0", SCHEMA)


def test_the_schema_hash_ignores_layout_and_nothing_else(tmp_path: Path) -> None:
    spaced = "CREATE   TABLE notes\n\t(id TEXT PRIMARY KEY,\n    body TEXT NOT NULL)\n STRICT;\n"
    renamed = SCHEMA.replace("body", "text")

    world = World("w", "1.0.0", SCHEMA, fixtures_dir=tmp_path)

    assert world.schema_hash == World("w", "1.0.0", spaced, fixtures_dir=tmp_path).schema_hash
    assert world.schema_hash != World("w", "1.0.0", renamed, fixtures_dir=tmp_path).schema_hash
    assert world.schema == SCHEMA


def test_a_world_whose_ddl_does_not_execute_cannot_be_built(tmp_path: Path) -> None:
    with pytest.raises(WorldBug) as raised:
        World("w", "1.0.0", "CREATE TALBE notes (id TEXT PRIMARY KEY);", fixtures_dir=tmp_path)

    assert "'w'" in str(raised.value)
    assert 'near "TALBE": syntax error' in str(raised.value)  # SQLite's own words, kept


def test_the_ddl_rules_belong_to_the_lint_and_not_to_construction(tmp_path: Path) -> None:
    """A table with no `STRICT` and no primary key is a lint finding, not a broken world."""
    World("w", "1.0.0", "CREATE TABLE notes (id TEXT);", fixtures_dir=tmp_path)


def test_an_explicit_fixtures_dir_is_used_as_given(tmp_path: Path) -> None:
    assert World("w", "1.0.0", SCHEMA, fixtures_dir=tmp_path / "elsewhere").fixtures_dir == (
        tmp_path / "elsewhere"
    )


def test_fixtures_are_at_the_project_root_of_a_src_layout(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'generated'\n")

    world = world_built_in(tmp_path / "src" / "generated" / "world.py")

    assert world.fixtures_dir == tmp_path / "fixtures"


def test_an_installed_package_has_its_fixtures_beside_it(tmp_path: Path) -> None:
    """No `pyproject.toml` above the module, as in a wheel. Construction still succeeds."""
    site_packages = tmp_path / "site-packages"

    world = world_built_in(site_packages / "generated" / "world.py")

    assert world.fixtures_dir == site_packages / "fixtures"


def test_the_working_directory_and_untracked_tables_are_carried(tmp_path: Path) -> None:
    world = World(
        "w",
        "1.0.0",
        SCHEMA,
        fixtures_dir=tmp_path,
        work_dir=tmp_path / "work",
        untracked_tables=["audit", "sessions"],
    )

    assert world.work_dir == tmp_path / "work"
    assert world.untracked_tables == ("audit", "sessions")
    # `None` is the default: a per-process temporary directory, resolved later.
    assert World("w", "1.0.0", SCHEMA, fixtures_dir=tmp_path).work_dir is None


def test_the_registry_is_ordered_and_read_only(world: World) -> None:
    world.tool(echo)

    @world.tool
    def second(ctx: Ctx) -> dict:
        """Second."""
        return {}

    assert list(world.tools) == ["echo", "second"]
    with pytest.raises(TypeError):
        world.tools["third"] = world.tools["echo"]  # ty: ignore[invalid-assignment]


def test_a_name_can_only_be_registered_once(world: World) -> None:
    world.tool(echo)

    with pytest.raises(WorldBug, match="registered twice"):
        world.tool(echo)


@pytest.mark.parametrize("name", ["reset", "step", "state", "close"])
def test_the_environment_verbs_are_not_tool_names(world: World, name: str) -> None:
    with pytest.raises(WorldBug, match="OpenEnv reserves"):
        world.tool(echo, name=name)


@pytest.mark.parametrize("name", ["controller_run_sql", "controller_changes"])
def test_a_control_tool_name_is_not_a_tool_name(world: World, name: str) -> None:
    with pytest.raises(WorldBug, match="control tool"):
        world.tool(echo, name=name)


def test_a_tool_from_a_factory_is_registered_as_it_is(world: World) -> None:
    built = Tool.from_function(echo, name="run_sql", description="Run SQL.", transaction=False)

    returned = world.tool(built)

    assert returned is built
    assert world.tools["run_sql"] is built
    assert world.tools["run_sql"].transaction is False


@pytest.mark.parametrize(
    "options",
    [{"name": "other"}, {"description": "other"}, {"transaction": False}, {"transaction": True}],
)
def test_options_cannot_be_passed_with_a_tool_a_factory_built(
    world: World, options: dict[str, Any]
) -> None:
    built = Tool.from_function(echo)

    with pytest.raises(WorldBug, match="built by a factory"):
        world.tool(built, **options)


def test_something_that_is_neither_a_function_nor_a_tool_is_refused(world: World) -> None:
    with pytest.raises(WorldBug, match="a function or a Tool"):
        world.tool(42)  # ty: ignore[invalid-argument-type]


def test_the_decorator_forms_return_what_was_decorated(world: World, ctx: Ctx) -> None:
    @world.tool
    def plain(ctx: Ctx, word: str) -> dict[str, str]:
        """Plain."""
        return {"word": word}

    @world.tool(name="renamed", description="Given.", transaction=False)
    def with_options(ctx: Ctx) -> dict:
        """Ignored."""
        return {}

    @world.middleware
    def timing(ctx: Ctx, call: Call, next_: Handler) -> Any:
        return next_(ctx, call)

    @world.instance_startup
    def startup(ctx: Ctx, *, user_id: str | None = None) -> None:
        return None

    # A decorated tool is still an ordinary function, callable in a test.
    assert plain(ctx, "hi") == {"word": "hi"}
    assert world.tools["plain"].name == "plain"
    # One call is one transaction unless the tool says otherwise, whether or not
    # an option was passed: `transaction=None` here means "not given", not "off".
    assert world.tools["plain"].transaction is True
    assert world.tools["renamed"].description == "Given."
    assert world.tools["renamed"].transaction is False
    assert world.middlewares == (timing,)
    assert [hook.fn for hook in world.startup_hooks] == [startup]


def test_the_verbs_also_work_called_with_parentheses_and_nothing_else(world: World) -> None:
    @world.tool()
    def tool(ctx: Ctx) -> dict:
        """Tool."""
        return {}

    @world.middleware()
    def middleware(ctx: Ctx, call: Call, next_: Handler) -> Any:
        return next_(ctx, call)

    @world.instance_startup()
    def startup(ctx: Ctx) -> None:
        return None

    assert list(world.tools) == ["tool"]
    assert world.middlewares == (middleware,)
    assert [hook.fn for hook in world.startup_hooks] == [startup]


def test_anything_callable_as_three_positional_arguments_is_a_middleware(world: World) -> None:
    class Callable_:
        def __call__(self, ctx: Ctx, call: Call, next_: Handler) -> Any:
            return next_(ctx, call)

    def variadic(*args: Any) -> Any:
        return args[2](args[0], args[1])

    world.middleware(Callable_())
    world.middleware(variadic)

    assert len(world.middlewares) == 2


@pytest.mark.parametrize(
    "obj",
    [
        lambda ctx, call: None,
        lambda: None,
        42,
    ],
)
def test_anything_else_is_not_a_middleware(world: World, obj: Any) -> None:
    with pytest.raises(WorldBug, match=r"\(ctx, call, next_\)"):
        world.middleware(obj)


def test_the_chain_is_rebuilt_as_middleware_arrives(world: World, ctx: Ctx) -> None:
    seen: list[str] = []

    @world.tool
    def touch(ctx: Ctx) -> dict:
        """Touch."""
        seen.append("tool")
        return {}

    call = Call("touch", {}, world.tools["touch"])
    world.chain(ctx.with_call(call), call)

    @world.middleware
    def later(ctx: Ctx, call: Call, next_: Handler) -> Any:
        seen.append("middleware")
        return {"intercepted": True}

    # Read at call time, so a middleware registered after the first call runs on
    # the second: a world stays registrable for its whole life.
    assert world.chain(ctx.with_call(call), call) == {"intercepted": True}
    assert seen == ["tool", "middleware"]


def test_a_tool_registered_later_joins_the_registry(world: World) -> None:
    world.tool(echo)

    @world.tool
    def late(ctx: Ctx) -> dict:
        """Late."""
        return {}

    assert list(world.tools) == ["echo", "late"]


def test_startup_hooks_record_the_reset_arguments_they_accept(world: World) -> None:
    @world.instance_startup
    def principal(ctx: Ctx, *, user_id: str | None = None, tenant: str = "t") -> None:
        return None

    @world.instance_startup
    def anything(ctx: Ctx, **kwargs: Any) -> None:
        return None

    first, second = world.startup_hooks

    assert (first.accepts, first.takes_var_kwargs) == (frozenset({"user_id", "tenant"}), False)
    assert (second.accepts, second.takes_var_kwargs) == (frozenset(), True)
    assert world.accepted_startup_kwargs == {"user_id", "tenant"}


def test_a_startup_hook_is_callable_as_registered(world: World, ctx: Ctx) -> None:
    seen: dict[str, Any] = {}

    @world.instance_startup
    def startup(ctx: Ctx, *, user_id: str) -> None:
        seen["user_id"] = user_id
        seen["ctx"] = ctx

    world.startup_hooks[0](ctx, user_id="u_1")

    assert seen == {"user_id": "u_1", "ctx": ctx}


def test_a_world_with_no_startup_hooks_accepts_no_reset_arguments(world: World) -> None:
    assert world.accepted_startup_kwargs == frozenset()


@pytest.mark.parametrize("name", ["fixture", "seed", "now"])
def test_a_startup_hook_cannot_take_resets_own_arguments(world: World, name: str) -> None:
    namespace: dict[str, Any] = {}
    exec(f"def startup(ctx, *, {name}=None): pass", namespace)

    with pytest.raises(WorldBug, match="reset's own"):
        world.instance_startup(namespace["startup"])


def test_a_startup_hook_takes_the_context_and_nothing_else_positionally(world: World) -> None:
    def two_positional(ctx: Ctx, user_id: str) -> None:
        return None

    def variadic(ctx: Ctx, *args: Any) -> None:
        return None

    def none_at_all() -> None:
        return None

    for hook in (two_positional, variadic, none_at_all):
        with pytest.raises(WorldBug, match="only positional parameter"):
            world.instance_startup(hook)


def test_a_control_tool_may_take_a_control_tools_name(world: World) -> None:
    """The seam `control.py` registers through: reserved for a world, not for the framework."""
    control_tool = replace(Tool.from_function(echo, name="controller_changes"), control=True)

    world.tool(control_tool)

    assert world.tools["controller_changes"].control is True


def test_not_even_a_control_tool_may_take_an_environment_verb(world: World) -> None:
    """`reset` and `step` belong to the wire, and no flag of this framework's reclaims them."""
    control_tool = replace(Tool.from_function(echo, name="reset"), control=True)

    with pytest.raises(WorldBug, match="OpenEnv reserves"):
        world.tool(control_tool)


def test_a_startup_hook_that_has_no_signature_is_refused(world: World) -> None:
    with pytest.raises(WorldBug, match="the context and keyword arguments"):
        world.instance_startup(42)  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize("has_project_file", [True, False])
def test_a_world_built_where_there_is_no_module_file_falls_back_to_the_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, has_project_file: bool
) -> None:
    """A `World` built in a REPL or an `exec` has no `__file__` to walk up from.

    The project root if there is one, and otherwise the working directory
    itself: "beside the package" means nothing when there is no package.
    """
    working_dir = tmp_path / "somewhere" / "deeper"
    working_dir.mkdir(parents=True)
    if has_project_file:
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'generated'\n")
    monkeypatch.chdir(working_dir)
    namespace: dict[str, Any] = {}

    exec(MODULE_SOURCE, namespace)

    expected = tmp_path if has_project_file else working_dir
    assert namespace["world"].fixtures_dir == expected / "fixtures"
