"""Protected view: a project Tafl doesn't know can be read, but nothing is calculated, and none of its
files or databases is touched, until it is trusted."""

import builtins
import datetime as dt
import hashlib
import io
import json
import os
import sqlite3
import sys

import pyodbc
import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from tafl import APP_NAME
from tafl.api import ActionError, Kind, P, files
from tafl.api.parameters import Failed, plain
from tafl.core import Parameter, Project, Status, engine, sheet
from tafl.core.parameters import NOT_WORKED_OUT
from tafl.ui import app as app_module
from tafl.ui import theme, trust
from tafl.ui.checks import checks
from tafl.ui.connections_dialog import ConnectionsDialog
from tafl.ui.forms import FormContext, PathEditor
from tafl.ui.main_window import MainWindow
from tafl.ui.parameters_dialog import ParametersDialog
from tafl.ui.session import PROTECTED, Session

pytestmark = pytest.mark.usefixtures("protected_view")

# On this computer, and nothing may ask for it: see Recorder
OUTSIDE = r"\\127.0.0.1\C$\tafl-protected"
MARKS = ("tafl-protected", "127.0.0.1")


def reaches_outside(value) -> bool:
    return any(mark in str(value) for mark in MARKS)


class Recorder:
    """Every look at a path of the project, or at a database: it is noted, and refused, so nothing
    leaves this computer. The two connects are noted whatever they connect to."""

    def __init__(self, monkeypatch) -> None:
        self.hits: list[tuple[str, str]] = []
        for module, names in (
            (os, ("stat", "lstat", "scandir", "listdir", "open", "access", "readlink", "mkdir", "remove", "rename", "replace")),
            (os.path, ("getsize", "getmtime", "realpath", "samefile")),
            (builtins, ("open",)),
            (io, ("open",)),
        ):
            for name in names:
                monkeypatch.setattr(module, name, self._refused(f"{module.__name__}.{name}", getattr(module, name)))
        for name in ("exists", "lexists", "isfile", "isdir", "islink", "ismount"):  # these never raise
            monkeypatch.setattr(os.path, name, self._no(f"os.path.{name}", getattr(os.path, name)))
        monkeypatch.setattr(sqlite3, "connect", self._connect("sqlite3.connect", sqlite3.OperationalError))
        monkeypatch.setattr(pyodbc, "connect", self._connect("pyodbc.connect", pyodbc.OperationalError))
        monkeypatch.setattr(pyodbc, "drivers", lambda: ["ODBC Driver 18 for SQL Server"])  # as if on any computer

    def _refused(self, name, real):
        def call(*arguments, **options):
            if arguments and reaches_outside(arguments[0]):
                self.hits.append((name, str(arguments[0])))
                raise FileNotFoundError(arguments[0])
            return real(*arguments, **options)

        return call

    def _no(self, name, real):
        def call(path, *arguments):
            if reaches_outside(path):
                self.hits.append((name, str(path)))
                return False
            return real(path, *arguments)

        return call

    def _connect(self, name, error):
        def call(target, *arguments, **options):
            self.hits.append((name, str(target)))
            raise error("no connection in this test")

        return call

    @property
    def calls(self) -> set[str]:
        return {name for name, _ in self.hits}


def ways_outside(action, project) -> list[dict]:
    """Settings for a step of ``action``, one for each way it can reach out of the project."""
    found = [{}]
    for param in action.params:
        if isinstance(param, P.File):
            found = [{**row, param.key: f"{start}\\{param.key}.csv"} for row in found for start in (OUTSIDE, "{Folder}")]
        elif isinstance(param, P.Folder):
            found = [{**row, param.key: start} for row in found for start in (OUTSIDE, "{Folder}")]
        elif isinstance(param, P.ConnectionRef):
            found = [{**row, param.key: connection.name} for row in found for connection in project.connections]
        elif isinstance(param, P.Code):
            found = [{**row, param.key: "SELECT * FROM Orders"} for row in found]
        elif param.key == "mode" and action.id == "db.import":
            found = [{**row, "mode": "query"} for row in found]
        elif param.key == "source":
            found = [{**row, "source": source} for row in found for source in ("file", "folder")]
    return found


