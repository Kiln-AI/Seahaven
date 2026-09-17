"""The fold: the net diff of an episode, computed from its change log.

`functional_spec.md` §3.6 defines the fold so that any reader computes the same
net diff from a saved document, and SQLite is the oracle it was defined from:
the instance's own long-lived session has been recording the whole episode, and
its changeset is the net diff by construction. Every episode shape the suite
exercises is folded and compared against it.
"""

from collections.abc import Callable
from pathlib import Path

import pytest

from seahaven.instances import Instance
from seahaven.world import World
from tests.conftest import build_world
from tests.support.fold import fold, net_of_changes
from tests.test_changes import BLOB_KEY_SCHEMA, add

# A fixture is what makes an update and a delete possible at all: a row has to
# exist before the episode starts for a call to change one that is not its own.
STARTING_ROWS = (("n1", "first", 1), ("n2", "second", 2))


def write(instance: Instance, sql: str) -> None:
    instance.call("execute", sql=sql)


def insert(instance: Instance) -> None:
    add(instance, "n3", "third", 3)


def insert_then_update(instance: Instance) -> None:
    add(instance, "n3", "third", 3)
    write(instance, "UPDATE notes SET n = 9 WHERE id = 'n3'")


def insert_then_delete(instance: Instance) -> None:
    add(instance, "n3", "third", 3)
    write(instance, "DELETE FROM notes WHERE id = 'n3'")


def update_then_update(instance: Instance) -> None:
    write(instance, "UPDATE notes SET n = 9 WHERE id = 'n1'")
    write(instance, "UPDATE notes SET body = 'rewritten', n = 10 WHERE id = 'n1'")


def update_then_update_back(instance: Instance) -> None:
    write(instance, "UPDATE notes SET n = 9 WHERE id = 'n1'")
    write(instance, "UPDATE notes SET n = 1 WHERE id = 'n1'")


def update_then_delete(instance: Instance) -> None:
    write(instance, "UPDATE notes SET body = 'rewritten' WHERE id = 'n1'")
    write(instance, "DELETE FROM notes WHERE id = 'n1'")


def delete_then_insert_something_else(instance: Instance) -> None:
    write(instance, "DELETE FROM notes WHERE id = 'n1'")
    add(instance, "n1", "replaced", 7)


def delete_then_insert_the_same_row(instance: Instance) -> None:
    write(instance, "DELETE FROM notes WHERE id = 'n1'")
    add(instance, "n1", "first", 1)


def rewrite_a_primary_key(instance: Instance) -> None:
    """A delete plus an insert, in the log and in the changeset alike."""
    write(instance, "UPDATE notes SET id = 'n9' WHERE id = 'n1'")


def calls_around_bulk(instance: Instance) -> None:
    write(instance, "UPDATE notes SET n = 9 WHERE id = 'n1'")
    with instance.bulk() as ctx:
        ctx.db.execute("UPDATE notes SET body = 'in bulk' WHERE id = 'n1'")
        ctx.db.execute("INSERT INTO notes VALUES ('n4', 'fourth', 4)")
    write(instance, "DELETE FROM notes WHERE id = 'n2'")


def a_bit_of_everything(instance: Instance) -> None:
    insert_then_update(instance)
    update_then_delete(instance)
    delete_then_insert_something_else(instance)
    write(instance, "UPDATE notes SET n = 22 WHERE id = 'n2'")


EPISODES: tuple[Callable[[Instance], None], ...] = (
    insert,
    insert_then_update,
    insert_then_delete,
    update_then_update,
    update_then_update_back,
    update_then_delete,
    delete_then_insert_something_else,
    delete_then_insert_the_same_row,
    rewrite_a_primary_key,
    calls_around_bulk,
    a_bit_of_everything,
)


@pytest.fixture
def started(tmp_path: Path) -> World:
    """A world with a fixture holding the rows an episode changes."""
    world = build_world(tmp_path)
    with world.instance(None) as instance:
        with instance.bulk() as ctx:
            for id, body, n in STARTING_ROWS:
                ctx.db.execute("INSERT INTO notes VALUES (?, ?, ?)", id, body, n)
        instance.freeze("start", "two notes")
    return world


@pytest.mark.parametrize("episode", EPISODES, ids=lambda episode: episode.__name__)
def test_the_fold_of_the_log_is_the_cumulative_changeset(
    started: World, episode: Callable[[Instance], None]
) -> None:
    with started.instance("start") as instance:
        episode(instance)

        assert fold(instance.change_log()) == net_of_changes(instance.changes())


def test_the_fold_drops_what_cancelled_out(started: World) -> None:
    """Not a vacuous comparison of two empty lists: the log has records and the fold has none."""
    with started.instance("start") as instance:
        insert_then_delete(instance)

        assert len(instance.change_log()) == 2
        assert fold(instance.change_log()) == []


def test_the_fold_is_shorter_than_the_log_it_folds(started: World) -> None:
    with started.instance("start") as instance:
        a_bit_of_everything(instance)

        assert len(fold(instance.change_log())) < len(instance.change_log())


def test_the_log_and_the_fold_sort_blob_keys_the_same_way(tmp_path: Path) -> None:
    """The one key type whose published text orders differently from its raw value.

    The framework sorts the log and a consumer re-sorts its fold, so the two have
    to agree on the base64 -- which is all a consumer has. The parametrised test
    above cannot catch a disagreement, because both sides of its comparison are
    sorted by `fold`.
    """
    world = build_world(tmp_path, BLOB_KEY_SCHEMA)
    with world.instance(None) as instance:
        instance.call("execute", sql="INSERT INTO keyed VALUES (x'00', 'zero'), (x'fb', 'high')")

        log = instance.change_log()

        assert [record.key["k"] for record in log] == ["+w==", "AA=="]
        assert [net.key for net in fold(log)] == [record.key for record in log]
