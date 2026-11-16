"""Composition root and development server.

    python -m shortener.main            # serves on :8080 using SQLite

Production: run the app factory under a WSGI server
(gunicorn -w 4 --threads 8 'shortener.main:wsgi()') behind a load balancer, with adapters swapped for
DynamoDB / ElastiCache / Kinesis (see docs/architecture.md).
"""
from __future__ import annotations

import logging
# os: HOST and PORT environment variables.
import os
# signal/sys: graceful shutdown on SIGTERM (e.g. from a container orchestrator).
import signal
import sys
# ThreadingMixIn: handle each request on its own thread.
from socketserver import ThreadingMixIn
# Standard-library WSGI server, good enough for development.
from wsgiref.simple_server import WSGIServer, make_server

# Every component the app is wired from.
from .analytics import ClickRecorder, SQLiteAnalyticsStore
from .cache import TTLCache
from .config import Settings
from .http import ShortenerApp, create_app
from .ratelimit import TokenBucketLimiter
from .repository import SQLiteDatabase, SQLiteLinkRepository
from .service import LinkService


# The one place where concrete adapters are chosen. Swapping SQLite for a cloud
# backend means changing only this function.
def build_app(settings: Settings) -> ShortenerApp:
    # One database file holds both links and analytics tables.
    db = SQLiteDatabase(settings.db_path)
    # Asynchronous, batched analytics writer.
    recorder = ClickRecorder(SQLiteAnalyticsStore(db), settings.analytics_batch_size,
                             settings.analytics_flush_interval_s, settings.analytics_queue_size)
    # Business logic, with a read-through cache in front of the repository.
    service = LinkService(settings, SQLiteLinkRepository(db), recorder,
                          cache=TTLCache(settings.cache_size))
    # HTTP layer with the create-rate limiter.
    return create_app(service, settings, TokenBucketLimiter(settings.create_rate_per_min))


def wsgi() -> ShortenerApp:
    """App factory for external WSGI servers."""
    return build_app(Settings.from_env())


# Threaded WSGI server; daemon threads don't block process exit.
class _ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


# Development entry point: configure logging, build the app and serve until stopped.
def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # Reads SHORTENER_* overrides and fails fast on an invalid configuration.
    settings = Settings.from_env()
    app = build_app(settings)
    host = os.environ.get("HOST", "127.0.0.1")   # set HOST=0.0.0.0 inside a container
    port = int(os.environ.get("PORT", "8080"))
    server = make_server(host, port, app, server_class=_ThreadingWSGIServer)

    # Stop cleanly so queued click events aren't lost.
    def shutdown(*_: object) -> None:
        app.service.recorder.close()  # flush buffered analytics before exit
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    logging.info("listening on %s:%d (db=%s)", host, port, settings.db_path)
    try:
        server.serve_forever()
    # Ctrl+C in a terminal takes the same graceful path.
    except KeyboardInterrupt:
        shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
