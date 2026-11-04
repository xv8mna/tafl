"""The status of every table stays current: a table that uses an incomplete one waits (it is
not red), Undo and Reload data refresh the tables that aren't selected, the map shows a table
whose rest was skipped, and a run reports its own duration and real errors only."""

import sqlite3
import threading
import types

import pytest

from tafl.api import ActionError, Mode, P, action
from tafl.core import Connection, Parameter, Project, Registry, Status, StepResult, TableResult
from tafl.ui import theme
from tafl.ui.labels import tooltip_text
from tafl.ui.session import Session
from tafl.ui.steps_panel import _ROLE as CARD

WAITING = "waits for “Orders”, whose step 11 is not complete yet"
HOLD, ENTERED, GO = threading.Event(), threading.Event(), threading.Event()
INSIDE: list[int] = []  # one item for each gate that waits now


@action("test.gate", "Gate", params=[P.Text("fail", "Fail")])
def _gate(df, p, ctx):
    """In a preview while HOLD is set: says it started, then waits until the test lets it go on.
    Fails when its setting says so."""
    if ctx.mode == Mode.PREVIEW and HOLD.is_set():
        INSIDE.append(1)
        ENTERED.set()
        GO.wait(20)
        INSIDE.pop()
    if p.get("fail") == "yes":
        raise ActionError("Failed on purpose")
    return df


@pytest.fixture
def gated(qapp, tmp_path):
    """A session with the step "test.gate", and a file a.csv."""
    registry = Registry()
    registry.load_builtin()
    module = types.ModuleType("gates")
    module.gate = _gate
    registry.add_module(module)
    (tmp_path / "a.csv").write_text("A\n1\n", encoding="utf-8")
    HOLD.clear()
    ENTERED.clear()
    GO.clear()
    INSIDE.clear()
    session = Session(registry)
    yield session
    HOLD.clear()
    GO.set()
    session.wait()


def table_item(window, name):
    return next(i for i in window.tables_panel._table_items() if i.text(0) == name)


def gated_project(session, settle, tmp_path, used=False):
    """Edited (a.csv, then the gate), User (from Edited) when ``used``, and Other (a.csv): all
    calculated."""
    actions = session.registry.actions
    project = Project()
    edited = project.add_table("Edited")
    project.add_step(edited, actions["csv.import"], {"file": "a.csv"})
    project.add_step(edited, actions["test.gate"], {"fail": "no"})
    if used:
        user = project.add_table("User")
        project.add_step(user, actions["table.derive"], {"table": edited.id})
    other = project.add_table("Other")
    project.add_step(other, actions["csv.import"], {"file": "a.csv"})
    session.open_project(project.save(tmp_path / "p.tafl"), trusted=True)
    session.calculate_all(True)
    settle(session)
    session.calculate_all(False)
    assert [session.table_status(t.id) for t in session.project.tables] == ["ok"] * len(session.project.tables)
    return session.project.tables


@pytest.mark.parametrize("used", [True, False], ids=["used", "alone"])
@pytest.mark.parametrize("when", ["waiting", "calculating"])
def test_an_edit_just_before_a_click_on_another_table_leaves_no_old_status(gated, settle, qtbot, tmp_path, when, used):
    """The click stops the timer of the edit, or the calculation that the timer started, and
    calculates the other table. A table that no other table uses must not hide behind a user."""
    session = gated
    *edited_and_user, other = gated_project(session, settle, tmp_path, used)
    if when == "calculating":
        HOLD.set()
    session.set_params(edited_and_user[0].steps[1].id, {"fail": "yes"})
    if when == "calculating":
        qtbot.waitUntil(ENTERED.is_set, timeout=10_000)
        assert session.busy and not session._timer.isActive()
    session.select_table(other.id)
    HOLD.clear()
    GO.set()
    settle(session)
    assert [session.table_status(t.id) for t in edited_and_user] == ["error"] * len(edited_and_user)
    assert session.table_status(other.id) == "ok"


