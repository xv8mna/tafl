import json
import sys
import threading
from pathlib import Path

import pytest
from PySide6.QtCore import qVersion
from PySide6.QtWidgets import QApplication, QMessageBox

from tafl import GUIDE_URL
from tafl.core import Engine
from tafl.ui import main_window


def test_window_shows_the_demo(window, settle, session, demo):
    assert window.open_path(demo)
    settle(session)
    assert window.tables_panel.tree.topLevelItemCount() == 3
    assert "demo.tafl" in window.windowTitle()

    session.select_table(session.project.table("Orders").id)
    settle(session)
    assert window.steps_panel.list.count() == 10
    assert window.data_panel.model.rowCount() == 156  # last step: an export, data passes through
    assert window.settings_panel.title.text() == "Export to CSV / text file"

    window.steps_panel.list.setCurrentRow(3)
    assert session.step.action == "table.merge"
    assert window.settings_panel.title.text() == "Merge with another table"
    assert window.data_panel.model.columnCount() == 7


def test_editing_a_formula_recalculates(window, settle, session, demo):
    window.open_path(demo)
    session.select_table(session.project.table("Orders").id)
    session.select_step(session.table.steps[5].id)  # Calculate Gross, Month, Size
    settle(session)
    formula = window.settings_panel.form.editor("columns")._rows[0]["formula"]
    formula._edit.setPlainText("[Net] * 2")
    formula._edit.committed.emit()  # what Enter or leaving the field does
    settle(session)
    df = window.data_panel.model.frame
    assert df["Gross"].to_list() == (df["Net"] * 2).to_list()
    assert window.windowTitle().startswith("demo.tafl •")


def test_typing_is_not_overwritten_but_undo_updates_the_form(window, settle, session, demo):
    window.open_path(demo)
    session.select_table(session.project.table("Orders").id)
    session.select_step(session.table.steps[5].id)
    settle(session)
    formula = window.settings_panel.form.editor("columns")._rows[0]["formula"]
    formula._edit.setPlainText("[Net] * 3")  # typed, not applied yet ...
    session.results_changed.emit()  # ... while a calculation finishes
    assert formula.value() == "[Net] * 3"

    formula._edit.committed.emit()
    settle(session)
    session.undo()
    formula = window.settings_panel.form.editor("columns")._rows[0]["formula"]
    assert formula.value() == "ROUND([Net] * 1.19, 2)"


@pytest.mark.parametrize("stop", [False, True], ids=["running", "stopping"])
def test_a_run_that_fails_leaves_no_message_that_it_runs(window, session, qtbot, monkeypatch, stop):
    """An unexpected error in a run (a fault, not the error of a step) is shown in a box. After
    it, the status bar must not say that the project runs, or stops, with nothing running."""
    session.error.disconnect(window._show_error)  # the box
    entered, leave = threading.Event(), threading.Event()

    def failing(self, tables=None, **kwargs):
        entered.set()
        leave.wait(10)
        raise RuntimeError("Unexpected on purpose")

    monkeypatch.setattr(Engine, "run", failing)
    session.project.add_table("Orders")
    window.run_project()
    assert entered.wait(10) and window.statusBar().currentMessage() == "Running the project…"
    if stop:
        window.stop_run()
        assert window.statusBar().currentMessage() == "Stopping after the current step…"
    with qtbot.waitSignal(session.error, timeout=10_000):
        leave.set()
    assert window.statusBar().currentMessage() == "" and window.run_action.isEnabled()


@pytest.mark.parametrize(
    ("answer", "closing"),
    [
        ("wait", "The window closes when the run is finished…"),
        ("stop", "Stopping after the current step…  The window closes then."),
    ],
    ids=["wait", "stop"],
)
def test_a_closing_cancelled_after_a_run_that_failed_leaves_no_message_that_it_closes(
    window, session, qtbot, monkeypatch, answer, closing
):
    """Closing during a run waits for the run, or stops it, and then asks about unsaved changes.
    After a run that failed (a fault, shown in a box) and a Cancel there, the window stays open:
    the status bar must not say that it closes."""
    session.error.disconnect(window._show_error)  # the box
    entered, leave = threading.Event(), threading.Event()

    def failing(self, tables=None, **kwargs):
        entered.set()
        leave.wait(10)
        raise RuntimeError("Unexpected on purpose")

    asked = []
    monkeypatch.setattr(Engine, "run", failing)
    monkeypatch.setattr(window, "ask_close_while_running", lambda: answer)
    monkeypatch.setattr(window, "ask_save", lambda reason: asked.append(reason) or "cancel")
    session.project.add_table("Orders")
    session._set_dirty(True)
    window.show()
    window.run_project()
    assert entered.wait(10) and not window.close()
    assert window.statusBar().currentMessage() == closing
    with qtbot.waitSignal(session.error, timeout=10_000):
        leave.set()
    qtbot.waitUntil(lambda: asked == ["before closing"], timeout=10_000)
    assert window.isVisible() and not session.running
    assert window.statusBar().currentMessage() == ""


