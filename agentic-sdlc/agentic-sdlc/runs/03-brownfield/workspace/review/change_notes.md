# Change notes for reviewers

Requirement: In the existing legacy-links service, add link expiration support: callers can pass an
optional TTL when creating a link, and expired links must stop redirecting. Also fix the bug
where creating a link with a custom alias that already exists silently overwrites the
original link.

## What changed and why
### Fix: custom alias silently overwrites an existing link
Touches roles: store, service, http -> `links/store.py`, `links/service.py`, `links/app.py`

### Feature: optional link expiration (TTL)
Touches roles: model, store, service, http -> `links/models.py`, `links/store.py`, `links/service.py`, `links/app.py`

## API impact
- `POST /links`: optional `ttl_seconds`; response adds `expires_at`; taken alias now returns `409 Conflict` instead of silently overwriting
- `GET /{code}`: may return `410 Gone` for expired links

## Rollout
1. Deploy (migration runs on start-up, backward compatible).
2. Clients that relied on alias overwrite will now receive 409 - communicate this behaviour change.
3. Rollback: previous version ignores the extra column.
