"""Text from a project or a data file is plain text: Qt reads a label, a tooltip or a message box with
a tag in it as HTML, and then loads the images the tag names, also from another computer."""

import ast
import itertools
import json
import sqlite3
from pathlib import Path

import polars as pl
import pytest
import xlsxwriter
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QGuiApplication, QTextDocument
from PySide6.QtGui import Qt as GuiQt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QGraphicsView,
    QInputDialog,
    QLabel,
    QMessageBox,
    QToolTip,
    QWidget,
)

from tafl import APP_NAME, crash
from tafl.api import Kind
from tafl.ui import app, labels
from tafl.ui.checks import checks
from tafl.ui.connections_dialog import ConnectionsDialog
from tafl.ui.filter_pane import _Values
from tafl.ui.filters import COUNT, VALUE, Filter
from tafl.ui.forms import ExpressionEditor
from tafl.ui.frame_model import FrameModel
from tafl.ui.labels import LONG_TIP_LINES, WrappedLabel, message_box, plain_html, tooltip_text
from tafl.ui.map.items import GroupFrame, GroupNode, TableNode
from tafl.ui.parameters_dialog import ParametersDialog
from tafl.ui.sheet_window import SheetWindow, ask, problem
from tafl.ui.tables_panel import confirm_delete_table

MARK = "<b>MARK"
LONG = " and some more words: a long cell has a tooltip"


def marked(name: str) -> str:
    return f'{MARK}-{name}</b><img src="{name}.png">'


# --- a project with a marker in every name and value --------------------------------------------


def settings(spec, name: str) -> dict:
    """A marker in everything an action (or a row of a list) takes as text."""
    values = {}
    for param in spec:
        code = f"{name}-{param.key}"
        if param.kind in ("text", "code", "expression", "file", "folder", "column", "table", "connection"):
            values[param.key] = marked(code)
        elif param.kind in ("columns", "tables"):
            values[param.key] = [marked(code)]
        elif param.kind == "list":
            values[param.key] = [settings(param.fields, code)]
    return values


def marked_files(folder) -> tuple[str, str]:
    """The data files, also with markers. Returns the names of the first two columns of data.csv."""
    first, second = marked("first"), marked("second")
    rows = {first: [marked(f"cell{n}") + LONG for n in range(3)], second: [marked(f"odd{n}") for n in range(3)], "number": [1, 2, 3]}
    pl.DataFrame(rows).write_csv(folder / "data.csv", separator=";")
    pl.DataFrame({marked("parquet"): [marked("parquet cell") + LONG], "number": [1]}).write_parquet(folder / "data.parquet")
    with xlsxwriter.Workbook(folder / "book.xlsx") as book:
        sheet = book.add_worksheet(f"{MARK}-sheet")
        sheet.write_row(0, 0, [marked("excel"), "number"])
        sheet.write_row(1, 0, [marked("excel cell") + LONG, 1])
    with sqlite3.connect(folder / "local.sqlite") as db:
        table, column = marked("sql table").replace('"', '""'), marked("sql column").replace('"', '""')
        db.execute(f'CREATE TABLE "{table}" ("{column}" TEXT)')
        db.execute(f'INSERT INTO "{table}" VALUES (?)', (marked("sql cell") + LONG,))
    return first, second


