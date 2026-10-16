"""The list of formula functions: search, categories, what each one does, how it is written, an
example. As a window it stays open next to the app (F1) and inserts into the formula field used
last; the formula window (F2) shows the same list beside its editor."""

from __future__ import annotations

import html
import weakref
from typing import Any, Protocol

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication, QKeyEvent
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTextBrowser,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from tafl.core import functions
from tafl.core.functions import CATEGORIES, CURSOR, MORE, Function

from . import theme

_ALL = "All"


def insert_into(edit: QPlainTextEdit, template: str) -> None:
    """Insert a function at the cursor; typing goes on where the template marks it. Selected text
    goes there too: with [Price] selected, ROUND gives ROUND([Price])."""
    before, _, after = template.partition(CURSOR)
    cursor = edit.textCursor()
    selected = cursor.selectedText().replace(" ", "\n")  # Qt's paragraph separator
    cursor.insertText(before + selected + after)
    cursor.setPosition(cursor.position() - len(after))
    edit.setTextCursor(cursor)


class FunctionsPanel(QWidget):
    """Search, categories, the list and the details of one function. ``chosen`` is emitted for
    Enter in the search or the list, and for a double-click."""

    chosen = Signal(object)  # Function
    current_changed = Signal()

    def __init__(self, parent: QWidget | None = None, *, compact: bool = False) -> None:
        super().__init__(parent)
        self.search = QLineEdit(self)
        self.search.setPlaceholderText("Search: name or what it does, e.g. round, month" if compact else
                                       "Search by name or by what it does, e.g. round, month, similar")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._search_changed)
        self.search.returnPressed.connect(self._choose)
        self.search.installEventFilter(self)
        self._searching = False

        names = [(_ALL, len(functions.all_functions()))]
        names += [(c, sum(f.category == c for f in functions.all_functions())) for c in CATEGORIES]
        if compact:  # a narrow pane: categories in a drop-down
            self.categories: QComboBox | QListWidget = QComboBox(self)
            for name, count in names:
                self.categories.addItem(f"{name}  ({count})", name)
            self.categories.currentIndexChanged.connect(lambda _index: self._fill())
        else:
            self.categories = QListWidget(self)
            for name, count in names:
                item = QListWidgetItem(f"{name}  ({count})")
                item.setData(Qt.ItemDataRole.UserRole, name)
                self.categories.addItem(item)
            self.categories.setCurrentRow(0)
            self.categories.currentRowChanged.connect(lambda _row: self._fill())
            self.categories.setMinimumWidth(150)

        self.list = QTreeWidget(self)
        self.list.setColumnCount(2)
        self.list.setHeaderLabels(["Function", "What it does"])
        self.list.setRootIsDecorated(False)
        self.list.setUniformRowHeights(True)
        self.list.setColumnWidth(0, 150 if compact else 190)
        self.list.currentItemChanged.connect(lambda current, _previous: self._show(current))
        self.list.itemActivated.connect(lambda _item, _column: self._choose())  # Enter or double-click

        self.details = QTextBrowser(self)
        self.details.setOpenLinks(False)

        listing = QSplitter(Qt.Orientation.Vertical, self)
        listing.addWidget(self.list)
        listing.addWidget(self.details)
        listing.setSizes([340, 220])
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.search)
        if compact:
            layout.addWidget(self.categories)
            layout.addWidget(listing, 1)
        else:
            body = QSplitter(Qt.Orientation.Horizontal, self)
            body.addWidget(self.categories)
            body.addWidget(listing)
            body.setStretchFactor(1, 1)
            body.setSizes([190, 770])
            layout.addWidget(body, 1)
        self._fill()

    def current(self) -> Function | None:
        item = self.list.currentItem()
        return item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None

    def category_index(self) -> int:
        categories = self.categories
        return categories.currentIndex() if isinstance(categories, QComboBox) else categories.currentRow()

    def set_category_index(self, index: int) -> None:
        if isinstance(self.categories, QComboBox):
            self.categories.setCurrentIndex(index)
        else:
            self.categories.setCurrentRow(index)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        """Up and down in the search field move through the list, so typing and Enter is enough."""
        steps = {Qt.Key.Key_Down: 1, Qt.Key.Key_Up: -1, Qt.Key.Key_PageDown: 10, Qt.Key.Key_PageUp: -10}
        if watched is self.search and isinstance(event, QKeyEvent) and event.type() == QEvent.Type.KeyPress:
            step = steps.get(Qt.Key(event.key()))
            if step is not None and self.list.topLevelItemCount():
                row = self.list.indexOfTopLevelItem(self.list.currentItem()) if self.list.currentItem() else -1
                row = min(max(row + step, 0), self.list.topLevelItemCount() - 1)
                self.list.setCurrentItem(self.list.topLevelItem(row))
                return True
        return super().eventFilter(watched, event)

    def _choose(self) -> None:
        function = self.current()
        if function is not None:
            self.chosen.emit(function)

    def _search_changed(self, text: str) -> None:
        """A new search looks through all categories (a category picked afterwards narrows it)."""
        if text.strip() and not self._searching:
            self.categories.blockSignals(True)
            self.set_category_index(0)
            self.categories.blockSignals(False)
        self._searching = bool(text.strip())
        self._fill()

    def _fill(self) -> None:
        if isinstance(self.categories, QComboBox):
            category = self.categories.currentData() or _ALL
        else:
            item = self.categories.currentItem()
            category = item.data(Qt.ItemDataRole.UserRole) if item is not None else _ALL
        found = functions.search(self.search.text(), None if category == _ALL else category)
        self.list.clear()
        rows = []
        for function in found:
            row = QTreeWidgetItem([function.name, function.summary])
            row.setData(0, Qt.ItemDataRole.UserRole, function)
            row.setToolTip(0, function.name)
            row.setToolTip(1, function.summary)
            if function.category == MORE:
                row.setForeground(0, QColor(theme.TEXT_DIM))
            rows.append(row)
        self.list.addTopLevelItems(rows)
        if rows:
            self.list.setCurrentItem(rows[0])
        else:
            self._show(None)

    def _show(self, item: QTreeWidgetItem | None) -> None:
        function: Function | None = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None
        self.current_changed.emit()
        if function is None:
            self.details.setHtml(f"<p style='color:{theme.TEXT_DIM}'>No function matches the search.</p>")
            return
        self.details.setHtml(details_html(function))


