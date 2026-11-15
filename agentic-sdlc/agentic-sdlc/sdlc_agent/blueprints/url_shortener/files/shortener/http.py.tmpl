"""HTTP adapter (WSGI, standard library only).

Implements the OpenAPI contract in openapi.yaml. `ROUTES` is the single source
of truth for routing and is checked against the contract by the test suite.
Errors are returned as RFC 9457 application/problem+json.
"""
from __future__ import annotations

# json: request parsing and response encoding.
import json
# logging: record unexpected errors server-side (clients get a generic 500).
import logging
# re: compile route templates into matching patterns.
import re
# parse_qs: read query-string parameters such as ?days=7.
from urllib.parse import parse_qs
from dataclasses import dataclass
# cached_property: compile each route's pattern once, on first use.
from functools import cached_property
from typing import Any, Callable, Iterable

from .config import Settings
from .domain import Link
# Errors this layer raises itself; domain errors come up from the service.
from .errors import (BadRequest, PayloadTooLarge, RateLimited, ShortenerError, Unauthorized)
from .ratelimit import TokenBucketLimiter
from .service import LinkService

# Module logger.
log = logging.getLogger(__name__)

# WSGI type aliases: the request environment dict and the start_response callable.
Environ = dict[str, Any]
StartResponse = Callable[..., Any]


# One API operation: HTTP method + path template -> handler method on ShortenerApp.
@dataclass(frozen=True)
class Route:
    method: str
    template: str            # OpenAPI-style path template
    # Name of the ShortenerApp method that handles the request.
    handler: str
    # Must match operationId in openapi.yaml (checked by the contract test).
    operation_id: str
    # True: the route calls authenticate() before the handler.
    auth: bool = False

    # Turn "/api/v1/links/{code}" into a regex with a named group per path parameter.
    @cached_property
    def regex(self) -> re.Pattern[str]:
        # Path parameters accept only code/alias characters, at most 32 long.
        pattern = re.sub(r"\{(\w+)\}", r"(?P<\1>[A-Za-z0-9_-]{1,32})", self.template)
        return re.compile(f"^{pattern}$")


# Routing table, matched in order. The catch-all redirect route must stay last so
# it doesn't swallow /healthz or /api/... paths.
ROUTES: tuple[Route, ...] = (
    Route("GET", "/healthz", "health", "health"),
    Route("POST", "/api/v1/links", "create_link", "createLink", auth=True),
    Route("GET", "/api/v1/links/{code}", "get_link", "getLink"),
    Route("DELETE", "/api/v1/links/{code}", "delete_link", "deleteLink", auth=True),
    Route("GET", "/api/v1/links/{code}/stats", "get_stats", "getLinkStats"),
    Route("GET", "/{code}", "redirect", "redirect"),
)

# Added to every response: no MIME sniffing, and no caching of API answers or redirects.
_SECURITY_HEADERS = [("X-Content-Type-Options", "nosniff"), ("Cache-Control", "no-store")]


# What handlers return; __call__ serialises it into a WSGI response.
class Response:
    def __init__(self, status: int, body: Any = None, headers: list[tuple[str, str]] | None = None,
                 content_type: str = "application/json") -> None:
        # HTTP status code.
        self.status = status
        # JSON-serialisable body, a text body, or None for no body.
        self.body = body
        # Extra headers specific to this response (Location, Retry-After, ...).
        self.headers = headers or []
        self.content_type = content_type


# Reason phrases for the status line ("404 Not Found").
_REASONS = {200: "OK", 201: "Created", 204: "No Content", 302: "Found", 400: "Bad Request",
            401: "Unauthorized", 404: "Not Found", 405: "Method Not Allowed", 409: "Conflict",
            410: "Gone", 413: "Payload Too Large", 422: "Unprocessable Entity",
            429: "Too Many Requests", 500: "Internal Server Error", 503: "Service Unavailable"}


