"""The line under a formula field: what the formula gives on the first rows, or what is wrong."""

from PySide6.QtCore import Qt


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
    gross.line.clicked.emit()  # clicking the line selects the mistake
    assert gross._edit.textCursor().selectedText() == "[Nett]"
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
