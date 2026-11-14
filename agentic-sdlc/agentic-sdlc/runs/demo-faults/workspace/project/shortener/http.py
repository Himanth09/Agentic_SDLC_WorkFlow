"""HTTP adapter (WSGI, standard library only).

Implements the OpenAPI contract in openapi.yaml. `ROUTES` is the single source
of truth for routing and is checked against the contract by the test suite.
Errors are returned as RFC 9457 application/problem+json.
"""
from __future__ import annotations

import json
import logging
import re
from urllib.parse import parse_qs
from dataclasses import dataclass
from functools import cached_property
from typing import Any, Callable, Iterable

from .config import Settings
from .domain import Link
from .errors import (BadRequest, PayloadTooLarge, RateLimited, ShortenerError, Unauthorized)
from .ratelimit import TokenBucketLimiter
from .service import LinkService

log = logging.getLogger(__name__)

Environ = dict[str, Any]
StartResponse = Callable[..., Any]


@dataclass(frozen=True)
class Route:
    method: str
    template: str            # OpenAPI-style path template
    handler: str
    operation_id: str
    auth: bool = False

    @cached_property
    def regex(self) -> re.Pattern[str]:
        pattern = re.sub(r"\{(\w+)\}", r"(?P<\1>[A-Za-z0-9_-]{1,32})", self.template)
        return re.compile(f"^{pattern}$")


ROUTES: tuple[Route, ...] = (
    Route("GET", "/healthz", "health", "health"),
    Route("POST", "/api/v1/links", "create_link", "createLink", auth=True),
    Route("GET", "/api/v1/links/{code}", "get_link", "getLink"),
    Route("DELETE", "/api/v1/links/{code}", "delete_link", "deleteLink", auth=True),
    Route("GET", "/api/v1/links/{code}/stats", "get_stats", "getLinkStats"),
    Route("GET", "/{code}", "redirect", "redirect"),
)

_SECURITY_HEADERS = [("X-Content-Type-Options", "nosniff"), ("Cache-Control", "no-store")]


class Response:
    def __init__(self, status: int, body: Any = None, headers: list[tuple[str, str]] | None = None,
                 content_type: str = "application/json") -> None:
        self.status = status
        self.body = body
        self.headers = headers or []
        self.content_type = content_type


_REASONS = {200: "OK", 201: "Created", 204: "No Content", 302: "Found", 400: "Bad Request",
            401: "Unauthorized", 404: "Not Found", 405: "Method Not Allowed", 409: "Conflict",
            410: "Gone", 413: "Payload Too Large", 422: "Unprocessable Entity",
            429: "Too Many Requests", 500: "Internal Server Error", 503: "Service Unavailable"}


