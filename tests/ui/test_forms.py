import time

import pytest
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


def test_missing_columns_are_kept(qapp, registry):
    """A column that disappeared upstream stays selected (and is marked), so nothing is lost."""
    action = registry.actions["rows.dedupe"]
    form = ParamForm(action.params, FormContext(columns=lambda source, v: ["A"]))
    form.set_values(json_ready(action.resolve({"columns": ["gone", "A"]})))
    assert form.values()["columns"] == ["gone", "A"]
    assert form.editor("columns")._list.item(0).text() == "gone  (missing)"


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