def marked_project(folder, registry):
    first, second = marked_files(folder)
    versions = {action.id: action.version for action in registry.actions.values()}

    def step(ident: str, action: str, params: dict) -> dict:
        return {"id": ident, "action": action, "version": versions.get(action, 1), "params": params, "note": marked(ident + " note")}

    data = {"file": "data.csv", "delimiter": ";", "file_column": False}
    tables = [
        {"id": "t1", "name": marked("table"), "steps": [
            step("s1", "csv.import", data),
            step("s2", "columns.rename", {"columns": [{"column": first, "new_name": marked("renamed")}]}),
            step("s3", "calc.columns", {"columns": [{"name": marked("calculated"), "formula": "[number] * 2"}]}),
            step("s4", "columns.change_type", {"columns": [{"column": second, "type": "integer"}], "on_error": "empty"}),
        ]},
        {"id": "off", "name": marked("off"), "enabled": False, "group": "g1", "steps": [step("s5", "csv.import", data)]},
        {"id": "uses", "name": marked("uses"), "group": "g1", "steps": [step("s6", "table.derive", {"table": "off"})]},
        {"id": "unfinished", "name": marked("unfinished"), "group": "g2", "steps": [step("s7", "csv.import", {})]},
        {"id": "waits", "name": marked("waits"), "group": "g2", "steps": [step("s8", "table.derive", {"table": "unfinished"})]},
        {"id": "unknown", "name": marked("unknown"), "steps": [step("s9", marked("action"), {"x": marked("setting")})]},
        {"id": "filtered", "name": marked("filtered"), "steps": [step("s10", "csv.import", data)], "filters": [
            {"column": marked("no such column"), "values": [marked("value")]}, {"column": first, "facet": "values", "values": []},
        ]},
        {"id": "excel", "name": marked("excel table"), "steps": [step("s11", "excel.import", {"file": "book.xlsx", "file_column": False})]},
        {"id": "parquet", "name": marked("parquet table"), "steps": [step("s12", "parquet.import", {"file": "data.parquet", "file_column": False})]},
        {"id": "database", "name": marked("database"), "steps": [
            step("s13", "db.import", {"connection": marked("sqlite"), "mode": "table", "table": marked("sql table")}),
        ]},
        {"id": "query", "name": marked("query"), "steps": [
            step("s14", "db.import", {"connection": marked("sqlite"), "mode": "query", "query": f"SELECT {marked('query')}"}),
        ]},
        {"id": "formula", "name": marked("formula"), "steps": [
            step("s15", "csv.import", data),
            step("s16", "calc.columns", {"columns": [{"name": "n", "formula": f"CAST([{second}] AS INTEGER)"}]}),
        ]},
        {"id": "parameter", "name": marked("parameter table"), "steps": [step("s17", "csv.import", {"file": "{P2}.csv"})]},
        {"id": "filled", "name": marked("filled"), "steps": [
            step("s18", "csv.import", data),
            step("s19", "calc.columns", {"columns": [{"name": "{P3}", "formula": "[number]"}, {"name": "n", "formula": "[number]"}]}),
        ]},
        {"id": "empty", "name": marked("empty"), "steps": []},
    ]
    for number, action in enumerate(registry.actions.values(), start=1):
        steps = [step(f"a{number}", action.id, settings(action.params, f"action {number}"))]
        if action.kind != Kind.SOURCE:
            steps.insert(0, step(f"b{number}", "table.derive", {"table": "t1"}))
        tables.append({"id": f"action{number}", "name": marked(f"action {number}"), "steps": steps})
    project = {
        "format": "tafl-project",
        "format_version": 1,
        "name": marked("project"),
        "connections": [
            {"name": marked("sqlite"), "type": "sqlite", "settings": {"file": "local.sqlite"}},
            {"name": marked("no plugin"), "type": marked("type"), "settings": {"file": marked("connection file")}},
        ],
        "groups": [
            {"id": "g1", "name": marked("group"), "color": "blue"},
            {"id": "g2", "name": marked("collapsed"), "color": "green", "collapsed": True},
            {"id": "g3", "name": marked("no tables"), "color": marked("color")},
        ],
        "parameters": [
            {"name": marked("parameter"), "value": marked("value")},
            {"name": "P2", "value": "=" + marked("formula")},
            {"name": "P3", "value": marked("filled in")},
        ],
        "tables": tables,
    }
    path = folder / "marked.tafl"
    path.write_text(json.dumps(project, ensure_ascii=False), encoding="utf-8")
    return path


# --- what Qt would read as HTML -------------------------------------------------------------------


def read_as_html(text) -> bool:
    return isinstance(text, str) and MARK in text and GuiQt.mightBeRichText(text)


def column_count(model, parent) -> int:
    try:
        return model.columnCount(parent)
    except TypeError:  # a list model: private in the binding
        return 1


