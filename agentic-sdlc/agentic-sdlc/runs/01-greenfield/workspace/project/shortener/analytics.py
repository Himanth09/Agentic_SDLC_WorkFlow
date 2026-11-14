"""Click analytics: asynchronous, batched, privacy-preserving.

The redirect path only *enqueues* an event (O(1), never blocks). A background
flusher writes batches: raw events (bounded retention) plus pre-aggregated
daily counters, so stats queries never scan raw events for totals.
If the queue is full, events are dropped and counted (`dropped`) - analytics
must never degrade redirect availability. Production equivalent: Kinesis /
Firehose -> S3 + Athena for raw events, DynamoDB atomic counters for daily totals.
"""
from __future__ import annotations

import hashlib
import logging
import queue
import sqlite3
import threading
from collections import Counter
from datetime import datetime, timezone
from typing import Protocol

from .domain import ClickEvent
from .repository import SQLiteDatabase

log = logging.getLogger(__name__)


def utc_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def anonymize_ip(ip: str | None, mode: str, salt: str, ts: float) -> str | None:
    """`hash`: salted SHA-256 that rotates daily - counts unique visitors per day
    without storing IPs and without enabling cross-day tracking. `drop`: nothing."""
    if mode == "drop" or not ip:
        return None
    digest = hashlib.sha256(f"{salt}|{utc_day(ts)}|{ip}".encode()).hexdigest()
    return digest[:16]


class AnalyticsStore(Protocol):
    def write_batch(self, events: list[ClickEvent]) -> None: ...

    def stats(self, code: str, since_ts: float, now: float) -> dict: ...

    def purge_before(self, ts: float) -> int: ...


class SQLiteAnalyticsStore:
    def __init__(self, db: SQLiteDatabase) -> None:
        self.db = db

    def write_batch(self, events: list[ClickEvent]) -> None:
        if not events:
            return
        daily = Counter((e.code, utc_day(e.ts)) for e in events)
        conn = self.db.conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.executemany(
                "INSERT INTO click_events (code, ts, referrer_host, ua_class, visitor) "
                "VALUES (?, ?, ?, ?, ?)",
                [(e.code, e.ts, e.referrer_host, e.ua_class, e.visitor) for e in events])
            conn.executemany(
                "INSERT INTO click_daily (code, day, clicks) VALUES (?, ?, ?) "
                "ON CONFLICT (code, day) DO UPDATE SET clicks = clicks + excluded.clicks",
                [(code, day, n) for (code, day), n in daily.items()])
            conn.execute("COMMIT")
        except sqlite3.Error:
            conn.execute("ROLLBACK")
            raise

    def stats(self, code: str, since_ts: float, now: float) -> dict:
        conn = self.db.conn()
        total = conn.execute("SELECT COALESCE(SUM(clicks), 0) FROM click_daily WHERE code = ?",
                             (code,)).fetchone()[0]
        by_day = conn.execute(
            "SELECT day, clicks FROM click_daily WHERE code = ? AND day >= ? ORDER BY day",
            (code, utc_day(since_ts))).fetchall()
        referrers = conn.execute(
            "SELECT COALESCE(referrer_host, '(direct)'), COUNT(*) AS n FROM click_events "
            "WHERE code = ? AND ts >= ? GROUP BY 1 ORDER BY n DESC, 1 LIMIT 10",
            (code, since_ts)).fetchall()
        agents = conn.execute(
            "SELECT ua_class, COUNT(*) FROM click_events WHERE code = ? AND ts >= ? "
            "GROUP BY 1 ORDER BY 1", (code, since_ts)).fetchall()
        uniques = conn.execute(
            "SELECT COUNT(DISTINCT visitor) FROM click_events "
            "WHERE code = ? AND ts >= ? AND visitor IS NOT NULL", (code, since_ts)).fetchone()[0]
        return {
            "total_clicks": int(total),
            "clicks_by_day": [{"day": d, "clicks": int(c)} for d, c in by_day],
            "top_referrers": [{"host": h, "clicks": int(n)} for h, n in referrers],
            "user_agents": {k: int(v) for k, v in agents},
            "unique_visitors": int(uniques),
        }

    def purge_before(self, ts: float) -> int:
        cur = self.db.conn().execute("DELETE FROM click_events WHERE ts < ?", (ts,))
        return cur.rowcount


class InMemoryAnalyticsStore:
    def __init__(self) -> None:
        self.events: list[ClickEvent] = []
        self._lock = threading.Lock()

    def write_batch(self, events: list[ClickEvent]) -> None:
        with self._lock:
            self.events.extend(events)

    def stats(self, code: str, since_ts: float, now: float) -> dict:
        with self._lock:
            mine = [e for e in self.events if e.code == code]
        recent = [e for e in mine if e.ts >= since_ts]
        days = Counter(utc_day(e.ts) for e in recent)
        refs = Counter(e.referrer_host or "(direct)" for e in recent)
        return {
            "total_clicks": len(mine),
            "clicks_by_day": [{"day": d, "clicks": n} for d, n in sorted(days.items())],
            "top_referrers": [{"host": h, "clicks": n}
                              for h, n in sorted(refs.items(), key=lambda kv: (-kv[1], kv[0]))[:10]],
            "user_agents": dict(sorted(Counter(e.ua_class for e in recent).items())),
            "unique_visitors": len({e.visitor for e in recent if e.visitor}),
        }

    def purge_before(self, ts: float) -> int:
        with self._lock:
            before = len(self.events)
            self.events = [e for e in self.events if e.ts >= ts]
            return before - len(self.events)


class ClickRecorder:
    """Bounded queue + background batch flusher. `synchronous=True` writes inline
    (deterministic tests)."""

    def __init__(self, store: AnalyticsStore, batch_size: int = 500,
                 flush_interval_s: float = 1.0, queue_size: int = 100_000,
                 synchronous: bool = False) -> None:
        self.store = store
        self.batch_size = batch_size
        self.flush_interval_s = flush_interval_s
        self.synchronous = synchronous
        self.dropped = 0
        self.written = 0
        self._q: queue.Queue[ClickEvent] = queue.Queue(maxsize=queue_size)
        self._stop = threading.Event()
        self._flush_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        if not synchronous:
            self._thread = threading.Thread(target=self._loop, name="click-flusher", daemon=True)
            self._thread.start()

    def record(self, event: ClickEvent) -> None:
        if self.synchronous:
            self.store.write_batch([event])
            self.written += 1
            return
        try:
            self._q.put_nowait(event)
        except queue.Full:
            self.dropped += 1

    def flush(self) -> int:
        with self._flush_lock:
            batch: list[ClickEvent] = []
            while True:
                try:
                    batch.append(self._q.get_nowait())
                except queue.Empty:
                    break
                if len(batch) >= self.batch_size:
                    self._write(batch)
                    batch = []
            self._write(batch)
            return self.written

    def _write(self, batch: list[ClickEvent]) -> None:
        if not batch:
            return
        try:
            self.store.write_batch(batch)
            self.written += len(batch)
        except Exception:  # noqa: BLE001 - never let analytics crash the flusher
            log.exception("failed to write %d click events; dropping batch", len(batch))
            self.dropped += len(batch)

    def _loop(self) -> None:
        while not self._stop.wait(self.flush_interval_s):
            self.flush()

    def close(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self.flush()

    @property
    def backlog(self) -> int:
        return self._q.qsize()
