"""Long texts (a summary, a hint, a skip reason with a path) never make the window wider than
it is, the window can be narrow, and texts in status colors are easy to read."""

import pytest
from PySide6.QtWidgets import QApplication, QLabel, QWidget

from tafl.core import Connection, Project, Status, StepResult, TableResult
from tafl.ui import theme
from tafl.ui.connections_dialog import ConnectionsDialog
from tafl.ui.labels import ElidedLabel, WrappedLabel, tooltip_text


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
    assert label.toolTip() == tooltip_text(text) and max(map(len, label.toolTip().splitlines())) <= 100
    label.setText("short")
    assert label.shown_text() == "short" and label.toolTip() == ""
    label.set_tooltip("What is missing")
    assert label.toolTip() == "What is missing"
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
