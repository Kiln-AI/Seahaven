"""`seahaven.http.runtime`: the registry of instances and `dispatch`, with no HTTP.

The server is a thin ASGI layer over these, so the instance rules (creation on
first use, replacement, the limit, seeds, the clock) and the transaction rules are
tested here. The last section drives the same handler through the world's own
tools, which is how an agent reaches it.
"""

import json
import threading
from collections.abc import Callable, Iterator
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from seahaven import SeahavenError, World, WorldBug
from seahaven.clock import TICK, Clock
from seahaven.http import HttpRequest, HttpResponse
from seahaven.http.runtime import (
    PUT_BODY,
    SERVER_OPTIONS,
    CapacityReached,
    CreationFailed,
    Registry,
    check_reset_options,
    dispatch,
)
from seahaven.instances import Instance
from tests.conftest import INSTANT, INSTANT_ISO, Caller
from tests.http_world import NoteError, build, handle

type MakeRegistry = Callable[..., Registry]


@pytest.fixture
def notes_world(tmp_path: Path) -> World:
    return build(fixtures_dir=tmp_path / "fixtures", work_dir=tmp_path / "work")


@pytest.fixture
def make_registry(notes_world: World) -> Iterator[MakeRegistry]:
    """A registry over `notes_world`, closed when the test ends."""
    made: list[Registry] = []

    def make(defaults: dict[str, Any] | None = None, max_instances: int = 100) -> Registry:
        registry = Registry(notes_world, defaults or {}, max_instances)
        made.append(registry)
        return registry

    yield make
    for registry in made:
        registry.close()


@pytest.fixture
def instance(notes_world: World) -> Iterator[Instance]:
    with notes_world.instance(None, now=INSTANT_ISO, clock_mode="fixed") as live:
        yield live


def make_fixture(world: World, fixture_id: str = "demo") -> None:
    """Freeze an instance holding one note as the fixture `fixture_id`."""
    with world.instance(None, now=INSTANT_ISO, clock_mode="fixed") as live:
        live.call("create_note", body="from the fixture")
        live.freeze(fixture_id, "one note")


def request(
    registry: Registry, id: str, method: str, path: str, *, query: str = "", body: Any = None
) -> HttpResponse:
    """One request to instance `id`, as the server makes it."""
    raw = b"" if body is None else json.dumps(body).encode()
    sent = HttpRequest(method, path, query=query, body=raw)
    return registry.run(id, partial(dispatch, handler=handle, request=sent))


def send(instance: Instance, method: str, path: str, *, query: str = "") -> HttpResponse:
    body = json.dumps({"body": "x"}).encode() if method == "POST" else b""
    return dispatch(instance, handle, HttpRequest(method, path, query=query, body=body))


def parsed(response: HttpResponse) -> Any:
    return json.loads(response.body_bytes)


def notes(instance: Instance) -> list[dict[str, Any]]:
    return instance.inspect().rows("SELECT * FROM notes ORDER BY id")


def the_instance(instance: Instance) -> Instance:
    return instance


# -- check_reset_options -------------------------------------------------------------------------


def test_an_unknown_key_is_refused_naming_the_keys_taken(notes_world: World) -> None:
    with pytest.raises(WorldBug) as raised:
        check_reset_options(notes_world, {"fixtrue": "demo"}, source=PUT_BODY)
    message = str(raised.value)
    assert message.startswith('the PUT body does not take "fixtrue"; it takes "clock_mode", ')
    assert '"fixture"' in message
    assert 'startup keywords go inside "startup"' in message


def test_control_tools_is_refused(notes_world: World) -> None:
    with pytest.raises(WorldBug) as raised:
        check_reset_options(notes_world, {"control_tools": True}, source=SERVER_OPTIONS)
    assert str(raised.value).startswith('reset_options does not take "control_tools"; it takes')


