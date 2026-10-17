"""Stopping a run, and closing the window while the project runs."""

import threading
import types

import pytest

from tafl.api import Mode, action
from tafl.core import Project, Registry
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
    """A window with a project whose run waits in its second step; the run is started."""
    STARTED.clear()
    RELEASE.clear()
    (tmp_path / "data.csv").write_text("A;B\n1;2\n", encoding="utf-8")
    project = Project()
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
    assert window.run_panel.tree.topLevelItemCount() == 0
    assert window.run_action.isEnabled() and not session.stopping


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
    assert session.run_seconds >= 0.5
    assert window.run_panel.summary.text().endswith(f", {session.run_seconds:.2f} s")
