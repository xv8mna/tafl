"""The formula window (F2): a large editor with the columns of the step and the functions beside
it, and a preview of the result on the first rows of the data, updated while typing."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

import polars as pl
from PySide6.QtCore import QByteArray, QSettings, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QKeyEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableView,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from etl_tool.api.dtypes import type_label
from etl_tool.core.completion import Suggestion, column_suggestions, parameter_suggestions
from etl_tool.core.formula_preview import NO_DATA, Column, Preview, preview

from . import theme
from .checks import checks
from .completer import FormulaCompleter
from .formula_style import FormulaHighlighter, bracket_selections
from .frame_model import FrameModel
from .functions_window import FunctionsPanel, insert_into

_DELAY_MS = 300  # check this long after the last key


class FormulaWindow(QDialog):
    """``applied(text)`` is emitted when the formula is applied; Cancel changes nothing."""

    applied = Signal(str)

    def __init__(
        self,
        parent: QWidget,
        text: str,
        *,
        title: str,
        frame: Callable[[], pl.DataFrame | None],
        no_data: Callable[[], str] = lambda: "",
        earlier: Callable[[], Sequence[tuple[str, str]]] = lambda: (),
        condition: bool = False,
        name: str = "",
        creates: bool = False,
        parameters: Callable[[], Mapping[str, Any]] = dict,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.resize(1100, 700)
        self._frame, self._no_data, self._earlier, self._parameters = frame, no_data, earlier, parameters
        self._condition, self._name, self._creates = condition, name, creates
        self._original = text
        self._shown_text = text  # the text of the last change (coloring changes nothing)
        self._discard_warned = False
        self._columns: list[Column] = []
        self._checked: tuple[object, str] | None = None  # the input of the last check

        self.editor = QPlainTextEdit(self)
        self.editor.setFont(theme.monospace_font())
        self.editor.setPlainText(text)
        self.editor.setPlaceholderText("[Price] * [Quantity]")
        self.editor.textChanged.connect(self._text_changed)
        self.editor.setMinimumHeight(4 * self.editor.fontMetrics().lineSpacing())
        self._highlighter = FormulaHighlighter(self.editor.document())
        self._completer = FormulaCompleter(self.editor, self._column_suggestions, lambda: parameter_suggestions(parameters()))
        self.editor.cursorPositionChanged.connect(
            lambda: self.editor.setExtraSelections(bracket_selections(self.editor))
        )
        self.status = QLabel(self)
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.model = FrameModel(self)
        self.table = QTableView(self)
        self.table.setModel(self.model)
        self.table.verticalHeader().setDefaultSectionSize(self.table.fontMetrics().height() + 6)
        self.table.setMinimumHeight(80)

        self.column_search = QLineEdit(self)
        self.column_search.setPlaceholderText("Search columns")
        self.column_search.setClearButtonEnabled(True)
        self.column_search.textChanged.connect(lambda _text: self._fill_columns())
        self.column_search.returnPressed.connect(self._insert_column)
        self.column_list = QTreeWidget(self)
        self.column_list.setHeaderLabels(["Column", "Type"])
        self.column_list.setRootIsDecorated(False)
        self.column_list.setColumnWidth(0, 170)
        self.column_list.itemActivated.connect(lambda _item, _column: self._insert_column())
        columns_tab = QWidget(self)
        columns_layout = QVBoxLayout(columns_tab)
        columns_layout.setContentsMargins(0, 4, 0, 0)
        columns_layout.addWidget(self.column_search)
        columns_layout.addWidget(self.column_list, 1)
        self.functions = FunctionsPanel(self, compact=True)
        self.functions.chosen.connect(self._insert_function)
        self.tabs = QTabWidget(self)
        self.tabs.addTab(columns_tab, "Columns")
        self.tabs.addTab(self.functions, "Functions")
        self.tabs.setMinimumWidth(180)

        top = QWidget(self)
        top_layout = QVBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.addWidget(self.editor, 1)
        top_layout.addWidget(self.status)
        self.right = QSplitter(Qt.Orientation.Vertical, self)
        self.right.addWidget(top)
        self.right.addWidget(self.table)
        self.right.setSizes([330, 300])
        self.body = QSplitter(Qt.Orientation.Horizontal, self)
        self.body.addWidget(self.tabs)
        self.body.addWidget(self.right)
        self.body.setStretchFactor(1, 1)
        self.body.setSizes([330, 770])
        for splitter in (self.body, self.right):
            splitter.setChildrenCollapsible(False)  # the editor must never vanish

        hint = QLabel("Enter starts a new line · Ctrl+Enter applies · Esc cancels", self)
        hint.setObjectName("Dim")
        self.cancel_button = QPushButton("Cancel", self)
        self.apply_button = QPushButton("Apply", self)
        for button in (self.cancel_button, self.apply_button):
            button.setAutoDefault(False)  # Enter belongs to the editor and the search fields
        self.cancel_button.clicked.connect(self.reject)
        self.apply_button.clicked.connect(self.apply)
        buttons = QHBoxLayout()
        buttons.addWidget(hint, 1)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.apply_button)
        layout = QVBoxLayout(self)
        layout.addWidget(self.body, 1)
        layout.addLayout(buttons)
        for keys in ("Ctrl+Return", "Ctrl+Enter"):  # before the focused field sees the key
            QShortcut(QKeySequence(keys), self, self.apply)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(_DELAY_MS)
        self._timer.timeout.connect(self.check)
        self._check = checks().client(self._show, self._failed)  # closing never waits for it
        self._restore()
        self.editor.setFocus()
        self.editor.moveCursor(self.editor.textCursor().MoveOperation.End)
        self.check()

    @property
    def busy(self) -> bool:
        return self._timer.isActive() or self._check.busy

    def text(self) -> str:
        return self.editor.toPlainText()

    def check(self) -> None:
        """Evaluate the formula on the first rows (in the background)."""
        self._timer.stop()
        if not self._check.alive:  # closed
            return
        frame, earlier, text = self._frame(), list(self._earlier()), self.text()
        no_data = (self._no_data() or NO_DATA) if frame is None else ""
        self._checked = (frame, no_data)
        condition, name, creates = self._condition, self._name, self._creates
        parameters = dict(self._parameters())
        self._highlighter.set_parameters(parameters)
        self._check.start(
            lambda cancel: preview(
                frame, text, earlier, condition=condition, name=name, creates=creates, cancel=cancel, no_data=no_data,
                parameters=parameters,
            )  # fmt: skip
        )

    def inputs_changed(self) -> None:
        """The results of the steps changed: check again if the data going into the step did."""
        frame = self._frame()
        if self._checked is None or frame is not self._checked[0] or (frame is None and self._no_data() != self._checked[1]):
            self._timer.start()

    def apply(self) -> None:
        self.applied.emit(self.text())
        self.accept()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        if key == Qt.Key.Key_Escape:
            if self.text() != self._original and not self._discard_warned:  # don't lose typing by accident
                self._discard_warned = True
                self._set_status(False, "Press Esc again to discard your changes.")
                return
            self.reject()
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):  # Enter never closes the window by itself
            focused = self.focusWidget()
            if isinstance(focused, QPushButton):  # but it presses the button that has the focus
                focused.click()
            return
        super().keyPressEvent(event)

    def done(self, result: int) -> None:
        self._timer.stop()
        if self._check.alive:
            self._check.close()  # stops the preview that is still running, without waiting for it
            self._save()
        super().done(result)

    def _text_changed(self) -> None:
        if self.text() == self._shown_text:  # e.g. coloring the text again: nothing was typed
            return
        self._shown_text = self.text()
        self._discard_warned = False
        self._timer.start()

    def _show(self, result: Preview) -> None:
        if self._timer.isActive():  # typing went on: a newer check follows
            return
        if not self._discard_warned:  # the Esc warning stays until the next key
            self._set_status(result.ok, result.message)
        self.model.set_frame(result.rows if result.rows is not None else pl.DataFrame())
        self.table.resizeColumnsToContents()
        if [(c.name, c.type, c.row) for c in result.columns] != [(c.name, c.type, c.row) for c in self._columns]:
            self._columns = result.columns
            self._fill_columns()
            self._highlighter.set_columns([c.name for c in result.columns] or None)
            self._completer.refresh()

    def _failed(self, details: str) -> None:
        if not self._discard_warned:
            self._set_status(False, "The preview failed: " + details.strip().splitlines()[-1])

    def _set_status(self, ok: bool, text: str) -> None:
        color = theme.TEXT_DIM if ok else theme.STATUS_COLORS["error"]
        self.status.setStyleSheet(f"color: {color};")
        self.status.setText(text)

    def _column_suggestions(self) -> list[Suggestion]:
        if self._columns:  # with the rows above
            return column_suggestions([(c.name, c.type, c.row) for c in self._columns])
        frame = self._frame()  # before the first preview: the input columns
        return column_suggestions([(n, type_label(t), None) for n, t in frame.schema.items()]) if frame is not None else []

    def _fill_columns(self) -> None:
        search = self.column_search.text().strip().lower()
        self.column_list.clear()
        items = []
        for column in self._columns:
            if search and search not in column.name.lower():
                continue
            kind = column.type if column.row is None else f"{column.type} (row {column.row})"
            item = QTreeWidgetItem([column.name, kind])
            item.setData(0, Qt.ItemDataRole.UserRole, column.name)
            if column.row is not None:
                item.setForeground(1, QColor(theme.ACCENT))
            items.append(item)
        self.column_list.addTopLevelItems(items)
        if items:
            self.column_list.setCurrentItem(items[0])

    def _insert_column(self) -> None:
        item = self.column_list.currentItem()
        if item is None:
            return
        name = item.data(0, Qt.ItemDataRole.UserRole)
        self.editor.insertPlainText("[" + name.replace("]", "]]") + "]")
        self.editor.setFocus()

    def _insert_function(self, function) -> None:
        if function.template:
            insert_into(self.editor, function.template)
            self.editor.setFocus()

    def _restore(self) -> None:
        settings = QSettings()
        geometry = settings.value("formula_window/geometry")
        if isinstance(geometry, QByteArray):
            self.restoreGeometry(geometry)
        for splitter, key in ((self.body, "body"), (self.right, "right")):
            state = settings.value(f"formula_window/{key}")
            if isinstance(state, QByteArray):
                splitter.restoreState(state)

    def _save(self) -> None:
        settings = QSettings()
        settings.setValue("formula_window/geometry", self.saveGeometry())
        settings.setValue("formula_window/body", self.body.saveState())
        settings.setValue("formula_window/right", self.right.saveState())