def outside_project(registry, folder) -> Project:
    """A step of every action, for each way it reaches outside; the tables are named after the
    actions, and a table starts with an import if its action can't start one."""
    project = Project(parameters=[Parameter("Folder", OUTSIDE), Parameter("Day", "=TODAY()")])
    project.add_connection("Server", "sqlserver", server="127.0.0.1,1", database="tafl-protected")
    project.add_connection("Lite", "sqlite", file=rf"{OUTSIDE}\data.sqlite")
    start = registry.actions["csv.import"]
    for action in registry.actions.values():
        for number, settings in enumerate(ways_outside(action, project), start=1):
            table = project.add_table(f"{action.id} {number}")
            if action.kind != Kind.SOURCE:
                project.add_step(table, start, {"source": "file", "file": rf"{OUTSIDE}\in.csv"})
            project.add_step(table, action, settings)
    # A file that a sheet wrote, and a variable that names no folder: Save as asks the disk about it
    table = project.add_table("sheet")
    project.add_step(table, start, {"source": "file", "file": rf"{OUTSIDE}\in.csv", files.SHEET_KEY: {"file": "in.csv"}})
    table = project.add_table("variable")
    project.add_step(table, start, {"source": "file", "file": r"%tafl-protected%\in.csv"})
    return project.save(folder / "outside.tafl")


@pytest.fixture
def outside(registry, tmp_path):
    return outside_project(registry, tmp_path)


def wait_a_moment(qtbot, session) -> None:
    qtbot.wait(250)  # the timer of a recalculation, the map's rebuild, the Find panel's search
    assert checks().wait(20_000)
    assert not session.busy and not session.running


def nothing_starts(monkeypatch, session) -> list:
    """What goes on to a step of the engine, or starts work in the background."""
    started = []

    def note(*arguments, **options):
        started.append(arguments)

    monkeypatch.setattr(session._worker, "start", note)
    monkeypatch.setattr(session._runner, "start", note)
    monkeypatch.setattr(engine._Evaluation, "_step", note)
    return started


# --- the window ------------------------------------------------------------------------------------


def test_a_project_tafl_doesnt_know_opens_in_protected_view(window, session, demo, settle):
    assert window.open_path(demo)
    settle(session)
    assert session.protected and session.results == {}
    assert not window.protected_bar.isHidden()
    assert window.protected_bar.text.text() == (
        "Protected view: this project comes from somewhere else. Tafl has calculated nothing and touched none "
        "of its files or databases. Read its steps first."
    )
    assert window.protected_bar.button.text() == "Trust this project"
    panel = window.data_panel
    assert panel.stack.currentWidget() is panel.message
    assert panel.message.text() == "Protected view: nothing is calculated until you trust this project."
    assert panel.message.text() == PROTECTED
    assert theme.ERROR_TEXT not in panel.message.styleSheet()  # a message, not an error
    assert session.table_status(session.table_id) == "pending"
    assert not window.reload_action.isEnabled()


def test_all_of_a_project_can_be_read_in_protected_view(window, session, demo, settle, qtbot):
    session.open_project(demo)
    orders = session.project.table("Orders")
    session.select_table(orders.id)
    session.select_step(orders.steps[0].id)
    assert window.steps_panel.list.count() == len(orders.steps)
    assert window.settings_panel.title.text() == "Import CSV / text files"
    assert window.settings_panel.form.values()["file"] == "data/orders.csv"
    assert window.data_panel.subtitle.text().startswith("after step 1:")
    assert window.data_panel.message.text() == PROTECTED

    window.show()
    window.open_find()
    window.find_panel.refresh()
    assert "import" in window.find_panel.summary.text()
    window.find_panel.search.setText("orders.csv")
    window.find_panel.refresh()
    assert any(hit.entry.step_id == orders.steps[0].id for hit in window.find_panel._hits)
    window.show_map(True)
    wait_a_moment(qtbot, session)
    assert window.map_view.canvas.scene().items()
    assert session.results == {}


