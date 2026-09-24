"""The handler contract: `HttpRequest`, `HttpResponse` and `HttpHandler`.

Seahaven's own plain types rather than a server framework's, so a world declares
and tests its API, and its tools call the handler, without the `serve` extra
installed. Standard library only.
"""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from seahaven.ctx import Ctx

__all__ = ["Headers", "HttpHandler", "HttpRequest", "HttpResponse"]

type Headers = tuple[tuple[str, str], ...]

JSON_CONTENT_TYPE = "application/json"

# RFC 9110's `token`, which is what a header name is.
_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_BODILESS = frozenset({204, 304})
_CONTROL = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")


@dataclass(frozen=True)
class HttpRequest:
    """One request to a world's handler, as the server builds it or a tool writes it.

    `path` is percent-decoded and relative to the instance, so `/v1/customers`
    rather than `/worlds/acme/v1/customers`. `query` is the raw query string
    without the `?`. Header names are lower case and a name may repeat.
    """

    method: str
    path: str
    query: str = ""
    headers: Headers = ()
    body: bytes = b""

    def __post_init__(self) -> None:
        object.__setattr__(self, "method", self.method.upper())
        object.__setattr__(
            self, "headers", tuple((name.lower(), value) for name, value in self.headers)
        )

    def header(self, name: str) -> str | None:
        """The first value of the header `name`, matched case-insensitively, or `None`."""
        wanted = name.lower()
        return next((value for key, value in self.headers if key == wanted), None)

    def json(self) -> Any:
        """The body parsed as JSON. Raises `json.JSONDecodeError` when it is not JSON."""
        return json.loads(self.body)


@dataclass(frozen=True)
class HttpResponse:
    """What a world's handler answers. A `str` body is sent UTF-8 encoded.

    Checked at construction, so a mistake raises in the handler that made it,
    under a tool as readily as under the server.
    """

    status: int = 200
    headers: Headers = ()
    body: bytes | str = b""

    def __post_init__(self) -> None:
        _check_status(self.status)
        _check_headers(self.headers)
        if not isinstance(self.body, bytes | str):
            raise TypeError(f"an HttpResponse body is bytes or str, not {type(self.body).__name__}")
        # The HTTP server sends no body for these, and closes the connection on one.
        if self.status in _BODILESS and self.body:
            raise ValueError(f"an HttpResponse with status {self.status} has no body")

    @classmethod
    def json(cls, data: Any, *, status: int = 200, headers: Headers = ()) -> HttpResponse:
        """A JSON body, with `content-type: application/json` unless `headers` set one."""
        body = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        if not any(name.lower() == "content-type" for name, _ in headers):
            headers = (*headers, ("content-type", JSON_CONTENT_TYPE))
        return cls(status=status, headers=headers, body=body)

    @property
    def body_bytes(self) -> bytes:
        """The body as sent: a `str` body UTF-8 encoded."""
        return self.body.encode() if isinstance(self.body, str) else self.body


type HttpHandler = Callable[[Ctx[Any], HttpRequest], HttpResponse]


def _check_status(status: object) -> None:
    # `bool` is a subclass of `int`, and `True` is never a status anyone meant.
    if isinstance(status, bool) or not isinstance(status, int):
        raise TypeError(f"an HttpResponse status is an int, not {type(status).__name__}")
    # A 1xx is an interim response, which the server cannot send as the answer.
    if not 200 <= status <= 599:
        raise ValueError(f"an HttpResponse status is from 200 to 599, not {status}")


def _check_headers(headers: object) -> None:
    if not isinstance(headers, tuple):
        raise TypeError(
            f"HttpResponse headers are a tuple of (name, value) pairs, not {type(headers).__name__}"
        )
    for pair in headers:
        if not (
            isinstance(pair, tuple)
            and len(pair) == 2
            and isinstance(pair[0], str)
            and isinstance(pair[1], str)
        ):
            raise TypeError(f"an HttpResponse header is a (str, str) pair, not {pair!r}")
        name, value = pair
        if not _TOKEN.fullmatch(name):
            raise ValueError(
                f"an HttpResponse header name is letters, digits and !#$%&'*+-.^_`|~, not {name!r}"
            )
        # A CR or LF would end the header line early and let the rest be read as
        # another header, and the HTTP server refuses the other control
        # characters and whitespace at either end.
        if _CONTROL.search(value) or value != value.strip(" \t"):
            raise ValueError(
                "an HttpResponse header value has no control characters other than tab, "
                f"and no space or tab at either end: {pair!r}"
            )
        # HTTP/1.1 carries header bytes as latin-1.
        try:
            value.encode("latin-1")
        except UnicodeEncodeError:
            raise ValueError(
                f"an HttpResponse header must be encodable as latin-1, which {pair!r} is not"
            ) from None