class Move:
    """What a map item reads of the event when the mouse moves over it."""

    def __init__(self, item, point: QPointF) -> None:
        self.item, self.point = item, point

    def pos(self) -> QPointF:
        return self.point

    def scenePos(self) -> QPointF:
        return self.item.mapToScene(self.point)


class Survey:
    """The kinds of places where a text was on its way to be read as HTML, in ``found``, each with its
    first example."""

    def __init__(self) -> None:
        self.found: dict[str, str] = {}
        self.where = ""
        self.titles: set[str] = set()  # of the boxes looked at

    def note(self, kind: str, text: str) -> None:
        self.found.setdefault(kind, f"{self.where}: {text[:70]}")

    def tip(self, owner, text) -> None:
        if read_as_html(text):
            self.note(f"tooltip of {type(owner).__name__}", text)

    def label(self, label: QLabel, kind: str = "") -> None:
        text, form = label.text(), label.textFormat()
        if MARK in text and (form == Qt.TextFormat.RichText or (form == Qt.TextFormat.AutoText and read_as_html(text))):
            self.note(kind or f"label {type(label).__name__} {label.objectName()}", text)

    def items(self, view: QAbstractItemView, parent, depth: int = 0) -> None:
        model = view.model()
        for row in range(min(model.rowCount(parent), 60)):
            for column in range(min(column_count(model, parent), 12)):
                self.tip(view, model.data(model.index(row, column, parent), Qt.ItemDataRole.ToolTipRole))
            first = model.index(row, 0, parent)
            if depth < 3 and model.rowCount(first) > 0:
                self.items(view, first, depth + 1)

    def walk(self, where: str) -> None:
        self.where = where
        for widget in QApplication.allWidgets():
            self.tip(widget, widget.toolTip())
            for action in widget.actions():
                self.tip(action, action.toolTip())
            if isinstance(widget, QLabel):
                self.label(widget)
            elif isinstance(widget, QAbstractItemView) and widget.model() is not None:
                self.items(widget, widget.rootIndex())
                for section in range(min(column_count(widget.model(), widget.rootIndex()), 12)):
                    tip = widget.model().headerData(section, Qt.Orientation.Horizontal, Qt.ItemDataRole.ToolTipRole)
                    self.tip(widget, tip)
            elif isinstance(widget, QGraphicsView) and widget.scene() is not None:
                for item in widget.scene().items():
                    self.tip(item, item.toolTip())

    def hover(self, view: QGraphicsView) -> None:
        """Map items make their tooltips when the mouse moves over them: over all rows of an item,
        its title, its port."""
        for item in view.scene().items():
            if isinstance(item, TableNode | GroupNode | GroupFrame):
                rect = item.boundingRect()
                points = [
                    QPointF(rect.left() + rect.width() * x / 10, rect.top() + rect.height() * y / 40)
                    for x, y in itertools.product(range(1, 10), range(1, 40))
                ]
                if isinstance(item, TableNode):
                    points.append(item.mapFromScene(item.out_anchor()))
                for point in points:
                    item.hoverMoveEvent(Move(item, point))
                    self.tip(item, item.toolTip())


@pytest.fixture
def boxes(monkeypatch):
    """Message boxes are looked at, not shown. Returns the Survey that holds what they would read."""
    survey = Survey()

    def shown(box) -> None:
        survey.titles.add(box.windowTitle())
        for label in box.findChildren(QLabel):
            survey.label(label, f"message box “{box.windowTitle()}”")

    def static(answer):
        def call(parent, title, text, *rest):
            if read_as_html(text):
                survey.note(f"message box “{title}”", text)
            return answer

        return staticmethod(call)

    monkeypatch.setattr(QMessageBox, "exec", shown)
    monkeypatch.setattr(QMessageBox, "warning", static(QMessageBox.StandardButton.Ok))
    monkeypatch.setattr(QMessageBox, "question", static(QMessageBox.StandardButton.No))
    return survey


@pytest.fixture
def marked_window(window, session, settle, tmp_path, qtbot, boxes):
    """The window, shown (offscreen), with the project of markers open."""
    window.resize(1500, 880)
    window.show()

    def wait() -> None:
        settle(session)
        qtbot.waitUntil(lambda: not checks().busy, timeout=20_000)

    assert window.open_path(marked_project(tmp_path, session.registry))
    wait()
    return window, wait