def test_after_trust_this_project_it_works_as_always(window, session, demo, settle, registry):
    session.open_project(demo)
    window.protected_bar.button.click()
    assert not session.protected and window.protected_bar.isHidden()
    assert window.data_panel.message.text() == "Calculating…"  # no longer the protected view's message
    settle(session)
    panel = window.data_panel
    assert session.result is not None and session.result.ok
    assert panel.stack.currentWidget() is panel.view and panel.model.frame is not None
    assert window.reload_action.isEnabled()
    window.show_map(True)  # all tables, as always
    settle(session)
    assert all(session.table_status(t.id) == "ok" for t in session.project.tables)

    other = Session(registry)
    other.open_project(demo)
    assert not other.protected  # the file is known now


def test_the_formulas_of_parameters_wait_for_trust(window, session, tmp_path, settle, qtbot, monkeypatch):
    project = Project(parameters=[Parameter("Year", "2026"), Parameter("Day", "=TODAY()")])
    path = project.save(tmp_path / "parameters.tafl")
    session.open_project(path)
    values = session.parameter_values()
    assert plain(values["Year"]) == 2026 and values["Day"] == Failed(NOT_WORKED_OUT)
    assert session.engine._parameter_values == {}

    dialog = ParametersDialog(session, window)
    qtbot.addWidget(dialog)
    assert not dialog.calculating
    assert dialog.table.item(0, 2).text().startswith("2026")
    assert dialog.table.item(1, 2).text() == "(Protected view: not worked out)"
    dialog.table.item(1, 1).setText("=TODAY() - 1")  # typed in the dialog: also not worked out
    assert not dialog.calculating and dialog.table.item(1, 2).text() == "(Protected view: not worked out)"
    dialog.reject()

    session.trust_project()
    assert session.parameter_values()["Day"] == dt.date.today()


def test_opening_a_protected_project_leaves_no_old_data_behind(window, session, demo, tmp_path, settle):
    session.open_project(demo, trusted=True)
    settle(session)
    assert window.data_panel.model.frame is not None
    text = demo.read_text(encoding="utf-8")
    copy = tmp_path / "copy.tafl"
    copy.write_text(text.replace("Tafl demo", "Copy"), encoding="utf-8")
    session.open_project(copy)
    settle(session)
    assert session.protected and window.data_panel.model.frame is None


def test_a_new_project_is_trusted_and_the_bar_goes(window, session, demo, settle):
    session.open_project(demo)
    assert not window.protected_bar.isHidden()
    session.new_project()
    assert not session.protected and window.protected_bar.isHidden() and window.reload_action.isEnabled() is False
    session.add_table(session.registry.actions["csv.import"])
    assert not session.protected and window.reload_action.isEnabled()
    settle(session)


def test_edits_and_undo_stay_in_protected_view(window, session, demo, settle, qtbot, monkeypatch):
    session.open_project(demo)
    started = nothing_starts(monkeypatch, session)
    step = session.project.tables[0].steps[-1]
    session.set_note(step.id, "looked at it")
    session.set_enabled(step.id, False)
    session.undo()
    session.redo()
    session.reload_sources()
    session.set_overview(True)
    session.calculate_all()
    session.recalculate()
    wait_a_moment(qtbot, session)
    assert started == [] and session.protected and session.dirty and session.results == {}
    session._set_dirty(False)


# --- nothing outside is touched --------------------------------------------------------------------


