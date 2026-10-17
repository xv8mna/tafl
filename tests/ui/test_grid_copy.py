"""Copying cells from the data grid (Ctrl+C): fast with big data, never freezing the window."""

import time

import polars as pl
from PySide6.QtCore import QItemSelection, QItemSelectionModel
from PySide6.QtWidgets import QApplication

from tafl.ui import data_panel
from tafl.ui.data_panel import DataPanel


def grid(session, df: pl.DataFrame) -> DataPanel:
    panel = DataPanel(session)
    panel.resize(1000, 700)
    panel.model.set_frame(df)
    panel.stack.setCurrentWidget(panel.view)
    panel.show()
    QApplication.processEvents()
    return panel


def select(panel: DataPanel, *ranges: tuple[int, int, int, int]) -> None:
    """Select cells: (top, bottom, left, right) each, inclusive."""
    selection = QItemSelection()
    for top, bottom, left, right in ranges:
        selection.select(panel.model.index(top, left), panel.model.index(bottom, right))
    panel.view.selectionModel().select(selection, QItemSelectionModel.SelectionFlag.ClearAndSelect)


def test_copy_cells_as_excel_pastes_them(session, qtbot):
    df = pl.DataFrame({"Name": ["Anna", "Ben\tBerlin", 'Say "hi"\nthere', None], "Net": [1.5, 2.0, None, 4.25], "No": [1, 2, 3, 4]})
    panel = grid(session, df)
    qtbot.addWidget(panel)
    select(panel, (0, 1, 0, 1))
    panel.copy_selection()
    decimal = panel.model.display.decimal
    assert QApplication.clipboard().text() == f'Anna\t1{decimal}5\r\n"Ben\tBerlin"\t2'
    select(panel, (1, 2, 0, 0), (3, 3, 2, 2))  # Ctrl+click: the cells between stay empty
    panel.copy_selection()
    assert QApplication.clipboard().text() == '"Ben\tBerlin"\t\r\n"Say ""hi""\nthere"\t\r\n\t4'


def test_select_all_and_copy_a_big_grid(session, qtbot):
    """300,000 × 4 cells: Ctrl+A took 3.5 s and Ctrl+C 5 s, and copied nothing."""
    rows = 300_000
    df = pl.DataFrame({"A": range(rows), "B": [f"Customer {i % 977}" for i in range(rows)], "C": [i * 0.5 for i in range(rows)]})
    df = df.with_columns(D=pl.lit("x"))
    panel = grid(session, df)
    qtbot.addWidget(panel)
    messages = []
    panel.status_message.connect(messages.append)
    started = time.perf_counter()
    panel.view.selectAll()
    QApplication.processEvents()  # drawing the selection
    panel.copy_selection()  # in the background
    assert time.perf_counter() - started < 1.5
    assert panel.copying and messages == ["Copying 1,200,000 cells…"]
    qtbot.waitUntil(lambda: not panel.copying, timeout=20_000)
    text = QApplication.clipboard().text()
    lines = text.split("\r\n")
    assert len(lines) == rows and lines[1] == f"1\tCustomer 1\t0{panel.model.display.decimal}5\tx"
    assert messages[-1] == "Copied 1,200,000 cells"


def test_too_many_cells_are_refused_with_a_message(session, qtbot, monkeypatch):
    monkeypatch.setattr(data_panel, "MAX_COPY_CELLS", 10)
    panel = grid(session, pl.DataFrame({"A": range(20)}))
    qtbot.addWidget(panel)
    messages = []
    panel.status_message.connect(messages.append)
    QApplication.clipboard().setText("before")
    panel.view.selectAll()
    panel.copy_selection()
    assert QApplication.clipboard().text() == "before"
    assert messages == ["20 cells are too many to copy (at most 10): select fewer, or export the table"]


def test_the_window_shows_the_message(window, session):
    window.data_panel.status_message.emit("Copied 3 cells")
    assert window.statusBar().currentMessage() == "Copied 3 cells"
