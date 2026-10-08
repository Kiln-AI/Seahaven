"""A world after `seahaven new` and `seahaven hub`, judged by OpenEnv's own code.

`test_cli_hub.py` asserts what `seahaven hub` writes. This module asserts what
upstream does with it: `openenv validate`, the structure check `openenv push`
runs, the `app:` field and Dockerfile `CMD` the container providers read, the
front matter a Hugging Face Space reads, and the consumer call the docs name,
`AutoEnv.from_hub(..., skip_install=True)`, against the scaffold's own app on a
real port. Each of these was once wrong in a scaffold whose file set was right.
"""

import json
import re
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from seahaven.cli.hub import add_hub_files
from seahaven.cli.new import render

pytest.importorskip(
    "seahaven.openenv", exc_type=ImportError, reason="the serve extra does not import here"
)

import yaml
from fastapi import FastAPI
from openenv import AutoEnv, GenericEnvClient
from openenv.cli._cli_utils import validate_env_structure
from openenv.core.containers.runtime._server_config import (
    parse_dockerfile_cmd,
    parse_openenv_app_field,
)
from openenv.validation import CheckStatus, load_policy, run_validation
from uvicorn.importer import import_from_string

from tests.serving import serving_app

pytestmark = pytest.mark.usefixtures("isolated_imports")

WORLD = "hubbed"

# The one finding `openenv validate` has on a scaffold, and the deviation
# `serving_and_openenv.md` documents: the block declares a reward range and a
# reward oracle, and a Seahaven world has no reward.
NO_VALIDATION_BLOCK = "openenv.yaml has no `validation:` block"


