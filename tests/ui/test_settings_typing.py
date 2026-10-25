"""What is typed into a step's setting belongs to that step, also when another step is selected
before the field is left: F3 in the Find panel, or a change of the selection in code. F3, Undo
and Redo apply it first. It never goes to a step of another project."""

import copy

import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QToolBar

from tafl.core import Project
from tafl.core.project import Connection


@pytest.fixture(autouse=True)
def fresh_find_settings(session):
    QSettings().remove("find")


@pytest.fixture
def shown(window, session, settle, demo):
    window.resize(1500, 900)
    window.show()
    window.activateWindow()
    window.open_path(demo)
    settle(session)
    yield window
    window.find_dock.hide()


def focused(qtbot, widget=None):
    """Wait until ``widget`` (or any widget) has the keyboard focus; returns the focus widget."""
    def ready() -> bool:
        focus = QApplication.focusWidget()
        return focus is not None and (widget is None or focus is widget)

    qtbot.waitUntil(ready, timeout=2000)
    return QApplication.focusWidget()


def settings_of(session) -> dict:
    return {step.id: copy.deepcopy(step.params) for table in session.project.tables for step in table.steps}


def notes_of(session) -> dict:
    return {step.id: step.note for table in session.project.tables for step in table.steps}


def step(session, table: str, number: int):
    return session.project.find_table(table).steps[number - 1]


def select(session, table: str, number: int):
    session.select_table(session.project.find_table(table).id)
    session.select_step(step(session, table, number).id)
    QApplication.processEvents()


def click(window, action) -> None:
    """Click a button of the toolbar: the keyboard focus stays where it is."""
    toolbar = window.findChild(QToolBar, "MainToolbar")
    QTest.mouseClick(toolbar.widgetForAction(action), Qt.MouseButton.LeftButton)
    QApplication.processEvents()


def type_into_formula(window, session, qtbot, *, on_map=False):
    """Find the Gross formula of Orders, Enter: the cursor is in it; type at its end."""
    window.show_map(on_map)
    panel = window.find_panel
    window.open_find()
    panel.regex_button.setChecked(True)
    panel.set_text("ROUND|OVER")
    titles = [hit.title.text for hit in panel.hits()]
    assert titles == ["Orders · 6 · Calculate columns", "Revenue by customer · 3 · Calculate columns"]
    panel.activate(0, focus=True)
    editor = focused(qtbot)
    settings = window.map_view.settings if on_map else window.settings_panel
    assert settings.isAncestorOf(editor) and editor.toPlainText() == "ROUND([Net] * 1.19, 2)"
    QTest.keyClick(editor, Qt.Key.Key_End)
    QTest.keyClicks(editor, " * 2")
    return editor


@pytest.mark.parametrize(
    ("key", "on_map"), [(Qt.Key.Key_F3, False), (Qt.Key.Key_F3, True), ("shift+f3", False)], ids=["f3", "f3-on-map", "shift-f3"]
)
def test_f3_while_typing_keeps_the_typing_in_its_own_step(shown, session, settle, qtbot, key, on_map):
    before = settings_of(session)
    editor = type_into_formula(shown, session, qtbot, on_map=on_map)
    if key == "shift+f3":
        QTest.keyClick(editor, Qt.Key.Key_F3, Qt.KeyboardModifier.ShiftModifier)
    else:
        QTest.keyClick(editor, key)
    revenue = step(session, "Revenue by customer", 3)
    assert session.step is revenue  # F3 went on (Shift+F3 wraps round to the last result)
    gross = step(session, "Orders", 6)
    assert gross.params["columns"][0]["formula"] == "ROUND([Net] * 1.19, 2) * 2"
    after = settings_of(session)
    assert {step_id for step_id in before if before[step_id] != after[step_id]} == {gross.id}
    settings = shown.map_view.settings if on_map else shown.settings_panel
    assert settings.form.values()["columns"][0]["formula"] == "[Revenue] / SUM([Revenue]) OVER ()"
    session.undo()  # one undo step, for the formula
    assert step(session, "Orders", 6).params["columns"][0]["formula"] == "ROUND([Net] * 1.19, 2)"
    settle(session)


def test_a_new_selection_in_code_keeps_the_typing_in_its_own_step(shown, session, settle, qtbot):
    before = settings_of(session)
    type_into_formula(shown, session, qtbot)
    session.select_step(step(session, "Orders", 3).id)  # the editor still has the focus
    QApplication.processEvents()
    gross = step(session, "Orders", 6)
    assert gross.params["columns"][0]["formula"] == "ROUND([Net] * 1.19, 2) * 2"
    after = settings_of(session)
    assert {step_id for step_id in before if before[step_id] != after[step_id]} == {gross.id}
    assert shown.settings_panel.form.values()["conditions"] == before[step(session, "Orders", 3).id]["conditions"]
    session.select_table(session.project.find_table("Customers").id)  # another table, the same
    QApplication.processEvents()
    assert settings_of(session) == after