def test_nothing_of_the_project_is_touched_until_it_is_trusted(window, session, outside, qtbot, settle, monkeypatch, tmp_path):
    """The whole window, from opening to Save as, with a project that names a network path and a
    SQL Server in every kind of step: no call reaches them. Then Trust this project: they do."""
    with monkeypatch.context() as watch:
        recorder = Recorder(watch)
        window.show()
        assert window.open_path(outside)
        assert session.protected
        started = nothing_starts(watch, session)
        window.ask_trust_and_run = lambda: False

        project = session.project
        assert len(project.tables) > len(session.registry.actions)
        for table in project.tables:  # every table and step, in the tables view and on the map
            session.select_table(table.id)
            for step in table.steps:
                session.select_step(step.id)
                assert window.data_panel.message.text() == PROTECTED
        wait_a_moment(qtbot, session)
        window.show_map(True)
        wait_a_moment(qtbot, session)
        for table in project.tables[:10]:
            session.select_table(table.id)
            for step in table.steps:
                session.select_step(step.id)
        wait_a_moment(qtbot, session)
        window.show_map(False)

        window.open_find()  # Find: all of it, the Problems filter, a search for the path
        window.find_panel.problems_button.setChecked(True)
        wait_a_moment(qtbot, session)
        assert "after you trust" in window.find_panel.summary.text()
        window.find_panel.problems_button.setChecked(False)
        window.find_panel.search.setText("tafl-protected")
        window.find_panel.refresh()
        assert window.find_panel._hits

        window.reload_action.setEnabled(True)  # Ctrl+R is off: also if it came through
        session.reload_sources()
        step = project.tables[0].steps[0]
        session.set_params(step.id, {"file": rf"{OUTSIDE}\other.csv"})  # a change of a step
        window.run_project()
        window.run_current_table()
        session.run_project([project.tables[0].id])

        sheet_step = project.find_table("sheet").steps[0]
        session.select_table(project.find_table("sheet").id)
        session.select_step(sheet_step.id)
        assert not window.settings_panel.sheet_button.isHidden()
        window.settings_panel.sheet_button.click()
        assert window._sheets == []
        assert "after you trust" in window.statusBar().currentMessage()
        assert session.sheet_outdated(sheet_step) is False
        session.sheet_seen(session.sheet_file(sheet_step))
        assert session.reads_file(session.sheet_file(sheet_step))

        dialog = ConnectionsDialog(session, window)
        qtbot.addWidget(dialog)
        dialog.show()
        for row in range(dialog.list.count()):
            dialog.list.setCurrentRow(row)
            dialog.test_button.click()
            assert "after you trust" in dialog.test_result.toPlainText()
        dialog.reject()
        dialog = ParametersDialog(session, window)
        qtbot.addWidget(dialog)
        dialog.reject()

        saved = session.save_project(tmp_path / "elsewhere" / "moved.tafl")  # Save as: the paths are looked at there
        wait_a_moment(qtbot, session)
        assert recorder.hits == [] and started == [] and session.results == {}
        assert session.protected
        assert trust._read(trust._DIGESTS) == []

    recorder = Recorder(monkeypatch)  # the engine is on its way again
    window.protected_bar.button.click()
    window.show_map(True)
    settle(session, 60_000)
    assert len(session.results) == len(project.tables)
    assert {"os.path.isfile", "os.path.isdir", "pyodbc.connect"} <= recorder.calls  # the test sees what it looks for
    assert any(OUTSIDE in path for _, path in recorder.hits)
    session._set_dirty(False)
    assert saved.is_file()


def test_the_engine_stops_any_other_way_in(session, outside, monkeypatch):
    """Should a new path forget the session's check, the engine itself calculates nothing."""
    session.open_project(outside)
    recorder = Recorder(monkeypatch)
    twin = session.engine.snapshot()
    results = twin.run()
    assert set(results) == {t.id for t in session.project.tables}
    assert all(r.skipped == "is in protected view" and not r.steps for r in results.values())
    table = session.project.tables[0].id
    assert twin.evaluate(table).steps == [] and twin.evaluate(table, until=0).steps == []
    assert twin.evaluate_all(table) == {} and twin.run(mode="preview")[table].skipped
    assert recorder.hits == []


