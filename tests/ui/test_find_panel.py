"""The Find panel: the imports, exports and commands at a glance, finding steps by any of their
texts, filters, grouping, and going to a step (and its setting) in both views."""

import importlib.util
import time
from pathlib import Path

import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QLineEdit,
    QListWidget,
    QSpinBox,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from tafl.core import Connection, Parameter, Project, Status, StepResult, TableResult
from tafl.core import index as project_index
from tafl.ui import find_panel
from tafl.ui.find_panel import FindPanel
from tafl.ui.forms import FormContext, ParamForm, flash
from tafl.ui.session import json_ready

TOOLS = Path(__file__).resolve().parents[2] / "tools"


@pytest.fixture(autouse=True)
def fresh_find_settings(session):
    """Every test starts with the panel's defaults (the settings are shared by all tests)."""
    QSettings().remove("find")


@pytest.fixture
def panel(window):
    """The Find panel of a shown window; hidden again before the window closes, so that the saved
    window state doesn't open it in later tests."""
    window.resize(1500, 900)
    window.show()
    window.activateWindow()
    yield window.find_panel
    window.find_dock.hide()


def open_demo(window, session, settle, demo):
    window.open_path(demo)
    settle(session)
    window.open_find()
    QApplication.processEvents()


def titles(panel) -> list[str]:
    return [hit.title.text for hit in panel.hits()]


def current(panel) -> str:
    """The title of the current result ("" for none)."""
    hit = panel.current()
    return hit.title.text if hit is not None else ""


def sections(panel, nodes=None) -> list:
    """(title, [sections or result titles]) of the list, as shown."""
    nodes = panel.model.roots if nodes is None else nodes
    shown = []
    for node in nodes:
        if isinstance(node.item, project_index.Section):
            shown.append((node.item.title, sections(panel, node.children)))
        else:
            shown.append(node.item.title.text)
    return shown


def row_of(panel, title: str) -> int:
    return titles(panel).index(title)


def focused(qtbot, widget) -> None:
    qtbot.waitUntil(lambda: QApplication.focusWidget() is widget, timeout=2000)


def click(panel, index) -> None:
    point = panel.view.visualRect(index).center()
    QTest.mouseClick(panel.view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)


def test_ctrl_shift_f_opens_the_panel_with_the_cursor_in_the_search_field(window, panel, session, settle, demo, qtbot):
    window.open_path(demo)
    settle(session)
    assert window.find_dock.isHidden() and window.find_dock.objectName() == "Find"
    assert window.find_action.shortcut().toString() == "Ctrl+Shift+F"
    keys = Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier
    QTest.keyClick(window.steps_panel.list, Qt.Key.Key_F, keys)
    focused(qtbot, panel.search)
    assert window.find_dock.isVisible()
    assert panel.search.placeholderText() == "Find in project…  (Ctrl+Shift+F)"
    # The menus keep their order; the toolbar has Find after the Map button
    menus = window.menuBar().actions()
    assert [m.text() for m in menus] == ["&File", "&Edit", "&Project", "&View", "&Help"]
    edit, view = menus[1].menu(), menus[3].menu()
    assert edit.actions()[-3:] == [window.find_action, window.find_next_action, window.find_previous_action]
    assert edit.actions()[-4].isSeparator()
    assert view.actions()[-2:] == [window.results_action, window.find_panel_action]
    assert window.find_panel_action.text() == "Find panel"
    actions = window.findChild(QToolBar, "MainToolbar").actions()
    assert actions[actions.index(window.map_view_action) + 1] is window.find_action
    assert window.find_action.iconText() == "Find"
    QTest.keyClicks(panel.search, "kunden")
    qtbot.waitUntil(lambda: len(panel.hits()) == 3, timeout=2000)


def test_the_empty_search_lists_imports_exports_and_commands(window, panel, session, settle, demo):
    open_demo(window, session, settle, demo)
    assert sections(panel) == [
        ("Imports", ["Customers · 1 · Import Excel files", "Orders · 1 · Import CSV / text files"]),
        (
            "Exports",
            [
                "Orders · 9 · Export to Excel file",
                "Orders · 10 · Export to CSV / text file",
                "Revenue by customer · 5 · Export to Excel file",
            ],
        ),
    ]
    assert panel.summary.text() == "2 imports · 3 exports"
    excel = panel.hits()[0]
    assert excel.line.text == "customers.xlsx, sheet Kunden  in  data"
    assert str(demo.parent / "data" / "customers.xlsx") in excel.tooltip  # the full path
    assert [hit.entry.kind for hit in panel.hits()] == ["in", "in", "out", "out", "out"]
    # A file command is in Commands
    session.select_table(session.project.table("Orders").id)
    session.add_step(session.registry.actions["files.mkdir"], params={"folder": "archive"})
    panel.refresh()
    assert sections(panel)[2] == ("Commands", ["Orders · 11 · Create folder"])
    assert panel.summary.text() == "2 imports · 3 exports · 1 command"
    session.new_project()
    panel.refresh()
    assert panel.hits() == [] and panel.summary.text() == "No imports, exports or commands yet"