def test_a_line_setting_and_the_note(shown, session, settle, qtbot):
    panel = shown.find_panel
    shown.open_find()
    before, notes = settings_of(session), notes_of(session)
    panel.set_text("data/orders.csv")
    panel.activate(0, focus=True)
    field = focused(qtbot, shown.settings_panel.form.editor("file")._edit)
    field.end(False)
    QTest.keyClicks(field, "x")
    QTest.keyClick(field, Qt.Key.Key_F3)  # the only result: F3 stays at the step and applies the typing
    QApplication.processEvents()
    importing = step(session, "Orders", 1)
    assert importing.params["file"] == "data/orders.csvx" and field.hasFocus()
    QTest.keyClicks(field, "y")
    session.select_step(step(session, "Orders", 2).id)  # in code: the field applies nothing yet
    QApplication.processEvents()
    assert importing.params["file"] == "data/orders.csvxy"
    after = settings_of(session)
    assert {step_id for step_id in before if before[step_id] != after[step_id]} == {importing.id}
    # The note: typed, then F3 to another step
    panel.set_text("")
    panel.activate(0)  # Customers · 1
    shown.settings_panel.note.setFocus()
    focused(qtbot, shown.settings_panel.note)
    QTest.keyClicks(shown.settings_panel.note, "Check the sheet")
    QTest.keyClick(shown.settings_panel.note, Qt.Key.Key_F3)
    assert session.step is step(session, "Orders", 1)
    assert step(session, "Customers", 1).note == "Check the sheet"
    assert {s for s, note in notes_of(session).items() if note != notes[s]} == {step(session, "Customers", 1).id}
    assert shown.settings_panel.note.text() == ""  # the note of the step shown now
    shown.settings_panel.note.clearFocus()
    QApplication.processEvents()
    assert step(session, "Orders", 1).note == ""


def test_typing_never_goes_to_another_project(shown, session, settle, qtbot, demo):
    """Step ids repeat across projects: what was typed for a step and not applied yet when another
    project is opened (here without leaving the field first) is not written into a step of that
    project with the same id."""
    again = demo.with_name("again.tafl")
    again.write_bytes(demo.read_bytes())  # the same step ids
    type_into_formula(shown, session, qtbot)
    session.open_project(again)
    QApplication.processEvents()
    fresh = settings_of(session)
    assert step(session, "Orders", 6).params["columns"][0]["formula"] == "ROUND([Net] * 1.19, 2)"
    assert not session.dirty and not session._undo
    # The note, typed and not applied yet
    shown.settings_panel.note.setFocus()
    focused(qtbot, shown.settings_panel.note)
    QTest.keyClicks(shown.settings_panel.note, "Check the sheet")
    typed_for = session.step.id
    session.open_project(demo)
    QApplication.processEvents()
    assert notes_of(session)[typed_for] == "" and not session.dirty
    shown.settings_panel.note.clearFocus()
    QApplication.processEvents()
    assert settings_of(session) == fresh and set(notes_of(session).values()) == {""} and not session.dirty
    settle(session)


def test_typing_never_goes_to_the_same_step_of_another_project(shown, session, settle, qtbot, tmp_path):
    """The step shown in the other project has the id and the action of the one typed in."""
    project = Project(name="One step")
    project.add_step(project.add_table("Orders"), session.registry.actions["csv.import"], {"file": "orders.csv"})
    first, second = project.save(tmp_path / "first.tafl"), project.save(tmp_path / "second.tafl")
    shown.open_path(first)
    field = shown.settings_panel.form.editor("file")._edit
    field.setFocus()
    focused(qtbot, field)
    field.end(False)
    QTest.keyClicks(field, "x")
    session.open_project(second)
    QApplication.processEvents()
    focus = QApplication.focusWidget()
    if focus is not None:
        focus.clearFocus()  # leaving a field applies what is typed in it
    QApplication.processEvents()
    assert session.project.tables[0].steps[0].params["file"] == "orders.csv" and not session.dirty
    assert shown.settings_panel.form.editor("file")._edit.text() == "orders.csv"


def type_then_f3(window, session, qtbot, *, on_map=False):
    """Find data/orders.csv, Enter: the cursor is in the File field of Orders · 1; type at its end,
    then F3 to the next of all imports, exports and commands."""
    window.show_map(on_map)
    panel = window.find_panel
    window.open_find()
    panel.set_text("data/orders.csv")
    panel.activate(0, focus=True)
    settings = window.map_view.settings if on_map else window.settings_panel
    field = focused(qtbot, settings.form.editor("file")._edit)
    field.end(False)
    QTest.keyClicks(field, "x")
    panel.set_text("")
    QTest.keyClick(field, Qt.Key.Key_F3)
    QApplication.processEvents()
    assert session.step is not step(session, "Orders", 1)
    assert step(session, "Orders", 1).params["file"] == "data/orders.csvx"
    return settings


