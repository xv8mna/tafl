"""The formula window (F2): editor, columns, functions and a preview of the result."""

import time

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QFocusEvent
from PySide6.QtWidgets import QApplication

from etl_tool.ui import forms
from etl_tool.ui.checks import checks


def _calculate(window, session, settle, demo, row=2):
    window.open_path(demo)
    session.select_table(session.project.table("Orders").id)
    session.select_step(session.table.steps[5].id)  # Calculate Gross, Month, Size
    settle(session)
    window.show()
    return window.settings_panel.form.editor("columns")._rows[row]["formula"]


def _open(editor, qtbot):
    editor._expand.click()
    formula = editor.window_open
    qtbot.waitUntil(lambda: not formula.busy, timeout=10_000)
    return formula


def _activate(formula, qtbot):
    """Shortcuts such as Ctrl+Enter only work in the active window."""
    formula.activateWindow()
    qtbot.waitUntil(lambda: QApplication.activeWindow() is formula, timeout=5_000)


def _formula(session):
    return session.table.steps[5].params["columns"][2]["formula"]


def test_the_window_shows_columns_and_a_preview(window, session, settle, demo, qtbot):
    editor = _calculate(window, session, settle, demo)
    formula = _open(editor, qtbot)
    assert formula.windowTitle() == "Formula for [Size]"
    assert formula.text() == "IIF([Net] >= 1000, 'large', 'normal')"
    listed = {formula.column_list.topLevelItem(i).text(0): formula.column_list.topLevelItem(i).text(1)
              for i in range(formula.column_list.topLevelItemCount())}  # fmt: skip
    assert listed["Net"] == "number" and listed["Gross"] == "number (row 1)" and listed["Month"] == "text (row 2)"
    assert "Result: text" in formula.status.text() and "156 rows" in formula.status.text()
    assert formula.model.frame.columns == ["Net", "Size"]

    formula.editor.setPlainText("[Gross] * 2")  # a column made by a row above
    qtbot.waitUntil(lambda: not formula.busy and "number" in formula.status.text(), timeout=10_000)
    assert formula.model.frame.columns == ["Gross", "Size"]
    formula.editor.setPlainText("[Nett] * 2")
    qtbot.waitUntil(lambda: not formula.busy and "did you mean [Net]" in formula.status.text(), timeout=10_000)
    formula.reject()