# The WSGI application: routing, authentication, request parsing and error mapping.
# Business rules live in LinkService; this class only translates HTTP to service calls.
class ShortenerApp:
    def __init__(self, service: LinkService, settings: Settings,
                 limiter: TokenBucketLimiter | None = None) -> None:
        self.service = service
        self.settings = settings
        # Rate limiter for link creation (injectable for tests).
        self.limiter = limiter or TokenBucketLimiter(settings.create_rate_per_min)
        self.routes = ROUTES

    # ------------------------------------------------------------------ WSGI
    # WSGI entry point: run the request and always return a well-formed response.
    def __call__(self, environ: Environ, start_response: StartResponse) -> Iterable[bytes]:
        try:
            resp = self.dispatch(environ)
        # Expected errors become problem+json with their own status.
        except ShortenerError as exc:
            resp = self.problem(exc)
        except Exception:  # noqa: BLE001 - last-resort guard, never leak internals
            log.exception("unhandled error")
            resp = self.problem(ShortenerError())
        # Encode the body: JSON types are serialised, anything else is sent as text.
        if resp.content_type == "application/json" or resp.content_type.endswith("+json"):
            payload = b"" if resp.body is None else json.dumps(resp.body).encode()
        else:
            payload = (resp.body or "").encode()
        # Standard headers first, then security headers, then response-specific ones.
        headers = [("Content-Type", resp.content_type), ("Content-Length", str(len(payload))),
                   *_SECURITY_HEADERS, *resp.headers]
        start_response(f"{resp.status} {_REASONS.get(resp.status, '')}", headers)
        return [payload]

    # Find the route for this method and path and call its handler.
    def dispatch(self, environ: Environ) -> Response:
        method = environ.get("REQUEST_METHOD", "GET").upper()
        path = environ.get("PATH_INFO", "/") or "/"
        # Methods registered for this path (used to build a 405 response).
        allowed: list[str] = []
        for route in self.routes:
            match = route.regex.match(path)
            if not match:
                continue
            # Right path, wrong method: remember it and keep looking.
            if route.method != method:
                allowed.append(route.method)
                continue
            # Protected routes resolve the caller first (may raise Unauthorized).
            principal = self.authenticate(environ) if route.auth else None
            # Path parameters (e.g. code) are passed as keyword arguments.
            return getattr(self, route.handler)(environ, principal=principal, **match.groupdict())
        # The path exists but not for this method: 405 with an Allow header.
        if allowed:
            return Response(405, {"error": "method_not_allowed"},
                            [("Allow", ", ".join(sorted(set(allowed))))])
        # No route matched at all.
        return self.problem(_NotFoundRoute())

    # -------------------------------------------------------------- handlers
    # GET /healthz: liveness plus analytics queue health.
    def health(self, environ: Environ, principal: str | None) -> Response:
        return Response(200, {"status": "ok", "analytics_backlog": self.service.recorder.backlog,
                              "analytics_dropped": self.service.recorder.dropped})

    # POST /api/v1/links: create a short link.
    def create_link(self, environ: Environ, principal: str | None) -> Response:
        # Rate-limit per API key when there is one, otherwise per client IP.
        client = principal or environ.get("REMOTE_ADDR", "anonymous")
        allowed, retry_after = self.limiter.acquire(client)
        if not allowed:
            raise RateLimited(retry_after)
        body = self.read_json(environ)
        # Strict input: reject unknown fields instead of silently ignoring them.
        unknown = set(body) - {"url", "alias", "ttl_seconds"}
        if unknown:
            raise BadRequest(f"unknown field(s): {', '.join(sorted(unknown))}")
        # The service validates the values and stores the link.
        link = self.service.create(body.get("url"), body.get("alias"), body.get("ttl_seconds"),
                                   owner=principal)
        # 201 Created, with a Location header pointing at the new resource.
        return Response(201, self.link_json(link),
                        [("Location", f"/api/v1/links/{link.code}")])

    # GET /api/v1/links/{code}: link metadata.
    def get_link(self, environ: Environ, principal: str | None, code: str) -> Response:
        return Response(200, self.link_json(self.service.get(code)))

    # DELETE /api/v1/links/{code}: soft-delete (the service checks ownership).
    def delete_link(self, environ: Environ, principal: str | None, code: str) -> Response:
        self.service.disable(code, principal)
        return Response(204)

    # GET /api/v1/links/{code}/stats?days=N: click analytics for the last N days.
    def get_stats(self, environ: Environ, principal: str | None, code: str) -> Response:
        days = self.query_int(environ, "days", default=30)
        return Response(200, self.service.stats(code, days))

    # GET /{code}: the hot path. Resolve, record the click asynchronously, redirect.
    def redirect(self, environ: Environ, principal: str | None, code: str) -> Response:
        # Raises LinkNotFound (404) or LinkExpired (410) when the link can't be used.
        link = self.service.resolve(code)
        # Enqueue only; never blocks the redirect.
        self.service.record_click(link, environ.get("HTTP_REFERER"),
                                  environ.get("HTTP_USER_AGENT"), environ.get("REMOTE_ADDR"))
        # 302 (not 301): browsers must come back so every click is counted and
        # disabled/expired links take effect immediately.
        return Response(302, None, [("Location", link.target_url),
                                    ("Referrer-Policy", "no-referrer-when-downgrade")],
                        content_type="text/plain")

    # --------------------------------------------------------------- helpers
    # Identify the caller from the X-API-Key header.
    def authenticate(self, environ: Environ) -> str | None:
        key = environ.get("HTTP_X_API_KEY", "")
        # Keys optional: use the key as the owner if one was sent, else anonymous.
        if not self.settings.require_api_key:
            return key or None
        # Keys required: missing or unknown keys are rejected.
        if not key or key not in self.settings.api_keys:
            raise Unauthorized()
        return key

    # Parse and sanity-check a JSON request body.
    def read_json(self, environ: Environ) -> dict[str, Any]:
        ctype = environ.get("CONTENT_TYPE", "")
        if not ctype.startswith("application/json"):
            raise BadRequest("Content-Type must be application/json")
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            raise BadRequest("invalid Content-Length") from None
        # Check the declared size before reading anything.
        if length > self.settings.max_body_bytes:
            raise PayloadTooLarge(f"body exceeds {self.settings.max_body_bytes} bytes")
        # Read exactly the declared length (WSGI input may not signal end-of-stream).
        raw = environ["wsgi.input"].read(length) if length else b""
        try:
            # An empty body is treated as an empty object.
            body = json.loads(raw or b"{}")
        except (ValueError, UnicodeDecodeError):
            raise BadRequest("body is not valid JSON") from None
        # Arrays, strings and numbers are valid JSON but not a valid request.
        if not isinstance(body, dict):
            raise BadRequest("body must be a JSON object")
        return body

    # Read an integer query parameter, with a default when it's absent.
    @staticmethod
    def query_int(environ: Environ, name: str, default: int) -> int:
        values = parse_qs(environ.get("QUERY_STRING", "")).get(name)
        if not values:
            return default
        try:
            # Repeated parameters: the first value wins.
            return int(values[0])
        except ValueError:
            raise BadRequest(f"query parameter '{name}' must be an integer") from None

    # Public JSON representation of a link (the Link schema in openapi.yaml).
    def link_json(self, link: Link) -> dict[str, Any]:
        return {"code": link.code, "short_url": f"{self.settings.base_url.rstrip('/')}/{link.code}",
                "target_url": link.target_url, "created_at": link.created_at,
                "expires_at": link.expires_at, "is_custom": link.is_custom,
                "disabled": link.disabled}

    # Convert an error into an RFC 9457 problem+json response.
    @staticmethod
    def problem(exc: ShortenerError) -> Response:
        headers = []
        # Rate-limited clients are told when to retry.
        if isinstance(exc, RateLimited):
            headers.append(("Retry-After", str(exc.retry_after_s)))
        return Response(exc.status, {"type": f"https://errors.shortener.example/{exc.code}",
                                     "title": exc.title, "status": exc.status,
                                     "detail": exc.detail},
                        headers, content_type="application/problem+json")


# 404 for paths that match no route (distinct title from "Link not found").
class _NotFoundRoute(ShortenerError):
    status, code, title = 404, "not_found", "No such route"


# Factory used by main.py and the tests.
def create_app(service: LinkService, settings: Settings,
               limiter: TokenBucketLimiter | None = None) -> ShortenerApp:
    return ShortenerApp(service, settings, limiter)
