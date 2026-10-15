"""The filter pane next to the data grid: instant filters (see ``filters``), one card per column.

A card lists the values of its column and how often each occurs among the rows that pass the
other filters, with a bar. Select values (click, Ctrl+click, Shift+click) and press Apply or
Enter; double-click a value to filter by it alone. Selected values are blue, values that are
possible with the other filters white, excluded ones grey.
"""

from __future__ import annotations

import datetime as dt
import json
import math
from collections.abc import Callable
from typing import Any

import polars as pl
from PySide6.QtCore import (
    QAbstractTableModel,
    QItemSelection,
    QItemSelectionModel,
    QModelIndex,
    QPersistentModelIndex,
    Qt,
)
from PySide6.QtGui import QColor, QFont, QKeySequence, QPainter, QPalette, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableView,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .filters import (
    COUNT,
    FACETS,
    MATCH,
    PERIODS,
    VALUE,
    Filter,
    default_options,
    facet,
    facets_for,
    formula,
    parse_thresholds,
    range_labels,
    thresholds,
    to_json,
    unusable,
)
from .frame_model import DisplayFormat
from .session import Session

Index = QModelIndex | QPersistentModelIndex
_ROOT = QModelIndex()
_BAR = Qt.ItemDataRole.UserRole + 1  # share of the rows (0..1), for the bar behind the count
_LIST_ROWS = 8  # rows a card shows before it scrolls


def _key(value: Any) -> Any:
    """A stored value in a form that can be looked up in a set (NaN never equals itself)."""
    return ("NaN",) if isinstance(value, float) and math.isnan(value) else value


