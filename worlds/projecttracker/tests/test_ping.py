"""`ping`, driven the way an agent drives it: `instance.call("ping", ...)`.

Nothing here calls the function. What is being tested is the tool -- its
schema, its argument model, its result and the transaction it runs in -- and all
of that is the framework's work on the signature, which only a real call does.
"""

from typing import Any

import pytest

import seahaven
from conftest import BLANK_NOW, FIXTURE_NOW
from projecttracker.world import world


def test_ping_answers_with_the_message_the_clock_and_the_user_count(
    tracker: seahaven.Instance,
) -> None:
    """The whole of the placeholder's behaviour, through the whole of the call path."""
    assert tracker.call("ping") == {"message": "pong", "now": FIXTURE_NOW, "users": 0}


def test_ping_echoes_the_message_it_is_given(tracker: seahaven.Instance) -> None:
    assert tracker.call("ping", message="are you there")["message"] == "are you there"


def test_pings_time_is_the_instances_clock_and_not_the_wall_clock(
    blank: seahaven.Instance,
) -> None:
    """A blank instance at a fixed `now` answers with that `now`, to the millisecond.

    The canonical format is the point: one format across every door, and the
    tool reads `ctx.clock.iso()` rather than `datetime.now()`.
    """
    assert blank.call("ping")["now"] == BLANK_NOW


def test_ping_counts_the_users_it_can_see(blank: seahaven.Instance) -> None:
    for n in range(3):
        with blank.bulk() as ctx:
            ctx.db.execute(
                "INSERT INTO users (id, email, name, role, created_at) VALUES (?, ?, ?, ?, ?)",
                ctx.ids.uuid(),
                f"user{n}@example.invalid",
                f"User {n}",
                "member",
                ctx.clock.iso(),
            )
        assert blank.call("ping")["users"] == n + 1


def test_ping_publishes_a_schema_an_agent_can_read(tracker: seahaven.Instance) -> None:
    """The listing OpenEnv serves: a description, one optional string argument."""
    (listed,) = tracker.tools()
    assert listed["name"] == "ping"
    assert "reachable" in listed["description"]
    schema = listed["input_schema"]
    assert schema["properties"]["message"]["default"] == "pong"
    assert schema["properties"]["message"]["maxLength"] == 200
    assert schema.get("required", []) == []
    assert schema["additionalProperties"] is False


def test_ping_refuses_an_argument_it_does_not_have(tracker: seahaven.Instance) -> None:
    """An unknown argument is the agent's mistake, and it is told in this world's words."""
    with pytest.raises(seahaven.ToolError) as raised:
        tracker.call("ping", mesage="typo")
    assert raised.value.code == "INVALID_INPUT"
    assert "mesage" in raised.value.message


@pytest.mark.parametrize(
    ("argument", "expected"),
    [
        pytest.param(5, "message", id="an int where a string belongs"),
        pytest.param(None, "message", id="a null"),
        pytest.param("x" * 201, "message", id="longer than the tool accepts"),
    ],
)
def test_ping_refuses_a_message_its_model_does_not_accept(
    tracker: seahaven.Instance, argument: Any, expected: str
) -> None:
    """Validation is strict and happens inside the chain, so the handler restates it."""
    with pytest.raises(seahaven.ToolError) as raised:
        tracker.call("ping", message=argument)
    assert raised.value.code == "INVALID_INPUT"
    assert raised.value.details == {"field": expected}


def test_an_unknown_tool_is_the_frameworks_error_and_does_not_reach_the_handler(
    tracker: seahaven.Instance,
) -> None:
    """`UnknownTool` is raised before the chain, so this world never restates it.

    Worth pinning rather than assuming: it is the one error an agent can provoke
    that the error handler does not see, and an agent that mistypes a tool name
    needs to be told the name, not `INVALID_INPUT`.
    """
    with pytest.raises(seahaven.UnknownTool) as raised:
        tracker.call("pign")
    assert raised.value.code == "unknown_tool"


def test_ping_is_a_read_and_leaves_no_changes(tracker: seahaven.Instance) -> None:
    """The changeset is what an eval is scored on; a smoke test must not be in it."""
    tracker.call("ping")
    assert tracker.changes() == []


def test_two_instances_of_one_fixture_do_not_see_each_other() -> None:
    """A fixture is copied, never opened, so a write in one is invisible in the other."""
    with world.instance("empty") as first, world.instance("empty") as second:
        with first.bulk() as ctx:
            ctx.db.execute(
                "INSERT INTO users (id, email, name, role, created_at) VALUES ('u', 'a@b.invalid',"
                " 'A', 'admin', '2026-06-01T09:00:00.000Z')"
            )
        assert first.call("ping")["users"] == 1
        assert second.call("ping")["users"] == 0
