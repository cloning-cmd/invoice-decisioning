"""In-memory event fan-out for live runs (one process, one event loop).

The pipeline runs in a worker thread (a Starlette BackgroundTask over a sync
function) and publishes every stage transition here. Each SSE connection
subscribes with its own asyncio.Queue. A subscriber that connects mid-run is
handed the full history first, so a page refresh never shows an empty
timeline.

When a run publishes its terminal event it is dropped from the hub; later
connections are served from the database, which is already complete by then
because the runner emits only after persisting.

This is why the server runs with a single worker: the hub is not shared across
processes. A stated prototype limitation (ASSUMPTIONS.md), not a scaling plan.
"""
from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from typing import Any

TERMINAL_EVENTS = ("run_finished", "run_failed")

Event = tuple[str, dict[str, Any]]


@dataclass
class _Live:
    history: list[Event] = field(default_factory=list)
    queues: set[asyncio.Queue[Event]] = field(default_factory=set)


class RunHub:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: dict[str, _Live] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        """The loop that owns the subscriber queues; worker threads post into it."""
        self._loop = loop

    def open(self, run_id: str) -> None:
        """Register a run before its background task starts, so no event can be missed."""
        with self._lock:
            self._runs[run_id] = _Live()

    def is_live(self, run_id: str) -> bool:
        with self._lock:
            return run_id in self._runs

    def publish(self, run_id: str, event: str, payload: dict[str, Any]) -> None:
        """Called from the pipeline's worker thread."""
        if self._loop is None:
            raise RuntimeError("RunHub.bind() was not called before a run published")
        with self._lock:
            live = self._runs.get(run_id)
            if live is None:
                raise RuntimeError(f"run {run_id} published {event!r} but is not open in the hub")
            item = (event, payload)
            live.history.append(item)
            for q in live.queues:
                self._loop.call_soon_threadsafe(q.put_nowait, item)
            if event in TERMINAL_EVENTS:
                del self._runs[run_id]

    def subscribe(self, run_id: str) -> tuple[list[Event], asyncio.Queue[Event]] | None:
        """History so far plus a queue for what follows, or None if the run is not live."""
        with self._lock:
            live = self._runs.get(run_id)
            if live is None:
                return None
            q: asyncio.Queue[Event] = asyncio.Queue()
            live.queues.add(q)
            return list(live.history), q

    def unsubscribe(self, run_id: str, q: asyncio.Queue[Event]) -> None:
        with self._lock:
            live = self._runs.get(run_id)
            if live is not None:
                live.queues.discard(q)


hub = RunHub()