@pytest.mark.parametrize("edit", ["duplicate", "duplicate itself", "new table", "arrow", "derived"])
@pytest.mark.parametrize("when", ["waiting", "calculating"])
def test_an_edit_that_selects_another_table_leaves_no_old_status(gated, settle, qtbot, tmp_path, when, edit):
    """Duplicate, New table, an arrow on the map and New table from this one select a table, as a
    click does: they stop the timer of an edit just before, or the calculation it started."""
    session, actions = gated, gated.registry.actions
    project = Project()
    edited = project.add_table("Edited")
    project.add_step(edited, actions["csv.import"], {"file": "a.csv"})
    project.add_step(edited, actions["test.gate"], {"fail": "no"})
    user = project.add_table("User")
    project.add_step(user, actions["table.derive"], {"table": edited.id})
    for name in ("Other", "Lone"):
        project.add_step(project.add_table(name), actions["csv.import"], {"file": "a.csv"})
    session.open_project(project.save(tmp_path / "p.tafl"), trusted=True)
    session.calculate_all(True)
    settle(session)
    session.calculate_all(False)
    edited, user, other, lone = session.project.tables
    assert session.table_id == edited.id and session.table_status(user.id) == "ok"
    if when == "calculating":
        HOLD.set()
    session.set_params(edited.steps[1].id, {"fail": "yes"})
    if when == "calculating":
        qtbot.waitUntil(ENTERED.is_set, timeout=10_000)
        assert session.busy and not session._timer.isActive()
    if edit == "duplicate":
        session.duplicate_table(other.id)
    elif edit == "duplicate itself":
        session.duplicate_table(edited.id)
    elif edit == "new table":
        session.add_table(actions["csv.import"])
    elif edit == "arrow":
        session.add_link(other.id, lone.id, "append")
    else:
        session.add_derived_table(other.id)
    assert session.table_id not in (edited.id, user.id)
    if when == "calculating":  # the timer starts the calculation of the new selection: it stops the one in the gate
        qtbot.waitUntil(lambda: not session._timer.isActive(), timeout=10_000)
    HOLD.clear()
    GO.set()
    settle(session)
    assert [session.table_status(t.id) for t in (edited, user)] == ["error", "error"]


@pytest.mark.parametrize("edit", ["off", "delete"])
def test_a_check_that_reports_during_the_timer_of_an_edit_leaves_no_old_status(gated, settle, qtbot, tmp_path, edit):
    """Reload data checks the other tables after the selected one. An edit while that check runs:
    the check reports during the timer of the edit, with results from before the edit."""
    session, actions = gated, gated.registry.actions
    project = Project()
    source = project.add_table("Source")
    project.add_step(source, actions["csv.import"], {"file": "a.csv"})
    user = project.add_table("User")
    project.add_step(user, actions["table.derive"], {"table": source.id})
    project.add_step(user, actions["test.gate"], {"fail": "no"})
    other = project.add_table("Other")
    project.add_step(other, actions["csv.import"], {"file": "a.csv"})
    session.open_project(project.save(tmp_path / "p.tafl"), trusted=True)
    session.calculate_all(True)
    settle(session)
    session.calculate_all(False)
    source, user, other = session.project.tables
    session.select_table(other.id)
    settle(session)
    session._timer.setInterval(2_000)  # the report surely comes during the timer
    HOLD.set()
    session.reload_sources()
    qtbot.waitUntil(ENTERED.is_set, timeout=10_000)  # the check of Source and User waits in the gate
    if edit == "off":
        session.set_table_enabled([source.id], False)
    else:
        session.delete_tables([source.id])
    with qtbot.waitSignal(session.results_changed, timeout=10_000):
        GO.set()
    assert session._timer.isActive()  # the report came during the timer
    HOLD.clear()
    settle(session)
    assert session.table_status(user.id) == ("skipped" if edit == "off" else "error")


@pytest.mark.parametrize("edit", ["off", "delete"])
def test_a_check_of_all_tables_that_reports_during_the_timer_of_an_edit_leaves_no_old_status(
    gated, settle, qtbot, tmp_path, edit
):
    """The Problems filter calculates the tables without a result after the selected one. An edit
    while that runs: a table without a result has no mark of the edit, and the report gives it a
    result from before."""
    session, actions = gated, gated.registry.actions
    project = Project()
    source = project.add_table("Source")
    project.add_step(source, actions["csv.import"], {"file": "a.csv"})
    user = project.add_table("User")
    project.add_step(user, actions["table.derive"], {"table": source.id})
    project.add_step(user, actions["test.gate"], {"fail": "no"})
    other = project.add_table("Other")
    project.add_step(other, actions["csv.import"], {"file": "a.csv"})
    session.open_project(project.save(tmp_path / "p.tafl"), trusted=True)
    source, user, other = session.project.tables
    session.select_table(other.id)
    settle(session)
    assert user.id not in session.results
    session._timer.setInterval(2_000)  # the report surely comes during the timer
    HOLD.set()
    session.calculate_all(True)
    qtbot.waitUntil(ENTERED.is_set, timeout=10_000)  # the check of all tables waits in the gate
    if edit == "off":
        session.set_table_enabled([source.id], False)
    else:
        session.delete_tables([source.id])
    with qtbot.waitSignal(session.results_changed, timeout=10_000):
        GO.set()
    assert session._timer.isActive()  # the report came during the timer
    HOLD.clear()
    settle(session)
    assert session.table_status(user.id) == ("skipped" if edit == "off" else "error")


