"""Application service: the only place business rules live.

HTTP, storage, cache and analytics are injected, so the same service runs
against SQLite in development and DynamoDB/ElastiCache/Kinesis in production.
"""
from __future__ import annotations

# time.time: default wall clock for creation, expiry and click timestamps.
import time
from typing import Callable

from .analytics import ClickRecorder, anonymize_ip
from .cache import TTLCache
from .codes import CodeGenerator
from .config import Settings
# Domain model plus the pure validation/classification helpers.
from .domain import ClickEvent, Link, classify_user_agent, referrer_host, validate_alias, validate_url
from .errors import (AliasTaken, CodeSpaceExhausted, DuplicateKey, InvalidAlias, InvalidTtl,
                     LinkExpired, LinkNotFound)
from .repository import LinkRepository

# Retries on code collisions before giving up (collisions are very rare at 62^7).
MAX_CODE_ATTEMPTS = 5
# Seconds in a day.
DAY_S = 86_400


class LinkService:
    # Every dependency is injected; tests pass in-memory adapters, a fake clock and a fake code generator.
    def __init__(self, settings: Settings, links: LinkRepository, recorder: ClickRecorder,
                 cache: TTLCache | None = None, codes: CodeGenerator | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.settings = settings
        # Link storage (the repository port).
        self.links = links
        # Analytics sink for clicks.
        self.recorder = recorder
        # Optional read-through cache for the redirect path.
        self.cache = cache
        self.codes = codes or CodeGenerator(settings.code_length)
        self.clock = clock
        # Number of generated codes that were already taken (observability).
        self.collisions = 0

    # ----------------------------------------------------------------- writes
    # Validate input and store a new link with a custom alias or a generated code.
    def create(self, target_url: object, alias: object = None, ttl_seconds: object = None,
               owner: str | None = None) -> Link:
        # Raises InvalidUrl for unsafe or malformed targets.
        url = validate_url(target_url, self.settings.max_url_length)
        now = self.clock()
        # Raises InvalidTtl; None means no expiry.
        expires_at = self._expiry(ttl_seconds, now)
        # Custom alias path.
        if alias is not None:
            if not self.settings.allow_custom_alias:
                raise InvalidAlias("custom aliases are disabled")
            code = validate_alias(alias)
            link = Link(code, url, now, expires_at, owner, is_custom=True)
            try:
                # Atomic insert: an existing alias is never overwritten.
                self.links.insert(link)
            except DuplicateKey:
                raise AliasTaken(f"alias '{code}' is already in use") from None
            # Clear any cached "not found" entry for this code.
            self._invalidate(code)
            return link
        # Generated code path: try new random codes until one is free.
        for _ in range(MAX_CODE_ATTEMPTS):
            link = Link(self.codes.next(), url, now, expires_at, owner)
            try:
                self.links.insert(link)
            except DuplicateKey:
                # Collision: count it and try another code.
                self.collisions += 1
                continue
            self._invalidate(link.code)
            return link
        # Repeated collisions suggest the code space is too small for the data set.
        raise CodeSpaceExhausted(f"no free code after {MAX_CODE_ATTEMPTS} attempts")

    # Soft-delete a link. Only the owner may do it, unless the link has no owner.
    def disable(self, code: str, owner: str | None) -> None:
        link = self.links.get(code)
        # Same response for "missing" and "not yours": do not leak existence.
        if link is None or link.disabled or (link.owner and link.owner != owner):
            raise LinkNotFound(f"no link '{code}'")
        self.links.disable(code)
        # Drop the cached copy so redirects stop immediately.
        self._invalidate(code)

    # ------------------------------------------------------------------ reads
    def resolve(self, code: str) -> Link:
        """Hot path: cache -> repository. Enforces disabled/expired state."""
        link = self._lookup(code)
        # Disabled links look exactly like missing ones to clients.
        if link is None or link.disabled:
            raise LinkNotFound(f"no link '{code}'")
        # Expiry is checked on every request, so a cached link still expires on time.
        if link.is_expired(self.clock()):
            raise LinkExpired(f"link '{code}' expired")
        return link

    # Metadata lookup: reads the repository directly (no cache) and includes disabled links.
    def get(self, code: str) -> Link:
        link = self.links.get(code)
        if link is None:
            raise LinkNotFound(f"no link '{code}'")
        return link

    # Build a privacy-safe click event and hand it to the asynchronous recorder.
    def record_click(self, link: Link, referrer: str | None, user_agent: str | None,
                     client_ip: str | None) -> None:
        ts = self.clock()
        self.recorder.record(ClickEvent(
            # Host only: full referrer URLs can contain personal data.
            code=link.code, ts=ts, referrer_host=referrer_host(referrer),
            # Coarse class only; the raw User-Agent string is not stored.
            ua_class=classify_user_agent(user_agent),
            # Daily-rotating salted hash, or None in "drop" mode.
            visitor=anonymize_ip(client_ip, self.settings.analytics_ip_mode,
                                 self.settings.ip_hash_salt, ts)))

    # Analytics for one link over the last `days` days.
    def stats(self, code: str, days: int = 30) -> dict:
        # 404 if the link doesn't exist.
        link = self.get(code)
        # Clamp the window to 1 day .. the retention period (older raw events are purged).
        days = max(1, min(days, self.settings.analytics_retention_days))
        now = self.clock()
        data = self.recorder.store.stats(code, now - days * DAY_S, now)
        return {"code": link.code, "window_days": days, **data}

    # ---------------------------------------------------------------- helpers
    # Work out the expiry timestamp from a requested TTL, or the configured default.
    def _expiry(self, ttl_seconds: object, now: float) -> float | None:
        # No TTL given: apply the default (0 days means never expire).
        if ttl_seconds is None:
            return now + self.settings.default_ttl_days * DAY_S if self.settings.default_ttl_days else None
        # bool is a subclass of int, so reject true/false explicitly.
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):
            raise InvalidTtl("ttl_seconds must be an integer")
        # At least a minute, at most max_ttl_days.
        if not 60 <= ttl_seconds <= self.settings.max_ttl_days * DAY_S:
            raise InvalidTtl(f"ttl_seconds must be between 60 and {self.settings.max_ttl_days * DAY_S}")
        return now + ttl_seconds

    # Read-through cache: check the cache, fall back to the repository, then cache the result.
    def _lookup(self, code: str) -> Link | None:
        if self.cache is None:
            return self.links.get(code)
        hit = self.cache.get(code)
        # A cached value, which may be None (a cached "not found").
        if not TTLCache.is_missing(hit):
            return hit  # type: ignore[return-value]
        link = self.links.get(code)
        # Existing links are cached longer; unknown codes only briefly (negative caching).
        ttl = self.settings.cache_ttl_s if link else self.settings.negative_cache_ttl_s
        self.cache.set(code, link, ttl)
        return link

    # Remove a code from the cache after any write that affects it.
    def _invalidate(self, code: str) -> None:
        if self.cache is not None:
            self.cache.invalidate(code)
