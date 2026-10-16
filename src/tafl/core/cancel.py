"""A cancel flag that can also stop work that never looks at it, such as a running DuckDB query."""

from __future__ import annotations

import threading
from collections.abc import Callable


class CancelEvent(threading.Event):
    """A ``threading.Event`` that calls back when it is set: ``when_set(con.interrupt)``."""

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._callbacks: list[Callable[[], None]] = []

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
