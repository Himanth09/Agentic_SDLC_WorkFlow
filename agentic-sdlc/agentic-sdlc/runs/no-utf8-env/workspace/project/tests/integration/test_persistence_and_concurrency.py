"""Durability across restarts and correctness under concurrent writers."""
import threading

from shortener.analytics import ClickRecorder, SQLiteAnalyticsStore
from shortener.errors import AliasTaken
from shortener.repository import SQLiteDatabase, SQLiteLinkRepository
from shortener.service import LinkService


def _service(path, settings, clock=None):
    db = SQLiteDatabase(path)
    kwargs = {"clock": clock} if clock else {}
    return LinkService(settings, SQLiteLinkRepository(db),
                       ClickRecorder(SQLiteAnalyticsStore(db), synchronous=True), **kwargs)


def test_links_and_clicks_survive_restart(tmp_path, settings):
    """Covers: AC-PERSIST, AC-ANALYTICS"""
    path = str(tmp_path / "durable.db")
    first = _service(path, settings)
    link = first.create("https://example.com/keep")
    first.record_click(link, None, "Mozilla/5.0", "198.51.100.9")

    restarted = _service(path, settings)          # new process, same database
    assert restarted.resolve(link.code).target_url == "https://example.com/keep"
    assert restarted.stats(link.code)["total_clicks"] == 1


def test_concurrent_creates_produce_unique_codes(tmp_path, settings):
    """Covers: AC-CONCURRENCY, AC-PERSIST"""
    svc = _service(str(tmp_path / "concurrent.db"), settings)
    codes: list[str] = []
    lock = threading.Lock()

    def worker(n: int) -> None:
        for i in range(40):
            code = svc.create(f"https://example.com/{n}/{i}").code
            with lock:
                codes.append(code)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(codes) == 320 and len(set(codes)) == 320
    assert all(svc.resolve(c) for c in codes)


def test_concurrent_alias_race_has_exactly_one_winner(tmp_path, settings):
    """Covers: AC-ALIAS, AC-CONCURRENCY"""
    if not settings.allow_custom_alias:
        return
    svc = _service(str(tmp_path / "race.db"), settings)
    results: list[str] = []
    barrier = threading.Barrier(6)

    def worker(n: int) -> None:
        barrier.wait()
        try:
            svc.create(f"https://example.com/{n}", alias="race-alias")
            results.append(f"won:{n}")
        except AliasTaken:
            results.append("lost")

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    winners = [r for r in results if r.startswith("won")]
    assert len(winners) == 1 and results.count("lost") == 5
    n = winners[0].split(":")[1]
    assert svc.resolve("race-alias").target_url == f"https://example.com/{n}"


def test_retention_purge(tmp_path, settings):
    """Covers: AC-PRIVACY"""
    db = SQLiteDatabase(str(tmp_path / "purge.db"))
    store = SQLiteAnalyticsStore(db)
    from shortener.domain import ClickEvent
    store.write_batch([ClickEvent("c0de123", 100.0, None, "bot", None),
                       ClickEvent("c0de123", 10_000.0, None, "bot", None)])
    assert store.purge_before(5_000.0) == 1
    # daily aggregates are retained after raw events are purged
    assert store.stats("c0de123", 0, 20_000.0)["total_clicks"] == 2
