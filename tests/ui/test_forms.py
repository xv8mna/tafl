import time

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QWidget

from tafl.core import Project
from tafl.ui import forms
from tafl.ui.forms import MAX_LIST_ROWS, FormContext, ParamForm
from tafl.ui.session import json_ready


def sort_keys(count):
    return [{"column": f"c{number}", "order": "asc"} for number in range(count)]


def sort_form(registry, rows):
    action = registry.actions["rows.sort"]
    form = ParamForm(action.params, FormContext())
    form.set_values(json_ready(action.resolve({"by": rows})))
    return form


@pytest.fixture
def few_editors(monkeypatch):
    """The rows of a long list would take gigabytes of widgets: an editor for each ends the test."""
    built = []
    create = forms.create_editor

    def counted(*args):
        built.append(args)
        assert len(built) < 50, "an editor for every row"
        return create(*args)

    monkeypatch.setattr(forms, "create_editor", counted)


def test_every_action_form_shows_its_settings(qapp, registry):
    """Every action (and connection type) gets a form that returns the values it was given."""
    specs = [(a.id, a.params, a.resolve({})) for a in registry.actions.values()]
    specs += [(c.id, c.params, {p.key: p.initial() for p in c.params}) for c in registry.connectors.values()]
    for name, params, resolved in specs:
        form = ParamForm(params, FormContext())
        values = json_ready(resolved)
        form.set_values(values)
        assert json_ready(form.values()) == {p.key: values[p.key] for p in params}, name


def test_settings_round_trip_with_values(qapp, registry):
    action = registry.actions["table.merge"]
    values = json_ready(
        action.resolve(
            {
                "other": "t2",
                "match": [{"column": "a", "other_column": "b"}, {"column": "c", "other_column": "d"}],
                "how": "full",
                "columns": ["x", "y"],
                "multiple": "first",
                "suffix": "_r",
            }
        )
    )
    context = FormContext(columns=lambda source, v: ["a", "c", "x", "y", "z"], tables=[("t2", "Other")])
    form = ParamForm(action.params, context)
    form.set_values(values)
    assert json_ready(form.values()) == values


def test_choices_show_and_hide_fields(qapp, registry):
    action = registry.actions["db.import"]
    form = ParamForm(action.params, FormContext(connections=["db"]))
    form.set_values(json_ready(action.resolve({})))
    assert form.editor("table").isVisibleTo(form) and not form.editor("query").isVisibleTo(form)
    form.editor("mode")._combo.setCurrentIndex(1)
    assert not form.editor("table").isVisibleTo(form) and form.editor("query").isVisibleTo(form)


def test_the_decimal_separator_of_a_database_import_shows_only_with_detection(qapp, registry):
    """The decimal separator only matters when numbers and dates are detected: it shows with that
    setting, with Detect automatically for a new step and Point for one saved before it existed."""
    action = registry.actions["db.import"]
    form = ParamForm(action.params, FormContext(connections=["db"]))
    form.set_values(json_ready(action.resolve({})))
    assert not form.editor("decimal").isVisibleTo(form) and form.values()["decimal"] == "auto"
    form.editor("detect_types")._box.setChecked(True)
    assert form.editor("decimal").isVisibleTo(form)
    form.editor("detect_types")._box.setChecked(False)
    assert not form.editor("decimal").isVisibleTo(form)
    old = action.resolve(action.upgrade({"connection": "db", "table": "prices", "detect_types": True}, 1))
    form.set_values(json_ready(old))
    assert form.editor("decimal").isVisibleTo(form) and form.values()["decimal"] == "."


def test_list_rows_can_be_added_moved_and_removed(qtbot, registry):
    action = registry.actions["rows.sort"]
    form = ParamForm(action.params, FormContext(columns=lambda source, v: ["A", "B"]))
    form.set_values(json_ready(action.resolve({"by": [{"column": "A"}]})))
    rows = form.editor("by")
    with qtbot.waitSignal(form.changed) as signal:
        rows._add_clicked()
    assert signal.args[0]["by"] == [{"column": "A", "order": "asc"}, {"column": "", "order": "asc"}]
    rows._rows[1]["column"].set_value("B")
    rows._move(1, -1)
    assert [r["column"] for r in rows.value()] == ["B", "A"]
    rows._remove(0)
    assert rows.value() == [{"column": "A", "order": "asc"}]


def test_values_of_the_wrong_kind_show_the_default(qapp, registry):
    """E.g. from a project file written by hand; the step's check reports them."""
    action = registry.actions["table.merge"]
    wrong = {"other": ["t2"], "match": "a", "columns": 3}
    form = ParamForm(action.params, FormContext(columns=lambda source, v: ["a"], tables=[("t2", "Other")]))
    form.set_values(json_ready(action.resolve(wrong)))
    assert {key: form.values()[key] for key in wrong} == {"other": "", "match": [], "columns": []}


