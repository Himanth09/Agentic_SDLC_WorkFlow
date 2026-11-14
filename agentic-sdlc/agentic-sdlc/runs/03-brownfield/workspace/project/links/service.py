"""Business logic for creating and resolving links."""
import secrets
import string
import time

from .models import Link
from .store import DuplicateCode, LinkStore

ALPHABET = string.ascii_letters + string.digits


class NotFound(Exception):
    pass


class Expired(Exception):
    """The link exists but its TTL has elapsed."""


class AliasTaken(Exception):
    """The requested alias is already in use."""


class LinkService:
    def __init__(self, store: LinkStore, clock=time.time) -> None:
        self.store = store
        self.clock = clock

    def shorten(self, url: str, alias: str | None = None,
                ttl_seconds: int | None = None) -> Link:
        if not url.startswith(("http://", "https://")):
            raise ValueError("url must start with http:// or https://")
        now = self.clock()
        if ttl_seconds is not None and (
                isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int)
                or ttl_seconds <= 0):
            raise ValueError("ttl_seconds must be a positive integer")
        expires_at = now + ttl_seconds if ttl_seconds is not None else None
        if alias:
            link = Link(code=alias, target_url=url, created_at=now, expires_at=expires_at)
            try:
                self.store.save(link)
            except DuplicateCode:
                raise AliasTaken(alias) from None
            return link
        for _ in range(5):  # random codes: retry on the rare collision
            code = "".join(secrets.choice(ALPHABET) for _ in range(6))
            link = Link(code=code, target_url=url, created_at=now, expires_at=expires_at)
            try:
                self.store.save(link)
                return link
            except DuplicateCode:
                continue
        raise RuntimeError("could not allocate a unique code")

    def resolve(self, code: str) -> str:
        link = self.store.get(code)
        if link is None:
            raise NotFound(code)
        if link.expires_at is not None and self.clock() >= link.expires_at:
            raise Expired(code)
        self.store.increment_clicks(code)
        return link.target_url

    def info(self, code: str) -> Link:
        link = self.store.get(code)
        if link is None:
            raise NotFound(code)
        return link