def test_browse_doesnt_start_at_a_path_of_the_project(qtbot, session, tmp_path, monkeypatch):
    seen = []

    def chosen(_parent, _title, start, _filter):
        seen.append(start)
        return "", ""

    monkeypatch.setattr(QFileDialog, "getOpenFileName", chosen)

    def browse(protected):
        editor = PathEditor(P.File("file", "File"), FormContext(project_dir=tmp_path, protected=protected))
        qtbot.addWidget(editor)
        editor.set_value(rf"{OUTSIDE}\in.csv")
        editor._browse()

    browse(True)
    browse(False)
    assert seen == [str(tmp_path), rf"{OUTSIDE}\in.csv"]


# --- run ---------------------------------------------------------------------------------------------


@pytest.fixture
def exporting(tmp_path):
    (tmp_path / "in.csv").write_text("A\n1\n", encoding="utf-8")
    project = Project()
    table = project.add_table("T")
    project.add_step(table, "csv.import", {"file": "in.csv"})
    project.add_step(table, "csv.export", {"file": "out.csv"})
    return project.save(tmp_path / "export.tafl")


@pytest.mark.parametrize("start", ["run_project", "run_current_table", "from_a_menu"])
def test_a_run_in_protected_view_asks_first(window, session, exporting, settle, start):
    session.open_project(exporting)

    def run():
        if start == "from_a_menu":  # the right-click menus of a table call the session
            session.run_project([session.project.tables[0].id])
        else:
            getattr(window, start)()

    asked = []
    window.ask_trust_and_run = lambda: asked.append(1) or False
    run()
    settle(session)
    assert asked == [1] and session.protected and session.results == {}
    assert not (exporting.parent / "out.csv").exists()

    window.ask_trust_and_run = lambda: asked.append(2) or True
    run()
    assert not session.protected
    settle(session)
    assert asked == [1, 2] and (exporting.parent / "out.csv").is_file()


def test_the_question_before_a_run_has_cancel_as_its_default(window, session, monkeypatch):
    shown = []

    def exec_box(box):
        shown.append((box.text(), box.defaultButton().text(), box.escapeButton().text(), box.textFormat()))

    monkeypatch.setattr(QMessageBox, "exec", exec_box)
    assert window.ask_trust_and_run() is False  # nothing clicked
    assert shown[0][:3] == ("Trust this project and run it?", "Cancel", "Cancel")


# --- what counts as known ------------------------------------------------------------------------------


def test_a_file_that_changed_elsewhere_asks_again(session, demo, registry):
    session.open_project(demo)
    session.trust_project()
    session.open_project(demo)
    assert not session.protected
    demo.write_bytes(demo.read_bytes() + b"\n")  # one more byte
    session.open_project(demo)
    assert session.protected


def test_the_same_file_elsewhere_is_known(session, demo, tmp_path):
    """A value of the bytes, not of a path."""
    session.open_project(demo)
    session.trust_project()
    copy = tmp_path / "another name.tafl"
    copy.write_bytes(demo.read_bytes())
    session.open_project(copy)
    assert not session.protected


def test_a_project_cant_answer_for_itself(session, demo):
    session.open_project(demo)
    session.trust_project()
    data = json.loads(demo.read_text(encoding="utf-8"))
    data |= {"trusted": True, "trusted_projects": [session.project.digest]}
    demo.write_text(json.dumps(data), encoding="utf-8")
    session.open_project(demo)
    assert session.protected


def test_saving_in_protected_view_adds_nothing(session, demo, tmp_path, registry):
    session.open_project(demo)
    session.set_note(session.project.tables[0].steps[0].id, "edited")
    session.save_project()
    assert session.protected and trust._read(trust._DIGESTS) == []
    session.save_project(tmp_path / "other.tafl")
    assert session.protected and trust._read(trust._DIGESTS) == []
    for path in (demo, tmp_path / "other.tafl"):
        other = Session(registry)
        other.open_project(path)
        assert other.protected


def test_trusting_after_a_save_in_protected_view_knows_the_file_as_saved(session, demo):
    session.open_project(demo)
    session.set_note(session.project.tables[0].steps[0].id, "edited")
    session.save_project()
    session.trust_project()
    session.open_project(demo)
    assert not session.protected


