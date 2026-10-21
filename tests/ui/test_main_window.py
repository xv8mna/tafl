import sys
from pathlib import Path

from PySide6.QtCore import qVersion
from PySide6.QtWidgets import QMessageBox

from tafl import GUIDE_URL
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
