"""Regression tests for the alias-overwrite bug (generated)."""
import io
import json

import pytest

from links.app import make_app
from links.service import AliasTaken, LinkService
from links.store import LinkStore


@pytest.fixture
def service():
    return LinkService(LinkStore())


def _post(app, body):
    raw = json.dumps(body).encode()
    env = {"REQUEST_METHOD": "POST", "PATH_INFO": "/links", "CONTENT_LENGTH": str(len(raw)),
           "wsgi.input": io.BytesIO(raw)}
    out = {}
    payload = b"".join(app(env, lambda s, h: out.update(status=s)))
    return out["status"], json.loads(payload or b"null")


def test_duplicate_alias_is_rejected_and_original_kept(service):
    """Covers: AC-BUG-ALIAS"""
    service.shorten("https://example.com/original", alias="promo")
    with pytest.raises(AliasTaken):
        service.shorten("https://attacker.example", alias="promo")
    assert service.resolve("promo") == "https://example.com/original"


def test_duplicate_alias_returns_409_over_http(service):
    """Covers: AC-BUG-ALIAS"""
    app = make_app(service)
    assert _post(app, {"url": "https://example.com/a", "alias": "launch"})[0].startswith("201")
    status, body = _post(app, {"url": "https://example.com/b", "alias": "launch"})
    assert status.startswith("409") and body["error"] == "alias already in use"


def test_random_code_collision_is_retried(service, monkeypatch):
    """Covers: AC-BUG-ALIAS"""
    import links.service as mod
    seq = iter("aaaaaa" "aaaaaa" "bbbbbb")
    monkeypatch.setattr(mod.secrets, "choice", lambda _alphabet: next(seq))
    first = service.shorten("https://example.com/1")
    second = service.shorten("https://example.com/2")
    assert (first.code, second.code) == ("aaaaaa", "bbbbbb")
    assert service.resolve("aaaaaa") == "https://example.com/1"
