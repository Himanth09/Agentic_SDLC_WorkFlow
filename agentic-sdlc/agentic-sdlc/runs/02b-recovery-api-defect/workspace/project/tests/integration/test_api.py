"""End-to-end HTTP tests: WSGI app + real SQLite storage + analytics pipeline."""
from dataclasses import replace

from shortener.http import create_app
from shortener.ratelimit import TokenBucketLimiter

from tests.support import call


def _create(app, auth, **body):
    return call(app, "POST", "/api/v1/links", {"url": "https://example.com/landing", **body},
                headers=auth)


def test_create_returns_201_with_short_url(app, auth, settings):
    """Covers: AC-CREATE, AC-CONTRACT"""
    res = _create(app, auth)
    assert res.status == 201
    data = res.json()
    assert data["short_url"] == f"{settings.base_url}/{data['code']}"
    assert res.headers["location"] == f"/api/v1/links/{data['code']}"
    assert res.headers["x-content-type-options"] == "nosniff"


def test_redirect_returns_302_to_target(app, auth):
    """Covers: AC-REDIRECT"""
    code = _create(app, auth).json()["code"]
    res = call(app, "GET", f"/{code}")
    assert res.status == 302
    assert res.headers["location"] == "https://example.com/landing"


def test_unknown_code_returns_problem_json_404(app):
    """Covers: AC-REDIRECT, AC-CONTRACT"""
    res = call(app, "GET", "/doesnotexist")
    assert res.status == 404
    assert res.headers["content-type"] == "application/problem+json"
    assert res.json()["status"] == 404


def test_unsafe_url_rejected_with_422(app, auth):
    """Covers: AC-VALIDATE"""
    res = call(app, "POST", "/api/v1/links", {"url": "javascript:alert(1)"}, headers=auth)
    assert res.status == 422 and res.json()["type"].endswith("/invalid_url")


def test_alias_conflict_returns_409_and_keeps_original(app, auth, settings):
    """Covers: AC-ALIAS"""
    first = _create(app, auth, alias="launch-day")
    if not settings.allow_custom_alias:
        assert first.status == 422
        return
    assert first.status == 201
    clash = call(app, "POST", "/api/v1/links",
                 {"url": "https://attacker.example", "alias": "launch-day"}, headers=auth)
    assert clash.status == 409
    assert call(app, "GET", "/launch-day").headers["location"] == "https://example.com/landing"


def test_expired_link_returns_410(app, auth, clock):
    """Covers: AC-EXPIRY"""
    code = _create(app, auth, ttl_seconds=120).json()["code"]
    assert call(app, "GET", f"/{code}").status == 302
    clock.advance(121)
    assert call(app, "GET", f"/{code}").status == 410


def test_clicks_are_counted_in_stats(app, auth):
    """Covers: AC-ANALYTICS, AC-PRIVACY"""
    code = _create(app, auth).json()["code"]
    call(app, "GET", f"/{code}", headers={"Referer": "https://twitter.example/post/1",
                                          "User-Agent": "Mozilla/5.0 (Android 14) Mobile"})
    call(app, "GET", f"/{code}", headers={"User-Agent": "Mozilla/5.0 (X11; Linux)"},
         remote_addr="198.51.100.20")
    stats = call(app, "GET", f"/api/v1/links/{code}/stats", query="days=7").json()
    assert stats["total_clicks"] == 2
    assert stats["unique_visitors"] in (0, 2)  # 0 when ip mode is "drop"
    assert stats["user_agents"] == {"desktop": 1, "mobile": 1}
    assert stats["clicks_by_day"][0]["clicks"] == 2
    assert {"host": "twitter.example", "clicks": 1} in stats["top_referrers"]


def test_stats_rejects_non_integer_days(app, auth):
    """Covers: AC-CONTRACT"""
    code = _create(app, auth).json()["code"]
    assert call(app, "GET", f"/api/v1/links/{code}/stats", query="days=abc").status == 400


def test_delete_disables_link(app, auth):
    """Covers: AC-ABUSE"""
    code = _create(app, auth).json()["code"]
    assert call(app, "DELETE", f"/api/v1/links/{code}", headers=auth).status == 204
    assert call(app, "GET", f"/{code}").status == 404


def test_write_requires_api_key_when_enabled(sqlite_service, settings, clock):
    """Covers: AC-ABUSE"""
    app = create_app(sqlite_service, replace(settings, require_api_key=True),
                     TokenBucketLimiter(100, clock=clock))
    assert call(app, "POST", "/api/v1/links", {"url": "https://example.com"}).status == 401
    assert call(app, "POST", "/api/v1/links", {"url": "https://example.com"},
                headers={"X-API-Key": "wrong"}).status == 401


def test_create_is_rate_limited(sqlite_service, settings, clock, auth):
    """Covers: AC-ABUSE"""
    app = create_app(sqlite_service, settings, TokenBucketLimiter(2, clock=clock))
    statuses = [_create(app, auth).status for _ in range(3)]
    assert statuses == [201, 201, 429]
    res = _create(app, auth)
    assert int(res.headers["retry-after"]) >= 1


def test_malformed_requests(app, auth, settings):
    """Covers: AC-CONTRACT"""
    assert call(app, "POST", "/api/v1/links", b"{not json", headers=auth).status == 400
    assert call(app, "POST", "/api/v1/links", [1, 2], headers=auth).status == 400
    assert call(app, "POST", "/api/v1/links", {"url": "https://e.com", "x": 1},
                headers=auth).status == 400
    big = {"url": "https://example.com/" + "a" * settings.max_body_bytes}
    assert call(app, "POST", "/api/v1/links", big, headers=auth).status == 413
    assert call(app, "PUT", "/api/v1/links/abcd123", {}, headers=auth).status == 405


def test_health(app):
    """Covers: AC-CONTRACT"""
    res = call(app, "GET", "/healthz")
    assert res.status == 200 and res.json()["status"] == "ok"
