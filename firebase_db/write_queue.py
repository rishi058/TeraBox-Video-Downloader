"""
firebase_db/write_queue.py
~~~~~~~~~~~~~~~~~~~~~~~~~~
Rate-limited write queue for Firestore operations.

Why this exists
---------------
Firestore enforces a hard limit of **1 sustained write/second per document**.
Bursting above that triggers RESOURCE_EXHAUSTED (429) errors, which can also
contribute to IP-level throttling on shared cloud platforms.

How it works
------------
- A single background daemon thread drains a FIFO queue of write callables.
- Writes are spaced at least `_MIN_INTERVAL` seconds apart (default 1.1 s —
  slightly over 1 s to give Firestore headroom).
- Callers can choose:
    • enqueue_wait()     — submit & block until done  (use for set_user_mode)
    • enqueue_nowait()   — fire-and-forget            (use for track_user)
- On RESOURCE_EXHAUSTED the worker retries with exponential back-off
  (up to MAX_RETRIES attempts) before logging the failure and moving on.
- Queue depth is capped at MAX_QUEUE_SIZE; overflow items are dropped with a
  warning rather than blocking the bot indefinitely.

Thread-safety
-------------
queue.Queue is inherently thread-safe. The worker thread is a daemon so it
is killed automatically when the main process exits.
"""

import logging
import queue
import threading
import time
from typing import Callable, Any

from google.api_core.exceptions import ResourceExhausted, ServiceUnavailable

log = logging.getLogger(__name__)

# ── Tunables ───────────────────────────────────────────────────────────────────
_MIN_INTERVAL   = 1.1     # seconds between consecutive Firestore writes
_MAX_RETRIES    = 4       # exponential back-off retries on 429 / 503
_BASE_BACKOFF   = 1.0     # seconds — doubles on each retry
_MAX_QUEUE_SIZE = 200     # drop writes that overflow this cap


# ── Internal helpers ───────────────────────────────────────────────────────────

class _WriteTask:
    """Wraps a single write callable with optional completion signalling."""

    __slots__ = ("fn", "args", "kwargs", "event", "result", "error")

    def __init__(self, fn: Callable, args: tuple, kwargs: dict, *, wait: bool):
        self.fn     = fn
        self.args   = args
        self.kwargs = kwargs
        self.event  = threading.Event() if wait else None
        self.result: Any = None
        self.error:  Exception | None = None

    def run(self) -> None:
        delay = _BASE_BACKOFF
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                self.result = self.fn(*self.args, **self.kwargs)
                return  # success
            except (ResourceExhausted, ServiceUnavailable) as e:
                if attempt == _MAX_RETRIES:
                    log.error(f"[WriteQueue] Giving up after {_MAX_RETRIES} retries: {e}")
                    self.error = e
                    return
                log.warning(f"[WriteQueue] {type(e).__name__} on attempt {attempt}, "
                            f"retrying in {delay:.1f}s…")
                time.sleep(delay)
                delay *= 2
            except Exception as e:
                log.error(f"[WriteQueue] Unexpected error: {e}")
                self.error = e
                return

    def signal(self) -> None:
        if self.event is not None:
            self.event.set()


# ── Queue singleton ────────────────────────────────────────────────────────────

class FirestoreWriteQueue:
    """
    Singleton background-thread write queue.

    Usage::

        from firebase_db.write_queue import write_queue

        # Blocking — returns True on success, False on error
        ok = write_queue.enqueue_wait(ref.set, data, merge=True)

        # Non-blocking fire-and-forget
        write_queue.enqueue_nowait(ref.update, {"last_active": t})
    """

    def __init__(self) -> None:
        self._q: queue.Queue[_WriteTask] = queue.Queue(maxsize=_MAX_QUEUE_SIZE)
        self._last_write: float = 0.0
        self._thread = threading.Thread(
            target=self._worker,
            name="firestore-write-queue",
            daemon=True,
        )
        self._thread.start()
        log.info("[WriteQueue] Background write thread started "
                 f"(rate ≤ {1/_MIN_INTERVAL:.2f} writes/s)")

    # ── Public interface ───────────────────────────────────────────────────────

    def enqueue_wait(self, fn: Callable, *args, **kwargs) -> bool:
        """
        Submit a write and **block** until it completes (or fails).

        Returns True on success, False on error.
        Use this when the caller needs to know if the write succeeded
        (e.g. set_user_mode → reply to user).
        """
        task = _WriteTask(fn, args, kwargs, wait=True)
        try:
            self._q.put_nowait(task)
        except queue.Full:
            log.warning("[WriteQueue] Queue full — dropping blocking write")
            return False
        task.event.wait()           # block caller until worker signals completion
        return task.error is None

    def enqueue_nowait(self, fn: Callable, *args, **kwargs) -> None:
        """
        Submit a fire-and-forget write. Returns immediately.

        Use this for best-effort tracking (track_user, add_to_cache) where
        the caller does not need to wait for the write to complete.
        """
        task = _WriteTask(fn, args, kwargs, wait=False)
        try:
            self._q.put_nowait(task)
        except queue.Full:
            log.warning("[WriteQueue] Queue full — dropping fire-and-forget write")

    @property
    def pending(self) -> int:
        """Number of writes currently waiting in the queue."""
        return self._q.qsize()

    # ── Worker ─────────────────────────────────────────────────────────────────

    def _worker(self) -> None:
        while True:
            try:
                task = self._q.get(timeout=2.0)
            except queue.Empty:
                continue

            # ── Rate limiting ──────────────────────────────────────────────────
            elapsed = time.monotonic() - self._last_write
            if elapsed < _MIN_INTERVAL:
                time.sleep(_MIN_INTERVAL - elapsed)

            # ── Execute ────────────────────────────────────────────────────────
            task.run()
            self._last_write = time.monotonic()
            task.signal()           # unblock enqueue_wait callers
            self._q.task_done()


# ── Module-level singleton ─────────────────────────────────────────────────────
write_queue = FirestoreWriteQueue()
