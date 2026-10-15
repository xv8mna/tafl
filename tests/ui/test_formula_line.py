"""The line under a formula field: what the formula gives on the first rows, or what is wrong."""

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from etl_tool.ui import theme
from etl_tool.ui.checks import checks


def _calculate(window, session, settle, demo):
    window.open_path(demo)
    session.select_table(session.project.table("Orders").id)
    session.select_step(session.table.steps[5].id)  # Calculate Gross, Month, Size
    settle(session)
    window.show()
    return [row["formula"] for row in window.settings_panel.form.editor("columns")._rows]


def _wait(editor, qtbot):
    qtbot.waitUntil(lambda: not editor.checking and editor.line.isVisible(), timeout=10_000)


def test_each_formula_shows_what_it_gives(window, session, settle, demo, qtbot):
    gross, month, size = _calculate(window, session, settle, demo)
    for editor in (gross, month, size):
        _wait(editor, qtbot)
    assert gross.line.text().startswith("→ number: 97.62, 435.6, 1766.46, …")
    assert gross.line.text().endswith("no errors in all 156 rows")
    assert month.line.text().startswith("→ text: '2026-02', '2026-04', '2026-02', …")
    assert size.line.text().startswith("→ text: 'normal', 'normal', 'large', …")
    map_editor = window.map_view.settings.form.editor("columns")._rows[0]["formula"]
    assert map_editor._checked_input is None  # the settings on the hidden map check nothing


def test_a_mistake_is_shown_and_can_be_fixed_without_applying(window, session, settle, demo, qtbot):
    gross = _calculate(window, session, settle, demo)[0]
    _wait(gross, qtbot)
    gross._edit.setPlainText("ROUND([Nett] * 1.19, 2)")
    qtbot.waitUntil(lambda: not gross.checking and "not found" in gross.line.text(), timeout=10_000)
    assert "Column [Nett] not found (did you mean [Net]?)" in gross.line.text() and "Write [Net]" in gross.line.text()
    (underline,) = gross._edit.extraSelections()
    assert underline.cursor.selectedText() == "[Nett]"
    QTest.mouseClick(gross.line, Qt.MouseButton.LeftButton)  # clicking the line selects the mistake
    assert gross._edit.textCursor().selectedText() == "[Nett]"
    assert session.table.steps[5].params["columns"][0]["formula"] == "ROUND([Net] * 1.19, 2)"  # nothing applied
    gross.line.linkActivated.emit("fix")
    assert gross.value() == "ROUND([Net] * 1.19, 2)"
    qtbot.waitUntil(lambda: not gross.checking and gross.line.text().startswith("→ number"), timeout=10_000)
    assert gross._edit.extraSelections() == []
    assert session.table.steps[5].params["columns"][0]["formula"] == "ROUND([Net] * 1.19, 2)" and not session.dirty


def test_rows_above_and_the_rows_name(window, session, settle, demo, qtbot):
    gross, month, size = _calculate(window, session, settle, demo)
    size._edit.setPlainText("[Gross] * 2")  # a column of a row above
    qtbot.waitUntil(lambda: not size.checking and "number" in size.line.text(), timeout=10_000)
    gross._edit.setPlainText("[Nett]")
    qtbot.keyClick(gross._edit, Qt.Key.Key_Return)  # applied: the row below now waits for it
    settle(session)
    qtbot.waitUntil(lambda: not size.checking and "Row 1" in size.line.text(), timeout=10_000)
    assert size.line.text().startswith("Row 1 ([Gross]) has an error: Column [Nett] not found")
    session.undo()  # rebuilds the rows: nothing typed is left to apply when closing
    settle(session)


def test_a_condition_and_no_data(window, session, settle, demo, qtbot):
    window.open_path(demo)
    session.select_table(session.project.table("Orders").id)
    session.add_step(session.registry.actions["rows.filter_formula"], 6, {"condition": "[Net] > 1000"})
    settle(session)
    window.show()
    condition = window.settings_panel.form.editor("condition")
    _wait(condition, qtbot)
    assert condition.line.text().startswith("→ true for ") and condition.line.text().endswith("of all 156 rows")
    session.set_table_enabled([session.table.id], False)
    settle(session)
    qtbot.waitUntil(lambda: not condition.checking and "turned off" in condition.line.text(), timeout=10_000)
    assert "#e06c6c" not in condition.line.styleSheet()  # not an error of the formula


