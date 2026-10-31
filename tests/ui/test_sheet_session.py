"""The data sheet and the project: Add as table, Edit in sheet, files changed outside the app."""

import contextlib
import ctypes
import json
import os
import threading
from ctypes import wintypes
from pathlib import Path

import polars as pl
import pytest
import shiboken6
from PySide6.QtCore import QPointF, QSettings
from PySide6.QtWidgets import QMenu, QPushButton

from tafl.api import files
from tafl.core import Parameter, Project
from tafl.core import sheet as sheet_module
from tafl.ui import main_window as main_window_module
from tafl.ui import session as session_module
from tafl.ui import sheet_window
from tafl.ui.action_menu import fill_new_table_menu
from tafl.ui.forms import FormContext, PathEditor
from tafl.ui.session import Session
from tafl.ui.settings_panel import SettingsPanel
from tafl.ui.sheet_window import SheetWindow

ALL = [".xlsx", ".csv", ".txt", ".parquet"]
TEXT = "Order\tAmount\tName\r\n00123\t1,50\tMüller\r\n00124\t1E5\t\r\n00125\t27.02.2026\t a b \r\n"
ROWS = {
    "Order": ["00123", "00124", "00125"],
    "Amount": ["1,50", "1E5", "27.02.2026"],
    "Name": ["Müller", None, " a b "],
}


