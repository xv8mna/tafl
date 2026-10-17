"""The data after the selected step, with the filter pane (instant filters) beside it."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from typing import Any

import polars as pl
from PySide6.QtCore import QItemSelectionModel, QPoint, Qt, Signal
from PySide6.QtGui import QAction, QGuiApplication, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QMenu,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTableView,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from tafl.api import Cancelled
from tafl.core import Status

from . import theme
from .filter_pane import FilterPane
from .filters import Filter, apply, to_json
from .frame_model import FrameModel
from .labels import ElidedLabel, WrappedLabel
from .session import Session, count, first_error, missing_settings, size_text, waiting_text
from .worker import Worker

MAX_COPY_CELLS = 2_000_000  # more is a case for an export
_COPY_AT_ONCE = 20_000  # more cells are copied in the background

Ranges = list[tuple[int, int, int, int]]  # selected cells: (top, bottom, left, right), inclusive


class DataPanel(QWidget):
    status_message = Signal(str)  # for the status bar

    def __init__(self, session: Session, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        # Texts of any length above the grid (a summary, a path): they end with "…" when there is
        # not enough room, and never make the window wider. The summary gives way first.
        self.title = ElidedLabel(self, widest=320, narrowest=100)
        self.title.setObjectName("Title")
        self.subtitle = ElidedLabel(self)
        self.subtitle.setObjectName("Dim")
        self.subtitle.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.info = ElidedLabel(self, widest=420)
        self.info.setObjectName("Dim")
        self.busy = ElidedLabel(self)
        self.busy.setText("Calculating…")
        self.busy.setStyleSheet(f"color: {theme.ACCENT_TEXT};")
        self.busy.hide()
        self.filter_pane = FilterPane(session, self)
        self.filter_pane.hide()
        self.filter_button = QToolButton(self)
        self.filter_button.setText("Filters")
        self.filter_button.setCheckable(True)
        self.filter_button.setToolTip(
            "Filter pane: see only the rows with the values you pick.\n"
            "It changes only what you see, and stays while you click through the steps."
        )
        self.filter_button.toggled.connect(self.filter_pane.setVisible)

        self.model = FrameModel(self)
        self.view = QTableView(self)
        self.view.setModel(self.model)
        self.view.setAlternatingRowColors(True)
        self.view.setWordWrap(False)
        self.view.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.view.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._cell_menu)
        vertical = self.view.verticalHeader()
        vertical.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        vertical.setDefaultSectionSize(self.fontMetrics().height() + 6)
        horizontal = self.view.horizontalHeader()
        horizontal.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        horizontal.setMinimumHeight(2 * self.fontMetrics().height() + 10)
        horizontal.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        horizontal.customContextMenuRequested.connect(self._header_menu)
        # The headers get a selection of their own (always empty): to show the selected columns,
        # a header asks the model about every row, which takes seconds after Ctrl+A on big data.
        for header in (horizontal, vertical):
            header.setSelectionModel(QItemSelectionModel(self.model, header))
        self.copy_action = QAction("Copy", self.view)
        self.copy_action.setShortcut(QKeySequence.StandardKey.Copy)
        self.copy_action.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        self.copy_action.triggered.connect(self.copy_selection)
        self.view.addAction(self.copy_action)
        self._copier = Worker(self)
        self._copier.finished.connect(self._copied)
        self._copier.failed.connect(lambda text: self.status_message.emit("Copying failed: " + text.strip().splitlines()[-1]))

        self.message = WrappedLabel(parent=self, narrowest=200)
        self.message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.message.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.message.setMargin(40)
        self.stack = QStackedWidget(self)
        self.stack.addWidget(self.view)
        self.stack.addWidget(self.message)
        self.body = QSplitter(Qt.Orientation.Horizontal, self)
        self.body.addWidget(self.filter_pane)
        self.body.addWidget(self.stack)
        self.body.setStretchFactor(1, 1)
        self.body.setCollapsible(1, False)
        self.body.setSizes([290, 800])

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.addWidget(self.title)
        header.addSpacing(10)
        header.addWidget(self.subtitle, 1)
        header.addWidget(self.busy)
        header.addSpacing(10)
        header.addWidget(self.info)
        header.addSpacing(6)
        header.addWidget(self.filter_button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(3, 6, 3, 6)
        layout.setSpacing(4)
        layout.addLayout(header)
        layout.addWidget(self.body, 1)

        self._source: pl.DataFrame | None = None  # the data the grid shows (before filtering) ...
        self._source_filters = ""  # ... and the filters it was filtered with
        self._fitted: pl.DataFrame | None = None  # the data the column widths were fitted to
        session.selection_changed.connect(self.refresh)
        session.results_changed.connect(self.refresh)
        session.structure_changed.connect(self.refresh)
        session.filters_changed.connect(lambda _table_id: self.refresh())
        session.busy_changed.connect(self.busy.setVisible)
        self.refresh()

    def refresh(self) -> None:
        session = self.session
        table, step = session.table, session.step
        self.title.setText(table.name if table else "No table")
        active = sum(1 for f in session.filters(table.id) if f.get("values")) if table else 0
        self.filter_button.setText(f"Filters ({active})" if active else "Filters")
        if table is None:
            self._show_message("Open a project (Ctrl+O), or create a table with “+ New table”.")
            self.subtitle.setText("")
            self.filter_pane.show_data(None, None)
            return
        if step is None:
            self._no_data(table.id, "This table has no steps yet. Add an import with “+ Add step”.")
            self.subtitle.setText("")
            return
        number = table.steps.index(step) + 1
        self.subtitle.setText(f"after step {number}: {session.describe(step)}")
        if not table.enabled:
            self._no_data(
                table.id,
                f"“{table.name}” is turned off: it is not calculated, and its exports don't run.\n\n"
                "Turn it on in its right-click menu (in the table list or on the map).",
            )
            return
        if session.result is not None and session.result.skipped:
            self._no_data(table.id, f"“{table.name}” is not calculated: it {session.result.skipped}.")
            return
        result = session.step_result(step)
        if result is None:
            if self.model.frame is None or not session.busy:  # while calculating, the old data stays
                self._no_data(table.id, "Calculating…" if session.busy else "Not calculated yet.")
            return
        needed = missing_settings(result)
        failed = first_error(session.result) if result.status == Status.BLOCKED else None
        if failed is not None and missing_settings(failed[1]):
            self._no_data(table.id, f"Step {failed[0]} is not complete yet: the steps after it are calculated when it is.")
            return
        if result.status in (Status.ERROR, Status.BLOCKED) and (waiting := session.waits_for(table.id)) is not None:
            first = first_error(session.result)
            text = f"Step {first[0] if first else number} {waiting_text(waiting)}"
            if result.status == Status.BLOCKED:
                text += ": the steps after it are calculated when it is"
            self._no_data(table.id, text + ".")
            return
        if result.status == Status.ERROR and not needed:
            self._no_data(table.id, f"Step {number} has an error:\n\n{result.error}", error=True)
            return
        if result.status == Status.BLOCKED:
            self._no_data(table.id, "An earlier step has an error. Click the red step to see it.", error=True)
            return
        df = session.input_frame(step) if needed else result.df
        if needed and df is None:
            self._no_data(table.id, f"Step {number} is not complete yet: fill in {', '.join(needed)}.")
            return
        if needed:  # the data before the step, until it is filled in
            self.subtitle.setText(f"going into step {number}: {session.describe(step)}")
        if df is None:
            self._no_data(table.id, "No data.")
            return
        self.filter_pane.show_data(table.id, df)
        shown = self._filtered(table.id, df)
        if shown is not self.model.frame:
            self._show_frame(df, shown)
        self.stack.setCurrentWidget(self.view)
        timing = "cached" if result.cached else f"{result.seconds:.2f} s"
        # Kept short: the settings beside the grid say what is missing (and the tooltip does)
        self.info.set_tooltip(f"Step {number} is not complete yet: fill in {', '.join(needed)}" if needed else "")
        if needed:
            timing = f"step {number} is not complete yet"
        elif result.status == Status.SKIPPED and result.skip_rest:
            timing = f"skipped: {result.skip_reason}"
        elif result.status == Status.SKIPPED:
            timing = "runs when the project is run"
        elif result.status == Status.DISABLED:
            timing = "step is off"
        if shown is df:
            self.info.setText(f"{size_text(df)}   ·   {timing}")
            self.info.setStyleSheet("")
        else:
            self.info.setText(
                f"{shown.height:,} of {count(df.height, 'row')} × {count(df.width, 'column')}  ·  filtered   ·   {timing}"
            )
            self.info.setStyleSheet(f"color: {theme.ACCENT_TEXT};")

    def _filtered(self, table_id: str, df: pl.DataFrame) -> pl.DataFrame:
        """The rows that pass the table's instant filters (the same frame again if nothing changed)."""
        filters = self.session.filters(table_id)
        key = json.dumps(filters, default=str)
        if df is self._source and key == self._source_filters and self.model.frame is not None:
            return self.model.frame
        self._source, self._source_filters = df, key
        return apply(df, [Filter.from_dict(f) for f in filters])

    def _show_frame(self, df: pl.DataFrame, shown: pl.DataFrame) -> None:
        """Show (filtered) data; column widths stay when only the filters changed."""
        old = self.model.frame
        same_columns = old is not None and old.columns == shown.columns and df is self._fitted
        widths = [self.view.columnWidth(c) for c in range(len(shown.columns))] if same_columns else []
        self.model.set_frame(shown)
        if same_columns:
            for column, width in enumerate(widths):
                self.view.setColumnWidth(column, width)
        else:
            self._fit_columns()
            self._fitted = df

    def _no_data(self, table_id: str, text: str, error: bool = False) -> None:
        self._show_message(text, error)
        self.filter_pane.show_data(table_id, None)

    def _show_message(self, text: str, error: bool = False) -> None:
        self.message.setText(text)
        self.message.setStyleSheet(f"color: {theme.ERROR_TEXT if error else theme.TEXT_DIM};")
        self.stack.setCurrentWidget(self.message)
        self.info.setText("")
        self.info.set_tooltip("")
        self.info.setStyleSheet("")

    def _fit_columns(self) -> None:
        df = self.model.frame
        if df is None:
            return
        cell_metrics = self.view.fontMetrics()
        header_metrics = self.view.horizontalHeader().fontMetrics()
        sample = min(df.height, 200)
        for column in range(df.width):
            header = self.model.headerData(column, Qt.Orientation.Horizontal) or ""
            width = max(header_metrics.horizontalAdvance(line) for line in header.split("\n")) + 24
            for row in range(sample):
                width = max(width, cell_metrics.horizontalAdvance(self.model.text(row, column)) + 16)
            self.view.setColumnWidth(column, min(width, 320))

    # --- filtering from the grid -------------------------------------------------------------------

    def filter_by(self, column: str, values: list[Any] | None = None) -> None:
        """A filter for ``column`` in the filter pane (with ``values`` applied)."""
        if self.session.table is None:
            return
        self.filter_button.setChecked(True)
        self.filter_pane.add_filter(column, values)

    def _cell_menu(self, pos: QPoint) -> None:
        menu = QMenu(self)
        menu.addAction(self.copy_action)
        index = self.view.indexAt(pos)
        frame = self.model.frame
        if index.isValid() and frame is not None:
            column = frame.columns[index.column()]
            value = self.model.value(index.row(), index.column())
            text = "(empty)" if value is None else self.model.text(index.row(), index.column())
            text = text if len(text) <= 40 else text[:40] + "…"
            menu.addSeparator()
            menu.addAction(f"Show only rows where {column} is “{text}”", lambda: self.filter_by(column, [to_json(value)]))
            menu.addAction(f"Filter by {column}", lambda: self.filter_by(column))
        menu.exec(self.view.viewport().mapToGlobal(pos))

    def _header_menu(self, pos: QPoint) -> None:
        header = self.view.horizontalHeader()
        section = header.logicalIndexAt(pos)
        frame = self.model.frame
        if section < 0 or frame is None:
            return
        column = frame.columns[section]
        menu = QMenu(self)
        menu.addAction(f"Filter by {column}", lambda: self.filter_by(column))
        menu.exec(header.mapToGlobal(pos))

    def copy_selection(self) -> None:
        """Copy the selected cells as tab-separated text (pastes into Excel). Many cells are
        copied in the background; too many are refused with a message."""
        frame, texts = self.model.frame, self.model.copier()
        ranges = [(r.top(), r.bottom(), r.left(), r.right()) for r in self.view.selectionModel().selection()]
        if frame is None or texts is None or not ranges:
            return
        cells = sum((bottom - top + 1) * (right - left + 1) for top, bottom, left, right in ranges)
        if cells > MAX_COPY_CELLS:
            self.status_message.emit(
                f"{cells:,} cells are too many to copy (at most {MAX_COPY_CELLS:,}): select fewer, or export the table"
            )
            return
        if cells <= _COPY_AT_ONCE:
            QGuiApplication.clipboard().setText(selection_text(frame, ranges, texts))
            return
        self.status_message.emit(f"Copying {cells:,} cells…")
        self._copier.start(lambda cancel: (selection_text(frame, ranges, texts, cancel), cells))

    @property
    def copying(self) -> bool:
        return self._copier.busy

    def _copied(self, outcome: tuple[str, int]) -> None:
        text, cells = outcome
        QGuiApplication.clipboard().setText(text)
        self.status_message.emit(f"Copied {cells:,} cells")


def selection_text(
    frame: pl.DataFrame, ranges: Ranges, texts: Callable[[pl.Series], list[str]], cancel: threading.Event | None = None
) -> str:
    """Selected cells as lines of tab-separated texts: the rows and columns that have a selected
    cell, in order, with the cells between that aren't selected left empty."""
    if len(ranges) == 1:
        top, bottom, left, right = ranges[0]
        rows = None
        block = frame.slice(top, bottom - top + 1)
        columns = list(range(left, right + 1))
    else:
        rows = sorted(set().union(*(range(top, bottom + 1) for top, bottom, _, _ in ranges)))
        block = frame.select(pl.all().gather(rows))
        columns = sorted(set().union(*(range(left, right + 1) for _, _, left, right in ranges)))
    cells = []
    for column in columns:
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        shown = texts(block.to_series(column))
        if rows is not None:
            spans = [(top, bottom) for top, bottom, left, right in ranges if left <= column <= right]
            shown = [text if any(top <= row <= bottom for top, bottom in spans) else "" for row, text in zip(rows, shown)]
        cells.append(shown)
    return "\r\n".join(map("\t".join, zip(*cells)))
