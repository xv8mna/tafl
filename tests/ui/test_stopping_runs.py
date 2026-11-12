"""Stopping a run, and closing the window while the project runs."""

import sqlite3
import threading
import time
import types

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QMenu

from tafl.api import ActionError, Mode, action
from tafl.core import Connection, Engine, Parameter, Project, Registry, Status, StepResult, TableResult
from tafl.ui import tables_panel, theme
from tafl.ui.main_window import MainWindow
from tafl.ui.map import view as map_view
from tafl.ui.session import Session

STARTED, RELEASE, FAIL = threading.Event(), threading.Event(), threading.Event()
CALCULATING, CANCELLED = threading.Event(), threading.Event()


@action("test.wait_on_calculation", "Wait (when calculated)")
def _wait_on_calculation(df, p, ctx):
    """While the table is calculated (not in a run): says it started, then takes 20 s, unless the
    calculation is cancelled."""
    if ctx.mode == Mode.PREVIEW:
        CALCULATING.set()
        end = time.monotonic() + 20
        while time.monotonic() < end:
            try:
                ctx.check_cancelled()
            except BaseException:
                CANCELLED.set()
                raise
            time.sleep(0.01)
    return df


BUSY = threading.Event()


@action("test.busy_on_calculation", "Busy (when calculated)")
def _busy_on_calculation(df, p, ctx):
    """While the table is calculated: says it started, then takes 1.5 s that can't be stopped, as
    a big import does."""
    if ctx.mode == Mode.PREVIEW:
        BUSY.set()
        time.sleep(1.5)
    return df


@action("test.wait_on_run", "Wait (when run)")
def _wait_on_run(df, p, ctx):
    """In a run: says it started, then waits until the test lets it go on, and fails if the test
    set FAIL."""
    if ctx.mode == Mode.RUN:
        STARTED.set()
        RELEASE.wait(20)
        if FAIL.is_set():
            raise ActionError("Failed on purpose")
    return df


@pytest.fixture(scope="module")
def slow_registry():
    registry = Registry()
    registry.load_builtin()
    module = types.ModuleType("waiting")
    module.wait = _wait_on_run
    module.calculating = _wait_on_calculation
    module.busy = _busy_on_calculation
    registry.add_module(module)
    return registry


@pytest.fixture
def running(qapp, qtbot, slow_registry, tmp_path, monkeypatch):
    """A window with a project whose run exports a quick table, then waits in the second step
    of the next table; the last table uses that one, a parameter, and exports. The run is started."""
    STARTED.clear()
    RELEASE.clear()
    FAIL.clear()
    (tmp_path / "data.csv").write_text("A;B\n1;2\n", encoding="utf-8")
    project = Project()
    project.parameters = [Parameter("Factor", "2")]
    quick = project.add_table("Quick")
    project.add_step(quick, slow_registry.actions["csv.import"], {"file": "data.csv"})
    project.add_step(quick, slow_registry.actions["csv.export"], {"file": "quick.csv"})
    table = project.add_table("Slow")
    project.add_step(table, slow_registry.actions["csv.import"], {"file": "data.csv"})
    project.add_step(table, slow_registry.actions["test.wait_on_run"], {})
    project.add_step(table, slow_registry.actions["rows.sort"], {"by": [{"column": "A", "order": "asc"}]})
    last = project.add_table("Last")
    project.add_step(last, slow_registry.actions["table.derive"], {"table": table.id})
    project.add_step(last, slow_registry.actions["calc.columns"], {"columns": [{"name": "F", "formula": "A * {Factor}"}]})
    project.add_step(last, slow_registry.actions["csv.export"], {"file": "last.csv"})
    path = project.save(tmp_path / "slow.tafl")
    qapp.setOrganizationName("Tafl Tests")
    session = Session(slow_registry)
    window = MainWindow(session)
    monkeypatch.setattr(window, "ask_save", lambda reason: pytest.fail(f"asked to save {reason}"))
    window.show()
    window.open_path(path)
    qtbot.waitUntil(lambda: not session.busy, timeout=10_000)
    window.run_project()
    assert STARTED.wait(10) and session.running
    yield window
    RELEASE.set()
    session.cancel_run()
    session.wait()
    qapp.processEvents()
    window._close_after_run = False
    session._set_dirty(False)
    window.close()
    window.deleteLater()


