"""Checking formulas in the background, for the line under each formula field and the formula
window: many fields at once, each with one check that matters."""

from __future__ import annotations

import traceback
from collections.abc import Callable
from typing import Any

import shiboken6
from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal
from PySide6.QtWidgets import QApplication

from tafl.api import Cancelled
from tafl.api.expressions import is_panic
from tafl.core.cancel import CancelEvent

_checks: Checks | None = None


def checks() -> Checks:
    """The one check service. It belongs to the application, not to a window: closing a window
    or rebuilding a form never waits for a check that is still running (it is stopped)."""
    global _checks
    if _checks is None or not shiboken6.isValid(_checks):
        _checks = Checks(QApplication.instance())
    return _checks


class Client:
    """One field (or window) of the service: ``start`` stops the check before it, and only the
    latest check reports, to ``done(result)`` or ``failed(traceback)``."""

    def __init__(self, service: Checks, done: Callable[[Any], None], failed: Callable[[str], None]) -> None:
        self._service = service
        self.done, self.failed = done, failed
        self.version = 0
        self.busy = False
        self.alive = True
        self._cancel: CancelEvent | None = None

    def start(self, work: Callable[[CancelEvent], Any]) -> None:
        """Run ``work(cancel)`` in the background."""
        self.cancel()
        self.version += 1
        self._cancel = CancelEvent()
        self.busy = True
        self._service._run(self, self.version, work, self._cancel)

    def cancel(self) -> None:
        if self._cancel is not None:
            self._cancel.set()
            self._cancel = None
        self.busy = False

    def close(self) -> None:
        """Stop for good: nothing is reported any more, and the field is let go."""
        self.cancel()
        self.alive = False
        self.done = self.failed = _ignore
        self._service._forget(self)


def _ignore(*_args: Any) -> None:
    pass


class _Signals(QObject):
    done = Signal(object, int, object)
    failed = Signal(object, int, str)
    cancelled = Signal(object, int)


class _Job(QRunnable):
    def __init__(self, client: Client, version: int, work: Callable[[CancelEvent], Any], cancel: CancelEvent, signals: _Signals) -> None:
        super().__init__()
        self.client, self.version, self.work, self.cancel, self.signals = client, version, work, cancel, signals

    def run(self) -> None:
        try:
            if self.cancel.is_set():  # a newer check came while this one waited for a thread
                raise Cancelled()
            result = self.work(self.cancel)
        except Cancelled:
            self.signals.cancelled.emit(self.client, self.version)
        except Exception:
            self.signals.failed.emit(self.client, self.version, traceback.format_exc())
        except BaseException as e:
            # A crash inside Polars (Rust), or plugin code that tries to end the program
            # (sys.exit, KeyboardInterrupt): report it instead of going silent
            if not (is_panic(e) or isinstance(e, SystemExit | KeyboardInterrupt)):
                raise
            self.signals.failed.emit(self.client, self.version, traceback.format_exc())
        else:
            self.signals.done.emit(self.client, self.version, result)


class Checks(QObject):
    """Runs the checks of all clients on a few background threads."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(4)
        self._pool.setExpiryTimeout(-1)  # the threads stay: each keeps its DuckDB connection for formulas
        self._signals = _Signals(self)
        self._signals.done.connect(self._done)
        self._signals.failed.connect(self._failed)
        self._signals.cancelled.connect(self._cancelled)
        self._clients: list[Client] = []
        self._running = 0  # jobs that haven't reported yet
        self._started = 0  # newer checks start first: slow old ones never hold up the field being typed in

    @property
    def busy(self) -> bool:
        return self._running > 0

    def client(self, done: Callable[[Any], None], failed: Callable[[str], None] = lambda _details: None) -> Client:
        client = Client(self, done, failed)
        self._clients.append(client)
        return client

    def stop_all(self) -> None:
        """Stop every check (e.g. when the app closes, so it doesn't wait for them)."""
        for client in self._clients:
            client.cancel()

    def wait(self, msecs: int = -1) -> bool:
        """Wait for all checks (for tests)."""
        return self._pool.waitForDone(msecs)

    def _run(self, client: Client, version: int, work: Callable[[CancelEvent], Any], cancel: CancelEvent) -> None:
        self._running += 1
        self._started += 1
        self._pool.start(_Job(client, version, work, cancel, self._signals), self._started)

    def _forget(self, client: Client) -> None:
        if client in self._clients:
            self._clients.remove(client)

    def _done(self, client: Client, version: int, result: object) -> None:
        self._running -= 1
        if self._latest(client, version):
            client.done(result)

    def _failed(self, client: Client, version: int, details: str) -> None:
        self._running -= 1
        if self._latest(client, version):
            client.failed(details)

    def _cancelled(self, client: Client, version: int) -> None:
        self._running -= 1
        self._latest(client, version)

    @staticmethod
    def _latest(client: Client, version: int) -> bool:
        if not client.alive or version != client.version or client._cancel is None:  # outdated or stopped
            return False
        client.busy = False
        return True
