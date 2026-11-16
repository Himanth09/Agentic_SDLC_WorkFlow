"""Domain errors. Each maps to exactly one HTTP status and a stable error code
that is part of the public API contract (application/problem+json)."""
from __future__ import annotations


# Base class. The HTTP layer turns any subclass into a problem+json response using
# its `status`, `code` and `title` class attributes.
class ShortenerError(Exception):
    # Defaults: an unexpected error is a 500 with a generic message.
    status = 500
    code = "internal_error"
    title = "Internal error"

    # `detail` is the human-readable specifics; it falls back to the title.
    def __init__(self, detail: str = "") -> None:
        super().__init__(detail or self.title)
        self.detail = detail or self.title


# 400: the request itself is malformed (bad JSON, wrong content type, unknown fields).
class BadRequest(ShortenerError):
    status, code, title = 400, "bad_request", "Malformed request"


# 401: API key missing or not recognised (only when keys are required).
class Unauthorized(ShortenerError):
    status, code, title = 401, "unauthorized", "Missing or invalid API key"


# 404: no link with this code (or it was deleted).
class LinkNotFound(ShortenerError):
    status, code, title = 404, "not_found", "Link not found"


# 409: the requested custom alias already exists.
class AliasTaken(ShortenerError):
    status, code, title = 409, "alias_taken", "Alias already in use"


# 410: the link existed but its expiry time has passed.
class LinkExpired(ShortenerError):
    status, code, title = 410, "link_expired", "Link has expired"


# 413: request body over the configured size limit.
class PayloadTooLarge(ShortenerError):
    status, code, title = 413, "payload_too_large", "Request body too large"


# 422: target URL failed validation (scheme, host, credentials, private IP...).
class InvalidUrl(ShortenerError):
    status, code, title = 422, "invalid_url", "Target URL is not allowed"


# 422: custom alias has the wrong format or is reserved.
class InvalidAlias(ShortenerError):
    status, code, title = 422, "invalid_alias", "Alias is not valid"


# 422: requested expiry is not a positive integer or exceeds the maximum.
class InvalidTtl(ShortenerError):
    status, code, title = 422, "invalid_ttl", "TTL is not valid"


# 429: create rate limit exceeded; carries how long the client should wait.
class RateLimited(ShortenerError):
    status, code, title = 429, "rate_limited", "Too many requests"

    def __init__(self, retry_after_s: int) -> None:
        super().__init__(f"retry after {retry_after_s}s")
        # Sent to the client as the Retry-After header.
        self.retry_after_s = retry_after_s


# 503: repeated code collisions; the service could not find a free code.
class CodeSpaceExhausted(ShortenerError):
    status, code, title = 503, "code_space_exhausted", "Could not allocate a unique code"


# Not a ShortenerError: it never reaches clients. The service catches it and either
# retries with a new code or converts it to AliasTaken.
class DuplicateKey(Exception):
    """Internal signal from repositories: the code is already taken."""
