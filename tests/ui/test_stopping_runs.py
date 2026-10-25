"""Stopping a run, and closing the window while the project runs."""

import threading
import time
import types

import pytest

from tafl.api import Mode, action
from tafl.core import Project, Registry, Status, StepResult, TableResult
from tafl.ui import theme
from tafl.ui.main_window import MainWindow
from tafl.ui.session import Session

STARTED, RELEASE = threading.Event(), threading.Event()


@action("test.wait_on_run", "Wait (when run)")
def _wait_on_run(df, p, ctx):
    """In a run: says it started, then waits until the test lets it go on."""
    if ctx.mode == Mode.RUN:
        STARTED.set()
        RELEASE.wait(20)
    return df


@pytest.fixture(scope="module")
def slow_registry():
    registry = Registry()
    registry.load_builtin()
    module = types.ModuleType("waiting")
    module.wait = _wait_on_run
    registry.add_module(module)
    return registry


@pytest.fixture
def running(qapp, qtbot, slow_registry, tmp_path, monkeypatch):
    """A window with a project whose run exports a quick table, then waits in the second step
    of the next table; the run is started."""
    STARTED.clear()
    RELEASE.clear()
    (tmp_path / "data.csv").write_text("A;B\n1;2\n", encoding="utf-8")
    project = Project()
    quick = project.add_table("Quick")
    project.add_step(quick, slow_registry.actions["csv.import"], {"file": "data.csv"})
    project.add_step(quick, slow_registry.actions["csv.export"], {"file": "quick.csv"})
    table = project.add_table("Slow")
    project.add_step(table, slow_registry.actions["csv.import"], {"file": "data.csv"})
    project.add_step(table, slow_registry.actions["test.wait_on_run"], {})
    project.add_step(table, slow_registry.actions["rows.sort"], {"by": [{"column": "A", "order": "asc"}]})
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
    rows = [[tree.topLevelItem(i).text(c) for c in range(2)] for i in range(tree.topLevelItemCount())]
    assert rows == [["Quick", "ok"], ["Slow", "stopped at step 3"]]
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
