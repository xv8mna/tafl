"""A cancel flag that can also stop work that never looks at it, such as a running DuckDB query."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from tafl.api import expressions


class CancelEvent(threading.Event):
    """A ``threading.Event`` that calls back when it is set: ``when_set(con.interrupt)``."""

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._callbacks: list[Callable[[], None]] = []
        self._signalled = False

    def set_from_signal(self) -> None:
        """For a signal handler, which may interrupt this event's own methods while they hold a
        lock: takes no lock. ``is_set()`` is true at once; the rest of ``set()`` (the callbacks)
        is up to another thread, e.g. the one that watches for Ctrl+C."""
        self._signalled = True

    def is_set(self) -> bool:
        return self._signalled or super().is_set()

    def clear(self) -> None:
        self._signalled = False
        super().clear()

    def set(self) -> None:
        with self._lock:
            super().set()
            callbacks, self._callbacks = self._callbacks, []
        for callback in callbacks:
            callback()

    def when_set(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Call ``callback`` when the event is set (now, if it already is). Returns a function
        that removes it again, for when the work it would stop is over."""
        with self._lock:
            if not self.is_set():
                self._callbacks.append(callback)
                return lambda: self._remove(callback)
        callback()
        return lambda: None

    def _remove(self, callback: Callable[[], None]) -> None:
        with self._lock:
            if callback in self._callbacks:
                self._callbacks.remove(callback)


_blocks: dict[int, list[CancelEvent]] = {}  # thread id -> the events of the stopping_queries blocks on it


@contextmanager
def stopping_queries(cancel: threading.Event | None) -> Iterator[None]:
    """Setting ``cancel`` (a CancelEvent) while the block runs stops a query that runs in DuckDB
    on this thread, e.g. a formula, and makes ``stopping()`` true on this thread. DuckDB forgets
    an interrupt that comes before the query starts, so it is repeated until the block is over."""
    if not isinstance(cancel, CancelEvent):
        yield
        return
    thread = threading.get_ident()
    lock = threading.Lock()
    running = True

    def keep_interrupting() -> None:
        while True:
            with lock:  # never after the block is over: it would stop the next query on this thread
                if not running:
                    return
                if (con := expressions._connections.get(thread)) is not None:  # one that ran a formula
                    con.interrupt()
            time.sleep(0.05)

    _blocks.setdefault(thread, []).append(cancel)
    remove = cancel.when_set(lambda: threading.Thread(target=keep_interrupting, daemon=True).start())
    try:
        yield
    finally:
        remove()
        with lock:
            running = False
        events = _blocks[thread]
        events.remove(cancel)
        if not events:
            del _blocks[thread]


def stopping() -> bool:
    """Whether the work on this thread is being stopped: the event of a ``stopping_queries``
    block around it is set. A query that fails then was stopped; it has no error of its own."""
    return any(event.is_set() for event in _blocks.get(threading.get_ident(), ()))
