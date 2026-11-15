# Unit tests for ClickRecorder: clicks must be batched off the redirect path, a full
# queue must drop events rather than block, and store failures must not kill the flusher.
import threading
import time

from shortener.analytics import ClickRecorder, InMemoryAnalyticsStore
from shortener.domain import ClickEvent


# A minimal click event; `i` offsets the timestamp so events are distinct.
def _event(i: int = 0) -> ClickEvent:
    return ClickEvent("code1234", 1_700_000_000.0 + i, None, "desktop", None)


def test_async_recorder_batches_and_flushes():
    """Covers: AC-ANALYTICS, AC-HOTPATH"""
    store = InMemoryAnalyticsStore()
    # A 60 s interval means the background thread won't flush during the test.
    rec = ClickRecorder(store, batch_size=10, flush_interval_s=60)
    for i in range(25):
        rec.record(_event(i))
    assert len(store.events) == 0, "recording must not write inline"
    # close() drains the queue: 25 events in batches of 10, 10 and 5.
    rec.close()
    assert len(store.events) == 25 and rec.written == 25


def test_full_queue_drops_instead_of_blocking():
    """Covers: AC-HOTPATH"""
    # The queue holds only 5 events, and nothing flushes during the test.
    rec = ClickRecorder(InMemoryAnalyticsStore(), queue_size=5, flush_interval_s=60)
    start = time.perf_counter()
    for i in range(50):
        rec.record(_event(i))
    # 50 record() calls return almost instantly even though the queue is full...
    assert time.perf_counter() - start < 0.5
    # ...and the 45 that didn't fit are counted as dropped.
    assert rec.dropped == 45
    rec.close()


def test_store_failure_does_not_crash_flusher():
    """Covers: AC-HOTPATH"""
    # A store whose writes always fail.
    class Broken(InMemoryAnalyticsStore):
        def write_batch(self, events):
            raise RuntimeError("store down")

    rec = ClickRecorder(Broken(), flush_interval_s=60)
    rec.record(_event())
    # flush() must not raise; the failed batch is counted as dropped instead.
    rec.flush()
    assert rec.dropped == 1


def test_background_flush_interval():
    """Covers: AC-ANALYTICS"""
    store = InMemoryAnalyticsStore()
    # Short interval: the background thread should write the event on its own.
    rec = ClickRecorder(store, flush_interval_s=0.05)
    rec.record(_event())
    done = threading.Event()
    # Poll for up to about 2 seconds without calling flush() ourselves.
    for _ in range(40):
        if store.events:
            done.set()
            break
        time.sleep(0.05)
    rec.close()
    assert done.is_set()