@contextlib.contextmanager
def held(path: Path):
    """The file stays open, as in another program that shares it for reading (e.g. Excel)."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(str(path), 0x80000000 | 0x40000000, 1, None, 3, 0, None)
    assert handle != wintypes.HANDLE(-1).value, ctypes.get_last_error()
    try:
        yield
    finally:
        kernel.CloseHandle(handle)


@pytest.fixture
def problems(monkeypatch) -> list[str]:
    shown: list[str] = []
    monkeypatch.setattr(SheetWindow, "show_problem", lambda self, text: shown.append(text))
    return shown


@pytest.fixture
def draft(window, qtbot, monkeypatch):
    """draft(text, ...): a sheet with ``text`` pasted. No questions on the way out."""
    made = []

    def make(text: str = TEXT, **settings) -> SheetWindow:
        monkeypatch.setattr(SheetWindow, "clipboard_text", lambda self: text)
        window.new_sheet(bool(text), **settings)
        made.append(window._sheets[-1])
        return made[-1]

    yield make
    for opened in list(window._sheets):
        opened.close_now()


@pytest.fixture
def add(draft, qtbot, monkeypatch, tmp_path):
    """add(file, ...): a sheet with ``text``, added as a table with that file."""

    def add_table(file: str | Path = "orders.csv", text: str = TEXT, **settings):
        path = tmp_path / file
        opened = draft(text, **settings)
        monkeypatch.setattr(opened, "ask_add", lambda: (path, path.suffix))
        monkeypatch.setattr(opened, "ask_replace", lambda _path: True)
        assert opened.add_as_table()
        qtbot.waitUntil(lambda: closed(opened), timeout=20_000)
        return path

    return add_table


@pytest.fixture
def edit(window, session, qtbot):
    """edit(step): the sheet of an import step, read."""

    def open_step(step) -> SheetWindow:
        session.want_sheet_edit(step.id)
        opened = window._sheets[-1]
        qtbot.waitUntil(lambda: not opened.busy, timeout=20_000)
        return opened

    return open_step


def closed(opened: SheetWindow) -> bool:
    return not shiboken6.isValid(opened) or not opened.isVisible()


def rows_of(opened: SheetWindow) -> list[list[str]]:
    sheet = opened.sheet
    return [[sheet.text(r, c) for c in range(sheet.column_count)] for r in range(sheet.row_count)]


def saved(opened: SheetWindow, qtbot, timeout: int = 20_000) -> None:
    assert opened.save()
    qtbot.waitUntil(lambda: not opened.busy, timeout=timeout)


# --- menus -------------------------------------------------------------------------------------


def test_the_new_table_menu_starts_with_the_sheets(qapp, registry):
    menu, chosen, sheets = QMenu(), [], []
    fill_new_table_menu(menu, registry, chosen.append, sheets.append)
    entries = menu.actions()
    assert [e.text() for e in entries[:2]] == ["Paste from clipboard", "Empty sheet"]
    assert entries[2].isSeparator() and entries[3].text()
    assert (
        entries[0].toolTip()
        == "A sheet with the data you copied, from a spreadsheet program, an ERP system, a web page or a mail"
    )
    assert entries[1].toolTip() == "A sheet to type data into"
    entries[0].trigger()
    entries[1].trigger()
    assert sheets == [True, False]
    entries[3].trigger()
    assert len(chosen) == 1


def test_the_menus_of_both_views_open_a_sheet(window, session, qtbot, monkeypatch):
    wanted = []
    session.new_sheet_wanted.disconnect(window.new_sheet)
    session.new_sheet_wanted.connect(lambda paste, position, group: wanted.append((paste, position, group)))
    window.tables_panel.add_menu.aboutToShow.emit()
    window.tables_panel.add_menu.actions()[0].trigger()
    window.tables_panel.add_menu.actions()[1].trigger()
    assert wanted == [(True, None, None), (False, None, None)]
    wanted.clear()
    window.show_map(True)
    window.map_view.new_sheet(True)
    window.map_view.new_sheet(False, QPointF(200, 150))
    assert wanted[0] == (True, None, None) and wanted[1][0] is False and wanted[1][1] == (200, 150)


def test_a_new_sheet_on_a_group_joins_the_group(window, session, add, qtbot, settle, registry):
    source = session.add_table(registry.actions["csv.import"])
    group = session.add_group("Inputs", [source.id])
    window.show_map(True)
    settle(session)
    view = window.map_view
    view.rebuild()
    spot = view.frames[group.id].rect.center()
    wanted = []
    session.new_sheet_wanted.connect(lambda paste, position, group_id: wanted.append((position, group_id)))
    view.new_sheet(False, spot)
    position, group_id = wanted[0]
    assert group_id == group.id
    add("placed.csv", position=position, group=group_id)
    table = session.project.table("placed")
    assert table.group == group.id and table.position == (round(position[0]), round(position[1]))


def test_a_group_that_is_gone_is_left_out(session, add):
    add("a.csv", position=(10, 20), group="nothing")
    assert session.project.table("a").group == ""


# --- Add as table ------------------------------------------------------------------------------


@pytest.mark.parametrize("suffix", ALL)
def test_add_as_table_writes_the_file_and_adds_one_import(window, session, settle, add, tmp_path, suffix):
    path = add(f"orders{suffix}")
    assert path.is_file() and not window._sheets
    assert [t.name for t in session.project.tables] == ["orders"]
    table = session.table
    assert table.name == "orders" and session.step_id is None and len(table.steps) == 1 and session.dirty
    assert session.can_undo
    settle(session)
    frame = session.result.df
    assert frame.to_dict(as_series=False) == ROWS  # exactly as the sheet showed it
    assert all(dtype == pl.String for dtype in frame.dtypes)
    assert window.statusBar().currentMessage() == f"Added “orders”: its data is in {path}"
    step = table.steps[0]
    assert step.params[files.SHEET_KEY]["file"] == path.name
    assert step.params[files.SHEET_KEY]["stamp"] == sheet_module.stamp_of(path)
    assert session.sheet_format(step).suffix == suffix


def test_add_as_table_is_one_undo_step_and_the_file_stays(session, settle, add):
    path = add("orders.csv")
    settle(session)
    session.undo()
    assert not session.project.tables and path.is_file() and not session.can_undo
    session.redo()
    settle(session)
    assert session.table.name == "orders" and session.result.ok
    assert session.table.steps[0].params[files.SHEET_KEY]["file"] == "orders.csv"


def test_the_table_is_a_normal_table_for_merges_runs_and_the_find_panel(
    window, session, settle, add, registry, tmp_path
):
    add("customers.csv", "No\tName\n1\tAnna\n2\tBen\n")
    add("orders.csv", "No\tNet\n1\t5\n1\t6\n2\t7\n")
    orders, customers = session.project.table("orders"), session.project.table("customers")
    for table in (customers, orders):
        session.select_table(table.id)
        settle(session)
    session.add_link(customers.id, orders.id, "left")
    export = session.add_step(registry.actions["csv.export"], params={"file": str(tmp_path / "out.csv")})
    settle(session)
    assert session.result.ok and session.result.df.columns[:2] == ["No", "Net"]
    assert session.step_result(export) is not None
    session.run_project()
    settle(session)
    assert (tmp_path / "out.csv").read_text(encoding="utf-8-sig").count("Anna") == 2
    window.open_find()
    window.find_panel.set_text("customers.csv")
    assert [hit.title.text for hit in window.find_panel.hits()] == ["customers · 1 · Import CSV / text files"]
    window.find_dock.hide()


def test_a_duplicate_of_the_table_keeps_the_mark(session, add):
    add("orders.csv")
    copy = session.duplicate_table(session.table.id)
    assert session.sheet_format(copy.steps[0]) is not None


def test_in_a_saved_project_the_path_is_relative_inside_its_folder(session, add, tmp_path):
    session.save_project(tmp_path / "p.tafl")
    add("orders.csv")
    assert session.table.steps[0].params["file"] == "orders.csv"
    (tmp_path / "sub").mkdir()
    add("sub/more.csv")
    assert session.table.steps[0].params["file"] == "sub/more.csv"


def test_outside_the_folder_of_the_project_the_path_is_full(session, add, tmp_path):
    (tmp_path / "project").mkdir()
    session.save_project(tmp_path / "project" / "p.tafl")
    add("orders.csv")
    assert session.table.steps[0].params["file"] == str(tmp_path / "orders.csv")


def test_in_an_unsaved_project_the_path_is_full_until_the_first_save(session, settle, add, tmp_path):
    path = add("orders.csv")
    step = session.table.steps[0]
    assert step.params["file"] == str(path)
    session.save_project(tmp_path / "p.tafl")
    assert session.table.steps[0].params["file"] == "orders.csv"
    assert session.sheet_format(session.table.steps[0]) is not None
    settle(session)
    assert session.result.ok


def test_the_mark_survives_save_and_load(session, add, tmp_path):
    add("orders.xlsx")
    project_file = session.save_project(tmp_path / "p.tafl")
    stored = json.loads(project_file.read_text(encoding="utf-8"))
    params = stored["tables"][0]["steps"][0]["params"]
    assert params[files.SHEET_KEY]["file"] == "orders.xlsx"
    assert "Müller" not in project_file.read_text(encoding="utf-8")  # the data stays in its file
    project = Project.load(project_file)
    assert project.tables[0].steps[0].params[files.SHEET_KEY]["file"] == "orders.xlsx"


def test_a_table_name_that_is_taken_gets_a_number(session, add, registry, tmp_path):
    table = session.add_table(registry.actions["csv.import"], name="orders")
    assert table.name == "orders"
    add("orders.csv")
    assert session.table.name == "orders 1"


def test_an_empty_sheet_says_so_and_writes_nothing(draft, monkeypatch):
    opened = draft("")
    monkeypatch.setattr(opened, "ask_add", lambda: pytest.fail("asked"))
    assert not opened.add_as_table()
    assert opened.status_text == "The sheet is empty: paste or type some data first."


def test_spare_rows_and_columns_at_the_end_are_not_written(session, settle, draft, qtbot, tmp_path, monkeypatch):
    opened = draft("")
    opened.model.setData(opened.model.index(0, 0), "x")
    opened.model.setData(opened.model.index(2, 0), "y")
    path = tmp_path / "typed.csv"
    monkeypatch.setattr(opened, "ask_add", lambda: (path, ".csv"))
    opened.add_as_table()
    qtbot.waitUntil(lambda: closed(opened))
    settle(session)
    assert session.result.df.columns == ["Column 1"]
    assert session.result.df["Column 1"].to_list() == ["x", None, "y"]


def test_cancel_in_the_popup_changes_nothing(session, draft, monkeypatch):
    opened = draft()
    monkeypatch.setattr(opened, "ask_add", lambda: None)
    assert not opened.add_as_table()
    assert opened.isVisible() and not session.project.tables and opened.sheet.changed


def test_an_existing_file_is_replaced_only_after_the_question(window, session, draft, qtbot, tmp_path, monkeypatch):
    path = tmp_path / "orders.csv"
    path.write_text("old", encoding="utf-8")
    opened = draft()
    answers = iter([(path, ".csv"), (path, ".csv"), None])
    asked = []
    monkeypatch.setattr(opened, "ask_add", lambda: next(answers))
    monkeypatch.setattr(opened, "ask_replace", lambda file: asked.append(file) or False)
    assert not opened.add_as_table()  # no: back to the popup; then Cancel there
    assert asked == [path, path] and path.read_text(encoding="utf-8") == "old"
    answers = iter([(path, ".csv")])
    monkeypatch.setattr(opened, "ask_replace", lambda file: True)
    assert opened.add_as_table()
    qtbot.waitUntil(lambda: closed(opened))
    assert "00123" in path.read_text(encoding="utf-8-sig")


def test_the_popup_comes_back_with_what_was_typed(session, draft, tmp_path, monkeypatch):
    path = tmp_path / "new" / "orders.csv"
    path.parent.mkdir()
    path.touch()
    opened = draft()
    dialogs = []

    def fake_exec(dialog):
        dialogs.append((dialog.name.text(), dialog.format.currentData(), dialog.folder.text()))
        return dialog.DialogCode.Rejected

    monkeypatch.setattr(sheet_window.AddSheetDialog, "exec", fake_exec)
    assert opened.ask_add() is None
    assert dialogs[0][0:2] == ("Sheet 1", ".xlsx")
    opened._choice = (path, ".csv")
    opened.ask_add()
    assert dialogs[1] == ("orders", ".csv", str(path.parent))


def test_the_popup_starts_in_the_folder_of_the_project_or_the_last_one(window, session, draft, tmp_path, monkeypatch):
    folders = []
    monkeypatch.setattr(
        sheet_window.AddSheetDialog, "exec", lambda d: folders.append(d.folder.text()) or d.DialogCode.Rejected
    )
    opened = draft()
    window.settings.setValue("last_dir", str(tmp_path))
    opened.ask_add()
    window.settings.setValue("last_dir", str(tmp_path / "gone"))
    opened.ask_add()
    (tmp_path / "project").mkdir()
    session.save_project(tmp_path / "project" / "p.tafl")
    opened.ask_add()
    assert folders == [str(tmp_path), str(Path.home()), str(tmp_path / "project")]


def test_a_failed_write_keeps_everything(session, draft, qtbot, tmp_path, problems, monkeypatch):
    (tmp_path / "taken").touch()
    opened = draft()
    monkeypatch.setattr(opened, "ask_add", lambda: (tmp_path / "taken" / "a.csv", ".csv"))
    assert opened.add_as_table()
    qtbot.waitUntil(lambda: not opened.busy)
    assert problems and opened.isVisible() and opened.sheet.changed
    assert not session.project.tables and rows_of(opened)[0][0] == "00123"
    assert opened.grid.isEnabled() and opened.main_action.isEnabled()


def test_a_file_held_open_says_so_and_keeps_the_sheet(session, draft, qtbot, tmp_path, problems, monkeypatch):
    path = tmp_path / "orders.csv"
    path.write_text("old", encoding="utf-8")
    opened = draft()
    monkeypatch.setattr(opened, "ask_add", lambda: (path, ".csv"))
    monkeypatch.setattr(opened, "ask_replace", lambda _path: True)
    with held(path):
        opened.add_as_table()
        qtbot.waitUntil(lambda: not opened.busy)
    assert problems == ["orders.csv is open in another program (e.g. Excel): close it and try again"]
    assert path.read_text(encoding="utf-8") == "old" and not session.project.tables


def test_stop_leaves_the_old_file_and_the_sheet(session, draft, qtbot, tmp_path, monkeypatch):
    path = tmp_path / "big.xlsx"
    path.write_bytes(b"old")
    opened = draft("a\tb\n" + "1\t2\n" * 200_000)
    qtbot.waitUntil(lambda: not opened.busy, timeout=20_000)
    monkeypatch.setattr(opened, "ask_add", lambda: (path, ".xlsx"))
    monkeypatch.setattr(opened, "ask_replace", lambda _path: True)
    assert opened.add_as_table()
    assert not opened.stop_button.isHidden() and opened.status_text == "Writing big.xlsx…"
    opened.stop_button.click()
    qtbot.waitUntil(lambda: not opened.busy, timeout=20_000)
    assert opened.status_text == "Stopped: the file was not written." and opened.stop_button.isHidden()
    assert path.read_bytes() == b"old" and not session.project.tables and opened.isVisible()
    assert opened.sheet.changed and opened.grid.isEnabled()
    assert [p.name for p in tmp_path.iterdir()] == ["big.xlsx"]


def test_writing_xlsx_shows_a_percentage(draft, qtbot, tmp_path, monkeypatch):
    shown = []
    opened = draft()
    monkeypatch.setattr(opened, "ask_add", lambda: (tmp_path / "a.xlsx", ".xlsx"))
    original = opened._show_progress
    monkeypatch.setattr(opened, "_status", lambda text, status=opened._status: shown.append(text) or status(text))
    assert opened.add_as_table()
    original(0.25)
    assert "Writing a.xlsx… 25%" in shown
    qtbot.waitUntil(lambda: closed(opened))


def test_a_file_replaced_by_a_new_sheet_is_read_again_by_its_table(session, settle, add):
    add("orders.csv")
    settle(session)
    first = session.table
    assert session.result.df.height == 3
    add("orders.csv", "Order\tAmount\n1\t2\n")
    settle(session)
    assert session.results[first.id].df.height == 1
    assert session.table.name == "orders 1"
    assert first.steps[0].params[files.SHEET_KEY]["stamp"] == session.table.steps[0].params[files.SHEET_KEY]["stamp"]


def test_a_draft_is_added_to_the_project_that_is_open_then(
    window, session, settle, demo, draft, qtbot, tmp_path, monkeypatch
):
    opened = draft()
    window.open_path(demo)
    settle(session)
    monkeypatch.setattr(opened, "ask_add", lambda: (tmp_path / "late.csv", ".csv"))
    opened.add_as_table()
    qtbot.waitUntil(lambda: closed(opened))
    assert session.project.table("late") is not None and session.project.path == demo


# --- Edit in sheet: the button -----------------------------------------------------------------


def buttons(window) -> list[QPushButton]:
    return [window.settings_panel.sheet_button, window.map_view.settings.sheet_button]


def test_the_button_is_only_there_for_files_a_sheet_wrote(window, session, add, registry, tmp_path):
    other = tmp_path / "other.csv"
    other.write_text("a;b\n1;2\n", encoding="utf-8")
    table = session.add_table(registry.actions["csv.import"])
    session.set_params(table.steps[0].id, {"file": str(other)})
    assert all(b.isHidden() for b in buttons(window))
    add("orders.csv")
    assert not any(b.isHidden() for b in buttons(window))
    assert buttons(window)[0].text() == "Edit in sheet" and buttons(window)[0].toolTip().startswith(
        "Open the file of this import"
    )
    session.select_table(table.id)
    assert all(b.isHidden() for b in buttons(window))


def test_the_button_goes_with_another_file_and_comes_back_with_undo(window, session, add, tmp_path):
    add("orders.csv")
    step = session.table.steps[0]
    session.set_params(step.id, {"file": str(tmp_path / "other.csv")})
    assert all(b.isHidden() for b in buttons(window))
    session.undo()
    assert not any(b.isHidden() for b in buttons(window))


def test_the_button_stays_for_a_step_that_is_off_and_goes_without_a_step(window, session, add):
    add("orders.csv")
    session.set_enabled(session.table.steps[0].id, False)
    assert not buttons(window)[0].isHidden()
    session.delete_table(session.table.id)
    assert all(b.isHidden() for b in buttons(window))


@pytest.mark.parametrize("on_map", [False, True])
def test_a_click_opens_the_sheet_in_both_views(window, session, add, qtbot, on_map):
    add("orders.csv")
    (window.map_view.settings if on_map else window.settings_panel).sheet_button.click()
    opened = window._sheets[-1]
    qtbot.waitUntil(lambda: not opened.busy)
    assert opened.title == "orders.csv" and rows_of(opened)[0][0] == "00123"
    opened.close_now()


def test_the_settings_panel_works_without_a_main_window(session, add):
    panel = SettingsPanel(session, lambda: None)
    wanted = []
    session.sheet_edit_wanted.connect(wanted.append)
    add("orders.csv")
    panel.sheet_button.click()
    assert wanted == [session.table.steps[0].id]


# --- Edit in sheet: reading --------------------------------------------------------------------


@pytest.mark.parametrize("suffix", ALL)
def test_the_sheet_shows_the_file_not_a_later_step(session, settle, add, edit, registry, suffix):
    add(f"orders{suffix}")
    step = session.table.steps[0]
    session.add_step(registry.actions["rows.filter"], params={"condition": "[Order] = '00123'"})
    settle(session)
    opened = edit(step)
    assert not opened.is_draft and opened.sheet.names == ["Order", "Amount", "Name"]
    assert rows_of(opened) == [["00123", "1,50", "Müller"], ["00124", "1E5", ""], ["00125", "27.02.2026", " a b "]]
    assert opened.main_button.text() == "Save" and opened.switch.isHidden() and not opened.sheet.changed
    assert opened.windowTitle() == f"orders{suffix}  —  Tafl"
    opened.close_now()


def test_the_settings_of_the_step_do_not_change_what_the_sheet_shows(session, settle, add, edit):
    add("orders.csv", "Net\tVat\n1,50\t7\n2,50\t8\n")
    step = session.table.steps[0]
    session.set_params(step.id, {"detect_types": True})
    settle(session)
    assert session.result.df["Vat"].to_list() == [7, 8]
    opened = edit(step)
    assert rows_of(opened) == [["1,50", "7"], ["2,50", "8"]]
    opened.close_now()


def test_a_second_click_raises_the_same_window(window, session, add, edit):
    add("orders.csv")
    step = session.table.steps[0]
    opened = edit(step)
    raised = []
    opened.raise_ = lambda: raised.append(1)
    session.want_sheet_edit(step.id)
    assert window._sheets == [opened] and raised == [1]
    copy = session.duplicate_table(session.table.id)
    session.want_sheet_edit(copy.steps[0].id)  # the same file
    assert window._sheets == [opened] and raised == [1, 1]
    opened.close_now()


def test_a_file_that_is_gone_is_said_plainly(window, session, add, monkeypatch):
    path = add("orders.csv")
    shown = []
    monkeypatch.setattr(window, "show_sheet_problem", shown.append)
    path.unlink()
    session.want_sheet_edit(session.table.steps[0].id)
    assert not window._sheets
    assert shown == [
        f"“orders.csv” is not there any more:\n{path}\n\nPut the file back, or choose another file in the step's settings."
    ]


def test_a_file_changed_outside_asks_before_opening(window, session, add, edit, monkeypatch):
    path = add("orders.csv")
    step = session.table.steps[0]
    assert not session.sheet_outdated(step)
    path.write_text("x;y\n1;2\n", encoding="utf-8")
    assert session.sheet_outdated(step)
    asked = []
    monkeypatch.setattr(window, "ask_open_changed", lambda file: asked.append(file) or False)
    session.want_sheet_edit(step.id)
    assert asked == [path] and not window._sheets
    monkeypatch.setattr(window, "ask_open_changed", lambda file: True)
    opened = edit(step)
    assert rows_of(opened) == [["1", "2"]]
    opened.close_now()


def test_the_question_about_a_changed_file_comes_once(window, session, add, edit, monkeypatch):
    path = add("orders.csv")
    step = session.table.steps[0]
    path.write_text("x;y\n1;2\n", encoding="utf-8")
    asked = []
    monkeypatch.setattr(window, "ask_open_changed", lambda file: asked.append(file) or True)
    edit(step).close_now()
    edit(step).close_now()
    assert asked == [path]
    path.write_text("x;y\n3;4;\n", encoding="utf-8")
    edit(step).close_now()
    assert asked == [path, path]


def test_cancel_at_the_question_keeps_it_for_the_next_click(window, session, add, monkeypatch):
    path = add("orders.csv")
    step = session.table.steps[0]
    path.write_text("x;y\n1;2\n", encoding="utf-8")
    monkeypatch.setattr(window, "ask_open_changed", lambda file: False)
    session.want_sheet_edit(step.id)
    assert session.sheet_outdated(step)


def test_a_saved_sheet_gives_no_question_when_the_project_was_not_saved(
    window, session, add, edit, qtbot, tmp_path, monkeypatch
):
    """Only Tafl wrote the file, though the project file holds the old stamp."""
    add("orders.csv")
    project = session.save_project(tmp_path / "p.tafl")
    opened = edit(session.table.steps[0])
    opened.model.setData(opened.model.index(0, 0), "x")
    saved(opened, qtbot)
    opened.close_now()
    session._set_dirty(False)  # "Don't save"
    window.open_path(project)
    monkeypatch.setattr(window, "ask_open_changed", lambda file: pytest.fail("asked"))
    opened = edit(session.table.steps[0])
    assert rows_of(opened)[0][0] == "x"
    opened.close_now()


def test_a_saved_sheet_gives_no_question_after_a_restart_of_the_app(session, registry, add, edit, qtbot, tmp_path):
    add("orders.csv")
    project = session.save_project(tmp_path / "p.tafl")
    opened = edit(session.table.steps[0])
    opened.model.setData(opened.model.index(0, 0), "x")
    saved(opened, qtbot)
    opened.close_now()
    later = Session(registry)
    later.open_project(project)
    assert not later.sheet_outdated(later.project.tables[0].steps[0])


def test_a_change_outside_after_a_saved_sheet_is_still_seen(session, add, edit, qtbot):
    path = add("orders.csv")
    step = session.table.steps[0]
    opened = edit(step)
    opened.model.setData(opened.model.index(0, 0), "x")
    saved(opened, qtbot)
    opened.close_now()
    path.write_text("x;y\n1;2\n", encoding="utf-8")
    assert session.sheet_outdated(session.table.steps[0])


def test_only_the_last_files_are_remembered(session, tmp_path, monkeypatch):
    monkeypatch.setattr(session_module, "_SHEET_STAMPS", 2)
    for name in ("a", "b", "c"):
        session._remember_stamp(tmp_path / name, name)
    assert list(session._sheet_stamps.values()) == ["b", "c"]
    assert session_module._saved_sheet_stamps() == session._sheet_stamps


def test_the_remembered_stamps_survive_a_broken_setting(qapp):
    QSettings().setValue("sheet_stamps", "not json")
    assert session_module._saved_sheet_stamps() == {}
    QSettings().remove("sheet_stamps")


def test_save_drops_the_results_of_the_old_file(session, settle, add, edit, qtbot):
    add("orders.csv")
    table = session.table
    derived = session.add_derived_table(table.id)
    session.select_table(derived.id)
    settle(session)
    session.select_table(table.id)
    settle(session)
    cache = session.engine.cache
    assert len(cache) >= 2
    opened = edit(table.steps[0])
    opened.model.setData(opened.model.index(0, 0), "x")
    old = {key for key, item in cache._items.items()}
    saved(opened, qtbot)
    settle(session)
    assert not old & set(cache._items)
    assert session.result.df["Order"][0] == "x"
    opened.close_now()


def test_save_keeps_the_results_of_the_steps_before_the_table_of_the_file_is_used(
    session, settle, add, edit, qtbot
):
    add("lookup.csv", "No\tName\n1\tAnna\n")
    add("orders.csv", "No\tNet\n1\t5\n")
    lookup, orders = session.project.table("lookup"), session.project.table("orders")
    for table in (lookup, orders):
        session.select_table(table.id)
        settle(session)
    session.add_link(lookup.id, orders.id, "left")
    settle(session)
    opened = edit(lookup.steps[0])
    opened.model.setData(opened.model.index(0, 1), "Ben")
    saved(opened, qtbot)
    settle(session)
    session.select_table(orders.id)
    settle(session)
    assert [step.cached for step in session.result.steps] == [True, False]
    assert session.result.df["Name"].to_list() == ["Ben"]
    opened.close_now()


def test_a_plain_import_of_the_file_gets_the_new_data_with_a_save(session, settle, add, edit, registry, qtbot):
    path = add("orders.csv")
    table = session.table
    plain = session.add_table(registry.actions["csv.import"], name="plain")
    session.set_params(plain.steps[0].id, {"source": "file", "file": str(path), "delimiter": ";", "detect_types": False})
    settle(session)
    session.select_table(table.id)
    settle(session)
    opened = edit(table.steps[0])
    opened.model.setData(opened.model.index(0, 0), "x")
    saved(opened, qtbot)
    settle(session)
    assert session.results[plain.id].df["Order"][0] == "x" and session.results[table.id].df["Order"][0] == "x"
    opened.close_now()


def test_a_save_of_a_file_that_only_a_plain_import_reads_changes_nothing_in_the_project(
    session, settle, add, edit, registry, qtbot
):
    path = add("orders.csv")
    session.delete_table(session.table.id)
    plain = session.add_table(registry.actions["csv.import"], name="plain")
    session.set_params(plain.steps[0].id, {"source": "file", "file": str(path), "delimiter": ";"})
    settle(session)
    session._set_dirty(False)
    session.sheet_saved(path)
    settle(session)
    assert not session.dirty


def test_a_row_longer_than_the_first_is_said_before_it_is_lost(window, session, add, edit, problems, monkeypatch):
    path = add("orders.csv", "a\tb\n1\tx\n")
    path.write_text(path.read_text(encoding="utf-8-sig") + "2;y;EXTRA\r\n", encoding="utf-8-sig")
    monkeypatch.setattr(window, "ask_open_changed", lambda file: True)
    opened = edit(session.table.steps[0])
    assert rows_of(opened) == [["1", "x"], ["2", "y"]]
    assert len(problems) == 1 and "left out" in problems[0] and problems[0].endswith("Save writes only what this sheet shows.")
    opened.close_now()


def test_numbers_in_a_changed_excel_file_are_shown_as_text(window, session, add, edit, monkeypatch):
    path = add("orders.xlsx")
    pl.DataFrame({"a": [1, 2], "b": [1.5, None], "c": [True, False]}).write_excel(path)
    monkeypatch.setattr(window, "ask_open_changed", lambda file: True)
    opened = edit(session.table.steps[0])
    assert rows_of(opened)[0][0] == "1" and all(column.dtype == pl.String for column in opened.sheet._columns)
    opened.close_now()


def test_a_file_that_can_t_be_read_closes_the_window_without_a_question(
    window, session, add, qtbot, problems, monkeypatch
):
    path = add("orders.parquet")
    path.write_bytes(b"not parquet")
    monkeypatch.setattr(window, "ask_open_changed", lambda file: True)
    monkeypatch.setattr(SheetWindow, "ask_close_edit", lambda self: pytest.fail("asked"))
    session.want_sheet_edit(session.table.steps[0].id)
    qtbot.waitUntil(lambda: not window._sheets)
    assert problems


def test_a_change_while_reading_counts_as_a_change(session, add, edit, monkeypatch):
    path = add("orders.csv")
    step = session.table.steps[0]
    real = sheet_window.read_file

    def changed_meanwhile(*args):
        frame = real(*args)
        path.write_text("x;y\n1;2\n", encoding="utf-8")
        return frame

    monkeypatch.setattr(sheet_window, "read_file", changed_meanwhile)
    opened = edit(step)
    opened.model.setData(opened.model.index(0, 0), "x")
    asked = []
    monkeypatch.setattr(opened, "ask_save_changed", lambda file: asked.append(file) or False)
    assert not opened.save()
    assert asked == [path] and opened.sheet.changed
    opened.close_now()


# --- Edit in sheet: saving ---------------------------------------------------------------------


@pytest.mark.parametrize("suffix", ALL)
def test_save_writes_the_file_and_the_table_is_calculated_again(window, session, settle, add, edit, qtbot, suffix):
    path = add(f"orders{suffix}")
    settle(session)
    step = session.table.steps[0]
    opened = edit(step)
    session._set_dirty(False)
    undo_steps = len(session._undo)
    opened.model.setData(opened.model.index(0, 1), "9,99")
    opened.add_row_action.trigger()
    assert opened.windowTitle().startswith(f"orders{suffix} •")
    saved(opened, qtbot)
    assert opened.status_text == f"Saved orders{suffix}" and not opened.sheet.changed and opened.isVisible()
    assert opened.windowTitle() == f"orders{suffix}  —  Tafl"
    assert session.dirty and len(session._undo) == undo_steps  # the new stamp, no undo step
    settle(session)
    assert session.result.df["Amount"].to_list() == ["9,99", None, "1E5", "27.02.2026"]
    stored = session.table.steps[0].params[files.SHEET_KEY]["stamp"]
    assert stored == sheet_module.stamp_of(path)
    assert not session.sheet_outdated(session.table.steps[0])
    opened.close_now()


def test_save_recalculates_the_tables_that_use_it(session, settle, add, edit, qtbot, registry):
    add("orders.csv")
    orders = session.table
    derived = session.add_derived_table(orders.id)
    last = session.add_derived_table(derived.id)
    settle(session)
    session.select_table(derived.id)
    settle(session)
    session.select_table(last.id)
    settle(session)
    assert session.results[last.id].df.height == 3
    session.select_table(orders.id)
    settle(session)
    opened = edit(orders.steps[0])
    opened.delete_rows_action.trigger()
    saved(opened, qtbot)
    settle(session)
    assert session.results[orders.id].df.height == 2
    assert session.results[derived.id].df.height == 2 and session.results[last.id].df.height == 2
    opened.close_now()


def test_save_changes_every_table_that_reads_the_file(session, settle, add, edit, qtbot):
    add("orders.csv")
    first = session.table
    copy = session.duplicate_table(first.id)
    settle(session)
    opened = edit(first.steps[0])
    opened.model.setData(opened.model.index(0, 0), "x")
    saved(opened, qtbot)
    settle(session)
    assert session.results[copy.id].df["Order"][0] == "x"
    stamps = {t.steps[0].params[files.SHEET_KEY]["stamp"] for t in (first, copy)}
    assert stamps == {sheet_module.stamp_of(session.sheet_file(first.steps[0]))}
    opened.close_now()


def test_save_with_undo_in_the_project_gives_no_false_question(session, settle, add, edit, qtbot):
    """Undo in the main window brings back the stamp from before the save."""
    add("orders.csv")
    step = session.table.steps[0]
    session.set_note(step.id, "a note")
    opened = edit(step)
    opened.model.setData(opened.model.index(0, 0), "x")
    saved(opened, qtbot)
    session.undo()
    assert not session.sheet_outdated(session.table.steps[0])
    opened.close_now()


def test_save_writes_only_the_step_s_stamp_as_a_new_dict(session, add, edit, qtbot):
    add("orders.csv")
    step = session.table.steps[0]
    params = step.params
    opened = edit(step)
    opened.model.setData(opened.model.index(0, 0), "x")
    saved(opened, qtbot)
    assert session.table.steps[0].params is not params
    opened.close_now()


def test_save_while_the_project_runs_waits(session, add, edit, monkeypatch):
    add("orders.csv")
    opened = edit(session.table.steps[0])
    opened.model.setData(opened.model.index(0, 0), "x")
    monkeypatch.setattr(type(session), "running", property(lambda self: True))
    assert not opened.save()
    assert opened.status_text == "The project is running: save when the run has finished."
    monkeypatch.undo()
    opened.close_now()


def test_excel_limits_at_save(session, add, edit, problems, monkeypatch):
    add("orders.xlsx")
    opened = edit(session.table.steps[0])
    opened.model.setData(opened.model.index(0, 0), "x" * 40_000)
    assert not opened.save()
    assert problems == [
        "1 cell has more than 32,767 characters, the most an Excel cell holds. Choose CSV, TXT or Parquet."
    ]
    opened.close_now()


def test_a_file_changed_outside_asks_before_saving(session, add, edit, qtbot, monkeypatch):
    path = add("orders.csv")
    opened = edit(session.table.steps[0])
    opened.model.setData(opened.model.index(0, 0), "x")
    path.write_text("new;data\n1;2\n", encoding="utf-8")
    asked = []
    monkeypatch.setattr(opened, "ask_save_changed", lambda file: asked.append(file) or False)
    assert not opened.save()
    assert asked == [path] and path.read_text(encoding="utf-8").startswith("new") and opened.sheet.changed
    monkeypatch.setattr(opened, "ask_save_changed", lambda file: True)
    saved(opened, qtbot)
    assert "00124" in path.read_text(encoding="utf-8-sig")
    opened.close_now()


def test_a_file_removed_outside_asks_before_saving(session, add, edit, qtbot, monkeypatch):
    path = add("orders.csv")
    opened = edit(session.table.steps[0])
    opened.model.setData(opened.model.index(0, 0), "x")
    path.unlink()
    monkeypatch.setattr(opened, "ask_save_gone", lambda file: False)
    assert not opened.save()
    assert not path.exists() and opened.sheet.changed
    monkeypatch.setattr(opened, "ask_save_gone", lambda file: True)
    saved(opened, qtbot)
    assert path.is_file()
    opened.close_now()


def test_a_second_save_after_outside_changes_does_not_ask(session, add, edit, qtbot, monkeypatch):
    path = add("orders.csv")
    opened = edit(session.table.steps[0])
    opened.model.setData(opened.model.index(0, 0), "x")
    monkeypatch.setattr(opened, "ask_save_changed", lambda file: pytest.fail("asked"))
    saved(opened, qtbot)
    opened.model.setData(opened.model.index(0, 0), "y")
    saved(opened, qtbot)
    assert "y" in path.read_text(encoding="utf-8-sig")
    opened.close_now()


def test_nothing_to_save(session, add, edit):
    add("orders.csv")
    opened = edit(session.table.steps[0])
    assert not opened.save()
    assert opened.status_text == "Nothing to save: the file has this data."
    opened.close_now()


def test_paste_into_a_file_without_rows_keeps_its_names(session, settle, add, edit, qtbot, monkeypatch):
    add("names.csv", "a\tb\n1\t2\n")
    step = session.table.steps[0]
    opened = edit(step)
    opened.delete_rows_action.trigger()
    saved(opened, qtbot)
    assert opened.sheet.row_count == 0 and not opened.sheet.has_values
    monkeypatch.setattr(opened, "clipboard_text", lambda: "x\ty\n")
    opened.paste_action.trigger()
    assert opened.sheet.names == ["a", "b"] and rows_of(opened) == [["x", "y"]] and not opened.sheet.header
    opened.close_now()


def test_a_file_held_open_fails_and_a_second_save_works(session, settle, add, edit, qtbot, problems):
    path = add("orders.csv")
    settle(session)
    opened = edit(session.table.steps[0])
    opened.model.setData(opened.model.index(0, 0), "x")
    before = path.read_bytes()
    with held(path):
        assert opened.save()
        qtbot.waitUntil(lambda: not opened.busy)
    assert problems[0].startswith("orders.csv is open in another program (e.g. Excel): close it and try again")
    assert path.read_bytes() == before and opened.sheet.changed and opened.isVisible()
    saved(opened, qtbot)
    assert not opened.sheet.changed and b"x" in path.read_bytes()
    opened.close_now()


def test_tafl_itself_reading_the_file_is_mentioned(draft, session, monkeypatch):
    opened = draft("")
    monkeypatch.setattr(type(session), "busy", property(lambda self: True))
    text = opened._problem_text(ValueError("a.csv is open in another program (e.g. Excel): close it and run again"))
    assert text == (
        "a.csv is open in another program (e.g. Excel): close it and try again "
        "(Tafl itself may be reading it: try again in a moment.)"
    )
    assert opened._problem_text(ValueError("No permission")) == "No permission"
    monkeypatch.undo()


def test_another_project_leaves_the_edit_window_open_and_save_writes_only_the_file(
    window, session, settle, add, edit, qtbot, demo
):
    path = add("orders.csv")
    opened = edit(session.table.steps[0])
    opened.model.setData(opened.model.index(0, 0), "x")
    session._set_dirty(False)
    window.open_path(demo)
    settle(session)
    assert opened.status_text == "This file is not part of the open project any more: Save writes only the file."
    saved(opened, qtbot)
    assert "x" in path.read_text(encoding="utf-8-sig") and not session.dirty
    assert opened.status_text == "Saved orders.csv"
    opened.close_now()


def test_another_project_says_so_at_once_after_a_message(window, session, settle, add, edit, qtbot, demo):
    add("orders.csv")
    opened = edit(session.table.steps[0])
    opened.model.setData(opened.model.index(0, 0), "x")
    saved(opened, qtbot)
    assert opened.status_text == "Saved orders.csv"
    session._set_dirty(False)
    window.open_path(demo)
    settle(session)
    assert opened.status_text == "This file is not part of the open project any more: Save writes only the file."
    opened.close_now()


def test_the_same_project_opened_again_is_no_other_project(window, session, settle, add, edit, tmp_path):
    add("orders.csv")
    session.save_project(tmp_path / "p.tafl")
    opened = edit(session.table.steps[0])
    window.open_path(tmp_path / "p.tafl")
    settle(session)
    assert "not part" not in opened.status_text
    opened.close_now()


def test_a_closed_edit_window_doesn_t_hear_of_another_project(window, session, add, edit, qapp):
    add("orders.csv")
    opened = edit(session.table.steps[0])
    opened.close()
    qapp.sendPostedEvents(None, 0)  # the window is deleted now
    qapp.processEvents()
    session.new_project()
    assert not window._sheets


def test_a_deleted_table_leaves_the_file_and_a_save_still_writes_it(session, add, edit, qtbot):
    path = add("orders.csv")
    opened = edit(session.table.steps[0])
    opened.model.setData(opened.model.index(0, 0), "x")
    session.delete_table(session.table.id)
    assert path.is_file()
    saved(opened, qtbot)
    assert "x" in path.read_text(encoding="utf-8-sig")
    opened.close_now()


def test_save_as_keeps_the_button_and_the_file(session, add, tmp_path):
    add("orders.csv")
    (tmp_path / "elsewhere").mkdir()
    session.save_project(tmp_path / "elsewhere" / "p.tafl")
    step = session.table.steps[0]
    assert step.params["file"] == str(tmp_path / "orders.csv") and session.sheet_format(step) is not None
    assert session.sheet_file(step) == tmp_path / "orders.csv"


def test_a_path_with_a_parameter_finds_the_file(session, add, tmp_path):
    add("orders.csv")
    session.set_parameters([Parameter("Folder", str(tmp_path))])
    step = session.table.steps[0]
    session.set_params(step.id, {"file": "{Folder}/orders.csv"})
    step = session.table.steps[0]
    assert session.sheet_format(step) is not None
    assert os.path.normcase(session.sheet_file(step)) == os.path.normcase(tmp_path / "orders.csv")


def test_a_file_of_the_same_name_in_another_folder_is_a_normal_import(session, add, tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    add("a/orders.xlsx")
    table = session.table
    session.set_params(table.steps[0].id, {"file": str(tmp_path / "b" / "orders.xlsx")})
    step = table.steps[0]
    assert session.sheet_format(step) is None and files.SHEET_KEY not in step.params
    session.undo()
    assert session.sheet_format(session.table.steps[0]) is not None


def test_a_file_named_by_a_parameter_is_a_normal_import(session, add):
    add("orders.csv")
    session.set_parameters([Parameter("Name", "orders")])
    step = session.table.steps[0]
    session.set_params(step.id, {"file": "{Name}.csv"})
    assert files.SHEET_KEY not in session.table.steps[0].params


def test_a_parameter_that_points_to_another_file_ends_the_mark(session, add, tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    add("a/orders.xlsx")
    session.set_parameters([Parameter("Folder", str(tmp_path / "a"))])
    session.set_params(session.table.steps[0].id, {"file": "{Folder}/orders.xlsx"})
    session.set_parameters([Parameter("Folder", str(tmp_path / "a")), Parameter("Other", "x")])
    assert session.sheet_format(session.table.steps[0]) is not None
    session.set_parameters([Parameter("Folder", str(tmp_path / "b"))])
    assert session.sheet_format(session.table.steps[0]) is None
    session.undo()
    assert session.sheet_format(session.table.steps[0]) is not None


def test_a_renamed_parameter_keeps_the_mark(session, add, tmp_path):
    add("orders.csv")
    session.set_parameters([Parameter("Folder", str(tmp_path))])
    session.set_params(session.table.steps[0].id, {"file": "{Folder}/orders.csv"})
    session.set_parameters([Parameter("Place", str(tmp_path))], {"Folder": "Place"})
    step = session.table.steps[0]
    assert step.params["file"] == "{Place}/orders.csv" and session.sheet_format(step) is not None


@pytest.mark.parametrize("folder", ["~old", "~$sub"])
def test_a_folder_with_a_tilde_stays_valid_after_the_first_save(session, settle, add, tmp_path, folder):
    (tmp_path / folder).mkdir()
    path = add(f"{folder}/x.csv", "a\tb\n1\t2\n")
    session.save_project(tmp_path / "p.tafl")
    step = session.table.steps[0]
    assert step.params["file"] == str(path) and session.sheet_file(step) == path
    assert session.sheet_path_problem(path) is None
    session.calculate_now()
    settle(session)
    assert session.results[session.table.id].df.rows() == [("1", "2")]


@pytest.mark.parametrize("folder", ["100%", "10% - 20%", "%NOSUCHVARIABLE%"])
def test_a_folder_with_a_percent_sign_stays_valid_after_save_as(session, settle, add, tmp_path, folder):
    (tmp_path / "p" / folder).mkdir(parents=True)
    (tmp_path / "q").mkdir()
    session.save_project(tmp_path / "p" / "p.tafl")
    path = add(f"p/{folder}/s.csv", "a\tb\n1\t2\n")
    assert session.table.steps[0].params["file"] == f"{folder}/s.csv"
    session.save_project(tmp_path / "q" / "p.tafl")
    step = session.table.steps[0]
    assert step.params["file"] == str(path) and session.sheet_file(step) == path


def test_a_variable_that_says_where_the_path_starts_stays_at_save_as(session, add, registry, tmp_path, monkeypatch):
    monkeypatch.setenv("TAFL_TEST_PLACE", str(tmp_path / "elsewhere"))
    (tmp_path / "p").mkdir()
    (tmp_path / "q").mkdir()
    session.save_project(tmp_path / "p" / "p.tafl")
    table = session.add_table(registry.actions["csv.import"], name="plain")
    session.set_params(table.steps[0].id, {"source": "file", "file": "%TAFL_TEST_PLACE%/x.csv"})
    session.save_project(tmp_path / "q" / "p.tafl")
    assert session.table.steps[0].params["file"] == "%TAFL_TEST_PLACE%/x.csv"


@pytest.mark.parametrize("text", ["%BATCH_ONLY%/x.csv", "%BATCH_ONLY%", "%BATCH-ONLY%\\x.csv", "%A%%B%\\x.csv", "%BATCH_ONLY%.csv"])
def test_a_variable_of_a_batch_file_stays_at_save_as(session, registry, tmp_path, text):
    (tmp_path / "p").mkdir()
    (tmp_path / "q").mkdir()
    session.save_project(tmp_path / "p" / "p.tafl")
    table = session.add_table(registry.actions["csv.import"], name="plain")
    session.set_params(table.steps[0].id, {"source": "file", "file": text})
    session.save_project(tmp_path / "q" / "p.tafl")
    assert session.table.steps[0].params["file"] == text


def test_a_file_chosen_in_a_folder_with_a_tilde_stays_a_full_path(qapp, registry, tmp_path):
    param = next(p for p in registry.actions["csv.import"].params if p.key == "file")
    field = PathEditor(param, FormContext(project_dir=tmp_path))
    assert field._relative(str(tmp_path / "~old" / "x.csv")) == str(tmp_path / "~old" / "x.csv")
    assert field._relative(str(tmp_path / "sub" / "x.csv")) == "sub/x.csv"


def test_a_path_with_dots_is_the_same_file_as_without_them(window, session, settle, add, edit, qtbot, tmp_path):
    (tmp_path / "sub").mkdir()
    add("s.csv", "a\tb\n1\t2\n")
    first = session.table
    copy = session.duplicate_table(first.id)
    session.set_params(copy.steps[0].id, {"file": str(tmp_path / "sub" / ".." / "s.csv")})
    assert session.sheet_format(copy.steps[0]) is not None
    settle(session)
    opened = edit(first.steps[0])
    session.want_sheet_edit(copy.steps[0].id)
    assert window._sheets == [opened]
    opened.model.setData(opened.model.index(0, 0), "x")
    saved(opened, qtbot)
    settle(session)
    assert session.results[copy.id].df["a"][0] == "x" and not session.sheet_outdated(copy.steps[0])
    opened.close_now()


def test_a_typed_path_with_dots_outside_the_project_is_read(window, session, settle, add, edit, registry, qtbot, tmp_path):
    (tmp_path / "proj").mkdir()
    path = add("one.csv", "a\tb\n1\t2\n")
    opened = edit(session.table.steps[0])
    session.delete_table(session.table.id)
    session.save_project(tmp_path / "proj" / "p.tafl")
    plain = session.add_table(registry.actions["csv.import"], name="plain")
    session.set_params(plain.steps[0].id, {"source": "file", "file": "../one.csv", "delimiter": ";"})
    settle(session)
    assert session.reads_file(path) and opened.status_text == ""
    opened.model.setData(opened.model.index(0, 0), "x")
    saved(opened, qtbot)
    settle(session)
    assert session.results[plain.id].df["a"][0] == "x"
    opened.close_now()


@pytest.mark.parametrize(
    ("settings", "reads"),
    [
        ({"pattern": "one.csv"}, True),
        ({"pattern": "*.CSV;*.txt"}, True),
        ({"pattern": "other*.csv"}, False),
        ({"pattern": "*.csv", "folder": "{Sub}"}, False),
        ({"pattern": "*.csv", "folder": "{Sub}", "subfolders": True}, False),
    ],
)
def test_a_folder_import_reads_the_file_it_lists(session, add, registry, tmp_path, settings, reads):
    (tmp_path / "sub").mkdir()
    path = add("one.csv")
    session.delete_table(session.table.id)
    session.set_parameters([Parameter("Sub", str(tmp_path / "sub"))])
    table = session.add_table(registry.actions["csv.import"], name="folder")
    session.set_params(table.steps[0].id, {"source": "folder", "folder": str(tmp_path), **settings})
    assert session.reads_file(path) is reads


def test_a_folder_import_with_subfolders_reads_a_file_below_it(session, add, registry, tmp_path):
    (tmp_path / "sub").mkdir()
    path = add("sub/one.csv")
    session.delete_table(session.table.id)
    table = session.add_table(registry.actions["csv.import"], name="folder")
    session.set_params(table.steps[0].id, {"source": "folder", "folder": str(tmp_path), "pattern": "*.csv"})
    assert not session.reads_file(path)
    session.set_params(table.steps[0].id, {"subfolders": True})
    assert session.reads_file(path)


def test_the_list_of_the_files_in_a_folder_counts_as_reading_the_file(session, add, registry, tmp_path):
    path = add("one.csv")
    session.delete_table(session.table.id)
    table = session.add_table(registry.actions["files.list"], name="files")
    session.set_params(table.steps[0].id, {"folder": str(tmp_path), "pattern": "*.csv"})
    assert session.reads_file(path)


def test_save_reaches_a_folder_import_and_the_tables_that_use_it(session, settle, add, edit, registry, qtbot, tmp_path):
    add("one.csv", "a\tb\n1\t2\n3\t4\n")
    marked = session.table
    add("two.csv", "a\tb\n5\t6\n")
    user = session.table
    folder = session.add_table(registry.actions["csv.import"], name="folder")
    session.set_params(
        folder.steps[0].id, {"source": "folder", "folder": str(tmp_path), "pattern": "one.csv", "delimiter": ";"}
    )
    session.add_link(folder.id, user.id, "append")
    settle(session)
    opened = edit(marked.steps[0])
    opened.model.setData(opened.model.index(0, 0), "x")
    saved(opened, qtbot)
    settle(session)
    assert session.results[folder.id].df["a"][0] == "x" and "x" in session.results[user.id].df["a"].to_list()
    opened.close_now()


def test_a_new_stamp_doesn_t_reset_what_is_typed_in_the_settings_of_the_step(window, session, settle, add, monkeypatch):
    path = add("orders.csv")
    session.select_step(session.table.steps[0].id)
    settle(session)
    panel = window.settings_panel
    monkeypatch.setattr(panel._form, "set_values", lambda values: pytest.fail("the form was set"))
    path.write_text(path.read_text(encoding="utf-8-sig") + "1;2;3\r\n", encoding="utf-8-sig")
    session.sheet_saved(path)
    settle(session)
    assert session.table.steps[0].params[files.SHEET_KEY]["stamp"] == sheet_module.stamp_of(path)


def test_a_wrong_path_is_shown_as_a_problem(window, session, add, monkeypatch):
    add("orders.csv")
    step = session.table.steps[0]
    step.params = {**step.params, "file": "C:orders.csv"}
    shown = []
    monkeypatch.setattr(window, "show_sheet_problem", shown.append)
    session.want_sheet_edit(step.id)
    assert shown and "needs a" in shown[0]


# --- closing an edit window --------------------------------------------------------------------


def test_closing_an_edit_window_without_changes_asks_nothing(window, session, add, edit, monkeypatch):
    add("orders.csv")
    monkeypatch.setattr(SheetWindow, "ask_close_edit", lambda self: pytest.fail("asked"))
    opened = edit(session.table.steps[0])
    assert opened.close() and not window._sheets


def test_closing_with_changes_asks_and_saves_first(window, session, add, edit, qtbot, monkeypatch):
    path = add("orders.csv")
    monkeypatch.setattr(SheetWindow, "ask_close_edit", lambda self: "save")
    opened = edit(session.table.steps[0])
    opened.model.setData(opened.model.index(0, 0), "x")
    assert not opened.close()
    assert opened.busy
    qtbot.waitUntil(lambda: not window._sheets)
    assert "x" in path.read_text(encoding="utf-8-sig")


def test_closing_with_don_t_save_and_cancel(window, session, add, edit, monkeypatch):
    path = add("orders.csv")
    before = path.read_bytes()
    opened = edit(session.table.steps[0])
    opened.model.setData(opened.model.index(0, 0), "x")
    monkeypatch.setattr(SheetWindow, "ask_close_edit", lambda self: "cancel")
    assert not opened.close() and window._sheets == [opened]
    monkeypatch.setattr(SheetWindow, "ask_close_edit", lambda self: "discard")
    assert opened.close() and not window._sheets and path.read_bytes() == before


def test_a_failed_save_on_closing_closes_nothing(window, session, add, edit, qtbot, problems, monkeypatch):
    path = add("orders.csv")
    monkeypatch.setattr(SheetWindow, "ask_close_edit", lambda self: "save")
    opened = edit(session.table.steps[0])
    opened.model.setData(opened.model.index(0, 0), "x")
    with held(path):
        assert not opened.close()
        qtbot.waitUntil(lambda: not opened.busy)
    assert problems and window._sheets == [opened] and not opened._close_after_write
    opened.close_now()


def test_a_save_that_is_refused_on_closing_keeps_the_window(window, session, add, edit, monkeypatch):
    path = add("orders.csv")
    monkeypatch.setattr(SheetWindow, "ask_close_edit", lambda self: "save")
    opened = edit(session.table.steps[0])
    opened.model.setData(opened.model.index(0, 0), "x")
    path.unlink()
    monkeypatch.setattr(opened, "ask_save_gone", lambda file: False)
    assert not opened.close()
    assert window._sheets == [opened] and not opened._close_after_write
    opened.close_now()


def test_closing_the_app_saves_an_edit_window_then_asks_about_the_project(
    window, session, add, edit, qtbot, monkeypatch
):
    path = add("orders.csv")
    order = []
    monkeypatch.setattr(SheetWindow, "ask_close_edit", lambda self: order.append("sheet") or "save")
    monkeypatch.setattr(window, "ask_save", lambda reason: order.append("project") or "discard")
    opened = edit(session.table.steps[0])
    opened.model.setData(opened.model.index(0, 0), "x")
    window.show()
    assert not window.close()
    qtbot.waitUntil(lambda: "project" in order, timeout=10_000)
    assert order == ["sheet", "project"] and not window.isVisible() and not window._sheets
    assert "x" in path.read_text(encoding="utf-8-sig")


def test_closing_the_app_asks_each_sheet_once(window, session, add, draft, edit, qtbot, monkeypatch):
    add("orders.csv")
    asked = []
    monkeypatch.setattr(SheetWindow, "ask_close_draft", lambda self: asked.append("draft") or "discard")
    monkeypatch.setattr(SheetWindow, "ask_close_edit", lambda self: asked.append("edit") or "save")
    monkeypatch.setattr(window, "ask_save", lambda reason: asked.append("project") or "discard")
    draft("a\tb\n1\t2\n")
    opened = edit(session.table.steps[0])
    opened.model.setData(opened.model.index(0, 0), "x")
    window.show()
    assert not window.close()
    qtbot.waitUntil(lambda: "project" in asked, timeout=10_000)
    assert asked == ["draft", "edit", "project"] and not window._sheets


def test_a_failed_save_on_closing_asks_again_at_the_next_closing(window, session, add, draft, edit, qtbot, monkeypatch):
    path = add("orders.csv")
    asked = []
    monkeypatch.setattr(SheetWindow, "ask_close_draft", lambda self: asked.append("draft") or "cancel")
    monkeypatch.setattr(SheetWindow, "ask_close_edit", lambda self: asked.append("edit") or "save")
    monkeypatch.setattr(SheetWindow, "show_problem", lambda self, text: None)
    opened = edit(session.table.steps[0])
    opened.model.setData(opened.model.index(0, 0), "x")
    window.show()
    with held(path):
        assert not window.close()
        qtbot.waitUntil(lambda: not opened.busy, timeout=10_000)
    assert window._sheets == [opened]
    draft("a\tb\n1\t2\n")
    assert not window.close()
    qtbot.waitUntil(lambda: "draft" in asked, timeout=10_000)
    assert asked == ["edit", "edit", "draft"]


@pytest.fixture
def slow_write(monkeypatch):
    """Once a test clears this event, the writes of the sheets wait until it sets it again."""
    gate = threading.Event()
    gate.set()
    gate.only = None  # the name of the one file that waits
    real = sheet_module.write_file

    def write(frame, path, *args, **settings):
        if gate.only in (None, path.name):
            assert gate.wait(30)
        return real(frame, path, *args, **settings)

    monkeypatch.setattr(sheet_window, "write_file", write)
    yield gate
    gate.set()


def two_sheets(session, add, edit) -> tuple[SheetWindow, SheetWindow]:
    add("c.csv", "a\tb\n1\t2\n")
    add("e.csv", "a\tb\n3\t4\n")
    first, second = (edit(table.steps[0]) for table in session.project.tables)
    return first, second


def test_a_sheet_changed_during_the_write_that_closing_waits_for_is_asked(
    window, session, add, edit, qtbot, slow_write, monkeypatch
):
    asked = []
    monkeypatch.setattr(window, "ask_save", lambda reason: asked.append("project") or "discard")
    first, second = two_sheets(session, add, edit)
    monkeypatch.setattr(SheetWindow, "ask_close_edit", lambda self: asked.append(self.file.name) or "save")
    second.model.setData(second.model.index(0, 0), "new")
    window.show()
    slow_write.clear()
    assert not window.close()
    assert asked == ["e.csv"] and second.busy
    first.model.setData(first.model.index(0, 0), "typed later")
    slow_write.set()
    qtbot.waitUntil(lambda: "project" in asked, timeout=10_000)
    assert asked == ["e.csv", "c.csv", "project"]
    assert "typed later" in (session.project.directory or first.file.parent).joinpath("c.csv").read_text(encoding="utf-8-sig")


def test_a_draft_that_got_data_during_the_write_is_asked(
    window, session, add, draft, edit, qtbot, slow_write, monkeypatch
):
    asked = []
    add("e.csv", "a\tb\n3\t4\n")
    empty = draft("")
    writer = edit(session.table.steps[0])
    monkeypatch.setattr(SheetWindow, "ask_close_edit", lambda self: asked.append("edit") or "save")
    monkeypatch.setattr(SheetWindow, "ask_close_draft", lambda self: asked.append("draft") or "cancel")
    writer.model.setData(writer.model.index(0, 0), "new")
    window.show()
    slow_write.clear()
    assert not window.close()
    assert asked == ["edit"]
    empty.model.setData(empty.model.index(0, 0), "pasted")
    slow_write.set()
    qtbot.waitUntil(lambda: "draft" in asked, timeout=10_000)
    assert window._sheets == [empty] and window.isVisible()


def test_cancel_at_the_run_question_ends_the_closing(window, session, add, edit, qtbot, slow_write, monkeypatch):
    """Later, a new closing asks every sheet again: what they hold now was never asked about."""
    asked = []
    first, second = two_sheets(session, add, edit)
    answers = {"c.csv": "discard", "e.csv": "save"}
    monkeypatch.setattr(SheetWindow, "ask_close_edit", lambda self: asked.append(self.file.name) or answers[self.file.name])
    monkeypatch.setattr(window, "ask_save", lambda reason: asked.append("project") or "cancel")
    monkeypatch.setattr(window, "ask_close_while_running", lambda: asked.append("run") or "cancel")
    first.model.setData(first.model.index(0, 0), "old")
    second.model.setData(second.model.index(0, 0), "new")
    window.show()
    slow_write.clear()
    assert not window.close()
    assert asked == ["c.csv", "e.csv"]
    monkeypatch.setattr(type(session), "running", property(lambda self: True))
    slow_write.set()
    qtbot.waitUntil(lambda: "run" in asked, timeout=10_000)
    monkeypatch.setattr(type(session), "running", property(lambda self: False))
    first.model.setData(first.model.index(0, 1), "work of later")
    answers["c.csv"] = "cancel"
    asked.clear()
    assert not window.close()
    assert asked == ["c.csv"] and window._sheets == [first]


def test_a_second_click_on_x_during_the_write_is_the_same_closing(
    window, session, add, edit, qtbot, slow_write, monkeypatch
):
    asked = []
    first, second = two_sheets(session, add, edit)
    answers = {"c.csv": "discard", "e.csv": "save"}
    monkeypatch.setattr(SheetWindow, "ask_close_edit", lambda self: asked.append(self.file.name) or answers[self.file.name])
    monkeypatch.setattr(window, "ask_save", lambda reason: asked.append("project") or "discard")
    first.model.setData(first.model.index(0, 0), "old")
    second.model.setData(second.model.index(0, 0), "new")
    window.show()
    slow_write.clear()
    assert not window.close()
    assert not window.close()
    assert asked == ["c.csv", "e.csv"] and second.busy
    slow_write.set()
    qtbot.waitUntil(lambda: "project" in asked, timeout=10_000)
    assert asked == ["c.csv", "e.csv", "project"]


def test_a_cancel_during_the_write_reaches_the_sheet_that_writes(
    window, session, add, edit, qapp, qtbot, slow_write, monkeypatch
):
    asked = []
    other, writer = two_sheets(session, add, edit)
    answers = {"e.csv": "save", "c.csv": "cancel"}
    monkeypatch.setattr(SheetWindow, "ask_close_edit", lambda self: asked.append(self.file.name) or answers[self.file.name])
    monkeypatch.setattr(window, "ask_save", lambda reason: asked.append("project") or "discard")
    writer.model.setData(writer.model.index(0, 0), "new")
    window.show()
    slow_write.clear()
    assert not window.close()
    assert asked == ["e.csv"] and writer.closes_app
    other.model.setData(other.model.index(0, 0), "x")
    assert not window.close()  # the same closing, with a new question about the changed sheet: Cancel
    assert asked == ["e.csv", "c.csv"] and not writer.closes_app
    slow_write.set()
    qtbot.waitUntil(lambda: not writer.busy, timeout=10_000)
    qapp.processEvents()
    assert window.isVisible() and other in window._sheets and "project" not in asked


def test_a_sheet_that_closes_itself_during_a_question_doesn_t_end_the_closing(
    window, session, add, draft, edit, qtbot, slow_write, tmp_path, monkeypatch
):
    """The project question still comes, and no error."""
    asked = []
    add("b.csv", "a\tb\n1\t2\n")
    other = edit(session.table.steps[0])
    writer = draft("a\tb\n3\t4\n")
    monkeypatch.setattr(writer, "ask_add", lambda: (tmp_path / "w.csv", ".csv"))

    def question(self) -> str:
        asked.append(self.file.name)
        slow_write.set()
        qtbot.waitUntil(lambda: not shiboken6.isValid(writer) or writer not in window._sheets, timeout=10_000)
        qtbot.wait(20)
        return "discard"

    monkeypatch.setattr(SheetWindow, "ask_close_edit", question)
    monkeypatch.setattr(window, "ask_save", lambda reason: asked.append("project") or "discard")
    other.model.setData(other.model.index(0, 0), "x")
    window.show()
    slow_write.clear()
    assert writer.add_as_table()
    window.close()
    qtbot.waitUntil(lambda: "project" in asked, timeout=10_000)
    assert asked == ["b.csv", "project"] and not window.isVisible()


def test_a_write_by_hand_after_the_answer_is_waited_for(window, session, add, edit, qtbot, slow_write, monkeypatch):
    """Ctrl+S in a sheet that was answered with Don't save: the file is written, then closing goes on."""
    asked = []
    first, second = two_sheets(session, add, edit)
    answers = {"c.csv": "discard", "e.csv": "save"}
    monkeypatch.setattr(SheetWindow, "ask_close_edit", lambda self: asked.append(self.file.name) or answers[self.file.name])
    monkeypatch.setattr(window, "ask_save", lambda reason: asked.append("project") or "discard")
    first.model.setData(first.model.index(0, 0), "kept after all")
    second.model.setData(second.model.index(0, 0), "new")
    path = first.file
    window.show()
    slow_write.clear()
    assert not window.close()
    assert asked == ["c.csv", "e.csv"] and first.save()
    slow_write.set()
    qtbot.waitUntil(lambda: "project" in asked, timeout=10_000)
    assert "kept after all" in path.read_text(encoding="utf-8-sig") and not window.isVisible()


