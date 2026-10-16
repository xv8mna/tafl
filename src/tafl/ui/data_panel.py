"""The data after the selected step, with the filter pane (instant filters) beside it."""

from __future__ import annotations

import json
from typing import Any

import polars as pl
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QAction, QGuiApplication, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QSplitter,
    QStackedWidget,
    QTableView,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from tafl.core import Status

from . import theme
from .filter_pane import FilterPane
from .filters import Filter, apply, to_json
from .frame_model import FrameModel
from .session import Session

_MAX_COPY_CELLS = 200_000


class DataPanel(QWidget):
    def __init__(self, session: Session, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self.title = QLabel(self)
        self.title.setObjectName("Title")
        self.subtitle = QLabel(self)
        self.subtitle.setObjectName("Dim")
        self.info = QLabel(self)
        self.info.setObjectName("Dim")
        self.busy = QLabel("Calculating…", self)
        self.busy.setStyleSheet(f"color: {theme.ACCENT};")
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
        self.copy_action = QAction("Copy", self.view)
        self.copy_action.setShortcut(QKeySequence.StandardKey.Copy)
        self.copy_action.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        self.copy_action.triggered.connect(self.copy_selection)
        self.view.addAction(self.copy_action)

        self.message = QLabel(self)
        self.message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.message.setWordWrap(True)
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
        self.subtitle.setText(f"after step {number}: {session.engine.describe(step)}")
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
        if result.status == Status.ERROR:
            self._no_data(table.id, f"Step {number} has an error:\n\n{result.error}", error=True)
            return
        if result.status == Status.BLOCKED:
            self._no_data(table.id, "An earlier step has an error. Click the red step to see it.", error=True)
            return
        df = result.df
        if df is None:
            self._no_data(table.id, "No data.")
            return
        self.filter_pane.show_data(table.id, df)
        shown = self._filtered(table.id, df)
        if shown is not self.model.frame:
            self._show_frame(df, shown)
        self.stack.setCurrentWidget(self.view)
        timing = "cached" if result.cached else f"{result.seconds:.2f} s"
        if result.status == Status.SKIPPED:
            timing = "runs when the project is run"
        elif result.status == Status.DISABLED:
            timing = "step is off"
        if shown is df:
            self.info.setText(f"{df.height:,} rows × {df.width} columns   ·   {timing}")
            self.info.setStyleSheet("")
        else:
            self.info.setText(f"{shown.height:,} of {df.height:,} rows × {df.width} columns  ·  filtered   ·   {timing}")
            self.info.setStyleSheet(f"color: {theme.ACCENT};")

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
        """Copy the selected cells as tab-separated text (pastes into Excel)."""
        indexes = self.view.selectedIndexes()
        if not indexes or len(indexes) > _MAX_COPY_CELLS:
            return
        rows = sorted({i.row() for i in indexes})
        columns = sorted({i.column() for i in indexes})
        selected = {(i.row(), i.column()) for i in indexes}
        lines = [
            "\t".join(self.model.text(r, c) if (r, c) in selected else "" for c in columns) for r in rows
        ]
        QGuiApplication.clipboard().setText("\r\n".join(lines))