def test_typing_finds_files_formulas_notes_and_names(window, panel, session, settle, demo, qtbot):
    open_demo(window, session, settle, demo)
    panel.set_text("orders xlsx")  # all words, in any of the step's texts
    assert titles(panel) == ["Orders · 9 · Export to Excel file"]
    assert panel.hits()[0].line.text == "File: output/orders.xlsx"
    assert panel.summary.text() == "1 step in 1 table"
    panel.set_text("ROUND")  # a formula
    hit = panel.hits()[0]
    assert titles(panel) == ["Orders · 6 · Calculate columns"]
    assert hit.line.text == "Formula [Gross]: ROUND([Net] * 1.19, 2)" and hit.focus == ("columns", 1, "formula")
    assert (17, 22, "match") in hit.line.marks and (0, 16, "label") in hit.line.marks
    panel.set_text("gross")  # column names, also in other tables
    assert titles(panel) == [
        "Orders · 9 · Export to Excel file",
        "Orders · 6 · Calculate columns",
        "Revenue by customer · 2 · Aggregate",
    ]
    assert sections(panel)[0][0] == "Exports" and panel.summary.text() == "3 steps in 2 tables"
    orders = session.project.table("Orders")
    session.set_note(orders.steps[2].id, "Cancelled orders are left out")
    panel.set_text("cancelled")
    assert titles(panel) == ["Orders · 3 · Filter rows"]
    assert panel.hits()[0].line.text == "Note: Cancelled orders are left out"
    assert panel.hits()[0].focus == (project_index.NOTE, 0, "")
    panel.set_text("revenue by")  # a table's name: all its steps
    assert len(panel.hits()) == 5
    assert panel.hits()[0].title.marks == ((0, 7, "match"), (8, 10, "match"))
    # Typing searches a moment after the last key
    panel.search.clear()
    QTest.keyClicks(panel.search, "kundennr")
    assert len(panel.hits()) == 5
    qtbot.waitUntil(lambda: len(panel.hits()) == 2, timeout=2000)


def test_upper_and_lower_case_and_regular_expressions(window, panel, session, settle, demo):
    open_demo(window, session, settle, demo)
    panel.set_text("Round")
    assert titles(panel) == ["Orders · 6 · Calculate columns"]
    panel.case_button.setChecked(True)
    assert titles(panel) == [] and panel.summary.text() == "Nothing found"
    panel.set_text("ROUND")
    assert titles(panel) == ["Orders · 6 · Calculate columns"]
    panel.case_button.setChecked(False)
    panel.regex_button.setChecked(True)
    panel.set_text(r"\[net\] \* 1\.19")
    assert titles(panel) == ["Orders · 6 · Calculate columns"]
    panel.set_text("([")  # a mistake: said in the summary
    assert titles(panel) == [] and panel.summary.text().startswith("Not a valid pattern: ")
    other = FindPanel(session)  # remembered
    assert other.regex_button.isChecked() and not other.case_button.isChecked()
    other.deleteLater()


def test_a_connection_finds_its_steps(window, panel, session, settle, tmp_path):
    project = Project(name="Shop")
    project.connections = [Connection("Shop", "sqlite", {"file": "shop.db"})]
    table = project.add_table("Orders")
    project.add_step(table, "db.import", {"connection": "Shop", "mode": "query", "query": "SELECT * FROM orders"})
    window.open_path(project.save(tmp_path / "shop.tafl"))
    window.open_find()
    assert [title for title, _ in sections(panel)] == ["Imports"]
    assert panel.hits()[0].line.text == "SQL on Shop: SELECT * FROM orders"
    panel.set_text("shop.db")  # the connection's settings
    hit = panel.hits()[0]
    assert hit.line.text == "Shop: SQLite, shop.db" and hit.focus == ("connection", 0, "")
    panel.set_text("")
    panel.set_group_by("place")
    assert sections(panel) == [("Shop (SQLite: shop.db)", [("SQL", ["Orders · 1 · Import from database"])])]


def test_a_click_selects_the_table_and_step_in_the_tables_view(window, panel, session, settle, demo, qtbot):
    open_demo(window, session, settle, demo)
    gone = []
    panel.go_to.connect(lambda table_id, step_id: gone.append(step_id))
    panel.set_text("xlsx")
    row = row_of(panel, "Revenue by customer · 5 · Export to Excel file")
    panel.activate(row)
    revenue = session.project.table("Revenue by customer")
    assert session.table_id == revenue.id and session.step.id == revenue.steps[4].id
    assert window.steps_panel.list.currentRow() == 4 and gone == [revenue.steps[4].id]
    assert current(panel) == "Revenue by customer · 5 · Export to Excel file"
    # A real click on another result
    index = panel.model.index_of(panel.model.results()[row_of(panel, "Customers · 1 · Import Excel files")])
    panel.view.scrollTo(index)
    click(panel, index)
    assert session.table.name == "Customers" and session.step_index == 0
    focused(qtbot, panel.view)  # the focus stays in the list
    # A click on a header closes and opens it
    header = panel.model.index_of(panel.model.roots[0])
    click(panel, header)
    assert not panel.view.isExpanded(header)
    qtbot.wait(QApplication.doubleClickInterval() + 50)  # two clicks, not a double click
    click(panel, header)
    assert panel.view.isExpanded(header)


def test_a_click_on_the_map_opens_a_collapsed_group_and_centres_the_box(window, panel, session, settle, demo, qtbot):
    open_demo(window, session, settle, demo)
    window.show_map(True)
    settle(session)
    customers, orders, _revenue = session.project.tables
    group = session.add_group("Inputs", [customers.id, orders.id])
    view = window.map_view
    view.set_collapsed(group.id, True)
    view.rebuild()
    assert orders.id not in view.nodes
    panel.set_text("orders.csv")
    panel.activate(row_of(panel, "Orders · 1 · Import CSV / text files"))
    qtbot.waitUntil(lambda: orders.id in view.nodes, timeout=2000)
    assert not session.project.group(group.id).collapsed
    assert session.table_id == orders.id and session.step.id == orders.steps[0].id
    node = view.nodes[orders.id]
    assert node.isSelected()
    visible = view.canvas.mapToScene(view.canvas.viewport().rect()).boundingRect()
    assert visible.contains(node.box_rect().center())
    assert window.views.currentWidget() is view  # the map stays


