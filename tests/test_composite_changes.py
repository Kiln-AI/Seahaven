"""The changeset of a composite: one list over every node, each row saying which node.

What is new here is `Change.world` and the concatenation. Everything a changeset
*is* -- net difference, rolled-back calls leaving no trace, untracked tables and
FTS5 shadow tables excluded -- is `test_changes.py`'s, and holds per node because
each node has its own session over its own world's tables.
"""

import copy
from collections.abc import Iterator
from pathlib import Path

import emporium
import pytest

from seahaven.changes import Change
from seahaven.ctx import Ctx
from seahaven.errors import ToolError
from seahaven.instances import Instance
from seahaven.world import World
from tests.conftest import INSTANT_ISO, composable_world

pytestmark = pytest.mark.usefixtures("isolated_imports")


class Boom(ToolError):
    """A world's own error, as every world defines its own."""

    def __init__(self, message: str) -> None:
        super().__init__("boom", message)


@pytest.fixture
def live(tmp_path: Path) -> Iterator[Instance]:
    """A blank instance of the committed composite world, on its own directory."""
    world = copy.copy(emporium.world)
    world.work_dir = tmp_path / "work"
    with world.instance(None, now=INSTANT_ISO) as instance:
        yield instance


def three_levels(tmp_path: Path) -> World:
    """`main` -> `child` -> `child/grand`, three worlds with three tables."""
    host = composable_world("host", work_dir=tmp_path / "work")
    child = composable_world("child")
    child.add_world(composable_world("grand"), name="grand")
    host.add_world(child, name="child")
    return host


# ----------------------------------------------------------- which node, and what


def test_a_change_names_the_node_it_belongs_to(live: Instance) -> None:
    live.call("settle_order", total=250)

    assert {(change.world, change.table) for change in live.changes()} == {
        ("main", "charge_owners"),
        ("payments", "charges"),
        ("shop", "orders"),
    }


def test_a_world_that_adds_nothing_says_main(tmp_path: Path) -> None:
    with composable_world("solo", work_dir=tmp_path / "work").instance(None) as live:
        live.call("solo_write", value="a row")

        assert [change.world for change in live.changes()] == ["main"]


def test_two_stores_of_one_world_are_told_apart_by_their_paths(live: Instance) -> None:
    """Same world, same table, two nodes: the path is the only thing that differs."""
    live.call("pay_create_charge", amount=100)
    live.call("eu_create_charge", amount=200)

    charged = {change.world: change.after["amount"] for change in live.changes() if change.after}

    assert charged == {"payments": 100, "payments_eu": 200}


def test_the_list_runs_in_the_compositions_canonical_order(tmp_path: Path) -> None:
    host = three_levels(tmp_path)
    with host.instance(None) as live:
        # Written deepest first, so insertion order cannot be what is asserted.
        live.call("grand_write", value="deepest")
        live.call("child_write", value="middle")
        live.call("host_write", value="root")

        assert [change.world for change in live.changes()] == ["main", "child", "child/grand"]


def test_a_change_renders_to_a_dict_carrying_its_world(tmp_path: Path) -> None:
    with composable_world("solo", work_dir=tmp_path / "work").instance(None) as live:
        live.call("solo_write", value="a row")

        (change,) = live.changes()

        assert change.to_dict()["world"] == "main"
        assert change.to_dict()["table"] == "solo_rows"


def test_controller_changes_covers_every_node(live: Instance) -> None:
    live.call("settle_order", total=250)

    reported = live.call("controller_changes")

    assert {change["world"] for change in reported} == {"main", "payments", "shop"}
    assert reported == [change.to_dict() for change in live.changes()]


# ------------------------------------------------------- what each node excludes


def test_untracked_tables_are_each_worlds_own(tmp_path: Path) -> None:
    """A host that tracks its audit table and a child that does not, in one list."""
    host = composable_world(
        "host",
        work_dir=tmp_path / "work",
        extra_schema="CREATE TABLE audit (id TEXT PRIMARY KEY) STRICT;",
    )
    child = composable_world(
        "child",
        extra_schema="CREATE TABLE audit (id TEXT PRIMARY KEY) STRICT;",
        untracked_tables=["audit"],
    )
    host.add_world(child, name="child")

    with host.instance(None) as live:
        live.call("host_write", value="tracked")
        live.call("child_write", value="tracked")
        with live.bulk() as ctx:
            ctx.db.execute("INSERT INTO audit (id) VALUES ('kept')")
            ctx.worlds.child.db.execute("INSERT INTO audit (id) VALUES ('dropped')")

        assert {(change.world, change.table) for change in live.changes()} == {
            ("main", "host_rows"),
            ("main", "audit"),
            ("child", "child_rows"),
        }


def test_an_fts5_shadow_table_is_excluded_on_the_node_that_has_one(tmp_path: Path) -> None:
    host = composable_world("host", work_dir=tmp_path / "work")
    child = composable_world("child", extra_schema="CREATE VIRTUAL TABLE docs USING fts5(body);")
    host.add_world(child, name="child")

    with host.instance(None) as live:
        live.call("host_write", value="a row")
        with live.bulk() as ctx:
            ctx.worlds.child.db.execute("INSERT INTO docs (body) VALUES ('searchable words')")

        assert [(change.world, change.table) for change in live.changes()] == [
            ("main", "host_rows")
        ]


# ------------------------------------------------------------- what rolled back


def test_a_failed_nested_call_leaves_no_trace_in_either_store(tmp_path: Path) -> None:
    """No cross-world atomicity: the host's own write is what rolls back with it."""
    host = composable_world("host", work_dir=tmp_path / "work")
    child = composable_world("child")
    host.add_world(child, name="child")

    @child.tool(name="child_refuse")
    def refuse(ctx: Ctx) -> None:
        """Write, then fail: the child's own transaction is what undoes it."""
        ctx.db.execute("INSERT INTO child_rows (id, value) VALUES ('c1', 'never')")
        raise Boom("the child said no")

    @host.tool(name="host_try")
    def attempt(ctx: Ctx) -> None:
        """Write into the host's store, then call a child tool that refuses."""
        ctx.db.execute("INSERT INTO host_rows (id, value) VALUES ('h1', 'never')")
        ctx.worlds.child.call("child_refuse")

    with host.instance(None) as live:
        with pytest.raises(ToolError, match="the child said no"):
            live.call("host_try")

        assert live.changes() == []


def test_a_change_is_the_net_difference_per_node(tmp_path: Path) -> None:
    """An insert and an update of one row is one insert, in whichever node holds it."""
    host = composable_world("host", work_dir=tmp_path / "work")
    host.add_world(composable_world("child"), name="child")

    with host.instance(None) as live:
        with live.bulk() as ctx:
            ctx.worlds.child.db.execute("INSERT INTO child_rows VALUES ('c1', 'first')")
            ctx.worlds.child.db.execute("UPDATE child_rows SET value = 'second' WHERE id = 'c1'")

        assert live.changes() == [
            Change(
                world="child",
                table="child_rows",
                op="insert",
                key={"id": "c1"},
                before=None,
                after={"id": "c1", "value": "second"},
            )
        ]