def number_form(registry, start):
    action = registry.actions["calc.enumerate"]
    form = ParamForm(action.params, FormContext())
    form.set_values(json_ready(action.resolve({"start": start})))
    return form


@pytest.mark.parametrize(
    "number", [2_100_000_000, 2**31, 3_000_000_000, -3_000_000_000, 2**63 - 1, 2**63, 10**30, -(2**63) - 1]
)
def test_a_whole_number_beyond_the_field_shows_and_stays(qtbot, registry, number):
    """From a project file written by hand: the field holds numbers up to 2,000,000,000 (Qt's end
    at 2**31). It shows the number, which stays until the field is changed; also beyond 2**63,
    which no number of Qt holds."""
    form = number_form(registry, number)
    spin = form.editor("start")._spin
    assert spin.text() == str(number) and form.values()["start"] == number
    with qtbot.waitSignal(form.changed) as signal:  # another setting
        form.editor("step")._spin.setValue(2)
    assert signal.args[0]["start"] == number
    with qtbot.waitSignal(form.changed) as signal:  # the arrow keys go on from the end of the range
        spin.stepBy(-1 if number > 0 else 1)
    assert signal.args[0]["start"] == spin.value() == (1_999_999_999 if number > 0 else -1_999_999_999)
    assert spin.text() == str(spin.value())


def test_the_number_at_the_end_of_the_field_can_be_typed_in_place_of_a_kept_one(qtbot, registry):
    """For Qt that is no change: the field's value is that number while it shows the kept one."""
    form = number_form(registry, 3_000_000_000)
    qtbot.addWidget(form)
    form.show()
    spin = form.editor("start")._spin
    spin.setFocus()
    spin.lineEdit().selectAll()
    QTest.keyClicks(spin.lineEdit(), "2000000000")
    with qtbot.waitSignal(form.changed) as signal:
        QTest.keyClick(spin, Qt.Key.Key_Return)
    assert signal.args[0]["start"] == 2_000_000_000 and spin.text() == "2000000000"


def test_an_infinite_whole_number_shows_0(qapp, registry):
    """As other values that are no whole number; the step's check reports it."""
    assert number_form(registry, float("inf")).values()["start"] == 0


@pytest.mark.parametrize("number", [3_000_000_000, 2**64])
def test_a_project_with_a_whole_number_beyond_the_field_opens(window, session, registry, tmp_path, number):
    """The step is selected when the project opens: its settings show, and the number stays when
    another setting changes."""
    project = Project(name="Big number")
    table = project.add_table("A")
    project.add_step(table, registry.actions["calc.enumerate"], {"start": number})
    assert window.open_path(project.save(tmp_path / "big.tafl"))
    form = window.settings_panel.form
    assert session.step.action == "calc.enumerate" and form.values()["start"] == number
    form.editor("step")._spin.setValue(2)
    assert session.step.params["step"] == 2 and session.step.params["start"] == number


def choice_form(registry, delimiter):
    action = registry.actions["csv.import"]
    form = ParamForm(action.params, FormContext())
    form.set_values(json_ready(action.resolve({"delimiter": delimiter})))
    return form


@pytest.mark.parametrize("value", [7, 2**63 - 1, 2**63, 2**64, -(2**63) - 1, [2**64]])
def test_a_choice_that_is_no_option_shows_and_stays(qtbot, registry, value):
    """From a project file written by hand, also a whole number beyond 2**63, which no number of Qt
    holds (also in a list): the choice shows it, and it stays when another setting changes and when
    the settings show again, with one item for it."""
    form = choice_form(registry, value)
    combo = form.editor("delimiter")._combo
    items = combo.count()
    assert combo.currentText() == f"{value} (unknown)" and form.values()["delimiter"] == value
    with qtbot.waitSignal(form.changed) as signal:  # another setting
        form.editor("header")._box.setChecked(False)
    assert signal.args[0]["delimiter"] == value
    form.set_values(signal.args[0])
    assert combo.count() == items and form.values()["delimiter"] == value
    with qtbot.waitSignal(form.changed) as signal:  # an option, and back
        combo.setCurrentIndex(0)
    assert signal.args[0]["delimiter"] == "auto"
    combo.setCurrentIndex(items - 1)
    assert form.values()["delimiter"] == value


def test_a_choice_in_a_row_of_a_list_keeps_a_whole_number_beyond_2_63(qtbot, registry):
    rows = [{"column": "a", "order": 2**64}, {"column": "b", "order": "desc"}]
    form = sort_form(registry, rows)
    by = form.editor("by")
    assert by.row_editor(1, "order")._combo.currentText() == f"{2**64} (unknown)" and form.values()["by"] == rows
    with qtbot.waitSignal(form.changed) as signal:  # the rows are built again
        by._add_clicked()
    assert signal.args[0]["by"] == [*rows, {"column": "", "order": "asc"}]