def test_a_write_by_hand_that_fails_during_the_closing_keeps_the_app(
    window, session, add, edit, qtbot, slow_write, problems, monkeypatch
):
    asked = []
    first, second = two_sheets(session, add, edit)
    answers = {"c.csv": "discard", "e.csv": "save"}
    monkeypatch.setattr(SheetWindow, "ask_close_edit", lambda self: asked.append(self.file.name) or answers[self.file.name])
    monkeypatch.setattr(window, "ask_save", lambda reason: asked.append("project") or "discard")
    first.model.setData(first.model.index(0, 0), "x")
    second.model.setData(second.model.index(0, 0), "new")
    window.show()
    slow_write.only = "e.csv"
    slow_write.clear()
    assert not window.close()
    answers["c.csv"] = "cancel"
    with held(first.file):
        assert first.save()
        qtbot.waitUntil(lambda: bool(problems), timeout=10_000)
    slow_write.set()
    qtbot.waitUntil(lambda: asked == ["c.csv", "e.csv", "c.csv"], timeout=10_000)
    qtbot.wait(150)
    assert "project" not in asked and window.isVisible() and first in window._sheets


@pytest.mark.parametrize("route", ["question", "refused"])
def test_a_try_by_hand_after_don_t_save_is_asked_about_again(window, session, add, edit, qtbot, slow_write, monkeypatch, route):
    """A Cancel at a question by hand, or a save that the run refuses: the answer before is void."""
    asked = []
    first, second = two_sheets(session, add, edit)
    answers = {"c.csv": "discard", "e.csv": "save"}
    monkeypatch.setattr(SheetWindow, "ask_close_edit", lambda self: asked.append(self.file.name) or answers[self.file.name])
    monkeypatch.setattr(window, "ask_save", lambda reason: asked.append("project") or "discard")
    first.model.setData(first.model.index(0, 0), "x")
    second.model.setData(second.model.index(0, 0), "new")
    window.show()
    slow_write.clear()
    assert not window.close()
    answers["c.csv"] = "cancel"
    if route == "question":
        assert not first.close()
    else:
        monkeypatch.setattr(type(session), "running", property(lambda self: True))
        assert not first.save()
        monkeypatch.setattr(type(session), "running", property(lambda self: False))
    asked.clear()
    slow_write.set()
    qtbot.waitUntil(lambda: "c.csv" in asked, timeout=10_000)
    assert window.isVisible() and first in window._sheets and "project" not in asked