def test_about_names_the_licenses_of_qt_and_pyside(window, monkeypatch, tmp_path):
    shown = []
    monkeypatch.setattr(QMessageBox, "about", lambda parent, title, text: shown.append(text))
    window.show_about()
    assert f"Qt {qVersion()} and PySide6 are under the LGPL-3.0.<br>" in shown[0]
    monkeypatch.setattr(sys, "frozen", True, raising=False)  # the download
    monkeypatch.setattr(sys, "executable", str(tmp_path / "tafl-app.exe"))
    window.show_about()
    assert f"LGPL-3.0; the licenses of all parts are in {tmp_path / 'licenses'}.<br>" in shown[1]


def test_user_guide_opens_the_page_of_the_download_or_the_guide_on_github(window, monkeypatch, tmp_path):
    opened = []

    class Desktop:  # never a real browser
        openUrl = staticmethod(opened.append)

    monkeypatch.setattr(main_window, "QDesktopServices", Desktop)
    help_menu = window.menuBar().actions()[-1].menu()
    assert help_menu.actions()[0] is window.guide_action
    assert window.guide_action.text() == "User guide"

    window.guide_action.trigger()  # a source install
    assert opened.pop().toString() == GUIDE_URL == "https://github.com/xv8mna/tafl/blob/main/docs/guide.md"

    monkeypatch.setattr(sys, "frozen", True, raising=False)  # the download
    monkeypatch.setattr(sys, "executable", str(tmp_path / "tafl-app.exe"))
    (tmp_path / "Guide.html").write_text("<!DOCTYPE html>", encoding="utf-8")
    window.guide_action.trigger()
    assert Path(opened.pop().toLocalFile()) == tmp_path / "Guide.html"

    (tmp_path / "Guide.html").unlink()  # removed from the folder: the guide on GitHub
    window.guide_action.trigger()
    assert opened.pop().toString() == GUIDE_URL


def _odd_project(tmp_path) -> Path:
    """Orders: its filter step stored "enabled": "maybe"; Summary (in a collapsed group, stored
    as "maybe" too) stored "enabled": 2. As a program may write a project file."""
    (tmp_path / "orders.csv").write_text("Id;N\n1;5\n2;50\n", encoding="utf-8")
    steps = [
        {"id": "s1", "action": "csv.import", "version": 3, "params": {"file": "orders.csv", "delimiter": ";"}},
        {"id": "s2", "action": "rows.filter_formula", "version": 1, "params": {"condition": "[N] > 6"}, "enabled": "maybe"},
    ]
    tables = [
        {"id": "t1", "name": "Orders", "steps": steps},
        {"id": "t2", "name": "Summary", "enabled": 2, "group": "g1", "steps": [{"id": "s3", "action": "table.derive", "version": 1, "params": {"table": "t1"}}]},
    ]
    groups = [{"id": "g1", "name": "Group", "color": "blue", "collapsed": "maybe"}]
    path = tmp_path / "odd.tafl"
    path.write_text(json.dumps({"format": "tafl-project", "format_version": 1, "name": "odd", "groups": groups, "tables": tables}), encoding="utf-8")
    return path


def test_steps_and_tables_stored_neither_on_nor_off_in_the_window(window, session, settle, tmp_path):
    """The panels, the map and the Find panel show them without a crash; the step stops with its
    message; switching it off writes a real false with an undo step, and undo brings the stored
    value back, which a save keeps as it is."""
    session.open_project(_odd_project(tmp_path), trusted=True)
    window.resize(1500, 900)
    window.show()
    session.select_table("t1")
    session.select_step("s2")
    settle(session)
    window.tables_panel.rebuild()
    window.steps_panel.update_items()
    window.settings_panel.refresh()
    assert window.settings_panel.enabled.isChecked()  # not off: it stops with an error
    assert session.step_result(session.step).error == 'Step is on: "maybe" is not true or false'
    window.show_map(True)
    window.map_view.rebuild()
    QApplication.processEvents()
    window.show_map(False)
    window.open_find()
    window.find_panel.set_text("Orders")
    window.find_panel.refresh()
    QApplication.processEvents()
    window.find_dock.hide()
    session.select_table("t2")
    settle(session)
    assert session.result.first_error == "Table is on: 2 is not true or false" and session.project.groups[0].collapsed is False

    session.select_table("t1")
    session.select_step("s2")
    window.settings_panel.refresh()
    window.settings_panel.enabled.setChecked(False)  # Step is on: off
    step = session.find_step("s2")[1]
    assert (step.enabled, step.stored_enabled) == (False, None)
    session.undo()
    assert session.find_step("s2")[1].stored_enabled == "maybe"
    session.set_table_enabled(["t2"], False)
    assert (session.project.table("t2").enabled, session.project.table("t2").stored_enabled) == (False, None)
    session.undo()
    session.save_project(tmp_path / "again.tafl")
    saved = json.loads((tmp_path / "again.tafl").read_text(encoding="utf-8"))
    assert saved["tables"][0]["steps"][1]["enabled"] == "maybe" and saved["tables"][1]["enabled"] == 2
    session.redo()
    assert session.project.table("t2").enabled is False
