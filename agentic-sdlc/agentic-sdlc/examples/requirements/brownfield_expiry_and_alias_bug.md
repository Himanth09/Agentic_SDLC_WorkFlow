In the existing legacy-links service, add link expiration support: callers can pass an
optional TTL when creating a link, and expired links must stop redirecting. Also fix the bug
where creating a link with a custom alias that already exists silently overwrites the
original link.