def another_project(registry, tmp_path, names=("First", "Second")):
    """A project in a folder of its own, whose first table waits in a run. Ids count from one in
    every project, so its ids are those of the project that runs, for other tables."""
    folder = tmp_path / "next"
    folder.mkdir()
    (folder / "data.csv").write_text("C\n3\n", encoding="utf-8")
    project = Project()
    for name in names:
        project.add_step(project.add_table(name), registry.actions["csv.import"], {"file": "data.csv"})
    project.add_step(project.tables[0], registry.actions["test.wait_on_run"], {})
    return project.save(folder / "next.tafl")


@pytest.fixture
def shop(slow_registry, tmp_path):
    """A project that reads a database, waits in a run, and exports. A database has no time
    stamp or size that says it changed, so only a new reload token reads it again."""
    with sqlite3.connect(tmp_path / "shop.db") as database:
        database.execute("CREATE TABLE items (id INTEGER)")
        database.execute("INSERT INTO items VALUES (1)")
    STARTED.clear()
    RELEASE.clear()
    FAIL.clear()
    project = Project(name="Shop")
    project.add_connection("Shop", "sqlite", file="shop.db")
    table = project.add_table("Items")
    project.add_step(table, slow_registry.actions["db.import"], {"connection": "Shop", "table": "items"})
    project.add_step(table, slow_registry.actions["test.wait_on_run"], {})
    project.add_step(table, slow_registry.actions["csv.export"], {"file": "out.csv"})
    return project.save(tmp_path / "shop.tafl")


@pytest.fixture
def two_reads(slow_registry, tmp_path):
    """Three tables on one database: the run waits in the second table, before it reaches the
    third, which starts from the first. All have a result of the first read."""
    with sqlite3.connect(tmp_path / "shop.db") as database:
        database.execute("CREATE TABLE items (id INTEGER)")
        database.execute("INSERT INTO items VALUES (1)")
    STARTED.clear()
    RELEASE.clear()
    FAIL.clear()
    project = Project(name="Shop")
    project.add_connection("Shop", "sqlite", file="shop.db")
    items = project.add_table("Items")
    project.add_step(items, slow_registry.actions["db.import"], {"connection": "Shop", "table": "items"})
    slow = project.add_table("Slow")
    project.add_step(slow, slow_registry.actions["db.import"], {"connection": "Shop", "query": "SELECT 5 AS n", "mode": "query"})
    project.add_step(slow, slow_registry.actions["test.wait_on_run"], {})
    copy = project.add_table("Copy")
    project.add_step(copy, slow_registry.actions["table.derive"], {"table": items.id})
    project.add_step(copy, slow_registry.actions["calc.columns"], {"columns": [{"name": "F", "formula": "id * 7"}]})
    return project.save(tmp_path / "shop.tafl")


def opened(session, path, qtbot):
    session.open_project(path, trusted=True)
    session.calculate_all(True)
    qtbot.waitUntil(lambda: not session.busy and len(session.results) == 3, timeout=10_000)
    session.calculate_all(False)
    return session.project.tables


def add_row(path):
    with sqlite3.connect(path.parent / "shop.db") as database:
        database.execute("INSERT INTO items VALUES (2)")


def exported(path):
    return (path.parent / "out.csv").read_text(encoding="utf-8-sig").split()


def run_to_the_end(session, qtbot):
    RELEASE.set()
    session.run_project()
    qtbot.waitUntil(lambda: not session.running and not session.busy and not session._timer.isActive(), timeout=10_000)


def lines(tree):
    return [[tree.topLevelItem(i).text(c) for c in range(2)] for i in range(tree.topLevelItemCount())]


def test_the_run_after_a_stop_reads_a_database_again(qtbot, slow_registry, shop):
    """The stopped run took its reload token with it: the next one must not take the same."""
    session = Session(slow_registry)
    session.open_project(shop, trusted=True)
    qtbot.waitUntil(lambda: not session.busy, timeout=10_000)
    session.run_project()
    assert STARTED.wait(10)
    session.cancel_run()
    RELEASE.set()
    qtbot.waitUntil(lambda: not session.running, timeout=10_000)
    add_row(shop)
    run_to_the_end(session, qtbot)
    assert exported(shop) == ["id", "1", "2"]


def test_two_stopped_runs_leave_no_table_with_the_rows_of_the_first_read(qtbot, slow_registry, two_reads):
    """Each stopped run has a place in the cache of the steps it reached, and the steps that it
    didn't reach keep the first read. A new read of the first table leads to them: the cache
    can't see that they are older, so a stopped run gives its token to the open project."""
    session = Session(slow_registry)
    items, _slow, copy = opened(session, two_reads, qtbot)
    for _ in range(2):
        add_row(two_reads)
        STARTED.clear()
        RELEASE.clear()
        session.run_project()
        assert STARTED.wait(10)
        session.cancel_run()
        RELEASE.set()
        qtbot.waitUntil(lambda: not session.running and not session.busy, timeout=10_000)
    session.select_table(copy.id)
    qtbot.waitUntil(lambda: not session.busy and not session._timer.isActive(), timeout=10_000)
    assert session.result.df["id"].to_list() == [1, 2, 2]
    assert session.results[items.id].df["id"].to_list() == [1, 2, 2]


