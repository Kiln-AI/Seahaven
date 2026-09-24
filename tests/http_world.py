"""`notes_api`: a small world written as an HTTP handler, with two tools over it.

What `seahaven.http` is tested against. A module rather than a package under
`tests/worlds/`, because the tests need a `World` object and a handler, not
discovery. Every route is here for a test: `/explode` and `/wrong` fail on
purpose, after a write, so that the rollback is observable.
"""

import json
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs

import seahaven
from seahaven.http import HttpRequest, HttpResponse

SCHEMA = """
CREATE TABLE notes (
    id TEXT PRIMARY KEY,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL
) STRICT;
"""


class NoteError(seahaven.ToolError):
    """The API answered a tool with a status that is not 2xx."""

    def __init__(self, response: HttpResponse) -> None:
        error = json.loads(response.body_bytes)["error"]
        super().__init__(error["type"], f"the notes API answered {response.status}", error)


def build(fixtures_dir: Path | None = None, work_dir: Path | None = None) -> seahaven.World:
    """A fresh `notes_api` world. Each call is a new object with its own registry."""
    world = seahaven.World(
        "notes_api",
        "1.0.0",
        SCHEMA,
        state_format="seahaven.state/1",
        fixtures_dir=fixtures_dir,
        work_dir=work_dir,
    )

    @world.instance_startup
    def explode_on_request(ctx: seahaven.Ctx, *, explode: bool = False) -> None:
        """Fail instance creation when asked to."""
        if explode:
            raise RuntimeError("the startup hook was asked to explode")

    @world.tool
    def get_note(ctx: seahaven.Ctx, note_id: str) -> dict[str, Any]:
        """Fetch one note by id."""
        return _result(handle(ctx, HttpRequest("GET", f"/notes/{note_id}")))

    @world.tool
    def create_note(ctx: seahaven.Ctx, body: str, reject: bool = False) -> dict[str, Any]:
        """Create a note. `reject` makes the API write the note and then answer 400."""
        request = HttpRequest(
            "POST",
            "/notes",
            query="reject=1" if reject else "",
            headers=(("Content-Type", "application/json"),),
            body=json.dumps({"body": body}, ensure_ascii=False).encode(),
        )
        return _result(handle(ctx, request))

    return world


def handle(ctx: seahaven.Ctx, request: HttpRequest) -> HttpResponse:
    """The notes API. The routes are listed in `architecture.md` §8.1 of the REST APIs project."""
    match request.method, request.path.split("/")[1:]:
        case "POST", ["notes"]:
            note = _insert(ctx, request.json()["body"])
            if parse_qs(request.query).get("reject") == ["1"]:
                return _error(400, "rejected")
            return HttpResponse.json(note, status=201)
        case "GET", ["notes", note_id]:
            row = ctx.db.one("SELECT * FROM notes WHERE id = ?", note_id)
            return _error(404, "not_found") if row is None else HttpResponse.json(row)
        case "GET", ["notes"]:
            return HttpResponse.json(ctx.db.rows("SELECT * FROM notes ORDER BY id"))
        case "POST", ["explode"]:
            _insert(ctx, "written before the handler raised")
            raise RuntimeError("the handler was asked to explode")
        case "GET", ["wrong"]:
            _insert(ctx, "written before the handler returned a dict")
            return cast(HttpResponse, {"not": "a response"})
        case _, ["echo", *_]:
            return HttpResponse.json(
                {
                    "method": request.method,
                    "path": request.path,
                    "query": request.query,
                    "headers": [list(pair) for pair in request.headers],
                    "body": request.body.decode(),
                },
                headers=(
                    ("set-cookie", "a=1"),
                    ("set-cookie", "b=2"),
                    ("content-length", "9999"),
                ),
            )
        case "GET", ["context"]:
            return HttpResponse.json({"call_is_none": ctx.call is None, "now": ctx.clock.iso()})
        case _:
            return _error(404, "no_route")


def _insert(ctx: seahaven.Ctx, body: str) -> dict[str, Any]:
    note = {"id": ctx.ids.uuid(), "body": body, "created_at": ctx.clock.iso()}
    ctx.db.execute(
        "INSERT INTO notes (id, body, created_at) VALUES (?, ?, ?)",
        note["id"],
        note["body"],
        note["created_at"],
    )
    return note


def _error(status: int, kind: str) -> HttpResponse:
    return HttpResponse.json({"error": {"type": kind}}, status=status)


def _result(response: HttpResponse) -> dict[str, Any]:
    if not 200 <= response.status < 300:
        raise NoteError(response)
    return json.loads(response.body_bytes)
