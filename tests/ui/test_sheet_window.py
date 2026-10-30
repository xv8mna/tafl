"""The data sheet window: pasting, typing, keys, editing and closing. Every question and the
clipboard text are replaced in the tests."""

import time

import polars as pl
import pytest
from PySide6.QtCore import QItemSelection, QItemSelectionModel, QPoint, Qt
from PySide6.QtGui import QAction
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QAbstractItemView, QApplication, QToolBar

from tafl.core import Parameter
from tafl.core import sheet as sheet_module
from tafl.core.sheet import Pasted, Sheet
from tafl.ui import data_panel, sheet_window
from tafl.ui.sheet_window import AddSheetDialog, SheetModel, SheetWindow

CTRL = Qt.KeyboardModifier.ControlModifier
SHIFT = Qt.KeyboardModifier.ShiftModifier
ALT = Qt.KeyboardModifier.AltModifier


@pytest.fixture
def clipboard(monkeypatch):
    """The text that the sheets get from the clipboard."""
    box = {"text": ""}
    monkeypatch.setattr(SheetWindow, "clipboard_text", lambda self: box["text"])
    return box


@pytest.fixture
def sheets(window, clipboard, qtbot):
    """sheets(text): a shown, active sheet, with ``text`` pasted. No questions on the way out."""

    def open_sheet(text: str = "", **settings) -> SheetWindow:
        clipboard["text"] = text
        window.new_sheet(bool(text), **settings)
        opened = window._sheets[-1]
        opened.activateWindow()
        opened.grid.setFocus()
        qtbot.waitUntil(lambda: QApplication.activeWindow() is opened)
        return opened

    yield open_sheet
    for opened in list(window._sheets):
        opened.close_now()


def contents(opened: SheetWindow) -> list[list[str]]:
    sheet = opened.sheet
    return [[sheet.text(r, c) for c in range(sheet.column_count)] for r in range(sheet.row_count)]


def select(opened: SheetWindow, *ranges: tuple[int, int, int, int]) -> None:
    selection = QItemSelection()
    for top, bottom, left, right in ranges:
        selection.select(opened.model.index(top, left), opened.model.index(bottom, right))
    opened.grid.selectionModel().select(selection, QItemSelectionModel.SelectionFlag.ClearAndSelect)


def go(opened: SheetWindow, row: int, column: int) -> None:
    opened.grid.selectionModel().setCurrentIndex(
        opened.model.index(row, column), QItemSelectionModel.SelectionFlag.ClearAndSelect
    )


def editor(opened: SheetWindow):
    return opened.grid.indexWidget(opened.grid.currentIndex())


def type_into(opened: SheetWindow, text: str) -> None:
    """Keys typed on the grid: the first opens the editor, the others go into it."""
    QTest.keyClicks(opened.grid, text[0])
    if text[1:]:
        QTest.keyClicks(editor(opened), text[1:])


# --- pasting -----------------------------------------------------------------------------------


def test_paste_from_clipboard_opens_a_sheet_with_the_data_and_the_first_row_as_names(sheets):
    opened = sheets("Order\tAmount\tDate\r\n00123\t1,50\t27.02.2026\r\n00124\t1E5\t28.02.2026\r\n")
    assert opened.sheet.names == ["Order", "Amount", "Date"] and opened.sheet.header
    assert contents(opened) == [
        ["00123", "1,50", "27.02.2026"],
        ["00124", "1E5", "28.02.2026"],
    ]  # stays text, as pasted
    assert opened.switch.isChecked() and opened.status_text == "Pasted 2 rows × 3 columns"
    assert opened.windowTitle().startswith("Sheet 1 •") and opened.grid.cell() == (0, 0)
    assert "2 rows × 3 columns" in opened.status_line.text()
    assert not opened.one_column_button.isVisibleTo(opened)  # a tab can't be a wrong guess


def test_a_single_column_keeps_its_first_value(sheets):
    opened = sheets("00123\n00124\n")
    assert not opened.sheet.header and contents(opened) == [["00123"], ["00124"]]
    assert opened.sheet.names == ["Column 1"]


def test_paste_into_a_sheet_with_data_grows_it_and_selects_the_block(sheets, clipboard):
    opened = sheets("a\tb\n1\t2\n")
    go(opened, 0, 1)
    clipboard["text"] = "x\ty\nz\tw"
    opened.paste_action.trigger()
    assert contents(opened) == [["1", "x", "y"], ["", "z", "w"]]
    assert opened.sheet.column_count == 3 and opened.sheet.row_count == 2
    assert opened.ranges() == [(0, 1, 1, 2)] and opened.grid.cell() == (0, 1)
    assert opened.status_text == "Pasted 2 rows × 2 columns"


def test_one_value_goes_into_all_selected_cells(sheets, clipboard):
    opened = sheets("a\tb\n1\t2\n3\t4\n")
    select(opened, (0, 1, 0, 1))
    clipboard["text"] = "x\r\n"
    opened.paste_action.trigger()
    assert contents(opened) == [["x", "x"], ["x", "x"]] and opened.status_text == "Pasted the value into 4 cells"
    opened.undo_action.trigger()
    assert contents(opened) == [["1", "2"], ["3", "4"]]


def test_paste_below_goes_under_the_last_row_with_a_value(sheets, clipboard):
    opened = sheets()
    opened.sheet.set_text(2, 1, "x")
    opened.model.sync()
    go(opened, 0, 2)
    clipboard["text"] = "1\t2"
    opened.paste_below_action.trigger()
    assert contents(opened)[3][:2] == ["1", "2"] and opened.sheet.row_count == 20


def test_paste_below_with_one_value_adds_a_row_also_with_several_cells_selected(sheets, clipboard):
    opened = sheets("A\tB\n1\t2\n3\t4\n")
    select(opened, (0, 1, 0, 1))
    clipboard["text"] = "B1\r\n"
    opened.paste_below_action.trigger()
    assert contents(opened) == [["1", "2"], ["3", "4"], ["B1", ""]]
    assert opened.status_text == "Pasted 1 row × 1 column"
    opened.undo_action.trigger()
    assert contents(opened) == [["1", "2"], ["3", "4"]]


def test_one_line_as_one_column_writes_one_cell_in_a_sheet_with_data(sheets, clipboard):
    opened = sheets("A\tB\tC\n1\t2\t3\n")
    go(opened, 0, 0)
    clipboard["text"] = "Müller, Hans"
    opened.paste_action.trigger()
    assert contents(opened) == [["Müller", " Hans", "3"]] and opened.status_text.endswith("split at commas")
    opened.one_column_button.click()
    assert contents(opened) == [["Müller, Hans", "2", "3"]]
    assert opened.status_text == "Pasted 1 row × 1 column"


def test_paste_as_one_column_in_a_big_selection_writes_the_lines_only(sheets, clipboard):
    opened = sheets("A\tB\tC\n1\t2\t3\n4\t5\t6\n7\t8\t9\n")
    go(opened, 0, 1)
    clipboard["text"] = "x,y\nz,w\n"
    opened.paste_action.trigger()
    opened.one_column_button.click()
    assert contents(opened) == [["1", "x,y", "3"], ["4", "z,w", "6"], ["7", "8", "9"]]


def test_ctrl_v_at_a_cell_of_an_empty_sheet_goes_to_that_cell(sheets, clipboard):
    opened = sheets()
    go(opened, 4, 2)
    clipboard["text"] = "a\tb\nc\td\n"
    opened.paste_action.trigger()
    assert opened.sheet.column_count == 4 and opened.sheet.row_count == 20 and not opened.sheet.header
    assert contents(opened)[4] == ["", "", "a", "b"] and contents(opened)[5] == ["", "", "c", "d"]
    assert opened.grid.cell() == (4, 2)


def test_paste_as_one_column_at_a_cell_of_an_empty_sheet(sheets, clipboard):
    opened = sheets()
    go(opened, 1, 1)
    clipboard["text"] = "x;y\nz;w\n"
    opened.paste_action.trigger()
    opened.one_column_button.click()
    assert contents(opened)[1:3] == [["", "x;y", ""], ["", "z;w", ""]]
    assert opened.sheet.column_count == 3


def test_a_stray_tab_without_another_separator_has_the_way_back(sheets):
    opened = sheets("abc\nd\te\nfgh\nijk\n")
    assert opened.status_text == "Pasted 4 rows × 2 columns, split at tabs"
    assert opened.one_column_button.isVisibleTo(opened)
    opened.one_column_button.click()
    assert contents(opened) == [["abc"], ["d\te"], ["fgh"], ["ijk"]]