@pytest.mark.parametrize("end", ["finished", "stopped", "failed"])
def test_reload_data_during_a_run_stays_after_the_run(qtbot, slow_registry, two_reads, monkeypatch, end):
    """The results of the run are older than the reload: a table that isn't selected doesn't go
    back to them, and keeps no status of them. A run that doesn't end takes no older token
    back to the open project."""
    session = Session(slow_registry)
    _items, slow, copy = opened(session, two_reads, qtbot)
    session.select_table(slow.id)  # a change of the selected table would reach its users
    qtbot.waitUntil(lambda: not session.busy and not session._timer.isActive(), timeout=10_000)
    if end == "failed":
        run = Engine.run

        def failing(*arguments, **keywords):
            results = run(*arguments, **keywords)
            if keywords.get("mode", Mode.RUN) == Mode.RUN:  # not the previews
                raise RuntimeError("It broke at the end")
            return results

        monkeypatch.setattr(Engine, "run", failing)
    session.run_project()
    assert STARTED.wait(10)
    add_row(two_reads)
    session.reload_sources()
    qtbot.waitUntil(lambda: not session.busy and not session._recheck, timeout=10_000)
    assert session.results[copy.id].df["id"].to_list() == [1, 2]
    token = session.engine.reload_token
    if end == "stopped":
        session.cancel_run()
    RELEASE.set()
    qtbot.waitUntil(lambda: not session.running and not session.busy and not session._recheck, timeout=10_000)
    assert session.results[copy.id].df["id"].to_list() == [1, 2]
    assert session.engine.reload_token == token
    session.select_table(copy.id)
    qtbot.waitUntil(lambda: not session.busy and not session._timer.isActive(), timeout=10_000)
    assert session.result.df["id"].to_list() == [1, 2]


def test_the_run_after_a_failed_run_reads_a_database_again(qtbot, slow_registry, shop, monkeypatch):
    session = Session(slow_registry)
    errors = []
    session.error.connect(errors.append)
    session.open_project(shop, trusted=True)
    qtbot.waitUntil(lambda: not session.busy, timeout=10_000)
    run = Engine.run

    def failing(*arguments, **keywords):
        run(*arguments, **keywords)
        raise RuntimeError("It broke at the end")

    monkeypatch.setattr(Engine, "run", failing)
    run_to_the_end(session, qtbot)
    assert errors and "It broke at the end" in errors[0]
    assert session._run_done == {}  # what it calculated stays out of memory
    assert session.engine.reload_token == session.run_engine.reload_token
    monkeypatch.setattr(Engine, "run", run)
    add_row(shop)
    run_to_the_end(session, qtbot)
    assert exported(shop) == ["id", "1", "2"]


@pytest.mark.parametrize("twin", [False, True])
def test_a_project_opened_again_reads_a_database_again(qtbot, slow_registry, shop, twin):
    """The window keeps its cache from one project to the next. Also another project of the
    folder with the same query."""
    session = Session(slow_registry)
    session.open_project(shop, trusted=True)
    qtbot.waitUntil(lambda: not session.busy, timeout=10_000)
    run_to_the_end(session, qtbot)
    assert exported(shop) == ["id", "1"]
    add_row(shop)
    other = shop.with_name("twin.tafl")
    other.write_bytes(shop.read_bytes())
    session.open_project(other if twin else shop, trusted=True)
    qtbot.waitUntil(lambda: not session.busy and not session._timer.isActive(), timeout=10_000)
    assert session.result.df["id"].to_list() == [1, 2]
    run_to_the_end(session, qtbot)
    assert exported(shop) == ["id", "1", "2"]


def test_stop_says_it_stops_and_that_it_stopped(running, qtbot):
    window, session = running, running.session
    assert window.stop_action.isEnabled() and window.statusBar().currentMessage() == "Running the project…"
    window.stop_action.trigger()
    assert window.statusBar().currentMessage() == "Stopping after the current step…"
    assert session.stopping and not window.stop_action.isEnabled()  # pressed once is enough
    RELEASE.set()  # the step that runs finishes; the next one doesn't start
    qtbot.waitUntil(lambda: not session.running, timeout=10_000)
    assert window.statusBar().currentMessage() == "Run stopped — the steps after the stop did not run"
    assert window.run_dock.isVisible()
    assert window.run_panel.summary.text().startswith("Run stopped at ")
    assert window.run_action.isEnabled() and not session.stopping


