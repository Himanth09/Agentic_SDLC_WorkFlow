"""Shared fixtures (helpers live in tests/support.py)."""
from __future__ import annotations

from dataclasses import replace

import pytest

from shortener.analytics import ClickRecorder, InMemoryAnalyticsStore, SQLiteAnalyticsStore
from shortener.cache import TTLCache
from shortener.config import Settings
from shortener.http import create_app
from shortener.ratelimit import TokenBucketLimiter
from shortener.repository import InMemoryLinkRepository, SQLiteDatabase, SQLiteLinkRepository
from shortener.service import LinkService

from tests.support import API_KEY, FakeClock


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def settings() -> Settings:
    return replace(Settings(), api_keys=frozenset({API_KEY}), ip_hash_salt="test-salt",
                   create_rate_per_min=1000)


@pytest.fixture
def auth() -> dict[str, str]:
    return {"X-API-Key": API_KEY}


@pytest.fixture
def memory_service(settings: Settings, clock: FakeClock) -> LinkService:
    recorder = ClickRecorder(InMemoryAnalyticsStore(), synchronous=True)
    return LinkService(settings, InMemoryLinkRepository(), recorder,
                       cache=TTLCache(1000, clock=clock), clock=clock)


@pytest.fixture
def sqlite_db(tmp_path) -> SQLiteDatabase:
    return SQLiteDatabase(str(tmp_path / "links.db"))


@pytest.fixture
def sqlite_service(settings: Settings, clock: FakeClock, sqlite_db: SQLiteDatabase) -> LinkService:
    recorder = ClickRecorder(SQLiteAnalyticsStore(sqlite_db), synchronous=True)
    return LinkService(settings, SQLiteLinkRepository(sqlite_db), recorder,
                       cache=TTLCache(1000, clock=clock), clock=clock)


@pytest.fixture
def app(sqlite_service: LinkService, settings: Settings, clock: FakeClock):
    return create_app(sqlite_service, settings,
                      TokenBucketLimiter(settings.create_rate_per_min, clock=clock))
