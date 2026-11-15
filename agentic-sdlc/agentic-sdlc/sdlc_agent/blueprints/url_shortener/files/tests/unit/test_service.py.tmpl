# Unit tests for LinkService business rules, run against in-memory adapters and a fake
# clock: creation, aliases, collisions, expiry, ownership, caching and click analytics.
from dataclasses import replace

import pytest

from shortener.analytics import ClickRecorder, InMemoryAnalyticsStore
from shortener.codes import CodeGenerator
from shortener.errors import (AliasTaken, CodeSpaceExhausted, InvalidAlias, InvalidTtl, InvalidUrl,
                              LinkExpired, LinkNotFound)
from shortener.repository import InMemoryLinkRepository
from shortener.service import LinkService


def test_create_and_resolve(memory_service):
    """Covers: AC-CREATE, AC-REDIRECT"""
    link = memory_service.create("https://example.com/landing")
    # Generated codes use the configured length.
    assert len(link.code) == memory_service.settings.code_length
    assert memory_service.resolve(link.code).target_url == "https://example.com/landing"


def test_create_rejects_unsafe_url(memory_service):
    """Covers: AC-VALIDATE"""
    with pytest.raises(InvalidUrl):
        memory_service.create("javascript:alert(document.cookie)")


def test_custom_alias_is_never_overwritten(memory_service):
    """Covers: AC-ALIAS"""
    # Aliases disabled by the requirement: any alias is refused.
    if not memory_service.settings.allow_custom_alias:
        with pytest.raises(InvalidAlias):
            memory_service.create("https://example.com/a", alias="spring-sale")
        return
    first = memory_service.create("https://example.com/a", alias="spring-sale")
    # A second claim on the same alias fails...
    with pytest.raises(AliasTaken):
        memory_service.create("https://evil.example/b", alias="spring-sale")
    # ...and the original target is unchanged.
    assert memory_service.resolve("spring-sale").target_url == first.target_url


def test_collision_is_retried(settings, clock):
    """Covers: AC-CONCURRENCY"""
    # Scripted randomness: link a gets 5; link b draws 5 twice (collisions), then 9.
    seq = iter([5, 5, 5, 9])
    codes = CodeGenerator(settings.code_length, randbelow=lambda _n: next(seq))
    svc = LinkService(settings, InMemoryLinkRepository(),
                      ClickRecorder(InMemoryAnalyticsStore(), synchronous=True),
                      codes=codes, clock=clock)
    a = svc.create("https://example.com/1")
    b = svc.create("https://example.com/2")
    assert a.code != b.code and svc.collisions == 2


def test_code_space_exhaustion_is_reported(settings, clock):
    """Covers: AC-CONCURRENCY"""
    # The generator always returns the same code, so the second create can never succeed.
    codes = CodeGenerator(settings.code_length, randbelow=lambda _n: 1)
    svc = LinkService(settings, InMemoryLinkRepository(),
                      ClickRecorder(InMemoryAnalyticsStore(), synchronous=True),
                      codes=codes, clock=clock)
    svc.create("https://example.com/1")
    # After MAX_CODE_ATTEMPTS collisions the service gives up with a clear error.
    with pytest.raises(CodeSpaceExhausted):
        svc.create("https://example.com/2")


def test_ttl_expiry(memory_service, clock):
    """Covers: AC-EXPIRY"""
    link = memory_service.create("https://example.com/flash", ttl_seconds=3600)
    assert memory_service.resolve(link.code)
    # One second past the hour, the link is expired (even though it is cached).
    clock.advance(3601)
    with pytest.raises(LinkExpired):
        memory_service.resolve(link.code)


# Zero, under the 60 s minimum, negative, a string, a float, a bool, and far over the maximum.
@pytest.mark.parametrize("ttl", [0, 59, -5, "3600", 3.5, True, 10**12])
def test_invalid_ttl_rejected(memory_service, ttl):
    """Covers: AC-EXPIRY"""
    with pytest.raises(InvalidTtl):
        memory_service.create("https://example.com", ttl_seconds=ttl)


def test_default_ttl_applied_when_configured(settings, clock):
    """Covers: AC-EXPIRY"""
    # With a 1-day default, a link created without a TTL expires 24 hours later.
    svc = LinkService(replace(settings, default_ttl_days=1), InMemoryLinkRepository(),
                      ClickRecorder(InMemoryAnalyticsStore(), synchronous=True), clock=clock)
    link = svc.create("https://example.com")
    assert link.expires_at == pytest.approx(clock() + 86_400)


def test_disable_respects_ownership_and_invalidates_cache(memory_service):
    """Covers: AC-ABUSE, AC-HOTPATH"""
    link = memory_service.create("https://example.com", owner="key-a")
    memory_service.resolve(link.code)                      # warm cache
    with pytest.raises(LinkNotFound):
        memory_service.disable(link.code, owner="key-b")   # not the owner
    memory_service.disable(link.code, owner="key-a")
    with pytest.raises(LinkNotFound):
        memory_service.resolve(link.code)                  # cache was invalidated


def test_resolve_uses_cache(memory_service):
    """Covers: AC-HOTPATH"""
    link = memory_service.create("https://example.com")
    # First resolve misses and fills the cache; the next four are hits.
    for _ in range(5):
        memory_service.resolve(link.code)
    assert memory_service.cache.hits >= 4


def test_unknown_code_is_negatively_cached(memory_service):
    """Covers: AC-HOTPATH"""
    # First lookup goes to the repository; the next two are answered by the cached "not found".
    for _ in range(3):
        with pytest.raises(LinkNotFound):
            memory_service.resolve("nope1234")
    assert memory_service.cache.hits == 2


def test_click_recording_and_stats(memory_service):
    """Covers: AC-ANALYTICS, AC-PRIVACY"""
    link = memory_service.create("https://example.com")
    # One mobile click from a referrer, one bot click with no referrer.
    memory_service.record_click(link, "https://news.example.org/x", "Mozilla/5.0 (iPhone)", "198.51.100.1")
    memory_service.record_click(link, None, "Googlebot", "198.51.100.2")
    stats = memory_service.stats(link.code)
    assert stats["total_clicks"] == 2
    # No referrer is reported as "(direct)".
    assert {r["host"] for r in stats["top_referrers"]} == {"news.example.org", "(direct)"}
    assert stats["user_agents"] == {"bot": 1, "mobile": 1}
    # No stored event contains any part of a raw IP address.
    events = memory_service.recorder.store.events
    assert all(e.visitor is None or "198.51" not in e.visitor for e in events)
