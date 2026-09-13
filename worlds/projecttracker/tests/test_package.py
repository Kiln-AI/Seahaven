"""The package as the tooling meets it: one world, one tool, one middleware.

The CLI, the pytest plugin and the OpenEnv app all find this world the same way
-- import the package, read `world` off it -- so what that import produces is
this world's real interface and is worth asserting directly.
"""

import json
import pkgutil
import subprocess
import sys
from pathlib import Path

import pytest

import projecttracker
import seahaven
from conftest import BLANK_NOW
from projecttracker import middleware, tools
from projecttracker.middleware.error_handler import error_handler
from projecttracker.world import world

# The table tests in the second half of this module drive an instance with no
# fixture behind it; the package tests in the first half take none at all, and a
# module-wide marker costs them nothing.
pytestmark = pytest.mark.seahaven(fixture=None, now=BLANK_NOW)


def test_the_package_exports_the_world_object_the_tooling_looks_for() -> None:
    """`projecttracker.world` is a `World`, not the module of that name.

    The package has a submodule called `world` and an attribute called `world`,
    and the attribute wins because `__init__` binds it last. Everything that
    discovers a world -- `seahaven check`'s SH501, the CLI, the plugin -- reads
    that attribute, so an import reordering that let the module shadow it would
    break all three at once and nothing else would notice.
    """
    assert isinstance(projecttracker.world, seahaven.World)
    assert projecttracker.world is world


def test_the_world_is_named_and_versioned() -> None:
    assert world.name == "projecttracker"
    assert world.version == "1.0.0"


def test_the_schema_is_the_sql_files_on_disk() -> None:
    """The DDL is `schema/*.sql` as written, and `World` proved it executes."""
    on_disk = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((Path(projecttracker.__file__).parent / "schema").glob("*.sql"))
    )
    assert world.schema == on_disk
    assert "CREATE TABLE users" in world.schema


def test_the_registry_holds_ping_and_the_two_control_tools() -> None:
    """The control tools are on every world; `ping` is this world's whole registry."""
    assert set(world.tools) == {"ping", "controller_run_sql", "controller_changes"}


def test_the_tool_list_an_agent_sees_is_exactly_ping() -> None:
    with world.instance(None, now="2026-01-01T00:00:00.000Z") as instance:
        listing = instance.tools()
    assert [tool["name"] for tool in listing] == ["ping"]
    assert listing[0]["description"], "SH205: a tool with an empty description"


def test_the_error_handler_is_registered_outermost() -> None:
    """Registration order is chain order, and an error wrapper wraps everything."""
    assert world.middlewares[0] is error_handler


def test_importing_the_package_is_what_registers_the_tools_and_the_middleware() -> None:
    """A fresh interpreter that imports the package and nothing else.

    Asserting this in process proves nothing: the test modules import
    `projecttracker.tools` and `projecttracker.middleware.error_handler`
    themselves, so the registrations have happened whether or not the package
    performs them. Deleting either import from `__init__.py` leaves a world that
    serves no tools and wraps no errors, and only a separate process can see it.
    """
    program = (
        "import json, projecttracker as p; "
        "print(json.dumps({'tools': sorted(p.world.tools), "
        "'middlewares': [m.__name__ for m in p.world.middlewares]}))"
    )
    done = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, check=True
    )
    registered = json.loads(done.stdout)
    assert "ping" in registered["tools"]
    assert registered["middlewares"] == ["error_handler"]


def test_every_module_under_tools_and_middleware_is_imported_by_the_package() -> None:
    """The property `seahaven check`'s SH301 exists to enforce, asserted here too.

    A module that is written, never imported and therefore never registered is
    invisible: the world runs, and the tool is simply missing. The lint is
    Phase 7's; the property is true today and cheap to pin.
    """
    for package in (tools, middleware):
        for module in pkgutil.iter_modules(package.__path__):
            assert hasattr(package, module.name), (
                f"{package.__name__}/{module.name}.py is never imported, so nothing in it registers"
            )


def test_the_world_reads_its_fixtures_from_the_package_directory() -> None:
    """`fixtures/` beside `pyproject.toml`, found by walking up from `world.py`."""
    assert world.fixtures_dir.name == "fixtures"
    assert (world.fixtures_dir.parent / "pyproject.toml").is_file()


