import importlib.util
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QDialog, QMessageBox  # noqa: E402

from etl_tool.ui.main_window import MainWindow  # noqa: E402
from etl_tool.ui.session import Session  # noqa: E402

EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "make_examples.py"


@pytest.fixture(scope="session", autouse=True)
def isolated_settings(tmp_path_factory):
    """Keep the tests away from the user's real window settings and recent files."""
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path_factory.mktemp("settings")))


@pytest.fixture(autouse=True)
def no_dialogs(monkeypatch):
    """A dialog nobody clicks would hang the tests: fail instead."""

    def fail(*args, **kwargs):
        raise AssertionError(f"unexpected dialog: {args[1:3] if len(args) > 2 else args}")

    for name in ("question", "warning", "critical", "information", "about"):
        monkeypatch.setattr(QMessageBox, name, fail)
    monkeypatch.setattr(QMessageBox, "exec", fail)
    monkeypatch.setattr(QDialog, "exec", fail)


@pytest.fixture
def window(qapp, session):
    window = MainWindow(session)
    yield window
    session._set_dirty(False)  # closing must not ask about unsaved changes
    window.close()
    window.deleteLater()


@pytest.fixture
def session(qapp, registry):
    qapp.setOrganizationName("Tafl Tests")
    session = Session(registry)
    yield session
    session.cancel_run()
    session.wait()


@pytest.fixture
def demo(tmp_path) -> Path:
    spec = importlib.util.spec_from_file_location("make_examples", EXAMPLES)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (tmp_path / "data").mkdir()
    module.write_customers(tmp_path / "data" / "customers.xlsx")
    module.write_orders(tmp_path / "data" / "orders.csv")
    return module.build_project().save(tmp_path / "demo.tafl")


@pytest.fixture
def settle(qtbot):
    """settle(session): wait until the session has no pending or running calculation."""

    def wait(session: Session, timeout: int = 20_000) -> None:
        qtbot.waitUntil(
            lambda: not session.busy and not session.running and not session._timer.isActive(), timeout=timeout
        )

    return wait
