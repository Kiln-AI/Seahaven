"""Serve a world's HTTP handler, one instance per client-chosen ID.

A world writes its API as one function, `handler(ctx, request) -> response`, over
the types below, and its tools call that function. The types need no extra.
"""

from seahaven.http.messages import HttpHandler, HttpRequest, HttpResponse
from seahaven.http.runtime import DEFAULT_MAX_INSTANCES

__all__ = ["DEFAULT_MAX_INSTANCES", "HttpHandler", "HttpRequest", "HttpResponse"]
