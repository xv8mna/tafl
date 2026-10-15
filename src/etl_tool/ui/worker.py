"""Running calculations in the background so the window never freezes."""

from __future__ import annotations

import threading
import traceback
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from etl_tool.api import Cancelled


class _Signals(QObject):
    done = Signal(int, object)
    failed = Signal(int, str)
    cancelled = Signal(int)


class _Job(QRunnable):
    def __init__(self, job_id: int, work: Callable[[], Any], signals: _Signals) -> None:
        super().__init__()
        self.job_id = job_id
        self.work = work
        self.signals = signals

    def run(self) -> None:
        try:
            result = self.work()
        except Cancelled:
            self.signals.cancelled.emit(self.job_id)
        except Exception:
            self.signals.failed.emit(self.job_id, traceback.format_exc())
        else:
            self.signals.done.emit(self.job_id, result)


class Worker(QObject):
    """Runs jobs in a background thread, one that matters at a time: starting a job cancels the
    previous one, and results of outdated jobs are dropped."""

    finished = Signal(object)  # result of the latest job
    failed = Signal(str)  # traceback of the latest job
    busy_changed = Signal(bool)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(2)  # a cancelled job may still be finishing its current step
        self._signals = _Signals(self)
        self._signals.done.connect(self._on_done)
        self._signals.failed.connect(self._on_failed)
        self._signals.cancelled.connect(self._on_cancelled)
        self._latest = 0
        self._busy = False
        self._cancel: threading.Event | None = None

    @property
    def busy(self) -> bool:
        return self._busy

    def start(self, work: Callable[[threading.Event], Any]) -> None:
        """Run ``work(cancel_event)`` in the background."""
        self.cancel()
        cancel = threading.Event()
        self._cancel = cancel
        self._latest += 1
        self._pool.start(_Job(self._latest, lambda: work(cancel), self._signals))
        self._set_busy(True)

    def cancel(self) -> None:
        if self._cancel is not None:
            self._cancel.set()

    def wait(self, msecs: int = -1) -> bool:
        """Wait for all jobs (for tests and when closing the app)."""
        return self._pool.waitForDone(msecs)

    def _on_done(self, job_id: int, result: object) -> None:
        if job_id == self._latest:
            self._set_busy(False)
            self.finished.emit(result)

    def _on_failed(self, job_id: int, text: str) -> None:
        if job_id == self._latest:
            self._set_busy(False)
            self.failed.emit(text)

    def _on_cancelled(self, job_id: int) -> None:
        if job_id == self._latest:
            self._set_busy(False)

    def _set_busy(self, busy: bool) -> None:
        if busy != self._busy:
            self._busy = busy
            self.busy_changed.emit(busy)