def test_a_file_saved_by_a_trusted_window_is_known(session, demo, tmp_path, registry):
    session.open_project(demo, trusted=True)
    assert trust._read(trust._DIGESTS) == []  # opened with trust, and nothing was written
    session.set_note(session.project.tables[0].steps[0].id, "edited")
    saved = session.save_project()
    assert trust._read(trust._DIGESTS) == [hashlib.sha256(saved.read_bytes()).hexdigest()]
    other = Session(registry)
    other.open_project(saved)
    assert not other.protected

    session.new_project()  # a new project is trusted, so its file is known too
    session.add_table(registry.actions["csv.import"])
    saved = session.save_project(tmp_path / "new.tafl")
    other.open_project(saved)
    assert not other.protected


def test_the_list_of_known_files_keeps_the_newest(qapp):
    full = [f"{number:064x}" for number in range(trust.LIMIT)]
    trust._write(trust._DIGESTS, full)
    trust.add("new")
    assert trust._read(trust._DIGESTS) == ["new", *full[:-1]]
    trust.add(full[-2])  # known again: first, once
    assert trust._read(trust._DIGESTS) == [full[-2], "new", *full[:-2]]


def test_opening_a_known_file_keeps_it_on_the_list(session, demo):
    session.open_project(demo)
    session.trust_project()
    digest = session.project.digest
    trust._write(trust._DIGESTS, [*(f"{number:064x}" for number in range(trust.LIMIT - 1)), digest])  # next to go
    session.open_project(demo)
    assert trust._read(trust._DIGESTS)[0] == digest


@pytest.mark.parametrize("value", ["no list", '{"a": 1}', "[1, 2]", ""])
def test_a_list_that_can_t_be_read_knows_nothing(session, demo, value):
    QSettings().setValue(trust._DIGESTS, value)
    session.open_project(demo)
    assert session.protected
    session.trust_project()
    assert trust._read(trust._DIGESTS) == [session.project.digest]


# --- the first start ---------------------------------------------------------------------------------


def test_the_recent_list_counts_once_at_the_first_start(session, demo, tmp_path, monkeypatch):
    other = tmp_path / "other.tafl"
    other.write_bytes(demo.read_bytes().replace(b"demo", b"other"))
    QSettings().setValue("recent", [str(demo), str(other), rf"{OUTSIDE}\gone.tafl"])
    with monkeypatch.context() as watch:
        recorder = Recorder(watch)
        trust.first_start()
    assert recorder.hits == []  # also the path that isn't there: no file is read
    assert len(trust._read(trust._PATHS)) == 3

    session.open_project(demo)  # a project of the recent list opens as before, and its value is known now
    assert not session.protected
    assert trust._read(trust._DIGESTS) == [session.project.digest]
    session.open_project(demo)
    assert not session.protected

    demo.write_bytes(demo.read_bytes() + b"\n")  # the path counted once: a changed file asks
    session.open_project(demo)
    assert session.protected

    session.open_project(other)
    assert not session.protected


def test_only_the_first_start_copies_the_recent_list(session, demo, tmp_path):
    trust.first_start()  # nothing recent yet
    QSettings().setValue("recent", [str(demo)])  # after that, a project is recent because it was opened
    trust.first_start()
    session.open_project(demo)
    assert session.protected


def test_one_recent_project_at_the_first_start(session, demo):
    QSettings().setValue("recent", [str(demo)])  # the settings give a single text for a list of one
    trust.first_start()
    session.open_project(demo)
    assert not session.protected


def test_a_project_that_can_t_be_opened_keeps_its_place_in_the_recent_paths(session, demo):
    good = demo.read_bytes()
    demo.write_text("no project", encoding="utf-8")
    QSettings().setValue("recent", [str(demo)])
    trust.first_start()
    with pytest.raises(Exception, match="damaged"):
        session.open_project(demo)
    demo.write_bytes(good)
    session.open_project(demo)
    assert not session.protected


