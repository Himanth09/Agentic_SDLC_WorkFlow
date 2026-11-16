"""Shared fixtures (helpers live in tests/support.py)."""
from __future__ import annotations

# replace: copy the frozen Settings with test-specific values.
from dataclasses import replace

import pytest

# Real components only: tests use the in-memory and SQLite adapters, no mocks.
from shortener.analytics import ClickRecorder, InMemoryAnalyticsStore, SQLiteAnalyticsStore
from shortener.cache import TTLCache
from shortener.config import Settings
from shortener.http import create_app
from shortener.ratelimit import TokenBucketLimiter
from shortener.repository import InMemoryLinkRepository, SQLiteDatabase, SQLiteLinkRepository
from shortener.service import LinkService

from tests.support import API_KEY, FakeClock


# Controllable time shared by the service, cache and rate limiter in a test.
@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


# Generated defaults plus: one known API key, a fixed salt (repeatable visitor hashes),
# and a high create limit so ordinary tests never hit the rate limiter.
@pytest.fixture
def settings() -> Settings:
    return replace(Settings(), api_keys=frozenset({API_KEY}), ip_hash_salt="test-salt",
                   create_rate_per_min=1000)


# Headers that authenticate as the test API key.
@pytest.fixture
def auth() -> dict[str, str]:
    return {"X-API-Key": API_KEY}


# Fast service for unit tests: in-memory storage, synchronous analytics, fake clock.
@pytest.fixture
def memory_service(settings: Settings, clock: FakeClock) -> LinkService:
    # synchronous=True writes clicks immediately, so assertions don't race a background thread.
    recorder = ClickRecorder(InMemoryAnalyticsStore(), synchronous=True)
    return LinkService(settings, InMemoryLinkRepository(), recorder,
                       cache=TTLCache(1000, clock=clock), clock=clock)


# A fresh SQLite file per test (pytest's tmp_path), so tests never share data.
@pytest.fixture
def sqlite_db(tmp_path) -> SQLiteDatabase:
    return SQLiteDatabase(str(tmp_path / "links.db"))


# Service on real SQLite storage, for persistence and integration tests.
@pytest.fixture
def sqlite_service(settings: Settings, clock: FakeClock, sqlite_db: SQLiteDatabase) -> LinkService:
    recorder = ClickRecorder(SQLiteAnalyticsStore(sqlite_db), synchronous=True)
    return LinkService(settings, SQLiteLinkRepository(sqlite_db), recorder,
                       cache=TTLCache(1000, clock=clock), clock=clock)


# The full WSGI app over SQLite, driven through tests.support.call().
@pytest.fixture
def app(sqlite_service: LinkService, settings: Settings, clock: FakeClock):
    return create_app(sqlite_service, settings,
                      TokenBucketLimiter(settings.create_rate_per_min, clock=clock))