@pytest.mark.parametrize("on_map", [False, True])
def test_enter_moves_the_focus_to_the_setting_that_matched(window, panel, session, settle, demo, qtbot, on_map):
    open_demo(window, session, settle, demo)
    window.show_map(on_map)
    settings = window.map_view.settings if on_map else window.settings_panel
    undo = len(session._undo)
    panel.set_text("data/orders.csv")
    index = panel.model.index_of(panel.model.results()[0])
    panel.view.setFocus()
    panel.view.setCurrentIndex(index)
    QTest.keyClick(panel.view, Qt.Key.Key_Return)
    editor = settings.form.editor("file")
    focused(qtbot, editor._edit)
    assert session.table.name == "Orders" and session.step_index == 0
    assert editor._edit.selectedText() == "data/orders.csv" and editor._edit.property("found") is True
    label = next(label for _, label, shown, _ in settings.form._rows if shown is editor)
    assert label.property("found") is True
    qtbot.waitUntil(lambda: editor._edit.property("found") is False, timeout=3000)  # for a moment
    # A field in a row of a list setting
    panel.set_text("kundennr")
    panel.activate(row_of(panel, "Orders · 2 · Rename columns"), focus=True)
    column = settings.form.editor("columns").row_editor(2, "column")
    focused(qtbot, column._combo)
    assert isinstance(QApplication.focusWidget(), QComboBox) and column._combo.currentText() == "Kundennr"
    assert column._combo.lineEdit().selectedText() == "Kundennr" and column._combo.property("found") is True
    # The note
    session.set_note(session.project.table("Orders").steps[2].id, "Cancelled orders are left out")
    undo += 1
    panel.set_text("cancelled")
    panel.activate(0, focus=True)
    focused(qtbot, settings.note)
    assert settings.note.selectedText() == "Cancelled orders are left out" and settings.note.property("found") is True
    # Leaving the fields without typing changes nothing
    panel.search.setFocus()
    focused(qtbot, panel.search)
    assert len(session._undo) == undo


def test_the_setting_of_an_overview_result_and_steps_without_such_setting(window, panel, session, settle, demo, qtbot):
    open_demo(window, session, settle, demo)
    asked = []
    panel.focus_setting.connect(lambda *focus: asked.append(focus))
    panel.activate(row_of(panel, "Orders · 9 · Export to Excel file"), focus=True)
    assert asked == [("file", 0, "")]
    focused(qtbot, window.settings_panel.form.editor("file")._edit)
    assert window.settings_panel.focus_setting("no such setting") is False
    assert window.settings_panel.focus_setting(project_index.SUMMARY) is False
    assert window.settings_panel.focus_setting("sheet") is True  # a setting the search didn't find