@pytest.mark.parametrize("on_map", [False, True], ids=["tables", "map"])
def test_after_f3_while_typing_the_focus_stays_in_the_settings(shown, session, settle, qtbot, on_map):
    """The field typed in is gone with the settings of its step: the keyboard focus stays in the
    settings panel, so typing on changes neither the selection nor a setting."""
    settings = type_then_f3(shown, session, qtbot, on_map=on_map)
    settle(session)
    focus = QApplication.focusWidget()
    assert focus is not None and (focus is settings or settings.isAncestorOf(focus))
    shown_step, before = session.step, settings_of(session)
    QTest.keyClicks(focus, "rev")
    QApplication.processEvents()
    assert session.step is shown_step and settings_of(session) == before
    settle(session)


@pytest.mark.parametrize("on_map", [False, True], ids=["tables", "map"])
def test_undo_of_the_typing_applied_by_f3_shows_its_step(shown, session, settle, qtbot, on_map):
    """F3 applies the typing before it goes on: the undo step has the step typed in as its
    selection, so Undo shows the step whose setting it takes back."""
    type_then_f3(shown, session, qtbot, on_map=on_map)
    shown.undo_action.trigger()  # Ctrl+Z, Edit → Undo
    importing = step(session, "Orders", 1)
    assert importing.params["file"] == "data/orders.csv"
    assert session.step is importing
    settle(session)


def connection_project(session, tmp_path):
    """Table T reads a database table through connection Old; table U has a step of its own."""
    project = Project(name="Connections")
    project.add_connection("Old", "sqlite", file="shop.db")
    project.add_step(project.add_table("T"), session.registry.actions["db.import"], {"connection": "Old", "table": "t"})
    project.add_step(project.add_table("U"), session.registry.actions["csv.import"], {"file": "u.csv"})
    return project.save(tmp_path / "connections.tafl")


def rename_then_type(window, session, settle, qtbot, tmp_path, *, on_map=False):
    """Rename connection Old to New (with table U selected), then type into the Table field of
    T's step without leaving it; returns the field."""
    window.resize(1500, 900)
    window.show()
    window.activateWindow()
    window.open_path(connection_project(session, tmp_path))
    window.show_map(on_map)
    settle(session)
    t, u = session.project.tables
    session.select_table(u.id)
    session.set_connections([Connection("New", "sqlite", {"file": "shop.db"})], renames={"Old": "New"})
    session.select_table(t.id)
    settings = window.map_view.settings if on_map else window.settings_panel
    field = settings.form.editor("table")._edit
    field.setFocus()
    focused(qtbot, field)
    field.end(False)
    QTest.keyClicks(field, "x")
    return field


def connection_state(session) -> tuple:
    """The project's connections, and the connection and table of T's step."""
    params = session.project.tables[0].steps[0].params
    return [c.name for c in session.project.connections], params["connection"], params["table"]


