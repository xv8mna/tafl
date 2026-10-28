"""Check data in the window: warnings in Run results, and the list of checks."""

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QSpinBox

from tafl.core import Project, Status, StepResult, TableResult
from tafl.ui import theme
from tafl.ui.forms import FormContext, ListEditor, ParamForm
from tafl.ui.run_panel import RunPanel
from tafl.ui.session import json_ready
from tafl.ui.steps_panel import _ROLE

WARNING = "The table has 2 rows; it must have at least 10 rows"


@pytest.fixture
def warned(tmp_path):
    """Orders: import (2 rows), check (at least 10 rows, only warn), export."""
    (tmp_path / "orders.csv").write_text("Id;Amount\n1;5\n2;7\n", encoding="utf-8")
    project = Project(name="Test")
    orders = project.add_table("Orders")
    project.add_step(orders, "csv.import", {"file": "orders.csv"})
    checks = [{"check": "rows", "compare": "at_least", "number": 10}]
    project.add_step(orders, "check.data", {"checks": checks, "on_fail": "warn"})
    project.add_step(orders, "csv.export", {"file": "out/orders.csv"})
    return project.save(tmp_path / "warned.tafl")


def _color(item, column=0) -> str:
    return item.icon(column).pixmap(12).toImage().pixelColor(6, 6).name()


def test_run_results_show_a_warning_without_a_click(qtbot, window, settle, session, warned):
    window.open_path(warned)
    settle(session)
    with qtbot.waitSignal(session.run_finished, timeout=30_000):
        session.run_project()
    settle(session)
    panel = window.run_panel
    assert ", no errors, 1 warning, " in panel.summary.text()
    assert theme.STATUS_TEXT["warning"] in panel.summary.styleSheet()
    assert window.statusBar().currentMessage() == "Run finished with warnings — see Run results"
    top = panel.tree.topLevelItem(0)
    assert top.text(1) == "ok, 1 warning" and top.isExpanded()
    assert top.foreground(1).color().name() == theme.STATUS_TEXT["warning"]
    assert _color(top) == theme.status_color("ok").name()  # the table is calculated, as the list of tables says
    check, export = top.child(1), top.child(2)
    assert check.isExpanded() and check.child(0).text(0) == f"⚠ {WARNING}"
    assert _color(check) == theme.status_color("warning").name()  # as in the list of steps
    assert not export.isExpanded() and _color(export) == theme.status_color("ok").name()
    assert (warned.parent / "out" / "orders.csv").exists()
    window.run_dock.hide()  # the window saves its docks when it closes: the next tests start without it


def test_run_results_count_the_warnings_of_all_tables(window, session):
    first, second = session.project.add_table("A"), session.project.add_table("B")
    step_a = session.project.add_step(first, "check.data", {})
    step_b = session.project.add_step(second, "check.data", {})
    warn = StepResult(step_a.id, Status.OK, messages=[("warning", "one"), ("info", "note"), ("warning", "two")])
    plain = StepResult(step_b.id, Status.OK, messages=[("info", "note")])
    window.run_panel.show_results({first.id: TableResult(first.id, [warn]), second.id: TableResult(second.id, [plain])})
    panel = window.run_panel
    assert ", no errors, 2 warnings, " in panel.summary.text()
    assert panel.tree.topLevelItem(0).text(1) == "ok, 2 warnings" and panel.tree.topLevelItem(1).text(1) == "ok"
    assert not panel.tree.topLevelItem(1).child(0).isExpanded()  # only messages: closed, as before
    error = StepResult(step_a.id, Status.ERROR, error="File not found", messages=[("warning", "one")])
    panel.show_results({first.id: TableResult(first.id, [error])})
    assert ", 1 with errors, 1 warning, " in panel.summary.text()
    assert theme.STATUS_TEXT["error"] in panel.summary.styleSheet()
    assert _color(panel.tree.topLevelItem(0).child(0)) == theme.status_color("error").name()
    panel.show_results({second.id: TableResult(second.id, [plain])})
    assert ", no errors, " in panel.summary.text() and "warning" not in panel.summary.text()


def test_a_double_click_goes_to_the_step_and_keeps_its_warning_open(qtbot, session):
    table = session.project.add_table("Orders")
    step = session.project.add_step(table, "check.data", {})
    panel = RunPanel(session)
    qtbot.addWidget(panel)
    panel.resize(700, 300)
    panel.show()
    panel.show_results({table.id: TableResult(table.id, [StepResult(step.id, Status.OK, messages=[("warning", WARNING)])])})
    top = panel.tree.topLevelItem(0)
    line = top.child(0)
    assert top.isExpanded() and line.isExpanded()
    viewport = panel.tree.viewport()
    for item in (top, line):
        point = panel.tree.visualItemRect(item).center()
        QTest.mouseClick(viewport, Qt.MouseButton.LeftButton, pos=point)
        QTest.mouseDClick(viewport, Qt.MouseButton.LeftButton, pos=point)
        assert session.table_id == table.id and session.step_id == (step.id if item is line else None)
        assert top.isExpanded() and line.isExpanded()  # the arrows open and close the lines