def test_the_server_options_must_name_a_fixture_the_world_has(notes_world: World) -> None:
    with pytest.raises(WorldBug) as raised:
        check_reset_options(notes_world, {"fixture": "nope"}, source=SERVER_OPTIONS)
    assert "fixture 'nope', which world notes_api does not have" in str(raised.value)
    assert str(raised.value).endswith("its fixtures are: none")

    make_fixture(notes_world)
    with pytest.raises(WorldBug) as raised:
        check_reset_options(notes_world, {"fixture": "nope"}, source=SERVER_OPTIONS)
    assert str(raised.value).endswith("its fixtures are: 'demo'")
    check_reset_options(notes_world, {"fixture": "demo"}, source=SERVER_OPTIONS)


def test_options_that_are_taken_pass(notes_world: World) -> None:
    check_reset_options(
        notes_world,
        {"fixture": None, "seed": 7, "now": INSTANT_ISO, "clock_mode": "tick", "startup": {}},
        source=SERVER_OPTIONS,
    )


def test_a_put_bodys_fixture_is_left_to_world_instance(notes_world: World) -> None:
    check_reset_options(notes_world, {"fixture": "nope"}, source=PUT_BODY)


# -- Registry: creation, the limit, put, delete, close ---------------------------------------------


def test_run_creates_on_first_use_and_reuses_after(make_registry: MakeRegistry) -> None:
    registry = make_registry()
    first = registry.run("a", the_instance)
    assert registry.run("a", the_instance) is first
    assert registry.run("b", the_instance) is not first


