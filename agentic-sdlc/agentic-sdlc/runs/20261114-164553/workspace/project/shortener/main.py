"""Composition root and development server.

    python -m shortener.main            # serves on :8080 using SQLite

Production: run the app factory under a WSGI server
(gunicorn -w 4 --threads 8 'shortener.main:wsgi()') behind a load balancer, with adapters swapped for
DynamoDB / ElastiCache / Kinesis (see docs/architecture.md).
"""
from __future__ import annotations

import logging
import os
import signal
import sys
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIServer, make_server

from .analytics import ClickRecorder, SQLiteAnalyticsStore
from .cache import TTLCache
from .config import Settings
from .http import ShortenerApp, create_app
from .ratelimit import TokenBucketLimiter
from .repository import SQLiteDatabase, SQLiteLinkRepository
from .service import LinkService


def build_app(settings: Settings) -> ShortenerApp:
    db = SQLiteDatabase(settings.db_path)
    recorder = ClickRecorder(SQLiteAnalyticsStore(db), settings.analytics_batch_size,
                             settings.analytics_flush_interval_s, settings.analytics_queue_size)
    service = LinkService(settings, SQLiteLinkRepository(db), recorder,
                          cache=TTLCache(settings.cache_size))
    return create_app(service, settings, TokenBucketLimiter(settings.create_rate_per_min))


def wsgi() -> ShortenerApp:
    """App factory for external WSGI servers."""
    return build_app(Settings.from_env())


class _ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings.from_env()
    app = build_app(settings)
    host = os.environ.get("HOST", "127.0.0.1")   # set HOST=0.0.0.0 inside a container
    port = int(os.environ.get("PORT", "8080"))
    server = make_server(host, port, app, server_class=_ThreadingWSGIServer)

    def shutdown(*_: object) -> None:
        app.service.recorder.close()  # flush buffered analytics before exit
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    logging.info("listening on %s:%d (db=%s)", host, port, settings.db_path)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
