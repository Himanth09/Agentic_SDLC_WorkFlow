"""Link persistence behind a port (hexagonal architecture).

`LinkRepository` is the port; SQLite and in-memory are adapters. A DynamoDB
adapter implements the same three methods with a conditional put
(`attribute_not_exists(code)`) for the uniqueness guarantee.
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Protocol

from .domain import Link
from .errors import DuplicateKey

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


class LinkRepository(Protocol):
    def insert(self, link: Link) -> None:
        """Insert atomically; raise DuplicateKey if the code exists. Never overwrites."""

    def get(self, code: str) -> Link | None: ...

    def disable(self, code: str) -> bool: ...


class InMemoryLinkRepository:
    def __init__(self) -> None:
        self._links: dict[str, Link] = {}
        self._lock = threading.Lock()

    def insert(self, link: Link) -> None:
        with self._lock:
            if link.code in self._links:
                raise DuplicateKey(link.code)
            self._links[link.code] = link

    def get(self, code: str) -> Link | None:
        with self._lock:
            return self._links.get(code)

    def disable(self, code: str) -> bool:
        with self._lock:
            link = self._links.get(code)
            if link is None:
                return False
            self._links[code] = Link(**{**link.__dict__, "disabled": True})
            return True


class SQLiteDatabase:
    """One connection per thread; WAL so readers never block the writer."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._local = threading.local()
        self._init_lock = threading.Lock()
        with self._init_lock:
            conn = self.conn()
            conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
            conn.commit()

    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=10, isolation_level=None,
                                   check_same_thread=False)
            if self.path != ":memory:":
                conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn


class SQLiteLinkRepository:
    def __init__(self, db: SQLiteDatabase) -> None:
        self.db = db

    def insert(self, link: Link) -> None:
        try:
            self.db.conn().execute(
                "INSERT INTO links (code, target_url, created_at, expires_at, owner, "
                "is_custom, disabled) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (link.code, link.target_url, link.created_at, link.expires_at, link.owner,
                 int(link.is_custom), int(link.disabled)))
        except sqlite3.IntegrityError as exc:
            raise DuplicateKey(link.code) from exc

    def get(self, code: str) -> Link | None:
        row = self.db.conn().execute(
            "SELECT code, target_url, created_at, expires_at, owner, is_custom, disabled "
            "FROM links WHERE code = ?", (code,)).fetchone()
        if row is None:
            return None
        return Link(code=row[0], target_url=row[1], created_at=row[2], expires_at=row[3],
                    owner=row[4], is_custom=bool(row[5]), disabled=bool(row[6]))

    def disable(self, code: str) -> bool:
        cur = self.db.conn().execute("UPDATE links SET disabled = 1 WHERE code = ?", (code,))
        return cur.rowcount > 0