def test_closing_waits_for_a_sheet_that_reads(window, session, add, edit, qtbot, monkeypatch):
    """A read that started during the wait: the closing goes on when it has ended."""
    asked = []
    add("c.csv", "a\tb\n1\t2\n")
    edit(session.table.steps[0])
    reading = {"on": True}
    monkeypatch.setattr(SheetWindow, "busy", property(lambda self: reading["on"]))
    monkeypatch.setattr(window, "ask_save", lambda reason: asked.append("project") or "discard")
    window.show()
    window.go_on_closing()
    qtbot.wait(150)
    assert window.isVisible() and not asked
    reading["on"] = False
    qtbot.waitUntil(lambda: "project" in asked, timeout=10_000)


def test_a_sheet_saved_by_hand_during_the_wait_goes_on_with_the_closing(
    window, session, add, edit, qtbot, slow_write, monkeypatch
):
    asked = []
    first, second = two_sheets(session, add, edit)
    monkeypatch.setattr(SheetWindow, "ask_close_edit", lambda self: asked.append(self.file.name) or "save")
    monkeypatch.setattr(window, "ask_save", lambda reason: asked.append("project") or "discard")
    first.model.setData(first.model.index(0, 0), "x")
    window.show()
    slow_write.clear()
    assert not window.close()
    second.model.setData(second.model.index(0, 0), "y")
    assert not second.close()
    slow_write.set()
    qtbot.waitUntil(lambda: "project" in asked, timeout=10_000)
    assert asked == ["c.csv", "e.csv", "project"] and not window.isVisible()


