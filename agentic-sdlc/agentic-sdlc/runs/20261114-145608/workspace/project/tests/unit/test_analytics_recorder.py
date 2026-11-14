import threading
import time

from shortener.analytics import ClickRecorder, InMemoryAnalyticsStore
from shortener.domain import ClickEvent


def _event(i: int = 0) -> ClickEvent:
    return ClickEvent("code1234", 1_700_000_000.0 + i, None, "desktop", None)


def test_async_recorder_batches_and_flushes():
    """Covers: AC-ANALYTICS, AC-HOTPATH"""
    store = InMemoryAnalyticsStore()
    rec = ClickRecorder(store, batch_size=10, flush_interval_s=60)
    for i in range(25):
        rec.record(_event(i))
    assert len(store.events) == 0, "recording must not write inline"
    rec.close()
    assert len(store.events) == 25 and rec.written == 25


def test_full_queue_drops_instead_of_blocking():
    """Covers: AC-HOTPATH"""
    rec = ClickRecorder(InMemoryAnalyticsStore(), queue_size=5, flush_interval_s=60)
    start = time.perf_counter()
    for i in range(50):
        rec.record(_event(i))
    assert time.perf_counter() - start < 0.5
    assert rec.dropped == 45
    rec.close()


def test_store_failure_does_not_crash_flusher():
    """Covers: AC-HOTPATH"""
    class Broken(InMemoryAnalyticsStore):
        def write_batch(self, events):
            raise RuntimeError("store down")

    rec = ClickRecorder(Broken(), flush_interval_s=60)
    rec.record(_event())
    rec.flush()
    assert rec.dropped == 1


def test_background_flush_interval():
    """Covers: AC-ANALYTICS"""
    store = InMemoryAnalyticsStore()
    rec = ClickRecorder(store, flush_interval_s=0.05)
    rec.record(_event())
    done = threading.Event()
    for _ in range(40):
        if store.events:
            done.set()
            break
        time.sleep(0.05)
    rec.close()
    assert done.is_set()