def test_f3_goes_through_the_results_and_wraps(window, panel, session, settle, demo, qtbot):
    open_demo(window, session, settle, demo)
    first, second, *_, last = panel.hits()
    window.find_next_action.trigger()
    assert current(panel) == first.title.text and session.step.id == first.entry.step_id
    QTest.keyClick(window.steps_panel.list, Qt.Key.Key_F3)
    assert current(panel) == second.title.text and session.step.id == second.entry.step_id
    window.find_previous_action.trigger()
    window.find_previous_action.trigger()
    assert current(panel) == last.title.text and session.step.id == last.entry.step_id  # wraps
    window.find_next_action.trigger()
    assert current(panel) == first.title.text
    # A collapsed section opens
    exports = panel.model.index_of(panel.model.roots[1])
    panel.view.collapse(exports)
    window.find_previous_action.trigger()
    assert panel.view.isExpanded(exports) and current(panel) == last.title.text
    # A hidden panel is shown (the focus stays where it is)
    window.find_dock.hide()
    window.steps_panel.list.setFocus()
    focused(qtbot, window.steps_panel.list)
    window.find_next_action.trigger()
    assert window.find_dock.isVisible() and current(panel) == first.title.text
    assert window.steps_panel.list.hasFocus()
    # Enter in the search field goes on, and the focus stays there
    panel.focus_search()
    focused(qtbot, panel.search)
    QTest.keyClick(panel.search, Qt.Key.Key_Return)
    assert current(panel) == second.title.text and panel.search.hasFocus()
    QTest.keyClick(panel.search, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
    assert current(panel) == first.title.text
    panel.set_text("nothing like this")
    window.find_next_action.trigger()
    assert window.statusBar().currentMessage() == "No results in the Find panel"


def test_f3_goes_on_from_a_result_that_left_the_list(window, panel, session, tmp_path):
    """The current result leaves the list when its problem is fixed or its note no longer matches:
    F3 and Shift+F3 go on from its place. Before, they started again at the first (or last)."""
    project = Project(name="Notes")
    for number in range(1, 7):
        project.add_step(project.add_table(f"T{number}"), "csv.import", {"file": f"t{number}.csv"}).note = "TODO check"
    window.open_path(project.save(tmp_path / "notes.tafl"))
    window.open_find()
    panel.set_text("todo")
    for _ in range(3):
        window.find_next_action.trigger()
    assert current(panel) == "T3 · 1 · Import CSV / text files"

    def checked() -> None:
        session.set_note(panel.current().entry.step_id, "Checked")
        panel.refresh()
        assert current(panel) == "" and not panel.view.currentIndex().isValid()

    checked()
    assert [title.split(" ")[0] for title in titles(panel)] == ["T1", "T2", "T4", "T5", "T6"]
    window.find_next_action.trigger()
    assert current(panel) == "T4 · 1 · Import CSV / text files" and session.table.name == "T4"
    checked()
    window.find_previous_action.trigger()
    assert current(panel) == "T2 · 1 · Import CSV / text files"
    panel.activate(len(panel.hits()) - 1)
    checked()  # the last one: F3 goes on with the first
    window.find_next_action.trigger()
    assert current(panel) == "T1 · 1 · Import CSV / text files"
    checked()
    session.undo()  # back in the list: the current result again
    panel.refresh()
    assert current(panel) == "T1 · 1 · Import CSV / text files"
    session.redo()
    panel.refresh()
    window.find_previous_action.trigger()  # the first one left: Shift+F3 goes on with the last
    assert current(panel) == "T5 · 1 · Import CSV / text files"
    # Another search starts at its first result
    panel.set_text("check")
    session.set_note(panel.current().entry.step_id, "Done")
    panel.refresh()
    assert current(panel) == "" and len(panel.hits()) == 5
    panel.set_text("checked")
    window.find_next_action.trigger()
    assert titles(panel)[0] == current(panel) == "T1 · 1 · Import CSV / text files"


def test_f3_after_a_fix_goes_to_the_next_error_of_the_same_table(window, panel, session, settle, tmp_path):
    """Fixing the first error of a table shows its next one at the same place in the list: F3 goes
    there. Before, F3 went on with the next table's error and skipped it."""
    (tmp_path / "d.csv").write_text("x\n1\n2\n", encoding="utf-8")
    project = Project(name="Problems")
    for number in range(1, 6):
        table = project.add_table(f"T{number}")
        project.add_step(table, "csv.import", {"file": "d.csv"})
        for column in ("a", "b"):  # two errors: the second step is blocked by the first
            project.add_step(table, "rows.filter", {"conditions": [{"column": column, "operator": "=", "value": "1"}]})
    window.open_path(project.save(tmp_path / "problems.tafl"))
    window.open_find()
    panel.problems_button.setChecked(True)
    settle(session)
    panel.refresh()
    assert titles(panel) == [f"T{number} · 2 · Filter rows" for number in range(1, 6)]
    for by in ("kind", "table"):
        panel.set_group_by(by)
        panel.activate(2)
        step = panel.current().entry.step_id
        session.set_params(step, {"conditions": [{"column": "x", "operator": "=", "value": "1"}]})
        session.calculate_now()
        settle(session)
        panel.refresh()
        assert current(panel) == "" and titles(panel)[2] == "T3 · 3 · Filter rows"
        window.find_next_action.trigger()
        assert current(panel) == "T3 · 3 · Filter rows"
        session.undo()
        session.calculate_now()
        settle(session)
        panel.refresh()
        assert titles(panel)[2] == "T3 · 2 · Filter rows"


def test_f3_goes_on_from_a_result_that_left_the_list_in_another_grouping(window, panel, session, tmp_path):
    """The list is grouped anew after the current result left it: F3 and Shift+F3 still go on from
    its place. Before, they went to the first (or the last) result."""
    project = Project(name="Notes")
    for number in range(1, 7):
        project.add_step(project.add_table(f"T{number}"), "csv.import", {"file": f"t{number}.csv"}).note = "TODO check"
    window.open_path(project.save(tmp_path / "notes.tafl"))
    window.open_find()
    panel.set_text("todo")

    def left(row: int, by: str) -> None:
        panel.activate(row)
        session.set_note(panel.current().entry.step_id, "Checked")
        panel.refresh()
        panel.set_group_by(by)
        assert current(panel) == ""

    left(2, "table")  # T3
    window.find_next_action.trigger()
    assert current(panel) == "T4 · 1 · Import CSV / text files"
    left(2, "place")  # T4: the files are below the project folder, by name
    window.find_previous_action.trigger()
    assert current(panel) == "T2 · 1 · Import CSV / text files"
    # A deleted step: F3 goes on with the result after it then
    session.delete_step(panel.current().entry.step_id)
    panel.refresh()
    panel.set_group_by("kind")
    assert current(panel) == ""
    window.find_next_action.trigger()
    assert current(panel) == "T5 · 1 · Import CSV / text files"


def test_keys_in_the_search_field_and_the_list(window, panel, session, settle, demo, qtbot):
    open_demo(window, session, settle, demo)
    panel.focus_search()
    focused(qtbot, panel.search)
    QTest.keyClick(panel.search, Qt.Key.Key_Down)  # into the list, on the first result
    focused(qtbot, panel.view)
    hits = titles(panel)
    assert current(panel) == hits[0]
    QTest.keyClick(panel.view, Qt.Key.Key_Down)  # the next result (past the header): selected in the window
    QTest.keyClick(panel.view, Qt.Key.Key_Down)
    assert current(panel) == hits[2] and session.step.id == panel.current().entry.step_id
    for _ in range(5):
        QTest.keyClick(panel.view, Qt.Key.Key_Down)
    assert current(panel) == hits[-1]  # no wrap
    QTest.keyClick(panel.view, Qt.Key.Key_Up)
    assert current(panel) == hits[-2]
    QTest.keyClick(panel.view, Qt.Key.Key_Escape)  # back to the search field
    focused(qtbot, panel.search)
    QTest.keyClicks(panel.search, "orders")
    QTest.keyClick(panel.search, Qt.Key.Key_Escape)  # clears the search
    assert panel.search.text() == "" and len(panel.hits()) == 5
    QTest.keyClick(panel.search, Qt.Key.Key_Escape)  # an empty field: back to the view
    focused(qtbot, window.steps_panel.list)
    window.show_map(True)
    panel.focus_search()
    focused(qtbot, panel.search)
    QTest.keyClick(panel.search, Qt.Key.Key_Escape)
    focused(qtbot, window.map_view.canvas)


def test_filters_off_and_changed(window, panel, session, settle, demo):
    open_demo(window, session, settle, demo)
    orders = session.project.table("Orders")
    revenue = session.project.table("Revenue by customer")
    panel.off_button.setChecked(True)
    assert titles(panel) == [] and panel.summary.text() == "Nothing found"
    session.set_enabled(orders.steps[2].id, False)
    session.set_table_enabled([revenue.id], False)
    panel.refresh()
    assert titles(panel)[:2] == ["Revenue by customer · 5 · Export to Excel file", "Orders · 3 · Filter rows"]
    assert len(panel.hits()) == 6
    assert {hit.status for hit in panel.hits()} == {"off"}
    panel.set_group_by("table")
    assert [title for title, _ in sections(panel)] == ["Orders", "Revenue by customer (off)"]
    panel.set_group_by("kind")
    panel.off_button.setChecked(False)
    panel.changed_button.setChecked(True)  # changed since the project was opened
    assert titles(panel)[:2] == ["Revenue by customer · 5 · Export to Excel file", "Orders · 3 · Filter rows"]
    assert len(panel.hits()) == 6
    session.undo()
    session.undo()
    panel.refresh()
    assert titles(panel) == []
    session.set_params(orders.steps[0].id, {"file": "data/other.csv"})
    panel.refresh()
    assert titles(panel) == ["Orders · 1 · Import CSV / text files"]
    panel.set_text("customers")  # with a text too
    assert titles(panel) == []


def test_a_setting_typed_back_is_not_changed(window, panel, session, settle, demo):
    """The demo saves its first steps with only some of their settings, and the settings form
    writes all of them. Once the file is typed back, "Changed" doesn't list the step: before, the
    settings the form had added with their defaults made it differ."""
    open_demo(window, session, settle, demo)
    orders = session.project.table("Orders")
    first = orders.steps[0]
    assert first.params == {"file": "data/orders.csv"}
    session.select_table(orders.id)
    session.select_step(first.id)
    panel.changed_button.setChecked(True)
    for file, changed in (("data/other.csv", ["Orders · 1 · Import CSV / text files"]), ("data/orders.csv", [])):
        form = window.settings_panel.form
        form.editor("file").set_value(file)
        form.changed.emit(form.values())
        panel.refresh()
        assert session.find_step(first.id)[1].params["file"] == file and titles(panel) == changed
    assert len(session.find_step(first.id)[1].params) > 1  # all of its settings now


def test_filter_uses_a_parameter_and_paths_with_parameters(window, panel, session, demo):
    project = Project.load(demo)
    project.parameters = [Parameter("Month", "2026-10"), Parameter("Day", "01")]
    orders = project.table("Orders")
    orders.steps[-1].params["file"] = "output/orders_{Month}.csv"
    window.open_path(project.save(demo))
    window.open_find()
    export = next(hit for hit in panel.hits() if hit.title.text == "Orders · 10 · Export to CSV / text file")
    assert export.line.text == "orders_{Month}.csv  in  output   now: orders_2026-10.csv"
    assert (7, 14, "parameter") in export.line.marks and (33, 37, "label") in export.line.marks
    panel.set_text("orders_2026-10")  # the values now are found too
    assert titles(panel) == ["Orders · 10 · Export to CSV / text file"]
    assert panel.hits()[0].line.text == "File: output/orders_{Month}.csv   now: output/orders_2026-10.csv"
    panel.set_text("")
    panel.choose_parameter("")  # any parameter
    assert panel.parameter_button.isChecked() and panel.parameter_button.text() == "Uses {…}"
    assert titles(panel) == ["Orders · 10 · Export to CSV / text file"]
    assert panel.hits()[0].line.text.startswith("File: output/orders_{Month}.csv")
    panel.choose_parameter("Day")
    assert panel.parameter_button.text() == "Uses {Day}" and titles(panel) == []
    panel.choose_parameter("Month")
    assert titles(panel) == ["Orders · 10 · Export to CSV / text file"]
    menu = panel._parameters_menu
    panel._fill_parameters()  # the menu, without opening it
    choices = [action for action in menu.actions() if not action.isSeparator()]
    assert [a.text() for a in choices] == ["Any parameter", "{Month}", "{Day}"]
    assert [a.isChecked() for a in choices] == [False, True, False]
    choices[0].trigger()
    assert panel.parameter_button.text() == "Uses {…}" and panel.query().parameter == ""
    panel.parameter_button.setChecked(False)
    assert panel.query().parameter is None and len(panel.hits()) == 5


def test_uses_a_parameter_follows_its_rename(window, panel, session, demo):
    """A rename writes {New} for {Old} in the steps: "Uses {Day}" follows it to {Date}, and back
    with Undo. Before, it became "any parameter" and listed the steps of the other parameters
    too. A parameter that is removed leaves "any parameter"."""
    project = Project.load(demo)
    project.parameters = [Parameter("Day", "01"), Parameter("Folder", "output")]
    orders = project.table("Orders")
    orders.steps[-1].params["file"] = "output/orders_{Day}.csv"
    orders.steps[-2].params["file"] = "{Folder}/orders.xlsx"
    window.open_path(project.save(demo))
    window.open_find()
    export = "Orders · 10 · Export to CSV / text file"
    panel.choose_parameter("Day")
    assert titles(panel) == [export]
    session.set_parameters([Parameter("Date", "01"), Parameter("Folder", "output")], {"Day": "Date"})
    panel.refresh()
    assert panel.parameter_button.text() == "Uses {Date}" and titles(panel) == [export]
    assert session.project.table("Orders").steps[-1].params["file"] == "output/orders_{Date}.csv"
    session.undo()
    panel.refresh()
    assert panel.parameter_button.text() == "Uses {Day}" and titles(panel) == [export]
    # Removed, and another one added: the steps still have {Day}
    session.set_parameters([Parameter("Date", "01"), Parameter("Folder", "output")])
    panel.refresh()
    assert panel.parameter_button.text() == "Uses {…}" and panel.parameter_button.isChecked()
    assert titles(panel) == ["Orders · 9 · Export to Excel file"]  # {Day} is no parameter now


def test_the_problems_filter_calculates_all_tables(window, panel, session, settle, demo):
    window.open_path(demo)
    settle(session)
    window.open_find()
    orders = session.project.table("Orders")
    session.select_table(orders.id)
    session.add_step(session.registry.actions["rows.filter"])  # not complete yet
    settle(session)
    session.select_table(session.project.tables[0].id)
    settle(session)
    assert session.project.tables[2].id not in session.results  # the tables view calculates the selected table
    panel.problems_button.setChecked(True)
    assert "calculating all tables" in panel.summary.text()
    settle(session)
    assert set(session.results) == {t.id for t in session.project.tables}
    panel.refresh()
    assert titles(panel) == ["Orders · 11 · Filter rows", "Revenue by customer · 1 · Start from another table"]
    incomplete, waiting = panel.hits()
    assert incomplete.status == "incomplete" and incomplete.line.text.startswith("Not complete: fill in Conditions")
    assert waiting.status == "waiting" and waiting.line.text.startswith("Waiting: waits for “Orders”")
    assert panel.summary.text() == "2 steps in 2 tables"


@pytest.mark.parametrize("by", ["selection", "edit"])
def test_the_problems_filter_goes_on_after_a_selection_or_an_edit(window, panel, session, settle, demo, by):
    """A selection or an edit while the Problems filter calculates all tables calculates the
    selected table first, and then the others. Before, the others were left out for good, and the
    filter said "Nothing found"."""
    project = Project.load(demo)
    revenue = project.table("Revenue by customer")
    project.add_step(revenue, "rows.filter", {"conditions": [{"column": "No such column", "operator": "=", "value": "1"}]})
    window.open_path(project.save(demo))
    settle(session)
    window.open_find()
    panel.problems_button.setChecked(True)
    assert titles(panel) == [] and panel.summary.text() == "Calculating all tables…"
    orders = session.project.table("Orders")
    if by == "selection":
        session.select_table(orders.id)
    else:
        session.set_note(session.table.steps[0].id, "An edit")
        session.calculate_now()  # what the edit starts a moment later
    settle(session)
    assert set(session.results) == {t.id for t in session.project.tables}
    panel.refresh()
    assert titles(panel) == ["Revenue by customer · 6 · Filter rows"] and panel.summary.text() == "1 step in 1 table"


def test_step_problems_come_from_the_results():
    project = Project(name="Problems")
    a, b, c, d, off, new = (project.add_table(name) for name in ("A", "B", "C", "D", "Off", "New"))
    a1, a2 = project.add_step(a, "csv.import", {"file": "a.csv"}), project.add_step(a, "rows.sort", {})
    b1, b2 = project.add_step(b, "csv.import", {"file": "b.csv"}), project.add_step(b, "rows.filter", {})
    c1 = project.add_step(c, "table.derive", {"table": b.id})
    d1 = project.add_step(d, "table.derive", {"table": d.id})
    off1 = project.add_step(off, "csv.import", {"file": "off.csv"})
    project.add_step(new, "csv.import", {"file": "new.csv"})
    off.enabled = False
    warned = [("info", "Encoding utf-8"), ("warning", "Column X is empty")]

    class Results:  # what step_problems uses of a session
        def __init__(self):
            self.project = project
            self.results = {
                a.id: TableResult(a.id, [StepResult(a1.id, Status.ERROR, error="File not found"), StepResult(a2.id, Status.BLOCKED)]),
                b.id: TableResult(
                    b.id,
                    [
                        StepResult(b1.id, Status.OK, messages=warned),
                        StepResult(b2.id, Status.ERROR, error="Conditions, row 1 – Column: required"),
                    ],
                ),
                c.id: TableResult(c.id, [StepResult(c1.id, Status.ERROR, error="“B” has an error", inherited=True)]),
                d.id: TableResult(d.id, [], error="Circular reference: D → D"),
                off.id: TableResult(off.id, [StepResult(off1.id, Status.ERROR, error="x")], skipped="turned off"),
            }

        def waits_for(self, table_id):
            return ("B", 2, ["Conditions, row 1 – Column"]) if table_id == c.id else None

    assert find_panel.step_problems(Results()) == {
        a1.id: ("error", "File not found"),  # not the blocked step after it
        b1.id: ("warning", "Column X is empty"),
        b2.id: ("incomplete", "fill in Conditions, row 1 – Column"),
        c1.id: ("waiting", "waits for “B”, whose step 2 is not complete yet"),
        d1.id: ("error", "Circular reference: D → D"),  # the table as a whole: at its first step
    }


def test_group_by_table_and_place(window, panel, session, demo):
    project = Project.load(demo)
    customers, orders, _revenue = project.tables
    group = project.add_group("Inputs", "green")
    customers.group = orders.group = group.id
    inbox = project.add_table("Inbox")
    project.add_step(inbox, "csv.import", {"source": "folder", "folder": "data", "pattern": "*.csv"})
    project.add_step(inbox, "files.move", {"which": "imported", "target": "archive"})
    project.add_step(inbox, "csv.export", {"file": str(demo.parent / "data" / "copy.csv")})  # other spellings
    project.add_step(inbox, "csv.export", {"file": "./data/again.csv"})
    window.open_path(project.save(demo))
    window.open_find()
    panel.set_group_by("table")
    inputs = [
        ("Customers", ["Customers · 1 · Import Excel files"]),
        ("Orders", ["Orders · 1 · Import CSV / text files", "Orders · 9 · Export to Excel file", "Orders · 10 · Export to CSV / text file"]),
    ]
    others = [
        ("Revenue by customer", ["Revenue by customer · 5 · Export to Excel file"]),
        (
            "Inbox",
            [
                "Inbox · 1 · Import CSV / text files",
                "Inbox · 2 · Move files",
                "Inbox · 3 · Export to CSV / text file",
                "Inbox · 4 · Export to CSV / text file",
            ],
        ),
    ]
    assert sections(panel) == [("Inputs", inputs), ("Not in a group", others)]
    assert panel.model.roots[0].item.color == "green"
    assert not panel._delegate.groups  # the sections are the groups
    panel.set_group_by("place")
    assert panel._delegate.groups
    shown = dict(sections(panel))
    assert list(shown) == ["archive", "data", "output"]
    assert dict(shown["data"]) == {
        "*.csv": ["Inbox · 1 · Import CSV / text files", "Inbox · 2 · Move files"],  # the files the import read
        "again.csv": ["Inbox · 4 · Export to CSV / text file"],
        "copy.csv": ["Inbox · 3 · Export to CSV / text file"],
        "customers.xlsx": ["Customers · 1 · Import Excel files"],
        "orders.csv": ["Orders · 1 · Import CSV / text files"],
    }
    assert dict(shown["archive"]) == {"The folder itself": ["Inbox · 2 · Move files"]}
    assert len(panel.hits()) == 10  # the move is there twice: what it moves, and where to
    assert panel.model.roots[1].item.tooltip == str(demo.parent / "data")
    assert QSettings().value("find/group_by") == "place"
    other = FindPanel(session)  # remembered
    assert other.group_by == "place" and other.group_buttons["place"].isChecked()
    other.deleteLater()


def test_collapsed_sections_and_the_current_result_are_kept(window, panel, session, settle, demo):
    open_demo(window, session, settle, demo)
    resets = []
    panel.model.modelReset.connect(lambda: resets.append(1))
    assert panel.view.model() is panel.model  # no proxy model
    panel.activate(3)
    shown = current(panel)
    scrolled = panel.view.verticalScrollBar().value()
    panel.refresh()
    session.results_changed.emit()
    panel.refresh()
    assert resets == [] and current(panel) == shown and panel.view.verticalScrollBar().value() == scrolled
    panel.view.collapse(panel.model.index_of(panel.model.roots[0]))
    panel.set_text("o")  # other results: a new list
    assert resets == [1]
    assert not panel.view.isExpanded(panel.model.index_of(panel.model.roots[0]))  # Imports stays collapsed
    assert current(panel) == shown
    panel.set_text("")
    assert not panel.view.isExpanded(panel.model.index_of(panel.model.roots[0]))
    assert panel.view.isExpanded(panel.model.index_of(panel.model.roots[1]))


def test_another_project_clears_the_search_and_the_filters(window, panel, session, settle, demo):
    open_demo(window, session, settle, demo)
    panel.case_button.setChecked(True)
    panel.set_group_by("table")
    panel.off_button.setChecked(True)
    panel.set_text("orders")
    session.new_project()
    session.open_project(demo)
    QApplication.processEvents()
    assert panel.search.text() == "" and panel.query() == project_index.Query(case=True)
    assert panel.group_by == "table" and panel.case_button.isChecked()
    panel.refresh()
    assert len(panel.hits()) == 5


def test_the_hidden_panel_does_nothing(window, session, settle, demo, monkeypatch, qtbot):
    built = []
    real = project_index.build
    monkeypatch.setattr(project_index, "build", lambda engine: built.append(1) or real(engine))
    window.show()
    window.open_path(demo)
    settle(session)
    session.set_note(session.project.tables[0].steps[0].id, "a note")
    qtbot.wait(300)
    assert built == []
    window.open_find()
    assert built == [1]
    session.set_note(session.project.tables[0].steps[0].id, "another note")
    qtbot.waitUntil(lambda: built == [1, 1], timeout=2000)
    window.find_dock.hide()


def test_the_panel_is_narrow_enough(window, panel, session, settle, demo):
    open_demo(window, session, settle, demo)
    assert panel.minimumSizeHint().width() <= 240
    flow = panel.layout().itemAt(1).layout()  # the filter buttons go on in a second row
    buttons = [panel.problems_button, panel.off_button, panel.changed_button, panel.parameter_button]
    row = max(button.sizeHint().height() for button in buttons)
    assert flow.heightForWidth(2000) == row and flow.heightForWidth(150) > row
    assert flow.minimumSize().width() == max(button.minimumSizeHint().width() for button in buttons)


def test_speed_with_the_big_project(window, panel, session, tmp_path):
    spec = importlib.util.spec_from_file_location("make_big_project", TOOLS / "make_big_project.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    project = module.build_project()
    project.parameters = [Parameter("Year", "2026"), Parameter("Today", "=TODAY()")]
    window.open_path(project.save(tmp_path / "big.tafl"))
    window.open_find()
    assert len(panel.hits()) == 22  # 12 imports, 10 exports
    times = {}
    for text in ("", "orders", "orders csv", "x"):
        panel.set_text(text)
        best = 1e9
        for _ in range(3):
            panel._show([])  # a new list each time
            started = time.perf_counter()
            panel.refresh()
            best = min(best, time.perf_counter() - started)
        times[text] = best
    print("refresh:", {text: f"{seconds * 1000:.1f} ms" for text, seconds in times.items()})
    assert all(seconds < 0.25 for seconds in times.values()), times
    panel._stale = True
    started = time.perf_counter()
    panel.refresh()
    print(f"index built and refresh: {(time.perf_counter() - started) * 1000:.1f} ms")
    assert time.perf_counter() - started < 0.5


def test_a_form_focuses_any_kind_of_setting_and_marks_it(qapp, registry, qtbot):
    host = QWidget()
    qtbot.addWidget(host)
    layout = QVBoxLayout(host)
    forms = {}
    for action_id in ("text.clean", "calc.columns", "db.import"):
        action = registry.actions[action_id]
        forms[action_id] = form = ParamForm(action.params, FormContext(connections=["Shop"]), host)
        form.set_values(json_ready(action.resolve({"columns": [{"name": "A", "formula": "1"}, {"name": "B", "formula": "2"}]})))
        layout.addWidget(form)
    host.show()
    host.activateWindow()
    clean, calc, db = forms.values()
    for key, kind in (("columns", QListWidget), ("trim", QComboBox), ("collapse_spaces", QCheckBox), ("pad_length", QSpinBox)):
        widget = clean.focus(key)
        assert isinstance(widget, kind), key
        focused(qtbot, widget)
    widget = clean.focus("pad_char")  # a line edit: its text selected
    assert isinstance(widget, QLineEdit) and widget.selectedText() == widget.text() != ""
    formula = calc.editor("columns").row_editor(2, "formula")
    assert calc.focus("columns", 2, "formula") is formula._edit  # an editor's focus proxy
    focused(qtbot, formula._edit)
    assert calc.focus("columns") is calc.editor("columns").row_editor(1, "name")._edit
    assert db.focus("query") is None  # not shown now (a table is read)
    assert db.focus("no such setting") is None
    assert db.focus("connection") is db.editor("connection")._combo
    flash([db.editor("table")._edit], msecs=20)
    assert db.editor("table")._edit.property("found") is True
    qtbot.waitUntil(lambda: db.editor("table")._edit.property("found") is False, timeout=2000)
    gone = QLineEdit()
    flash([gone], msecs=20)
    gone.deleteLater()  # deleted before the mark ends: nothing happens
    qtbot.wait(50)


def test_save_as_into_another_folder_changes_no_step(window, panel, session, settle, demo, tmp_path):
    """Save as rewrites relative paths for the new folder: "Changed" doesn't list those steps,
    but still lists a step changed before."""
    open_demo(window, session, settle, demo)
    orders = session.project.table("Orders")
    session.set_note(orders.steps[2].id, "Check the status")
    (tmp_path / "elsewhere").mkdir()
    session.save_project(tmp_path / "elsewhere" / "copy.tafl")
    assert orders.steps[0].params["file"] != "data/orders.csv"  # rebased
    panel.changed_button.setChecked(True)
    panel.refresh()
    assert titles(panel) == ["Orders · 3 · Filter rows"]
    session.set_params(orders.steps[0].id, {"file": "data/other.csv"})  # a change after it counts
    panel.refresh()
    assert titles(panel) == ["Orders · 1 · Import CSV / text files", "Orders · 3 · Filter rows"]


@pytest.mark.parametrize("edited", ["data/b.csv", "outside"], ids=["relative", "outside-both-folders"])
def test_an_edit_undone_after_save_as_into_another_folder_is_no_change(window, panel, session, tmp_path, edited):
    """The steps as opened get the paths of the new folder too, as the steps and the undo steps
    do: a step edited before Save as and undone afterwards is as opened. Before, "Changed" still
    listed it. Also when Save as rewrote no path of the steps as they are now (a full path)."""
    project = Project(name="x")
    first = project.add_step(project.add_table("T"), "csv.import", {"file": "data/a.csv"})
    window.open_path(project.save(tmp_path / "here" / "x.tafl"))
    window.open_find()
    QApplication.processEvents()  # the steps as opened are taken
    panel.changed_button.setChecked(True)
    file = str(tmp_path / "outside" / "b.csv") if edited == "outside" else edited
    session.set_params(first.id, {"file": file})
    (tmp_path / "other").mkdir()
    session.save_project(tmp_path / "other" / "x.tafl")
    panel.refresh()
    assert titles(panel) == ["T · 1 · Import CSV / text files"]
    session.undo()
    assert Path(session.find_step(first.id)[1].params["file"]).resolve() == (tmp_path / "here" / "data" / "a.csv").resolve()
    panel.refresh()
    assert titles(panel) == []
    session.save_project(tmp_path / "here" / "x.tafl")  # back: relative again, still as opened
    assert session.find_step(first.id)[1].params["file"] == "data/a.csv"
    panel.refresh()
    assert titles(panel) == []
    session.redo()
    panel.refresh()
    assert titles(panel) == ["T · 1 · Import CSV / text files"]


def test_collapsed_sections_are_not_carried_into_another_project(window, panel, session, settle, demo, tmp_path):
    open_demo(window, session, settle, demo)
    panel.set_group_by("table")
    orders = next(node for node in panel.model.roots if node.item.title == "Orders")
    panel.view.collapse(panel.model.index_of(orders))
    assert panel._collapsed == {orders.item.key}
    other = Project.load(demo)
    other.tables[0].name = "Kunden"  # the same table ids
    window.open_path(other.save(tmp_path / "other.tafl"))
    QApplication.processEvents()
    panel.refresh()
    assert panel._collapsed == set()
    assert all(panel.view.isExpanded(panel.model.index_of(node)) for node in panel.model.roots)


def test_long_folders_keep_their_end(window, panel, session, settle, demo, qtbot):
    open_demo(window, session, settle, demo)
    long = demo.parent / ("a" * 60) / "inbox"
    project = session.project
    session.set_params(project.table("Orders").steps[0].id, {"file": str(long / "orders.csv")})
    panel.set_group_by("place")
    panel.refresh()
    node = next(n for n in panel.model.roots if n.item.tooltip == str(long))
    painted = []
    real = find_panel.QFontMetrics.elidedText
    find_panel.QFontMetrics.elidedText = lambda self, text, mode, width, *rest: painted.append((text, mode)) or real(self, text, mode, width, *rest)
    try:
        panel.view.viewport().repaint()
    finally:
        find_panel.QFontMetrics.elidedText = real
    assert (node.item.title, Qt.TextElideMode.ElideMiddle) in painted
    assert ("orders.csv", Qt.TextElideMode.ElideMiddle) in painted  # the file below it too


def test_enter_on_a_step_not_complete_yet_goes_to_the_setting_to_fill_in(window, panel, session, settle, demo, qtbot):
    open_demo(window, session, settle, demo)
    table = session.add_table(session.registry.actions["csv.import"])  # its file is still empty
    settle(session)
    panel.set_text(table.name)
    assert titles(panel) == [f"{table.name} · 1 · Import CSV / text files"]
    panel.activate(0, focus=True)
    focused(qtbot, window.settings_panel.form.editor("file")._edit)
