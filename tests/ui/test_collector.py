"""Reference cycles are collected on the window's thread only: see ``tafl.ui.collector``."""

import gc
import threading

from tafl.ui import app as app_module
from tafl.ui.collector import Collector


def collections(call) -> list[dict]:
    """The generation and the thread of each collection during ``call``."""
    seen = []

    def note(phase, info):
        if phase == "start":
            seen.append({"generation": info["generation"], "thread": threading.get_ident()})

    gc.callbacks.append(note)
    try:
        call()
    finally:
        gc.callbacks.remove(note)
    return seen


def cycles(count: int) -> None:
    for _ in range(count):
        cycle = []
        cycle.append(cycle)


def test_a_thread_at_work_leaves_the_collecting_to_the_window_thread(qtbot):
    def work():
        worker = threading.Thread(target=cycles, args=(300_000,))
        worker.start()
        qtbot.waitUntil(lambda: not worker.is_alive(), timeout=10_000)
        worker.join()
        qtbot.waitUntil(lambda: gc.get_count()[0] < gc.get_threshold()[0], timeout=5_000)  # the next tick

    seen = collections(work)
    assert seen
    assert {entry["thread"] for entry in seen} == {threading.get_ident()}


def test_it_collects_by_python_s_limits(qapp):
    collector = Collector()
    try:
        gc.collect()
        assert collections(collector.collect) == []  # nothing new: no pause
        cycles(gc.get_threshold()[0] + 10)
        assert [entry["generation"] for entry in collections(collector.collect)] == [0]
        assert gc.get_count()[0] < 100
        seen = []
        for _ in range(gc.get_threshold()[1] + 1):
            cycles(2 * gc.get_threshold()[0])
            seen += [entry["generation"] for entry in collections(collector.collect)]
        assert seen.count(2) == 1  # the older objects from time to time
    finally:
        collector.stop()


def test_stop_leaves_the_automatic_collector_as_it_was(qapp):
    assert not gc.isenabled()  # the fixture of these tests, as in the app
    Collector().stop()
    assert not gc.isenabled()
    gc.enable()
    try:
        collector = Collector()
        assert not gc.isenabled()
        collector.stop()
        assert gc.isenabled()
    finally:
        gc.disable()


def test_the_app_collects_on_its_own_thread(qapp, monkeypatch, tmp_path):
    made = []

    class Noted(Collector):
        def __init__(self) -> None:
            super().__init__()
            made.append(self)

        def stop(self) -> None:
            made.append("stopped")
            super().stop()

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.delenv("TAFL_PLUGINS", raising=False)
    monkeypatch.setattr(app_module, "Collector", Noted)
    monkeypatch.setattr(type(qapp), "exec", lambda self: 0)
    monkeypatch.setattr(app_module.sys, "excepthook", app_module.sys.excepthook)
    assert app_module.main(["tafl-app"]) == 0
    assert len(made) == 2 and made[1] == "stopped"
    window = [w for w in type(qapp).topLevelWidgets() if isinstance(w, app_module.MainWindow) and w.isVisible()][-1]
    window.session._set_dirty(False)
    window.close()
    window.session.wait()