def test_an_empty_formula_shows_no_line(window, session, settle, demo, qtbot):
    size = _calculate(window, session, settle, demo)[2]
    _wait(size, qtbot)
    size._edit.setPlainText("")
    qtbot.waitUntil(lambda: not size.checking and not size.line.isVisible(), timeout=10_000)
    size._edit.setPlainText(size._last)


def test_while_typing_the_line_is_greyed_until_it_is_checked(window, session, settle, demo, qtbot):
    gross = _calculate(window, session, settle, demo)[0]
    gross._edit.setPlainText("[Nett] * 2")
    qtbot.waitUntil(lambda: not gross.checking and "not found" in gross.line.text(), timeout=10_000)
    gross._edit.textCursor().insertText("x")
    assert theme.TEXT_DISABLED in gross.line.styleSheet() and gross._edit.extraSelections() == []
    gross._edit.textCursor().deletePreviousChar()  # the same text again: the result fits again
    assert theme.STATUS_COLORS["error"] in gross.line.styleSheet() and len(gross._edit.extraSelections()) == 1
    gross._edit.setPlainText(gross._last)


def test_unusual_results_and_formulas(window, session, settle, demo, qtbot):
    gross = _calculate(window, session, settle, demo)[0]
    gross._edit.setPlainText("'infinity'::DATE")
    qtbot.waitUntil(lambda: not gross.checking and "date" in gross.line.text(), timeout=10_000)
    assert gross.line.text() == "→ date: (values out of range) · no errors in all 156 rows"
    gross._edit.setPlainText("-- later")
    qtbot.waitUntil(lambda: not gross.checking and "empty" in gross.line.text(), timeout=10_000)
    assert gross.line.text() == "The formula is empty: it has only comments."
    gross._edit.setPlainText(gross._last)


def test_a_new_row_without_a_name(window, session, settle, demo, qtbot):
    _calculate(window, session, settle, demo)
    rows = window.settings_panel.form.editor("columns")
    rows._add_clicked()
    settle(session)
    formula = window.settings_panel.form.editor("columns")._rows[-1]["formula"]
    qtbot.waitUntil(formula.isVisible)
    formula._edit.setPlainText("[Net] * 2")
    qtbot.waitUntil(lambda: not formula.checking and "name" in formula.line.text(), timeout=10_000)
    assert formula.line.text() == "This row has no column name yet: the step needs one."
    formula._edit.setPlainText(formula._last)


def test_slow_checks_dont_hold_up_the_others(qtbot):
    service = checks()
    slow = [service.client(lambda _result: None) for _ in range(3)]
    for client in slow:
        client.start(lambda cancel: cancel.wait(30))
    got = []
    fast = service.client(got.append)
    fast.start(lambda _cancel: 42)
    qtbot.waitUntil(lambda: got == [42], timeout=3_000)
    for client in [*slow, fast]:
        client.close()
    qtbot.waitUntil(lambda: not service.busy, timeout=3_000)


def test_checks_reuse_the_connection_of_their_thread(qtbot):
    """A Qt pool thread gets new thread-local data for every job: the connection must not."""
    import threading

    import polars as pl

    from etl_tool.api import expressions

    def job(_cancel):
        expressions.evaluate(pl.DataFrame({"a": [1]}), "[a] + 1")
        return threading.get_ident(), id(expressions._connection())

    seen = []
    client = checks().client(seen.append)
    for number in range(1, 9):
        client.start(job)
        qtbot.waitUntil(lambda number=number: len(seen) == number, timeout=5_000)
    assert len({con for _, con in seen}) == len({ident for ident, _ in seen})  # one per thread, not per job
    client.close()