def test_the_markers_get_to_the_window(marked_window, session):
    """The test below looks at the real thing: the names and values are on their way into the panels."""
    window, _wait = marked_window
    assert len(session.project.tables) > 40
    assert MARK in window.settings_panel.title.text() + window.data_panel.title.text() + window.windowTitle()


def test_no_panel_reads_a_name_or_a_value_as_html(marked_window, session, boxes, qtbot):
    window, wait = marked_window
    survey = boxes
    survey.walk("opened")
    # Every table and step, with their tooltips
    for table in session.project.tables:
        session.select_table(table.id)
        wait()
        survey.walk(f"table {table.id}")
        for step in table.steps:
            session.select_step(step.id)
            wait()
            survey.walk(f"table {table.id}, step {step.id}")
    # The filter pane
    panel = window.data_panel
    for table_id in ("t1", "filtered", "parquet", "excel"):
        session.select_table(table_id)
        wait()
        panel.filter_button.setChecked(True)
        for column in panel.model.frame.columns[:3]:
            panel.filter_by(column)
        wait()
        survey.walk(f"filter pane, {table_id}")
        panel.filter_button.setChecked(False)
    # The Find panel, in each of its groupings
    window.open_find()
    find = window.find_panel
    for grouping in find.group_buttons.values():
        grouping.click()
        wait()
        survey.walk(f"Find, {grouping.text()}")
    find.set_text("MARK")
    wait()
    survey.walk("Find, a search")
    window.find_dock.hide()
    # The map, also with the groups collapsed
    window.show_map(True)
    wait()
    qtbot.wait(100)
    survey.walk("map")
    canvas = window.map_view.canvas
    survey.hover(canvas)
    for group in session.project.groups:
        window.map_view.set_collapsed(group.id, not group.collapsed)
    wait()
    qtbot.wait(100)
    survey.where = "map, collapsed"
    survey.hover(canvas)
    survey.walk("map, collapsed")
    used = next(step for table in session.project.tables for step in table.steps if step.id.startswith("b"))
    session.set_enabled(used.id, False)  # the arrow of this step gets a new tooltip
    survey.walk("map, an arrow changed")
    window.show_map(False)
    wait()
    # The results of a run
    session.calculate_all()
    wait()
    window.run_dock.show()
    window.run_panel.show_results(session.results, 1.0)
    window.run_panel.tree.expandAll()
    survey.walk("run panel")
    assert not survey.found, survey.found


def test_no_dialog_reads_a_name_or_a_value_as_html(marked_window, session, boxes, qtbot):
    window, wait = marked_window
    survey = boxes
    connections = ConnectionsDialog(session, window)
    connections.show()
    for row in range(connections.list.count()):
        connections.list.setCurrentRow(row)
        survey.walk(f"connection {row}")
    connections.reject()
    parameters = ParametersDialog(session, window)
    parameters.show()
    qtbot.waitUntil(lambda: parameters.problem.text() != "", timeout=20_000)
    survey.walk("parameters")
    parameters.reject()
    # The formula window: its status line shows what the preview says
    session.select_table("formula")
    session.select_step("s16")
    wait()
    editor = window.settings_panel.form.findChildren(ExpressionEditor)[0]
    editor.open_window()
    qtbot.waitUntil(lambda: editor.window_open.status.text() != "", timeout=20_000)
    survey.walk("formula window")
    editor.window_open.reject()
    # A column of the same step with a parameter in its name: the tooltip says what the name is now
    session.select_table("filled")
    session.select_step("s19")
    wait()
    editor = window.settings_panel.form.findChildren(ExpressionEditor)[1]
    editor.open_window()
    qtbot.waitUntil(lambda: editor.window_open.column_list.topLevelItemCount() > 0, timeout=20_000)
    survey.walk("formula window, a filled name")
    editor.window_open.reject()
    assert not survey.found, survey.found