@pytest.mark.parametrize("value", [2**64, [2**64]])
def test_a_project_with_a_choice_beyond_2_63_opens(window, session, registry, settle, tmp_path, value):
    """The step is selected when the project opens: its settings show, the value stays when
    another setting changes, and the step's check reports it."""
    project = Project(name="Big choice")
    table = project.add_table("A")
    project.add_step(table, registry.actions["csv.import"], {"file": "a.csv", "delimiter": value})
    assert window.open_path(project.save(tmp_path / "big.tafl"))
    form = window.settings_panel.form
    assert session.step.action == "csv.import" and form.values()["delimiter"] == value
    form.editor("header")._box.setChecked(False)
    assert session.step.params["header"] is False and session.step.params["delimiter"] == value
    settle(session)
    assert window.settings_panel.error.text().startswith(f"Delimiter: '{value}' is not one of: auto, ")


def test_missing_columns_are_kept(qapp, registry):
    """A column that disappeared upstream stays selected (and is marked), so nothing is lost."""
    action = registry.actions["rows.dedupe"]
    form = ParamForm(action.params, FormContext(columns=lambda source, v: ["A"]))
    form.set_values(json_ready(action.resolve({"columns": ["gone", "A"]})))
    assert form.values()["columns"] == ["gone", "A"]
    assert form.editor("columns")._list.item(0).text() == "gone  (missing)"


class Searched(list):
    """A list that counts how often it is searched."""

    searches = 0

    def __contains__(self, item):
        self.searches += 1
        return super().__contains__(item)


def texts(editor):
    return [editor._list.item(row).text() for row in range(editor._list.count())]


def test_columns_are_not_searched_for_in_a_list(qapp, registry):
    """A project file can hold any number of columns, and its form opens by itself: a search through
    all of them for each of them takes minutes."""
    available = Searched(["a", "b", "c"])
    form = ParamForm(registry.actions["columns.select"].params, FormContext(columns=lambda source, v: available))
    editor = form.editor("columns")
    editor.refresh(form.ctx, {})
    selected = Searched(["b", "gone", "a"])
    editor._rebuild(selected)
    assert texts(editor) == ["b", "gone  (missing)", "a", "c"]  # the selected ones in their order, then the others
    assert editor.value() == ["b", "gone", "a"]
    assert selected.searches == available.searches == 0


def test_tables_are_not_searched_for_in_a_list(qapp, registry):
    form = ParamForm(registry.actions["table.append"].params, FormContext(tables=[("t1", "One"), ("t2", "Two"), ("t3", "Three")]))
    editor = form.editor("tables")
    editor._selected = Searched(["t2", "gone"])
    editor._fill()
    assert texts(editor) == ["Two", "gone (missing)", "One", "Three"]
    assert editor.value() == ["t2", "gone"]
    assert editor._selected.searches == 0


def test_the_tables_list_fills_again_only_when_it_changes(qapp, registry):
    """Qt takes more than twice as long for twice the rows: a refresh of the same list would
    take seconds at many thousand tables."""
    tables = [("t1", "One"), ("t2", "Two"), ("t3", "Three")]
    form = ParamForm(registry.actions["table.append"].params, FormContext(tables=tables))
    editor = form.editor("tables")
    editor.set_value(["t2", "gone"])
    fills = []
    fill = editor._fill
    editor._fill = lambda: fills.append(1) or fill()
    editor.refresh(FormContext(tables=list(tables)), {})
    assert fills == [] and editor.value() == ["t2", "gone"]
    editor.refresh(FormContext(tables=[*tables, ("t4", "Four")]), {})
    assert fills == [1] and texts(editor) == ["Two", "gone (missing)", "One", "Three", "Four"]
    editor._list.item(2).setCheckState(Qt.CheckState.Checked)  # the row "One": a new tick fills too
    assert len(fills) > 1 and editor.value() == ["t2", "gone", "t1"]
    editor.refresh(FormContext(tables=[("t2", "Zwei")]), {})
    assert texts(editor) == ["Zwei", "gone (missing)", "t1 (missing)"]


def test_a_new_tick_moves_the_table_up_to_the_ticked_ones(qapp, registry):
    tables = [("t1", "One"), ("t2", "Two"), ("t3", "Three")]
    form = ParamForm(registry.actions["table.append"].params, FormContext(tables=tables))
    editor = form.editor("tables")
    editor.set_value(["t2"])
    editor._list.item(2).setCheckState(Qt.CheckState.Checked)  # the row "Three"
    assert texts(editor) == ["Two", "Three", "One"] and editor.value() == ["t2", "t3"]