def test_the_run_results_say_what_ran_before_the_stop(running, qtbot, tmp_path):
    window, session = running, running.session
    window.stop_action.trigger()
    RELEASE.set()
    qtbot.waitUntil(lambda: not session.running, timeout=10_000)
    tree = window.run_panel.tree
    assert lines(tree) == [["Quick", "ok"], ["Slow", "stopped at step 3"]]
    quick, slow = tree.topLevelItem(0), tree.topLevelItem(1)
    assert [quick.child(i).text(1) for i in range(quick.childCount())] == ["1 row × 2 columns"] * 2  # the export ran
    assert (tmp_path / "quick.csv").exists()
    steps = [(slow.child(i).text(0)[:3], slow.child(i).text(1)) for i in range(slow.childCount())]
    assert steps == [("1. ", "1 row × 2 columns"), ("2. ", "1 row × 2 columns"), ("3. ", "stopped")]
    assert slow.isExpanded() and slow.foreground(1).color().name() == theme.STATUS_TEXT["warning"]
    tree.itemDoubleClicked.emit(slow.child(2), 0)  # the step where the run stopped
    assert session.table.name == "Slow" and session.step is session.table.steps[2]
    assert window.run_panel.summary.text().startswith("Run stopped at ")


def test_the_steps_after_the_stop_did_not_run(window, session):
    table = session.project.add_table("Slow")
    steps = [session.project.add_step(table, "csv.import", {"file": "a.csv"}) for _ in range(3)]
    stopped = TableResult(table.id, [StepResult(steps[0].id, Status.OK, seconds=1.5)], stopped=True)
    window.run_panel.show_stopped({table.id: stopped})
    top = window.run_panel.tree.topLevelItem(0)
    assert [top.text(1), top.text(2)] == ["stopped at step 2", "1.50 s"]
    assert [top.child(i).text(1) for i in range(top.childCount())] == ["", "stopped", "not run"]
    window.run_panel.show_stopped({})  # stopped before a table was calculated
    assert window.run_panel.tree.topLevelItemCount() == 0
    assert window.run_panel.summary.text().startswith("Run stopped at ")


def test_the_stopped_step_shows_its_messages(window, session):
    """E.g. how many files a Move files step had moved before the stop."""
    table = session.project.add_table("Archive")
    steps = [session.project.add_step(table, "csv.import", {"file": "a.csv"}) for _ in range(3)]
    moved = [("info", "Moved 202 files to archive before the stop"), ("warning", "Something to know")]
    stopped = TableResult(table.id, [StepResult(steps[0].id, Status.OK)], stopped=True, stopped_messages=moved)
    window.run_panel.show_stopped({table.id: stopped})
    top = window.run_panel.tree.topLevelItem(0)
    running, after = top.child(1), top.child(2)
    assert running.text(1) == "stopped" and after.text(1) == "not run" and after.childCount() == 0
    assert [running.child(i).text(0) for i in range(running.childCount())] == [
        "Moved 202 files to archive before the stop",
        "⚠ Something to know",
    ]


def test_closing_during_a_run_waits_for_it(running, qtbot, monkeypatch):
    window, session = running, running.session
    asked = []
    monkeypatch.setattr(window, "ask_close_while_running", lambda: asked.append(1) or "wait")
    assert not window.close()  # the window stays while the run writes its files
    assert window.isVisible() and session.running and not session.stopping
    assert window.statusBar().currentMessage() == "The window closes when the run is finished…"
    window.close()  # asked once
    assert asked == [1]
    RELEASE.set()
    qtbot.waitUntil(lambda: not window.isVisible(), timeout=10_000)
    assert window.run_panel.summary.text().startswith("Run finished")


def test_closing_during_a_run_can_stop_it(running, qtbot, monkeypatch):
    window, session = running, running.session
    monkeypatch.setattr(window, "ask_close_while_running", lambda: "stop")
    window.close()
    assert window.isVisible() and session.stopping
    assert window.statusBar().currentMessage() == "Stopping after the current step…  The window closes then."
    RELEASE.set()
    qtbot.waitUntil(lambda: not window.isVisible(), timeout=10_000)
    assert window.run_panel.summary.text().startswith("Run stopped at ")


