"""Parameters in the window: the dialog, renaming, and {Name} in formula fields."""

import datetime as dt

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialogButtonBox

from etl_tool.core import Parameter
from etl_tool.ui.parameters_dialog import ParametersDialog


def _cell(dialog, row, column):
    return dialog.table.item(row, column).text()


def _ok(dialog):
    return dialog.box.button(QDialogButtonBox.StandardButton.Ok)


def test_the_dialog_shows_values_and_problems(session):
    session.project.parameters = [Parameter("Day", "=DATE '2026-10-15' - 1"), Parameter("Month", "=strftime({Day}, '%Y-%m')")]
    dialog = ParametersDialog(session)
    assert [_cell(dialog, row, 2) for row in range(2)] == ["2026-10-14   (date)", "2026-10   (text)"]
    dialog.table.item(0, 1).setText("=1 +")  # typing a value shows what it is at once
    assert _cell(dialog, 0, 2).startswith("Error: the formula ends too early")
    assert _cell(dialog, 1, 2).startswith("Error: The parameter {Day} has an error")
    dialog.table.item(1, 0).setText("day")
    assert dialog.problem.text() == "There are two parameters named 'day'" and not _ok(dialog).isEnabled()
    dialog.table.item(1, 0).setText("")
    assert dialog.problem.text() == "Every parameter needs a name." and not _ok(dialog).isEnabled()
    dialog.table.item(1, 0).setText("Month")
    assert dialog.problem.isHidden() and _ok(dialog).isEnabled()
    dialog.reject()
    assert session.project.parameters[0].value == "=DATE '2026-10-15' - 1"  # Cancel changes nothing
    dialog.deleteLater()


def test_add_move_delete_and_undo(session):
    dialog = ParametersDialog(session)
    dialog.add()
    dialog.add()
    assert [p.name for p in dialog.parameters()] == ["Parameter1", "Parameter2"]
    dialog.table.item(1, 1).setText("2026-10-01")
    dialog.table.setCurrentCell(1, 1)
    dialog.move(-1)
    assert [(p.name, p.value) for p in dialog.parameters()] == [("Parameter2", "2026-10-01"), ("Parameter1", "")]
    assert _cell(dialog, 0, 2) == "2026-10-01   (date)" and dialog.table.currentRow() == 0
    dialog.table.setCurrentCell(1, 0)
    dialog.remove()
    dialog._accept()
    assert [(p.name, p.value) for p in session.project.parameters] == [("Parameter2", "2026-10-01")]
    assert session.dirty and session.parameter_values() == {"Parameter2": dt.date(2026, 10, 1)}
    session.undo()
    assert session.project.parameters == [] and session.parameter_values() == {}
    dialog.deleteLater()


def test_renaming_a_parameter_updates_its_steps(session, registry, tmp_path):
    session.project.parameters = [Parameter("Day", "2026-10-01"), Parameter("Month", "10")]
    table = session.project.add_table("Orders")
    step = session.project.add_step(table, "csv.import", {"file": "orders_{day}_{Month}.csv"})
    dialog = ParametersDialog(session)
    dialog.table.item(0, 0).setText("Month")  # the two swap their names
    dialog.table.item(1, 0).setText("Day")
    dialog._accept()
    assert step.params["file"] == "orders_{Month}_{Day}.csv"
    assert [p.name for p in session.project.parameters] == ["Month", "Day"]
    dialog.deleteLater()


def test_the_window_has_the_dialog(window):
    assert window.parameters_action in window.menuBar().actions()[2].menu().actions()


def _gross(window, session, settle, demo, qtbot):
    window.open_path(demo)
    session.set_parameters([Parameter("VAT", "1.19"), Parameter("Bad", "=1 +")])
    session.select_table(session.project.table("Orders").id)
    session.select_step(session.table.steps[5].id)  # Calculate Gross, Month, Size
    settle(session)
    window.show()
    editor = window.settings_panel.form.editor("columns")._rows[0]["formula"]
    qtbot.waitUntil(lambda: not editor.checking and editor.line.isVisible(), timeout=10_000)
    return editor


def test_formula_fields_check_with_the_parameters(window, session, settle, demo, qtbot):
    gross = _gross(window, session, settle, demo, qtbot)
    gross._edit.setPlainText("ROUND([Net] * {vat}, 2)")
    qtbot.waitUntil(lambda: not gross.checking and gross.line.text().startswith("→ number: 97.62"), timeout=10_000)
    gross._edit.setPlainText("ROUND([Nett] * {VAT}, 2)")  # a mistake is still found where it is
    qtbot.waitUntil(lambda: not gross.checking and "not found" in gross.line.text(), timeout=10_000)
    assert gross._edit.extraSelections()[0].cursor.selectedText() == "[Nett]"
    gross._edit.setPlainText("[Net] * {VAT2}")
    qtbot.waitUntil(lambda: not gross.checking and "no parameter" in gross.line.text(), timeout=10_000)
    assert gross.line.text().startswith("There is no parameter {VAT2}")
    assert gross._edit.extraSelections()[0].cursor.selectedText() == "{VAT2}"
    gross._edit.setPlainText("[Net] * {Bad}")
    qtbot.waitUntil(lambda: not gross.checking and "has an error" in gross.line.text(), timeout=10_000)
    assert gross.line.text().startswith("The parameter {Bad} has an error: the formula ends too early")
    assert session.table.steps[5].params["columns"][0]["formula"] == "ROUND([Net] * 1.19, 2)"  # typed, not applied
    gross.set_value("ROUND([Net] * 1.19, 2)")


def test_a_brace_lists_the_parameters(window, session, settle, demo, qtbot):
    gross = _gross(window, session, settle, demo, qtbot)
    gross._edit.setFocus()
    gross._edit.setPlainText("[Net] * ")
    gross._edit.moveCursor(gross._edit.textCursor().MoveOperation.End)
    qtbot.keyClicks(gross._edit, "{")
    completer = gross._completer
    assert completer.visible and [(s.name, s.detail) for s in completer.shown] == [("VAT", "number: 1.19"), ("Bad", "error")]
    qtbot.keyClicks(gross._edit, "v")
    qtbot.keyClick(gross._edit, Qt.Key.Key_Return)
    assert gross.value() == "[Net] * {VAT}" and not completer.visible
    gross.set_value("ROUND([Net] * 1.19, 2)")


def test_the_formula_window_uses_the_parameters(window, session, settle, demo, qtbot):
    gross = _gross(window, session, settle, demo, qtbot)
    gross.set_value("ROUND([Net] * {VAT}, 2)")
    gross.open_window()
    formula_window = gross.window_open
    qtbot.waitUntil(lambda: not formula_window.busy and formula_window.status.text().startswith("Result: number"), timeout=10_000)
    assert formula_window.model.rowCount() > 0
    formula_window.reject()
    gross.set_value("ROUND([Net] * 1.19, 2)")