def test_the_list_of_checks_names_only_the_fields_it_shows(qapp, registry):
    action = registry.actions["check.data"]
    form = ParamForm(action.params, FormContext())
    form.resize(415, 300)
    form.show()
    checks = form.editor("checks")
    form.set_values(json_ready(action.resolve({"checks": [{"check": "rows", "compare": "at_least", "number": 1000}]})))
    qapp.processEvents()
    shown = {key: label.isVisibleTo(form) for key, label in checks._labels.items()}
    assert shown == {"check": True, "compare": True, "number": True, "up_to": False}
    checks.row_editor(1, "compare")._combo.setCurrentIndex(3)  # between: "and" shows
    assert checks._labels["up_to"].isVisibleTo(form) and checks.row_editor(1, "up_to").isVisibleTo(form)
    checks._remove(0)
    assert not any(label.isVisibleTo(form) for label in checks._labels.values())  # no rows: no headings
    checks._add_clicked()
    assert checks._labels["number"].isVisibleTo(form) and not checks._labels["up_to"].isVisibleTo(form)
    form.close()


def _checks(registry, number: int, up_to: int) -> ListEditor:
    param = next(p for p in registry.actions["check.data"].params if p.key == "checks")
    checks = ListEditor(param, FormContext())
    checks.set_value([{"check": "rows", "compare": "between", "number": number, "up_to": up_to}])
    return checks


def _plain_width(largest: int) -> int:
    """The width a number field without arrows needs for numbers up to ``largest``."""
    plain = QSpinBox()
    plain.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
    plain.setRange(0, largest)
    return plain.sizeHint().width()


def test_with_between_the_choices_stay_readable_in_a_narrow_panel(qapp, registry):
    """The number fields are as wide as their numbers, without arrows, so that the drop-down lists
    next to them keep their texts: a row needs only the room of its texts and numbers (a "between
    10 and 100" row fits the settings panel of a window 1229 pixels wide, the first start on a
    1366 × 768 screen: checked on screenshots, the fonts of the tests are others)."""
    checks = _checks(registry, 10, 100)
    choices = [checks.row_editor(1, key)._combo for key in ("check", "compare")]
    numbers = _plain_width(99) + _plain_width(999) + 2 * checks.row_editor(1, "number").layout().spacing()
    tools = checks._row_widgets[0][-1].sizeHint().width()
    room = sum(c.sizeHint().width() for c in choices) + numbers + tools + 4 * checks._grid.horizontalSpacing()
    checks.resize(room, 200)
    checks.show()
    qapp.processEvents()
    assert all(choice.width() >= choice.sizeHint().width() for choice in choices)
    spin = checks.row_editor(1, "number")._spin
    spin.setValue(2_000_000_000)
    assert spin.minimumSizeHint().width() == _plain_width(2_000_000_000)  # a long number fits
    checks.close()
    alone = ParamForm(registry.actions["calc.enumerate"].params, FormContext()).findChildren(type(spin))
    assert alone and not any(field.narrow for field in alone)  # outside a list, as before


def test_a_number_shows_all_its_digits_while_it_is_typed(qtbot, registry):
    checks = _checks(registry, 10, 100)
    qtbot.addWidget(checks)
    checks.show()
    checks.resize(checks.minimumSizeHint().width(), 200)
    line = checks.row_editor(1, "up_to")._spin.lineEdit()
    line.setFocus()
    line.selectAll()
    QTest.keyClicks(line, "2000000000")  # no Enter yet: the step doesn't have the number yet
    assert line.text() == "2000000000" and checks.value()[0]["up_to"] == 100
    end = line.fontMetrics().horizontalAdvance(line.text())

    def all_digits_show() -> bool:
        line.grab()  # draws the field: a text that doesn't fit is scrolled, the cursor at its end stays in sight
        return line.cursorRect().center().x() >= end

    qtbot.waitUntil(all_digits_show, timeout=2_000)


def test_a_number_changed_without_typing_still_fits(qtbot, registry):
    """The mouse wheel, the arrow keys and the number Qt puts back into an emptied field write the
    text without the signal of typing: the field grows with them too."""
    checks = _checks(registry, 99, 9999)
    qtbot.addWidget(checks)
    checks.show()
    spin = checks.row_editor(1, "up_to")._spin

    def fits() -> bool:  # the field has the room its number needs (the layout follows a moment later)
        return spin.width() == spin.sizeHint().width()

    qtbot.waitUntil(fits, timeout=2_000)
    spin.setFocus()
    narrow = spin.width()
    QTest.keyClick(spin, Qt.Key.Key_Up)  # 9999 -> 10000
    assert spin.value() == 10000
    qtbot.waitUntil(lambda: fits() and spin.width() > narrow, timeout=2_000)
    wide = spin.width()
    spin.lineEdit().selectAll()
    QTest.keyClick(spin.lineEdit(), Qt.Key.Key_Backspace)  # empty: narrow while typing
    qtbot.waitUntil(lambda: spin.width() < wide, timeout=2_000)
    QTest.keyClick(spin, Qt.Key.Key_Return)  # Qt puts 10000 back
    assert spin.text() == "10000"
    qtbot.waitUntil(lambda: spin.width() == wide, timeout=2_000)


def test_the_list_of_steps_writes_wide_tables_with_separators(window, session, settle, tmp_path):
    header = ";".join(f"C{i}" for i in range(1_001))
    (tmp_path / "wide.csv").write_text(f"{header}\n" + ";".join("1" for _ in range(1_001)) + "\n", encoding="utf-8")
    project = Project(name="Test")
    table = project.add_table("Wide")
    project.add_step(table, "csv.import", {"file": "wide.csv"})
    window.open_path(project.save(tmp_path / "wide.tafl"))
    session.select_table(session.project.table("Wide").id)
    settle(session)
    assert window.steps_panel.list.item(0).data(_ROLE)["info"] == "1 × 1,001"