def test_closing_during_a_calculation_stops_it_at_once(qapp, qtbot, slow_registry, tmp_path):
    """Up to 0.6.4 closing the window waited 5 s for the calculation of the selected table, which
    then went on for as long as it took."""
    CALCULATING.clear()
    CANCELLED.clear()
    (tmp_path / "data.csv").write_text("A;B\n1;2\n", encoding="utf-8")
    project = Project()
    table = project.add_table("Slow")
    project.add_step(table, slow_registry.actions["csv.import"], {"file": "data.csv"})
    project.add_step(table, slow_registry.actions["test.wait_on_calculation"], {})
    path = project.save(tmp_path / "slow.tafl")
    qapp.setOrganizationName("Tafl Tests")
    session = Session(slow_registry)
    window = MainWindow(session)
    window.show()
    window.open_path(path)
    try:
        assert CALCULATING.wait(10) and session.busy
        started = time.monotonic()
        assert window.close()
        assert time.monotonic() - started < 2
        assert CANCELLED.wait(2) and session.wait(2000)  # its thread ended
    finally:
        session._worker.cancel()  # when the test fails: the next tests don't wait for the 20 s
        session.wait()
        window.deleteLater()


def test_closing_during_a_step_that_cant_stop_hides_the_window_at_once(qapp, qtbot, slow_registry, tmp_path, monkeypatch):
    """A step that can't stop in its middle ends before the program does: closing waits for it
    without the window. Before, the window stayed until the step ended, up to 5 s."""
    BUSY.clear()
    (tmp_path / "data.csv").write_text("A;B\n1;2\n", encoding="utf-8")
    project = Project()
    table = project.add_table("Busy")
    project.add_step(table, slow_registry.actions["csv.import"], {"file": "data.csv"})
    project.add_step(table, slow_registry.actions["test.busy_on_calculation"], {})
    path = project.save(tmp_path / "busy.tafl")
    qapp.setOrganizationName("Tafl Tests")
    session = Session(slow_registry)
    window = MainWindow(session)
    window.show()
    window.open_path(path)
    waits = []
    wait = session.wait
    monkeypatch.setattr(session, "wait", lambda msecs=30_000: (waits.append((window.isVisible(), time.monotonic())), wait(msecs))[1])
    try:
        assert BUSY.wait(10) and session.busy
        started = time.monotonic()
        assert window.close()
        assert time.monotonic() - started > 0.5  # (it waited for the step)
        (visible, at), *_ = waits
        assert not visible and at - started < 0.5
        assert not window.isVisible() and not session._worker.running
    finally:
        session.wait()
        window.deleteLater()


def test_closing_during_a_run_can_be_cancelled(running, monkeypatch):
    window, session = running, running.session
    monkeypatch.setattr(window, "ask_close_while_running", lambda: "cancel")
    window.close()
    assert window.isVisible() and session.running and not session.stopping
    assert not window._close_after_run


def test_run_results_show_how_long_the_run_took(running, qtbot):
    """Not the time of the steps added up: the step that waits counts its time, but a step that
    failed after a long wait used to count none."""
    window, session = running, running.session
    threading.Timer(0.5, RELEASE.set).start()
    qtbot.waitUntil(lambda: not session.running, timeout=10_000)
    assert session.run_seconds >= 0.45  # the timer's clock may end a few milliseconds before the run's
    assert window.run_panel.summary.text().endswith(f", {session.run_seconds:.2f} s")


def test_undo_during_a_run_keeps_the_results_of_all_tables(running, qtbot):
    """An undo replaces the project object, but it is still the project that runs."""
    window, session = running, running.session
    session.set_note(session.project.tables[0].steps[0].id, "edited")
    before = session.project
    session.undo()
    assert session.project is not before
    RELEASE.set()
    qtbot.waitUntil(lambda: not session.running, timeout=10_000)
    assert set(session.results) == {t.id for t in session.project.tables}  # the second table only comes from the run
    assert session.result.ok and session.results[session.project.tables[1].id].ok
    assert window.run_panel.summary.text().startswith("Run finished")


@pytest.mark.parametrize("stopped", [False, True])
def test_the_data_of_a_run_goes_with_its_report(running, qtbot, stopped):
    """Only a stopped run needs the data on the way, and no limit of the cache counts it."""
    window, session = running, running.session
    assert session._run_done  # the first table is done
    if stopped:
        window.stop_action.trigger()
    RELEASE.set()
    qtbot.waitUntil(lambda: not session.running, timeout=10_000)
    assert session._run_done == {}