def test_concurrent_first_requests_create_one_instance(
    make_registry: MakeRegistry, notes_world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    created: list[Instance] = []
    real = notes_world.instance

    def counting(*args: Any, **kwargs: Any) -> Instance:
        created.append(real(*args, **kwargs))
        return created[-1]

    monkeypatch.setattr(notes_world, "instance", counting)
    registry = make_registry()
    barrier = threading.Barrier(8)
    seen: list[Instance] = []

    def first_request() -> None:
        barrier.wait()
        seen.append(registry.run("a", the_instance))

    callers = [Caller(first_request) for _ in range(8)]
    for caller in callers:
        caller.start()
    for caller in callers:
        caller.finish()
    assert len(created) == 1
    assert seen == created * 8


def test_the_limit_refuses_one_more_and_leaves_no_slot(make_registry: MakeRegistry) -> None:
    registry = make_registry(max_instances=2)
    registry.run("a", the_instance)
    registry.run("b", the_instance)
    with pytest.raises(CapacityReached) as raised:
        registry.run("c", the_instance)
    assert str(raised.value) == (
        "this server holds at most 2 instances; DELETE /worlds/{id} to free one"
    )
    with pytest.raises(CapacityReached):
        registry.put("c", {})
    assert set(registry._slots) == {"a", "b"}


def test_replacing_does_not_count_and_deleting_frees_a_place(make_registry: MakeRegistry) -> None:
    registry = make_registry(max_instances=2)
    registry.run("a", the_instance)
    registry.run("b", the_instance)
    assert registry.put("a", {}) is False
    assert registry.delete("b") is True
    registry.run("c", the_instance)
    with pytest.raises(CapacityReached):
        registry.run("d", the_instance)


def test_a_failed_creation_frees_its_place(make_registry: MakeRegistry) -> None:
    registry = make_registry(max_instances=1)
    with pytest.raises(CreationFailed):
        registry.put("a", {"startup": {"explode": True}})
    assert registry._slots == {}
    registry.run("b", the_instance)


def test_a_failed_creation_on_first_use_carries_the_cause(make_registry: MakeRegistry) -> None:
    registry = make_registry({"startup": {"explode": True}}, max_instances=1)
    with pytest.raises(CreationFailed) as raised:
        registry.run("a", the_instance)
    assert isinstance(raised.value.cause, RuntimeError)
    assert str(raised.value) == "the startup hook was asked to explode"
    assert registry._slots == {}


def test_a_max_of_zero_means_no_limit(make_registry: MakeRegistry) -> None:
    registry = make_registry(max_instances=0)
    for id in "abc":
        registry.run(id, the_instance)
    assert set(registry._slots) == {"a", "b", "c"}


def test_put_creates_then_replaces_with_a_new_instance(make_registry: MakeRegistry) -> None:
    registry = make_registry()
    assert registry.put("a", {}) is True
    first = registry.run("a", the_instance)
    request(registry, "a", "POST", "/notes", body={"body": "old"})
    assert registry.put("a", {}) is False
    second = registry.run("a", the_instance)
    assert second is not first
    assert first.closed
    assert not first.dir.exists()
    assert notes(second) == []


def test_put_lays_the_body_over_the_defaults(
    make_registry: MakeRegistry, notes_world: World
) -> None:
    make_fixture(notes_world)
    registry = make_registry({"fixture": "demo", "seed": 3})
    assert len(notes(registry.run("a", the_instance))) == 1

    registry.put("b", {"seed": 5})
    b = registry.run("b", the_instance)
    assert (b.fixture, b.caller_seed, len(notes(b))) == ("demo", 5, 1)

    registry.put("c", {"fixture": None})
    c = registry.run("c", the_instance)
    assert (c.fixture, c.caller_seed, notes(c)) == (None, 3, [])


def test_a_null_in_the_body_removes_the_default_rather_than_passing_none(
    make_registry: MakeRegistry,
) -> None:
    registry = make_registry({"seed": 3, "clock_mode": "fixed"})
    registry.put("a", {"seed": None, "clock_mode": None})
    a = registry.run("a", the_instance)
    # Absent, not `None`: the registry's own defaults, a random seed and `wall`.
    assert a.caller_seed is not None
    assert a.clock.mode == "wall"


def test_a_failed_replacement_leaves_the_id_empty(make_registry: MakeRegistry) -> None:
    registry = make_registry()
    registry.put("a", {})
    old = registry.run("a", the_instance)
    with pytest.raises(CreationFailed) as raised:
        registry.put("a", {"fixture": "nope"})
    assert isinstance(raised.value.cause, SeahavenError)
    assert old.closed
    assert registry.delete("a") is False
    assert registry.run("a", the_instance) is not old


def test_delete_destroys_and_a_later_run_creates_again(make_registry: MakeRegistry) -> None:
    registry = make_registry()
    assert registry.delete("a") is False
    first = registry.run("a", the_instance)
    assert registry.delete("a") is True
    assert first.closed
    assert not first.dir.exists()
    assert registry.delete("a") is False
    assert registry.run("a", the_instance) is not first


@pytest.mark.parametrize("operation", ["run", "put"])
def test_an_operation_holding_a_retired_slot_starts_again(
    make_registry: MakeRegistry, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    """What a thread sees when it took the slot, then waited while another thread deleted it."""
    registry = make_registry()
    registry.run("a", the_instance)
    stale = registry._slots["a"]
    registry.delete("a")
    real = registry._slot
    handed_out: list[object] = []

    def stale_first(id: str) -> Any:
        handed_out.append(stale if not handed_out else real(id))
        return handed_out[-1]

    monkeypatch.setattr(registry, "_slot", stale_first)
    if operation == "run":
        created = registry.run("a", the_instance)
    else:
        assert registry.put("a", {}) is True
        created = registry._slots["a"].instance
    assert handed_out[0] is stale and len(handed_out) == 2
    assert stale.instance is None
    assert registry._slots["a"].instance is created is not None


def test_close_destroys_every_instance(make_registry: MakeRegistry) -> None:
    registry = make_registry()
    instances = [registry.run(id, the_instance) for id in ("a", "b")]
    assert all(instance.dir.exists() for instance in instances)
    registry.close()
    assert all(instance.closed and not instance.dir.exists() for instance in instances)
    assert registry._slots == {}


# -- Registry: seeds and the clock -----------------------------------------------------------------


def minted_id(registry: Registry, id: str) -> str:
    return parsed(request(registry, id, "POST", "/notes", body={"body": "x"}))["id"]


def test_instances_without_a_seed_mint_different_ids(make_registry: MakeRegistry) -> None:
    registry = make_registry()
    assert minted_id(registry, "a") != minted_id(registry, "b")


def test_an_explicit_seed_replays(make_registry: MakeRegistry) -> None:
    registry = make_registry({"seed": 7})
    assert minted_id(registry, "a") == minted_id(registry, "b")
    assert registry.run("a", the_instance).caller_seed == 7


def test_a_none_seed_is_passed_through(make_registry: MakeRegistry) -> None:
    registry = make_registry({"seed": None})
    assert registry.run("a", the_instance).caller_seed is None


def test_the_default_clock_mode_is_wall(make_registry: MakeRegistry) -> None:
    assert make_registry().run("a", the_instance).clock.mode == "wall"


def test_a_given_clock_mode_is_kept(make_registry: MakeRegistry, notes_world: World) -> None:
    assert make_registry({"clock_mode": "fixed"}).run("a", the_instance).clock.mode == "fixed"
    world_default = make_registry({"clock_mode": None}).run("a", the_instance).clock.mode
    assert world_default == notes_world.default_clock_mode


def test_each_request_moves_a_tick_clock_one_step(make_registry: MakeRegistry) -> None:
    registry = make_registry({"clock_mode": "tick", "now": INSTANT_ISO})
    read = [parsed(request(registry, "a", "GET", "/context"))["now"] for _ in range(3)]
    assert read == [Clock(INSTANT + step * TICK).iso() for step in (1, 2, 3)]


# -- dispatch ------------------------------------------------------------------------------------


def test_an_error_status_commits_the_write(instance: Instance) -> None:
    response = send(instance, "POST", "/notes", query="reject=1")
    assert response.status == 400
    assert len(notes(instance)) == 1


def test_a_raise_rolls_back_and_answers_500(
    instance: Instance, caplog: pytest.LogCaptureFixture
) -> None:
    response = send(instance, "POST", "/explode")
    assert response.status == 500
    assert response.headers == (("content-type", "application/json"),)
    assert parsed(response) == {
        "seahaven_error": "the handler raised RuntimeError: the handler was asked to explode"
    }
    assert notes(instance) == []
    [record] = [each for each in caplog.records if each.name == "seahaven.http"]
    assert record.getMessage() == "the HTTP handler failed on POST /explode"
    assert record.exc_info is not None


def test_a_non_response_rolls_back_and_answers_500(instance: Instance) -> None:
    response = send(instance, "GET", "/wrong")
    assert response.status == 500
    assert parsed(response) == {"seahaven_error": "the handler returned dict, not an HttpResponse"}
    assert notes(instance) == []


def test_a_request_is_not_a_tool_call(instance: Instance) -> None:
    assert parsed(send(instance, "GET", "/context"))["call_is_none"] is True
    send(instance, "POST", "/notes")
    assert [(record.i, record.op) for record in instance.change_log()] == [(None, "insert")]
    assert instance.call_log() == []


# -- the real entry point: the world's tools over the handler -------------------------------------


def test_the_tools_reach_the_handler(instance: Instance) -> None:
    created = instance.call("create_note", body="hello")
    assert created["body"] == "hello"
    assert instance.call("get_note", note_id=created["id"]) == created


def test_a_missing_note_is_the_tools_error(instance: Instance) -> None:
    with pytest.raises(NoteError) as raised:
        instance.call("get_note", note_id="missing")
    assert (raised.value.code, raised.value.message) == ("not_found", "the notes API answered 404")


def test_the_handlers_write_is_logged_under_the_tool_call(instance: Instance) -> None:
    instance.call("create_note", body="first")
    instance.call("create_note", body="second")
    assert [record.i for record in instance.change_log()] == [0, 1]


def test_a_tool_that_raises_on_an_error_status_rolls_back(instance: Instance) -> None:
    with pytest.raises(NoteError) as raised:
        instance.call("create_note", body="rejected", reject=True)
    assert raised.value.code == "rejected"
    assert notes(instance) == []
    assert instance.change_log() == []