@pytest.mark.parametrize("how", ["undo", "delete"])
def test_the_status_of_a_table_that_is_gone_is_no_error_box(gated, settle, qtbot, tmp_path, how):
    """The map shows the box of a table that is gone until it is built again, a moment later, and
    the table can still have a result: after Undo of a Duplicate, or from a check of all tables
    that began before the delete. Its status must not stop with "There is no table"."""
    session, actions = gated, gated.registry.actions
    project = Project()
    broken = project.add_table("Broken")
    project.add_step(broken, actions["csv.import"], {"file": "gone.csv"})
    user = project.add_table("User")
    project.add_step(user, actions["table.derive"], {"table": broken.id})
    other = project.add_table("Other")
    project.add_step(other, actions["csv.import"], {"file": "a.csv"})
    project.add_step(other, actions["test.gate"], {"fail": "no"})
    session.open_project(project.save(tmp_path / "p.tafl"), trusted=True)
    settle(session)
    broken, user, other = session.project.tables
    if how == "undo":
        gone = session.duplicate_table(user.id)
        settle(session)
        session.undo()
    else:
        gone = user
        session._timer.setInterval(2_000)  # the report surely comes during the timer
        HOLD.set()
        session.calculate_all(True)
        qtbot.waitUntil(ENTERED.is_set, timeout=10_000)  # the check of all tables waits in the gate
        session.delete_tables([user.id])
        with qtbot.waitSignal(session.results_changed, timeout=10_000):
            GO.set()
        HOLD.clear()
    assert gone.id in session.results and session.project.find_table(gone.id) is None
    assert session.table_status(gone.id) == "error" and session.waits_for(gone.id) is None
    settle(session)


def test_the_check_of_the_selected_table_that_reports_during_the_timer_of_a_redo_leaves_no_old_status(
    gated, settle, qtbot, tmp_path
):
    """Reload data calculates the selected table first. A Redo while that runs changes that table
    and selects another one: the timer calculates the other table, and the report of the first
    comes during the timer."""
    session, actions = gated, gated.registry.actions
    project = Project()
    edited = project.add_table("Edited")
    project.add_step(edited, actions["csv.import"], {"file": "a.csv"})
    project.add_step(edited, actions["test.gate"], {"fail": "no"})
    other = project.add_table("Other")
    project.add_step(other, actions["csv.import"], {"file": "a.csv"})
    session.open_project(project.save(tmp_path / "p.tafl"), trusted=True)
    session.calculate_all(True)
    settle(session)
    session.calculate_all(False)
    edited, other = session.project.tables
    session.select_table(edited.id)
    settle(session)
    session.set_params(edited.steps[1].id, {"fail": "yes"})
    settle(session)
    session.select_table(other.id)
    settle(session)
    session.undo()  # Edited works again and is selected; Redo selects Other (selected at the Undo)
    settle(session)
    assert session.table_id == edited.id and session.table_status(edited.id) == "ok"
    session._timer.setInterval(2_000)
    HOLD.set()
    session.reload_sources()
    qtbot.waitUntil(ENTERED.is_set, timeout=10_000)  # the calculation of Edited waits in the gate
    session.redo()
    assert session.table_id == other.id
    with qtbot.waitSignal(session.results_changed, timeout=10_000):
        GO.set()
    assert session._timer.isActive()  # the report came during the timer
    HOLD.clear()
    settle(session)
    assert session.table_status(edited.id) == "error"