@pytest.mark.parametrize("change", ["position", "filter", "group", "note"])
def test_a_change_of_view_during_a_run_keeps_what_the_run_found(running, qtbot, change):
    """No calculation reads these: a preview would turn the error of the run into ok."""
    session = running.session
    slow = session.project.tables[1]
    if change == "position":
        session.set_positions({slow.id: (100.0, 200.0)})
    elif change == "filter":
        session.set_filters(slow.id, [{"column": "A", "values": ["1"]}])
    elif change == "group":
        session.add_group("G", [slow.id])
    else:
        session.set_note(slow.steps[1].id, "A note")
    FAIL.set()
    RELEASE.set()
    qtbot.waitUntil(lambda: not session.running and not session.busy and not session._timer.isActive(), timeout=10_000)
    assert session.table_status(slow.id) == "error"


@pytest.mark.parametrize("stopped", [False, True])
def test_a_run_of_the_project_before_leaves_no_result_in_the_next_project(running, qtbot, slow_registry, tmp_path, stopped):
    """Ids count from one in every project. A table that isn't selected isn't calculated, so
    nothing would replace a result it was given."""
    window, session = running, running.session
    assert window.open_path(another_project(slow_registry, tmp_path))
    assert session.running and not session.protected
    first, second = session.project.tables
    token = session.engine.reload_token
    if stopped:
        window.stop_action.trigger()
    RELEASE.set()
    qtbot.waitUntil(lambda: not session.running and not session.busy and not session._timer.isActive(), timeout=10_000)
    assert second.id not in session.results and session.table_status(second.id) == "pending"
    assert session.results[first.id].df.columns == ["C"]  # calculated for this project, as always
    assert session.engine.reload_token == token
    assert window.run_panel.summary.text().startswith("Run stopped at " if stopped else "Run finished")
    assert not window.stop_action.isEnabled()


@pytest.mark.parametrize("trusted", [True, False])
def test_the_report_of_a_run_names_its_tables_when_another_project_is_open(running, settle, slow_registry, tmp_path, trusted):
    """The open project doesn't have the tables with the errors, and its ids mean other tables."""
    window, session = running, running.session
    assert window.open_path(another_project(slow_registry, tmp_path, ["Only"]), trusted=trusted)
    selected = session.table_id, session.step_id
    FAIL.set()
    RELEASE.set()
    settle(session)
    tree = window.run_panel.tree
    assert [line[0] for line in lines(tree)] == ["Quick", "Slow", "Last"]
    assert lines(tree)[1] == ["Slow", "Failed on purpose"]
    slow = tree.topLevelItem(1)
    assert slow.child(1).text(0).startswith("2. Wait (when run)") and slow.child(1).text(1) == "Failed on purpose"
    assert "3 tables, 2 with errors, " in window.run_panel.summary.text()
    assert window.statusBar().currentMessage() == "Run finished with errors — see Run results"
    for line in (slow, slow.child(1), tree.topLevelItem(0)):  # the first line's id is in the open project too
        tree.itemDoubleClicked.emit(line, 0)
        assert (session.table_id, session.step_id) == selected


def test_the_report_of_a_stopped_run_names_its_tables_when_another_project_is_open(running, settle, slow_registry, tmp_path):
    window, session = running, running.session
    assert window.open_path(another_project(slow_registry, tmp_path, ["Only"]))
    window.stop_action.trigger()
    RELEASE.set()
    settle(session)
    tree = window.run_panel.tree
    assert lines(tree) == [["Quick", "ok"], ["Slow", "stopped at step 3"]]
    assert tree.topLevelItem(1).child(2).text(1) == "stopped"


def test_the_report_of_a_run_after_a_new_empty_project(running, settle):
    window, session = running, running.session
    session.new_project()
    RELEASE.set()
    settle(session)
    assert lines(window.run_panel.tree) == [["Quick", "ok"], ["Slow", "ok"], ["Last", "ok"]]


def test_the_steps_of_a_run_name_its_tables_when_another_project_is_open(running, settle, slow_registry, tmp_path):
    window, session = running, running.session
    assert window.open_path(another_project(slow_registry, tmp_path, ["First", "Second", "Third"]))
    RELEASE.set()
    settle(session)
    derive = window.run_panel.tree.topLevelItem(2).child(0).text(0)
    assert "Slow" in derive and "Second" not in derive


