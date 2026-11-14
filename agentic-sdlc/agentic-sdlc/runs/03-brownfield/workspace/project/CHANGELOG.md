# Changelog

## Unreleased (2026-11-12)

### Fixed
- Fix: custom alias silently overwrites an existing link
  - Creating a link with an alias that already exists fails (AliasTaken / HTTP 409) and the original link is unchanged; random-code collisions are retried instead of overwriting

### Added
- Feature: optional link expiration (TTL)
  - POST /links accepts an optional positive integer ttl_seconds; an expired link returns 410 Gone
  - Links created without ttl_seconds never expire (backward compatible API and behaviour)
  - Existing databases without an expires_at column are migrated automatically and idempotently

### Migration
- `links.expires_at REAL` is added automatically on start-up (idempotent `ALTER TABLE`); no downtime, existing links never expire.