class _Values(QAbstractTableModel):
    """The values of one card: applied ones first, then sorted by value (empty first) or count,
    narrowed down by the search. Sorting and searching are done on Python lists here: a sorting
    proxy would call back into Python for every comparison, far too slow for columns with many
    different values."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.all: list[list[Any]] = []  # [value, count, stored value, text, lower-case text]
        self.rows: list[list[Any]] = []  # the ones shown, in order
        self.applied: set[Any] = set()  # keys of the applied values
        self.total = 0  # rows that pass the other filters
        self.search = ""
        self.sort_column, self.descending = 0, False
        self._format: Callable[[Any], str] = str

    def set_values(self, table: pl.DataFrame, total: int, applied: list[Any], label: Callable[[Any], str]) -> None:
        self._format = label
        self.all = [[value, count, to_json(value), None, None] for value, count in table.select(VALUE, COUNT).iter_rows()]
        self.applied = {_key(v) for v in applied}
        self.total = total
        self._arrange()

    def clear(self) -> None:
        self.all, self.applied, self.total = [], set(), 0
        self._arrange()

    def set_search(self, text: str) -> None:
        self.search = text.strip().lower()
        self._arrange()

    def sort(self, column: int, order: Qt.SortOrder = Qt.SortOrder.AscendingOrder) -> None:
        self.sort_column, self.descending = column, order == Qt.SortOrder.DescendingOrder
        self._arrange()

    def _arrange(self) -> None:
        self.beginResetModel()
        items = self.all
        if self.search:
            items = [item for item in items if self.search in self._lower(item)]
        key = _by_count if self.sort_column == 1 else _by_value
        applied = [item for item in items if _key(item[2]) in self.applied]
        others = [item for item in items if _key(item[2]) not in self.applied]
        try:
            applied.sort(key=key, reverse=self.descending)
            others.sort(key=key, reverse=self.descending)
        except TypeError:  # values that can't be compared: by their text
            applied.sort(key=self.text, reverse=self.descending)
            others.sort(key=self.text, reverse=self.descending)
        self.rows = applied + others  # applied values first, whichever way it is sorted
        self.endResetModel()

    def text(self, item: list[Any]) -> str:
        if item[3] is None:
            value = item[0]
            item[3] = "(empty)" if value is None else "(empty text)" if value == "" else self._format(value)
        return item[3]

    def _lower(self, item: list[Any]) -> str:
        if item[4] is None:
            item[4] = self.text(item).lower()
        return item[4]

    def is_applied(self, row: int) -> bool:
        return _key(self.rows[row][2]) in self.applied

    def is_excluded(self, row: int) -> bool:
        """Not among the rows shown now: not selected in a filtered column, or not possible."""
        return not self.is_applied(row) if self.applied else self.rows[row][1] == 0

    def rowCount(self, parent: Index = _ROOT) -> int:
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent: Index = _ROOT) -> int:
        return 0 if parent.isValid() else 2

    def data(self, index: Index, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        item = self.rows[index.row()]
        value, count = item[0], item[1]
        if role == Qt.ItemDataRole.DisplayRole:
            return self.text(item) if index.column() == 0 else f"{count:,}"
        if role == Qt.ItemDataRole.ToolTipRole and index.column() == 0 and len(self.text(item)) > 30:
            return self.text(item)
        if role == Qt.ItemDataRole.ForegroundRole and self.is_excluded(index.row()):
            return QColor(theme.TEXT_DISABLED)
        if role == Qt.ItemDataRole.FontRole and (value is None or value == ""):
            font = QFont()
            font.setItalic(True)
            return font
        if role == Qt.ItemDataRole.TextAlignmentRole and index.column() == 1:
            return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        if role == _BAR and index.column() == 1:
            return count / self.total if self.total else 0.0
        return None

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return ("Value", "Count")[section]
        return None


def _value_key(value: Any) -> tuple[int, Any]:
    """Sort key of a value: empty first, NaN last."""
    if value is None:
        return (0, 0)
    if isinstance(value, float) and math.isnan(value):
        return (2, 0)
    return (1, value)


def _by_value(item: list[Any]) -> tuple[int, Any]:
    return _value_key(item[0])


def _by_count(item: list[Any]) -> tuple[int, tuple[int, Any]]:
    return item[1], _value_key(item[0])


class _CountBar(QStyledItemDelegate):
    """The count with a bar behind it: its share of the rows."""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: Index) -> None:
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        widget = opt.widget
        style = widget.style() if widget is not None else QApplication.style()
        style.drawPrimitive(QStyle.PrimitiveElement.PE_PanelItemViewItem, opt, painter, widget)
        share = index.data(_BAR) or 0.0
        if share > 0:
            bar = opt.rect.adjusted(2, 3, -2, -3)
            bar.setWidth(max(2, round(bar.width() * min(share, 1.0))))
            color = QColor(theme.ACCENT)
            color.setAlpha(70)
            painter.fillRect(bar, color)
        selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        foreground = index.data(Qt.ItemDataRole.ForegroundRole)
        role = QPalette.ColorRole.HighlightedText if selected else QPalette.ColorRole.Text
        painter.setPen(foreground if foreground is not None and not selected else opt.palette.color(role))
        painter.drawText(opt.rect.adjusted(4, 0, -6, 0), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, opt.text)


class FilterCard(QFrame):
    """One filter: its column, how it groups the cells (its mode, called facet in the code), the
    values to pick from, and Apply / Revert / Clear."""

    def __init__(self, pane: FilterPane, f: Filter) -> None:
        super().__init__(pane)
        self.pane = pane
        self.column = f.column
        self.facet = f.facet
        self.index = 0
        self._dtype: pl.DataType | None = None
        self._applied: list[Any] = []
        self.setObjectName("FilterCard")
        self.title = QLabel(f.column, self)
        font = QFont(self.title.font())
        font.setBold(True)
        self.title.setFont(font)
        self.title.setToolTip(f.column)
        self.title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)  # long names don't widen the card
        self.mode = QToolButton(self)
        self.mode.setText(f"{FACETS[f.facet]} ▾")
        self.mode.setToolTip("How to filter: by values, time periods, number ranges, text length or a pattern")
        self.mode.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.mode.setStyleSheet(f"QToolButton {{ color: {theme.TEXT_DIM}; }} QToolButton::menu-indicator {{ image: none; }}")
        self.mode_menu = QMenu(self.mode)
        self.mode_menu.aboutToShow.connect(self._fill_mode_menu)
        self.mode.setMenu(self.mode_menu)
        more = QToolButton(self)
        more.setText("⋯")
        more.setToolTip("More")
        more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        more.setStyleSheet("QToolButton::menu-indicator { image: none; }")
        menu = QMenu(more)
        self.as_step = menu.addAction("Add as step (Filter rows by formula)", lambda: self.pane.add_as_step(self.index))
        menu.addAction("Remove filter", lambda: self.pane.remove(self.index))
        more.setMenu(menu)
        close = QToolButton(self)
        close.setText("✕")
        close.setToolTip("Remove this filter")
        close.clicked.connect(lambda: self.pane.remove(self.index))
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.addWidget(self.title, 1)
        header.addWidget(self.mode)
        header.addWidget(more)
        header.addWidget(close)

        self.options = self._option_widgets(f.facet)
        self.search = QLineEdit(self)
        self.search.setPlaceholderText("Search values…")
        self.search.setClearButtonEnabled(True)
        self.links = QLabel(self)
        self.links.setText(
            " · ".join(
                f'<a href="{k}" style="color:{theme.ACCENT}; text-decoration:none">{k.capitalize()}</a>'
                for k in ("all", "possible", "excluded", "none")
            )
        )
        self.links.setToolTip(
            "Select values (of the ones shown, if you searched):\nAll · Possible with the other filters · "
            "Excluded (grey; inverts a selection) · None"
        )
        self.links.linkActivated.connect(self.quick_select)

        self.model = _Values(self)
        self.search.textChanged.connect(lambda text: self._keeping_selection(lambda: self.model.set_search(text)))
        self.view = QTableView(self)
        self.view.setModel(self.model)
        self.view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.view.setWordWrap(False)
        self.view.setShowGrid(False)
        self.view.setItemDelegateForColumn(1, _CountBar(self.view))
        self.view.verticalHeader().hide()
        row_height = self.fontMetrics().height() + 4
        self.view.verticalHeader().setDefaultSectionSize(row_height)
        horizontal = self.view.horizontalHeader()
        horizontal.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        horizontal.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        horizontal.resizeSection(1, 64)
        horizontal.setToolTip("Click to sort by value or count (again: the other way round)")
        horizontal.setSectionsClickable(True)
        horizontal.setSortIndicatorShown(True)
        horizontal.setSortIndicator(0, Qt.SortOrder.AscendingOrder)
        horizontal.sortIndicatorChanged.connect(
            lambda column, order: self._keeping_selection(lambda: self.model.sort(column, order))
        )
        rows = _LIST_ROWS if f.facet in ("values", "period", "length") else 4
        self.view.setFixedHeight(horizontal.sizeHint().height() + rows * row_height + 4)
        self.view.doubleClicked.connect(self.apply_only)
        self.view.selectionModel().selectionChanged.connect(self._selection_changed)
        for key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            shortcut = QShortcut(QKeySequence(key), self.view)
            shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
            shortcut.activated.connect(self.apply)

        self.status = QLabel(self)
        self.status.setObjectName("Dim")
        self.status.setWordWrap(True)
        self.apply_button = QPushButton("Apply", self)
        self.apply_button.setToolTip("Filter by the selected values (Enter)")
        self.apply_button.clicked.connect(self.apply)
        self.revert_button = QToolButton(self)
        self.revert_button.setText("Revert")
        self.revert_button.setToolTip("Back to the values that are applied")
        self.revert_button.clicked.connect(self._select_applied)
        self.clear_button = QToolButton(self)
        self.clear_button.setText("Clear")
        self.clear_button.setToolTip("Stop filtering by this column (the filter stays here)")
        self.clear_button.clicked.connect(lambda: self.pane.set_values(self.index, []))
        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.addWidget(self.clear_button)
        buttons.addStretch(1)
        buttons.addWidget(self.revert_button)
        buttons.addWidget(self.apply_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(4)
        layout.addLayout(header)
        if self.options is not None:
            layout.addWidget(self.options)
        layout.addWidget(self.search)
        layout.addWidget(self.links)
        layout.addWidget(self.view)
        layout.addWidget(self.status)
        layout.addLayout(buttons)

    def _option_widgets(self, facet_name: str) -> QWidget | None:
        """The settings of a mode: how dates are grouped, the thresholds, the pattern."""
        if facet_name not in ("period", "ranges", "regex"):
            return None
        box = QWidget(self)
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        if facet_name == "period":
            row.addWidget(QLabel("Group by", box))
            self.period = QComboBox(box)
            for key, text in PERIODS.items():
                self.period.addItem(text, key)
            self.period.currentIndexChanged.connect(
                lambda _: self.pane.set_options(self.index, {"by": self.period.currentData()})
            )
            row.addWidget(self.period, 1)
        elif facet_name == "ranges":
            self.limits = QLineEdit(box)
            self.limits.setPlaceholderText("Thresholds, e.g. 100; 1000")
            self.limits.setToolTip("Numbers that separate the ranges, with ; or spaces between them (Enter applies)")
            self.limits.editingFinished.connect(self._limits_changed)
            self.side = QComboBox(box)
            self.side.addItem("< x  |  ≥ x", True)
            self.side.addItem("≤ x  |  > x", False)
            self.side.setToolTip("Which range a value equal to a threshold belongs to")
            self.side.currentIndexChanged.connect(lambda _: self._limits_changed())
            row.addWidget(self.limits, 1)
            row.addWidget(self.side)
        else:
            self.pattern = QLineEdit(box)
            self.pattern.setPlaceholderText("Pattern, e.g. ^[0-9]{5}$  or  GmbH")
            self.pattern.setToolTip(
                "A regular expression (Enter applies). Picks texts that contain a match:\n"
                "^ start, $ end, . any character, [0-9] a digit, + one or more, (?i) ignore case"
            )
            self.pattern.editingFinished.connect(
                lambda: self.pane.set_options(self.index, {"pattern": self.pattern.text()})
            )
            row.addWidget(self.pattern, 1)
        return box

    def _limits_changed(self) -> None:
        limits = parse_thresholds(self.limits.text(), self.pane.display.decimal)
        self.pane.set_options(self.index, {"thresholds": limits, "lower_inclusive": bool(self.side.currentData())})

    def _sync_options(self, f: Filter) -> None:
        """Show the filter's settings (without reacting to the change)."""
        names = {"period": ("period",), "ranges": ("limits", "side"), "regex": ("pattern",)}.get(f.facet, ())
        widgets = [getattr(self, name) for name in names]
        for widget in widgets:
            widget.blockSignals(True)
        if f.facet == "period":
            self.period.setCurrentIndex(max(0, self.period.findData(f.options.get("by", "month"))))
        elif f.facet == "ranges":
            self.limits.setText("; ".join(self.pane.display.number(t) for t in thresholds(f)))
            self.side.setCurrentIndex(0 if f.options.get("lower_inclusive", True) else 1)
        elif f.facet == "regex" and not self.pattern.hasFocus():
            self.pattern.setText(str(f.options.get("pattern") or ""))
        for widget in widgets:
            widget.blockSignals(False)

    def _fill_mode_menu(self) -> None:
        self.mode_menu.clear()
        possible = facets_for(self._dtype) if self._dtype is not None else list(FACETS)
        for key, text in FACETS.items():
            entry = self.mode_menu.addAction(text, lambda k=key: self.pane.set_facet(self.index, k))
            entry.setCheckable(True)
            entry.setChecked(key == self.facet)
            entry.setEnabled(key in possible)

    # --- showing -------------------------------------------------------------------------------

    def update_values(self, df: pl.DataFrame | None, filters: list[Filter], index: int, display: DisplayFormat) -> None:
        f = filters[index]
        self.index = index
        self._applied = list(f.values)
        self._dtype = df.schema[f.column] if df is not None and f.column in df.columns else None
        self._sync_options(f)
        if df is None:
            why: str | None = "No data at this step."
        elif reason := unusable(f, df.schema):
            why = f"Not used at this step: {reason}."
        else:
            why = None
        result = facet(df, filters, index) if why is None else None
        if result is None and why is None:
            why = "These values can't be listed."
        listed = result is not None
        self.search.setVisible(listed and f.facet in ("values", "period"))
        self.links.setVisible(listed)
        self.view.setVisible(listed)
        self.view.setEnabled(listed)
        if result is None:
            self.model.clear()
            self.status.setText(why or "")
        else:
            table, total = result
            self.model.set_values(table, total, self._applied, self._labeler(f, display))
            self._select_applied()
            count = len(self.model.all)
            noun = {"period": "period", "ranges": "range", "length": "length", "regex": "group"}.get(f.facet, "value")
            text = f"{count:,} {noun}{'s' if count != 1 else ''}"
            if self._applied:
                text += f" · {len(self._applied):,} selected"
            self.status.setText(text)
        self._selection_changed()

    def _labeler(self, f: Filter, display: DisplayFormat) -> Callable[[Any], str]:
        """How the values of the list are shown."""
        if f.facet == "period":
            if f.options.get("by") == "date":
                date = display.formatter(pl.Date)
                return lambda key: date(dt.date.fromisoformat(key))
            return str
        if f.facet == "ranges":
            labels = range_labels(thresholds(f), f.options.get("lower_inclusive", True), display.number)
            return lambda i: labels[i] if 0 <= i < len(labels) else str(i)
        if f.facet == "length":
            return lambda n: "0 characters (empty text)" if n == 0 else f"{n:,} character{'s' if n != 1 else ''}"
        if f.facet == "regex":
            return lambda key: "matches" if key == MATCH else "doesn't match"
        return display.formatter(self._dtype) if self._dtype is not None else str

    def _select_applied(self) -> None:
        self._select(self._applied)

    def _select(self, values: list[Any]) -> None:
        keys = {_key(v) for v in values}
        self._select_rows([row for row, item in enumerate(self.model.rows) if _key(item[2]) in keys])

    def _select_rows(self, rows: list[int]) -> None:
        """Select these rows (ascending), in as few ranges as possible."""
        selection = QItemSelection()
        start = end = -2
        for row in [*rows, -2]:
            if row == end + 1:
                end = row
                continue
            if start >= 0:
                selection.select(self.model.index(start, 0), self.model.index(end, 1))
            start = end = row
        flags = QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows
        self.view.selectionModel().select(selection, flags)

    def _keeping_selection(self, change: Callable[[], None]) -> None:
        """Search and sort without losing the values selected so far."""
        selected = self.pending()
        change()
        self._select(selected)

    def pending(self) -> list[Any]:
        """The stored form of the selected values, in list order."""
        rows = sorted(i.row() for i in self.view.selectionModel().selectedRows())
        return [self.model.rows[row][2] for row in rows]

    def _selection_changed(self, *_: Any) -> None:
        changed = {_key(v) for v in self.pending()} != {_key(v) for v in self._applied}
        self.apply_button.setEnabled(changed and self.view.isEnabled())
        self.revert_button.setEnabled(changed and self.view.isEnabled())
        self.clear_button.setEnabled(bool(self._applied))
        self.as_step.setEnabled(bool(self._applied) and self.view.isEnabled())

    # --- choosing values -----------------------------------------------------------------------

    def apply(self) -> None:
        if self.view.isEnabled():
            self.pane.set_values(self.index, self.pending())

    def apply_only(self, index: QModelIndex) -> None:
        self.pane.set_values(self.index, [self.model.rows[index.row()][2]])

    def quick_select(self, kind: str) -> None:
        """All, Possible, Excluded or None of the values shown (they still need Apply)."""
        rows = self.model.rows
        if kind == "all":
            chosen = list(range(len(rows)))
        elif kind == "possible":
            chosen = [row for row in range(len(rows)) if rows[row][1] > 0]
        elif kind == "excluded":
            chosen = [row for row in range(len(rows)) if self.model.is_excluded(row)]
        else:
            chosen = []
        self._select_rows(chosen)


