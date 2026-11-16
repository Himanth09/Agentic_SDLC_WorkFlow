"""Test helpers: fake clock and an in-process WSGI client."""
from __future__ import annotations

# io.BytesIO: in-memory request body stream (wsgi.input).
import io
import json
from typing import Any
# Fills in the WSGI environ keys a real server would provide.
from wsgiref.util import setup_testing_defaults

# The one API key the test settings accept.
API_KEY = "example-test-key"


# A clock tests move forward explicitly, so expiry, cache TTLs and rate limits need no sleeping.
class FakeClock:
    # Start at a fixed timestamp (November 2023) so dates in assertions are predictable.
    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start

    # Called like time.time().
    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# A captured HTTP response: integer status, lower-cased headers, raw body.
class Result:
    def __init__(self, status: str, headers: list[tuple[str, str]], body: bytes) -> None:
        # "302 Found" -> 302.
        self.status = int(status.split()[0])
        # Lower-case names make header lookups case-insensitive.
        self.headers = {k.lower(): v for k, v in headers}
        self.body = body

    def json(self) -> Any:
        return json.loads(self.body)


# Send one request straight into the WSGI app, with no network or server involved.
def call(app, method: str, path: str, body: Any = None, headers: dict[str, str] | None = None,
         query: str = "", remote_addr: str = "203.0.113.7") -> Result:
    environ: dict[str, Any] = {}
    setup_testing_defaults(environ)
    # Bytes are sent as-is (to test malformed JSON); anything else is JSON-encoded.
    raw = b"" if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    # remote_addr defaults to a documentation-range IP (203.0.113.0/24).
    environ.update({"REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": query,
                    "CONTENT_LENGTH": str(len(raw)), "wsgi.input": io.BytesIO(raw),
                    "REMOTE_ADDR": remote_addr})
    if body is not None:
        environ["CONTENT_TYPE"] = "application/json"
    # WSGI convention: "User-Agent" becomes HTTP_USER_AGENT (Content-Type has no HTTP_ prefix).
    for k, v in (headers or {}).items():
        name = k.upper().replace("-", "_")
        environ[name if name in ("CONTENT_TYPE",) else f"HTTP_{name}"] = v
    captured: dict[str, Any] = {}

    # Record the status line and headers the app sends.
    def start_response(status: str, hdrs: list[tuple[str, str]]) -> None:
        captured["status"], captured["headers"] = status, hdrs

    # Run the app and join the body chunks.
    payload = b"".join(app(environ, start_response))
    return Result(captured["status"], captured["headers"], payload)