class ShortenerApp:
    def __init__(self, service: LinkService, settings: Settings,
                 limiter: TokenBucketLimiter | None = None) -> None:
        self.service = service
        self.settings = settings
        self.limiter = limiter or TokenBucketLimiter(settings.create_rate_per_min)
        self.routes = ROUTES

    # ------------------------------------------------------------------ WSGI
    def __call__(self, environ: Environ, start_response: StartResponse) -> Iterable[bytes]:
        try:
            resp = self.dispatch(environ)
        except ShortenerError as exc:
            resp = self.problem(exc)
        except Exception:  # noqa: BLE001 - last-resort guard, never leak internals
            log.exception("unhandled error")
            resp = self.problem(ShortenerError())
        if resp.content_type == "application/json" or resp.content_type.endswith("+json"):
            payload = b"" if resp.body is None else json.dumps(resp.body).encode()
        else:
            payload = (resp.body or "").encode()
        headers = [("Content-Type", resp.content_type), ("Content-Length", str(len(payload))),
                   *_SECURITY_HEADERS, *resp.headers]
        start_response(f"{resp.status} {_REASONS.get(resp.status, '')}", headers)
        return [payload]

    def dispatch(self, environ: Environ) -> Response:
        method = environ.get("REQUEST_METHOD", "GET").upper()
        path = environ.get("PATH_INFO", "/") or "/"
        allowed: list[str] = []
        for route in self.routes:
            match = route.regex.match(path)
            if not match:
                continue
            if route.method != method:
                allowed.append(route.method)
                continue
            principal = self.authenticate(environ) if route.auth else None
            return getattr(self, route.handler)(environ, principal=principal, **match.groupdict())
        if allowed:
            return Response(405, {"error": "method_not_allowed"},
                            [("Allow", ", ".join(sorted(set(allowed))))])
        return self.problem(_NotFoundRoute())

    # -------------------------------------------------------------- handlers
    def health(self, environ: Environ, principal: str | None) -> Response:
        return Response(200, {"status": "ok", "analytics_backlog": self.service.recorder.backlog,
                              "analytics_dropped": self.service.recorder.dropped})

    def create_link(self, environ: Environ, principal: str | None) -> Response:
        client = principal or environ.get("REMOTE_ADDR", "anonymous")
        allowed, retry_after = self.limiter.acquire(client)
        if not allowed:
            raise RateLimited(retry_after)
        body = self.read_json(environ)
        unknown = set(body) - {"url", "alias", "ttl_seconds"}
        if unknown:
            raise BadRequest(f"unknown field(s): {', '.join(sorted(unknown))}")
        link = self.service.create(body.get("url"), body.get("alias"), body.get("ttl_seconds"),
                                   owner=principal)
        return Response(201, self.link_json(link),
                        [("Location", f"/api/v1/links/{link.code}")])

    def get_link(self, environ: Environ, principal: str | None, code: str) -> Response:
        return Response(200, self.link_json(self.service.get(code)))

    def delete_link(self, environ: Environ, principal: str | None, code: str) -> Response:
        self.service.disable(code, principal)
        return Response(204)

    def get_stats(self, environ: Environ, principal: str | None, code: str) -> Response:
        days = self.query_int(environ, "days", default=30)
        return Response(200, self.service.stats(code, days))

    def redirect(self, environ: Environ, principal: str | None, code: str) -> Response:
        link = self.service.resolve(code)
        self.service.record_click(link, environ.get("HTTP_REFERER"),
                                  environ.get("HTTP_USER_AGENT"), environ.get("REMOTE_ADDR"))
        # 302 (not 301): browsers must come back so every click is counted and
        # disabled/expired links take effect immediately.
        return Response(302, None, [("Location", link.target_url),
                                    ("Referrer-Policy", "no-referrer-when-downgrade")],
                        content_type="text/plain")

    # --------------------------------------------------------------- helpers
    def authenticate(self, environ: Environ) -> str | None:
        key = environ.get("HTTP_X_API_KEY", "")
        if not self.settings.require_api_key:
            return key or None
        if not key or key not in self.settings.api_keys:
            raise Unauthorized()
        return key

    def read_json(self, environ: Environ) -> dict[str, Any]:
        ctype = environ.get("CONTENT_TYPE", "")
        if not ctype.startswith("application/json"):
            raise BadRequest("Content-Type must be application/json")
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            raise BadRequest("invalid Content-Length") from None
        if length > self.settings.max_body_bytes:
            raise PayloadTooLarge(f"body exceeds {self.settings.max_body_bytes} bytes")
        raw = environ["wsgi.input"].read(length) if length else b""
        try:
            body = json.loads(raw or b"{}")
        except (ValueError, UnicodeDecodeError):
            raise BadRequest("body is not valid JSON") from None
        if not isinstance(body, dict):
            raise BadRequest("body must be a JSON object")
        return body

    @staticmethod
    def query_int(environ: Environ, name: str, default: int) -> int:
        values = parse_qs(environ.get("QUERY_STRING", "")).get(name)
        if not values:
            return default
        try:
            return int(values[0])
        except ValueError:
            raise BadRequest(f"query parameter '{name}' must be an integer") from None

    def link_json(self, link: Link) -> dict[str, Any]:
        return {"code": link.code, "short_url": f"{self.settings.base_url.rstrip('/')}/{link.code}",
                "target_url": link.target_url, "created_at": link.created_at,
                "expires_at": link.expires_at, "is_custom": link.is_custom,
                "disabled": link.disabled}

    @staticmethod
    def problem(exc: ShortenerError) -> Response:
        headers = []
        if isinstance(exc, RateLimited):
            headers.append(("Retry-After", str(exc.retry_after_s)))
        return Response(exc.status, {"type": f"https://errors.shortener.example/{exc.code}",
                                     "title": exc.title, "status": exc.status,
                                     "detail": exc.detail},
                        headers, content_type="application/problem+json")


class _NotFoundRoute(ShortenerError):
    status, code, title = 404, "not_found", "No such route"


def create_app(service: LinkService, settings: Settings,
               limiter: TokenBucketLimiter | None = None) -> ShortenerApp:
    return ShortenerApp(service, settings, limiter)
