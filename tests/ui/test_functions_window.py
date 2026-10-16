"""The window with the formula functions (F1)."""

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QFocusEvent
from PySide6.QtWidgets import QApplication

from tafl.core.functions import CURSOR
from tafl.ui import forms, functions_window
from tafl.ui.functions_window import FunctionsWindow


def _formula(window, session, settle, demo):
    window.open_path(demo)
    session.select_table(session.project.table("Orders").id)
    session.select_step(session.table.steps[5].id)  # Calculate Gross, Month, Size
    settle(session)
    window.show()
    return window.settings_panel.form.editor("columns")._rows[0]["formula"]


def test_f1_opens_the_list_and_search_finds(window, qtbot):
    window.show()
    window.functions_action.trigger()
    functions = window._functions_window
    assert isinstance(functions, FunctionsWindow) and functions.isVisible()
    qtbot.keyClicks(functions.search, "round")
    assert functions.current().name == "ROUND"
    assert "ROUND(number [, decimals])" in functions.details.toPlainText()
    qtbot.keyClick(functions.search, Qt.Key.Key_Down)  # up and down move through the list while typing
    assert functions.current().name == "CEIL"
    functions.categories.setCurrentRow(5)  # Dates
    functions.search.clear()
    assert functions.current().category == "Dates"


def test_insert_puts_the_function_at_the_cursor(window, session, settle, demo, qtbot):
    formula = _formula(window, session, settle, demo)
    applied = session.table.steps[5].params["columns"][0]["formula"]
    formula._edit.setPlainText("ROUND([Net] * 1.19, 2) + ")
    formula._edit.moveCursor(formula._edit.textCursor().MoveOperation.End)
    functions_window.remember(formula)  # what clicking into the field does
    functions = functions_window.show_functions(window)
    qtbot.keyClicks(functions.search, "abs")
    assert functions.insert_button.isEnabled()
    functions.insert()
    assert formula.value() == "ROUND([Net] * 1.19, 2) + ABS()"
    assert formula._edit.textCursor().position() == len("ROUND([Net] * 1.19, 2) + ABS(")  # typing goes on inside
    assert session.table.steps[5].params["columns"][0]["formula"] == applied  # inserting doesn't apply yet
    formula._edit.setPlainText(applied)  # closing the window applies what is typed


def test_insert_needs_a_formula_field(window, monkeypatch):
    monkeypatch.setattr(functions_window, "_target", None)
    window.show()
    functions = functions_window.show_functions(window)
    assert not functions.insert_button.isEnabled()
    assert "click into a formula field" in functions.hint.text()
    functions.insert()  # nothing happens


def test_the_formula_field_has_a_functions_button(window, session, settle, demo):
    formula = _formula(window, session, settle, demo)
    formula._functions.click()
    assert window._functions_window.isVisible()
    assert functions_window.target() is formula


def test_keyboard_and_categories(window, session, settle, demo, qtbot):
    formula = _formula(window, session, settle, demo)
    formula._edit.setPlainText("")
    functions_window.remember(formula)
    functions = functions_window.show_functions(window)
    functions.categories.setCurrentRow(5)  # Dates ...
    qtbot.keyClicks(functions.search, "abs")  # ... a new search looks through all categories
    assert functions.categories.currentRow() == 0 and functions.current().name == "ABS"
    functions.list.setFocus()
    qtbot.keyClick(functions.list, Qt.Key.Key_Return)  # Enter in the list inserts too
    assert formula.value() == "ABS()"
    qtbot.keyClick(functions.search, Qt.Key.Key_Escape)
    assert not functions.isVisible()
    formula._edit.setPlainText(session.table.steps[5].params["columns"][0]["formula"])


def test_insert_wraps_the_selection(window, session, settle, demo):
    formula = _formula(window, session, settle, demo)
    formula._edit.setPlainText("[Net] * 1.19")
    cursor = formula._edit.textCursor()
    cursor.setPosition(0)
    cursor.setPosition(5, cursor.MoveMode.KeepAnchor)  # [Net] selected
    formula._edit.setTextCursor(cursor)
    formula.insert_template(f"ROUND({CURSOR})")
    assert formula.value() == "ROUND([Net]) * 1.19"
    assert formula._edit.textCursor().position() == len("ROUND([Net]")  # ready for ", 2"
    formula._edit.setPlainText(session.table.steps[5].params["columns"][0]["formula"])


def test_looking_up_a_function_doesnt_apply_the_formula(window, session, settle, demo, monkeypatch):
    formula = _formula(window, session, settle, demo)
    applied = session.table.steps[5].params["columns"][0]["formula"]
    formula._edit.setPlainText(applied + " + ")  # half written
    functions = functions_window.show_functions(window)
    monkeypatch.setattr(forms.QApplication, "activeWindow", staticmethod(lambda: functions))
    QApplication.sendEvent(formula._edit, QFocusEvent(QEvent.Type.FocusOut, Qt.FocusReason.ActiveWindowFocusReason))
    assert session.table.steps[5].params["columns"][0]["formula"] == applied
    QApplication.sendEvent(formula._edit, QFocusEvent(QEvent.Type.FocusOut, Qt.FocusReason.MouseFocusReason))
    assert session.table.steps[5].params["columns"][0]["formula"] == applied + " + "  # leaving it otherwise applies
    session.undo()


def test_saving_applies_what_is_typed(window, session, settle, demo, tmp_path):
    formula = _formula(window, session, settle, demo)
    formula._edit.setPlainText("[Net] * 2")
    functions_window.remember(formula)  # e.g. typed, then looked up a function
    assert window._save_to(tmp_path / "saved.tafl")
    assert session.table.steps[5].params["columns"][0]["formula"] == "[Net] * 2"