def test_no_data_sheet_reads_a_cell_as_html(window, session, boxes, qtbot, monkeypatch):
    survey = boxes
    pasted = f"{marked('head')}\tnumber\n{marked('pasted') + LONG}\t1\n"
    monkeypatch.setattr(SheetWindow, "clipboard_text", lambda self: pasted)
    window.new_sheet(True)
    sheet = window._sheets[-1]
    sheet.show()
    qtbot.waitUntil(lambda: sheet.sheet.row_count > 0)
    survey.walk("sheet")
    assert any(MARK in sheet.sheet.text(row, 0) for row in range(sheet.sheet.row_count))
    sheet.title = marked("title")
    sheet.ask_close_draft()
    sheet.ask_close_edit()
    sheet.close_now()
    problem(window, marked("problem"))
    ask(window, "Sheet", marked("question"), marked("informative"), {"cancel": ("OK", QMessageBox.ButtonRole.AcceptRole)}, "cancel")
    assert not survey.found, survey.found


def test_no_message_box_reads_a_name_as_html(marked_window, session, boxes, tmp_path, monkeypatch):
    window, _wait = marked_window
    survey = boxes
    survey.where = "boxes"
    confirm_delete_table(window, session, "t1")
    confirm_delete_table(window, session, "off")  # it names the table that uses it
    window.map_view.delete_tables(["t1", "off"])
    # A name that is taken, by the two ways to rename a table
    taken = session.project.table("off").name
    item = next(i for i in window.tables_panel._table_items() if i.text(0) == session.project.table("t1").name)
    item.setText(0, taken)
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *args, **kwargs: (taken, True)))
    window.map_view.rename_table("t1")
    # A project that can't be opened or saved says why, with names from the file. Qt looks for HTML in the
    # first line, where the path is: a file name can't hold a <, but it can hold &lt;
    broken = tmp_path / "broken&lt;2026.tafl"
    broken.write_text(json.dumps({"format": "tafl-project", "format_version": 1, "tables": [{"name": marked("broken"), "steps": 7}]}), encoding="utf-8")
    assert not window.open_path(broken)

    def no_room(*args):
        raise OSError(marked("disk"))

    monkeypatch.setattr(session, "save_project", no_room)
    assert not window._save_to(tmp_path / "saved&lt;2026.tafl")
    window.ask_save(marked("reason"))
    window.confirm_overwrite(Path(f"{MARK}-file.tafl"))
    window.confirm(marked("title"), marked("text"), "OK")
    window._show_error(marked("background"))
    stopped = {"project": marked("project"), "table": marked("table"), "number": 1, "action": marked("action")}
    app._report_stopped(window, [crash.Stopped([stopped])])
    app._excepthook(window)(ValueError, ValueError(marked("unexpected")), None)
    assert not survey.found, survey.found
    assert survey.titles >= {"Delete table", "Delete tables", "Rename table", "Open project", "Save project", f"{APP_NAME} stopped the last time"}


# --- normal text stays as it is -------------------------------------------------------------------


def reads(rich: str) -> str:
    """What a person reads of a text that Qt takes as HTML."""
    document = QTextDocument()
    document.setHtml(rich)
    return document.toPlainText()


@pytest.mark.parametrize(
    "text",
    [
        "Müller & Söhne",
        "Tom's orders",
        'Customer "Gold"',
        "a < b",
        "a > b & c < d",
        "first line\nsecond line",
        "first < line\n\nthird > line",
        "two  spaces   and three",
        "&amp; and &lt;b&gt; as they are typed",
        '<img src="x.png"> & <a href="y">a link</a>',
    ],
)
def test_normal_text_reads_as_it_is(qapp, text):
    assert GuiQt.mightBeRichText(plain_html(text)) and reads(plain_html(text)) == reads(tooltip_text(text)) == text
    label = WrappedLabel(text)
    assert label.text() == text and label.textFormat() == Qt.TextFormat.PlainText


def test_a_tag_is_no_tag_any_more():
    assert "<img" not in plain_html('<img src="x.png">') and "<a " not in tooltip_text('<a href="x">link</a>')


def test_no_text_stays_no_text():
    assert plain_html("") == "" and tooltip_text("") == ""


