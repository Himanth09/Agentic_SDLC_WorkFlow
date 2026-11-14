"""Minimal WSGI front-end."""
import json

from .service import LinkService, NotFound


def make_app(service: LinkService):
    def create(environ):
        length = int(environ.get("CONTENT_LENGTH") or 0)
        body = json.loads(environ["wsgi.input"].read(length) or b"{}")
        try:
            link = service.shorten(body.get("url", ""), body.get("alias"))
        except ValueError as exc:
            return "400 Bad Request", {"error": str(exc)}, []
        return "201 Created", {"code": link.code, "target_url": link.target_url}, []

    def redirect(environ, code):
        try:
            url = service.resolve(code)
        except NotFound:
            return "404 Not Found", {"error": "not found"}, []
        return "302 Found", None, [("Location", url)]

    def info(environ, code):
        try:
            link = service.info(code)
        except NotFound:
            return "404 Not Found", {"error": "not found"}, []
        return "200 OK", {"code": link.code, "target_url": link.target_url,
                          "clicks": link.clicks}, []

    ROUTES = {
        ("POST", "/links"): create,
    }

    def app(environ, start_response):
        method, path = environ["REQUEST_METHOD"], environ.get("PATH_INFO", "/")
        handler = ROUTES.get((method, path))
        if handler:
            status, payload, headers = handler(environ)
        elif method == "GET" and path.startswith("/links/"):
            status, payload, headers = info(environ, path[len("/links/"):])
        elif method == "GET" and path.count("/") == 1 and len(path) > 1:
            status, payload, headers = redirect(environ, path[1:])
        else:
            status, payload, headers = "404 Not Found", {"error": "no route"}, []
        body = b"" if payload is None else json.dumps(payload).encode()
        start_response(status, [("Content-Type", "application/json"), *headers])
        return [body]

    return app
