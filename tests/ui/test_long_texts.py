"""Long texts (a summary, a hint, a skip reason with a path) never make the window wider than
it is, the window can be narrow, and texts in status colors are easy to read."""

import types

import pytest
from PySide6.QtGui import QTextDocument
from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget

from tafl.api import ActionError, action
from tafl.core import Connection, Project, Registry, Status, StepResult, TableResult
from tafl.ui import theme
from tafl.ui.connections_dialog import ConnectionsDialog
from tafl.ui.labels import LONG_TIP_LINES, ElidedLabel, MessageBox, WrappedLabel, tooltip_text
from tafl.ui.main_window import MainWindow
from tafl.ui.session import Session

HUGE = ("huge error text " * 4500)[:70_000]


@action("test.huge_error", "Fail with a huge error")
def _huge_error(df, p, ctx):
    raise ActionError(HUGE)


@pytest.fixture(scope="module")
def failing_registry():
    registry = Registry()
    registry.load_builtin()
    module = types.ModuleType("failing")
    module.huge = _huge_error
    registry.add_module(module)
    return registry


def contrast(a: str, b: str) -> float:
    def luminance(color: str) -> float:
        channels = [int(color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
        r, g, b_ = (c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels)
        return 0.2126 * r + 0.7152 * g + 0.0722 * b_

    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def layout_done(window, qtbot) -> None:
    for _ in range(3):
        QApplication.processEvents()
        qtbot.wait(5)


def test_a_one_line_label_ends_with_dots_and_never_asks_for_more_room(qtbot):
    host = QWidget()
    qtbot.addWidget(host)
    label = ElidedLabel(host, widest=200)
    text = "skipped: no files named *.csv in " + "C:\\inbox\\" * 40
    label.setText(text)
    assert label.minimumSizeHint().width() == 0 and label.sizeHint().width() == 200
    label.resize(150, 20)
    assert label.text() == text and label.shown_text().endswith("…") and len(label.shown_text()) < len(text)
    document = QTextDocument()
    document.setHtml(label.toolTip())
    assert label.toolTip() == tooltip_text(text) and max(map(len, document.toPlainText().splitlines())) <= 100
    label.setText("short")
    assert label.shown_text() == "short" and label.toolTip() == ""
    label.set_tooltip("What is missing")
    assert label.toolTip() == tooltip_text("What is missing")
    name = ElidedLabel(host, narrowest=100)  # keeps a short name whole, and 100 pixels of a long one
    name.setText("Orders")
    assert 0 < name.minimumSizeHint().width() == name.sizeHint().width() < 100
    name.setText("Orders of all customers in all countries " * 3)
    assert name.minimumSizeHint().width() == 100
    huge = ElidedLabel(host)
    huge.setText("x" * 1_000_000)  # a summary with thousands of values: it works with the first part
    assert huge.sizeHint().width() < huge.fontMetrics().horizontalAdvance("x" * 1100)
    wrapped = WrappedLabel("A path: " + "x" * 300, host)
    assert wrapped.minimumSizeHint().width() <= 120


def open_demo(window, session, settle, demo, qtbot, width=1100):
    window.resize(width, 800)
    window.show()
    window.open_path(demo)
    orders = session.project.table("Orders")
    session.select_table(orders.id)
    session.select_step(orders.steps[-1].id)
    settle(session)
    layout_done(window, qtbot)
    return orders


@pytest.mark.parametrize("action", ["table.merge", "calc.columns", "rows.filter"])
def test_a_new_step_doesnt_make_the_window_wider(window, session, settle, demo, qtbot, action):
    """The hint "step 11 is not complete yet: fill in …" made the window wider than the screen
    and squeezed the settings."""
    open_demo(window, session, settle, demo, qtbot)
    settings = window.splitter.sizes()[2]
    session.add_step(session.registry.actions[action])
    settle(session)
    layout_done(window, qtbot)
    assert session.table_status(session.table_id) == "incomplete"
    assert window.width() == 1100 and window.minimumSizeHint().width() <= 1100
    assert window.splitter.sizes()[2] == settings
    session.undo()
    settle(session)
    layout_done(window, qtbot)
    assert window.width() == 1100


def test_a_skip_reason_with_a_long_path_doesnt_make_the_window_wider(window, session, settle, tmp_path, qtbot):
    inbox = tmp_path / ("inbox_" + "a" * 60) / ("b" * 50)
    inbox.mkdir(parents=True)
    project = Project(name="Test")
    table = project.add_table("Inbox")
    project.add_step(table, "csv.import", {"source": "folder", "folder": str(inbox), "pattern": "*.csv", "no_files": "skip"})
    project.add_step(table, "rows.sort", {"by": [{"column": "File", "order": "asc"}]})
    path = project.save(tmp_path / "skip.tafl")
    window.resize(1100, 800)
    window.show()
    window.open_path(path)
    session.select_table(session.project.tables[0].id)
    settle(session)
    layout_done(window, qtbot)
    panel = window.data_panel
    assert session.step_result(session.step).status == Status.SKIPPED
    assert str(inbox) in panel.info.text() and panel.info.shown_text().endswith("…")
    assert str(inbox) in panel.info.toolTip().replace("\n", "")
    assert window.width() == 1100 and window.minimumSizeHint().width() <= 1100
    assert window.settings_panel.minimumSizeHint().width() < 300  # the reason is in a box there too


def test_a_huge_error_scrolls_in_its_box_and_the_data_message_ends_with_dots(qapp, qtbot, failing_registry, settle, tmp_path):
    """Up to 0.6.4 an error of 70,000 characters made its box 663 pixels high and left 68 pixels to
    the step's settings; the message over the data was cut at the bottom, without "…"."""
    (tmp_path / "d.csv").write_text("A;B\n1;2\n", encoding="utf-8")
    project = Project(name="Huge")
    table = project.add_table("Huge error")
    project.add_step(table, "csv.import", {"file": "d.csv"})
    project.add_step(table, "test.huge_error", {})
    path = project.save(tmp_path / "huge.tafl")
    qapp.setOrganizationName("Tafl Tests")
    session = Session(failing_registry)
    window = MainWindow(session)
    try:
        window.resize(1500, 900)
        window.show()
        window.open_path(path)
        session.select_step(session.project.tables[0].steps[1].id)
        settle(session)
        layout_done(window, qtbot)
        box, settings = window.settings_panel.error, window.settings_panel
        assert box.isVisible() and box.text() == HUGE
        assert box.view.height() == 8 * box.view.fontMetrics().lineSpacing() + 1  # 8 lines, then it scrolls
        assert box.view.verticalScrollBar().maximum() > 0
        assert settings.scroll.height() > settings.height() // 2  # the room of the step's settings
        box.view.selectAll()  # all of the error can be selected, and so copied
        assert box.view.textCursor().selectedText() == HUGE
        message = window.data_panel.message
        assert message.text() == f"Step 2 has an error:\n\n{HUGE}"
        assert message.shown_text().endswith("…") and message.heightForWidth(message.width()) <= message.height()
        assert message.toolTip() == tooltip_text(message.text(), LONG_TIP_LINES)
        assert window.size().toTuple() == (1500, 900)
    finally:
        session._set_dirty(False)
        window.close()
        window.deleteLater()


def test_a_long_path_in_a_box_shows_whole(window, session, settle, tmp_path, qtbot):
    """Up to 0.6.4 the box of a skip reason cut a long path at its right edge, without "…" or a
    tooltip: now the path wraps where it must, and every line fits."""
    inbox = tmp_path / ("inbox_for_the_daily_reports_" + "x" * 90)
    inbox.mkdir()
    project = Project(name="Test")
    table = project.add_table("Inbox")
    project.add_step(table, "csv.import", {"source": "folder", "folder": str(inbox), "pattern": "*.csv", "no_files": "skip"})
    project.add_step(table, "rows.sort", {"by": [{"column": "File", "order": "asc"}]})
    path = project.save(tmp_path / "skip.tafl")
    window.resize(1100, 800)
    window.show()
    window.open_path(path)
    session.select_step(session.project.tables[0].steps[0].id)
    settle(session)
    layout_done(window, qtbot)
    info = window.settings_panel.info
    assert info.isVisible() and str(inbox) in info.text() and info.view.toPlainText() == info.text()
    view, widths = info.view, []
    block = view.document().firstBlock()
    while block.isValid():
        widths += [block.layout().lineAt(i).naturalTextWidth() for i in range(block.layout().lineCount())]
        block = block.next()
    assert len(widths) > 3 and max(widths) <= view.viewport().width()
    assert window.width() == 1100


def test_a_message_box_lays_out_its_text_at_its_own_width(qtbot):
    """Also when it gets its text before it is shown, at another width: its text could keep the
    lines of a width with a scroll bar, without the scroll bar, so that the last line was out of
    sight. Many widths and lengths: which of them did so depends on the font."""
    for width in range(180, 420, 40):
        for count in range(60, 260, 40):
            host = QWidget()
            qtbot.addWidget(host)
            layout = QVBoxLayout(host)
            box = MessageBox("InfoBox", host)
            box.setText("The steps after this one are skipped: no files named *.csv in D:\\" + "x" * count + ".")
            layout.addWidget(box)
            layout.addStretch(1)
            host.resize(width, 600)
            host.show()
            QApplication.processEvents()
            view, widths = box.view, []
            block = view.document().firstBlock()
            while block.isValid():
                widths += [block.layout().lineAt(i).naturalTextWidth() for i in range(block.layout().lineCount())]
                block = block.next()
            assert max(widths) <= view.viewport().width(), (width, count)
            if not view.verticalScrollBar().isVisible():  # then all the lines show
                assert view.height() == len(widths) * view.fontMetrics().lineSpacing() + 1, (width, count)
            host.close()


def test_a_wrapped_label_ends_what_doesnt_fit_with_dots(qtbot):
    """A word too long for the label (a path) ends with "…" instead of being cut at the edge; a
    label that fills a page ends its last line with "…" when the text needs more lines. The
    tooltip then has the text."""
    host = QWidget()
    qtbot.addWidget(host)
    host.resize(400, 400)
    host.show()  # (a hidden label gets its resize event when it is shown)
    text = "No files named *.csv in C:\\inbox_for_the_daily_reports_of_the_northern_warehouse_" + "x" * 100
    label = WrappedLabel(text, host)
    label.show()
    label.resize(200, 200)
    shown, width = label.shown_text().splitlines(), label.contentsRect().width()
    assert label.text() == text and len(shown) > 1 and text.startswith(shown[0]) and shown[-1].endswith("…")
    assert all(label.fontMetrics().horizontalAdvance(line) <= width for line in shown)
    assert label.toolTip() == tooltip_text(text, LONG_TIP_LINES)
    label.setText("A short text")
    assert label.shown_text() == "A short text" and label.toolTip() == ""
    lines = "\n".join(f"line {n}" for n in range(100))
    page = WrappedLabel(lines, host, fill=True)
    page.show()
    page.resize(300, 10 * page.fontMetrics().lineSpacing())
    shown = page.shown_text().splitlines()
    assert shown[0] == "line 0" and len(shown) <= 10 and shown[-1].endswith("…")
    assert page.text() == lines and page.toolTip() == tooltip_text(lines, LONG_TIP_LINES)
    page.resize(300, 101 * page.fontMetrics().lineSpacing())  # room for all of it again
    assert page.shown_text() == lines and page.toolTip() == ""


def test_a_summary_with_thousands_of_values_is_cut_short(window, session, settle, demo, qtbot):
    """A filter added from the filter pane with thousands of values has a summary of millions
    of pixels: the window became that wide."""
    open_demo(window, session, settle, demo, qtbot)
    condition = "[Name] IN (" + ", ".join(f"'customer {n}'" for n in range(3000)) + ")"
    step = session.add_step(session.registry.actions["rows.filter_formula"], params={"condition": condition, "keep": "keep"})
    settle(session)
    layout_done(window, qtbot)
    summary = session.describe(step)
    assert len(summary) <= 300 and summary.endswith("…") and summary.startswith("Keep rows where [Name] IN (")
    assert window.data_panel.subtitle.text() == f"after step 11: {summary}"
    assert window.width() == 1100 and window.minimumSizeHint().width() <= 1100


def test_the_window_can_be_narrow(window, session, settle, demo, qtbot):
    """It could not be narrower than about 1000 pixels (the map's header): too wide for a
    small screen."""
    open_demo(window, session, settle, demo, qtbot)
    for on_map in (False, True):
        window.show_map(on_map)
        layout_done(window, qtbot)
        assert window.minimumSizeHint().width() <= 800
    hint = next(label for label in window.map_view.findChildren(QLabel) if label.text().startswith("Drag a table"))
    assert hint.minimumSizeHint().width() == 0
    assert window.map_view.search.minimumWidth() <= 150


def test_a_small_first_window_leaves_most_room_to_the_data(window, demo):
    window.resize(1100, 600)
    window.show()
    window.open_path(demo)
    QApplication.processEvents()
    window.splitter.setSizes([280, 800, 420])  # as made for 1500 pixels: the data gets what is left
    window.fit_panels(window.splitter.width())
    left, data, settings = window.splitter.sizes()
    assert data > left and data > settings >= 260
    window.show_map(True)
    QApplication.processEvents()
    board, right = window.map_view.splitter.sizes()
    assert board > right >= 260


def test_texts_in_status_colors_are_easy_to_read():
    for background in (theme.WINDOW, theme.BASE, theme.ALTERNATE):
        for status, color in theme.STATUS_TEXT.items():
            assert contrast(color, background) >= 4.5, status
        assert contrast(theme.ACCENT_TEXT, background) >= 4.5


def test_run_results_use_the_text_colors(window, session):
    table = session.project.add_table("Orders")
    step = session.project.add_step(table, "csv.import", {"file": "orders.csv"})
    off = session.project.add_table("Off")
    result = StepResult(step.id, Status.OK, messages=[("info", "Encoding utf-8")])
    window.run_panel.show_results({table.id: TableResult(table.id, [result]), off.id: TableResult(off.id, skipped="turned off")})
    panel = window.run_panel
    assert theme.STATUS_TEXT["ok"] in panel.summary.styleSheet()
    top = panel.tree.topLevelItem(0)
    assert top.child(0).child(0).foreground(0).color().name() == theme.STATUS_TEXT["skipped"]
    assert panel.tree.topLevelItem(1).foreground(1).color().name() == theme.STATUS_TEXT["skipped"]
    error = StepResult(step.id, Status.ERROR, error="File not found")
    panel.show_results({table.id: TableResult(table.id, [error])})
    assert theme.STATUS_TEXT["error"] in panel.summary.styleSheet()


def test_the_filtered_info_is_easy_to_read(window, session, settle, demo, qtbot):
    orders = open_demo(window, session, settle, demo, qtbot)
    session.set_filters(orders.id, [{"column": "City", "values": ["Köln"]}])
    settle(session)
    assert "filtered" in window.data_panel.info.text()
    assert theme.ACCENT_TEXT in window.data_panel.info.styleSheet()


def test_the_connection_test_shows_whole_lines(session, qtbot):
    session.project.connections = [Connection("shop", "sqlite", {"file": "shop.db"})]
    dialog = ConnectionsDialog(session)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog._tested((False, "\n".join(f"line {n}" for n in range(1, 9))))
    box, line = dialog.test_result, dialog.test_result.fontMetrics().lineSpacing()
    for scrolled in (0, 1, 3):  # five whole lines, none cut in half, also when scrolled
        box.verticalScrollBar().setValue(scrolled)
        top = box.blockBoundingGeometry(box.firstVisibleBlock()).translated(box.contentOffset()).top()
        assert top == 0 and box.viewport().height() == 5 * line
    assert box.styleSheet() == ""  # the colors come from the palette: no black scroll bar groove
    assert box.palette().color(box.palette().ColorRole.Text).name() == theme.STATUS_TEXT["error"]
