import logging
import sys

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from tafl import APP_FOLDER, APP_NAME, COMMAND
from tafl.core import Engine
from tafl.ui import app as app_module
from tafl.ui.main_window import MainWindow
from tafl.ui.session import Session

BROKEN, PROBLEM = '{"format": "tafl-project", "tables": 5}', '"tables" is 5 instead of a list'


@pytest.fixture
def start(qapp, monkeypatch, tmp_path):
    """start(*arguments) runs the entry point the desktop shortcut uses and returns its exit code
    and the window (QApplication.exec returns at once unless a test replaces it). Without the
    plugins of this computer: a broken one would change the exit code."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.delenv("TAFL_PLUGINS", raising=False)
    monkeypatch.setattr(QApplication, "exec", lambda self: 0)
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    handlers = list(logging.getLogger().handlers)

    def run(*arguments):
        code = app_module.main(["tafl-app", *map(str, arguments)])
        return code, [w for w in QApplication.topLevelWidgets() if isinstance(w, MainWindow) and w.isVisible()][-1]

    yield run
    for window in [w for w in QApplication.topLevelWidgets() if isinstance(w, MainWindow)]:
        window.session._set_dirty(False)
        window.close()
        window.session.wait()
    for handler in set(logging.getLogger().handlers) - set(handlers):  # the log file of main()
        logging.getLogger().removeHandler(handler)
        handler.close()


def test_app_starts_and_opens_a_project(start, tmp_path, demo):
    code, window = start(demo)
    assert code == 0 and window.session.project.name == "Demo"
    assert (tmp_path / APP_FOLDER / f"{COMMAND}.log").exists()


def test_open_dialogs_offer_project_files():
    from tafl.ui.main_window import _FILTER

    assert _FILTER == f"{APP_NAME} projects (*.tafl);;All files (*.*)"


def test_a_damaged_project_shows_a_message_and_the_window_stays_usable(window, monkeypatch, tmp_path, demo):
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text, *rest: warnings.append(text))
    broken = tmp_path / "broken.tafl"
    broken.write_text(BROKEN, encoding="utf-8")
    assert not window.open_path(broken)
    assert warnings == [f"Could not open {broken}:\n\nThe project file broken.tafl is damaged: {PROBLEM}"]
    assert window.open_path(demo) and window.session.project.name == "Demo"


def test_a_damaged_project_given_at_the_start_leaves_the_app_open(start, monkeypatch, tmp_path):
    """E.g. a project file dropped onto the program."""
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text, *rest: warnings.append(text))
    broken = tmp_path / "broken.tafl"
    broken.write_text(BROKEN, encoding="utf-8")
    code, window = start(broken)
    assert code == 0 and window.session.project.path is None
    assert len(warnings) == 1 and "broken.tafl is damaged" in warnings[0]


def test_an_unexpected_error_while_opening_at_the_start_leaves_the_app_open(start, monkeypatch, demo):
    shown = []
    monkeypatch.setattr(QMessageBox, "exec", lambda box: shown.append(box.text()))
    monkeypatch.setattr(MainWindow, "open_path", lambda self, path: 1 / 0)
    code, window = start(demo)
    assert code == 0 and window.isVisible()
    assert shown == ["ZeroDivisionError: division by zero"]


@pytest.mark.parametrize("error, code", [(None, 0), ("while opening", 4), ("later", 4), ("in a background task", 4)])
def test_the_startup_check_fails_after_an_unexpected_error(start, monkeypatch, qtbot, demo, error, code):
    """The build starts the app with TAFL_STARTUP_CHECK and expects 0. An unexpected error gives
    4 instead, and only goes to the log: nobody is there to click a dialog away."""
    monkeypatch.setenv("TAFL_STARTUP_CHECK", "1")
    codes, shown = [], []
    monkeypatch.setattr(QApplication, "exit", staticmethod(codes.append))
    monkeypatch.setattr(QApplication, "exec", lambda self: qtbot.waitUntil(lambda: bool(codes), timeout=60_000))
    monkeypatch.setattr(QMessageBox, "exec", lambda box: shown.append(box.text()))
    opened = Session.open_project

    def open_project(session, path):
        opened(session, path)
        if error == "while opening":
            raise RuntimeError(error)
        if error == "later":  # e.g. in a slot: Qt passes it to sys.excepthook
            QTimer.singleShot(0, lambda: 1 / 0)

    monkeypatch.setattr(Session, "open_project", open_project)
    if error == "in a background task":  # the calculation fails: the worker reports it
        monkeypatch.setattr(Engine, "evaluate_all", lambda *arguments, **options: 1 / 0)
    _, window = start(demo)
    assert codes == [code] and shown == []
    if error == "in a background task":
        assert window.session.result is None
    else:
        assert window.session.result is not None and window.session.result.ok  # the project itself calculated
