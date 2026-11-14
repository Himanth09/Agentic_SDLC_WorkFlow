"""Run context: the shared blackboard, tracer and fault injector.

Agents never talk to each other directly. They read typed inputs from the
blackboard and publish typed outputs back to it; the orchestrator owns the
schedule. Every write records its producer so the summary can show provenance.
"""
# Store annotations as strings.
from __future__ import annotations

# json: read and write trace events (JSON Lines).
import json
# threading: locks, because tasks run in parallel threads.
import threading
# time: timestamps relative to the start of the run.
import time
# dataclass/field: the FaultInjector container.
from dataclasses import dataclass, field
# Path: location of the trace file.
from pathlib import Path
# Any/Callable: types for stored values and update functions.
from typing import Any, Callable

# to_jsonable: turn dataclasses/enums into plain JSON-compatible values.
from .models import to_jsonable


# Thread-safe key/value store shared by all agents, with per-key provenance.
class Blackboard:
    # Start empty.
    def __init__(self) -> None:
        # The actual key -> value data.
        self._data: dict[str, Any] = {}
        # key -> ordered list of producers that wrote it.
        self._provenance: dict[str, list[str]] = {}
        # Re-entrant lock (snapshot() calls provenance() while already holding it).
        self._lock = threading.RLock()

    # Set a key (overwriting) and record who wrote it.
    def put(self, key: str, value: Any, producer: str) -> None:
        with self._lock:
            self._data[key] = value
            prov = self._provenance.setdefault(key, [])
            # Don't record the same producer twice in a row.
            if not prov or prov[-1] != producer:
                prov.append(producer)

    # Read a key, or return the default if missing.
    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    # Read a key that must exist; a missing input is a programming/plan error.
    def require(self, key: str) -> Any:
        with self._lock:
            if key not in self._data:
                raise KeyError(f"blackboard is missing required input '{key}'")
            return self._data[key]

    # Extend a list-valued key (created if missing), e.g. "artifacts".
    def append(self, key: str, items: list[Any], producer: str) -> None:
        with self._lock:
            self._data.setdefault(key, []).extend(items)
            self._provenance.setdefault(key, []).append(producer)

    # Replace a key with fn(old value) under the lock.
    def update(self, key: str, fn: Callable[[Any], Any], producer: str) -> Any:
        """Atomic read-modify-write (parallel tasks contribute to shared keys)."""
        with self._lock:
            # fn receives None when the key doesn't exist yet.
            value = fn(self._data.get(key))
            self._data[key] = value
            self._provenance.setdefault(key, []).append(producer)
            return value

    # Sorted list of all keys.
    def keys(self) -> list[str]:
        with self._lock:
            return sorted(self._data)

    # Copy of the provenance map (copied so callers can't mutate internal state).
    def provenance(self) -> dict[str, list[str]]:
        with self._lock:
            return {k: list(v) for k, v in self._provenance.items()}

    # checkpoint support
    # Shallow copy of all data plus provenance, for checkpointing.
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"data": dict(self._data), "provenance": self.provenance()}

    # Replace the contents with a previously taken snapshot (resume).
    def restore(self, snap: dict[str, Any]) -> None:
        with self._lock:
            self._data = dict(snap["data"])
            self._provenance = {k: list(v) for k, v in snap["provenance"].items()}


# Records every orchestration event to trace.jsonl and (optionally) the console.
class Tracer:
    """Append-only JSONL execution trace (the audit log)."""

    # Open (or continue) the trace at `path`.
    def __init__(self, path: Path, echo: bool = True) -> None:
        # Trace file location.
        self.path = path
        # Whether to also print each event.
        self.echo = echo
        # Serialises writes from parallel threads.
        self._lock = threading.Lock()
        # Run start time; event "t" values are seconds since this.
        self.t0 = time.time()
        # In-memory copy of all events (used by the Reporter).
        self.events: list[dict[str, Any]] = []
        if path.exists():   # resumed run: keep one continuous trace and clock
            # Load the earlier events.
            self.events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
            # Shift t0 back so new timestamps continue from the last recorded one.
            if self.events:
                self.t0 -= self.events[-1]["t"]

    # Record one event with arbitrary extra fields.
    def emit(self, event: str, **fields: Any) -> None:
        # Event record: relative time, name, and JSON-safe fields.
        rec = {"t": round(time.time() - self.t0, 3), "event": event, **to_jsonable(fields)}
        with self._lock:
            self.events.append(rec)
            # Append one JSON line to the file.
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, default=str) + "\n")
            # Print a one-line human-readable version.
            if self.echo:
                print(_fmt(rec), flush=True)


# Console icon for each event type.
_ICON = {
    "run_started": "▶", "task_started": "·", "task_succeeded": "✔", "task_failed": "✘",
    "task_retry": "↻", "task_skipped": "⤼", "task_degraded": "!", "repair_cycle": "⟲",
    "gate_requested": "⏸", "gate_decided": "☑", "plan_extended": "+", "run_finished": "■",
    "guardrail": "⛨", "checkpoint": "⎘", "fault_injected": "⚡",
}


# Format an event as "[  12.34s] ✔ task_succeeded  key=value ...".
def _fmt(rec: dict[str, Any]) -> str:
    # Icon for the event ("-" if unknown).
    icon = _ICON.get(rec["event"], "-")
    # Every field except the time and event name.
    extras = {k: v for k, v in rec.items() if k not in ("t", "event")}
    # Only scalar fields are shown on the console (lists/dicts are too long).
    detail = " ".join(f"{k}={v}" for k, v in extras.items() if not isinstance(v, (list, dict)))
    return f"[{rec['t']:7.2f}s] {icon} {rec['event']:<15} {detail}"


# Fires configured faults once each, to demonstrate retries and repair cycles.
@dataclass
class FaultInjector:
    """Deterministic fault injection used to *demonstrate* recovery paths.

    spec examples: "transient:design_contract", "bug:gen_api"
    Each fault fires once.
    """
    # Configured faults as "kind:task_id" strings.
    specs: list[str] = field(default_factory=list)
    # Faults that have already fired.
    _fired: set[str] = field(default_factory=set)
    # Protects _fired across parallel tasks.
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # True exactly once if a fault of `kind` is configured for `task_id`.
    def take(self, kind: str, task_id: str) -> bool:
        key = f"{kind}:{task_id}"
        with self._lock:
            # Configured and not yet fired: mark it fired and trigger it.
            if key in self.specs and key not in self._fired:
                self._fired.add(key)
                return True
        return False
