"""End-to-end HTTP tests: WSGI app + real SQLite storage + analytics pipeline."""
# Each test docstring names the acceptance criteria the test covers; the
# traceability report is built from those docstrings.
from dataclasses import replace

from shortener.http import create_app
from shortener.ratelimit import TokenBucketLimiter

from tests.support import call


# POST a new link; extra keyword arguments (alias, ttl_seconds, ...) go into the body.
def _create(app, auth, **body):
    return call(app, "POST", "/api/v1/links", {"url": "https://example.com/landing", **body},
                headers=auth)


def test_create_returns_201_with_short_url(app, auth, settings):
    """Covers: AC-CREATE, AC-CONTRACT"""
    res = _create(app, auth)
    assert res.status == 201
    data = res.json()
    # short_url is base_url + "/" + code.
    assert data["short_url"] == f"{settings.base_url}/{data['code']}"
    # Location points at the new resource.
    assert res.headers["location"] == f"/api/v1/links/{data['code']}"
    # Security headers are present on every response.
    assert res.headers["x-content-type-options"] == "nosniff"


def test_redirect_returns_302_to_target(app, auth):
    """Covers: AC-REDIRECT"""
    code = _create(app, auth).json()["code"]
    res = call(app, "GET", f"/{code}")
    # Temporary redirect, so every click comes back to the service.
    assert res.status == 302
    assert res.headers["location"] == "https://example.com/landing"


def test_unknown_code_returns_problem_json_404(app):
    """Covers: AC-REDIRECT, AC-CONTRACT"""
    res = call(app, "GET", "/doesnotexist")
    assert res.status == 404
    # Errors use the RFC 9457 media type, with the status repeated in the body.
    assert res.headers["content-type"] == "application/problem+json"
    assert res.json()["status"] == 404


def test_unsafe_url_rejected_with_422(app, auth):
    """Covers: AC-VALIDATE"""
    # A script URL must never become a redirect target.
    res = call(app, "POST", "/api/v1/links", {"url": "javascript:alert(1)"}, headers=auth)
    assert res.status == 422 and res.json()["type"].endswith("/invalid_url")


def test_alias_conflict_returns_409_and_keeps_original(app, auth, settings):
    """Covers: AC-ALIAS"""
    first = _create(app, auth, alias="launch-day")
    # If the requirement disabled custom aliases, any alias is rejected instead.
    if not settings.allow_custom_alias:
        assert first.status == 422
        return
    assert first.status == 201
    # Someone else tries to take the same alias for a different target...
    clash = call(app, "POST", "/api/v1/links",
                 {"url": "https://attacker.example", "alias": "launch-day"}, headers=auth)
    # ...and is refused, while the original link still redirects to its own target.
    assert clash.status == 409
    assert call(app, "GET", "/launch-day").headers["location"] == "https://example.com/landing"


def test_expired_link_returns_410(app, auth, clock):
    """Covers: AC-EXPIRY"""
    code = _create(app, auth, ttl_seconds=120).json()["code"]
    # Works before the TTL elapses...
    assert call(app, "GET", f"/{code}").status == 302
    # ...and is Gone one second after it.
    clock.advance(121)
    assert call(app, "GET", f"/{code}").status == 410


def test_clicks_are_counted_in_stats(app, auth):
    """Covers: AC-ANALYTICS, AC-PRIVACY"""
    code = _create(app, auth).json()["code"]
    # Click 1: mobile browser arriving from a referrer, default client IP.
    call(app, "GET", f"/{code}", headers={"Referer": "https://twitter.example/post/1",
                                          "User-Agent": "Mozilla/5.0 (Android 14) Mobile"})
    # Click 2: desktop browser, no referrer, a different client IP.
    call(app, "GET", f"/{code}", headers={"User-Agent": "Mozilla/5.0 (X11; Linux)"},
         remote_addr="198.51.100.20")
    stats = call(app, "GET", f"/api/v1/links/{code}/stats", query="days=7").json()
    assert stats["total_clicks"] == 2
    assert stats["unique_visitors"] in (0, 2)  # 0 when ip mode is "drop"
    assert stats["user_agents"] == {"desktop": 1, "mobile": 1}
    # Both clicks fall on the same (fake) day.
    assert stats["clicks_by_day"][0]["clicks"] == 2
    # Only the referrer host is reported, not the full referrer URL.
    assert {"host": "twitter.example", "clicks": 1} in stats["top_referrers"]


def test_stats_rejects_non_integer_days(app, auth):
    """Covers: AC-CONTRACT"""
    code = _create(app, auth).json()["code"]
    assert call(app, "GET", f"/api/v1/links/{code}/stats", query="days=abc").status == 400


def test_delete_disables_link(app, auth):
    """Covers: AC-ABUSE"""
    code = _create(app, auth).json()["code"]
    assert call(app, "DELETE", f"/api/v1/links/{code}", headers=auth).status == 204
    # Disabled immediately: the cached entry was invalidated too.
    assert call(app, "GET", f"/{code}").status == 404


def test_write_requires_api_key_when_enabled(sqlite_service, settings, clock):
    """Covers: AC-ABUSE"""
    # Same service, but with API keys switched on.
    app = create_app(sqlite_service, replace(settings, require_api_key=True),
                     TokenBucketLimiter(100, clock=clock))
    # No key -> 401.
    assert call(app, "POST", "/api/v1/links", {"url": "https://example.com"}).status == 401
    # Unknown key -> 401.
    assert call(app, "POST", "/api/v1/links", {"url": "https://example.com"},
                headers={"X-API-Key": "wrong"}).status == 401


def test_create_is_rate_limited(sqlite_service, settings, clock, auth):
    """Covers: AC-ABUSE"""
    # Two creates per minute; the clock doesn't move, so no tokens refill.
    app = create_app(sqlite_service, settings, TokenBucketLimiter(2, clock=clock))
    statuses = [_create(app, auth).status for _ in range(3)]
    assert statuses == [201, 201, 429]
    # A limited client is told how long to wait.
    res = _create(app, auth)
    assert int(res.headers["retry-after"]) >= 1


def test_malformed_requests(app, auth, settings):
    """Covers: AC-CONTRACT"""
    # Body isn't valid JSON.
    assert call(app, "POST", "/api/v1/links", b"{not json", headers=auth).status == 400
    # Valid JSON, but not an object.
    assert call(app, "POST", "/api/v1/links", [1, 2], headers=auth).status == 400
    # Unknown field.
    assert call(app, "POST", "/api/v1/links", {"url": "https://e.com", "x": 1},
                headers=auth).status == 400
    # Body over max_body_bytes.
    big = {"url": "https://example.com/" + "a" * settings.max_body_bytes}
    assert call(app, "POST", "/api/v1/links", big, headers=auth).status == 413
    # Known path, unsupported method.
    assert call(app, "PUT", "/api/v1/links/abcd123", {}, headers=auth).status == 405


def test_health(app):
    """Covers: AC-CONTRACT"""
    res = call(app, "GET", "/healthz")
    assert res.status == 200 and res.json()["status"] == "ok"