def test_tabs_of_a_spreadsheet_program_have_no_button(sheets):
    opened = sheets("a\tb\nc\td\n")
    assert opened.status_text == "Pasted 1 row × 2 columns" and not opened.one_column_button.isVisibleTo(opened)


def test_a_list_in_a_frame_says_so_and_has_the_way_back(sheets):
    text = "-----------\n|Name|Wert|\n-----------\n|a   |1   |\n|b   |2   |\n-----------\n"
    opened = sheets(text)
    assert opened.sheet.header and opened.sheet.names == ["Name", "Wert"]
    assert contents(opened) == [["a   ", "1   "], ["b   ", "2   "]]
    assert opened.status_text == "Pasted 2 rows × 2 columns, split at pipes, without the frame"
    opened.one_column_button.click()
    assert len(contents(opened)) == 6 and contents(opened)[0] == ["-----------"]


def test_a_list_without_column_names_keeps_its_first_record(sheets):
    opened = sheets("00123\tMüller\t1,50\n00124\tSchmidt\t2,75\n")
    assert not opened.sheet.header and opened.sheet.row_count == 2
    assert opened.status_text == "Pasted 2 rows × 3 columns"


def test_paste_as_one_column_replaces_a_wrong_split(sheets):
    opened = sheets("Müller; Hans\nMeier; Eva\n")
    assert contents(opened) == [["Meier", " Eva"]] and opened.status_text.endswith("split at semicolons")
    assert opened.one_column_button.isVisibleTo(opened)
    opened.one_column_button.click()
    assert contents(opened) == [["Müller; Hans"], ["Meier; Eva"]] and not opened.sheet.header
    assert opened.status_text == "Pasted 2 rows × 1 column" and not opened.one_column_button.isVisibleTo(opened)
    opened.undo_action.trigger()
    assert not opened.sheet.has_values  # the wrong split is not an undo step of its own


def test_paste_as_one_column_in_the_middle_of_a_sheet(sheets, clipboard):
    opened = sheets("a\tb\n1\t2\n")
    go(opened, 0, 1)
    clipboard["text"] = "x,5\ny,6\n"
    opened.paste_action.trigger()
    assert contents(opened)[0] == ["1", "x", "5"] and opened.status_text.endswith("split at commas")
    opened.one_column_button.click()
    assert contents(opened) == [["1", "x,5"], ["", "y,6"]]


def test_paste_as_one_column_after_a_paste_that_changed_nothing_keeps_the_edit_before(sheets, clipboard):
    opened = sheets("A\tB\tC\n1\t2\t3\n")
    go(opened, 0, 0)
    clipboard["text"] = "p,q\n"
    opened.paste_action.trigger()
    assert contents(opened) == [["p", "q", "3"]]
    opened.model.setData(opened.model.index(0, 2), "typed")
    go(opened, 0, 0)
    opened.paste_action.trigger()  # the same cells: no undo step
    assert opened.one_column_button.isVisibleTo(opened)
    opened.one_column_button.click()
    assert contents(opened) == [["p,q", "q", "typed"]]


def test_after_paste_as_one_column_the_keys_go_to_the_grid(sheets):
    opened = sheets("Müller; Hans\nMeier; Eva\n")
    opened.one_column_button.setFocus()
    opened.one_column_button.click()
    assert opened.grid.hasFocus()


def test_after_the_switch_the_keys_go_to_the_grid(sheets):
    opened = sheets("a\tb\n1\t2\n")
    opened.switch.setFocus()
    opened.switch.click()
    assert opened.grid.hasFocus()


def test_a_click_on_one_column_with_an_open_editor_keeps_the_text_and_the_keys(sheets):
    """The typed text is the next edit, so the button has nothing to do any more."""
    opened = sheets("1, Hans\n2, Eva\n3, Ina\n")
    go(opened, 1, 0)
    opened.grid.setFocus()
    QTest.keyClicks(opened.grid, "z")
    QTest.mouseClick(opened.one_column_button, Qt.MouseButton.LeftButton)
    assert opened.sheet.text(1, 0) == "z" and opened.grid.hasFocus()
    assert not opened.one_column_button.isVisibleTo(opened) and opened.sheet.column_count == 2


def test_after_a_click_on_add_as_table_and_cancel_the_keys_go_to_the_grid(sheets, monkeypatch):
    opened = sheets("a\tb\n1\t2\n")
    monkeypatch.setattr(opened, "ask_add", lambda: None)
    QTest.mouseClick(opened.main_button, Qt.MouseButton.LeftButton)
    assert opened.grid.hasFocus()


def test_a_click_on_add_as_table_applies_the_open_editor(sheets, monkeypatch):
    opened = sheets("a\tb\n1\t2\n")
    seen = []
    monkeypatch.setattr(opened, "ask_add", lambda: seen.append(opened.sheet.text(0, 1)))
    go(opened, 0, 1)
    opened.grid.setFocus()
    QTest.keyClicks(opened.grid, "z")
    QTest.mouseClick(opened.main_button, Qt.MouseButton.LeftButton)
    assert seen == ["z"]


def test_a_value_on_a_selection_from_the_first_cell_of_an_empty_sheet_fills_it(sheets, clipboard):
    opened = sheets("")
    clipboard["text"] = "x"
    select(opened, (0, 2, 0, 1))
    opened.paste_action.trigger()
    assert opened.status_text == "Pasted the value into 6 cells"
    assert [row[:2] for row in contents(opened)[:3]] == [["x", "x"]] * 3 and opened.sheet.row_count == 20
    opened.undo_action.trigger()
    assert not opened.sheet.has_values


def test_a_value_at_the_first_cell_alone_still_replaces_an_empty_sheet(sheets, clipboard):
    opened = sheets("")
    clipboard["text"] = "x"
    go(opened, 0, 0)
    opened.paste_action.trigger()
    assert (opened.sheet.row_count, opened.sheet.column_count) == (1, 1)


def test_a_paste_at_the_first_cell_keeps_the_names_given_to_the_columns(sheets, clipboard):
    opened = sheets("")
    opened.sheet.rename_column(0, "Customer")
    go(opened, 0, 0)
    clipboard["text"] = "Anna\t10\nBen\t20\n"
    opened.paste_action.trigger()
    assert opened.sheet.names[0] == "Customer" and not opened.sheet.header
    assert contents(opened)[:2] == [["Anna", "10", ""], ["Ben", "20", ""]]


def test_columns_keep_their_widths_when_a_column_comes_or_goes(sheets):
    opened = sheets("a\tb\tc\td\n1\t2\t3\t4\n")
    grid, names = opened.grid, opened.sheet.names

    def widths() -> dict[str, int]:
        return {name: grid.columnWidth(i) for i, name in enumerate(opened.sheet.names)}

    start = dict(zip(names, (100, 200, 300, 150), strict=True))
    for column, width in enumerate(start.values()):
        grid.setColumnWidth(column, width)
    go(opened, 0, 0)
    opened.add_column_action.trigger()
    assert {name: width for name, width in widths().items() if name in start} == start
    opened.undo_action.trigger()
    assert widths() == start
    go(opened, 0, 1)
    opened.delete_columns_action.trigger()
    assert widths() == {"a": 100, "c": 300, "d": 150}


def test_widths_stay_at_a_new_column_after_the_switch(sheets):
    opened = sheets("a\tb\tc\n1\t2\t3\n4\t5\t6\n")
    grid = opened.grid
    for column, width in enumerate((100, 200, 300)):
        grid.setColumnWidth(column, width)
    opened.switch.click()
    go(opened, 0, 0)
    opened.add_column_action.trigger()
    assert [grid.columnWidth(i) for i in (0, 2, 3)] == [100, 200, 300]


def test_widths_stay_at_a_new_column_after_a_rename(sheets):
    opened = sheets("x\ty\tc\n1\t2\t3\n")
    grid = opened.grid
    for column, width in enumerate((111, 222, 300)):
        grid.setColumnWidth(column, width)
    for column, name in ((0, "t"), (1, "x"), (0, "y")):  # the two swap their names
        opened._edit(lambda column=column, name=name: opened.sheet.rename_column(column, name))
    go(opened, 0, 2)
    opened.add_column_action.trigger()
    assert {name: grid.columnWidth(i) for i, name in enumerate(opened.sheet.names) if name in "xyc"} == {
        "y": 111,
        "x": 222,
        "c": 300,
    }


