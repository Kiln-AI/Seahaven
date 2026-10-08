"""`world.instance(setup_sql=...)`: a caller's SQL, run on a new instance before its first call.

Driven through `world.instance(...)` throughout, on small inline worlds: a leaf
with a full-text index, and a host that adds `payments`, which adds `tax`. The
unit tests at the end cover the splitter and the error text on their own.
"""

import re
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

import apsw
import pytest

from seahaven.clock import Clock
from seahaven.ctx import Ctx
from seahaven.db import build_blank
from seahaven.errors import WorldBug
from seahaven.instances import Instance
from seahaven.setup_sql import (
    Refused,
    SetupAuthorizer,
    _explain,
    run_setup_sql,
    split_statements,
)
from seahaven.world import World
from tests.conftest import INSTANT_ISO, build_world

SCHEMA = """
CREATE TABLE users (id TEXT PRIMARY KEY, name TEXT NOT NULL) STRICT;

CREATE TABLE issues (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    owner_id TEXT REFERENCES users (id)
) STRICT;

CREATE VIRTUAL TABLE issues_fts USING fts5(title, content='issues', content_rowid='rowid');

CREATE TRIGGER issues_ai AFTER INSERT ON issues BEGIN
    INSERT INTO issues_fts (rowid, title) VALUES (new.rowid, new.title);
END;

CREATE TRIGGER issues_au AFTER UPDATE ON issues BEGIN
    INSERT INTO issues_fts (issues_fts, rowid, title) VALUES ('delete', old.rowid, old.title);
    INSERT INTO issues_fts (rowid, title) VALUES (new.rowid, new.title);
END;
"""

SCHEMA_FIX = (
    "setup_sql may only read and write rows (INSERT, UPDATE, DELETE, SELECT). Change the schema "
    "in the world and bump its version."
)
TRANSACTION_FIX = (
    "setup_sql already runs in one transaction that Seahaven opens and commits; remove BEGIN, "
    "COMMIT and SAVEPOINT."
)
ATTACH_FIX = (
    "Every node is already attached: name the root's tables unqualified and an added node's as "
    "<schema>.<table>"
)
PRAGMA_FIX = "Only read-only pragmas such as table_info are allowed."


def leaf(tmp_path: Path) -> World:
    """Users and issues, with a full-text index kept in step by triggers."""
    world = build_world(tmp_path, SCHEMA, name="tracker")

    @world.tool
    def search(ctx: Ctx, query: str) -> list[str]:
        """The ids of the issues whose title matches."""
        return [
            row["id"]
            for row in ctx.db.rows(
                "SELECT issues.id FROM issues_fts JOIN issues ON issues.rowid = issues_fts.rowid "
                "WHERE issues_fts MATCH ? ORDER BY issues.id",
                query,
            )
        ]

    return world


def composed(tmp_path: Path) -> World:
    """`orders` at the root, which adds `payments` (`charges`), which adds `tax` (`rates`)."""
    host = build_world(
        tmp_path,
        "CREATE TABLE orders (id TEXT PRIMARY KEY, total INTEGER NOT NULL) STRICT;",
        name="shop",
    )
    payments = World(
        "payments",
        "1.0.0",
        "CREATE TABLE charges (id TEXT PRIMARY KEY, amount INTEGER NOT NULL) STRICT;",
        state_format="seahaven.state/1",
    )
    tax = World(
        "tax",
        "1.0.0",
        "CREATE TABLE rates (region TEXT PRIMARY KEY, percent INTEGER NOT NULL) STRICT;",
        state_format="seahaven.state/1",
    )
    payments.add_world(tax, name="tax")
    host.add_world(payments, name="payments")
    return host


def instance_dirs(tmp_path: Path) -> list[Path]:
    """Every instance directory under the test's working root."""
    return [
        path for path in (tmp_path / "work").rglob("*") if path.is_dir() and _is_uuid(path.name)
    ]


def _is_uuid(name: str) -> bool:
    try:
        uuid.UUID(name)
    except ValueError:
        return False
    return True


def first_line(error: pytest.ExceptionInfo[WorldBug]) -> str:
    return str(error.value).splitlines()[0]


def rows(live: Instance, sql: str) -> list[dict[str, Any]]:
    return live.inspect().rows(sql)


# ------------------------------------------------------------------ behaviour


def test_rows_written_by_setup_sql_are_there_for_the_first_call(tmp_path: Path) -> None:
    sql = "INSERT INTO users (id, name) VALUES ('u1', 'Ada')"
    with leaf(tmp_path).instance(None, setup_sql=sql) as live:
        assert live.call("rows", sql="SELECT id, name FROM users") == [{"id": "u1", "name": "Ada"}]
        assert rows(live, "SELECT id FROM users") == [{"id": "u1"}]


