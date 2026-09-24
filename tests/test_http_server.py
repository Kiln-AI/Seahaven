"""`seahaven.http.app` over real HTTP: the routes, the instances and the handler.

Every test here starts uvicorn on a real port and talks to it with `httpx`, because
what the transport does to a request and a response (decoding, framing, a header
it refuses) cannot be seen through a test client. The instance rules themselves
are tested without HTTP in `test_http_runtime.py`; this module proves the server
answers each of them with the right status and body.
"""

import logging
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

import seahaven.http
from seahaven import World, WorldBug
from seahaven.clock import TICK, Clock
from seahaven.instances import Instance
from tests.conftest import INSTANT, INSTANT_ISO, Caller
from tests.http_world import build, handle

# `seahaven.http.server` imports Starlette and uvicorn, which both extras
# install. `httpx` comes with the serve extra only, so the mcp job skips this
# module; the server stack is the same version in both.
pytest.importorskip(
    "seahaven.http.server", exc_type=ImportError, reason="the server stack does not import here"
)
pytest.importorskip("httpx", reason="httpx comes with the serve extra")

import httpx

from seahaven.http import server
from tests.serving import serving_app

NOTE = {"body": "hello"}


@pytest.fixture
def notes_world(tmp_path: Path) -> World:
    return build(fixtures_dir=tmp_path / "fixtures", work_dir=tmp_path / "work")


@contextmanager
def served(
    world: World, *, config: dict[str, Any] | None = None, **options: Any
) -> Iterator[httpx.Client]:
    """`seahaven.http.app(world, handle, **options)` on a real port, and a client for it."""
    with (
        serving_app(seahaven.http.app(world, handle, **options), **(config or {})) as url,
        httpx.Client(base_url=url) as client,
    ):
        yield client


@pytest.fixture
def client(notes_world: World) -> Iterator[httpx.Client]:
    with served(notes_world) as live:
        yield live


@pytest.fixture
def created(notes_world: World, monkeypatch: pytest.MonkeyPatch) -> list[Instance]:
    """Every instance `notes_world.instance(...)` creates, in order."""
    made: list[Instance] = []
    real = notes_world.instance

    def counting(*args: Any, **kwargs: Any) -> Instance:
        made.append(real(*args, **kwargs))
        return made[-1]

    monkeypatch.setattr(notes_world, "instance", counting)
    return made


def seahaven_error(response: httpx.Response) -> str:
    """The server's own message, after checking the response is shaped as one."""
    assert response.headers["content-type"] == "application/json"
    body = response.json()
    assert list(body) == ["seahaven_error"]
    return body["seahaven_error"]


