"""The status of every table stays current: a table that uses an incomplete one waits (it is
not red), Undo and Reload data refresh the tables that aren't selected, the map shows a table
whose rest was skipped, and a run reports its own duration and real errors only."""

import pytest

from tafl.core import Project, Status, StepResult, TableResult
from tafl.ui import theme
from tafl.ui.steps_panel import _ROLE as CARD

WAITING = "waits for “Orders”, whose step 11 is not complete yet"


def table_item(window, name):
    return next(i for i in window.tables_panel._table_items() if i.text(0) == name)


def add_merge_to_orders(window, settle, session, demo):
    """Revenue by customer starts from Orders; Orders gets a Merge step that isn't filled in."""
    window.open_path(demo)
    _, orders, revenue = session.project.tables
    session.select_table(revenue.id)  # calculated once: it has a status
    settle(session)
    session.select_table(orders.id)
    session.select_step(orders.steps[-1].id)
    settle(session)
    session.add_step(session.registry.actions["table.merge"])  # step 11
    settle(session)
    return orders, revenue


def test_a_table_that_uses_an_incomplete_one_waits_for_it(window, settle, session, demo):
    orders, revenue = add_merge_to_orders(window, settle, session, demo)
    assert session.table_status(orders.id) == "incomplete"
    assert session.table_status(revenue.id) == "waiting"  # calculated again without being selected
    name, number, needed = session.waits_for(revenue.id)
    assert (name, number) == ("Orders", 11) and "Other table" in needed
    assert table_item(window, "Revenue by customer").toolTip(0) == "W" + WAITING[1:]

    session.select_table(revenue.id)
    session.select_step(revenue.steps[0].id)
    settle(session)
    panel = window.data_panel
    assert panel.message.text() == f"Step 1 {WAITING}."
    assert theme.ERROR_TEXT not in panel.message.styleSheet()
    card = window.steps_panel.list.item(0).data(CARD)
    assert (card["status"], card["info"], card["tooltip"]) == ("waiting", "waiting", f"This step {WAITING}")
    settings = window.settings_panel
    assert settings.error.isHidden() and settings.info.text().startswith(f"This step {WAITING} (fill in Other table")
    session.select_step(revenue.steps[1].id)
    assert panel.message.text() == f"Step 1 {WAITING}: the steps after it are calculated when it is."
    assert settings.info.text() == f"Step 1 {WAITING}: this step is calculated when it is."
    assert session.input_problem(revenue.steps[1]) == f"Step 1 {WAITING}: the preview follows when it is."

    window.show_map(True)
    settle(session)
    node = window.map_view.nodes[revenue.id]
    assert node.status == "waiting" and node.info == "waiting" and node._incomplete() == "\nW" + WAITING[1:]


def test_undo_refreshes_the_tables_that_use_the_table(window, settle, session, demo):
    orders, revenue = add_merge_to_orders(window, settle, session, demo)
    assert session.table_status(revenue.id) == "waiting"
    session.undo()
    settle(session)
    assert session.table_id == orders.id and session.table_status(revenue.id) == "ok"
    assert table_item(window, "Revenue by customer").toolTip(0).endswith(" columns")  # its size, not the old message


def test_a_real_error_in_a_used_table_is_still_an_error(window, settle, session, demo):
    orders, revenue = add_merge_to_orders(window, settle, session, demo)
    session.undo()
    settle(session)
    session.select_step(orders.steps[5].id)
    session.add_step(session.registry.actions["rows.filter_formula"], params={"condition": "[Missing] > 1"})
    settle(session)
    assert session.table_status(orders.id) == "error"
    assert session.table_status(revenue.id) == "error" and session.waits_for(revenue.id) is None


def test_turning_off_or_deleting_a_used_table_refreshes_the_tables_that_use_it(window, settle, session, demo):
    window.open_path(demo)
    customers, orders, revenue = session.project.tables
    for table in (revenue, customers):
        session.select_table(table.id)
        settle(session)
    session.set_table_enabled([orders.id], False)
    settle(session)
    assert session.table_status(revenue.id) == "skipped"
    session.set_table_enabled([orders.id], True)
    settle(session)
    assert session.table_status(revenue.id) == "ok"
    session.delete_table(orders.id)
    settle(session)
    assert session.table_status(revenue.id) == "error"


@pytest.fixture
def two_inputs(tmp_path):
    (tmp_path / "orders.csv").write_text("Id;Amount\n1;5\n2;7\n", encoding="utf-8")
    (tmp_path / "inbox").mkdir()
    (tmp_path / "inbox" / "a.csv").write_text("Id;Amount\n3;9\n", encoding="utf-8")
    project = Project(name="Test")
    orders = project.add_table("Orders")
    project.add_step(orders, "csv.import", {"file": "orders.csv"})
    folder = project.add_table("Folder")
    project.add_step(folder, "csv.import", {"source": "folder", "folder": "inbox", "pattern": "*.csv"})
    return project.save(tmp_path / "two.tafl")