def test_statements_run_in_the_order_written(tmp_path: Path) -> None:
    sql = """
        INSERT INTO users (id, name) VALUES ('u1', 'Ada');
        UPDATE users SET name = name || ' Lovelace' WHERE id = 'u1';
        INSERT INTO issues (id, title, owner_id) SELECT 'i1', name, id FROM users;
    """
    with leaf(tmp_path).instance(None, setup_sql=sql) as live:
        assert rows(live, "SELECT title, owner_id FROM issues") == [
            {"title": "Ada Lovelace", "owner_id": "u1"}
        ]


def test_a_semicolon_inside_a_literal_does_not_split(tmp_path: Path) -> None:
    sql = "INSERT INTO users (id, name) VALUES ('u1', 'a;b'); INSERT INTO users VALUES ('u2', ';')"
    with leaf(tmp_path).instance(None, setup_sql=sql) as live:
        assert rows(live, "SELECT name FROM users ORDER BY id") == [{"name": "a;b"}, {"name": ";"}]


def test_unqualified_names_write_the_root_and_qualified_names_write_a_node(
    tmp_path: Path,
) -> None:
    sql = """
        INSERT INTO orders (id, total) VALUES ('o1', 100);
        INSERT INTO main.orders (id, total) VALUES ('o2', 50);
        INSERT INTO payments.charges (id, amount) VALUES ('c1', 100);
        INSERT INTO payments__tax.rates (region, percent) VALUES ('eu', 20);
    """
    with composed(tmp_path).instance(None, setup_sql=sql) as live, live.bulk() as ctx:
        assert ctx.db.rows("SELECT id FROM orders ORDER BY id") == [{"id": "o1"}, {"id": "o2"}]
        assert ctx.worlds.payments.db.rows("SELECT id FROM charges") == [{"id": "c1"}]
        assert ctx.worlds.payments.worlds.tax.db.rows("SELECT region FROM rates") == [
            {"region": "eu"}
        ]


def test_one_statement_reads_one_node_and_writes_another(tmp_path: Path) -> None:
    sql = """
        INSERT INTO orders (id, total) VALUES ('o1', 100), ('o2', 40);
        INSERT INTO payments.charges (id, amount) SELECT 'c-' || id, total FROM orders;
    """
    with composed(tmp_path).instance(None, setup_sql=sql) as live:
        assert rows(live, "SELECT id, amount FROM payments.charges ORDER BY id") == [
            {"id": "c-o1", "amount": 100},
            {"id": "c-o2", "amount": 40},
        ]


def test_setup_sql_runs_on_a_copied_fixture_and_fts5_follows_it(tmp_path: Path) -> None:
    world = leaf(tmp_path)
    with world.instance(None, now=INSTANT_ISO, clock_mode="fixed") as live:
        live.call("execute", sql="INSERT INTO issues (id, title) VALUES ('i1', 'Login fails')")
        live.freeze("seeded", "one issue")

    sql = "UPDATE issues SET title = 'Export is slow' WHERE id = 'i1'"
    with world.instance("seeded", setup_sql=sql) as live:
        assert live.call("search", query="export") == ["i1"]
        assert live.call("search", query="login") == []

    with world.instance("seeded") as live:
        assert live.call("search", query="login") == ["i1"]


def test_fts5_commands_select_and_read_only_pragmas_are_allowed(tmp_path: Path) -> None:
    sql = """
        INSERT INTO issues (id, title) VALUES ('i1', 'Dark mode');
        INSERT INTO issues_fts (issues_fts) VALUES ('rebuild');
        SELECT count(*) FROM issues;
        PRAGMA table_info(issues);
    """
    with leaf(tmp_path).instance(None, setup_sql=sql) as live:
        assert live.call("search", query="dark") == ["i1"]


def test_setup_sql_may_read_a_view(tmp_path: Path) -> None:
    world = build_world(
        tmp_path,
        SCHEMA + "CREATE VIEW named AS SELECT id, upper(name) AS shout FROM users;",
    )
    sql = """
        INSERT INTO users (id, name) VALUES ('u1', 'Ada');
        INSERT INTO issues (id, title, owner_id) SELECT 'i1', shout, id FROM named;
    """
    with world.instance(None, setup_sql=sql) as live:
        assert rows(live, "SELECT title FROM issues") == [{"title": "ADA"}]