def details_html(function: Function) -> str:
    mono = f"font-family:'{theme.monospace_font().family()}'"  # inside style="..."
    dim = f"color:{theme.TEXT_DIM}"
    summary = function.summary if function.summary.endswith((".", "!", "?", "…")) else function.summary + "."
    parts = [
        f"<h3 style='margin:0'>{html.escape(function.name)}</h3>",
        f'<p style="{mono}; color:{theme.ACCENT}">{html.escape(function.syntax)}</p>',
        f"<p>{html.escape(summary)}</p>",
    ]
    if function.note:
        parts += [f"<p>{html.escape(paragraph)}</p>" for paragraph in function.note.split("\n\n")]
    if function.example:
        parts.append(f'<p><b>Example</b><br><span style="{mono}">{html.escape(function.example)}</span></p>')
    if function.sql_example:
        caveat = " There [1, 2] is a list, not a column; columns are written without brackets." if "[" in function.sql_example else ""
        parts.append(
            f"<p><b>DuckDB's example</b> <span style='{dim}'>(plain SQL, not a formula.{caveat})</span><br>"
            f'<span style="{mono}">{html.escape(function.sql_example)}</span></p>'
        )
    if function.aliases:
        parts.append(f"<p>Also written: {html.escape(', '.join(function.aliases))}</p>")
    if function.instead_of:
        parts.append(f"<p>Use it instead of: {html.escape(', '.join(function.instead_of))} (from SQL Server or spreadsheets)</p>")
    if function.helper:
        parts.append(f"<p style='{dim}'>Added by Tafl for people who know SQL Server.</p>")
    parts.append(f"<p style='{dim}'>{html.escape(function.category)}</p>")
    return "".join(parts)