def test_a_line_of_the_run_before_goes_nowhere_during_the_next_run(running, settle, slow_registry, tmp_path):
    """The latest run is then one of the open project, but the line is not."""
    window, session = running, running.session
    RELEASE.set()
    settle(session)
    line = window.run_panel.tree.topLevelItem(1)
    assert window.open_path(another_project(slow_registry, tmp_path))
    settle(session)
    selected = session.table_id
    STARTED.clear()
    RELEASE.clear()
    window.run_project()
    assert STARTED.wait(10) and session.run_is_open
    window.run_panel.tree.itemDoubleClicked.emit(line, 0)
    assert session.table_id == selected


def test_a_table_deleted_during_a_run_leaves_no_old_status(running, settle):
    """The run still reports it, and the table that used it has lost its source: a table that
    isn't selected shows that too."""
    window, session = running, running.session
    quick, slow, last = session.project.tables
    session.delete_table(slow.id)
    RELEASE.set()
    settle(session)
    assert session.table_status(last.id) == "error"
    assert slow.id not in session.results
    tree = window.run_panel.tree
    assert [line[0] for line in lines(tree)] == ["Quick", "Slow", "Last"]
    tree.itemDoubleClicked.emit(tree.topLevelItem(1), 0)
    assert session.table_id == quick.id


def test_a_second_run_during_a_run_changes_nothing(running, settle, tmp_path):
    window, session = running, running.session
    reported = []
    session.run_finished.connect(lambda results: reported.append(list(results)))
    job, engine = session._runner._latest, session.run_engine
    session.run_project([session.project.tables[1].id])
    session.run_project()
    assert session.running and session._runner._latest == job and session.run_engine is engine
    RELEASE.set()
    settle(session)
    assert reported == [[t.id for t in session.project.tables]]
    assert "3 tables" in window.run_panel.summary.text()
    assert (tmp_path / "last.csv").is_file()  # the export after the wait


def test_a_second_run_of_a_protected_project_during_a_run_asks_nothing(running, settle, slow_registry, tmp_path, monkeypatch):
    window, session = running, running.session
    assert window.open_path(another_project(slow_registry, tmp_path), trusted=False)
    monkeypatch.setattr(window, "ask_trust_and_run", lambda: pytest.fail("asked to trust a project"))
    session.run_project([session.project.tables[0].id])
    assert session.protected and session.running
    RELEASE.set()
    settle(session)
    assert session.protected and (tmp_path / "last.csv").is_file()


def run_entry_after_a_click(monkeypatch, module, click):
    """Whether "Run this table" can be chosen in the menu that ``click`` opens."""
    shown = []

    class Menu(QMenu):
        def exec(self, *args):  # instead of showing it
            shown.append(next(a for a in self.actions() if a.text() == "Run this table").isEnabled())

    with monkeypatch.context() as patch:
        patch.setattr(module, "QMenu", Menu)
        click()
    return shown[0]


def test_run_this_table_is_off_in_the_list_of_tables_during_a_run(running, settle, monkeypatch):
    session, panel = running.session, running.tables_panel

    def click():
        panel._context_menu(panel.tree.visualItemRect(panel.tree.topLevelItem(0)).center())

    assert not run_entry_after_a_click(monkeypatch, tables_panel, click)
    RELEASE.set()
    settle(session)
    assert run_entry_after_a_click(monkeypatch, tables_panel, click)


def test_run_this_table_is_off_on_the_map_during_a_run(running, settle, monkeypatch):
    window, session = running, running.session
    window.show_map(True)
    view = window.map_view
    view.rebuild()
    node = view.nodes[session.project.tables[0].id]

    def click():
        view.node_menu(node, QPoint())

    assert not run_entry_after_a_click(monkeypatch, map_view, click)
    RELEASE.set()
    settle(session)
    assert run_entry_after_a_click(monkeypatch, map_view, click)


def test_an_edit_during_a_run_leaves_no_old_status(running, settle):
    """The table isn't selected, and the run calculated it as it was: it is calculated again,
    and so is the table that uses it. The report stays what the run did."""
    window, session = running, running.session
    quick, slow, last = session.project.tables
    session.set_params(slow.steps[0].id, {"file": "gone.csv"})
    RELEASE.set()
    settle(session)
    assert session.table_id == quick.id
    assert session.table_status(quick.id) == "ok"
    assert session.table_status(slow.id) == session.table_status(last.id) == "error"
    assert not session._recheck
    assert lines(window.run_panel.tree) == [["Quick", "ok"], ["Slow", "ok"], ["Last", "ok"]]