TRIGGERED = """
CREATE TABLE items (id TEXT PRIMARY KEY, tags TEXT NOT NULL DEFAULT '[]') STRICT;
CREATE TABLE audit (
    id INTEGER PRIMARY KEY, item_id TEXT NOT NULL, changed INTEGER, tag TEXT
) STRICT;

CREATE TRIGGER items_counted AFTER INSERT ON items BEGIN
    INSERT INTO audit (item_id, changed) VALUES (new.id, changes());
END;

CREATE TRIGGER items_tagged AFTER UPDATE OF tags ON items BEGIN
    INSERT INTO audit (item_id, tag) SELECT new.id, value FROM json_each(new.tags);
END;

CREATE VIEW item_changes AS SELECT id, changes() AS changed FROM items;

CREATE VIEW live_items AS SELECT id, tags FROM items;

CREATE TRIGGER live_items_update INSTEAD OF UPDATE ON live_items BEGIN
    UPDATE items SET tags = new.tags WHERE id = old.id;
END;

CREATE TRIGGER live_items_delete INSTEAD OF DELETE ON live_items BEGIN
    DELETE FROM items WHERE id = old.id;
END;
"""

# A trigger that shares the view's name, which SQLite allows: a name is trusted
# only as a trigger, so this one's body is held to the allowlists.
NAMESAKE = """
CREATE TRIGGER live_items AFTER DELETE ON items BEGIN
    INSERT INTO audit (item_id, changed) VALUES (old.id, changes());
END;
"""


def test_a_worlds_trigger_may_call_a_function_setup_sql_may_not(tmp_path: Path) -> None:
    world = build_world(tmp_path, TRIGGERED)
    with world.instance(None, setup_sql="INSERT INTO items (id) VALUES ('a')") as live:
        assert rows(live, "SELECT item_id, changed FROM audit") == [{"item_id": "a", "changed": 0}]


def test_a_worlds_trigger_may_read_json_each(tmp_path: Path) -> None:
    sql = """
        INSERT INTO items (id) VALUES ('a');
        UPDATE items SET tags = '["red", "blue"]' WHERE id = 'a';
    """
    with build_world(tmp_path, TRIGGERED).instance(None, setup_sql=sql) as live:
        assert rows(live, "SELECT tag FROM audit WHERE tag IS NOT NULL ORDER BY tag") == [
            {"tag": "blue"},
            {"tag": "red"},
        ]


def test_an_added_nodes_trigger_is_trusted_too(tmp_path: Path) -> None:
    host = build_world(tmp_path, "CREATE TABLE orders (id TEXT PRIMARY KEY) STRICT;", name="shop")
    payments = World(
        "payments",
        "1.0.0",
        """
        CREATE TABLE charges (id TEXT PRIMARY KEY, changed INTEGER) STRICT;
        CREATE TRIGGER charges_counted AFTER INSERT ON charges BEGIN
            UPDATE charges SET changed = changes() WHERE id = new.id;
        END;
        """,
        state_format="seahaven.state/1",
    )
    host.add_world(payments, name="payments")

    sql = "INSERT INTO payments.charges (id) VALUES ('c1')"
    with host.instance(None, setup_sql=sql) as live:
        assert rows(live, "SELECT id, changed FROM payments.charges") == [
            {"id": "c1", "changed": 0}
        ]


def test_a_worlds_view_is_held_to_the_allowlists(tmp_path: Path) -> None:
    with pytest.raises(WorldBug) as error:
        build_world(tmp_path, TRIGGERED).instance(None, setup_sql="SELECT * FROM item_changes")

    assert first_line(error) == (
        "setup_sql statement 1 of 1 was refused: function changes() is not allowed. setup_sql "
        "has the functions run_sql has."
    )


def test_dml_on_a_view_runs_its_instead_of_triggers(tmp_path: Path) -> None:
    sql = """
        INSERT INTO items (id) VALUES ('a'), ('b');
        UPDATE live_items SET tags = '["red"]' WHERE id = 'a';
        DELETE FROM live_items WHERE id = 'b';
    """
    with build_world(tmp_path, TRIGGERED).instance(None, setup_sql=sql) as live:
        assert rows(live, "SELECT id, tags FROM items") == [{"id": "a", "tags": '["red"]'}]


@pytest.mark.parametrize("schema", [TRIGGERED, TRIGGERED + NAMESAKE], ids=["view", "namesake"])
@pytest.mark.parametrize(
    ("statement", "message"),
    [
        (
            "UPDATE live_items SET tags = (SELECT count(*) FROM sqlite_master)",
            "read of table 'sqlite_master' is not allowed. setup_sql may only read and write the "
            "world's own tables.",
        ),
        (
            "DELETE FROM live_items WHERE sqlite_version() IS NOT NULL",
            "function sqlite_version() is not allowed. setup_sql has the functions run_sql has.",
        ),
    ],
)
def test_the_callers_own_sql_in_dml_on_a_view_is_not_trusted(
    tmp_path: Path, schema: str, statement: str, message: str
) -> None:
    with pytest.raises(WorldBug) as error:
        build_world(tmp_path, schema).instance(None, setup_sql=statement)

    assert first_line(error) == f"setup_sql statement 1 of 1 was refused: {message}"