def test_reload_data_refreshes_every_table(window, settle, session, two_inputs):
    """A folder that was gone and is back: Reload data clears its red status, also when another
    table is selected."""
    window.open_path(two_inputs)
    orders, folder = session.project.tables
    inbox = two_inputs.parent / "inbox"
    inbox.rename(two_inputs.parent / "gone")
    session.select_table(folder.id)
    settle(session)
    assert session.table_status(folder.id) == "error"
    session.select_table(orders.id)
    settle(session)
    (two_inputs.parent / "gone").rename(inbox)
    session.reload_sources()
    settle(session)
    assert session.table_status(folder.id) == "ok" and session.table_status(orders.id) == "ok"
    assert table_item(window, "Folder").toolTip(0).startswith("1 row")


def test_selecting_a_table_calculates_nothing_else_again(window, settle, session, two_inputs, monkeypatch):
    window.open_path(two_inputs)
    orders, folder = session.project.tables
    session.select_table(folder.id)
    settle(session)
    scopes = []
    original = session._calculate
    monkeypatch.setattr(session, "_calculate", lambda scope, *args: scopes.append(scope) or original(scope, *args))
    session.select_table(orders.id)
    settle(session)
    assert scopes == ["table"]  # the tables that use nothing that changed keep their results


def test_an_unexpected_failure_is_reported_once(session, qtbot):
    session._recheck = {"t1", "t2"}
    with qtbot.waitSignal(session.error) as reported:
        session._worker.failed.emit("Traceback …\nRuntimeError: something unexpected")
    assert reported.args == ["Traceback …\nRuntimeError: something unexpected"]
    assert session._recheck == set()  # the next edit doesn't calculate them (and fail) again


@pytest.fixture
def skipping(tmp_path):
    """Orders: import, filter (no rows are left), skip the rest if empty, calculate, export."""
    (tmp_path / "orders.csv").write_text("Id;Amount\n1;5\n2;7\n", encoding="utf-8")
    project = Project(name="Test")
    orders = project.add_table("Orders")
    project.add_step(orders, "csv.import", {"file": "orders.csv"})
    project.add_step(orders, "rows.filter_formula", {"condition": "[Amount] > 100"})
    project.add_step(orders, "flow.skip_if_empty", {})
    project.add_step(orders, "calc.columns", {"columns": [{"name": "Double", "formula": "[Amount] * 2"}]})
    project.add_step(orders, "csv.export", {"file": "out/orders.csv"})
    return project.save(tmp_path / "skip.tafl")


def test_the_map_shows_that_the_rest_of_a_table_was_skipped(window, settle, session, skipping):
    window.open_path(skipping)
    window.show_map(True)
    settle(session)
    node = window.map_view.nodes[session.project.tables[0].id]
    assert node.status == "ok" and node.info == "0 × 2 · rest skipped" and node.skipped_after == 3
    assert [(row.number, row.kind) for row in node.rows] == [(1, "step"), (0, "gap"), (5, "export")]
    assert [node.dimmed(i) for i in range(len(node.rows))] == [False, False, True]  # the export is skipped
    node.skipped_after = 1  # an import that skipped the rest: every row after it
    assert [node.dimmed(i) for i in range(len(node.rows))] == [False, True, True]


def test_run_results_show_how_long_the_run_took(window, session):
    slow, quick = session.project.add_table("Slow"), session.project.add_table("Quick")
    slow_step = session.project.add_step(slow, "csv.import", {"file": "a.csv"})
    quick_step = session.project.add_step(quick, "csv.import", {"file": "b.csv"})
    results = {
        slow.id: TableResult(slow.id, [StepResult(slow_step.id, Status.OK, seconds=3.0)]),
        quick.id: TableResult(quick.id, [StepResult(quick_step.id, Status.OK, seconds=0.01)]),
    }
    panel = window.run_panel
    panel.show_results(results, 3.05)
    assert panel.summary.text().endswith(": 2 tables, no errors, 3.05 s")
    assert [panel.tree.topLevelItem(i).text(2) for i in range(2)] == ["3.00 s", "0.01 s"]
    panel.show_results(results)  # without the run's time: its steps added up
    assert panel.summary.text().endswith(", 3.01 s")


def test_a_table_that_is_off_is_no_error_in_the_status_bar(window, session):
    orders, off = session.project.add_table("Orders"), session.project.add_table("Off")
    step = session.project.add_step(orders, "csv.import", {"file": "a.csv"})
    results = {orders.id: TableResult(orders.id, [StepResult(step.id, Status.OK)]), off.id: TableResult(off.id, skipped="turned off")}
    window._run_finished(results)
    assert window.statusBar().currentMessage() == "Run finished — see Run results"
    results[orders.id] = TableResult(orders.id, [StepResult(step.id, Status.ERROR, error="File not found")])
    window._run_finished(results)
    assert window.statusBar().currentMessage() == "Run finished with errors — see Run results"