class FilterPane(QWidget):
    """Instant filters of the selected table, next to its data."""

    def __init__(self, session: Session, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self.display = DisplayFormat()
        self.table_id: str | None = None
        self.df: pl.DataFrame | None = None
        self.cards: list[FilterCard] = []
        self._shown: tuple[Any, ...] | None = None
        title = QLabel("Filters", self)
        title.setObjectName("PanelTitle")
        self.add_button = QPushButton("+ Filter", self)
        self.add_button.setToolTip("Add a filter for a column (or right-click a column or a cell in the grid)")
        self.add_menu = QMenu(self.add_button)
        self.add_menu.aboutToShow.connect(self._fill_add_menu)
        self.add_button.setMenu(self.add_menu)
        header = QHBoxLayout()
        header.setContentsMargins(4, 0, 4, 0)
        header.addWidget(title, 1)
        header.addWidget(self.add_button)
        self.hint = QLabel(
            "Pick a column with “+ Filter”, or right-click a column or a cell in the grid.\n\n"
            "Filters only change what you see here — not the data, a run or an export — and they "
            "stay while you click through the steps.",
            self,
        )
        self.hint.setObjectName("Dim")
        self.hint.setWordWrap(True)
        self.hint.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.hint.setMargin(6)
        container = QWidget(self)
        self.cards_layout = QVBoxLayout(container)
        self.cards_layout.setContentsMargins(2, 2, 2, 2)
        self.cards_layout.setSpacing(8)
        self.cards_layout.addWidget(self.hint)
        self.cards_layout.addStretch(1)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(container)
        self.container = container
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addLayout(header)
        layout.addWidget(scroll, 1)
        self.setStyleSheet(
            f"QFrame#FilterCard {{ border: 1px solid {theme.BORDER_LIGHT}; border-radius: 6px; background: {theme.BASE}; }}"
        )
        self.setMinimumWidth(220)

    def filters(self) -> list[Filter]:
        return [Filter.from_dict(f) for f in self.session.filters(self.table_id)] if self.table_id else []

    def show_data(self, table_id: str | None, df: pl.DataFrame | None) -> None:
        """Show the filters of a table for its data after the selected step."""
        filters = [Filter.from_dict(f) for f in self.session.filters(table_id)] if table_id else []
        shown = (table_id, id(df), json.dumps([f.to_dict() for f in filters], default=str))
        if shown == self._shown and df is self.df:
            return
        self._shown, self.table_id, self.df = shown, table_id, df
        if [(c.column, c.facet) for c in self.cards] != [(f.column, f.facet) for f in filters]:
            for card in self.cards:
                self.cards_layout.removeWidget(card)
                card.hide()
                card.deleteLater()  # a card may still be handling a click
            self.cards = [FilterCard(self, f) for f in filters]
            for number, card in enumerate(self.cards):
                self.cards_layout.insertWidget(number + 1, card)
        for number, card in enumerate(self.cards):
            card.update_values(df, filters, number, self.display)
        self.hint.setVisible(not filters)
        self.add_button.setEnabled(table_id is not None)

    def _fill_add_menu(self) -> None:
        self.add_menu.clear()
        used = {f.column for f in self.filters()}
        columns = self.df.columns if self.df is not None else []
        for column in columns:  # a second filter for a column can group it differently
            text = f"{column}  (another filter)" if column in used else column
            self.add_menu.addAction(text, lambda c=column: self.add_filter(c, again=True))
        if not columns:
            self.add_menu.addAction("No columns at this step").setEnabled(False)

    # --- changing filters ------------------------------------------------------------------------

    def _save(self, filters: list[Filter]) -> None:
        if self.table_id is not None:
            self.session.set_filters(self.table_id, [f.to_dict() for f in filters])

    def add_filter(self, column: str, values: list[Any] | None = None, again: bool = False) -> None:
        """A filter for ``column`` (with ``values`` applied). If the column has a filter by values
        already, that one gets the values; ``again`` adds another filter in any case."""
        filters = self.filters()
        existing = next((f for f in filters if f.column == column and (values is None or f.facet == "values")), None)
        if existing is None or again:
            filters.append(Filter(column, list(values or [])))
        elif values is not None:
            existing.values = list(values)
        self._save(filters)

    def set_facet(self, index: int, facet_name: str) -> None:
        """Filter by values, time periods, number ranges, text length or a pattern (the
        selection starts over)."""
        filters = self.filters()
        if 0 <= index < len(filters) and filters[index].facet != facet_name:
            filters[index] = Filter(filters[index].column, [], facet_name, default_options(facet_name))
            self._save(filters)

    def set_options(self, index: int, options: dict[str, Any]) -> None:
        """Change how a filter groups its values; a changed grouping starts the selection over."""
        filters = self.filters()
        if 0 <= index < len(filters):
            merged = {**filters[index].options, **options}
            if merged != filters[index].options:
                filters[index].options = merged
                filters[index].values = []
                self._save(filters)

    def set_values(self, index: int, values: list[Any]) -> None:
        filters = self.filters()
        if 0 <= index < len(filters):
            filters[index].values = list(values)
            self._save(filters)

    def remove(self, index: int) -> None:
        filters = self.filters()
        if 0 <= index < len(filters):
            del filters[index]
            self._save(filters)

    def add_as_step(self, index: int) -> None:
        """Turn the filter into a step after the selected one ("Filter rows by formula"); the
        filter itself is removed."""
        filters = self.filters()
        if not 0 <= index < len(filters) or self.df is None or unusable(filters[index], self.df.schema):
            return
        chosen = filters.pop(index)
        condition = formula(chosen, self.df.schema[chosen.column])
        action = self.session.registry.actions["rows.filter_formula"]
        self.session.add_step(action, params={"condition": condition, "keep": "keep"})
        self._save(filters)