def test_a_cte_named_after_a_world_trigger_is_a_documented_residual(tmp_path: Path) -> None:
    """SQLite's auth context names a CTE as it names a trigger, by name alone.

    So a CTE the caller names after a world trigger shares that trigger's trust
    for function calls and reads. Accepted: setup SQL comes from the eval's
    author, and these checks catch mistakes rather than stop an attacker. Writes,
    schema changes, attaches and pragmas stay refused whatever the CTE is called.
    """
    sql = """
        WITH items_counted AS (
            SELECT name || '-' || typeof(sqlite_version()) AS v
            FROM sqlite_master WHERE name = 'items'
        )
        INSERT INTO items (id) SELECT v FROM items_counted
    """
    with build_world(tmp_path, TRIGGERED).instance(None, setup_sql=sql) as live:
        # It ran and was not refused: nothing here tells the CTE from the trigger.
        assert rows(live, "SELECT id FROM items") == [{"id": "items-text"}]

    with pytest.raises(WorldBug, match="was refused: function sqlite_version"):
        build_world(tmp_path, TRIGGERED).instance(
            None,
            setup_sql="WITH named AS (SELECT sqlite_version() AS v) "
            "INSERT INTO items (id) SELECT v FROM named",
        )


def test_a_trigger_that_shares_a_views_name_is_not_trusted(tmp_path: Path) -> None:
    sql = "INSERT INTO items (id) VALUES ('a'); DELETE FROM items"
    with pytest.raises(WorldBug) as error:
        build_world(tmp_path, TRIGGERED + NAMESAKE).instance(None, setup_sql=sql)

    assert first_line(error) == (
        "setup_sql statement 2 of 2 was refused: function changes() is not allowed. setup_sql "
        "has the functions run_sql has."
    )


@pytest.mark.parametrize(
    ("statement", "message"),
    [
        (
            "INSERT INTO items (id) VALUES (changes())",
            "function changes() is not allowed. setup_sql has the functions run_sql has.",
        ),
        (
            "SELECT * FROM item_changes WHERE changes() >= 0",
            "function changes() is not allowed. setup_sql has the functions run_sql has.",
        ),
        (
            "UPDATE items SET tags = (SELECT json_group_array(value) FROM json_each('[1]'))",
            "read of table 'json_each' is not allowed. setup_sql may only read and write the "
            "world's own tables.",
        ),
        (
            "INSERT INTO items (id) SELECT name FROM sqlite_master",
            "read of table 'sqlite_master' is not allowed. setup_sql may only read and write the "
            "world's own tables.",
        ),
    ],
)
def test_the_callers_own_sql_is_still_refused_beside_a_worlds_trigger(
    tmp_path: Path, statement: str, message: str
) -> None:
    with pytest.raises(WorldBug) as error:
        build_world(tmp_path, TRIGGERED).instance(None, setup_sql=statement)

    assert first_line(error) == f"setup_sql statement 1 of 1 was refused: {message}"


def test_a_startup_hook_sees_the_rows_setup_sql_wrote(tmp_path: Path) -> None:
    world = leaf(tmp_path)

    @world.instance_startup
    def sign_in(ctx: Ctx, *, user_id: str) -> None:
        ctx.state["user"] = ctx.db.one("SELECT name FROM users WHERE id = ?", user_id)

    sql = "INSERT INTO users (id, name) VALUES ('u9', 'Grace')"
    with world.instance(None, setup_sql=sql, startup={"user_id": "u9"}) as live:
        assert live.ctx.state["user"] == {"name": "Grace"}


def test_the_same_seed_and_setup_sql_give_the_same_rows(tmp_path: Path) -> None:
    sql = "INSERT INTO users (id, name) VALUES (hex(randomblob(8)), random())"

    def drawn(seed: int) -> list[dict[str, Any]]:
        with leaf(tmp_path).instance(None, seed=seed, setup_sql=sql) as live:
            return rows(live, "SELECT id, name FROM users")

    assert drawn(7) == drawn(7)
    assert drawn(7) != drawn(8)


def test_random_draws_from_the_roots_stream_whichever_node_the_row_lands_in(
    tmp_path: Path,
) -> None:
    world = composed(tmp_path)
    into_root = "INSERT INTO orders (id, total) VALUES (hex(randomblob(8)), 1)"
    into_node = "INSERT INTO payments.charges (id, amount) VALUES (hex(randomblob(8)), 1)"

    with world.instance(None, seed=7, setup_sql=into_root) as live:
        expected = rows(live, "SELECT id FROM orders")
    with world.instance(None, seed=7, setup_sql=into_node) as live:
        assert rows(live, "SELECT id FROM payments.charges") == expected


