"""SQLite persistence for links."""
import sqlite3
import threading

from .models import Link

SCHEMA = """
CREATE TABLE IF NOT EXISTS links (
    code TEXT PRIMARY KEY,
    target_url TEXT NOT NULL,
    created_at REAL NOT NULL,
    clicks INTEGER NOT NULL DEFAULT 0
)
"""


class LinkStore:
    def __init__(self, path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute(SCHEMA)
            self._conn.commit()

    def save(self, link: Link) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO links (code, target_url, created_at, clicks) "
                "VALUES (?, ?, ?, ?)",
                (link.code, link.target_url, link.created_at, link.clicks),
            )
            self._conn.commit()

    def get(self, code: str):
        with self._lock:
            row = self._conn.execute(
                "SELECT code, target_url, created_at, clicks FROM links WHERE code = ?",
                (code,),
            ).fetchone()
        if row is None:
            return None
        return Link(code=row[0], target_url=row[1], created_at=row[2], clicks=row[3])

    def increment_clicks(self, code: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE links SET clicks = clicks + 1 WHERE code = ?", (code,))
            self._conn.commit()
