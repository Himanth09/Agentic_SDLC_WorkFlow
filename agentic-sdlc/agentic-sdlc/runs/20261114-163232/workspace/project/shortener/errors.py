"""Domain errors. Each maps to exactly one HTTP status and a stable error code
that is part of the public API contract (application/problem+json)."""
from __future__ import annotations


class ShortenerError(Exception):
    status = 500
    code = "internal_error"
    title = "Internal error"

    def __init__(self, detail: str = "") -> None:
        super().__init__(detail or self.title)
        self.detail = detail or self.title


class BadRequest(ShortenerError):
    status, code, title = 400, "bad_request", "Malformed request"


class Unauthorized(ShortenerError):
    status, code, title = 401, "unauthorized", "Missing or invalid API key"


class LinkNotFound(ShortenerError):
    status, code, title = 404, "not_found", "Link not found"


class AliasTaken(ShortenerError):
    status, code, title = 409, "alias_taken", "Alias already in use"


class LinkExpired(ShortenerError):
    status, code, title = 410, "link_expired", "Link has expired"


class PayloadTooLarge(ShortenerError):
    status, code, title = 413, "payload_too_large", "Request body too large"


class InvalidUrl(ShortenerError):
    status, code, title = 422, "invalid_url", "Target URL is not allowed"


class InvalidAlias(ShortenerError):
    status, code, title = 422, "invalid_alias", "Alias is not valid"


class InvalidTtl(ShortenerError):
    status, code, title = 422, "invalid_ttl", "TTL is not valid"


class RateLimited(ShortenerError):
    status, code, title = 429, "rate_limited", "Too many requests"

    def __init__(self, retry_after_s: int) -> None:
        super().__init__(f"retry after {retry_after_s}s")
        self.retry_after_s = retry_after_s


class CodeSpaceExhausted(ShortenerError):
    status, code, title = 503, "code_space_exhausted", "Could not allocate a unique code"


class DuplicateKey(Exception):
    """Internal signal from repositories: the code is already taken."""
