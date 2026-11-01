"""Python's collector of reference cycles runs on whichever thread happens to make new objects. On a
calculation thread it can delete an object of the window, and Qt allows that only on the object's own
thread: the delete holds a lock of Qt and waits for Python, the window's thread holds Python and
waits for that lock, and the program hangs. So the automatic collector stays off, and the window's
thread collects, from a timer. No widgets here."""

from __future__ import annotations

import gc

from PySide6.QtCore import QObject, QTimer

_EVERY_MS = 1000


class Collector(QObject):
    """Collects on the thread that makes it, by the limits Python would use, until ``stop``."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._was_on = gc.isenabled()
        gc.disable()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.collect)
        self._timer.start(_EVERY_MS)

    def collect(self) -> None:
        new, younger, _ = gc.get_count()
        limits = gc.get_threshold()
        if new >= limits[0]:
            gc.collect(2 if younger >= limits[1] else 0)

    def stop(self) -> None:
        self._timer.stop()
        if self._was_on:
            gc.enable()
