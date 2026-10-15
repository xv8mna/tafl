"""Suggestions while typing a formula, colors and the matching bracket, in the settings and in
the formula window."""

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFrame, QScrollArea

from etl_tool.ui import theme

CTRL = Qt.KeyboardModifier.ControlModifier


def _field(window, session, settle, demo, row=2):
    window.open_path(demo)
    session.select_table(session.project.table("Orders").id)
    session.select_step(session.table.steps[5].id)  # Calculate Gross, Month, Size
    settle(session)
    window.show()
    editor = window.settings_panel.form.editor("columns")._rows[row]["formula"]
    qtbot_wait = editor  # the columns with their types come with the first check
    return qtbot_wait


def _ready(editor, qtbot):
    qtbot.waitUntil(lambda: not editor.checking and editor._result is not None, timeout=10_000)
    editor._edit.setFocus()
    editor._edit.setPlainText("")


def _formula(session, row=2):
    return session.table.steps[5].params["columns"][row]["formula"]


def test_brackets_list_the_columns_and_enter_writes_one_in(window, session, settle, demo, qtbot):
    editor = _field(window, session, settle, demo)
    _ready(editor, qtbot)
    qtbot.keyClicks(editor._edit, "[")
    completer = editor._completer
    assert completer.visible and [s.name for s in completer.shown][:3] == ["OrderNo", "CustomerNo", "Date"]
    assert ("Gross", "number (row 1)") in [(s.name, s.detail) for s in completer.shown]  # a row above
    qtbot.keyClicks(editor._edit, "gr")
    assert [s.name for s in completer.shown] == ["Gross"]
    qtbot.keyClick(editor._edit, Qt.Key.Key_Return)
    assert editor.value() == "[Gross]" and not completer.visible
    assert _formula(session) == "IIF([Net] >= 1000, 'large', 'normal')"  # written in, not applied
    qtbot.keyClicks(editor._edit, " * 2")
    qtbot.keyClick(editor._edit, Qt.Key.Key_Return)  # the list is closed: Enter applies
    assert _formula(session) == "[Gross] * 2"
    session.undo()


def test_functions_after_two_letters_tab_and_esc(window, session, settle, demo, qtbot):
    editor = _field(window, session, settle, demo)
    _ready(editor, qtbot)
    completer = editor._completer
    qtbot.keyClicks(editor._edit, "r")
    assert not completer.visible  # one letter is not enough
    qtbot.keyClicks(editor._edit, "ou")
    assert completer.visible and completer.shown[0].name == "ROUND"
    qtbot.keyClick(editor._edit, Qt.Key.Key_Tab)
    assert editor.value() == "round()" and editor._edit.textCursor().position() == 6 and editor._edit.hasFocus()
    qtbot.keyClicks(editor._edit, "[ne")
    assert completer.visible
    qtbot.keyClick(editor._edit, Qt.Key.Key_Escape)  # closes the list, keeps the text
    assert not completer.visible and editor.value() == "round([ne)"
    editor._edit.setPlainText("CASE WHEN [Net] > 1 THEN 'a' END")
    editor._edit.moveCursor(editor._edit.textCursor().MoveOperation.End)
    qtbot.keyClicks(editor._edit, " ")
    qtbot.keyClick(editor._edit, Qt.Key.Key_Backspace)
    assert not completer.visible  # END is complete: Enter applies
    qtbot.keyClick(editor._edit, Qt.Key.Key_Return)
    assert _formula(session) == "CASE WHEN [Net] > 1 THEN 'a' END"
    session.undo()


def test_the_button_ctrl_space_and_a_click(window, session, settle, demo, qtbot):
    editor = _field(window, session, settle, demo)
    _ready(editor, qtbot)
    completer = editor._completer
    QTest.mouseClick(editor._insert, Qt.MouseButton.LeftButton)  # [ ]: as when typing [
    assert editor.value() == "[" and completer.visible
    popup = completer._popup
    row = next(popup.list.topLevelItem(i) for i in range(popup.list.topLevelItemCount()) if popup.list.topLevelItem(i).text(0) == "City")
    QTest.mouseClick(popup.list.viewport(), Qt.MouseButton.LeftButton, pos=popup.list.visualItemRect(row).center())
    assert editor.value() == "[City]" and not completer.visible
    assert _formula(session) == "IIF([Net] >= 1000, 'large', 'normal')"  # nothing applied by the clicks
    qtbot.keyClicks(editor._edit, " || u")
    assert not completer.visible
    qtbot.keyClick(editor._edit, Qt.Key.Key_Space, CTRL)  # Ctrl+Space: from the first letter on
    assert completer.visible and completer.shown[0].name == "UPPER"
    qtbot.keyClick(editor._edit, Qt.Key.Key_Escape)
    editor._edit.setPlainText(editor._last)


def test_colors_and_the_matching_bracket(window, session, settle, demo, qtbot):
    editor = _field(window, session, settle, demo)
    _ready(editor, qtbot)
    editor._edit.setPlainText("ROUND([Nett], 2)")
    qtbot.waitUntil(lambda: not editor.checking and "not found" in editor.line.text(), timeout=10_000)
    block = editor._edit.document().firstBlock()
    colors = {block.text()[r.start : r.start + r.length]: r.format.foreground().color().name() for r in block.layout().formats()}
    assert colors["ROUND"] == theme.FORMULA_COLORS["function"] and colors["[Nett]"] == theme.FORMULA_COLORS["unknown"]
    cursor = editor._edit.textCursor()
    cursor.setPosition(6)  # just after ROUND(
    editor._edit.setTextCursor(cursor)
    brackets = [s for s in editor._edit.extraSelections() if s.format.background().style() != Qt.BrushStyle.NoBrush]
    if editor._edit.hasFocus():
        assert sorted(s.cursor.selectionStart() for s in brackets) == [5, 15]
    editor._edit.setPlainText(editor._last)


