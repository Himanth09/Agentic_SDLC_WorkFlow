"""Thread-safe LRU cache with per-entry TTL for the redirect hot path.

Positive entries absorb read amplification on popular links (read:write is
~100:1); short-lived negative entries stop bursts of lookups for unknown codes
(scanners) from reaching the database. Production equivalent: ElastiCache.
"""
from __future__ import annotations

# threading: one lock protects the cache across request threads.
import threading
# time.monotonic: default clock, immune to wall-clock changes.
import time
# OrderedDict: keeps entries in recency order, giving O(1) LRU eviction.
from collections import OrderedDict
# Typing helpers for an injectable clock and the generic value type.
from typing import Callable, Generic, TypeVar

# Type of the cached values.
V = TypeVar("V")
# Sentinel meaning "not in the cache", distinct from a cached None (a negative entry).
_MISSING = object()


class TTLCache(Generic[V]):
    # `clock` is injectable so tests can move time forward without sleeping.
    def __init__(self, max_size: int, clock: Callable[[], float] = time.monotonic) -> None:
        if max_size <= 0:
            raise ValueError("max_size must be positive")
        # Maximum number of entries before the least recently used one is evicted.
        self.max_size = max_size
        self._clock = clock
        # key -> (expires_at, value), ordered from least to most recently used.
        self._data: OrderedDict[str, tuple[float, object]] = OrderedDict()
        self._lock = threading.Lock()
        # Counters exposed for observability (hit ratio).
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> object:
        """Return the cached value (which may be None for a negative entry) or _MISSING."""
        now = self._clock()
        with self._lock:
            item = self._data.get(key)
            # Absent or expired: count a miss.
            if item is None or item[0] <= now:
                # Remove the expired entry so it doesn't take up space.
                if item is not None:
                    del self._data[key]
                self.misses += 1
                return _MISSING
            # Hit: mark as most recently used.
            self._data.move_to_end(key)
            self.hits += 1
            return item[1]

    # Store a value (None is allowed, meaning "known not to exist") for ttl_s seconds.
    def set(self, key: str, value: object, ttl_s: float) -> None:
        with self._lock:
            self._data[key] = (self._clock() + ttl_s, value)
            self._data.move_to_end(key)
            # Evict least recently used entries until we are back within max_size.
            while len(self._data) > self.max_size:
                self._data.popitem(last=False)

    # Drop one entry, e.g. after a link is deleted or changed.
    def invalidate(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)

    # Current number of entries (expired ones are only removed when touched).
    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    # True if get() returned the "not cached" sentinel.
    @staticmethod
    def is_missing(value: object) -> bool:
        return value is _MISSING