def test_setup_sql_draws_from_a_stream_of_its_own(tmp_path: Path) -> None:
    """An instance's own draws are the same whether or not setup SQL drew first."""
    world = leaf(tmp_path)
    draw = "SELECT random() AS r, hex(randomblob(4)) AS b"

    with world.instance(None, seed=7) as live:
        expected = live.call("rows", sql=draw)
    with world.instance(None, seed=7, setup_sql="SELECT random(), randomblob(4)") as live:
        assert live.call("rows", sql=draw) == expected
    with world.instance(
        None, seed=7, setup_sql="INSERT INTO users VALUES (random(), hex(randomblob(4)))"
    ) as live:
        assert rows(live, "SELECT CAST(id AS INTEGER) AS r, name AS b FROM users") != expected


def test_the_clock_functions_read_the_instances_clock(tmp_path: Path) -> None:
    sql = "INSERT INTO users (id, name) VALUES ('u1', datetime('now'))"
    with leaf(tmp_path).instance(None, now=INSTANT_ISO, clock_mode="fixed", setup_sql=sql) as live:
        assert rows(live, "SELECT name FROM users") == [{"name": INSTANT_ISO}]


def test_setup_sql_rows_are_not_in_the_change_log(tmp_path: Path) -> None:
    sql = "INSERT INTO users (id, name) VALUES ('u1', 'Ada')"
    with leaf(tmp_path).instance(None, setup_sql=sql) as live:
        assert (live.change_log(), live.call_count) == ([], 0)

        live.call("execute", sql="UPDATE users SET name = 'Grace' WHERE id = 'u1'")

        assert [(r.i, r.table, r.op, r.before, r.after) for r in live.change_log()] == [
            (0, "users", "update", {"name": "Ada"}, {"name": "Grace"})
        ]


def test_setup_sql_is_kept_on_the_instance(tmp_path: Path) -> None:
    world = leaf(tmp_path)
    sql = "  INSERT INTO users VALUES ('u1', 'Ada');  "
    with world.instance(None, setup_sql=sql) as live:
        assert live.setup_sql == sql
    with world.instance(None) as live:
        assert live.setup_sql is None


@pytest.mark.parametrize("sql", ["", "  \n\t ", "-- nothing to do", ";;", "/* a */ ; -- b"])
def test_an_empty_string_whitespace_or_a_comment_changes_nothing(tmp_path: Path, sql: str) -> None:
    with leaf(tmp_path).instance(None, setup_sql=sql) as live:
        assert rows(live, "SELECT count(*) AS n FROM users") == [{"n": 0}]
        assert live.setup_sql == sql


# ------------------------------------------------------------------- refusals


@pytest.mark.parametrize(
    ("statement", "message"),
    [
        ("CREATE TABLE extra (x)", f"was refused: it changes the schema. {SCHEMA_FIX}"),
        ("DROP TABLE users", f"was refused: it changes the schema. {SCHEMA_FIX}"),
        (
            "CREATE INDEX by_name ON users (name)",
            f"was refused: it changes the schema. {SCHEMA_FIX}",
        ),
        ("ALTER TABLE users ADD COLUMN age", f"was refused: it changes the schema. {SCHEMA_FIX}"),
        (
            "CREATE TRIGGER t AFTER INSERT ON users BEGIN SELECT 1; END",
            f"was refused: it changes the schema. {SCHEMA_FIX}",
        ),
        ("CREATE VIEW v AS SELECT 1", f"was refused: it changes the schema. {SCHEMA_FIX}"),
        ("REINDEX", f"was refused: it changes the schema. {SCHEMA_FIX}"),
        ("ANALYZE", f"was refused: it changes the schema. {SCHEMA_FIX}"),
        ("BEGIN", f"was refused: it controls the transaction. {TRANSACTION_FIX}"),
        ("COMMIT", f"was refused: it controls the transaction. {TRANSACTION_FIX}"),
        ("ROLLBACK", f"was refused: it controls the transaction. {TRANSACTION_FIX}"),
        ("SAVEPOINT s", f"was refused: it controls the transaction. {TRANSACTION_FIX}"),
        ("RELEASE s", f"was refused: it controls the transaction. {TRANSACTION_FIX}"),
        (
            "ATTACH 'elsewhere.sqlite' AS elsewhere",
            f"was refused: it attaches or detaches a database. {ATTACH_FIX} "
            "(this world adds none).",
        ),
        (
            "DETACH main",
            f"was refused: it attaches or detaches a database. {ATTACH_FIX} "
            "(this world adds none).",
        ),
        (
            "PRAGMA journal_mode = DELETE",
            f"was refused: PRAGMA journal_mode can change the connection. {PRAGMA_FIX}",
        ),
        (
            "PRAGMA foreign_keys = OFF",
            f"was refused: PRAGMA foreign_keys can change the connection. {PRAGMA_FIX}",
        ),
        (
            "SELECT load_extension('evil')",
            "was refused: function load_extension() is not allowed. setup_sql has the functions "
            "run_sql has.",
        ),
        (
            "SELECT * FROM sqlite_master",
            "was refused: read of table 'sqlite_master' is not allowed. setup_sql may only read "
            "and write the world's own tables.",
        ),
        (
            "INSERT INTO issues_fts_data (id, block) VALUES (99, x'00')",
            "failed: table issues_fts_data may not be modified.",
        ),
        ("UPDATE sqlite_master SET sql = ''", "failed: table sqlite_master may not be modified."),
    ],
)
def test_a_refused_statement_names_what_was_refused(
    tmp_path: Path, statement: str, message: str
) -> None:
    sql = f"INSERT INTO users (id, name) VALUES ('u1', 'Ada'); {statement}"
    with pytest.raises(WorldBug) as error:
        leaf(tmp_path).instance(None, setup_sql=sql)

    assert first_line(error) == f"setup_sql statement 2 of 2 {message}"
    assert str(error.value).splitlines()[1] == f"  {statement}"
    assert isinstance(error.value.__cause__, apsw.Error)


