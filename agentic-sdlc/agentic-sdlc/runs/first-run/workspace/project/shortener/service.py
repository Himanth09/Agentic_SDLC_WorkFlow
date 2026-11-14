"""Application service: the only place business rules live.

HTTP, storage, cache and analytics are injected, so the same service runs
against SQLite in development and DynamoDB/ElastiCache/Kinesis in production.
"""
from __future__ import annotations

import time
from typing import Callable

from .analytics import ClickRecorder, anonymize_ip
from .cache import TTLCache
from .codes import CodeGenerator
from .config import Settings
from .domain import ClickEvent, Link, classify_user_agent, referrer_host, validate_alias, validate_url
from .errors import (AliasTaken, CodeSpaceExhausted, DuplicateKey, InvalidAlias, InvalidTtl,
                     LinkExpired, LinkNotFound)
from .repository import LinkRepository

MAX_CODE_ATTEMPTS = 5
DAY_S = 86_400


class LinkService:
    def __init__(self, settings: Settings, links: LinkRepository, recorder: ClickRecorder,
                 cache: TTLCache | None = None, codes: CodeGenerator | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.settings = settings
        self.links = links
        self.recorder = recorder
        self.cache = cache
        self.codes = codes or CodeGenerator(settings.code_length)
        self.clock = clock
        self.collisions = 0

    # ----------------------------------------------------------------- writes
    def create(self, target_url: object, alias: object = None, ttl_seconds: object = None,
               owner: str | None = None) -> Link:
        url = validate_url(target_url, self.settings.max_url_length)
        now = self.clock()
        expires_at = self._expiry(ttl_seconds, now)
        if alias is not None:
            if not self.settings.allow_custom_alias:
                raise InvalidAlias("custom aliases are disabled")
            code = validate_alias(alias)
            link = Link(code, url, now, expires_at, owner, is_custom=True)
            try:
                self.links.insert(link)
            except DuplicateKey:
                raise AliasTaken(f"alias '{code}' is already in use") from None
            self._invalidate(code)
            return link
        for _ in range(MAX_CODE_ATTEMPTS):
            link = Link(self.codes.next(), url, now, expires_at, owner)
            try:
                self.links.insert(link)
            except DuplicateKey:
                self.collisions += 1
                continue
            self._invalidate(link.code)
            return link
        raise CodeSpaceExhausted(f"no free code after {MAX_CODE_ATTEMPTS} attempts")

    def disable(self, code: str, owner: str | None) -> None:
        link = self.links.get(code)
        # Same response for "missing" and "not yours": do not leak existence.
        if link is None or link.disabled or (link.owner and link.owner != owner):
            raise LinkNotFound(f"no link '{code}'")
        self.links.disable(code)
        self._invalidate(code)

    # ------------------------------------------------------------------ reads
    def resolve(self, code: str) -> Link:
        """Hot path: cache -> repository. Enforces disabled/expired state."""
        link = self._lookup(code)
        if link is None or link.disabled:
            raise LinkNotFound(f"no link '{code}'")
        if link.is_expired(self.clock()):
            raise LinkExpired(f"link '{code}' expired")
        return link

    def get(self, code: str) -> Link:
        link = self.links.get(code)
        if link is None:
            raise LinkNotFound(f"no link '{code}'")
        return link

    def record_click(self, link: Link, referrer: str | None, user_agent: str | None,
                     client_ip: str | None) -> None:
        ts = self.clock()
        self.recorder.record(ClickEvent(
            code=link.code, ts=ts, referrer_host=referrer_host(referrer),
            ua_class=classify_user_agent(user_agent),
            visitor=anonymize_ip(client_ip, self.settings.analytics_ip_mode,
                                 self.settings.ip_hash_salt, ts)))

    def stats(self, code: str, days: int = 30) -> dict:
        link = self.get(code)
        days = max(1, min(days, self.settings.analytics_retention_days))
        now = self.clock()
        data = self.recorder.store.stats(code, now - days * DAY_S, now)
        return {"code": link.code, "window_days": days, **data}

    # ---------------------------------------------------------------- helpers
    def _expiry(self, ttl_seconds: object, now: float) -> float | None:
        if ttl_seconds is None:
            return now + self.settings.default_ttl_days * DAY_S if self.settings.default_ttl_days else None
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):
            raise InvalidTtl("ttl_seconds must be an integer")
        if not 60 <= ttl_seconds <= self.settings.max_ttl_days * DAY_S:
            raise InvalidTtl(f"ttl_seconds must be between 60 and {self.settings.max_ttl_days * DAY_S}")
        return now + ttl_seconds

    def _lookup(self, code: str) -> Link | None:
        if self.cache is None:
            return self.links.get(code)
        hit = self.cache.get(code)
        if not TTLCache.is_missing(hit):
            return hit  # type: ignore[return-value]
        link = self.links.get(code)
        ttl = self.settings.cache_ttl_s if link else self.settings.negative_cache_ttl_s
        self.cache.set(code, link, ttl)
        return link

    def _invalidate(self, code: str) -> None:
        if self.cache is not None:
            self.cache.invalidate(code)