@pytest.mark.parametrize("columns", [299, 300, 310])
def test_widths_stay_by_name_also_with_very_many_columns(sheets, columns):
    names = [f"h{i}" for i in range(columns)]
    opened = sheets("\t".join(names) + "\n" + "\t".join("1" * columns) + "\n")
    grid = opened.grid
    for column, width in ((5, 250), (6, 260), (7, 270)):
        grid.setColumnWidth(column, width)
    go(opened, 0, 2)
    opened.delete_columns_action.trigger()
    assert [grid.columnWidth(opened.sheet.names.index(name)) for name in ("h5", "h6", "h7")] == [250, 260, 270]


def test_the_button_for_one_column_goes_with_the_next_edit(sheets):
    opened = sheets("a;b\n1;2\n")
    assert opened.one_column_button.isVisibleTo(opened)
    opened.model.setData(opened.model.index(0, 0), "x")
    assert not opened.one_column_button.isVisibleTo(opened)
    opened.undo_action.trigger()
    assert not opened.one_column_button.isVisibleTo(opened)


def test_a_very_long_text_gets_no_button_for_one_column(sheets, monkeypatch):
    monkeypatch.setattr(sheet_window, "_KEEP_TEXT", 5)
    opened = sheets("a;b\n1;2\n")
    assert opened.status_text.endswith("split at semicolons") and not opened.one_column_button.isVisibleTo(opened)


def test_a_clipboard_without_text_leaves_an_empty_sheet_open(window, sheets, clipboard):
    opened = sheets()
    clipboard["text"] = ""
    opened.paste_action.trigger()
    assert opened.status_text == "The clipboard holds no text. Copy some data, then press Ctrl+V here."
    assert not opened.sheet.has_values and window._sheets == [opened]
    window.new_sheet(True)  # New table → Paste from clipboard, with nothing to paste
    other = window._sheets[-1]
    assert other.status_text == "The clipboard holds no text. Copy some data, then press Ctrl+V here."
    assert other.sheet.row_count == 20


def test_too_much_data_leaves_the_sheet_as_it_was(sheets, clipboard, monkeypatch):
    opened = sheets("a\tb\n1\t2\n")
    monkeypatch.setattr(sheet_module, "MAX_CELLS", 6)
    clipboard["text"] = "1\t2\t3\n4\t5\t6\n7\t8\t9\n"
    opened.paste_action.trigger()
    assert (
        opened.status_text
        == "The clipboard holds 3 rows × 3 columns. A sheet holds at most 6 cells: put the data in a file and import it."
    )
    assert contents(opened) == [["1", "2"]]
    go(opened, 0, 0)
    clipboard["text"] = "1\t2\t3\n4\t5\t6\n"
    opened.paste_action.trigger()
    assert opened.status_text == "A sheet holds at most 6 cells." and contents(opened) == [["1", "2"]]


def test_a_big_text_is_read_in_the_background_with_the_window_disabled(sheets, clipboard, qtbot, monkeypatch):
    monkeypatch.setattr(sheet_window, "_PASTE_AT_ONCE", 10)
    opened = sheets()
    clipboard["text"] = "a\tb\n" + "1\t2\n" * 50
    opened.paste_action.trigger()
    assert opened.busy and not opened.grid.isEnabled() and not opened.paste_action.isEnabled()
    assert opened.status_text == "Pasting…"
    qtbot.waitUntil(lambda: not opened.busy)
    assert opened.grid.isEnabled() and opened.paste_action.isEnabled()
    assert opened.sheet.row_count == 50 and opened.status_text == "Pasted 50 rows × 2 columns"