# --- the window (F1) and the formula field it inserts into ------------------------------------


class FormulaField(Protocol):
    """A formula field the window can insert into (see forms.ExpressionEditor)."""

    def insert_template(self, template: str) -> None: ...

    def apply(self) -> None: ...

    def isVisible(self) -> bool: ...


_target: weakref.ref[Any] | None = None


def remember(field: FormulaField) -> None:
    """Called when a formula field gets the focus: Insert goes there."""
    global _target
    _target = weakref.ref(field)
    for window in FunctionsWindow.open_windows():
        window.update_insert()


def target() -> FormulaField | None:
    field = _target() if _target is not None else None
    try:
        return field if field is not None and field.isVisible() else None
    except RuntimeError:  # the widget is gone
        return None


def show_functions(anchor: QWidget) -> FunctionsWindow:
    """Show the window of the app window that ``anchor`` belongs to (one per app window)."""
    top = anchor.window()
    window = getattr(top, "_functions_window", None)
    if window is None:
        window = FunctionsWindow(top)
        top._functions_window = window
    window.show()
    window.raise_()
    window.activateWindow()
    window.search.setFocus()
    window.search.selectAll()
    return window


class FunctionsWindow(QWidget):
    _instances: weakref.WeakSet[FunctionsWindow] = weakref.WeakSet()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Tool)  # stays above the app window
        FunctionsWindow._instances.add(self)
        self.setWindowTitle("Formula functions")
        self.resize(960, 600)
        self.panel = FunctionsPanel(self)
        self.panel.chosen.connect(lambda _function: self.insert())
        self.panel.current_changed.connect(self.update_insert)
        self.search, self.list, self.details, self.categories = (
            self.panel.search, self.panel.list, self.panel.details, self.panel.categories,
        )  # fmt: skip

        self.insert_button = QPushButton("Insert into the formula", self)
        self.insert_button.clicked.connect(self.insert)
        self.copy_button = QPushButton("Copy the example", self)
        self.copy_button.clicked.connect(self.copy_example)
        self.hint = QLabel(self)
        self.hint.setObjectName("Dim")
        buttons = QHBoxLayout()
        buttons.addWidget(self.hint, 1)
        buttons.addWidget(self.copy_button)
        buttons.addWidget(self.insert_button)
        layout = QVBoxLayout(self)
        layout.addWidget(self.panel, 1)
        layout.addLayout(buttons)
        self.update_insert()

    @classmethod
    def open_windows(cls) -> list[FunctionsWindow]:
        found = []
        for window in list(cls._instances):
            try:
                if window.isVisible():
                    found.append(window)
            except RuntimeError:  # deleted together with its app window
                pass
        return found

    def current(self) -> Function | None:
        return self.panel.current()

    def insert(self) -> None:
        function, field = self.current(), target()
        if function is None or field is None or not function.template:
            self.update_insert()  # e.g. the field is gone: the hint says what to do
            QApplication.beep()
            return
        field.insert_template(function.template)

    def copy_example(self) -> None:
        function = self.current()
        if function is not None and function.example:
            QGuiApplication.clipboard().setText(function.example)

    def update_insert(self) -> None:
        function, field = self.current(), target()
        self.insert_button.setEnabled(function is not None and field is not None and bool(function.template))
        if field is None:
            self.hint.setText("To insert a function, click into a formula field first.")
        elif function is not None and not function.template:
            self.hint.setText("This one is written as shown in the example.")
        else:
            self.hint.setText("Enter or a double-click inserts the function.")
        self.copy_button.setEnabled(function is not None and bool(function.example))

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)

    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.ActivationChange:
            self.update_insert()
        super().changeEvent(event)
