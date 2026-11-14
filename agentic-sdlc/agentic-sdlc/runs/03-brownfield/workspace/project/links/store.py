"""SQLite persistence for links."""
import sqlite3
import threading

from .models import Link


class DuplicateCode(Exception):
    """Raised when a code/alias already exists."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS links (
    code TEXT PRIMARY KEY,
    target_url TEXT NOT NULL,
    created_at REAL NOT NULL,
    clicks INTEGER NOT NULL DEFAULT 0,
    expires_at REAL
)
"""


class LinkStore:
    def __init__(self, path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute(SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """Backward-compatible, idempotent migration for pre-existing databases."""
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(links)")}
        if "expires_at" not in cols:
            self._conn.execute("ALTER TABLE links ADD COLUMN expires_at REAL")

    def save(self, link: Link) -> None:
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO links (code, target_url, created_at, clicks, expires_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (link.code, link.target_url, link.created_at, link.clicks, link.expires_at),
                )
            except sqlite3.IntegrityError as exc:
                raise DuplicateCode(link.code) from exc
            self._conn.commit()

    def get(self, code: str):
        with self._lock:
            row = self._conn.execute(
                "SELECT code, target_url, created_at, clicks, expires_at FROM links WHERE code = ?",
                (code,),
            ).fetchone()
        if row is None:
            return None
        return Link(code=row[0], target_url=row[1], created_at=row[2], clicks=row[3], expires_at=row[4])

    def increment_clicks(self, code: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE links SET clicks = clicks + 1 WHERE code = ?", (code,))
            self._conn.commit()