def test_a_refusal_leaves_no_instance_directory(tmp_path: Path) -> None:
    world = leaf(tmp_path)
    with pytest.raises(WorldBug, match="statement 2 of 2 was refused"):
        world.instance(None, setup_sql="INSERT INTO users VALUES ('u1', 'Ada'); DROP TABLE users")

    assert instance_dirs(tmp_path) == []
    with world.instance(None) as live:
        assert rows(live, "SELECT count(*) AS n FROM users") == [{"n": 0}]
        assert len(instance_dirs(tmp_path)) == 1


def test_an_attach_in_a_composed_world_names_its_schemas(tmp_path: Path) -> None:
    with pytest.raises(WorldBug) as error:
        composed(tmp_path).instance(None, setup_sql="DETACH payments")

    assert first_line(error) == (
        "setup_sql statement 1 of 1 was refused: it attaches or detaches a database. "
        f"{ATTACH_FIX} (this world: payments, payments__tax)."
    )


def test_a_syntax_error_names_the_statement_and_sqlites_message(tmp_path: Path) -> None:
    with pytest.raises(WorldBug) as error:
        leaf(tmp_path).instance(None, setup_sql="SELECT 1; SELEC 2; SELECT 3")

    assert first_line(error) == 'setup_sql statement 2 of 3 failed: near "SELEC": syntax error.'


def test_a_trailing_fragment_is_reported_by_sqlite(tmp_path: Path) -> None:
    sql = "SELECT 1; INSERT INTO users VALUES ('u1', ';'"
    with pytest.raises(WorldBug) as error:
        leaf(tmp_path).instance(None, setup_sql=sql)

    assert first_line(error) == "setup_sql statement 2 of 2 failed: incomplete input."


def test_a_constraint_failure_fails_the_instance(tmp_path: Path) -> None:
    sql = "INSERT INTO issues (id, title, owner_id) VALUES ('i1', 'x', 'nobody')"
    with pytest.raises(WorldBug) as error:
        leaf(tmp_path).instance(None, setup_sql=sql)

    assert first_line(error) == (
        "setup_sql statement 1 of 1 failed: FOREIGN KEY constraint failed."
    )
    assert isinstance(error.value.__cause__, apsw.ConstraintError)
    assert instance_dirs(tmp_path) == []


def test_a_deferred_constraint_that_fails_at_commit_is_a_world_bug(tmp_path: Path) -> None:
    world = build_world(
        tmp_path,
        """
        CREATE TABLE parents (id TEXT PRIMARY KEY) STRICT;
        CREATE TABLE children (
            id TEXT PRIMARY KEY,
            parent_id TEXT REFERENCES parents (id) DEFERRABLE INITIALLY DEFERRED
        ) STRICT;
        """,
    )
    with pytest.raises(WorldBug) as error:
        world.instance(None, setup_sql="INSERT INTO children VALUES ('c1', 'nobody')")

    assert str(error.value) == (
        "setup_sql failed at commit: FOREIGN KEY constraint failed. A deferred constraint is "
        "checked once every statement has run."
    )
    assert isinstance(error.value.__cause__, apsw.ConstraintError)
    assert instance_dirs(tmp_path) == []

    sql = "INSERT INTO children VALUES ('c1', 'p1'); INSERT INTO parents VALUES ('p1')"
    with world.instance(None, setup_sql=sql) as live:
        assert rows(live, "SELECT parent_id FROM children") == [{"parent_id": "p1"}]


