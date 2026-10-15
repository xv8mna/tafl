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
