"""Link persistence behind a port (hexagonal architecture).

`LinkRepository` is the port; SQLite and in-memory are adapters. A DynamoDB
adapter implements the same three methods with a conditional put
(`attribute_not_exists(code)`) for the uniqueness guarantee.
"""
from __future__ import annotations

# sqlite3: the development/production-lite database.
import sqlite3
# threading: per-thread connections and locks.
import threading
from pathlib import Path
# Protocol: the port that every storage adapter implements.
from typing import Protocol

from .domain import Link
# Raised when an insert would reuse an existing code.
from .errors import DuplicateKey

# schema.sql ships next to this module and is applied on startup.
SCHEMA_PATH = Path(__file__).with_name("schema.sql")


# The storage interface the service depends on (it never sees SQL).
class LinkRepository(Protocol):
    def insert(self, link: Link) -> None:
        """Insert atomically; raise DuplicateKey if the code exists. Never overwrites."""

    # Fetch a link by code, or None if there isn't one.
    def get(self, code: str) -> Link | None: ...

    # Mark a link disabled; returns False if the code doesn't exist.
    def disable(self, code: str) -> bool: ...


# Dict-backed adapter used by unit tests.
class InMemoryLinkRepository:
    def __init__(self) -> None:
        self._links: dict[str, Link] = {}
        self._lock = threading.Lock()

    def insert(self, link: Link) -> None:
        # Check-and-insert under one lock, so concurrent inserts of the same code can't both succeed.
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
            # Link is frozen: store a copy with disabled=True.
            self._links[code] = Link(**{**link.__dict__, "disabled": True})
            return True


class SQLiteDatabase:
    """One connection per thread; WAL so readers never block the writer."""

    def __init__(self, path: str) -> None:
        # Database file path, or ":memory:" for tests.
        self.path = path
        # Holds each thread's own connection.
        self._local = threading.local()
        self._init_lock = threading.Lock()
        # Create tables and indexes if they don't exist (the schema is idempotent).
        with self._init_lock:
            conn = self.conn()
            conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
            conn.commit()

    # The calling thread's connection, created and configured on first use.
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            # isolation_level=None: autocommit; multi-statement writes use explicit BEGIN/COMMIT.
            conn = sqlite3.connect(self.path, timeout=10, isolation_level=None,
                                   check_same_thread=False)
            # Write-ahead logging lets readers run while a write is in progress.
            if self.path != ":memory:":
                conn.execute("PRAGMA journal_mode=WAL")
            # Wait up to 10 s for a lock instead of failing immediately.
            conn.execute("PRAGMA busy_timeout=10000")
            # Safe with WAL and much faster than FULL.
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn


# SQLite adapter for the LinkRepository port.
class SQLiteLinkRepository:
    def __init__(self, db: SQLiteDatabase) -> None:
        self.db = db

    def insert(self, link: Link) -> None:
        try:
            # Plain INSERT (never "insert or replace"): an existing code must fail, not be overwritten.
            self.db.conn().execute(
                "INSERT INTO links (code, target_url, created_at, expires_at, owner, "
                "is_custom, disabled) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (link.code, link.target_url, link.created_at, link.expires_at, link.owner,
                 int(link.is_custom), int(link.disabled)))
        # The PRIMARY KEY constraint is the uniqueness guarantee, even across processes.
        except sqlite3.IntegrityError as exc:
            raise DuplicateKey(link.code) from exc

    def get(self, code: str) -> Link | None:
        # Column list must match the links table in schema.sql.
        row = self.db.conn().execute(
            "SELECT code, target_url, created_at, expires_at, owner, is_custom, disabled "
            "FROM links WHERE code = ?", (code,)).fetchone()
        if row is None:
            return None
        # SQLite stores booleans as 0/1 integers.
        return Link(code=row[0], target_url=row[1], created_at=row[2], expires_at=row[3],
                    owner=row[4], is_custom=bool(row[5]), disabled=bool(row[6]))

    def disable(self, code: str) -> bool:
        # Soft delete: the row stays, so the code is never reissued to someone else.
        cur = self.db.conn().execute("UPDATE links SET disabled = 1 WHERE code = ?", (code,))
        return cur.rowcount > 0