def test_a_missing_table_in_a_composed_world_names_the_schemas(tmp_path: Path) -> None:
    with pytest.raises(WorldBug) as error:
        composed(tmp_path).instance(None, setup_sql="DELETE FROM refunds")

    assert first_line(error) == (
        "setup_sql statement 1 of 1 failed: no such table: refunds. An added node's tables are "
        "named <schema>.<table> (this world: payments, payments__tax)."
    )


def test_a_missing_table_in_a_leaf_world_has_no_hint(tmp_path: Path) -> None:
    with pytest.raises(WorldBug) as error:
        leaf(tmp_path).instance(None, setup_sql="DELETE FROM charges")

    assert first_line(error) == "setup_sql statement 1 of 1 failed: no such table: charges."


def test_a_long_statement_is_collapsed_and_cut_in_the_message(tmp_path: Path) -> None:
    statement = "INSERT INTO nowhere\n    VALUES ('" + "x" * 300 + "')"
    with pytest.raises(WorldBug) as error:
        leaf(tmp_path).instance(None, setup_sql=statement)

    quoted = str(error.value).splitlines()[1]
    assert quoted == "  INSERT INTO nowhere VALUES ('" + "x" * 171 + "..."


@pytest.mark.parametrize("value", [["DELETE FROM users"], b"DELETE FROM users", 3])
def test_a_non_string_setup_sql_is_refused_before_anything_is_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: object
) -> None:
    def no_build(*_: object, **__: object) -> None:
        raise AssertionError("built an instance before refusing setup_sql")

    monkeypatch.setattr("seahaven.instances.build_blank", no_build)
    with pytest.raises(WorldBug) as error:
        leaf(tmp_path).instance(None, setup_sql=value)  # ty: ignore[invalid-argument-type]

    assert str(error.value) == (
        f"setup_sql takes a string of SQL statements separated by ';', not {type(value).__name__}"
    )
    assert instance_dirs(tmp_path) == []


# ---------------------------------------------------------- run_setup_sql alone


@pytest.fixture
def files(tmp_path: Path) -> tuple[Path, Path]:
    """A root file and one node's file, as a new composed instance has them."""
    clock = Clock.from_iso(INSTANT_ISO)
    root, node = tmp_path / "state.sqlite", tmp_path / "state.payments.sqlite"
    build_blank(root, "CREATE TABLE orders (id TEXT PRIMARY KEY);", clock=clock, seed=b"s").close()
    build_blank(node, "CREATE TABLE charges (id TEXT PRIMARY KEY);", clock=clock, seed=b"s").close()
    return root, node


def test_a_failure_rolls_back_every_file(files: tuple[Path, Path]) -> None:
    root, node = files
    sql = """
        INSERT INTO orders VALUES ('o1');
        INSERT INTO payments.charges VALUES ('c1');
        INSERT INTO orders VALUES ('o1');
    """
    with pytest.raises(WorldBug, match="statement 3 of 3 failed: UNIQUE constraint failed"):
        run_setup_sql(
            sql,
            root=root,
            attachments=[("payments", node)],
            clock=Clock.from_iso(INSTANT_ISO),
            seed=b"s",
        )

    assert _count(root, "orders") == 0
    assert _count(node, "charges") == 0


def test_an_on_conflict_rollback_that_ends_the_transaction_is_still_a_clean_failure(
    files: tuple[Path, Path],
) -> None:
    root, _ = files
    sql = "INSERT INTO orders VALUES ('o1'); INSERT OR ROLLBACK INTO orders VALUES ('o1')"
    with pytest.raises(WorldBug, match="statement 2 of 2 failed: UNIQUE constraint failed"):
        run_setup_sql(sql, root=root, attachments=[], clock=Clock.from_iso(INSTANT_ISO), seed=b"s")

    assert _count(root, "orders") == 0


def test_success_commits_every_file(files: tuple[Path, Path]) -> None:
    root, node = files
    run_setup_sql(
        "INSERT INTO orders VALUES ('o1'); INSERT INTO payments.charges VALUES ('c1')",
        root=root,
        attachments=[("payments", node)],
        clock=Clock.from_iso(INSTANT_ISO),
        seed=b"s",
    )

    assert (_count(root, "orders"), _count(node, "charges")) == (1, 1)


def _count(path: Path, table: str) -> int:
    with closing(apsw.Connection(str(path))) as conn:
        return int(conn.execute(f"SELECT count(*) FROM {table}").get)


# -------------------------------------------------------------- the splitter


