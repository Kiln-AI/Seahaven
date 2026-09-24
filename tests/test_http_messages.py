"""`HttpRequest` and `HttpResponse`: the normalisation, the helpers and every refusal."""

import dataclasses
import json
from typing import Any

import pytest

from seahaven.http import HttpRequest, HttpResponse


def test_a_request_upper_cases_the_method_and_lower_cases_header_names() -> None:
    request = HttpRequest(
        "post", "/v1/x", headers=(("X-Trace", "1"), ("Accept", "a"), ("x-trace", "2"))
    )
    assert request.method == "POST"
    assert request.headers == (("x-trace", "1"), ("accept", "a"), ("x-trace", "2"))


def test_a_request_keeps_header_values_and_the_rest_as_given() -> None:
    request = HttpRequest("GET", "/a b", query="q=1&q=2", headers=(("A", "Mixed Case"),), body=b"x")
    assert (request.path, request.query, request.body) == ("/a b", "q=1&q=2", b"x")
    assert request.headers == (("a", "Mixed Case"),)


def test_a_list_of_headers_becomes_a_tuple() -> None:
    request = HttpRequest("GET", "/", headers=[("A", "1")])  # ty: ignore[invalid-argument-type]
    assert request.headers == (("a", "1"),)


def test_header_is_case_insensitive_and_answers_the_first_value() -> None:
    request = HttpRequest("GET", "/", headers=(("Set-Thing", "first"), ("set-thing", "second")))
    assert request.header("SET-THING") == "first"
    assert request.header("missing") is None


def test_request_json_parses_the_body() -> None:
    assert HttpRequest("POST", "/", body=b'{"a": [1, "\xc3\xa9"]}').json() == {"a": [1, "é"]}


def test_request_json_raises_on_a_body_that_is_not_json() -> None:
    with pytest.raises(json.JSONDecodeError):
        HttpRequest("POST", "/", body=b"not json").json()


def test_response_json_sets_the_content_type_and_a_compact_body() -> None:
    response = HttpResponse.json({"a": [1, 2]}, status=201, headers=(("x-id", "7"),))
    assert response.status == 201
    assert response.headers == (("x-id", "7"), ("content-type", "application/json"))
    assert response.body == '{"a":[1,2]}'


def test_response_json_keeps_a_content_type_given_in_any_case() -> None:
    response = HttpResponse.json([], headers=(("Content-Type", "application/problem+json"),))
    assert response.headers == (("Content-Type", "application/problem+json"),)


def test_response_json_encodes_non_ascii_as_utf8() -> None:
    response = HttpResponse.json({"name": "Zoë"})
    assert response.body_bytes == '{"name":"Zoë"}'.encode()
    assert b"\\u" not in response.body_bytes


def test_body_bytes_encodes_a_str_body_and_passes_bytes_through() -> None:
    assert HttpResponse(body="é").body_bytes == b"\xc3\xa9"
    assert HttpResponse(body=b"\xff").body_bytes == b"\xff"


def test_the_defaults_are_an_empty_200() -> None:
    assert HttpResponse() == HttpResponse(200, (), b"")


@pytest.mark.parametrize(
    ("fields", "error", "words"),
    [
        ({"status": 99}, ValueError, "from 100 to 599, not 99"),
        ({"status": 600}, ValueError, "not 600"),
        ({"status": True}, TypeError, "an int, not bool"),
        ({"status": 200.0}, TypeError, "an int, not float"),
        ({"headers": [("a", "b")]}, TypeError, "not list"),
        ({"headers": (("a", "b", "c"),)}, TypeError, "a (str, str) pair"),
        ({"headers": (("a", 1),)}, TypeError, "a (str, str) pair"),
        ({"headers": ((b"a", "b"),)}, TypeError, "a (str, str) pair"),
        ({"headers": (("a\r\nb", "c"),)}, ValueError, "CR or LF"),
        ({"headers": (("a", "b\nx-injected: 1"),)}, ValueError, "CR or LF"),
        ({"headers": (("a", "Zoë ☃"),)}, ValueError, "latin-1"),
        ({"body": {"a": 1}}, TypeError, "bytes or str, not dict"),
        ({"body": None}, TypeError, "not NoneType"),
    ],
)
def test_a_response_is_checked_at_construction(
    fields: dict[str, Any], error: type[Exception], words: str
) -> None:
    with pytest.raises(error) as raised:
        HttpResponse(**fields)
    assert words in str(raised.value)


def test_a_latin1_header_value_is_accepted() -> None:
    assert HttpResponse(headers=(("x-name", "Zoë"),)).headers == (("x-name", "Zoë"),)


def test_both_types_are_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        HttpRequest("GET", "/").path = "/other"  # ty: ignore[invalid-assignment]
    with pytest.raises(dataclasses.FrozenInstanceError):
        HttpResponse().status = 404  # ty: ignore[invalid-assignment]
