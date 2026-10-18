"""A step that is not filled in yet (e.g. just added) is incomplete, not an error: the table
doesn't look broken, and the data going into the step stays in sight."""

from tafl.ui import theme
from tafl.ui.steps_panel import _ROLE as CARD


def add_condition_step(window, settle, session, demo):
    window.open_path(demo)
    orders = session.project.table("Orders")
    session.select_table(orders.id)
    session.select_step(orders.steps[2].id)
    settle(session)
    going_in = session.step_result(orders.steps[2]).df
    step = session.add_step(session.registry.actions["rows.filter_formula"])  # as step 4
    settle(session)
    return orders, step, going_in


def test_a_new_step_is_incomplete(window, settle, session, demo):
    orders, step, going_in = add_condition_step(window, settle, session, demo)
    assert session.missing_settings(step) == ["Condition"]
    assert session.table_status(orders.id) == "incomplete"

    item = window.steps_panel.list.item(3)
    data = item.data(CARD)
    assert data["status"] == "incomplete" and data["info"] == "incomplete"
    assert item.toolTip() == "Not complete yet: fill in Condition"

    settings = window.settings_panel
    assert settings.error.isHidden() and settings.info.text() == "Not complete yet: fill in Condition."

    panel = window.data_panel  # the data going into the step
    assert panel.stack.currentWidget() is panel.view and panel.model.frame is going_in
    assert panel.subtitle.text().startswith("going into step 4: ")
    assert panel.info.text().endswith("   ·   step 4 is not complete yet")  # what to fill in is beside it
    assert panel.info.toolTip() == "Step 4 is not complete yet: fill in Condition"

    tables = window.tables_panel
    entry = next(i for i in tables._table_items() if i.text(0) == "Orders")
    assert entry.toolTip(0) == "Step 4 is not complete yet: fill in Condition"


def test_the_steps_after_an_incomplete_one_wait_for_it(window, settle, session, demo):
    orders, step, _ = add_condition_step(window, settle, session, demo)
    session.select_step(orders.steps[5].id)
    settle(session)
    panel = window.data_panel
    assert panel.message.text() == "Step 4 is not complete yet: the steps after it are calculated when it is."
    assert theme.ERROR_TEXT not in panel.message.styleSheet()
    assert window.settings_panel.info.text() == "Step 4 is not complete yet: this step is calculated when it is."
    assert window.settings_panel.error.isHidden()
    assert "Step 4 is not complete yet (fill in Condition)" in session.input_problem(orders.steps[5])


def test_filled_in_it_is_calculated_and_a_real_mistake_is_an_error(window, settle, session, demo):
    orders, step, _ = add_condition_step(window, settle, session, demo)
    session.set_params(step.id, {"condition": "[Net] > 100"})
    settle(session)
    assert session.table_status(orders.id) == "ok" and session.missing_settings(step) == []
    session.set_params(step.id, {"condition": "[Missing] > 100"})
    settle(session)
    assert session.table_status(orders.id) == "error" and session.missing_settings(step) == []
    assert not window.settings_panel.error.isHidden()


def test_the_map_shows_an_incomplete_table(window, settle, session, demo):
    orders, _, _ = add_condition_step(window, settle, session, demo)
    window.show_map(True)
    settle(session)
    view = window.map_view
    view.rebuild()
    assert view.nodes[orders.id].status == "incomplete" and view.nodes[orders.id].info == "incomplete"
    assert view.nodes[orders.id]._incomplete() == "\nStep 4 is not complete yet: fill in Condition"


def test_a_new_aggregate_step_is_incomplete_too(window, settle, session, demo):
    window.open_path(demo)
    orders = session.project.table("Orders")
    session.select_table(orders.id)
    step = session.add_step(session.registry.actions["calc.aggregate"])
    settle(session)
    assert session.missing_settings(step) == ["Calculate, row 1 – Column"]
    assert session.table_status(orders.id) == "incomplete" and session.describe(step) == "Aggregate: Sum of ?"
    revenue = session.project.table("Revenue by customer")
    session.select_table(revenue.id)
    settle(session)
    assert session.table_status(revenue.id) == "waiting"  # for Orders, not an error


def test_a_collapsed_group_tells_waiting_tables_from_incomplete_ones(window, settle, session, demo):
    orders, _, _ = add_condition_step(window, settle, session, demo)
    revenue = session.project.table("Revenue by customer")
    window.show_map(True)
    settle(session)
    view = window.map_view
    group = session.add_group("Reports", [revenue.id])
    view.set_collapsed(group.id, True)
    view.rebuild()
    assert session.table_status(revenue.id) == "waiting"
    assert view.group_nodes[group.id].summary == "1 waiting"  # its own steps are complete
    session.set_table_group([orders.id], group.id)
    view.rebuild()
    assert view.group_nodes[group.id].summary == "1 incomplete, 1 waiting"
    session.set_table_group([revenue.id], None)
    view.rebuild()
    assert view.group_nodes[group.id].summary == "1 incomplete"