@pytest.mark.parametrize("on_map", [False, True], ids=["tables", "map"])
def test_undo_and_redo_from_the_toolbar_while_typing(window, session, settle, qtbot, tmp_path, on_map):
    """The toolbar's buttons leave the keyboard focus in the field typed in: Undo applies the
    typing first and takes back just that, and Redo brings it back. Ctrl+Z in the field is the
    field's own undo."""
    field = rename_then_type(window, session, settle, qtbot, tmp_path, on_map=on_map)
    history = len(session._undo)
    QTest.keyClick(field, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert field.text() == "t" and len(session._undo) == history and connection_state(session) == (["New"], "New", "t")
    QTest.keyClicks(field, "x")
    t, u = session.project.tables
    click(window, window.undo_action)  # the typing
    assert connection_state(session) == (["New"], "New", "t") and session.can_redo
    assert session.table_id == t.id and field.text() == "t"
    click(window, window.undo_action)  # the rename
    assert connection_state(session) == (["Old"], "Old", "t") and session.table_id == u.id
    click(window, window.redo_action)
    assert connection_state(session) == (["New"], "New", "t")
    click(window, window.redo_action)
    assert connection_state(session) == (["New"], "New", "tx") and not session.can_redo
    settle(session)


def test_an_undo_while_typing_keeps_the_settings_it_restores(window, session, settle, qtbot, tmp_path):
    """An undo in code while a field holds typing that is not applied yet: the settings the undo
    restores stay, only what was typed is applied to its step (not the out-of-date values of the
    form's other fields)."""
    rename_then_type(window, session, settle, qtbot, tmp_path)
    session.undo()  # the rename; the selection goes back to table U
    QApplication.processEvents()
    assert connection_state(session) == (["Old"], "Old", "tx")
    settle(session)


def type_a_note(window, session, qtbot, text: str, *, on_map=False):
    """Click into the note of Orders · 1 and type ``text`` without leaving the field."""
    window.show_map(on_map)
    select(session, "Orders", 1)
    note = (window.map_view.settings if on_map else window.settings_panel).note
    note.setFocus()
    focused(qtbot, note)
    QTest.keyClicks(note, text)
    return note


@pytest.mark.parametrize("on_map", [False, True], ids=["tables", "map"])
def test_undo_and_redo_from_the_toolbar_while_typing_a_note(shown, session, settle, qtbot, on_map):
    """Undo takes back the note typed, and the note field shows the note the step has then, also
    with the keyboard focus still in it: the next Undo (another step) doesn't write it back."""
    select(session, "Customers", 1)
    session.set_note(step(session, "Customers", 1).id, "Check the sheet")
    note = type_a_note(shown, session, qtbot, "Late orders", on_map=on_map)
    session.recalculate()
    settle(session)  # the results of the calculation: the typing stays
    assert note.text() == "Late orders" and step(session, "Orders", 1).note == ""
    click(shown, shown.undo_action)  # the note typed
    assert step(session, "Orders", 1).note == "" and session.can_redo
    assert note.text() == "" and note.hasFocus()
    click(shown, shown.undo_action)  # the note of Customers · 1, which is shown again
    assert session.step is step(session, "Customers", 1)
    assert set(notes_of(session).values()) == {""} and session.can_redo
    click(shown, shown.redo_action)
    click(shown, shown.redo_action)
    assert step(session, "Customers", 1).note == "Check the sheet" and step(session, "Orders", 1).note == "Late orders"
    assert not session.can_redo
    settle(session)


def test_a_note_applied_while_typing_keeps_what_is_typed(shown, session, settle, qtbot):
    """Saving and running apply the note typed so far (without its spaces at the end); the field
    keeps showing what is typed, so typing on adds to it. Once it is left, it shows the note."""
    note = type_a_note(shown, session, qtbot, "Check the ")
    shown.apply_typing()  # what Save and Run do first
    settle(session)
    assert step(session, "Orders", 1).note == "Check the" and note.text() == "Check the "
    QTest.keyClicks(note, "sheet ")
    shown.steps_panel.list.setFocus()
    QApplication.processEvents()
    settle(session)
    assert step(session, "Orders", 1).note == "Check the sheet" and note.text() == "Check the sheet"


@pytest.mark.parametrize("on_map", [False, True], ids=["tables", "map"])
def test_after_f3_while_typing_a_note_the_focus_stays_in_the_settings(shown, session, settle, qtbot, on_map):
    """The note goes to its own step; then the note field shows the note of the next result, and
    the keyboard focus goes to the settings panel: typing on doesn't change that note."""
    shown.open_find()
    shown.find_panel.set_text("")  # all imports, exports and commands
    note = type_a_note(shown, session, qtbot, "Late orders", on_map=on_map)
    QTest.keyClick(note, Qt.Key.Key_F3)
    QApplication.processEvents()
    assert step(session, "Orders", 1).note == "Late orders"
    shown_step = session.step
    assert shown_step is not step(session, "Orders", 1) and note.text() == shown_step.note == ""
    settings = shown.map_view.settings if on_map else shown.settings_panel
    focus = QApplication.focusWidget()
    assert focus is not note and focus is not None and settings.isAncestorOf(focus)
    QTest.keyClicks(focus, "more")
    QApplication.processEvents()
    assert session.step is shown_step and shown_step.note == "" and note.text() == ""
    settle(session)


def test_redo_while_typing_says_that_there_is_nothing_to_redo(shown, session, settle, qtbot):
    """Redo applies what is typed first: that is a change, so nothing is redone, and the status
    bar says why."""
    select(session, "Orders", 1)
    session.set_params(step(session, "Orders", 1).id, {"file": "data/orders2.csv"})
    session.undo()
    assert shown.redo_action.isEnabled()
    note = type_a_note(shown, session, qtbot, "Late orders")
    click(shown, shown.redo_action)
    assert step(session, "Orders", 1).note == "Late orders" and step(session, "Orders", 1).params["file"] == "data/orders.csv"
    assert not shown.redo_action.isEnabled() and "nothing to redo" in shown.statusBar().currentMessage()
    click(shown, shown.undo_action)
    assert step(session, "Orders", 1).note == "" and note.text() == "" and shown.redo_action.isEnabled()
    settle(session)
