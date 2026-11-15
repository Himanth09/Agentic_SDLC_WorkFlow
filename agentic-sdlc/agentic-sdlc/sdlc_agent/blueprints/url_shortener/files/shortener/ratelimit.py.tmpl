"""Per-client token bucket for write endpoints (abuse protection).

In-process buckets are correct for a single instance; behind a load balancer
use a shared store (Redis INCR with expiry, or API Gateway usage plans).
"""
from __future__ import annotations

# math.ceil: round the Retry-After wait up to whole seconds.
import math
# threading: one lock guards all buckets across request threads.
import threading
# time.monotonic: default clock, immune to wall-clock changes.
import time
from typing import Callable


# Each client has a bucket holding up to `capacity` tokens; a request spends one, and
# tokens refill continuously at rate_per_min / 60 per second.
class TokenBucketLimiter:
    # `clock` is injectable so tests can advance time without sleeping.
    def __init__(self, rate_per_min: int, burst: int | None = None,
                 clock: Callable[[], float] = time.monotonic, max_keys: int = 100_000) -> None:
        # Maximum burst size; defaults to one minute's worth of requests.
        self.capacity = float(burst if burst is not None else rate_per_min)
        # Tokens added per second.
        self.refill_per_s = rate_per_min / 60.0
        self._clock = clock
        # Upper bound on tracked clients, so memory can't grow without limit.
        self._max_keys = max_keys
        # client key -> (tokens left, time of last update).
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def acquire(self, key: str) -> tuple[bool, int]:
        """Take one token. Returns (allowed, retry_after_seconds)."""
        now = self._clock()
        with self._lock:
            # A client seen for the first time starts with a full bucket.
            tokens, last = self._buckets.get(key, (self.capacity, now))
            # Refill for the time elapsed since the last request, capped at capacity.
            tokens = min(self.capacity, tokens + (now - last) * self.refill_per_s)
            # Enough for this request: spend a token and allow it.
            if tokens >= 1.0:
                self._buckets[key] = (tokens - 1.0, now)
                self._evict_if_needed()
                return True, 0
            # Not enough: save the refilled state and say how long until one token is available.
            self._buckets[key] = (tokens, now)
            wait = math.ceil((1.0 - tokens) / self.refill_per_s)
            # Always ask the client to wait at least one second.
            return False, max(wait, 1)

    # Keep the bucket table bounded (many distinct clients, e.g. a scan from many IPs).
    def _evict_if_needed(self) -> None:
        if len(self._buckets) > self._max_keys:
            # drop the stalest quarter; they would be full again anyway
            for k, _ in sorted(self._buckets.items(), key=lambda kv: kv[1][1])[: self._max_keys // 4]:
                del self._buckets[k]
