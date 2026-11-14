"""Tests for link expiration (generated)."""
import io
import sqlite3

import pytest

from links.app import make_app
from links.service import Expired, LinkService
from links.store import LinkStore


class Clock:
    def __init__(self):
        self.t = 1_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def service(clock):
    return LinkService(LinkStore(), clock=clock)


def _get(app, path):
    env = {"REQUEST_METHOD": "GET", "PATH_INFO": path, "wsgi.input": io.BytesIO(b"")}
    out = {}
    b"".join(app(env, lambda s, h: out.update(status=s, headers=dict(h))))
    return out["status"], out["headers"]


def test_link_with_ttl_expires(service, clock):
    """Covers: AC-EXP-TTL"""
    link = service.shorten("https://example.com/flash", ttl_seconds=60)
    assert link.expires_at == 1_060.0
    assert service.resolve(link.code) == "https://example.com/flash"
    clock.t = 1_060.0
    with pytest.raises(Expired):
        service.resolve(link.code)


def test_expired_link_returns_410_over_http(service, clock):
    """Covers: AC-EXP-TTL"""
    link = service.shorten("https://example.com/flash", ttl_seconds=60)
    app = make_app(service)
    assert _get(app, f"/{link.code}")[0].startswith("302")
    clock.t += 61
    assert _get(app, f"/{link.code}")[0].startswith("410")


def test_links_without_ttl_never_expire(service, clock):
    """Covers: AC-EXP-COMPAT"""
    link = service.shorten("https://example.com/forever")
    clock.t += 10 * 365 * 86_400
    assert link.expires_at is None
    assert service.resolve(link.code) == "https://example.com/forever"


@pytest.mark.parametrize("ttl", [0, -1, "60", 1.5, True])
def test_invalid_ttl_rejected(service, ttl):
    """Covers: AC-EXP-TTL"""
    with pytest.raises(ValueError):
        service.shorten("https://example.com", ttl_seconds=ttl)


def test_existing_database_is_migrated(tmp_path):
    """Covers: AC-EXP-MIGRATION"""
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE links (code TEXT PRIMARY KEY, target_url TEXT NOT NULL, "
                 "created_at REAL NOT NULL, clicks INTEGER NOT NULL DEFAULT 0)")
    conn.execute("INSERT INTO links VALUES ('old123', 'https://example.com/old', 1.0, 7)")
    conn.commit()
    conn.close()
    store = LinkStore(path)
    link = store.get("old123")
    assert link.clicks == 7 and link.expires_at is None
    LinkStore(path)  # idempotent: second open does not fail