def test_the_app_makes_the_copy_and_opens_a_project_in_protected_view(qapp, monkeypatch, tmp_path, demo):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setattr(QApplication, "exec", lambda self: 0)
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    other = demo.with_name("other.tafl")
    other.write_bytes(demo.read_bytes())
    qapp.setOrganizationName(APP_NAME)  # what the app reads
    qapp.setApplicationName(APP_NAME)
    settings = QSettings()
    settings.remove(trust._DIGESTS)
    settings.remove(trust._PATHS)
    settings.setValue("recent", [str(other)])
    demo.write_bytes(demo.read_bytes() + b"\n")
    assert app_module.main(["tafl-app", str(demo)]) == 0
    window = [w for w in QApplication.topLevelWidgets() if isinstance(w, MainWindow) and w.isVisible()][-1]
    assert window.session.protected and not window.protected_bar.isHidden()
    assert trust._read(trust._PATHS) == [trust._path_key(other)]
    window.session._set_dirty(False)
    window.close()
    window.session.wait()


def test_the_startup_check_calculates_the_project_it_is_given(qapp, monkeypatch, tmp_path, demo, qtbot):
    """The build's check of the app: the project is its own, and no one is there to trust it."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.delenv("TAFL_PLUGINS", raising=False)
    monkeypatch.setenv("TAFL_STARTUP_CHECK", "1")
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    codes = []
    monkeypatch.setattr(QApplication, "exit", staticmethod(codes.append))
    monkeypatch.setattr(QApplication, "exec", lambda self: qtbot.waitUntil(lambda: bool(codes), timeout=60_000))
    app_module.main(["tafl-app", str(demo)])
    window = [w for w in QApplication.topLevelWidgets() if isinstance(w, MainWindow) and w.isVisible()][-1]
    assert codes == [0] and not window.session.protected
    window.session._set_dirty(False)
    window.close()
    window.session.wait()


def test_a_session_given_trusted_doesnt_ask(session, demo):
    session.open_project(demo, trusted=True)
    assert not session.protected
    session.open_project(demo, trusted=False)
    assert session.protected


def test_edit_in_sheet_needs_trust(window, session, tmp_path, settle, registry):
    data = tmp_path / "people.csv"
    data.write_text("Name\nAnna\n", encoding="utf-8")
    project = Project()
    table = project.add_table("People")
    settings = {"source": "file", "file": "people.csv", files.SHEET_KEY: sheet.mark(data)}
    project.add_step(table, registry.actions["csv.import"], settings)
    path = project.save(tmp_path / "people.tafl")
    session.open_project(path)
    step = session.project.tables[0].steps[0]
    session.want_sheet_edit(step.id)
    assert window._sheets == []
    session.trust_project()
    session.want_sheet_edit(step.id)
    settle(session)
    assert len(window._sheets) == 1
    window._sheets[0].close_now()


def test_the_formula_window_says_why_it_has_no_data(session, demo, settle):
    session.open_project(demo, trusted=False)
    session.select_table(session.project.table("Orders").id)
    assert session.input_problem(session.project.table("Orders").steps[1]) == PROTECTED
    session.trust_project()
    settle(session)
    assert session.input_problem(session.project.table("Orders").steps[1]) != PROTECTED
    assert session.step_result(session.step).status in (Status.OK, Status.SKIPPED)


def test_a_path_with_a_formula_parameter_has_no_value_in_protected_view(session, tmp_path):
    project = Project(parameters=[Parameter("Day", "=TODAY()")])
    table = project.add_table("T")
    step = project.add_step(table, "csv.import", {"source": "file", "file": "{Day}.csv", files.SHEET_KEY: {"file": "{Day}.csv"}})
    path = project.save(tmp_path / "day.tafl")
    session.open_project(path)
    with pytest.raises(ActionError, match="not worked out in protected view"):
        session.sheet_file(session.find_step(step.id)[1])


def test_the_big_test_has_steps_of_every_kind(registry, outside):
    project = Project.load(outside)
    assert {registry.actions[step.action].kind for table in project.tables for step in table.steps} == set(Kind)
    assert {step.action for table in project.tables for step in table.steps} == set(registry.actions)