def tip_height(text: str) -> int:
    QToolTip.showText(QPoint(0, 0), text)
    height = next(w for w in QApplication.topLevelWidgets() if w.objectName() == "qtooltip_label").height()
    QToolTip.hideText()
    return height


def box_width(text: str) -> int:
    box = QMessageBox(QMessageBox.Icon.Warning, "Title", text)
    box.show()
    width = box.width()
    box.close()
    box.deleteLater()
    return width


def test_a_path_in_a_tooltip_stays_on_one_line(qapp):
    """Qt gives HTML the shape of a page: it would break the path at each / and -."""
    assert tip_height(tooltip_text("D:/Data/2026-01/sales-2026-01.csv")) == tip_height(tooltip_text("short"))


def test_a_long_text_wraps_in_a_box_as_a_plain_one_does(qapp, monkeypatch):
    text = "Could not open D:/some/folder/project.tafl:\n\nThe project file is damaged: " + "words and words " * 20
    widths = []

    def shown(box) -> None:
        box.show()
        widths.append(box.width())
        box.close()

    monkeypatch.setattr(QMessageBox, "exec", shown)
    message_box(None, "Title", text)
    assert widths == [box_width(text)]


def test_the_tooltip_of_a_long_list_keeps_all_its_lines(qapp):
    lines = "\n".join(f"table {number}" for number in range(30))
    assert reads(tooltip_text(lines, 30)) == lines
    assert len(reads(tooltip_text(lines)).splitlines()) == 12 and reads(tooltip_text(lines)).endswith("…")


NOTE = "\n".join(f"Line {number}: a note on the order" for number in range(1, 21))


def test_the_tooltip_of_a_cell_has_what_the_grid_leaves_out(qapp):
    words = "A long comment on a customer and more " * 80
    model = FrameModel()
    model.set_frame(pl.DataFrame({"Note": [NOTE, words, "x" * 9000]}))

    def tip(row: int) -> str:
        return reads(model.data(model.index(row, 0), Qt.ItemDataRole.ToolTipRole))

    assert tip(0) == NOTE and tip(1).replace("\n", " ") == words.strip()
    assert len(tip(2).splitlines()) == LONG_TIP_LINES and tip(2).endswith("…")


def test_the_tooltip_of_a_value_in_a_filter_has_all_its_lines(qapp):
    holder = QWidget()
    model = _Values(holder)
    model.set_values(pl.DataFrame({VALUE: [NOTE], COUNT: [1]}), 1, Filter(column="Note"), str)
    assert reads(model.data(model.index(0, 0), Qt.ItemDataRole.ToolTipRole)) == NOTE


# --- message boxes ---------------------------------------------------------------------------------