def test_apply_and_cancel(window, session, settle, demo, qtbot):
    editor = _calculate(window, session, settle, demo)
    formula = _open(editor, qtbot)
    formula.editor.setPlainText("[Net] * 2")
    formula.reject()  # Cancel changes nothing
    assert _formula(session) == "IIF([Net] >= 1000, 'large', 'normal')" and editor.window_open is None

    formula = _open(editor, qtbot)
    _activate(formula, qtbot)
    formula.editor.setPlainText("[Net] * 2")
    qtbot.keyClick(formula.editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)  # Ctrl+Enter applies
    assert _formula(session) == "[Net] * 2" and editor.value() == "[Net] * 2"
    assert not formula.isVisible()

    formula = _open(editor, qtbot)
    _activate(formula, qtbot)
    formula.editor.setPlainText("[Net] * 3")
    qtbot.keyClicks(formula.column_search, "gro")
    qtbot.keyClick(formula.column_search, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    assert _formula(session) == "[Net] * 3"  # applied as it is: nothing inserted from the search

    formula = _open(editor, qtbot)
    formula.editor.setPlainText("[Net] * 4")
    formula.apply_button.setFocus()
    qtbot.keyClick(formula.apply_button, Qt.Key.Key_Return)  # Enter presses the button that has the focus
    assert _formula(session) == "[Net] * 4" and not formula.isVisible()


def test_enter_is_a_new_line_and_esc_asks_before_discarding(window, session, settle, demo, qtbot):
    editor = _calculate(window, session, settle, demo)
    formula = _open(editor, qtbot)
    qtbot.keyClick(formula.editor, Qt.Key.Key_Return)
    assert formula.text().count("\n") == 1 and formula.isVisible()  # Enter doesn't close the window
    qtbot.keyClick(formula.editor, Qt.Key.Key_Escape)
    assert formula.isVisible() and "Esc again" in formula.status.text()
    qtbot.keyClick(formula.editor, Qt.Key.Key_Escape)
    assert not formula.isVisible()
    assert _formula(session) == "IIF([Net] >= 1000, 'large', 'normal')"


def test_columns_and_functions_are_inserted(window, session, settle, demo, qtbot):
    editor = _calculate(window, session, settle, demo)
    formula = _open(editor, qtbot)
    formula.editor.setPlainText("")
    qtbot.keyClicks(formula.column_search, "gro")
    qtbot.keyClick(formula.column_search, Qt.Key.Key_Return)
    assert formula.text() == "[Gross]"
    cursor = formula.editor.textCursor()
    cursor.select(cursor.SelectionType.Document)
    formula.editor.setTextCursor(cursor)
    formula.tabs.setCurrentIndex(1)
    qtbot.keyClicks(formula.functions.search, "round")
    qtbot.keyClick(formula.functions.search, Qt.Key.Key_Return)
    assert formula.text() == "ROUND([Gross])"  # around the selection
    formula.reject()


def test_a_condition_shows_how_many_rows_it_keeps(window, session, settle, demo, qtbot):
    window.open_path(demo)
    session.select_table(session.project.table("Orders").id)
    session.add_step(session.registry.actions["rows.filter_formula"], 6, {"condition": "[Net] > 1000"})
    settle(session)
    window.show()
    editor = window.settings_panel.form.editor("condition")
    formula = _open(editor, qtbot)
    assert formula.windowTitle() == "Condition"
    assert formula.status.text().startswith("True for ") and "of all 156 rows" in formula.status.text()
    formula.editor.setPlainText("[Net] + 1")
    qtbot.waitUntil(lambda: not formula.busy and "true or false" in formula.status.text(), timeout=10_000)
    formula.reject()


def test_the_field_waits_while_the_window_is_open(window, session, settle, demo, qtbot, monkeypatch):
    editor = _calculate(window, session, settle, demo)
    editor._edit.setPlainText("[Net] * ")  # half typed, then F2
    qtbot.keyClick(editor._edit, Qt.Key.Key_F2)
    formula = editor.window_open
    assert formula is not None and formula.text() == "[Net] * "  # the window starts from what is typed
    monkeypatch.setattr(forms.QApplication, "activeWindow", staticmethod(lambda: formula))
    QApplication.sendEvent(editor._edit, QFocusEvent(QEvent.Type.FocusOut, Qt.FocusReason.ActiveWindowFocusReason))
    assert _formula(session) == "IIF([Net] >= 1000, 'large', 'normal')"  # nothing applied by opening
    qtbot.waitUntil(lambda: not formula.busy, timeout=10_000)
    formula.reject()
    editor._edit.setPlainText(_formula(session))


def test_the_esc_warning_stays_until_the_next_key(window, session, settle, demo, qtbot):
    editor = _calculate(window, session, settle, demo)
    formula = _open(editor, qtbot)
    qtbot.keyClicks(formula.editor, " || 'x'")
    qtbot.keyClick(formula.editor, Qt.Key.Key_Escape)  # before the preview of the change arrives
    qtbot.waitUntil(lambda: not formula.busy, timeout=10_000)
    assert formula.status.text() == "Press Esc again to discard your changes."
    qtbot.keyClick(formula.editor, Qt.Key.Key_Backspace)
    qtbot.waitUntil(lambda: not formula.busy and "closing ' is missing" in formula.status.text(), timeout=10_000)
    formula.reject()


def test_closing_never_waits_for_a_slow_preview(window, session, settle, demo, qtbot):
    editor = _calculate(window, session, settle, demo)
    formula = _open(editor, qtbot)
    formula.editor.setPlainText("(SELECT SUM(range % 7) FROM range(5000000000)) + [Net]")
    formula.check()
    qtbot.waitUntil(lambda: formula.busy, timeout=5_000)
    started = time.perf_counter()
    formula.reject()
    assert time.perf_counter() - started < 1
    qtbot.waitUntil(lambda: not checks().busy, timeout=5_000)  # the query was stopped


def test_the_preview_follows_new_results(window, session, settle, demo, qtbot):
    editor = _calculate(window, session, settle, demo)
    formula = _open(editor, qtbot)
    assert "all 156 rows" in formula.status.text()
    session.set_enabled(session.table.steps[2].id, False)  # the filter before it
    settle(session)
    qtbot.waitUntil(lambda: not formula.busy and "156 rows" not in formula.status.text(), timeout=10_000)
    assert formula.status.text().startswith("Result: text")
    formula.reject()


def test_the_editor_never_disappears(window, session, settle, demo, qtbot):
    editor = _calculate(window, session, settle, demo)
    formula = _open(editor, qtbot)
    formula.right.setSizes([0, 600])
    formula.body.setSizes([0, 1000])
    assert formula.right.sizes()[0] > 0 and formula.body.sizes()[0] > 0
    formula.reject()