@pytest.mark.parametrize("change", ["table off", "step off", "step moved"])
def test_a_change_of_what_a_table_does_during_a_run_leaves_no_result_of_the_run(running, settle, change):
    session = running.session
    slow = session.project.tables[1]
    reported = []
    session.run_finished.connect(reported.append)
    if change == "table off":
        session.set_table_enabled([slow.id], False)
    elif change == "step off":
        session.set_enabled(slow.steps[2].id, False)
    else:
        session.move_step(slow.steps[2].id, 1)
    RELEASE.set()
    settle(session)
    assert session.results[slow.id] is not reported[0][slow.id] and not session._recheck


def test_a_step_that_finds_its_table_during_a_run_leaves_no_old_status(running, settle):
    """The run gave a step an error, as its table isn't there, and Undo brought that table back by
    now (a new table never gets the id that a step names)."""
    session = running.session
    quick, slow, last = session.project.tables
    RELEASE.set()
    settle(session)
    session.select_table(slow.id)
    session.set_params(last.steps[0].id, {"table": quick.id})
    session.delete_tables([quick.id])
    settle(session)
    STARTED.clear()
    RELEASE.clear()
    session.run_project()
    assert STARTED.wait(10)
    session.undo()
    session.select_table(slow.id)  # a table that changes while it is selected tells its users by itself
    RELEASE.set()
    settle(session)
    assert session.table_status(last.id) == "ok"


def test_a_change_of_parameters_during_a_run_leaves_no_old_status(running, settle, tmp_path):
    session = running.session
    session.set_parameters([Parameter("Factor", "3")])
    RELEASE.set()
    settle(session)
    assert session.results[session.project.tables[2].id].df["F"].to_list() == [3]
    assert (tmp_path / "last.csv").read_text(encoding="utf-8-sig").split() == ["A;B;F", "1;2;2"]  # the run used the old value


def test_a_change_of_connections_during_a_run_calculates_every_table_again(running, settle):
    session = running.session
    reported = []
    session.run_finished.connect(reported.append)
    session.set_connections([Connection("New", "sqlite")])
    RELEASE.set()
    settle(session)
    assert [session.table_status(t.id) for t in session.project.tables] == ["ok"] * 3
    assert not any(session.results[t.id] is reported[0][t.id] for t in session.project.tables)


@pytest.mark.parametrize("undone", [False, True], ids=["no edit", "an edit undone"])
def test_a_run_of_the_project_as_it_is_keeps_its_results(running, settle, undone):
    """Nothing is calculated again: the tables that aren't selected have the results of the run."""
    session = running.session
    reported = []
    session.run_finished.connect(reported.append)
    if undone:
        session.set_note(session.project.tables[1].steps[0].id, "edited")
        session.undo()
    RELEASE.set()
    settle(session)
    assert all(session.results[t.id] is reported[0][t.id] for t in session.project.tables[1:])


def test_save_as_during_a_run_leaves_every_table_ok(running, settle, tmp_path):
    """The saved paths differ now, so the tables are calculated again: none stays pending."""
    session = running.session
    folder = tmp_path / "elsewhere"
    folder.mkdir()
    session.save_project(folder / "moved.tafl")
    RELEASE.set()
    settle(session)
    assert [session.table_status(t.id) for t in session.project.tables] == ["ok"] * 3
    assert not session._recheck and not session.dirty


def test_stop_also_stops_a_formula_that_runs(qapp, qtbot, slow_registry, tmp_path, monkeypatch):
    """A slow formula doesn't make Stop wait for it: its query is interrupted."""
    (tmp_path / "data.csv").write_text("A;B\n1;2\n", encoding="utf-8")
    project = Project()
    table = project.add_table("Slow")
    project.add_step(table, slow_registry.actions["csv.import"], {"file": "data.csv"})
    slow = "(SELECT COUNT(*) FROM range(3000000000) t(i) WHERE i % 7 = 8) = 0"  # many seconds
    project.add_step(table, slow_registry.actions["rows.filter_formula"], {"condition": slow})
    path = project.save(tmp_path / "slow.tafl")
    qapp.setOrganizationName("Tafl Tests")
    session = Session(slow_registry)
    window = MainWindow(session)
    monkeypatch.setattr(window, "ask_save", lambda reason: pytest.fail(f"asked to save {reason}"))
    window.show()
    window.open_path(path)
    session._worker.cancel()  # the preview stops its slow formula too
    assert session._worker.wait(5_000)
    window.run_project()
    qtbot.wait(500)  # the formula runs now
    started = time.perf_counter()
    window.stop_action.trigger()
    qtbot.waitUntil(lambda: not session.running, timeout=10_000)
    assert time.perf_counter() - started < 5
    assert window.run_panel.summary.text().startswith("Run stopped at ")
    session._set_dirty(False)
    window.close()
    window.deleteLater()
