from etl_tool.ui.forms import FormContext, ParamForm
from etl_tool.ui.session import json_ready


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


def test_missing_columns_are_kept(qapp, registry):
    """A column that disappeared upstream stays selected (and is marked), so nothing is lost."""
    action = registry.actions["rows.dedupe"]
    form = ParamForm(action.params, FormContext(columns=lambda source, v: ["A"]))
    form.set_values(json_ready(action.resolve({"columns": ["gone", "A"]})))
    assert form.values()["columns"] == ["gone", "A"]
    assert form.editor("columns")._list.item(0).text() == "gone  (missing)"
