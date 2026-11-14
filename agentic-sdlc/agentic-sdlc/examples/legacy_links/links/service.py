"""Business logic for creating and resolving links."""
import secrets
import string
import time

from .models import Link
from .store import LinkStore

ALPHABET = string.ascii_letters + string.digits


class NotFound(Exception):
    pass


class LinkService:
    def __init__(self, store: LinkStore, clock=time.time) -> None:
        self.store = store
        self.clock = clock

    def shorten(self, url: str, alias: str | None = None) -> Link:
        if not url.startswith(("http://", "https://")):
            raise ValueError("url must start with http:// or https://")
        code = alias or "".join(secrets.choice(ALPHABET) for _ in range(6))
        link = Link(code=code, target_url=url, created_at=self.clock())
        self.store.save(link)
        return link

    def resolve(self, code: str) -> str:
        link = self.store.get(code)
        if link is None:
            raise NotFound(code)
        self.store.increment_clicks(code)
        return link.target_url

    def info(self, code: str) -> Link:
        link = self.store.get(code)
        if link is None:
            raise NotFound(code)
        return link
