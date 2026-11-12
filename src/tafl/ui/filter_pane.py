"""The filter pane next to the data grid: instant filters (see ``filters``), one card per column.

A card lists the values of its column and how often each occurs among the rows that pass the
other filters, with a bar. Select values (click, Ctrl+click, Shift+click) and press Apply or
Enter; double-click a value to filter by it alone. Selected values are blue, values that are
possible with the other filters white, excluded ones grey.
"""

from __future__ import annotations

import datetime as dt
import operator
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import polars as pl
from PySide6.QtCore import (
    QAbstractTableModel,
    QItemSelection,
    QItemSelectionModel,
    QModelIndex,
    QPersistentModelIndex,
    Qt,
    QTimer,
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

from tafl.api import Cancelled, col, dtypes

from . import theme
from .filters import (
    COUNT,
    FACETS,
    MATCH,
    PERIODS,
    VALUE,
    Filter,
    NotAStep,
    apply,
    default_options,
    facet,
    facets_for,
    fingerprint,
    formula,
    parse_thresholds,
    range_labels,
    selection,
    stored_values,
    threshold_text,
    thresholds,
    unusable,
    value_key,
)
from .frame_model import DisplayFormat
from .labels import LONG_TIP_LINES, tooltip_text
from .session import Session
from .worker import Worker

Index = QModelIndex | QPersistentModelIndex
_ROOT = QModelIndex()
_BAR = Qt.ItemDataRole.UserRole + 1  # share of the rows (0..1), for the bar behind the count
_LIST_ROWS = 8  # rows a card shows before it scrolls
MAX_CARDS = 100  # the filters after these only apply, without a card: a card takes about 1 MB of widgets
_SLICE = 32_768  # values turned into Python's values at a time
_AT_ONCE = 20_000  # rows (of all the lists to make) that are listed at once; more in the background
_LISTS_AFTER_MS = 50  # the lists of big data start once the window has shown the change (see FilterPane) ...
_BIG_LISTS = 500_000  # ... from these rows (or values of a list marked anew); others at once
_ADD_TIP = "Add a filter for a column (or right-click a column or a cell in the grid)"


Arrangement = tuple[str, int, bool]  # how a list is arranged: the search, the column it is sorted by, descending


@dataclass
class _Listing:
    """The values of a card, as plain Python lists: they can be made in the background (see
    ``_listing``), and the card only takes them. Items: [value, count, stored value, text,
    lower-case text, applied, rank (the place in the order of the values)]."""

    all: list[list[Any]]
    rows: list[list[Any]]  # the ones shown, in order: the applied ones first (see _arranged)
    filtering: bool  # the filter leaves values out (else every value counts as applied)
    total: int  # rows that pass the other filters
    label: Callable[[Any], str]
    arrangement: Arrangement
    applied: int  # of all
    shown: int  # applied ones among the rows: rows 0 .. shown - 1


def _listing(table: pl.DataFrame, total: int, f: Filter, label: Callable[[Any], str], how: Arrangement) -> _Listing:
    """The list of the values ``table`` of ``facet``, in the order of their values: empty first,
    NaN last, texts in the order of Sort (like Excel). No Qt: it can run in another thread, and
    there Polars sorts, without Python's lock; each value keeps its place as a number (its rank),
    so that later sorts compare numbers."""
    dtype = table.schema[VALUE]
    value = col(VALUE).cast(pl.String) if isinstance(dtype, pl.Categorical | pl.Enum) else col(VALUE)
    table = table.sort([dtypes.text_sort_key(value), value] if dtype == pl.String else value, maintain_order=True)
    column = table.get_column(VALUE)
    stored_forms: list[Any] = []
    for start in range(0, column.len(), _SLICE):  # (making Python's values holds Python's lock)
        stored_forms += stored_values(column.slice(start, _SLICE))  # with nanoseconds, which Python's values don't have
    applies = f.picker()
    rows = table.select(VALUE, COUNT).iter_rows()
    items = [
        [value, count, stored, None, None, applies(stored), rank]
        for rank, ((value, count), stored) in enumerate(zip(rows, stored_forms, strict=True))
    ]
    return _arranged(items, bool(f.values), total, label, how)


def _marked(listing: _Listing, f: Filter, how: Arrangement) -> _Listing:
    """``listing`` with the values that ``f`` applies now: the counts stay. New items: the card
    shows the old ones meanwhile."""
    applies = f.picker()
    items = [[value, count, stored, text, lower, applies(stored), rank] for value, count, stored, text, lower, _, rank in listing.all]
    return _arranged(items, bool(f.values), listing.total, listing.label, how)


def _arranged(
    items: list[list[Any]], filtering: bool, total: int, label: Callable[[Any], str], how: Arrangement
) -> _Listing:
    """``items`` as a list that is searched and sorted: applied ones first, then by value (empty
    first) or count. On Python lists: a sorting proxy would call back into Python for every
    comparison, far too slow for columns with many different values."""
    search, column, descending = how
    shown = [item for item in items if search in _lower(item, label)] if search else items
    size = len(items) + 1  # by count, then by value: one number
    key = (lambda item: item[1] * size + item[6]) if column == 1 else _rank
    applied = [item for item in shown if item[5]] if filtering else []
    others = [item for item in shown if not item[5]] if filtering else list(shown)
    applied.sort(key=key, reverse=descending)  # (by value: the items are in that order already)
    others.sort(key=key, reverse=descending)
    # Without filtering, either no value is applied or all are (all but none)
    if not filtering:
        count = len(items) if items and items[0][5] else 0
    else:
        count = len(applied) if shown is items else sum(1 for item in items if item[5])
    first = len(applied) if filtering else (len(others) if count else 0)
    return _Listing(items, applied + others, filtering, total, label, how, count, first)


def _text(item: list[Any], label: Callable[[Any], str]) -> str:
    if item[3] is None:
        value = item[0]
        item[3] = "(empty)" if value is None else "(empty text)" if value == "" else label(value)
    return item[3]


def _lower(item: list[Any], label: Callable[[Any], str]) -> str:
    if item[4] is None:
        item[4] = _text(item, label).lower()
    return item[4]


class _Values(QAbstractTableModel):
    """The values of one card (see ``_Listing``)."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.listing = _arranged([], False, 0, str, ("", 0, False))
        self.search = ""
        self.sort_column, self.descending = 0, False

    @property
    def all(self) -> list[list[Any]]:
        return self.listing.all

    @property
    def rows(self) -> list[list[Any]]:
        return self.listing.rows

    @property
    def filtering(self) -> bool:
        return self.listing.filtering

    @property
    def total(self) -> int:
        return self.listing.total

    @property
    def arrangement(self) -> Arrangement:
        return self.search, self.sort_column, self.descending

    def set_listing(self, listing: _Listing) -> None:
        """Show a list (made for the arrangement of that time: the search or the sort may have
        changed since)."""
        if listing.arrangement != self.arrangement:
            listing = _arranged(listing.all, listing.filtering, listing.total, listing.label, self.arrangement)
        self.beginResetModel()
        self.listing = listing
        self.endResetModel()

    def set_values(self, table: pl.DataFrame, total: int, f: Filter, label: Callable[[Any], str]) -> None:
        self.set_listing(_listing(table, total, f, label, self.arrangement))

    def clear(self) -> None:
        self.set_listing(_arranged([], False, 0, str, self.arrangement))

    def applied(self) -> list[Any]:
        """The applied values (as stored)."""
        return [item[2] for item in self.all if item[5]]

    def set_search(self, text: str) -> None:
        self.search = text.strip().lower()
        self._arrange()

    def sort(self, column: int, order: Qt.SortOrder = Qt.SortOrder.AscendingOrder) -> None:
        self.sort_column, self.descending = column, order == Qt.SortOrder.DescendingOrder
        self._arrange()

    def _arrange(self) -> None:
        self.set_listing(self.listing)

    def text(self, item: list[Any]) -> str:
        return _text(item, self.listing.label)

    def is_applied(self, row: int) -> bool:
        return self.rows[row][5]

    def is_excluded(self, row: int) -> bool:
        """Not among the rows shown now: not selected in a filtered column, or not possible."""
        return not self.is_applied(row) if self.filtering else self.rows[row][1] == 0

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
            return tooltip_text(self.text(item), LONG_TIP_LINES)
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


@dataclass
class _Plan:
    """What the list of a card needs (see FilterCard.prepare)."""

    card: FilterCard
    filter: Filter
    filters: list[Filter]
    index: int
    why: str | None  # why nothing can be listed
    label: Callable[[Any], str]
    arrangement: Arrangement
    made: tuple[Any, Any]  # what the list is made from, and what it is marked for
    data: pl.DataFrame | None = None  # the data of a list that is made anew (None: it is marked anew)
    old: _Listing | None = field(default=None, repr=False)  # the list that is marked anew

    def __post_init__(self) -> None:
        if self.data is None and self.why is None:
            self.old = self.card.model.listing


def _make(plans: list[_Plan], more: list[Filter], cancel: threading.Event | None = None) -> list[tuple[_Plan, Any]]:
    """The lists of ``plans``: a _Listing each, or why there is none. No Qt: for big data this runs
    in the background. ``more``: the filters without a card, which narrow the rows once (for each
    card they would take minutes)."""
    made: list[tuple[_Plan, Any]] = []
    rows: dict[int, pl.DataFrame | None] = {}
    for plan in plans:
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        if plan.why is not None:
            made.append((plan, plan.why))
        elif plan.data is None:  # only the applied values changed: the counts stay
            assert plan.old is not None
            made.append((plan, _marked(plan.old, plan.filter, plan.arrangement)))
        else:
            if id(plan.data) not in rows:
                rows[id(plan.data)] = apply(plan.data, more) if more else None
            result = facet(plan.data, plan.filters, plan.index, rows[id(plan.data)])
            if result is None:
                made.append((plan, "These values can't be listed."))
            else:
                made.append((plan, _listing(result[0], result[1], plan.filter, plan.label, plan.arrangement)))
    return made


_rank = operator.itemgetter(6)  # the place of a value in the order of the values (see _listing)


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
        self._filter = f
        self._made: tuple[Any, ...] | None = None  # what the list shown is made from (see prepare)
        self._marked: tuple[Any, ...] | None = None  # the values it marks as applied
        self._status = ""  # the status line of the list shown
        self._why: str | None = None  # why it lists nothing
        self.updating = False  # a list is being made in the background
        self.setObjectName("FilterCard")
        self.title = QLabel(f.column, self)
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        font = QFont(self.title.font())
        font.setBold(True)
        self.title.setFont(font)
        self.title.setToolTip(tooltip_text(f.column))
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
        self.links.setTextFormat(Qt.TextFormat.RichText)
        self.links.setText(
            " · ".join(
                f'<a href="{k}" style="color:{theme.ACCENT_TEXT}; text-decoration:none">{k.capitalize()}</a>'
                for k in ("all", "possible", "excluded", "none")
            )
        )
        self.links.setWordWrap(True)  # a narrow pane: two lines
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
        # The header gets a selection of its own (always empty): to show a selected column, it
        # asks the model about every row, which takes long with many values (as in the grid)
        horizontal.setSelectionModel(QItemSelectionModel(self.model, horizontal))
        rows = _LIST_ROWS if f.facet in ("values", "period", "length") else 4
        self.view.setFixedHeight(horizontal.sizeHint().height() + rows * row_height + 4)
        self.view.doubleClicked.connect(self.apply_only)
        self.view.selectionModel().selectionChanged.connect(self._selection_changed)
        for key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            shortcut = QShortcut(QKeySequence(key), self.view)
            shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
            shortcut.activated.connect(self.apply)

        self.status = QLabel(self)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
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
        limits = parse_thresholds(self.limits.text())  # (as the steps read a number typed into a setting)
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
            display = self.pane.display
            self.limits.setText("; ".join(threshold_text(t, display.number, display.decimal) for t in thresholds(f)))
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

    def prepare(
        self,
        df: pl.DataFrame | None,
        filters: list[Filter],
        index: int,
        display: DisplayFormat,
        prints: list[tuple[Any, ...]],
        more: list[tuple[Any, ...]],
    ) -> _Plan | None:
        """Take the card's filter (``filters[index]``) and data, and say what its list needs: None
        when the list it shows is the list for them. A list is made anew when its data changed:
        the data, the filter's column, mode and settings, or another filter (the counts are among
        the rows of the others, ``more`` are the filters without a card); when only the filter's
        values changed, it is marked anew. ``prints``: the fingerprint of each filter (the same
        for all cards: a long list of values is not looked at once per card)."""
        f = filters[index]
        self.index = index
        self._filter = f
        self._dtype = df.schema[f.column] if df is not None and f.column in df.columns else None
        self._sync_options(f)
        others = prints[:index] + prints[index + 1 : len(filters)]
        made = (df, display, (*prints[index][:3], others, more))  # (column, mode and settings)
        marked = prints[index][3:]  # (excluded, the values)
        before = self._made
        if before is not None and before[0] is df and before[1] is display and before[2] == made[2]:
            if marked == self._marked:
                return None
            if self._why is None:  # a list, not why there is none
                return _Plan(self, f, filters, index, None, self._labeler(f, display), self.model.arrangement, (made, marked))
        if df is None:
            why: str | None = "No data at this step."
        elif reason := unusable(f, df.schema):
            why = f"Not used at this step: {reason}."
        else:
            why = None
        return _Plan(self, f, filters, index, why, self._labeler(f, display), self.model.arrangement, (made, marked), df)

    def show_updating(self) -> None:
        """The list is being made: "…", and nothing can be picked meanwhile."""
        self.updating = True
        self.status.setText("…")
        self.view.setEnabled(False)
        self._selection_changed()

    def show_current(self) -> None:
        """The list shown is the right one after all (its update was not needed any more): with
        its applied values selected, as when it is made (not what was selected for the change
        that came before: a double-click and then Clear left the value selected)."""
        if self.updating:
            self.updating = False
            self.status.setText(self._status)
            self.view.setEnabled(self.view.isVisibleTo(self))
            self._select_applied()
            self._selection_changed()

    def show_list(self, plan: _Plan, listing: _Listing | str) -> None:
        """Show the list made for ``plan``, or why there is none."""
        f = plan.filter
        listed = isinstance(listing, _Listing)
        self.updating = False
        self.search.setVisible(listed and f.facet in ("values", "period"))
        self.links.setVisible(listed)
        self.view.setVisible(listed)
        self.view.setEnabled(listed)
        if isinstance(listing, _Listing):
            self.model.set_listing(listing)
            self._select_applied()
            count = len(self.model.all)
            noun = {"period": "period", "ranges": "range", "length": "length", "regex": "group"}.get(f.facet, "value")
            text = f"{count:,} {noun}{'s' if count != 1 else ''}"
            if f.values or f.excluded:
                text += f" · {self.model.listing.applied:,} selected"
        else:
            self.model.clear()
            text = listing
        self._status, self._why = text, None if listed else text
        self.status.setText(text)
        self._made, self._marked = plan.made
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
        self._select_rows(range(self.model.listing.shown))  # the applied values are the first rows

    def _select(self, values: list[Any]) -> None:
        keys = {value_key(v) for v in values}
        self._select_rows([row for row, item in enumerate(self.model.rows) if value_key(item[2]) in keys])

    def _select_rows(self, rows: list[int] | range) -> None:
        """Select these rows (ascending), in as few ranges as possible."""
        selection = QItemSelection()
        if isinstance(rows, range):  # one run (of all values, maybe): not row by row
            if len(rows):
                selection.select(self.model.index(rows[0], 0), self.model.index(rows[-1], 1))
        else:
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
        rows = self.model.rows  # (not selectedRows(), nor row by row: fast with many values)
        return [item[2] for top, bottom in self._merged_selection() for item in rows[top : bottom + 1]]

    def _stored(self, chosen: list[Any]) -> tuple[list[Any], bool]:
        """How the filter stores these chosen values: ``filters.selection``."""
        return selection(chosen, [item[2] for item in self.model.all])

    def _merged_selection(self) -> list[tuple[int, int]]:
        """The selected rows, as (first, last) of each run of rows."""
        merged: list[tuple[int, int]] = []
        for top, bottom in sorted((part.top(), part.bottom()) for part in self.view.selectionModel().selection()):
            if merged and top <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(merged[-1][1], bottom))
            else:
                merged.append((top, bottom))
        return merged

    def _selection_changed(self, *_: Any) -> None:
        # Applied, the selected values would be the ones applied: is that what is applied now? The
        # applied values are the first rows, all of them unless the search hides some
        listing = self.model.listing
        applied = [(0, listing.shown - 1)] if listing.shown else []
        changed = self._merged_selection() != applied or listing.applied != listing.shown
        filtering = bool(self._filter.values)
        self.apply_button.setEnabled(changed and self.view.isEnabled())
        self.revert_button.setEnabled(changed and self.view.isEnabled())
        self.clear_button.setEnabled(filtering or self._filter.excluded)
        self.as_step.setEnabled(filtering and self.view.isEnabled())

    # --- choosing values -----------------------------------------------------------------------

    def apply(self) -> None:
        if not self.view.isEnabled():
            return
        rows = self.model.rows
        if len(rows) == len(self.model.all) and self._merged_selection() == [(0, len(rows) - 1)]:
            self.pane.set_values(self.index, [], True)  # all values (as _stored says, without going through them)
        else:
            self.pane.set_values(self.index, *self._stored(self.pending()))

    def apply_only(self, index: QModelIndex) -> None:
        self.pane.set_values(self.index, [self.model.rows[index.row()][2]])

    def quick_select(self, kind: str) -> None:
        """All, Possible, Excluded or None of the values shown (they still need Apply)."""
        rows = self.model.rows
        chosen: list[int] | range
        if kind == "all":
            chosen = range(len(rows))
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
        self.add_button.setToolTip(tooltip_text(_ADD_TIP))
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
        self.too_many = QLabel(self)
        self.too_many.setTextFormat(Qt.TextFormat.PlainText)
        self.too_many.setObjectName("Dim")
        self.too_many.setWordWrap(True)
        self.too_many.setMargin(6)
        self.too_many.hide()
        container = QWidget(self)
        self.cards_layout = QVBoxLayout(container)
        self.cards_layout.setContentsMargins(2, 2, 2, 2)
        self.cards_layout.setSpacing(8)
        self.cards_layout.addWidget(self.hint)
        self.cards_layout.addWidget(self.too_many)  # the cards go before this line
        self.cards_layout.addStretch(1)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        # The pane can be narrower than a card (a narrow window): then the cards scroll sideways
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setWidget(container)
        self.scroll = scroll
        self.container = container
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addLayout(header)
        layout.addWidget(scroll, 1)
        self.setStyleSheet(
            f"QFrame#FilterCard {{ border: 1px solid {theme.BORDER_LIGHT}; border-radius: 6px; background: {theme.BASE}; }}"
        )
        # The lists of big data are made in the background, one job that matters at a time
        self._lister = Worker(self)
        self._lister.finished.connect(self._listed)
        self._lister.failed.connect(self._not_listed)
        self._making: list[_Plan] = []  # the plans of the lists being made
        self._others: list[Filter] = []  # ... and the filters without a card
        # They start once the change is done; lists of big data once the window has shown it (the
        # data panels too, and it draws them a moment later): in the background they would take
        # turns with it for Python's lock, and the drawing, many short calls into Python, would wait
        # each time. Small lists don't wait: they are made in less time than that.
        self._soon = QTimer(self)
        self._soon.setSingleShot(True)
        self._soon.timeout.connect(self._start_lists)
        self._lists = True  # the cards have their lists (else: once the pane comes into sight)

    @property
    def updating(self) -> bool:
        """Lists are being made in the background (their cards show "…")."""
        return self._lister.busy or self._soon.isActive()

    def filters(self) -> list[Filter]:
        return [Filter.from_dict(f) for f in self.session.filters(self.table_id)] if self.table_id else []

    def offer(self, table_id: str | None, df: pl.DataFrame | None) -> None:
        """The data panel's data. A closed pane, or the pane of the view that isn't shown, makes
        no lists (only its cards): it makes them when it comes into sight."""
        self._show(table_id, df, self.isVisibleTo(self.window()))

    def setVisible(self, visible: bool) -> None:
        super().setVisible(visible)
        if visible:
            self._catch_up()

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        self._catch_up()

    def _catch_up(self) -> None:
        if not self._lists and self.isVisibleTo(self.window()):
            self._show(self.table_id, self.df, True)

    def stop(self) -> None:
        """Stop making lists (the window closes)."""
        self._soon.stop()
        self._lister.forget()

    def show_data(self, table_id: str | None, df: pl.DataFrame | None) -> None:
        """Show the filters of a table for its data after the selected step. A card's list is made
        only when what it lists changed (see FilterCard.prepare); for big data in the background,
        while the card shows "…"."""
        self._show(table_id, df, True)

    def _show(self, table_id: str | None, df: pl.DataFrame | None, lists: bool) -> None:
        filters = [Filter.from_dict(f) for f in self.session.filters(table_id)] if table_id else []
        prints = [fingerprint(f) for f in filters]  # once for all cards
        shown = (table_id, id(df), prints)
        if shown == self._shown and df is self.df and (self._lists or not lists):
            return
        self._soon.stop()
        self._lister.forget()  # a list for the data or filters before: not wanted any more
        self._shown, self.table_id, self.df, self._lists = shown, table_id, df, lists
        first = filters[:MAX_CARDS]  # the number of a card is the index of its filter
        if [(c.column, c.facet) for c in self.cards] != [(f.column, f.facet) for f in first]:
            for card in self.cards:
                self.cards_layout.removeWidget(card)
                card.hide()
                card.deleteLater()  # a card may still be handling a click
            self.cards = [FilterCard(self, f) for f in first]
            for number, card in enumerate(self.cards):
                self.cards_layout.insertWidget(number + 1, card)
        others = filters[MAX_CARDS:]
        plans = []
        for number, card in enumerate(self.cards):
            plan = card.prepare(df, first, number, self.display, prints, prints[MAX_CARDS:])
            if plan is None:
                card.show_current()
            else:
                plans.append(plan)
        self._making = []
        if lists:
            slow = [plan for plan in plans if plan.why is None]  # (why there is no list: at once)
            size = sum(len(plan.data) if plan.data is not None else len(plan.old.all if plan.old else []) for plan in slow)
            at_once = [plan for plan in plans if plan.why is not None] + (slow if size <= _AT_ONCE else [])
            for plan, listing in _make(at_once, others):
                plan.card.show_list(plan, listing)
            self._making = [plan for plan in slow if plan not in at_once]
        if self._making:
            for plan in self._making:
                plan.card.show_updating()
            self._others = others
            biggest = max(len(plan.data) if plan.data is not None else len(plan.old.all if plan.old else []) for plan in self._making)
            self._soon.start(_LISTS_AFTER_MS if biggest >= _BIG_LISTS else 0)
        more = len(filters) - len(first)
        full = len(filters) >= MAX_CARDS
        most = f"A table shows {MAX_CARDS} filters at most: remove {f'{more + 1:,}' if more else 'one'} to add another."
        self.too_many.setText(
            f"{more:,} more filter{'s are' if more != 1 else ' is'} on this table: too many to show.\n{most}" if more else most
        )
        self.too_many.setVisible(full)
        self.hint.setVisible(not filters)
        self.add_button.setEnabled(table_id is not None and not full)
        self.add_button.setToolTip(tooltip_text(most if full else _ADD_TIP))

    def _start_lists(self) -> None:
        making, others = list(self._making), self._others
        if making:
            self._lister.start(lambda cancel: _make(making, others, cancel))

    def _listed(self, made: list[tuple[_Plan, Any]]) -> None:
        self._making = []
        for plan, listing in made:
            if plan.card in self.cards:
                plan.card.show_list(plan, listing)

    def _not_listed(self, text: str) -> None:
        """Making the lists failed (a fault): the cards say so, and the window shows the error."""
        making, self._making = self._making, []
        for plan in making:
            if plan.card in self.cards:
                plan.card.show_list(plan, "These values can't be listed.")
        self.session.error.emit(text)

    def _fill_add_menu(self) -> None:
        self.add_menu.clear()
        used = {f.column for f in self.filters()}
        # Not the columns of a plugin's own values (Object): they can't be filtered
        columns = [name for name, dtype in self.df.schema.items() if dtype != pl.Object] if self.df is not None else []
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
        already, that one gets the values; ``again`` adds another filter in any case, unless the
        table has MAX_CARDS filters: a new one would have no card, and so would a filter past them
        that takes the values. The line under the cards says so, and comes into view."""
        filters = self.filters()
        first = filters[:MAX_CARDS]
        existing = next((f for f in first if f.column == column and (values is None or f.facet == "values")), None)
        if existing is None or again:
            if len(filters) >= MAX_CARDS:
                QTimer.singleShot(0, self.scroll, self._show_too_many)
                return
            filters.append(Filter(column, list(values or [])))
        elif values is not None:
            existing.values, existing.excluded = list(values), False
        self._save(filters)

    def _show_too_many(self) -> None:
        across = self.scroll.horizontalScrollBar()
        kept = across.value()  # only down: the cards must not move to the side
        self.scroll.ensureWidgetVisible(self.too_many)
        across.setValue(kept)

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
                filters[index].values, filters[index].excluded = [], False
                self._save(filters)

    def set_values(self, index: int, values: list[Any], excluded: bool = False) -> None:
        """Apply values of a filter; ``excluded``: all values except these."""
        filters = self.filters()
        if 0 <= index < len(filters):
            filters[index].values, filters[index].excluded = list(values), excluded
            self._save(filters)

    def remove(self, index: int) -> None:
        filters = self.filters()
        if 0 <= index < len(filters):
            del filters[index]
            self._save(filters)

    def add_as_step(self, index: int) -> None:
        """Turn the filter into a step after the selected one ("Filter rows by formula"); the
        filter itself is removed. A filter that a step can't say the same way stays: its card says
        why."""
        filters = self.filters()
        if not 0 <= index < len(filters) or self.df is None or unusable(filters[index], self.df.schema):
            return
        chosen = filters.pop(index)
        try:
            condition = formula(chosen, self.df.schema[chosen.column], self.df.get_column(chosen.column))
        except NotAStep as reason:
            if index < len(self.cards):
                self.cards[index].status.setText(f"Not added as a step: {reason}.")
            return
        action = self.session.registry.actions["rows.filter_formula"]
        self.session.add_step(action, params={"condition": condition, "keep": "keep"})
        self._save(filters)