def test_a_choice_that_hides_a_field_stays_under_the_mouse(qapp, registry):
    """Filter rows: "is empty" hides the value. The operator stays where it was, so that the next
    turns of the mouse wheel go on changing it, and not the column beside it."""
    action = registry.actions["rows.filter"]
    form = ParamForm(action.params, FormContext(columns=lambda source, v: ["Status", "Name"]))
    form.resize(415, 400)
    form.show()
    form.set_values(json_ready(action.resolve({"conditions": [{"column": "Status", "operator": "!=", "value": "x"}]})))
    qapp.processEvents()
    conditions = form.editor("conditions")
    operator = conditions.row_editor(1, "operator")
    mouse = operator.mapTo(form, operator.rect().center())
    for choice in ("empty", "not_empty", "="):
        operator._combo.setCurrentIndex(operator._combo.findData(choice))
        qapp.processEvents()
        assert conditions.row_editor(1, "value").isVisibleTo(form) == (choice == "=")
        assert operator.rect().contains(operator.mapFrom(form, mouse)), choice
    form.close()


def test_a_list_up_to_the_limit_shows_its_rows(qapp, registry):
    rows = sort_keys(MAX_LIST_ROWS)
    form = sort_form(registry, rows)
    by = form.editor("by")
    assert len(by._rows) == MAX_LIST_ROWS and by.value() == rows
    assert by._add.isEnabled() and by._too_many.isHidden()


@pytest.mark.parametrize("count", [MAX_LIST_ROWS + 1, 20_000])
def test_a_longer_list_shows_no_rows_and_stays_as_it_is(qapp, registry, few_editors, count):
    """A project file written by hand can hold any number of rows, and its form opens by itself."""
    rows = sort_keys(count)
    started = time.perf_counter()
    form = sort_form(registry, rows)
    assert time.perf_counter() - started < 2
    by = form.editor("by")
    assert not by._rows and not by._row_widgets and len(by.findChildren(QWidget)) < 10
    assert by._too_many.text() == f"{count:,} rows: too many to show here. They stay as they are."
    assert not by._too_many.isHidden() and not by._add.isEnabled()
    assert not any(label.isVisibleTo(by) for label in by._labels.values())  # no rows: no headings
    assert form.values()["by"] == rows


def test_a_list_goes_back_to_its_rows_when_it_is_short_again(qapp, registry, few_editors):
    form = sort_form(registry, sort_keys(2))
    by = form.editor("by")
    by.set_value(sort_keys(2_000))
    assert not by._rows and by.value() == sort_keys(2_000)
    by.set_value(sort_keys(3))
    assert len(by._rows) == 3 and by.value() == sort_keys(3)
    assert by._add.isEnabled() and by._too_many.isHidden()


def test_another_setting_of_the_step_keeps_a_long_list(qtbot, registry, few_editors):
    action = registry.actions["table.merge"]
    rows = [{"column": f"c{number}", "other_column": "x"} for number in range(MAX_LIST_ROWS + 1)]
    form = ParamForm(action.params, FormContext(tables=[("t2", "Other")]))
    form.set_values(json_ready(action.resolve({"other": "t2", "match": rows})))
    with qtbot.waitSignal(form.changed) as signal:
        form.editor("how")._combo.setCurrentIndex(1)
    assert signal.args[0]["match"] == rows


def test_the_settings_a_form_sends_are_a_copy(qtbot, registry, few_editors):
    """The step shares no list or row with the form, also not the rows of a long list that the
    form keeps as they are: a change of the step's settings doesn't change the form."""
    action = registry.actions["table.merge"]
    rows = [{"column": f"c{number}", "other_column": "x"} for number in range(MAX_LIST_ROWS + 1)]
    form = ParamForm(action.params, FormContext(tables=[("t2", "Other")]))
    form.set_values(json_ready(action.resolve({"other": "t2", "match": rows})))
    with qtbot.waitSignal(form.changed) as signal:
        form.editor("how")._combo.setCurrentIndex(1)
    signal.args[0]["match"][0]["column"] = "other"
    assert form.values()["match"] == rows


def test_a_project_with_a_very_long_list_opens_unchanged(window, session, registry, tmp_path, few_editors, protected_view):
    """The last step of the first table is selected when a project opens, here in protected view."""
    project = Project(name="Long list")
    table = project.add_table("A")
    project.add_step(table, registry.actions["csv.import"], {"file": "a.csv"})
    rows = sort_keys(20_000)
    project.add_step(table, registry.actions["rows.sort"], {"by": rows})
    path = project.save(tmp_path / "long.tafl")
    started = time.perf_counter()
    assert window.open_path(path)
    assert time.perf_counter() - started < 10
    assert session.protected and session.step.action == "rows.sort"
    assert window.settings_panel.form.values()["by"] == rows
    window.settings_panel.refresh()
    assert not session.dirty
    assert window.settings_panel.form.editor("by").value() == rows