@pytest.mark.parametrize(
    ("sql", "statements"),
    [
        ("SELECT 1; SELECT 2;", ["SELECT 1", "SELECT 2"]),
        ("SELECT 1; SELECT 2", ["SELECT 1", "SELECT 2"]),
        ("SELECT 'a;b'; SELECT 2", ["SELECT 'a;b'", "SELECT 2"]),
        ('SELECT 1 AS "x;y"; SELECT 2', ['SELECT 1 AS "x;y"', "SELECT 2"]),
        ("SELECT 1 /* ; */; SELECT 2", ["SELECT 1 /* ; */", "SELECT 2"]),
        (
            "UPDATE a SET t = 'x;y'; -- c;\n CREATE TRIGGER r AFTER INSERT ON a BEGIN "
            "SELECT 1; SELECT 2; END; SELECT 3",
            [
                "UPDATE a SET t = 'x;y'",
                "-- c;\n CREATE TRIGGER r AFTER INSERT ON a BEGIN SELECT 1; SELECT 2; END",
                "SELECT 3",
            ],
        ),
        ("SELECT 1; SELECT 'open", ["SELECT 1", "SELECT 'open"]),
        ("SELECT 1; -- done", ["SELECT 1", "-- done"]),
        ("", []),
        ("  \n ", []),
        (" ; ;\n;", []),
    ],
)
def test_split_statements(sql: str, statements: list[str]) -> None:
    assert split_statements(sql) == statements


# ------------------------------------------------------------- the error text


def test_an_action_with_no_reason_of_its_own_is_described_by_the_sandbox() -> None:
    refused = Refused(apsw.SQLITE_READ, "secrets", "value", "read of table 'secrets'")

    assert _explain(refused, apsw.AuthError("not authorized"), []) == (
        "read of table 'secrets' is not allowed.",
        "setup_sql may only read and write the world's own tables.",
    )


@pytest.mark.parametrize("table", ["sqlite_master", "sqlite_schema", "SQLITE_TEMP_MASTER"])
def test_a_write_to_a_schema_table_is_a_schema_change(table: str) -> None:
    refused = Refused(apsw.SQLITE_UPDATE, table, None, f"action UPDATE {table!r}")

    assert _explain(refused, apsw.AuthError("not authorized"), []) == (
        "it changes the schema.",
        SCHEMA_FIX,
    )


def test_a_write_to_another_sqlite_table_is_refused_but_not_as_a_schema_change(
    tmp_path: Path,
) -> None:
    world = build_world(
        tmp_path, "CREATE TABLE counted (n INTEGER PRIMARY KEY AUTOINCREMENT, v TEXT) STRICT;"
    )
    with pytest.raises(WorldBug) as error:
        world.instance(None, setup_sql="DELETE FROM sqlite_sequence")

    assert first_line(error) == (
        "setup_sql statement 1 of 1 was refused: action DELETE 'sqlite_sequence' is not "
        "allowed. setup_sql may only read and write the world's own tables."
    )


def test_a_plain_sqlite_error_other_than_a_missing_table_has_no_fix() -> None:
    error = apsw.SQLError('near "x": syntax error')

    assert _explain(None, error, ["payments"]) == ('near "x": syntax error.', "")


def test_the_authorizer_records_the_first_refusal_until_reset() -> None:
    authorizer = SetupAuthorizer(["users"])

    assert authorizer(apsw.SQLITE_INSERT, "users", None, "main", None) == apsw.SQLITE_OK
    assert authorizer(apsw.SQLITE_READ, "other", "x", "main", None) == apsw.SQLITE_DENY
    assert authorizer(apsw.SQLITE_TRANSACTION, "BEGIN", None, None, None) == apsw.SQLITE_DENY
    assert authorizer.refused == Refused(apsw.SQLITE_READ, "other", "x", "read of table 'other'")

    authorizer.reset()

    assert authorizer.refused is None
    assert authorizer.refusals == ()


@pytest.mark.parametrize("pragma", ["table_info", "TABLE_LIST", "foreign_key_check"])
def test_the_authorizer_allows_the_read_only_pragmas(pragma: str) -> None:
    authorizer = SetupAuthorizer([])

    assert authorizer(apsw.SQLITE_PRAGMA, pragma, "users", "main", None) == apsw.SQLITE_OK
    assert authorizer.refused is None


def test_the_quoted_statement_collapses_whitespace() -> None:
    with pytest.raises(WorldBug) as error:
        run_setup_sql(
            "SELECT\n\n   nope   FROM\tnowhere",
            root=Path(":memory:"),
            attachments=[],
            clock=Clock.from_iso(INSTANT_ISO),
            seed=b"s",
        )

    assert re.fullmatch(r".*\n  SELECT nope FROM nowhere", str(error.value), re.DOTALL)
