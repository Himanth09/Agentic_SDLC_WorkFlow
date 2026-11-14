"""Per-client token bucket for write endpoints (abuse protection).

In-process buckets are correct for a single instance; behind a load balancer
use a shared store (Redis INCR with expiry, or API Gateway usage plans).
"""
from __future__ import annotations

import math
import threading
import time
from typing import Callable


class TokenBucketLimiter:
    def __init__(self, rate_per_min: int, burst: int | None = None,
                 clock: Callable[[], float] = time.monotonic, max_keys: int = 100_000) -> None:
        self.capacity = float(burst if burst is not None else rate_per_min)
        self.refill_per_s = rate_per_min / 60.0
        self._clock = clock
        self._max_keys = max_keys
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def acquire(self, key: str) -> tuple[bool, int]:
        """Take one token. Returns (allowed, retry_after_seconds)."""
        now = self._clock()
        with self._lock:
            tokens, last = self._buckets.get(key, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - last) * self.refill_per_s)
            if tokens >= 1.0:
                self._buckets[key] = (tokens - 1.0, now)
                self._evict_if_needed()
                return True, 0
            self._buckets[key] = (tokens, now)
            wait = math.ceil((1.0 - tokens) / self.refill_per_s)
            return False, max(wait, 1)

    def _evict_if_needed(self) -> None:
        if len(self._buckets) > self._max_keys:
            # drop the stalest quarter; they would be full again anyway
            for k, _ in sorted(self._buckets.items(), key=lambda kv: kv[1][1])[: self._max_keys // 4]:
                del self._buckets[k]
