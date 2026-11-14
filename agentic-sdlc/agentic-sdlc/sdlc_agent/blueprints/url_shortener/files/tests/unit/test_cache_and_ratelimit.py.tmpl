import pytest

from shortener.cache import TTLCache
from shortener.ratelimit import TokenBucketLimiter


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_cache_hit_miss_and_expiry():
    """Covers: AC-HOTPATH"""
    clock = Clock()
    cache = TTLCache(10, clock=clock)
    assert TTLCache.is_missing(cache.get("a"))
    cache.set("a", "A", ttl_s=5)
    assert cache.get("a") == "A"
    clock.t = 5.1
    assert TTLCache.is_missing(cache.get("a"))
    assert (cache.hits, cache.misses) == (1, 2)


def test_cache_negative_entries_and_lru_eviction():
    """Covers: AC-HOTPATH"""
    cache = TTLCache(2, clock=Clock())
    cache.set("missing", None, ttl_s=5)
    assert cache.get("missing") is None and not TTLCache.is_missing(cache.get("missing"))
    cache.set("b", "B", 5)
    cache.get("missing")            # touch -> most recently used
    cache.set("c", "C", 5)          # evicts "b"
    assert TTLCache.is_missing(cache.get("b"))
    assert len(cache) == 2
    cache.invalidate("c")
    assert TTLCache.is_missing(cache.get("c"))


def test_cache_rejects_invalid_size():
    with pytest.raises(ValueError):
        TTLCache(0)


def test_token_bucket_limits_and_refills():
    """Covers: AC-ABUSE"""
    clock = Clock()
    limiter = TokenBucketLimiter(rate_per_min=3, clock=clock)
    assert [limiter.acquire("k")[0] for _ in range(3)] == [True, True, True]
    allowed, retry = limiter.acquire("k")
    assert not allowed and retry >= 1
    assert limiter.acquire("other")[0], "buckets are per client"
    clock.t = 20.0                  # 3/min => one token every 20s
    assert limiter.acquire("k")[0]


def test_token_bucket_evicts_stale_keys():
    clock = Clock()
    limiter = TokenBucketLimiter(rate_per_min=10, clock=clock, max_keys=8)
    for i in range(20):
        clock.t = float(i)
        limiter.acquire(f"client-{i}")
    assert len(limiter._buckets) <= 8
