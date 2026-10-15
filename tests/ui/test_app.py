import sys

from PySide6.QtWidgets import QApplication

from etl_tool import APP_FOLDER, APP_NAME, COMMAND
from etl_tool.ui import app as app_module
from etl_tool.ui.main_window import MainWindow


def test_app_starts_and_opens_a_project(qapp, monkeypatch, tmp_path, demo):
    """The same entry point the desktop shortcut uses (without waiting for the window to close)."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(QApplication, "exec", lambda self: 0)
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    try:
        assert app_module.main(["tafl-app", str(demo)]) == 0
        windows = [w for w in QApplication.topLevelWidgets() if isinstance(w, MainWindow)]
        assert windows and windows[-1].session.project.name == "Demo"
        assert (tmp_path / APP_FOLDER / f"{COMMAND}.log").exists()
    finally:
        for window in [w for w in QApplication.topLevelWidgets() if isinstance(w, MainWindow)]:
            window.session._set_dirty(False)
            window.close()
            window.session.wait()


def test_open_dialogs_offer_project_files():
    from etl_tool.ui.main_window import _FILTER

    assert _FILTER == f"{APP_NAME} projects (*.tafl);;All files (*.*)"