def fail_destroy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make `destroy` do its work and then raise, as a failure to remove a directory would."""
    real = Instance.destroy

    def destroy(self: Instance) -> None:
        real(self)
        raise OSError("the disk went away")

    monkeypatch.setattr(Instance, "destroy", destroy)


def make_fixture(world: World) -> None:
    """Freeze an instance holding one note as the fixture `demo`."""
    with world.instance(None, now=INSTANT_ISO, clock_mode="fixed") as live:
        live.call("create_note", body="from the fixture")
        live.freeze("demo", "one note")


# -- routes ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/nope", "/worlds", "/v1/worlds/a/notes"])
def test_a_path_outside_worlds_is_404(client: httpx.Client, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 404
    assert seahaven_error(response) == (
        f"no route {path}: the world's API is under /worlds/{{id}}/, and PUT or DELETE "
        "/worlds/{id} manages an instance"
    )


@pytest.mark.parametrize(
    ("path", "id"), [("/worlds/a.b/notes", "a.b"), ("/worlds/", ""), ("/worlds/%C3%A9", "é")]
)
def test_a_bad_instance_id_is_400(client: httpx.Client, path: str, id: str) -> None:
    response = client.get(path)
    assert response.status_code == 400
    assert seahaven_error(response) == (
        f"instance ids are 1 to 64 letters, digits, '-' or '_', not {id!r}"
    )


def test_an_id_of_64_characters_is_taken_and_65_is_not(client: httpx.Client) -> None:
    assert client.get(f"/worlds/{'a' * 64}/notes").status_code == 200
    assert client.get(f"/worlds/{'a' * 65}/notes").status_code == 400


@pytest.mark.parametrize("method", ["GET", "POST", "PATCH"])
def test_the_instance_path_takes_only_put_and_delete(client: httpx.Client, method: str) -> None:
    response = client.request(method, "/worlds/a")
    assert response.status_code == 405
    assert response.headers["allow"] == "PUT, DELETE"
    assert seahaven_error(response) == (
        "/worlds/{id} takes PUT or DELETE; the world's API is under /worlds/{id}/"
    )


def test_a_websocket_is_refused(client: httpx.Client) -> None:
    pytest.importorskip("websockets")
    from websockets.exceptions import InvalidStatus
    from websockets.sync.client import connect

    url = str(client.base_url).replace("http://", "ws://")
    with pytest.raises(InvalidStatus) as raised:
        connect(f"{url}/worlds/a/notes")
    assert raised.value.response.status_code == 403


# -- forwarding ------------------------------------------------------------------------------------


def test_the_instance_root_reaches_the_handler_as_a_slash(client: httpx.Client) -> None:
    response = client.get("/worlds/a/")
    assert response.status_code == 404
    assert response.json() == {"error": {"type": "no_route", "path": "/"}}


@pytest.mark.parametrize("method", ["POST", "PATCH", "OPTIONS"])
def test_a_request_reaches_the_handler_unchanged(client: httpx.Client, method: str) -> None:
    response = client.request(
        method,
        "/worlds/a/echo/a%20b",
        params=[("x", "1"), ("x", "2")],
        headers=[("X-Trace", "1"), ("x-trace", "2")],
        content=b"hello",
    )
    assert response.status_code == 200
    echoed = response.json()
    assert (echoed["method"], echoed["path"], echoed["query"], echoed["body"]) == (
        method,
        "/echo/a b",
        "x=1&x=2",
        "hello",
    )
    assert ["host", str(client.base_url.netloc.decode())] in echoed["headers"]
    assert [value for name, value in echoed["headers"] if name == "x-trace"] == ["1", "2"]


def test_the_handlers_headers_come_back_with_the_servers_framing(client: httpx.Client) -> None:
    response = client.get("/worlds/a/echo")
    assert response.headers.get_list("set-cookie") == ["a=1", "b=2"]
    # The handler said 9999 and gzip; the server frames the body it actually sends.
    assert response.headers["content-length"] == str(len(response.content))
    assert "transfer-encoding" not in response.headers
    assert response.json()["method"] == "GET"


def test_head_gets_the_headers_and_no_body(client: httpx.Client) -> None:
    response = client.head("/worlds/a/echo")
    assert response.status_code == 200
    assert response.content == b""
    assert response.headers.get_list("set-cookie") == ["a=1", "b=2"]


def test_a_root_path_is_taken_off_the_front(notes_world: World) -> None:
    """Behind a proxy that strips `/api`, uvicorn puts it back in front of the path."""
    with served(notes_world, config={"root_path": "/api"}) as client:
        assert client.get("/worlds/a/echo/x").json()["path"] == "/echo/x"


# -- the handler -----------------------------------------------------------------------------------


def test_any_status_commits_and_a_failure_rolls_back(client: httpx.Client) -> None:
    rejected = client.post("/worlds/a/notes", params={"reject": "1"}, json=NOTE)
    assert rejected.status_code == 400
    assert rejected.json() == {"error": {"type": "rejected"}}

    exploded = client.post("/worlds/a/explode")
    assert exploded.status_code == 500
    assert seahaven_error(exploded) == (
        "the handler raised RuntimeError: the handler was asked to explode"
    )

    wrong = client.get("/worlds/a/wrong")
    assert wrong.status_code == 500
    assert seahaven_error(wrong) == "the handler returned dict, not an HttpResponse"

    assert [note["body"] for note in client.get("/worlds/a/notes").json()] == ["hello"]


@pytest.mark.parametrize(
    ("status", "name", "value"),
    [
        ("103", "x-a", "v"),
        ("204", "x-a", "v"),
        ("304", "x-a", "v"),
        ("200", "", "v"),
        ("200", "a b", "v"),
        ("200", "a:b", "v"),
        ("200", "x-a", "a\x00b"),
        ("200", "x-a", "a\x0bb"),
        ("200", "x-a", "v "),
    ],
)
def test_a_response_the_server_cannot_send_fails_in_the_handler(
    client: httpx.Client, status: str, name: str, value: str
) -> None:
    """Refused where the handler builds it, so the answer is a 500 rather than a dropped line."""
    response = client.get(
        "/worlds/a/respond", params={"status": status, "name": name, "value": value}
    )
    assert response.status_code == 500
    assert seahaven_error(response).startswith("the handler raised ValueError: an HttpResponse")


def test_a_response_at_the_edge_of_what_is_taken_is_sent(client: httpx.Client) -> None:
    response = client.get(
        "/worlds/a/respond",
        params={"status": "299", "name": "x-edge!#$%&'*+.^_`|~", "value": "a\tb Zoë"},
    )
    assert response.status_code == 299
    assert response.headers["x-edge!#$%&'*+.^_`|~"] == "a\tb Zoë"
    assert response.text == "made"


def test_a_first_request_whose_creation_fails_is_500(
    notes_world: World, caplog: pytest.LogCaptureFixture
) -> None:
    with served(notes_world, reset_options={"startup": {"explode": True}}) as client:
        response = client.get("/worlds/a/notes")
    assert response.status_code == 500
    assert seahaven_error(response) == (
        "instance a could not be created: RuntimeError: the startup hook was asked to explode"
    )
    [record] = [each for each in caplog.records if each.name == "seahaven.http"]
    assert record.levelno == logging.ERROR
    assert record.getMessage() == "instance a could not be created"
    assert record.exc_info is not None


# -- PUT -------------------------------------------------------------------------------------------


def test_put_creates_then_replaces(client: httpx.Client) -> None:
    first = client.put("/worlds/a", json={})
    assert (first.status_code, first.json()) == (201, {"id": "a"})
    client.post("/worlds/a/notes", json=NOTE)
    second = client.put("/worlds/a", json={})
    assert (second.status_code, second.json()) == (200, {"id": "a"})
    assert client.get("/worlds/a/notes").json() == []


@pytest.mark.parametrize("body", [b"", b" \n"])
def test_an_empty_put_body_takes_the_servers_options(notes_world: World, body: bytes) -> None:
    options = {"clock_mode": "fixed", "now": INSTANT_ISO}
    with served(notes_world, reset_options=options) as client:
        assert client.put("/worlds/a", content=body).status_code == 201
        assert client.get("/worlds/a/context").json()["now"] == INSTANT_ISO


def test_a_put_body_is_laid_over_the_servers_options(notes_world: World) -> None:
    make_fixture(notes_world)
    with served(notes_world, reset_options={"fixture": "demo"}) as client:
        assert len(client.get("/worlds/a/notes").json()) == 1
        client.put("/worlds/b", json={"fixture": None})
        assert client.get("/worlds/b/notes").json() == []
        client.put("/worlds/c", json={"seed": 5})
        assert len(client.get("/worlds/c/notes").json()) == 1


@pytest.mark.parametrize(
    ("body", "words"),
    [
        (b"{", "the PUT body is not JSON: "),
        (b"\xff", "the PUT body is not JSON: "),
        (b"[" * 100_000, "the PUT body is not JSON: "),
        (b"[1]", "the PUT body is a JSON object of reset options, not a JSON array"),
        (b'"x"', "not a JSON string"),
        (b"null", "not a JSON null"),
        (b'{"fixtrue": "demo"}', 'the PUT body does not take "fixtrue"; it takes "clock_mode"'),
        (b'{"control_tools": true}', 'the PUT body does not take "control_tools"'),
        (b'{"fixture": "nope"}', "world 'notes_api' has no fixture 'nope'"),
        (b'{"clock_mode": "nope"}', "unknown clock mode 'nope'"),
        (b'{"startup": {"nope": 1}}', "unknown startup keyword(s): ['nope']"),
    ],
)
def test_a_put_body_that_is_refused_is_400(
    client: httpx.Client, created: list[Instance], body: bytes, words: str
) -> None:
    response = client.put("/worlds/a", content=body)
    assert response.status_code == 400
    assert words in seahaven_error(response)
    assert created == []
    assert client.delete("/worlds/a").status_code == 404


def test_a_put_whose_startup_fails_is_500_and_leaves_the_id_empty(
    client: httpx.Client, caplog: pytest.LogCaptureFixture
) -> None:
    client.post("/worlds/a/notes", json=NOTE)
    response = client.put("/worlds/a", json={"startup": {"explode": True}})
    assert response.status_code == 500
    assert seahaven_error(response) == (
        "instance a could not be created: RuntimeError: the startup hook was asked to explode"
    )
    assert any(each.exc_info for each in caplog.records if each.name == "seahaven.http")
    assert client.delete("/worlds/a").status_code == 404
    assert client.get("/worlds/a/notes").json() == []


def test_the_limit_is_503_and_replacing_does_not_count(notes_world: World) -> None:
    with served(notes_world, max_instances=1) as client:
        assert client.get("/worlds/a/notes").status_code == 200
        message = "this server holds at most 1 instances; DELETE /worlds/{id} to free one"
        for response in (client.get("/worlds/b/notes"), client.put("/worlds/b", json={})):
            assert response.status_code == 503
            assert seahaven_error(response) == message
        assert client.put("/worlds/a", json={}).status_code == 200
        assert client.delete("/worlds/a").status_code == 204
        assert client.get("/worlds/b/notes").status_code == 200


def test_a_destroy_that_fails_is_500_and_leaves_the_id_empty(
    client: httpx.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    for id in ("a", "b"):
        client.post(f"/worlds/{id}/notes", json=NOTE)
    with monkeypatch.context() as patch:
        fail_destroy(patch)
        replaced = client.put("/worlds/a", json={})
        deleted = client.delete("/worlds/b")
    assert replaced.status_code == deleted.status_code == 500
    assert seahaven_error(replaced) == (
        "instance a could not be replaced: OSError: the disk went away"
    )
    assert seahaven_error(deleted) == (
        "instance b could not be destroyed: OSError: the disk went away"
    )
    for id in ("a", "b"):
        assert client.delete(f"/worlds/{id}").status_code == 404
        assert client.get(f"/worlds/{id}/notes").json() == []


# -- DELETE ----------------------------------------------------------------------------------------


def test_delete_answers_204_then_404_and_a_later_request_creates_again(
    client: httpx.Client,
) -> None:
    client.post("/worlds/a/notes", json=NOTE)
    deleted = client.delete("/worlds/a")
    assert (deleted.status_code, deleted.content) == (204, b"")
    missing = client.delete("/worlds/a")
    assert missing.status_code == 404
    assert seahaven_error(missing) == "no instance a"
    assert client.get("/worlds/a/notes").json() == []


# -- instances -------------------------------------------------------------------------------------


def test_concurrent_first_requests_create_one_instance(
    client: httpx.Client, created: list[Instance]
) -> None:
    barrier = threading.Barrier(8)
    statuses: list[int] = []

    def first_request() -> None:
        barrier.wait()
        statuses.append(client.get("/worlds/a/notes").status_code)

    callers = [Caller(first_request) for _ in range(8)]
    for caller in callers:
        caller.start()
    for caller in callers:
        caller.finish()
    assert statuses == [200] * 8
    assert len(created) == 1


def test_instances_without_a_seed_mint_different_ids(client: httpx.Client) -> None:
    minted = {client.post(f"/worlds/{id}/notes", json=NOTE).json()["id"] for id in "ab"}
    assert len(minted) == 2


def test_each_request_moves_a_tick_clock_one_step(notes_world: World) -> None:
    options = {"clock_mode": "tick", "now": INSTANT_ISO}
    with served(notes_world, reset_options=options) as client:
        read = [client.get("/worlds/a/context").json()["now"] for _ in range(3)]
    assert read == [Clock(INSTANT + step * TICK).iso() for step in (1, 2, 3)]


def test_stopping_the_server_destroys_every_instance(
    notes_world: World, created: list[Instance]
) -> None:
    with served(notes_world) as client:
        for id in "ab":
            client.get(f"/worlds/{id}/notes")
        assert all(instance.dir.exists() for instance in created)
    assert len(created) == 2
    assert all(instance.closed and not instance.dir.exists() for instance in created)


# -- app() and serve() -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("arguments", "words"),
    [
        (
            {"handler": "handle"},
            "handler must be a function (ctx, request) -> HttpResponse, not str",
        ),
        ({"max_instances": -1}, "max_instances is an int of 0 or more, 0 for no limit, not -1"),
        ({"max_instances": True}, "not True"),
        ({"max_instances": 1.5}, "not 1.5"),
        ({"reset_options": {"fixtrue": "demo"}}, 'reset_options does not take "fixtrue"'),
        ({"reset_options": {"control_tools": True}}, 'reset_options does not take "control_tools"'),
        ({"reset_options": {"fixture": "nope"}}, "names the fixture 'nope'"),
    ],
)
def test_app_refuses_what_would_fail_later(
    notes_world: World, arguments: dict[str, Any], words: str
) -> None:
    handler = arguments.pop("handler", handle)
    with pytest.raises(WorldBug) as raised:
        seahaven.http.app(notes_world, handler, **arguments)
    assert words in str(raised.value)


@pytest.mark.parametrize("missing", ["uvicorn", "starlette"])
def test_without_the_server_stack_app_and_serve_say_to_install_the_extra(
    notes_world: World, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    monkeypatch.delitem(sys.modules, "seahaven.http.server")
    # The package and every submodule already imported, as if none were installed.
    for name in [each for each in sys.modules if each.partition(".")[0] == missing]:
        monkeypatch.setitem(sys.modules, name, None)
    with pytest.raises(ImportError) as raised:
        seahaven.http.app(notes_world, handle)
    assert str(raised.value) == seahaven.http.MISSING_EXTRA
    with pytest.raises(ImportError) as raised:
        seahaven.http.serve(notes_world, handle)
    assert str(raised.value) == seahaven.http.MISSING_EXTRA


def test_another_missing_module_is_not_blamed_on_the_extra(
    notes_world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "seahaven.http.server", None)
    with pytest.raises(ModuleNotFoundError) as raised:
        seahaven.http.app(notes_world, handle)
    assert str(raised.value) != seahaven.http.MISSING_EXTRA


@pytest.fixture
def uvicorn_run(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """What `serve` handed `uvicorn.run`, instead of serving for ever."""
    call: dict[str, Any] = {}

    def run(app: Any, **kwargs: Any) -> None:
        call["app"] = app
        call["kwargs"] = kwargs

    monkeypatch.setattr(server.uvicorn, "run", run)
    return call


def test_serve_prints_the_base_url_and_runs_one_worker(
    notes_world: World, uvicorn_run: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    seahaven.http.serve(notes_world, handle)
    assert capsys.readouterr().out == "Serving notes_api at http://127.0.0.1:8000/worlds/{id}\n"
    assert uvicorn_run["kwargs"] == {
        "host": "127.0.0.1",
        "port": 8000,
        "workers": 1,
        "log_level": "info",
    }
    assert callable(uvicorn_run["app"])


def test_serve_builds_the_app_from_its_arguments(
    notes_world: World, uvicorn_run: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    seahaven.http.serve(notes_world, handle, host="0.0.0.0", port=9000, max_instances=1)
    assert capsys.readouterr().out == "Serving notes_api at http://127.0.0.1:9000/worlds/{id}\n"
    assert (uvicorn_run["kwargs"]["host"], uvicorn_run["kwargs"]["port"]) == ("0.0.0.0", 9000)
    with serving_app(uvicorn_run["app"]) as url, httpx.Client(base_url=url) as client:
        client.get("/worlds/a/notes")
        assert client.get("/worlds/b/notes").status_code == 503


def test_serve_refuses_bad_options_before_printing(
    notes_world: World, uvicorn_run: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(WorldBug):
        seahaven.http.serve(notes_world, handle, reset_options={"nope": 1})
    assert capsys.readouterr().out == ""
    assert uvicorn_run == {}


@pytest.mark.parametrize(
    ("host", "url"),
    [
        ("127.0.0.1", "http://127.0.0.1:8000/worlds/{id}"),
        ("0.0.0.0", "http://127.0.0.1:8000/worlds/{id}"),
        ("::", "http://127.0.0.1:8000/worlds/{id}"),
        ("", "http://127.0.0.1:8000/worlds/{id}"),
        ("::1", "http://[::1]:8000/worlds/{id}"),
        ("localhost", "http://localhost:8000/worlds/{id}"),
    ],
)
def test_base_url_is_an_address_a_client_can_reach(host: str, url: str) -> None:
    assert server.base_url(host, 8000) == url