@pytest.fixture
def published(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A world with the hub files, importable the way `uv sync` makes it inside the image."""
    root = render(WORLD, tmp_path / WORLD)
    add_hub_files(root)
    monkeypatch.syspath_prepend(str(root / "src"))
    return root


@pytest.fixture
def manifest(published: Path) -> dict[str, Any]:
    return dict(yaml.safe_load((published / "openenv.yaml").read_text(encoding="utf-8")))


@pytest.fixture
def served_app(published: Path) -> FastAPI:
    """The app `openenv.yaml` names, read with upstream's reader and imported as uvicorn does."""
    reference = parse_openenv_app_field((published / "openenv.yaml").read_text(encoding="utf-8"))
    assert reference is not None, "openenv.yaml has no `app:` for the container providers"
    served = import_from_string(reference)
    assert isinstance(served, FastAPI)
    return served


@pytest.fixture
def url(served_app: FastAPI) -> Iterator[str]:
    with serving_app(served_app) as url:
        yield url


def get(url: str) -> bytes:
    """The body of a `GET`; `urlopen` raises on any status from 400 up."""
    with urllib.request.urlopen(url) as response:
        return bytes(response.read())


def test_openenv_validate_reports_only_the_missing_validation_block(published: Path) -> None:
    report = run_validation(published, policy=load_policy())
    assert [(result.check_id, result.status, result.evidence) for result in report.results] == [
        ("static.manifest", CheckStatus.FAIL, [NO_VALIDATION_BLOCK])
    ]


def test_the_scaffold_has_the_structure_openenv_push_requires(published: Path) -> None:
    """Raises on a missing required file; the one warning is for a directory of eval output."""
    assert validate_env_structure(published) == ["Recommended directory missing: outputs/"]


def test_the_app_in_openenv_yaml_is_this_worlds_server(url: str) -> None:
    assert json.loads(get(url + "/health"))["status"] == "healthy"
    assert json.loads(get(url + "/metadata"))["name"] == WORLD


def test_the_dockerfile_serves_the_same_app_on_the_same_port(
    published: Path, manifest: dict[str, Any]
) -> None:
    dockerfile = (published / "Dockerfile").read_text(encoding="utf-8")
    command = parse_dockerfile_cmd(dockerfile)
    assert command is not None
    assert manifest["app"] in command.split()
    assert f"--port {manifest['port']}" in command
    assert f"EXPOSE {manifest['port']}" in dockerfile


def test_the_healthcheck_asks_a_route_the_server_has(
    published: Path, manifest: dict[str, Any], url: str
) -> None:
    dockerfile = (published / "Dockerfile").read_text(encoding="utf-8")
    probe = re.search(r"^HEALTHCHECK .*?http://localhost:(\d+)(/[\w/]*)", dockerfile, re.M | re.S)
    assert probe is not None, "the Dockerfile has no HEALTHCHECK that asks the server"
    assert int(probe[1]) == manifest["port"]
    get(url + probe[2])


def instructions(published: Path) -> list[list[str]]:
    """Each top-level Dockerfile instruction as `[keyword, argument]`, in order."""
    return [
        line.split(maxsplit=1)
        for line in (published / "Dockerfile").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith((" ", "#"))
    ]


def workdir(published: Path) -> str:
    return next(argument for keyword, argument in instructions(published) if keyword == "WORKDIR")


def position(published: Path, keyword: str, argument_start: str) -> int:
    """The index of the one instruction with this keyword whose argument starts so."""
    [index] = [
        index
        for index, (instruction, argument) in enumerate(instructions(published))
        if instruction == keyword and argument.startswith(argument_start)
    ]
    return index


def test_the_worlds_interpreter_is_first_on_the_images_path(published: Path) -> None:
    """The providers run `python -m uvicorn <app>`; `uv sync` puts the venv in the workdir."""
    assert ["ENV", f'PATH="{workdir(published)}/.venv/bin:$PATH"'] in instructions(published)


def test_the_sync_runs_with_a_uv_that_can_install_cpython_3_14(published: Path) -> None:
    """The base image's uv 0.5.27 knows no final 3.14; uv 0.9.0 was the first that did."""
    copy = position(published, "COPY", "--from=ghcr.io/astral-sh/uv:")
    pinned = re.fullmatch(
        r"--from=ghcr\.io/astral-sh/uv:(\d+)\.(\d+)\.(\d+) /uv /uvx /usr/local/bin/",
        instructions(published)[copy][1],
    )
    assert pinned is not None, "uv is not copied in at an exact version"
    assert tuple(int(part) for part in pinned.groups()) >= (0, 9, 0)
    assert copy < position(published, "RUN", "uv sync")


def test_the_sync_installs_cpython_where_a_non_root_user_can_read_it(published: Path) -> None:
    """A Space runs the image as uid 1000, and uv's default directory is under /root."""
    setting = position(published, "ENV", "UV_PYTHON_INSTALL_DIR=")
    directory = instructions(published)[setting][1].removeprefix("UV_PYTHON_INSTALL_DIR=")
    assert directory.startswith("/") and not Path(directory).is_relative_to("/root")
    assert setting < position(published, "RUN", "uv sync")


def test_the_server_starts_without_uv(published: Path) -> None:
    """`uv run` as uid 1000 cannot write a cache or sync the root-owned venv."""
    command = parse_dockerfile_cmd((published / "Dockerfile").read_text(encoding="utf-8"))
    assert command is not None
    assert command.split()[:3] == ["python", "-m", "uvicorn"]


def test_the_healthcheck_runs_the_worlds_own_interpreter(published: Path) -> None:
    """A bare `python` falls through to the base image's, and passes on a broken image."""
    dockerfile = (published / "Dockerfile").read_text(encoding="utf-8")
    probe = re.search(r"^HEALTHCHECK .*? CMD[\s\\]+(\S+) -c ", dockerfile, re.M | re.S)
    assert probe is not None, "the HEALTHCHECK does not run a Python interpreter"
    assert probe[1] == f"{workdir(published)}/.venv/bin/python"


def test_the_space_card_points_hugging_face_at_the_server(
    published: Path, manifest: dict[str, Any], url: str
) -> None:
    readme = (published / "README.md").read_text(encoding="utf-8")
    assert readme.startswith("---\n")
    card = yaml.safe_load(readme.split("---\n", 2)[1])
    assert card["sdk"] == "docker"
    assert card["app_port"] == manifest["port"]
    assert b"<html" in get(url + card["base_path"])


def test_a_world_without_hub_has_no_space_card(tmp_path: Path) -> None:
    readme = (render(WORLD, tmp_path / WORLD) / "README.md").read_text(encoding="utf-8")
    assert readme.startswith(f"# {WORLD}\n")
    assert "from_hub" not in readme


def test_from_hub_with_skip_install_drives_the_published_world(url: str) -> None:
    """The consumer call the docs and the scaffold README name, with no Seahaven client side."""
    env = AutoEnv.from_hub(f"someone/{WORLD}", base_url=url, skip_install=True)
    assert isinstance(env, GenericEnvClient)
    with env:
        reset = env.reset(seed=7)
        assert reset.observation["metadata"]["tools"] == 2
        listed = env.step({"type": "list_tools"}).observation
        assert [tool["name"] for tool in listed["tools"]] == ["create_item", "get_item"]
        created = env.step(
            {"type": "call_tool", "tool_name": "create_item", "arguments": {"name": "First"}}
        ).observation
        assert created["error"] is None
        fetched = env.step(
            {
                "type": "call_tool",
                "tool_name": "get_item",
                "arguments": {"item_id": created["result"]["id"]},
            }
        ).observation
        assert fetched["result"] == created["result"]
        state = env.state()
        assert state["world"]["name"] == WORLD
        assert state["step_count"] == 3
