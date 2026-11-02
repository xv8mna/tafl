"""Running calculations in the background so the window never freezes."""

from __future__ import annotations

import threading
import traceback
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from tafl.api import Cancelled
from tafl.api.expressions import is_panic
from tafl.core.cancel import CancelEvent


class _Signals(QObject):
    done = Signal(int, object)
    failed = Signal(int, str)
    cancelled = Signal(int)


class _Count:
    """The jobs whose thread runs their work now, outdated ones too."""

    def __init__(self) -> None:
        self.value = 0
        self._lock = threading.Lock()

    def add(self, step: int) -> None:
        with self._lock:
            self.value += step


class _Job(QRunnable):
    def __init__(self, job_id: int, work: Callable[[], Any], signals: _Signals, running: _Count) -> None:
        super().__init__()
        self.job_id = job_id
        self.work = work
        self.signals = signals
        self.running = running

    def run(self) -> None:
        self.running.add(1)
        try:
            report, args = self._outcome()
        finally:
            self.running.add(-1)  # before the report: what it starts may ask whether a job still runs
        report.emit(*args)

    def _outcome(self) -> tuple[Any, tuple[Any, ...]]:
        try:
            result = self.work()
        except Cancelled:
            return self.signals.cancelled, (self.job_id,)
        except Exception:
            return self.signals.failed, (self.job_id, traceback.format_exc())
        except BaseException as e:
            # A crash inside Polars (Rust), or plugin code that tries to end the program
            # (sys.exit, KeyboardInterrupt): report it instead of going silent
            if not (is_panic(e) or isinstance(e, SystemExit | KeyboardInterrupt)):
                raise
            return self.signals.failed, (self.job_id, traceback.format_exc())
        return self.signals.done, (self.job_id, result)


class Worker(QObject):
    """Runs jobs in a background thread, one that matters at a time: starting a job cancels the
    previous one, and results of outdated jobs are dropped."""

    finished = Signal(object)  # result of the latest job
    failed = Signal(str)  # traceback of the latest job
    cancelled = Signal()  # the latest job stopped because it was cancelled
    busy_changed = Signal(bool)
    # After a report: no job runs its work any more, also no outdated one (a job that a slot of the
    # report started may not have its thread yet: it is busy)
    drained = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(2)  # a cancelled job may still be finishing its current step
        self._pool.setExpiryTimeout(-1)  # the threads stay: each keeps its DuckDB connection for formulas
        self._signals = _Signals(self)
        self._signals.done.connect(self._on_done)
        self._signals.failed.connect(self._on_failed)
        self._signals.cancelled.connect(self._on_cancelled)
        self._latest = 0
        self._busy = False
        self._cancel: CancelEvent | None = None
        self._running = _Count()

    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def running(self) -> bool:
        """A job runs its work in a thread, maybe an outdated one: a cancel takes effect only between
        steps. A job that start() has just given to the pool counts once its thread takes it."""
        return self._running.value > 0

    def start(self, work: Callable[[threading.Event], Any]) -> None:
        """Run ``work(cancel_event)`` in the background. The event is a CancelEvent, so the work
        can also have a callback run when it is cancelled."""
        self.cancel()
        self._pool.clear()  # jobs that are still waiting for a thread are outdated now
        cancel = CancelEvent()
        self._cancel = cancel
        self._latest += 1
        self._pool.start(_Job(self._latest, lambda: work(cancel), self._signals, self._running))
        self._set_busy(True)

    def cancel(self) -> None:
        if self._cancel is not None:
            self._cancel.set()

    def forget(self) -> None:
        """Cancel the latest job and take nothing more from it: a cancel only takes effect between
        steps, so a job in its last step would still report. Its thread ends in the background."""
        self.cancel()
        self._latest += 1
        self._set_busy(False)

    def wait(self, msecs: int = -1) -> bool:
        """Wait for all jobs (for tests and when closing the app)."""
        return self._pool.waitForDone(msecs)

    def _on_done(self, job_id: int, result: object) -> None:
        if job_id == self._latest:
            self._set_busy(False)
            self.finished.emit(result)
        self._ended()

    def _on_failed(self, job_id: int, text: str) -> None:
        if job_id == self._latest:
            self._set_busy(False)
            self.failed.emit(text)
        self._ended()

    def _on_cancelled(self, job_id: int) -> None:
        if job_id == self._latest:
            self._set_busy(False)
            self.cancelled.emit()
        self._ended()

    def _ended(self) -> None:
        if not self.running:
            self.drained.emit()

    def _set_busy(self, busy: bool) -> None:
        if busy != self._busy:
            self._busy = busy
            self.busy_changed.emit(busy)