def test_the_formula_window_suggests_too(window, session, settle, demo, qtbot):
    editor = _field(window, session, settle, demo)
    editor._expand.click()
    formula = editor.window_open
    qtbot.waitUntil(lambda: not formula.busy, timeout=10_000)
    formula.editor.setPlainText("")
    qtbot.keyClicks(formula.editor, "[cit")
    assert formula._completer.visible and [s.name for s in formula._completer.shown] == ["City"]
    qtbot.keyClick(formula.editor, Qt.Key.Key_Return)  # takes the suggestion, no new line
    assert formula.text() == "[City]" and formula.isVisible()
    qtbot.keyClicks(formula.editor, " || lo")
    qtbot.keyClick(formula.editor, Qt.Key.Key_Escape)  # closes the list, not the window
    assert formula.isVisible() and not formula._completer.visible
    formula.reject()


def test_the_window_lists_columns_at_once_and_ctrl_enter_takes_the_suggestion(window, session, settle, demo, qtbot):
    editor = _field(window, session, settle, demo)
    editor._expand.click()
    formula = editor.window_open
    formula.editor.setPlainText("")
    qtbot.keyClicks(formula.editor, "[")  # before the first preview has come in
    assert formula._completer.visible and "City" in [s.name for s in formula._completer.shown]
    qtbot.waitUntil(lambda: not formula.busy, timeout=10_000)
    formula.activateWindow()
    qtbot.waitUntil(lambda: QApplication.activeWindow() is formula, timeout=5_000)
    qtbot.keyClicks(formula.editor, "cit")
    qtbot.keyClick(formula.editor, Qt.Key.Key_Return, CTRL)  # takes the suggestion: nothing is applied
    assert formula.isVisible() and formula.text() == "[City]"
    qtbot.keyClick(formula.editor, Qt.Key.Key_Escape)
    assert "Esc again" in formula.status.text()
    formula._highlighter.set_columns(["x"])  # coloring the text again is no typing
    qtbot.keyClick(formula.editor, Qt.Key.Key_Escape)
    assert not formula.isVisible()


def test_ctrl_enter_and_enter_after_a_unit_in_the_field(window, session, settle, demo, qtbot):
    editor = _field(window, session, settle, demo)
    _ready(editor, qtbot)
    qtbot.keyClicks(editor._edit, "[ne")
    qtbot.keyClick(editor._edit, Qt.Key.Key_Return, CTRL)  # the list is open: it takes the suggestion
    assert editor.value() == "[Net]" and _formula(session) == "IIF([Net] >= 1000, 'large', 'normal')"
    editor._edit.setPlainText("[Date] + INTERVAL 1 MONTH")
    editor._edit.moveCursor(editor._edit.textCursor().MoveOperation.End)
    qtbot.keyClicks(editor._edit, "H")  # MONTH typed to its end
    qtbot.keyClick(editor._edit, Qt.Key.Key_Backspace)
    assert not editor._completer.visible
    qtbot.keyClick(editor._edit, Qt.Key.Key_Return)  # applies
    assert _formula(session) == "[Date] + INTERVAL 1 MONTH"
    session.undo()


def test_the_list_goes_with_its_field(window, session, settle, demo, qtbot):
    editor = _field(window, session, settle, demo)
    _ready(editor, qtbot)
    qtbot.keyClicks(editor._edit, "[ne")
    popup = editor._completer._popup
    assert popup.isVisible()
    scroll = window.settings_panel.findChild(QScrollArea)
    scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
    scroll.resize(scroll.width(), 120)  # the field is out of view now
    editor._completer._place()
    assert not editor._completer.visible
    qtbot.keyClick(editor._edit, Qt.Key.Key_Space, CTRL)
    editor._edit.setPlainText(editor._last)
    destroyed = []
    popup.destroyed.connect(lambda: destroyed.append(True))
    session.select_step(session.table.steps[4].id)  # another step: the form and its fields go
    qtbot.waitUntil(lambda: destroyed == [True], timeout=5_000)  # and so does the list
    assert not [w for w in window.findChildren(QFrame, "Suggestions") if w.isVisible()]


def test_the_buttons_never_show_a_focus_frame(window, session, settle, demo, qtbot):
    editor = _field(window, session, settle, demo)
    assert all(b.focusPolicy() == Qt.FocusPolicy.NoFocus for b in (editor._insert, editor._functions, editor._expand))
    _ready(editor, qtbot)
    QTest.mouseClick(editor._functions, Qt.MouseButton.LeftButton)  # opens the function list
    assert _formula(session) == "IIF([Net] >= 1000, 'large', 'normal')"
    QTest.mouseClick(editor.line, Qt.MouseButton.LeftButton)
    assert _formula(session) == "IIF([Net] >= 1000, 'large', 'normal')"  # the line keeps the focus too
    for window_ in QApplication.topLevelWidgets():
        if type(window_).__name__ == "FunctionsWindow":
            window_.close()
    editor._edit.setPlainText(editor._last)