def test_closing_goes_on_only_when_no_question_is_open(window, session, add, edit, qtbot, monkeypatch):
    """The write ends while a question of the sheet is open: its answer can be a Cancel."""
    asked = []
    two_sheets(session, add, edit)
    open_box = {"box": window}
    monkeypatch.setattr(main_window_module.QApplication, "activeModalWidget", lambda: open_box["box"])
    monkeypatch.setattr(window, "ask_save", lambda reason: asked.append("project") or "discard")
    window.show()
    window.go_on_closing()
    qtbot.wait(150)
    assert window.isVisible() and not asked
    open_box["box"] = None
    qtbot.waitUntil(lambda: "project" in asked, timeout=10_000)
    assert not window.isVisible()


def test_the_answers_of_a_closing_that_ended_are_gone(window, session, add, edit, qtbot, slow_write, monkeypatch):
    """Stop ends a closing. A later closing that waits for a run starts with no answers."""
    asked = []
    first, second = two_sheets(session, add, edit)
    answers = {"c.csv": "discard", "e.csv": "save"}
    monkeypatch.setattr(SheetWindow, "ask_close_edit", lambda self: asked.append(self.file.name) or answers[self.file.name])
    first.model.setData(first.model.index(0, 0), "old")
    second.model.setData(second.model.index(0, 0), "new")
    window.show()
    slow_write.clear()
    assert not window.close()
    assert asked == ["c.csv", "e.csv"]
    second.stop()
    slow_write.set()
    qtbot.waitUntil(lambda: not second.busy, timeout=10_000)
    assert not second.closes_app
    asked.clear()
    answers["c.csv"] = "cancel"
    monkeypatch.setattr(type(session), "running", property(lambda self: True))
    monkeypatch.setattr(window, "ask_close_while_running", lambda: "wait")
    assert not window.close()
    monkeypatch.setattr(type(session), "running", property(lambda self: False))
    window._run_busy(False)
    qtbot.waitUntil(lambda: bool(asked), timeout=10_000)
    assert asked == ["c.csv"] and window.isVisible()


def test_a_window_with_its_question_open_is_not_asked_twice(window, session, add, edit, monkeypatch):
    add("orders.csv")
    opened = edit(session.table.steps[0])
    opened.model.setData(opened.model.index(0, 0), "x")
    asked = []

    def question(self) -> str:
        asked.append("edit")
        assert not self.may_close(app=True)  # a second closing, while the first question is open
        return "cancel"

    monkeypatch.setattr(SheetWindow, "ask_close_edit", question)
    assert not opened.close()
    assert asked == ["edit"]


# --- memory: the project stays small -----------------------------------------------------------


def test_no_data_of_a_sheet_goes_into_the_undo_of_the_project(session, add):
    add("orders.csv")
    assert "Müller" not in json.dumps(session._undo, default=str)
    assert "Müller" not in json.dumps(session.project.to_dict(), default=str)