def test_after_the_click_the_edited_table_waits_for_its_stopped_calculation(gated, settle, qtbot, tmp_path):
    """A stop takes effect between steps, so the calculation of the edit can still read a big file.
    A second read of it at the same time takes twice the memory, and the next click would wait for
    a thread. After the stopped calculation, the cache has the data."""
    session = gated
    edited, other = gated_project(session, settle, tmp_path)
    HOLD.set()
    session.set_params(edited.steps[1].id, {"fail": "yes"})
    qtbot.waitUntil(ENTERED.is_set, timeout=10_000)
    with qtbot.waitSignal(session.results_changed, timeout=10_000):
        session.select_table(other.id)
    qtbot.wait(200)  # time for a calculation that starts now to reach the gate
    assert len(INSIDE) == 1 and session.busy  # it waits, and the window says so
    GO.set()
    settle(session)
    assert session.table_status(edited.id) == "error" and session.table_status(other.id) == "ok"


def waiting_session(session, settle, qtbot, tmp_path):
    """Edited and Other (each a.csv, then the gate): the calculation of an edit of Edited waits in
    the gate, a click on Other stops it, and the session waits for it before Edited again."""
    actions = session.registry.actions
    project = Project()
    for name in ("Edited", "Other"):
        table = project.add_table(name)
        project.add_step(table, actions["csv.import"], {"file": "a.csv"})
        project.add_step(table, actions["test.gate"], {"fail": "no"})
    path = project.save(tmp_path / "p.tafl")
    session.open_project(path, trusted=True)
    session.calculate_all(True)
    settle(session)
    session.calculate_all(False)
    edited, other = session.project.tables
    HOLD.set()
    session.set_params(edited.steps[1].id, {"fail": "yes"})
    qtbot.waitUntil(ENTERED.is_set, timeout=10_000)
    HOLD.clear()  # only the stopped calculation waits in the gate
    with qtbot.waitSignal(session.results_changed, timeout=10_000):
        session.select_table(other.id)
    qtbot.wait(100)
    assert len(INSIDE) == 1 and session.busy
    return path, edited, other


@pytest.mark.parametrize("how", ["empty", "protected"])
def test_another_project_ends_the_wait_at_once(gated, settle, qtbot, tmp_path, how):
    """The window must not say "Calculating…" for a project that calculates nothing."""
    session = gated
    path, *_ = waiting_session(session, settle, qtbot, tmp_path)
    if how == "empty":
        session.new_project()
    else:
        session.open_project(path, trusted=False)
    assert not session.busy
    GO.set()
    settle(session)


def test_an_edit_during_the_wait_is_calculated_before_the_rest(gated, settle, qtbot, tmp_path):
    """The end of the stopped calculation comes while the timer of the edit runs: the edited table
    goes first, as always, and the rest after it."""
    session = gated
    _path, edited, other = waiting_session(session, settle, qtbot, tmp_path)
    starts = []
    start = session._worker.start
    session._worker.start = lambda work: (starts.append(session._timer.isActive()), start(work))[1]
    session.set_params(other.steps[1].id, {"fail": "no, after all"})
    GO.set()
    settle(session)
    assert starts and not any(starts)  # nothing started during the timer
    assert session.table_status(edited.id) == "error" and session.table_status(other.id) == "ok"


def test_closing_during_the_wait_starts_nothing_after_it(gated, settle, qtbot, tmp_path):
    """A calculation that starts after the window closed would keep the program open."""
    session = gated
    waiting_session(session, settle, qtbot, tmp_path)
    starts = []
    start = session._worker.start
    session._worker.start = lambda work: (starts.append(1), start(work))[1]
    GO.set()
    assert session.wait(5_000)  # as the window does when it closes
    qtbot.wait(100)  # the report of the stopped calculation comes now
    assert not starts and not session.busy


def test_the_result_of_an_earlier_edit_does_not_settle_a_later_one(gated, settle, qtbot, tmp_path):
    """The calculation of edit 1 reports while the timer of edit 2 runs: the table is still
    unsettled, and a click during the calculation of edit 2 calculates it again."""
    session = gated
    edited, other = gated_project(session, settle, tmp_path)
    session._timer.setInterval(1_000)  # time for the report of edit 1 while the timer of edit 2 runs
    HOLD.set()
    session.set_params(edited.steps[1].id, {"fail": "yes"})
    qtbot.waitUntil(ENTERED.is_set, timeout=10_000)
    ENTERED.clear()
    session.set_params(edited.steps[1].id, {"fail": "no, after all"})
    GO.set()
    qtbot.waitUntil(lambda: not session.busy, timeout=10_000)
    assert session._timer.isActive() and session.table_status(edited.id) == "error"  # the result of edit 1
    GO.clear()
    qtbot.waitUntil(ENTERED.is_set, timeout=10_000)  # the calculation of edit 2
    assert session.busy and not session._timer.isActive()
    session.select_table(other.id)
    HOLD.clear()
    GO.set()
    settle(session)
    assert session.table_status(edited.id) == "ok"


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
    assert table_item(window, "Revenue by customer").toolTip(0) == tooltip_text("W" + WAITING[1:])

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
    assert " columns" in table_item(window, "Revenue by customer").toolTip(0)  # its size, not the old message


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


