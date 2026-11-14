"""Thread-safe LRU cache with per-entry TTL for the redirect hot path.

Positive entries absorb read amplification on popular links (read:write is
~100:1); short-lived negative entries stop bursts of lookups for unknown codes
(scanners) from reaching the database. Production equivalent: ElastiCache.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Callable, Generic, TypeVar

V = TypeVar("V")
_MISSING = object()


class TTLCache(Generic[V]):
    def __init__(self, max_size: int, clock: Callable[[], float] = time.monotonic) -> None:
        if max_size <= 0:
            raise ValueError("max_size must be positive")
        self.max_size = max_size
        self._clock = clock
        self._data: OrderedDict[str, tuple[float, object]] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> object:
        """Return the cached value (which may be None for a negative entry) or _MISSING."""
        now = self._clock()
        with self._lock:
            item = self._data.get(key)
            if item is None or item[0] <= now:
                if item is not None:
                    del self._data[key]
                self.misses += 1
                return _MISSING
            self._data.move_to_end(key)
            self.hits += 1
            return item[1]

    def set(self, key: str, value: object, ttl_s: float) -> None:
        with self._lock:
            self._data[key] = (self._clock() + ttl_s, value)
            self._data.move_to_end(key)
            while len(self._data) > self.max_size:
                self._data.popitem(last=False)

    def invalidate(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    @staticmethod
    def is_missing(value: object) -> bool:
        return value is _MISSING