@pytest.mark.parametrize(
    "role",
    ["admin", "member", "viewer"],
)
def test_the_users_table_accepts_the_three_roles(instance: seahaven.Instance, role: str) -> None:
    with instance.bulk() as ctx:
        ctx.db.execute(
            "INSERT INTO users (id, email, name, role, created_at) VALUES (?, ?, ?, ?, ?)",
            f"u_{role}",
            f"{role}@example.invalid",
            role.title(),
            role,
            ctx.clock.iso(),
        )
    assert instance.call("ping")["users"] == 1


def test_the_users_table_refuses_a_role_that_is_not_one_of_the_three(
    instance: seahaven.Instance,
) -> None:
    """The CHECK constraint is the schema's, and it is really there."""
    with pytest.raises(seahaven.DbError), instance.bulk() as ctx:
        ctx.db.execute(
            "INSERT INTO users (id, email, name, role, created_at) VALUES (?, ?, ?, ?, ?)",
            "u_1",
            "someone@example.invalid",
            "Someone",
            "owner",
            ctx.clock.iso(),
        )


def test_the_users_table_refuses_a_duplicate_email(instance: seahaven.Instance) -> None:
    with instance.bulk() as ctx:
        ctx.db.execute(
            "INSERT INTO users (id, email, name, role, created_at) VALUES ('u_1', 'a@b.invalid',"
            " 'A', 'member', '2026-06-01T09:00:00.000Z')"
        )
    with pytest.raises(seahaven.DbError), instance.bulk() as ctx:
        ctx.db.execute(
            "INSERT INTO users (id, email, name, role, created_at) VALUES ('u_2', 'a@b.invalid',"
            " 'B', 'member', '2026-06-01T09:00:00.000Z')"
        )


@pytest.mark.parametrize("column", ["name", "created_at", "email", "role"])
def test_the_users_table_refuses_a_row_with_a_column_missing(
    instance: seahaven.Instance, column: str
) -> None:
    """Every column of `users` is `NOT NULL`, and every one of them is asserted.

    The schema hash in the fixture's sidecar notices any change to this file, so
    dropping a `NOT NULL` is "caught" by a fixture test -- but that is a tripwire
    on the text, not a statement about the table. This asks the table.
    """
    row = {
        "id": "u_1",
        "email": "a@b.invalid",
        "name": "A",
        "role": "member",
        "created_at": "2026-06-01T09:00:00.000Z",
    }
    row[column] = None
    with pytest.raises(seahaven.DbError), instance.bulk() as ctx:
        ctx.db.execute(
            "INSERT INTO users (id, email, name, role, created_at) VALUES (?, ?, ?, ?, ?)",
            row["id"],
            row["email"],
            row["name"],
            row["role"],
            row["created_at"],
        )


def test_the_users_table_refuses_a_duplicate_id(instance: seahaven.Instance) -> None:
    """`id TEXT PRIMARY KEY`: the key is a key, not just a column named id."""
    with instance.bulk() as ctx:
        ctx.db.execute(
            "INSERT INTO users (id, email, name, role, created_at) VALUES ('u_1', 'a@b.invalid',"
            " 'A', 'member', '2026-06-01T09:00:00.000Z')"
        )
    with pytest.raises(seahaven.DbError), instance.bulk() as ctx:
        ctx.db.execute(
            "INSERT INTO users (id, email, name, role, created_at) VALUES ('u_1', 'c@d.invalid',"
            " 'C', 'member', '2026-06-01T09:00:00.000Z')"
        )


def test_the_users_table_is_strict(instance: seahaven.Instance) -> None:
    """STRICT in the DDL and STRICT in the live schema, which is what matters.

    The lint that would catch a table without it is Phase 7's (SH101); this asks
    SQLite, which is the only answer that cannot be wrong. A blob into a TEXT
    column is what the flag buys: any other type SQLite would convert, and a
    blob it refuses.
    """
    assert instance.inspect().one("SELECT strict FROM pragma_table_list WHERE name = 'users'") == {
        "strict": 1
    }
    with pytest.raises(seahaven.DbError), instance.bulk() as ctx:
        ctx.db.execute(
            "INSERT INTO users (id, email, name, role, created_at) VALUES (x'00ff', 'a@b.invalid',"
            " 'A', 'member', '2026-06-01T09:00:00.000Z')"
        )