def test_undo_refreshes_a_table_outside_the_selection(window, settle, session, demo):
    window.open_path(demo)
    customers, _orders, revenue = session.project.tables
    for table in (revenue, customers):
        session.select_table(table.id)
        settle(session)
    session.set_table_enabled([revenue.id], False)
    settle(session)
    assert session.table_status(revenue.id) == "disabled"
    session.undo()
    settle(session)
    assert session.table_id == customers.id and session.table_status(revenue.id) == "ok"
    session.redo()
    settle(session)
    assert session.table_status(revenue.id) == "disabled"


def test_undo_of_a_broken_path_refreshes_a_table_outside_the_selection(window, settle, session, demo):
    window.open_path(demo)
    customers, orders, revenue = session.project.tables
    for table in (revenue, orders, customers):
        session.select_table(table.id)
        settle(session)
    session.set_params(orders.steps[0].id, {"file": "gone.csv"})
    session.select_table(orders.id)
    settle(session)
    assert session.table_status(orders.id) == "error"
    session.select_table(customers.id)
    settle(session)
    session.undo()
    settle(session)
    assert session.table_status(orders.id) == "ok"


def test_undo_keeps_a_table_it_did_not_touch_out_of_the_calculation(window, settle, session, demo):
    window.open_path(demo)
    customers, orders, revenue = session.project.tables
    for table in (customers, orders, revenue):
        session.select_table(table.id)
        settle(session)
    session.set_note(revenue.steps[0].id, "a note")
    settle(session)
    session.undo()
    assert session._recheck == {revenue.id}  # no table uses it


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
    assert "1 row" in table_item(window, "Folder").toolTip(0)


@pytest.mark.parametrize("change", ["parameter", "connection"])
def test_new_parameters_or_connections_refresh_the_tables_not_selected(session, settle, registry, tmp_path, change):
    """Any table can use a parameter or a connection, not only the selected one: Read imports
    {Name}.csv, or from the connection Shop, and User starts from Read."""
    (tmp_path / "a.csv").write_text("A\n1\n", encoding="utf-8")
    with sqlite3.connect(tmp_path / "shop.db") as database:
        database.execute("CREATE TABLE items (id INTEGER)")
    project = Project()
    project.parameters = [Parameter("Name", "a")]
    project.add_connection("Shop", "sqlite", file="shop.db")
    read = project.add_table("Read")
    if change == "parameter":
        project.add_step(read, registry.actions["csv.import"], {"file": "{Name}.csv"})
    else:
        project.add_step(read, registry.actions["db.import"], {"connection": "Shop", "table": "items"})
    user = project.add_table("User")
    project.add_step(user, registry.actions["table.derive"], {"table": read.id})
    other = project.add_table("Other")
    project.add_step(other, registry.actions["csv.import"], {"file": "a.csv"})
    session.open_project(project.save(tmp_path / "p.tafl"), trusted=True)
    session.calculate_all(True)
    settle(session)
    session.calculate_all(False)
    read, user, other = session.project.tables
    session.select_table(other.id)
    settle(session)
    assert [session.table_status(t.id) for t in (read, user)] == ["ok", "ok"]
    if change == "parameter":
        session.set_parameters([Parameter("Name", "gone")])
    else:
        session.set_connections([Connection("Shop", "sqlite", {"file": "gone.db"})])
    settle(session)
    assert [session.table_status(t.id) for t in (read, user, other)] == ["error", "error", "ok"]


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
    session._recheck, session._unsettled = {"t1", "t2"}, "t1"
    with qtbot.waitSignal(session.error) as reported:
        session._worker.failed.emit("Traceback …\nRuntimeError: something unexpected")
    assert reported.args == ["Traceback …\nRuntimeError: something unexpected"]
    assert session._recheck == set()  # the next edit doesn't calculate them (and fail) again
    assert session._unsettled is None  # nor the next click


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