def test_a_big_sheet_stays_quick(sheets, clipboard, qtbot):
    """300,000 rows: no step of the window may read all cells."""
    text = "a\tb\tc\n" + "1\t2,50\tsome text\n" * 300_000
    opened = sheets()
    clipboard["text"] = text
    opened.paste_action.trigger()
    qtbot.waitUntil(lambda: not opened.busy, timeout=20_000)
    assert opened.sheet.row_count == 300_000
    started = time.perf_counter()
    bar = opened.grid.verticalScrollBar()
    bar.setValue(bar.maximum() // 2)
    QApplication.processEvents()
    bar.setValue(bar.maximum())
    QApplication.processEvents()
    go(opened, 299_999, 2)
    type_into(opened, "x")
    QTest.keyClick(editor(opened), Qt.Key.Key_Return)
    assert opened.sheet.row_count == 300_001
    QTest.keyClick(opened.grid, Qt.Key.Key_A, CTRL)
    QApplication.processEvents()
    QTest.keyClick(opened.grid, Qt.Key.Key_Delete)
    QApplication.processEvents()
    assert opened.sheet.used_rows == 0
    opened.undo_action.trigger()
    QApplication.processEvents()
    opened.delete_rows_action.trigger()
    opened.undo_action.trigger()
    opened.switch.click()
    opened.undo_action.trigger()
    QApplication.processEvents()
    assert time.perf_counter() - started < 2
    assert opened.sheet.text(299_999, 2) == "x"


def test_a_failed_paste_in_the_background_says_why(sheets, clipboard, qtbot, monkeypatch):
    monkeypatch.setattr(sheet_window, "_PASTE_AT_ONCE", 1)
    monkeypatch.setattr(sheet_module, "MAX_CELLS", 2)
    opened = sheets()
    clipboard["text"] = "1\t2\t3\n"
    opened.paste_action.trigger()
    qtbot.waitUntil(lambda: not opened.busy)
    assert opened.status_text.startswith("The clipboard holds 1 row") and not opened.sheet.has_values


# --- typing ------------------------------------------------------------------------------------


def test_typing_replaces_the_cell_and_enter_goes_to_the_next_row(sheets):
    opened = sheets("old\nnext\n")
    type_into(opened, "new")
    assert editor(opened) is not None
    QTest.keyClick(editor(opened), Qt.Key.Key_Return)
    assert opened.sheet.text(0, 0) == "new" and opened.grid.cell() == (1, 0) and editor(opened) is None
    assert opened.windowTitle().startswith("Sheet 1 •")


def test_f2_and_double_click_keep_the_text_with_the_cursor_at_its_end(sheets):
    opened = sheets("old\n")
    QTest.keyClick(opened.grid, Qt.Key.Key_F2)
    assert editor(opened).text() == "old"
    QTest.keyClicks(editor(opened), "er")
    QTest.keyClick(editor(opened), Qt.Key.Key_Return)
    assert opened.sheet.text(0, 0) == "older"
    go(opened, 0, 0)
    cell = opened.grid.visualRect(opened.model.index(0, 0)).center()
    QTest.mouseClick(opened.grid.viewport(), Qt.MouseButton.LeftButton, pos=cell)
    QTest.mouseDClick(opened.grid.viewport(), Qt.MouseButton.LeftButton, pos=cell)
    assert editor(opened).text() == "older"


def test_esc_leaves_the_editor_without_applying(sheets):
    opened = sheets("old\n")
    type_into(opened, "new")
    QTest.keyClick(editor(opened), Qt.Key.Key_Escape)
    assert opened.sheet.text(0, 0) == "old" and editor(opened) is None and opened.grid.cell() == (0, 0)
    assert opened.sheet.can_undo and contents(opened) == [["old"]]


@pytest.mark.parametrize(
    "cell",
    [
        "two\nlines",
        "a b",  # a no-break space: toPlainText would change it
        "  spaces  ",
        "tab\there",
        "'quoted'\"",
        "emoji 🙂 ü",
        "line separator",
        "\n\nthree\n",
    ],
)
def test_a_cell_stays_as_it_was_when_opened_and_applied(sheets, cell):
    opened = sheets()
    opened.model.setData(opened.model.index(0, 0), cell)
    undo_steps = len(opened.sheet._undo)
    QTest.keyClick(opened.grid, Qt.Key.Key_F2)
    assert editor(opened).text() == cell
    QTest.keyClick(editor(opened), Qt.Key.Key_Return)
    assert opened.sheet.text(0, 0) == cell and len(opened.sheet._undo) == undo_steps  # no edit at all


@pytest.mark.parametrize("cell", ["two\nlines", "a b", "  spaces", "tab\there", "emoji 🙂 ü"])
def test_a_small_edit_changes_only_what_was_typed(sheets, cell):
    opened = sheets()
    opened.model.setData(opened.model.index(0, 0), cell)
    QTest.keyClick(opened.grid, Qt.Key.Key_F2)
    QTest.keyClicks(editor(opened), "!")
    QTest.keyClick(editor(opened), Qt.Key.Key_Return)
    assert opened.sheet.text(0, 0) == cell + "!"


def test_shift_enter_makes_a_line_break_and_the_editor_grows(sheets):
    opened = sheets()
    type_into(opened, "a")
    field = editor(opened)
    one_line = field.height()
    QTest.keyClick(field, Qt.Key.Key_Return, SHIFT)
    QTest.keyClicks(field, "b")
    assert field.text() == "a\nb" and field.height() > one_line
    for _ in range(9):
        QTest.keyClick(field, Qt.Key.Key_Return, SHIFT)
    five_lines = field.height()
    QTest.keyClick(field, Qt.Key.Key_Return, SHIFT)
    assert field.height() == five_lines  # at most five lines
    QTest.keyClick(field, Qt.Key.Key_Return)
    assert opened.sheet.text(0, 0).startswith("a\nb\n") and opened.grid.cell() == (1, 0)
    assert opened.model.data(opened.model.index(0, 0)).startswith("a↵b↵")  # the cell stays one line


def test_enter_in_the_last_row_adds_a_row(sheets):
    opened = sheets("a\nb\n")
    go(opened, 1, 0)
    QTest.keyClick(opened.grid, Qt.Key.Key_Return)  # not editing
    assert opened.sheet.row_count == 3 and opened.grid.cell() == (2, 0)
    type_into(opened, "c")
    QTest.keyClick(editor(opened), Qt.Key.Key_Return)
    assert contents(opened) == [["a"], ["b"], ["c"], [""]] and opened.grid.cell() == (3, 0)


def test_shift_enter_goes_up_when_not_editing(sheets):
    opened = sheets("a\nb\n")
    go(opened, 1, 0)
    QTest.keyClick(opened.grid, Qt.Key.Key_Return, SHIFT)
    assert opened.grid.cell() == (0, 0) and opened.ranges() == [(0, 0, 0, 0)]  # the selection doesn't grow
    QTest.keyClick(opened.grid, Qt.Key.Key_Return, SHIFT)
    assert opened.grid.cell() == (0, 0)


def test_tab_goes_right_and_on_to_the_next_row_and_adds_a_row_at_the_end(sheets):
    opened = sheets("a\tb\n1\t2\n")
    type_into(opened, "x")
    QTest.keyClick(editor(opened), Qt.Key.Key_Tab)
    assert opened.sheet.text(0, 0) == "x" and opened.grid.cell() == (0, 1)
    QTest.keyClick(opened.grid, Qt.Key.Key_Tab)  # past the last column: a new row, as this is the last one
    assert opened.sheet.row_count == 2 and opened.grid.cell() == (1, 0)
    go(opened, 0, 1)
    QTest.keyClick(opened.grid, Qt.Key.Key_Tab)
    assert opened.grid.cell() == (1, 0)
    QTest.keyClick(opened.grid, Qt.Key.Key_Tab, SHIFT)
    assert opened.grid.cell() == (0, 1)
    QTest.keyClick(opened.grid, Qt.Key.Key_Tab, SHIFT)
    QTest.keyClick(opened.grid, Qt.Key.Key_Tab, SHIFT)
    assert opened.grid.cell() == (0, 0)  # the first cell: nowhere to go


def test_shift_tab_in_the_editor_applies_and_goes_left(sheets):
    opened = sheets("a\tb\n1\t2\n")
    go(opened, 0, 1)
    type_into(opened, "y")
    QTest.keyClick(editor(opened), Qt.Key.Key_Backtab, SHIFT)
    assert opened.sheet.text(0, 1) == "y" and opened.grid.cell() == (0, 0)


def test_up_and_down_apply_and_move_in_a_cell_of_one_line(sheets):
    opened = sheets("a\nb\nc\n")
    go(opened, 1, 0)
    type_into(opened, "x")
    QTest.keyClick(editor(opened), Qt.Key.Key_Down)
    assert opened.sheet.text(1, 0) == "x" and opened.grid.cell() == (2, 0)
    type_into(opened, "y")
    QTest.keyClick(editor(opened), Qt.Key.Key_Up)
    assert opened.sheet.text(2, 0) == "y" and opened.grid.cell() == (1, 0)


def test_up_and_down_stay_in_a_cell_of_several_lines_left_and_right_stay_in_the_text(sheets):
    opened = sheets()
    opened.model.setData(opened.model.index(1, 0), "one\ntwo")
    go(opened, 1, 0)
    QTest.keyClick(opened.grid, Qt.Key.Key_F2)
    field = editor(opened)
    QTest.keyClick(field, Qt.Key.Key_Up)
    QTest.keyClick(field, Qt.Key.Key_Left)
    QTest.keyClick(field, Qt.Key.Key_Right)
    assert editor(opened) is field and opened.grid.cell() == (1, 0)
    QTest.keyClick(field, Qt.Key.Key_Escape)


def test_ctrl_enter_applies_the_text_to_all_selected_cells(sheets):
    opened = sheets("a\tb\n1\t2\n3\t4\n")
    select(opened, (0, 1, 0, 1))
    type_into(opened, "x")
    QTest.keyClick(editor(opened), Qt.Key.Key_Return, CTRL)
    assert contents(opened) == [["x", "x"], ["x", "x"]] and editor(opened) is None
    assert opened.ranges() == [(0, 1, 0, 1)]


def test_a_character_of_altgr_starts_editing_as_well(sheets):
    opened = sheets("old\n")
    QTest.keyClick(opened.grid, "@", CTRL | ALT)
    assert editor(opened) is not None and editor(opened).text() == "@"
    QTest.keyClick(editor(opened), Qt.Key.Key_Return)
    assert opened.sheet.text(0, 0) == "@"


def test_toolbar_buttons_apply_what_is_typed_first(sheets):
    opened = sheets("a\tb\n1\t2\n")
    type_into(opened, "x")
    opened.add_row_action.trigger()
    assert contents(opened) == [["x", "2"], ["", ""]] and editor(opened) is None


# --- keys --------------------------------------------------------------------------------------


def test_del_clears_the_selection_and_undo_brings_it_back(sheets):
    opened = sheets("a\tb\n1\t2\n3\t4\n")
    select(opened, (0, 1, 1, 1))
    QTest.keyClick(opened.grid, Qt.Key.Key_Delete)
    assert contents(opened) == [["1", ""], ["3", ""]]
    QTest.keyClick(opened.grid, Qt.Key.Key_Z, CTRL)
    assert contents(opened) == [["1", "2"], ["3", "4"]]
    QTest.keyClick(opened.grid, Qt.Key.Key_Y, CTRL)
    assert contents(opened) == [["1", ""], ["3", ""]]
    QTest.keyClick(opened.grid, Qt.Key.Key_Z, CTRL)
    QTest.keyClick(opened.grid, Qt.Key.Key_Z, CTRL | SHIFT)
    assert contents(opened) == [["1", ""], ["3", ""]]


def test_ctrl_z_in_an_open_editor_is_the_editor_s_own(sheets):
    opened = sheets("old\n")
    QTest.keyClick(opened.grid, Qt.Key.Key_F2)
    QTest.keyClicks(editor(opened), "x")
    steps = len(opened.sheet._undo)
    QTest.keyClick(editor(opened), Qt.Key.Key_Z, CTRL)
    assert editor(opened).text() == "old" and len(opened.sheet._undo) == steps
    QTest.keyClick(editor(opened), Qt.Key.Key_Escape)


def test_ctrl_v_in_an_open_editor_is_the_editor_s_own(sheets, clipboard):
    opened = sheets("old\n")
    QApplication.clipboard().setText("pasted")
    clipboard["text"] = "must not be used"
    QTest.keyClick(opened.grid, Qt.Key.Key_F2)
    QTest.keyClick(editor(opened), Qt.Key.Key_V, CTRL)
    assert editor(opened).text() == "oldpasted"
    QTest.keyClick(editor(opened), Qt.Key.Key_Escape)


def test_ctrl_v_pastes_at_the_top_left_cell_of_the_selection(sheets, clipboard):
    opened = sheets("a\tb\n1\t2\n3\t4\n")
    select(opened, (0, 1, 1, 1))
    clipboard["text"] = "x\ny\n"
    QTest.keyClick(opened.grid, Qt.Key.Key_V, CTRL)
    assert contents(opened) == [["1", "x"], ["3", "y"]]


def test_ctrl_c_copies_as_excel_does_and_ctrl_x_clears_too(sheets):
    opened = sheets("a\tb\n1\t2\n")
    opened.model.setData(opened.model.index(0, 1), 'two "x"\nlines')
    select(opened, (0, 0, 0, 1))
    QTest.keyClick(opened.grid, Qt.Key.Key_C, CTRL)
    assert QApplication.clipboard().text() == '1\t"two ""x""\nlines"'
    assert opened.sheet.text(0, 0) == "1"
    QTest.keyClick(opened.grid, Qt.Key.Key_X, CTRL)
    assert QApplication.clipboard().text() == '1\t"two ""x""\nlines"'
    assert contents(opened) == [["", ""]]


def test_copy_and_paste_within_a_sheet_are_exact(sheets, clipboard):
    opened = sheets()
    for row, cell in enumerate(['"Best" offer', "two\nlines", 'a"b', "tab\there", "1,50", "  x  "]):
        opened.model.setData(opened.model.index(row, 0), cell)
    select(opened, (0, 5, 0, 0))
    opened.copy_action.trigger()
    clipboard["text"] = QApplication.clipboard().text()
    go(opened, 0, 1)
    opened.paste_action.trigger()
    assert [opened.sheet.text(r, 1) for r in range(6)] == [opened.sheet.text(r, 0) for r in range(6)]


def test_too_many_cells_are_not_copied_and_cut_clears_nothing_then(sheets, monkeypatch):
    opened = sheets("a\tb\n1\t2\n3\t4\n")
    monkeypatch.setattr(data_panel, "MAX_COPY_CELLS", 3)
    QApplication.clipboard().setText("before")
    select(opened, (0, 1, 0, 1))
    opened.cut_action.trigger()
    assert QApplication.clipboard().text() == "before" and contents(opened) == [["1", "2"], ["3", "4"]]
    assert opened.status_text == "4 cells are too many to copy (at most 3): select fewer, or export the table"


def test_many_cells_are_copied_in_the_background(sheets, qtbot, monkeypatch):
    monkeypatch.setattr(data_panel, "_COPY_AT_ONCE", 2)
    opened = sheets("a\tb\n1\t2\n3\t4\n")
    select(opened, (0, 1, 0, 1))
    opened.copy_action.trigger()
    assert opened.status_text == "Copying 4 cells…"
    qtbot.waitUntil(lambda: opened.status_text == "Copied 4 cells", timeout=5000)
    assert QApplication.clipboard().text() == "1\t2\r\n3\t4"


def test_ctrl_d_and_ctrl_r_fill_down_and_right(sheets):
    opened = sheets("a\tb\n1\t2\n3\t4\n5\t6\n")
    select(opened, (0, 2, 0, 0))
    QTest.keyClick(opened.grid, Qt.Key.Key_D, CTRL)
    assert [row[0] for row in contents(opened)] == ["1", "1", "1"]
    select(opened, (2, 2, 0, 1))
    QTest.keyClick(opened.grid, Qt.Key.Key_R, CTRL)
    assert contents(opened)[2] == ["1", "1"]


def test_ctrl_r_is_not_reload_data_in_a_sheet(window, sheets, monkeypatch):
    reloads = []
    monkeypatch.setattr(window.session, "reload_sources", lambda: reloads.append(1))
    opened = sheets("a\tb\n1\t2\n")
    QTest.keyClick(opened.grid, Qt.Key.Key_R, CTRL)
    assert not reloads


def test_selecting_all_and_whole_rows_and_columns_with_keys(sheets):
    opened = sheets("a\tb\tc\n1\t2\t3\n4\t5\t6\n")
    QTest.keyClick(opened.grid, Qt.Key.Key_A, CTRL)
    assert opened.ranges() == [(0, 1, 0, 2)]
    select(opened, (0, 0, 1, 1))
    QTest.keyClick(opened.grid, Qt.Key.Key_Space, CTRL)
    assert opened.ranges() == [(0, 1, 1, 1)]
    select(opened, (1, 1, 1, 1))
    QTest.keyClick(opened.grid, Qt.Key.Key_Space, SHIFT)
    assert opened.ranges() == [(1, 1, 0, 2)]
    assert opened.sheet.text(1, 1) == "5"  # no space was typed


def test_ctrl_s_starts_add_as_table(sheets, monkeypatch):
    asked = []
    monkeypatch.setattr(SheetWindow, "ask_add", lambda self: asked.append(1))
    opened = sheets("a\n")
    QTest.keyClick(opened.grid, Qt.Key.Key_S, CTRL)
    assert asked == [1]


def test_the_usual_keys_of_a_grid_move_around(sheets):
    opened = sheets("a\tb\tc\n1\t2\t3\n4\t5\t6\n7\t8\t9\n")
    QTest.keyClick(opened.grid, Qt.Key.Key_End, CTRL)
    assert opened.grid.cell() == (2, 2)
    QTest.keyClick(opened.grid, Qt.Key.Key_Home)
    assert opened.grid.cell() == (2, 0)
    QTest.keyClick(opened.grid, Qt.Key.Key_Up)
    assert opened.grid.cell() == (1, 0)
    QTest.keyClick(opened.grid, Qt.Key.Key_Right)
    assert opened.grid.cell() == (1, 1)
    QTest.keyClick(opened.grid, Qt.Key.Key_Home, CTRL)
    assert opened.grid.cell() == (0, 0)


def test_cut_and_copy_in_an_open_editor_are_the_editor_s_own(sheets):
    opened = sheets("old\nnext\n")
    QTest.keyClick(opened.grid, Qt.Key.Key_F2)
    QTest.keyClick(editor(opened), Qt.Key.Key_A, CTRL)
    QTest.keyClick(editor(opened), Qt.Key.Key_X, CTRL)
    assert editor(opened).text() == "" and QApplication.clipboard().text() == "old"
    assert opened.sheet.text(0, 0) == "old"
    QTest.keyClick(editor(opened), Qt.Key.Key_Escape)


def test_keys_of_a_sheet_do_not_reach_the_main_window(window, session, sheets, registry):
    session.add_table(registry.actions["csv.import"])
    opened = sheets("a\n")
    QTest.keyClick(opened.grid, Qt.Key.Key_Z, CTRL)
    assert len(session.project.tables) == 1 and not opened.sheet.has_values


# --- adding, deleting, the switch --------------------------------------------------------------


def test_add_and_delete_rows_and_columns(sheets):
    opened = sheets("a\tb\n1\t2\n3\t4\n")
    go(opened, 0, 0)
    opened.add_row_action.trigger()
    assert contents(opened) == [["1", "2"], ["", ""], ["3", "4"]] and opened.grid.cell() == (1, 0)
    opened.row_above_action.trigger()
    assert opened.sheet.row_count == 4 and opened.grid.cell() == (1, 0)
    select(opened, (1, 2, 0, 0))
    opened.delete_rows_action.trigger()
    assert contents(opened) == [["1", "2"], ["3", "4"]]
    go(opened, 0, 0)
    opened.add_column_action.trigger()
    assert contents(opened) == [["1", "", "2"], ["3", "", "4"]] and opened.grid.cell() == (0, 1)
    assert opened.sheet.names == ["a", "Column 2", "b"]
    opened.column_left_action.trigger()
    assert opened.sheet.column_count == 4 and opened.grid.cell() == (0, 1)
    select(opened, (0, 0, 1, 2))
    opened.delete_columns_action.trigger()
    assert contents(opened) == [["1", "2"], ["3", "4"]]
    opened.undo_action.trigger()
    assert opened.sheet.column_count == 4


def test_deleting_all_rows_and_all_columns_leaves_a_usable_sheet(sheets):
    opened = sheets("a\tb\n1\t2\n")
    QTest.keyClick(opened.grid, Qt.Key.Key_A, CTRL)
    opened.delete_rows_action.trigger()
    assert (
        opened.sheet.row_count == 0
        and opened.model.rowCount() == 0
        and "0 rows × 2 columns" in opened.status_line.text()
    )
    opened.add_row_action.trigger()
    assert opened.sheet.row_count == 1 and opened.grid.cell() == (0, 0)
    QTest.keyClick(opened.grid, Qt.Key.Key_A, CTRL)
    opened.delete_columns_action.trigger()
    assert opened.sheet.column_count == 1 and opened.sheet.names == ["Column 1"]
    type_into(opened, "x")
    QTest.keyClick(editor(opened), Qt.Key.Key_Return)
    assert opened.sheet.text(0, 0) == "x"


def test_a_sheet_without_rows_says_why_no_column_can_be_deleted(sheets):
    opened = sheets("a\tb\n1\t2\n")
    opened.grid.selectAll()
    opened.delete_rows_action.trigger()
    assert opened.sheet.row_count == 0
    opened.delete_columns_action.trigger()
    assert opened.sheet.column_count == 2
    assert opened.status_text == "A sheet without rows has no cell to select: add a row with + Row, then select the column."


def test_the_widths_of_the_columns_stay_when_columns_change(sheets):
    opened = sheets("a\tb\tc\n1\t2\t3\n")
    opened.grid.setColumnWidth(0, 222)
    opened.grid.setColumnWidth(2, 111)
    go(opened, 0, 2)
    opened.add_column_action.trigger()
    assert opened.grid.columnWidth(0) == 222 and opened.grid.columnWidth(2) == 111 and opened.grid.columnWidth(3) > 0
    opened.undo_action.trigger()
    assert opened.grid.columnWidth(0) == 222 and opened.grid.columnWidth(2) == 111
    opened.model.setData(opened.model.index(0, 0), "x")
    assert opened.grid.columnWidth(0) == 222


def test_undo_keeps_the_current_cell_within_the_sheet(sheets, clipboard):
    opened = sheets("a\tb\n1\t2\n3\t4\n")
    go(opened, 1, 1)
    opened.undo_action.trigger()  # the sheet before the paste
    assert not opened.sheet.has_values and opened.grid.cell() == (1, 1)
    opened.redo_action.trigger()
    assert opened.grid.cell() == (1, 1)
    clipboard["text"] = "x"
    go(opened, 1, 1)
    opened.add_row_action.trigger()
    go(opened, 2, 1)
    opened.undo_action.trigger()
    assert opened.grid.cell() == (1, 1)  # that row is gone


def test_undo_and_redo_buttons_follow_the_sheet(sheets):
    opened = sheets()
    assert not opened.undo_action.isEnabled() and not opened.redo_action.isEnabled()
    opened.model.setData(opened.model.index(0, 0), "x")
    assert opened.undo_action.isEnabled()
    opened.undo_action.trigger()
    assert not opened.undo_action.isEnabled() and opened.redo_action.isEnabled()
    assert not opened.windowTitle().startswith("Sheet 1 •")


def test_the_switch_moves_the_first_row_in_and_out_of_the_names(sheets):
    opened = sheets("a\tb\n1\t2\n")
    opened.switch.click()
    assert not opened.sheet.header and contents(opened) == [["a", "b"], ["1", "2"]]
    assert opened.sheet.names == ["Column 1", "Column 2"] and opened.model.rowCount() == 2
    assert opened.model.headerData(0, Qt.Orientation.Horizontal) == "Column 1"
    opened.undo_action.trigger()
    assert opened.switch.isChecked() and opened.sheet.names == ["a", "b"]
    assert opened.model.headerData(1, Qt.Orientation.Horizontal) == "b"


def test_rename_a_column_by_double_click(sheets, monkeypatch):
    opened = sheets("a\tb\n1\t2\n")
    monkeypatch.setattr(SheetWindow, "ask_name", lambda self, old: old + "x")
    header = opened.grid.horizontalHeader()
    QTest.mouseDClick(
        header.viewport(), Qt.MouseButton.LeftButton, pos=QPoint(header.sectionViewportPosition(1) + 5, 5)
    )
    assert opened.sheet.names == ["a", "bx"] and opened.model.headerData(1, Qt.Orientation.Horizontal) == "bx"


def test_rename_problems_go_to_the_status_line(sheets, monkeypatch):
    opened = sheets("a\tb\n1\t2\n")
    monkeypatch.setattr(SheetWindow, "ask_name", lambda self, old: "b")
    opened.rename_column(0)
    assert opened.status_text == "A column named “b” already exists" and opened.sheet.names == ["a", "b"]
    monkeypatch.setattr(SheetWindow, "ask_name", lambda self, old: " ")
    opened.rename_column(0)
    assert opened.status_text == "A column needs a name"
    monkeypatch.setattr(SheetWindow, "ask_name", lambda self, old: None)
    opened.rename_column(0)
    assert opened.sheet.names == ["a", "b"]


def test_fill_writes_one_value_into_the_selected_cells(sheets, monkeypatch):
    opened = sheets("a\tb\n1\t2\n3\t4\n")
    asked = []
    monkeypatch.setattr(SheetWindow, "ask_fill", lambda self, cells: asked.append(cells) or "x")
    select(opened, (0, 1, 0, 1))
    opened.fill_action.trigger()
    assert asked == [4] and contents(opened) == [["x", "x"], ["x", "x"]]
    monkeypatch.setattr(SheetWindow, "ask_fill", lambda self, cells: None)
    opened.undo_action.trigger()
    opened.fill_action.trigger()
    assert contents(opened) == [["1", "2"], ["3", "4"]]


def test_menus_and_toolbar_texts(sheets):
    opened = sheets("a\tb\n1\t2\n")
    texts = [a.text() for a in opened.grid.cell_menu().actions() if not a.isSeparator()]
    assert texts == ["Copy", "Paste", "Fill…", "Clear", "Insert row above", "Insert row below", "Delete rows"]
    texts = [a.text() for a in opened.grid.column_menu(0).actions()]
    assert texts == ["Rename…", "Insert column left", "Insert column right", "Delete columns"]
    assert [a.text() for a in opened.grid.row_menu().actions()] == [
        "Insert row above",
        "Insert row below",
        "Delete rows",
    ]
    toolbar = opened.findChild(QToolBar, "SheetToolbar")
    assert [a.iconText() for a in toolbar.actions() if not a.isSeparator()] == [
        "Paste",
        "Paste below",
        "Undo",
        "Redo",
        "+ Row",
        "+ Column",
        "Delete rows",
        "Delete columns",
        "Fill…",
    ]
    assert opened.paste_action.toolTip() == "Paste the clipboard at the selected cell (Ctrl+V)"
    assert opened.main_button.text() == "Add as table…"


def test_a_cell_shows_one_line_of_at_most_500_characters_and_a_tooltip(sheets):
    opened = sheets()
    opened.model.setData(opened.model.index(0, 0), "x" * 600)
    opened.model.setData(opened.model.index(1, 0), "short")
    index = opened.model.index(0, 0)
    assert opened.model.data(index) == "x" * 500 + "…"
    assert opened.model.data(index, Qt.ItemDataRole.EditRole) == "x" * 600
    assert opened.model.data(index, Qt.ItemDataRole.ToolTipRole)
    assert opened.model.data(opened.model.index(1, 0), Qt.ItemDataRole.ToolTipRole) is None


def test_edit_triggers(sheets):
    triggers = sheets("a\n").grid.editTriggers()
    for trigger in ("DoubleClicked", "EditKeyPressed", "AnyKeyPressed"):
        assert triggers & getattr(QAbstractItemView.EditTrigger, trigger)


def test_the_model_stays_in_step_with_the_sheet_after_each_kind_of_change(qapp):
    sheet = Sheet(columns=2, rows=3)
    model = SheetModel(sheet)
    events = []
    model.modelReset.connect(lambda: events.append("reset"))
    model.rowsInserted.connect(lambda *a: events.append("rows+"))
    model.rowsRemoved.connect(lambda *a: events.append("rows-"))
    model.dataChanged.connect(lambda *a: events.append("data"))
    sheet.set_text(0, 0, "x")
    model.sync()
    assert events == ["data"]
    sheet.insert_rows(1, 2)
    model.sync()
    assert events[1:] == ["rows+", "data"] and model.rowCount() == 5
    sheet.delete_rows([(0, 3)])
    model.sync()
    assert events[3:] == ["rows-", "data"] and model.rowCount() == 1
    sheet.insert_columns(0)
    model.sync()
    assert events[5:] == ["reset"] and model.columnCount() == 3
    sheet.delete_rows([(0, 0)])
    model.sync()
    assert model.rowCount() == 0


# --- the popup of Add as table -----------------------------------------------------------------


def frame_sheet(**columns) -> Sheet:
    return Sheet.from_frame(pl.DataFrame(columns))


def dialog_for(tmp_path, sheet=None, name="Sheet 1", unsaved=False) -> AddSheetDialog:
    return AddSheetDialog(sheet or frame_sheet(a=["1"]), name, tmp_path, unsaved)


def test_a_typed_name_gives_up_one_suffix(qapp, tmp_path):
    dialog = dialog_for(tmp_path)
    dialog.name.setText("report.csv.txt")
    dialog.name.editingFinished.emit()
    assert dialog.name.text() == "report.csv" and dialog.format.currentData() == ".txt"
    assert dialog.check() is None and dialog.chosen() == (tmp_path / "report.csv.txt", ".txt")


def test_a_name_that_gave_up_its_suffix_keeps_the_rest_when_the_popup_comes_back(qapp, tmp_path):
    dialog = dialog_for(tmp_path)
    dialog.show_choice(tmp_path / "report.csv.txt", ".txt")
    assert dialog.name.text() == "report.csv" and dialog.format.currentData() == ".txt"
    assert dialog.check() is None and dialog.chosen() == (tmp_path / "report.csv.txt", ".txt")


@pytest.mark.parametrize("name", ["a.csv/", "report.csv\\", "a.csv\\\\"])
def test_a_slash_at_the_end_of_a_name_is_no_suffix(qapp, tmp_path, name):
    dialog = dialog_for(tmp_path)
    dialog.name.setText(name)
    assert dialog.check() == "A file name can't contain / or \\: the Folder says where the file goes."


def test_a_file_name_can_t_start_with_a_tilde(qapp, tmp_path):
    dialog = dialog_for(tmp_path)
    dialog.name.setText("~budget")
    assert dialog.check() == "A file name here can't start with ~: it means your home folder in the settings of a step."


def test_the_popup_starts_with_a_free_name_and_xlsx(qapp, tmp_path):
    dialog = dialog_for(tmp_path)
    assert dialog.name.text() == "Sheet 1" and dialog.format.currentData() == ".xlsx"
    assert [dialog.format.itemText(i) for i in range(dialog.format.count())] == [
        "Excel workbook (.xlsx)",
        "CSV file (.csv)",
        "Text file with tabs (.txt)",
        "Parquet file (.parquet)",
    ]
    assert dialog.folder.text() == str(tmp_path)
    (tmp_path / "Sheet 1.xlsx").touch()
    (tmp_path / "Sheet 2.xlsx").touch()
    (tmp_path / "Sheet 3.csv").touch()
    dialog = dialog_for(tmp_path)
    assert dialog.name.text() == "Sheet 3"
    dialog.set_suffix(".csv")
    assert dialog.name.text() == "Sheet 1"  # that name is free as .csv
    (tmp_path / "Sheet 4.csv").touch()
    dialog = dialog_for(tmp_path, name="Sheet 4")
    dialog.set_suffix(".csv")
    assert dialog.name.text() == "Sheet 5"


def test_a_typed_name_stays_when_the_format_changes(qapp, tmp_path):
    (tmp_path / "orders.csv").touch()
    dialog = dialog_for(tmp_path)
    dialog.name.setText("orders")
    dialog.name.setModified(True)
    dialog.set_suffix(".csv")
    assert dialog.name.text() == "orders"


def test_a_typed_extension_chooses_the_format(qapp, tmp_path):
    dialog = dialog_for(tmp_path)
    dialog.name.setText("orders.CSV")
    dialog.accept()
    assert dialog.result() == dialog.DialogCode.Accepted
    assert dialog.chosen() == (tmp_path / "orders.csv", ".csv") and dialog.name.text() == "orders"


@pytest.mark.parametrize(
    ("name", "problem"),
    [
        ("", "Enter a file name."),
        ("   ", "Enter a file name."),
        ("a{b", "A file name here can't contain { } or %: they have a meaning in the settings of a step."),
        ("50%", "A file name here can't contain { } or %: they have a meaning in the settings of a step."),
        ("a:b", None),  # the message of files.name_problem
        ("CON", None),
    ],
)
def test_name_problems_keep_the_popup_open_with_a_red_line(qapp, tmp_path, name, problem):
    dialog = dialog_for(tmp_path)
    dialog.name.setText(name)
    dialog.accept()
    assert dialog.result() != dialog.DialogCode.Accepted and dialog.problem.text()
    assert dialog.problem.isVisibleTo(dialog)
    if problem:
        assert dialog.problem.text() == problem


@pytest.mark.parametrize(
    ("name", "problem"),
    [
        ("a/b", "A file name can't contain / or \\: the Folder says where the file goes."),
        ("a\\b", "A file name can't contain / or \\: the Folder says where the file goes."),
        ("..\\x", "A file name can't contain / or \\: the Folder says where the file goes."),
        (".", "Enter a file name."),
        ("..", "Enter a file name."),
        ("...", "Enter a file name."),
    ],
)
def test_a_file_name_is_no_path(qapp, tmp_path, name, problem):
    dialog = dialog_for(tmp_path)
    dialog.name.setText(name)
    dialog.accept()
    assert dialog.problem.text() == problem and dialog.result() == 0
    assert list(tmp_path.iterdir()) == []


def test_a_path_the_step_could_not_read_back_keeps_the_popup_open(window, session, sheets, tmp_path, monkeypatch):
    folder = tmp_path / "a %TEMP% b"
    folder.mkdir()
    monkeypatch.setenv("TEMP", "C:\\Temp")
    opened = sheets("a\tb\n1\t2\n")
    dialog = AddSheetDialog(opened.sheet, "x", folder, True, opened, session.sheet_path_problem)
    dialog.accept()
    assert dialog.problem.text() == (
        "This path can't be used: %NAME% and {Name} have a meaning in the settings of a step. Choose another folder."
    )
    plain = tmp_path / "100% {brace}"
    plain.mkdir()
    dialog.folder.setText(str(plain))
    assert dialog.check() is None


def test_a_folder_with_the_name_of_a_parameter_is_refused(session, tmp_path):
    session.project.parameters.append(Parameter("Day", "Monday"))
    assert session.sheet_path_problem(tmp_path / "{Day}" / "x.xlsx") is not None
    assert session.sheet_path_problem(tmp_path / "{Week}" / "x.xlsx") is None
    assert session.sheet_path_problem(tmp_path / "x.xlsx") is None


def test_a_folder_written_in_another_way_is_the_same_folder(session, demo):
    session.open_project(demo)
    folder = demo.parent
    assert session.sheet_path_problem(folder / "data" / ".." / "x.csv") is None


def test_folder_problems_keep_the_popup_open(qapp, tmp_path):
    (tmp_path / "file").touch()
    dialog = dialog_for(tmp_path)
    dialog.folder.setText(str(tmp_path / "file"))
    dialog.accept()
    assert "is a file, not a folder" in dialog.problem.text()
    dialog.folder.setText("relative")
    dialog.accept()
    assert dialog.problem.text() == "Enter the full path of a folder."
    dialog.folder.setText(str(tmp_path / "new" / "folder"))  # a new folder is made when the file is written
    dialog.accept()
    assert dialog.result() == dialog.DialogCode.Accepted


def test_excel_limits_are_shown_in_the_popup(qapp, tmp_path, monkeypatch):
    dialog = dialog_for(tmp_path, frame_sheet(a=["x" * 40_000, "y" * 40_000]))
    dialog.accept()
    assert (
        dialog.problem.text()
        == "2 cells have more than 32,767 characters, the most an Excel cell holds. Choose CSV, TXT or Parquet."
    )
    dialog.set_suffix(".csv")
    dialog.accept()
    assert dialog.result() == dialog.DialogCode.Accepted


def test_the_notes_of_the_popup(qapp, tmp_path):
    assert not dialog_for(tmp_path).note.text()
    dialog = dialog_for(tmp_path, unsaved=True)
    assert dialog.note.text() == "The project is not saved yet: it keeps the full path of this file."

    class Big:
        row_count, column_count = 1_000_000, 20

    dialog = dialog_for(tmp_path, Big())
    assert dialog.note.text() == (
        "This sheet has 20,000,000 cells: writing .xlsx takes about 2 minutes, "
        "and reading it takes about 2.6 GB of memory. CSV and Parquet take seconds."
    )
    dialog.set_suffix(".parquet")
    assert not dialog.note.text()


# --- closing -----------------------------------------------------------------------------------


@pytest.fixture
def no_project_question(window, monkeypatch):
    asked = []
    monkeypatch.setattr(window, "ask_save", lambda reason: asked.append(reason) or "discard")
    return asked


def test_an_empty_or_untouched_sheet_closes_without_a_question(window, sheets, qtbot, monkeypatch):
    monkeypatch.setattr(SheetWindow, "ask_close_draft", lambda self: pytest.fail("asked"))
    opened = sheets()
    assert opened.close() and window._sheets == []
    opened = sheets()
    opened.model.setData(opened.model.index(0, 0), "x")
    opened.undo_action.trigger()
    assert opened.close() and window._sheets == []


def test_closing_a_draft_asks_and_cancel_keeps_it(window, sheets, monkeypatch):
    asked = []
    monkeypatch.setattr(SheetWindow, "ask_close_draft", lambda self: asked.append(self.title) or "cancel")
    opened = sheets("a\n")
    assert not opened.close()
    assert asked == ["Sheet 1"] and window._sheets == [opened] and opened.isVisible()
    monkeypatch.setattr(SheetWindow, "ask_close_draft", lambda self: "discard")
    assert opened.close() and window._sheets == []


def test_closing_applies_what_is_typed_first(sheets, monkeypatch):
    asked = []
    monkeypatch.setattr(SheetWindow, "ask_close_draft", lambda self: asked.append(1) or "cancel")
    opened = sheets()
    type_into(opened, "x")
    assert not opened.close()
    assert asked == [1] and opened.sheet.text(0, 0) == "x"


def test_a_draft_can_be_added_from_the_closing_question(window, session, sheets, tmp_path, qtbot, monkeypatch):
    monkeypatch.setattr(SheetWindow, "ask_close_draft", lambda self: "add")
    monkeypatch.setattr(SheetWindow, "ask_add", lambda self: (tmp_path / "a.csv", ".csv"))
    opened = sheets("a\tb\n1\t2\n")
    assert not opened.close()
    assert window._sheets == [opened] and opened.busy
    qtbot.waitUntil(lambda: not window._sheets)
    assert [t.name for t in session.project.tables] == ["a"]


def test_cancel_in_the_popup_after_the_closing_question_keeps_the_sheet(window, sheets, monkeypatch):
    monkeypatch.setattr(SheetWindow, "ask_close_draft", lambda self: "add")
    monkeypatch.setattr(SheetWindow, "ask_add", lambda self: None)
    opened = sheets("a\n")
    assert not opened.close()
    assert window._sheets == [opened] and not opened.busy


def test_closing_while_busy_waits(window, sheets, qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(SheetWindow, "ask_add", lambda self: (tmp_path / "a.xlsx", ".xlsx"))
    opened = sheets("a\tb\n1\t2\n")
    opened.add_as_table()
    assert opened.busy and not opened.close()
    assert opened.status_text == "The sheet is being written: wait, or press Stop."
    qtbot.waitUntil(lambda: not window._sheets)


def test_the_reason_stays_while_the_file_is_written(window, sheets, qtbot, tmp_path, monkeypatch):
    """Closing the app waits for the file, and both windows go on saying why."""
    monkeypatch.setattr(SheetWindow, "ask_add", lambda self: (tmp_path / "a.xlsx", ".xlsx"))
    opened = sheets("a\tb\n1\t2\n")
    opened.add_as_table()
    window.close()
    reason = "The sheet is being written: wait, or press Stop."
    assert window.statusBar().currentMessage() == reason
    opened._show_progress(0.5)
    assert opened.status_text == f"{opened._job_text} 50%  ·  {reason}"
    qtbot.waitUntil(lambda: not window._sheets)


def test_the_reason_ends_with_the_job(window, sheets, qtbot, tmp_path, monkeypatch):
    opened = sheets("a\tb\n1\t2\n")
    monkeypatch.setattr(opened, "ask_add", lambda: (tmp_path / "a.xlsx", ".xlsx"))
    monkeypatch.setattr(opened, "_added", lambda path, suffix: None)
    opened.add_as_table()
    assert not opened.close()
    qtbot.waitUntil(lambda: not opened.busy)
    assert opened._close_refused == "" and "written" not in opened.status_text


def test_closing_the_app_asks_each_sheet_then_the_project(
    window, session, registry, sheets, no_project_question, monkeypatch
):
    order = []
    monkeypatch.setattr(SheetWindow, "ask_close_draft", lambda self: order.append(self.title) or "discard")
    monkeypatch.setattr(window, "ask_save", lambda reason: order.append("project") or "discard")
    sheets("a\n")
    sheets("b\n")
    session.add_table(registry.actions["csv.import"])
    assert window.close()
    assert order == ["Sheet 1", "Sheet 2", "project"] and window._sheets == []


def test_cancel_at_a_sheet_stops_closing_the_app(window, session, registry, sheets, no_project_question, monkeypatch):
    monkeypatch.setattr(SheetWindow, "ask_close_draft", lambda self: "cancel")
    first = sheets("a\n")
    session.add_table(registry.actions["csv.import"])
    assert not window.close()
    assert not no_project_question and window._sheets == [first]


def test_cancel_at_the_project_keeps_every_sheet_with_its_data(window, session, registry, sheets, monkeypatch):
    monkeypatch.setattr(SheetWindow, "ask_close_draft", lambda self: "discard")
    monkeypatch.setattr(window, "ask_save", lambda reason: "cancel")
    first = sheets("a\n")
    session.add_table(registry.actions["csv.import"])
    assert not window.close()
    assert window._sheets == [first] and first.sheet.text(0, 0) == "a" and first.isVisible()


def test_add_as_table_while_the_app_closes_goes_on_with_the_next_question(
    window, session, sheets, tmp_path, qtbot, monkeypatch
):
    order = []
    monkeypatch.setattr(
        SheetWindow,
        "ask_close_draft",
        lambda self: order.append(self.title) or ("add" if self.title == "Sheet 1" else "discard"),
    )
    monkeypatch.setattr(SheetWindow, "ask_add", lambda self: (tmp_path / "a.csv", ".csv"))
    monkeypatch.setattr(window, "ask_save", lambda reason: order.append("project") or "discard")
    sheets("a\n")
    sheets("b\n")
    window.show()
    assert not window.close()
    assert order == ["Sheet 1"]
    qtbot.waitUntil(lambda: "project" in order)
    assert order == ["Sheet 1", "Sheet 2", "project"] and not window.isVisible() and window._sheets == []
    assert [t.name for t in session.project.tables] == ["a"]


def test_a_failed_add_while_the_app_closes_stops_closing(window, sheets, tmp_path, qtbot, monkeypatch):
    """The file can't be written: the window stays with its data, and nothing closes."""
    (tmp_path / "taken").touch()
    problems = []
    monkeypatch.setattr(SheetWindow, "ask_close_draft", lambda self: "add")
    monkeypatch.setattr(SheetWindow, "ask_add", lambda self: (tmp_path / "taken" / "a.csv", ".csv"))
    monkeypatch.setattr(SheetWindow, "show_problem", lambda self, text: problems.append(text))
    opened = sheets("a\n")
    window.show()
    assert not window.close()
    qtbot.waitUntil(lambda: not opened.busy)
    assert problems and window._sheets == [opened] and window.isVisible() and not opened.closes_app
    assert opened.sheet.changed


def test_a_running_project_is_asked_about_first(window, session, sheets, monkeypatch):
    """The sheets come after the question about the run: closing can still be cancelled there."""
    monkeypatch.setattr(SheetWindow, "ask_close_draft", lambda self: pytest.fail("asked"))
    monkeypatch.setattr(window, "ask_close_while_running", lambda: "cancel")
    monkeypatch.setattr(type(session), "running", property(lambda self: True))
    sheets("a\n")
    assert not window.close()
    monkeypatch.undo()


@pytest.mark.parametrize("answer", ["wait", "stop"])
def test_a_run_that_ended_during_the_question_closes_the_window(
    window, session, qtbot, no_project_question, monkeypatch, answer
):
    state = {"running": True}

    def asked() -> str:
        state["running"] = False
        return answer

    monkeypatch.setattr(window, "ask_close_while_running", asked)
    monkeypatch.setattr(type(session), "running", property(lambda self: state["running"]))
    window.show()
    assert not window.close()
    qtbot.waitUntil(lambda: not window.isVisible())
    assert not window._close_after_run
    monkeypatch.undo()


def test_opening_another_project_leaves_a_draft_open(window, session, sheets, demo, settle):
    opened = sheets("a\n")
    window.open_path(demo)
    settle(session)
    assert window._sheets == [opened] and opened.sheet.text(0, 0) == "a"
    assert opened.status_text == "Pasted 1 row × 1 column"


def test_sheet_numbers_go_on(window, sheets):
    assert sheets().title == "Sheet 1"
    assert sheets("a\n").title == "Sheet 2"


def test_the_window_remembers_its_size(window, sheets):
    opened = sheets()
    opened.resize(700, 500)
    opened.close()
    assert sheets().size().width() == 700


def test_a_sheet_is_a_window_of_its_own_that_does_not_block_the_main_window(window, sheets):
    opened = sheets()
    assert opened.isWindow() and opened.parentWidget() is window and not opened.isModal()
    assert opened.testAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    assert isinstance(opened.main_action, QAction)


def test_the_model_shows_the_names_of_a_pasted_first_row(qapp):
    sheet = Sheet()
    sheet.load(Pasted(pl.DataFrame({"c0": ["a", "1"], "c1": ["b", "2"]}), "\t"))
    assert SheetModel(sheet).headerData(1, Qt.Orientation.Horizontal) == "b"
