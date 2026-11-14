"""Domain model and input validation (pure functions, no I/O)."""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from .errors import InvalidAlias, InvalidUrl

ALIAS_RE = re.compile(r"^[A-Za-z0-9_-]{4,32}$")
RESERVED_ALIASES = frozenset({"api", "healthz", "readyz", "metrics", "admin", "static", "docs"})
_BLOCKED_HOSTS = frozenset({"localhost", "localhost.localdomain"})


@dataclass(frozen=True)
class Link:
    code: str
    target_url: str
    created_at: float
    expires_at: float | None = None
    owner: str | None = None
    is_custom: bool = False
    disabled: bool = False

    def is_expired(self, now: float) -> bool:
        return self.expires_at is not None and now >= self.expires_at


@dataclass(frozen=True)
class ClickEvent:
    code: str
    ts: float
    referrer_host: str | None
    ua_class: str
    visitor: str | None  # anonymised visitor id (salted hash) or None


def validate_url(raw: object, max_length: int) -> str:
    """Accept only absolute http(s) URLs to public hosts.

    Rejects: other schemes (javascript:, data:, file:), embedded credentials
    (classic phishing vector), localhost / private / link-local IP literals
    (prevents the service being used to bounce users to internal endpoints).
    Returns a normalised URL (lower-case scheme and host)."""
    if not isinstance(raw, str) or not raw.strip():
        raise InvalidUrl("url must be a non-empty string")
    url = raw.strip()
    if len(url) > max_length:
        raise InvalidUrl(f"url longer than {max_length} characters")
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https"):
        raise InvalidUrl("only http and https URLs are allowed")
    if not parts.hostname:
        raise InvalidUrl("url must include a host")
    if parts.username or parts.password:
        raise InvalidUrl("urls with embedded credentials are not allowed")
    host = parts.hostname.lower()
    if host in _BLOCKED_HOSTS or host.endswith(".localhost"):
        raise InvalidUrl("local hosts are not allowed")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and (ip.is_private or ip.is_loopback or ip.is_link_local
                           or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
        raise InvalidUrl("private or reserved IP addresses are not allowed")
    netloc = host if parts.port is None else f"{host}:{parts.port}"
    if ip is not None and ip.version == 6:
        netloc = f"[{host}]" if parts.port is None else f"[{host}]:{parts.port}"
    return urlunsplit((parts.scheme.lower(), netloc, parts.path or "/", parts.query,
                       parts.fragment))


def validate_alias(alias: object) -> str:
    if not isinstance(alias, str) or not ALIAS_RE.fullmatch(alias):
        raise InvalidAlias("alias must be 4-32 characters of [A-Za-z0-9_-]")
    if alias.lower() in RESERVED_ALIASES:
        raise InvalidAlias(f"'{alias}' is reserved")
    return alias


_BOT_HINTS = ("bot", "crawler", "spider", "slurp", "preview", "curl", "wget", "python-requests", "python-urllib")
_MOBILE_HINTS = ("mobile", "android", "iphone", "ipad")


def classify_user_agent(ua: str | None) -> str:
    if not ua:
        return "unknown"
    low = ua.lower()
    if any(h in low for h in _BOT_HINTS):
        return "bot"
    if any(h in low for h in _MOBILE_HINTS):
        return "mobile"
    return "desktop"


def referrer_host(referrer: str | None) -> str | None:
    """Keep only the referring host: full referrer URLs can carry PII in query strings."""
    if not referrer:
        return None
    host = urlsplit(referrer).hostname
    return host.lower() if host else None