def copy_from(box) -> None:
    """Ctrl+C in the box, as it opens. The clipboard of the offscreen platform stays in this process."""
    assert QGuiApplication.platformName() == "offscreen"
    box.show()
    QTest.keyClick(box, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    box.close()


def test_ctrl_c_in_a_box_copies_the_text_as_it_reads(window, monkeypatch, tmp_path):
    monkeypatch.setattr(QMessageBox, "exec", copy_from)
    folder = tmp_path / "Müller & Söhne"
    folder.mkdir()
    broken = folder / "sales 2026.tafl"
    broken.write_text('{"format": "tafl-project", "tables": "a < b"}', encoding="utf-8")
    assert not window.open_path(broken)
    copied = QGuiApplication.clipboard().text()
    assert f"Could not open {broken}:\n\nThe project file sales 2026.tafl is damaged: " in copied and "a < b" in copied
    assert "<p" not in copied and "&amp;" not in copied and "&lt;" not in copied


def test_a_warning_has_its_title_its_text_and_ok(qapp, monkeypatch):
    seen = []
    monkeypatch.setattr(QMessageBox, "exec", lambda box: seen.append(box))
    assert message_box(None, "Title", "a < b & c\r\nnext line") is False
    box = seen[0]
    assert (box.windowTitle(), box.text()) == ("Title", "a < b & c\r\nnext line")
    assert box.icon() == QMessageBox.Icon.Warning and box.textFormat() == Qt.TextFormat.PlainText
    assert box.standardButtons() == QMessageBox.StandardButton.Ok


@pytest.mark.parametrize("key, yes", [(Qt.Key.Key_Return, True), (Qt.Key.Key_Enter, True), (Qt.Key.Key_Escape, False)])
def test_a_question_answers_as_a_key_says(qapp, monkeypatch, key, yes):
    """As QMessageBox.question does: Enter is Yes, Escape is No."""

    def press(box) -> None:
        box.show()
        QTest.keyClick(box, key)
        box.close()

    monkeypatch.setattr(QMessageBox, "exec", press)
    assert message_box(None, "Title", "Sure?", question=True) is yes


def test_a_question_nobody_answers_is_no(qapp, monkeypatch):
    seen = []
    monkeypatch.setattr(QMessageBox, "exec", lambda box: seen.append(box))
    assert message_box(None, "Title", "Sure?", question=True) is False
    assert seen[0].icon() == QMessageBox.Icon.Question and seen[0].textFormat() == Qt.TextFormat.PlainText


def test_a_box_goes_when_it_is_answered(window, monkeypatch, qapp):
    """It has the window as parent: it would stay with it for as long as the window is open."""
    monkeypatch.setattr(QMessageBox, "exec", lambda box: None)
    before = len(window.findChildren(QMessageBox))
    message_box(window, "Title", "Text")
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert len(window.findChildren(QMessageBox)) == before


def test_a_table_goes_only_after_yes(window, session, settle, demo, monkeypatch):
    window.open_path(demo)
    settle(session)
    tables = [table.id for table in session.project.tables]
    answer = QMessageBox.StandardButton.No
    monkeypatch.setattr(QMessageBox, "exec", lambda box: box.button(answer).click())
    confirm_delete_table(window, session, tables[0])
    window.map_view.delete_tables(tables[:2])
    assert [table.id for table in session.project.tables] == tables
    answer = QMessageBox.StandardButton.Yes
    confirm_delete_table(window, session, tables[0])
    assert [table.id for table in session.project.tables] == tables[1:]
    window.map_view.delete_tables(tables[1:])
    assert session.project.tables == []


def test_a_box_takes_its_informative_text_as_it_is_too(qapp):
    """Qt gives the informative text the format of the box, in either order: the boxes count on that."""
    for first, second in (("setTextFormat", "setInformativeText"), ("setInformativeText", "setTextFormat")):
        box = QMessageBox()
        arguments = {"setTextFormat": Qt.TextFormat.PlainText, "setInformativeText": marked("informative")}
        getattr(box, first)(arguments[first])
        getattr(box, second)(arguments[second])
        assert box.findChild(QLabel, "qt_msgbox_informativelabel").textFormat() == Qt.TextFormat.PlainText


# --- the rule in the source -----------------------------------------------------------------------

UI = Path(labels.__file__).parent
HELPERS = ("tooltip_text", "plain_html")  # for a tooltip: a static box takes fixed text only
BOXES = ("warning", "question", "critical", "information")  # not about: its text is the program's own


def clean(node: ast.expr | None, helpers: tuple[str, ...] = HELPERS) -> bool:
    """Whether a text can't hold a name: it is written in the code, or it passed a helper."""
    match node:
        case None | ast.Constant():
            return True
        case ast.JoinedStr(values=values):
            return all(isinstance(value, ast.Constant) for value in values)
        case ast.IfExp(body=first, orelse=second) | ast.BinOp(left=first, right=second):
            return clean(first, helpers) and clean(second, helpers)
        case ast.BoolOp(values=values):
            return all(clean(value, helpers) for value in values)
        case ast.Call(func=ast.Name(id=name)):
            return name in helpers
    return False


def asks_for_tooltip(test: ast.expr) -> bool:
    """``role == ...ToolTipRole``, alone or with more conditions."""
    return any(
        isinstance(node, ast.Compare)
        and isinstance(node.ops[0], ast.Eq)
        and isinstance(node.comparators[0], ast.Attribute)
        and node.comparators[0].attr == "ToolTipRole"
        for node in ast.walk(test)
    )


def formats(tree: ast.AST) -> set[str]:
    """What gets a text format in this part of the code."""
    return {
        ast.unparse(node.func.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "setTextFormat"
    }


def is_text(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def faults(source: str) -> list[str]:
    tree = ast.parse(source)
    found = []
    named = {id(node.value): ast.unparse(node.targets[0]) for node in ast.walk(tree) if isinstance(node, ast.Assign)}

    def fault(node: ast.AST) -> None:
        found.append(f"line {node.lineno}: {ast.unparse(node)[:100]}")

    for node in ast.walk(tree):
        match node:
            case ast.Call(func=ast.Attribute(attr="setToolTip"), args=[*_, text]) if not clean(text):
                fault(node)
            case ast.Call(func=ast.Attribute(value=ast.Name(id="QMessageBox"), attr=name), args=[_, _, text, *_]) if (
                name in BOXES and not clean(text, ())
            ):
                fault(node)
            case ast.If(test=test, body=body) if asks_for_tooltip(test):
                for part in body:
                    for inner in ast.walk(part):
                        if isinstance(inner, ast.Return) and not clean(inner.value):
                            fault(inner)
            case ast.FunctionDef():  # a box as an object: PlainText, right there
                for inner in ast.walk(node):
                    match inner:
                        case ast.Assign(targets=[target], value=ast.Call(func=ast.Name(id="QMessageBox"))):
                            if ast.unparse(target) not in formats(node):
                                fault(inner)
            case ast.ClassDef(bases=bases) if any(isinstance(base, ast.Name) and base.id == "QLabel" for base in bases):
                if "self" not in formats(node):
                    fault(node.bases[0])
            case ast.Call(func=ast.Name(id="QLabel"), args=args) if not (args and is_text(args[0])):
                if named.get(id(node)) not in formats(tree):
                    fault(node)
    return found


@pytest.mark.parametrize("file", sorted(UI.rglob("*.py")), ids=lambda file: file.name)
def test_text_on_its_way_to_html_passes_the_helper(file):
    assert faults(file.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize(
    "source",
    [
        "w.setToolTip(name)",
        'w.setToolTip(f"Table {name}")',
        "item.setToolTip(0, name)",
        'w.setToolTip("Table " + name)',
        'w.setToolTip("Table" if off else name)',
        'QMessageBox.warning(self, "Title", str(e))',
        'QMessageBox.question(self, "Title", f"Delete {name}?")',
        'QMessageBox.warning(self, "Title", plain_html(str(e)))',
        "if role == Qt.ItemDataRole.ToolTipRole:\n    return name",
        "if role == Qt.ItemDataRole.ToolTipRole and big:\n    return name if name else None",
        'def ask():\n    box = QMessageBox(icon, "Title", name)\n    box.exec()',
        "label = QLabel(name, self)",
        "self.label = QLabel(self)",
        "row.addWidget(QLabel(name, self))",
        "class Line(QLabel):\n    pass",
    ],
)
def test_the_source_test_sees_a_text_that_didn_t_pass_the_helper(source):
    assert faults(source)


@pytest.mark.parametrize(
    "source",
    [
        'w.setToolTip("Fixed")',
        'w.setToolTip("Fixed" if on else "Also fixed")',
        "w.setToolTip(tooltip_text(name))",
        "item.setToolTip(0, tooltip_text(name))",
        'QMessageBox.warning(self, "Title", "Fixed")',
        'message_box(self, "Title", str(e))',
        'QMessageBox.about(self, "Title", f"<b>{APP_NAME}</b>")',
        "if role == Qt.ItemDataRole.ToolTipRole:\n    return tooltip_text(name) or None",
        'def ask():\n    box = QMessageBox(icon, "Title", name)\n    box.setTextFormat(Qt.TextFormat.PlainText)',
        'label = QLabel("Fixed", self)',
        'row.addWidget(QLabel("Fixed", self))',
        "self.label = QLabel(self)\nself.label.setTextFormat(Qt.TextFormat.PlainText)",
        "class Line(QLabel):\n    def __init__(self):\n        self.setTextFormat(Qt.TextFormat.PlainText)",
    ],
)
def test_the_source_test_lets_a_safe_text_pass(source):
    assert faults(source) == []
