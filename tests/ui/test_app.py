import sys

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from etl_tool import APP_FOLDER, APP_NAME, COMMAND, OLD_APP_NAMES
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


def test_settings_of_the_old_name_are_taken_over(qapp):
    """Recent files and the window layout of "ETL Tool" carry over to the renamed app."""
    names = qapp.organizationName(), qapp.applicationName()
    try:
        QApplication.setOrganizationName(APP_NAME)
        QApplication.setApplicationName(APP_NAME)
        QSettings().clear()
        old = QSettings(OLD_APP_NAMES[0], OLD_APP_NAMES[0])
        old.setValue("recent", ["C:/data/sales.etlproj"])
        old.sync()
        app_module._adopt_old_settings()
        assert QSettings().value("recent") == ["C:/data/sales.etlproj"]
        QSettings().setValue("recent", ["C:/data/sales.tafl"])
        app_module._adopt_old_settings()  # only once: settings of the new name stay
        assert QSettings().value("recent") == ["C:/data/sales.tafl"]
    finally:
        QSettings().clear()
        QSettings(OLD_APP_NAMES[0], OLD_APP_NAMES[0]).clear()
        QApplication.setOrganizationName(names[0])
        QApplication.setApplicationName(names[1])


def test_open_dialogs_offer_old_and_new_project_files():
    from etl_tool.ui.main_window import _FILTER

    assert _FILTER.startswith(f"{APP_NAME} projects (*.tafl *.etlproj)")
