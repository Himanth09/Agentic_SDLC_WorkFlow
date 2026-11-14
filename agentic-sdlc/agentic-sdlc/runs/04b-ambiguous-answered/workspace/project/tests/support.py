"""Test helpers: fake clock and an in-process WSGI client."""
from __future__ import annotations

import io
import json
from typing import Any
from wsgiref.util import setup_testing_defaults

API_KEY = "example-test-key"


class FakeClock:
    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Result:
    def __init__(self, status: str, headers: list[tuple[str, str]], body: bytes) -> None:
        self.status = int(status.split()[0])
        self.headers = {k.lower(): v for k, v in headers}
        self.body = body

    def json(self) -> Any:
        return json.loads(self.body)


def call(app, method: str, path: str, body: Any = None, headers: dict[str, str] | None = None,
         query: str = "", remote_addr: str = "203.0.113.7") -> Result:
    environ: dict[str, Any] = {}
    setup_testing_defaults(environ)
    raw = b"" if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    environ.update({"REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": query,
                    "CONTENT_LENGTH": str(len(raw)), "wsgi.input": io.BytesIO(raw),
                    "REMOTE_ADDR": remote_addr})
    if body is not None:
        environ["CONTENT_TYPE"] = "application/json"
    for k, v in (headers or {}).items():
        name = k.upper().replace("-", "_")
        environ[name if name in ("CONTENT_TYPE",) else f"HTTP_{name}"] = v
    captured: dict[str, Any] = {}

    def start_response(status: str, hdrs: list[tuple[str, str]]) -> None:
        captured["status"], captured["headers"] = status, hdrs

    payload = b"".join(app(environ, start_response))
    return Result(captured["status"], captured["headers"], payload)
