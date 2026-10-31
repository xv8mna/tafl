"""The data sheet: a window to paste or type data that is not in a file. Its data goes into a file, and
a normal table with one import step reads that file (see ``tafl.core.sheet``).

A sheet is a draft until it is added as a table. "Edit in sheet" opens such a file again: the data
as it is in the file, not after later steps. The window takes no part in the project's undo: it has
its own, and the project holds nothing of the data.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import polars as pl
from PySide6.QtCore import (
    QAbstractTableModel,
    QEvent,
    QItemSelection,
    QItemSelectionModel,
    QModelIndex,
    QPersistentModelIndex,
    QPoint,
    QRect,
    QSettings,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QAction, QCloseEvent, QGuiApplication, QKeyEvent, QKeySequence, QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemDelegate,
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QStyledItemDelegate,
    QTableView,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from tafl import APP_NAME
from tafl.api import ActionError, files
from tafl.core.sheet import (
    FORMATS,
    XLSX_CELLS_PER_SECOND,
    XLSX_READ_BYTES,
    XLSX_SLOW_CELLS,
    Pasted,
    Ranges,
    Sheet,
    SheetError,
    SheetFormat,
    format_problem,
    parse_text,
    read_file,
    stamp_of,
    write_file,
)

from . import data_panel, theme
from .frame_model import excel_text
from .labels import ElidedLabel, tooltip_text
from .session import Session, count
from .worker import Worker

Index = QModelIndex | QPersistentModelIndex
_ROOT = QModelIndex()
_FLAGS = (
    Qt.ItemFlag.ItemIsEnabled
    | Qt.ItemFlag.ItemIsSelectable
    | Qt.ItemFlag.ItemIsEditable
    | Qt.ItemFlag.ItemNeverHasChildren
)

_MAX_TEXT = 500  # characters a cell shows
_LINE_BREAK = "↵"  # a cell stays one line
_TIP_FROM = 40  # characters from which a cell has a tooltip
_WIDEST_COLUMN = 320
_NARROWEST_COLUMN = 90
_FITTED_ROWS = 200
_FITTED_COLUMNS = 300  # the others keep the default width: there can be thousands
_MEASURED_TEXT = 80  # characters measured of a cell: more never fit into the widest column
_EDITOR_LINES = 5
_EDITOR_WIDTH = 160
_EDITOR_MARGIN = 2

_PASTE_AT_ONCE = 5_000_000  # more characters are read in the background
_KEEP_TEXT = 50_000_000  # more characters are not kept for "Paste as one column": they would double the memory
_SPLIT_AT = {";": "semicolons", ",": "commas", "|": "pipes"}

_NUMBERED = re.compile(r"(.*?) (\d+)")


class SheetModel(QAbstractTableModel):
    """The grid's view of a sheet. Only the cells on screen are read. The sheet is changed first,
    and ``sync`` then brings the grid up to date: a reset only when the columns differ, because a
    reset loses the widths of the columns and the selection."""

    edited = Signal()  # a cell was typed into

    def __init__(self, sheet: Sheet, parent: Any = None) -> None:
        super().__init__(parent)
        self.sheet = sheet
        self._rows, self._columns = sheet.row_count, sheet.column_count

    def set_sheet(self, sheet: Sheet) -> None:
        self.beginResetModel()
        self.sheet = sheet
        self._rows, self._columns = sheet.row_count, sheet.column_count
        self.endResetModel()

    def sync(self) -> None:
        rows, columns = self.sheet.row_count, self.sheet.column_count
        if columns != self._columns:
            self.set_sheet(self.sheet)
            return
        if rows > self._rows:
            self.beginInsertRows(_ROOT, self._rows, rows - 1)
            self._rows = rows
            self.endInsertRows()
        elif rows < self._rows:
            self.beginRemoveRows(_ROOT, rows, self._rows - 1)
            self._rows = rows
            self.endRemoveRows()
        if rows and columns:
            self.dataChanged.emit(self.index(0, 0), self.index(rows - 1, columns - 1))
        self.headerDataChanged.emit(Qt.Orientation.Horizontal, 0, columns - 1)

    def rowCount(self, parent: Index = _ROOT) -> int:
        return 0 if parent.isValid() else self._rows

    def columnCount(self, parent: Index = _ROOT) -> int:
        return 0 if parent.isValid() else self._columns

    def flags(self, index: Index) -> Qt.ItemFlag:
        return _FLAGS if index.isValid() else Qt.ItemFlag.NoItemFlags

    def data(self, index: Index, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            text = self.sheet.text(index.row(), index.column())
            if len(text) > _MAX_TEXT:
                text = text[:_MAX_TEXT] + "…"
            return text.replace("\n", _LINE_BREAK)
        if role == Qt.ItemDataRole.EditRole:
            return self.sheet.text(index.row(), index.column())
        if role == Qt.ItemDataRole.ToolTipRole:
            text = self.sheet.text(index.row(), index.column())
            return tooltip_text(text) if len(text) > _TIP_FROM else None
        return None

    def setData(self, index: Index, value: Any, role: int = Qt.ItemDataRole.EditRole) -> bool:
        if not index.isValid() or role != Qt.ItemDataRole.EditRole:
            return False
        self.sheet.set_text(index.row(), index.column(), value)
        self.dataChanged.emit(index, index)
        self.edited.emit()
        return True

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        horizontal = orientation == Qt.Orientation.Horizontal
        if role == Qt.ItemDataRole.DisplayRole:
            if not horizontal:
                return str(section + 1)
            names = self.sheet.names
            return names[section] if section < len(names) else None
        if role == Qt.ItemDataRole.TextAlignmentRole and not horizontal:
            return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        return None


class _CellEditor(QPlainTextEdit):
    """The cell editor: a QLineEdit would lose the line breaks of a cell. It grows with its lines."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.replacing = False  # the key that opened it replaces the text of the cell
        self._cell = QRect()
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setStyleSheet(f"border: 1px solid {theme.ACCENT};")
        self.document().setDocumentMargin(_EDITOR_MARGIN)
        self.document().blockCountChanged.connect(lambda _count: self._fit())

    def text(self) -> str:
        """The text as it is: ``toPlainText`` turns a no-break space into a space."""
        return self.document().toRawText().replace(" ", "\n")

    def set_text(self, text: str) -> None:
        self.setPlainText(text)
        self.moveCursor(QTextCursor.MoveOperation.End)

    def place(self, cell: QRect) -> None:
        self._cell = QRect(cell)
        self._fit()

    def _fit(self) -> None:
        """At least the cell, at most five lines, inside the grid."""
        lines = min(self.document().blockCount(), _EDITOR_LINES)
        height = max(self._cell.height(), lines * self.fontMetrics().lineSpacing() + 2 * _EDITOR_MARGIN + 4)
        area = self.parentWidget().rect()
        rect = QRect(self._cell.x(), self._cell.y(), max(self._cell.width(), _EDITOR_WIDTH), height)
        rect.setSize(rect.size().boundedTo(area.size()))
        rect.moveRight(min(rect.right(), area.right()))
        rect.moveBottom(min(rect.bottom(), area.bottom()))
        rect.moveLeft(max(rect.left(), 0))
        rect.moveTop(max(rect.top(), 0))
        self.setGeometry(rect)
        if self.document().blockCount() <= _EDITOR_LINES:
            self.verticalScrollBar().setValue(0)  # all lines are in view: from the first one


class _CellDelegate(QStyledItemDelegate):
    moved = Signal(int, int)  # the editor was left with a key: rows and columns to go
    fill_wanted = Signal(str)  # Ctrl+Enter: the text for all selected cells

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.replacing = False  # the next editor opens for a typed character

    def createEditor(self, parent: QWidget, _option: Any, _index: Index) -> QWidget:
        editor = _CellEditor(parent)
        editor.replacing, self.replacing = self.replacing, False
        return editor

    def setEditorData(self, editor: _CellEditor, index: Index) -> None:
        editor.set_text("" if editor.replacing else index.data(Qt.ItemDataRole.EditRole))
        editor.replacing = False

    def setModelData(self, editor: _CellEditor, model: SheetModel, index: Index) -> None:
        if editor.document().isModified():  # a cell that was not changed stays exactly as it was
            model.setData(index, editor.text(), Qt.ItemDataRole.EditRole)

    def updateEditorGeometry(self, editor: _CellEditor, option: Any, _index: Index) -> None:
        editor.place(option.rect)

    def eventFilter(self, editor: Any, event: QEvent) -> bool:
        if event.type() == QEvent.Type.KeyPress and self._key(editor, event):
            return True
        return super().eventFilter(editor, event)

    def _key(self, editor: _CellEditor, event: QKeyEvent) -> bool:
        key, modifiers = event.key(), event.modifiers()
        shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if modifiers & Qt.KeyboardModifier.ControlModifier:
                text = editor.text()
                self.closeEditor.emit(editor, QAbstractItemDelegate.EndEditHint.NoHint)
                self.fill_wanted.emit(text)
            elif shift:
                editor.insertPlainText("\n")
            else:
                self.finish(editor, 1, 0)
        elif key == Qt.Key.Key_Backtab or (key == Qt.Key.Key_Tab and shift):
            self.finish(editor, 0, -1)
        elif key == Qt.Key.Key_Tab:
            self.finish(editor, 0, 1)
        elif key in (Qt.Key.Key_Up, Qt.Key.Key_Down) and not shift and editor.document().blockCount() == 1:
            self.finish(editor, -1 if key == Qt.Key.Key_Up else 1, 0)
        else:
            return False
        return True

    def finish(self, editor: _CellEditor, rows: int = 0, columns: int = 0) -> None:
        """Apply the text and close the editor; the grid then goes ``rows`` and ``columns`` on."""
        self.commitData.emit(editor)
        self.closeEditor.emit(editor, QAbstractItemDelegate.EndEditHint.NoHint)
        if rows or columns:
            self.moved.emit(rows, columns)


class _Grid(QTableView):
    def __init__(self, host: SheetWindow) -> None:
        super().__init__(host)
        self.host = host
        model = host.model
        self.setModel(model)
        self.delegate = _CellDelegate(self)
        self.delegate.moved.connect(self.go_on)
        self.delegate.fill_wanted.connect(host.fill_with)
        self.setItemDelegate(self.delegate)
        self.setAlternatingRowColors(True)
        self.setWordWrap(False)
        self.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
            | QAbstractItemView.EditTrigger.AnyKeyPressed
        )
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._cell_menu)
        vertical = self.verticalHeader()
        vertical.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        vertical.setDefaultSectionSize(self.fontMetrics().height() + 6)
        vertical.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        vertical.customContextMenuRequested.connect(self._row_menu)
        horizontal = self.horizontalHeader()
        horizontal.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        horizontal.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        horizontal.customContextMenuRequested.connect(self._column_menu)
        horizontal.sectionDoubleClicked.connect(host.rename_column)
        # As in the data grid: a header with the grid's selection asks about every row after Ctrl+A
        for header in (horizontal, vertical):
            header.setSelectionModel(QItemSelectionModel(model, header))
        self._widths: list[int] = []
        self._width_count = 0  # columns at that moment: names and widths stop at _FITTED_COLUMNS
        self._width_names: list[str] = []  # as the grid shows them: at a reset, the model has new ones already
        model.modelAboutToBeReset.connect(self._remember_widths)
        model.modelReset.connect(self._restore_widths)
        model.headerDataChanged.connect(self._note_names)

    # --- columns

    def fit_columns(self, first: int = 0) -> None:
        for column in range(first, min(self.model().columnCount(), _FITTED_COLUMNS)):
            self._fit_column(column)
        self._width_names = self._column_names()

    def _fit_column(self, column: int) -> None:
        model = self.model()
        cells, header = self.fontMetrics(), self.horizontalHeader().fontMetrics()
        width = header.horizontalAdvance(model.headerData(column, Qt.Orientation.Horizontal) or "") + 24
        for row in range(min(model.rowCount(), _FITTED_ROWS)):
            text = model.data(model.index(row, column))[:_MEASURED_TEXT]
            width = max(width, cells.horizontalAdvance(text) + 16)
        self.setColumnWidth(column, max(_NARROWEST_COLUMN, min(width, _WIDEST_COLUMN)))

    def _column_names(self) -> list[str]:
        return self.model().sheet.names[:_FITTED_COLUMNS]

    def _note_names(self) -> None:
        self._width_names = self._column_names()

    def _remember_widths(self) -> None:
        self._width_count = self.model().columnCount()  # the old count: only the reset changes it
        self._widths = [self.columnWidth(c) for c in range(min(self._width_count, _FITTED_COLUMNS))]

    def _restore_widths(self) -> None:
        """Each column keeps its width: by its place, or by its name after columns came or went."""
        names = self._column_names()
        if self.model().columnCount() == self._width_count:
            for column, width in enumerate(self._widths):
                self.setColumnWidth(column, width)
        else:
            known = dict(zip(self._width_names, self._widths, strict=False))
            for column, name in enumerate(names):
                if name in known:
                    self.setColumnWidth(column, known[name])
                else:
                    self._fit_column(column)
        self._width_names = names

    # --- moving

    def cell(self) -> tuple[int, int]:
        """The row and column of the current cell, -1 for none."""
        index = self.currentIndex()
        return index.row(), index.column()

    def go_to(self, row: int, column: int) -> None:
        """Make this cell current, or the nearest one. A cell that is current already keeps the
        selection around it."""
        model = self.model()
        row, column = min(max(row, 0), model.rowCount() - 1), min(max(column, 0), model.columnCount() - 1)
        index = model.index(row, column)
        if index.isValid() and index != self.currentIndex():
            # Not setCurrentIndex: with Shift or Ctrl held down, it adds to the selection
            self.selectionModel().setCurrentIndex(index, QItemSelectionModel.SelectionFlag.ClearAndSelect)

    def go_on(self, rows: int, columns: int) -> None:
        """Enter and Tab: one cell on. Tab goes from the end of a row to the next row, and a step
        past the last row adds a row."""
        row, column = self.cell()
        if row < 0:
            return
        last = self.model().columnCount() - 1
        row, column = row + rows, column + columns
        if column > last:
            row, column = row + 1, 0
        elif column < 0:
            row, column = (row - 1, last) if row > 0 else (0, 0)
        if row >= self.model().rowCount() and not self.host.add_last_row():
            return
        self.go_to(row, column)

    # --- keys

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key, modifiers = event.key(), event.modifiers()
        control = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        alt_gr = control and bool(modifiers & Qt.KeyboardModifier.AltModifier)  # in Windows, AltGr is Ctrl+Alt
        typed = bool(event.text()) and event.text().isprintable()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.go_on(-1 if shift else 1, 0)
        elif key == Qt.Key.Key_Backtab or (key == Qt.Key.Key_Tab and shift):
            self.go_on(0, -1)
        elif key == Qt.Key.Key_Tab:
            self.go_on(0, 1)
        elif key == Qt.Key.Key_Delete:
            self.host.clear_cells()
        elif key == Qt.Key.Key_Space and control:
            self.select_lines(columns=True)
        elif key == Qt.Key.Key_Space and shift:
            self.select_lines(columns=False)
        elif typed and alt_gr:  # @ or € on many keyboards: Qt doesn't start editing for those
            self.delegate.replacing = True
            self.edit(self.currentIndex(), QAbstractItemView.EditTrigger.AnyKeyPressed, event)
        else:
            self.delegate.replacing = typed and not control
            super().keyPressEvent(event)
        self.delegate.replacing = False

    def select_lines(self, *, columns: bool) -> None:
        """All of the columns (or rows) that have a selected cell."""
        model = self.model()
        rows, width = model.rowCount(), model.columnCount()
        if not rows or not width:
            return
        selection = QItemSelection()
        for top, bottom, left, right in self.host.ranges():
            if columns:
                selection.select(model.index(0, left), model.index(rows - 1, right))
            else:
                selection.select(model.index(top, 0), model.index(bottom, width - 1))
        self.selectionModel().select(selection, QItemSelectionModel.SelectionFlag.ClearAndSelect)

    def apply_typing(self) -> None:
        """Apply what is typed in an open editor, and close it."""
        editor = self.indexWidget(self.currentIndex())
        if editor is not None:
            self.delegate.finish(editor)

    # --- menus

    def cell_menu(self) -> QMenu:
        host = self.host
        menu = QMenu(self)
        menu.addActions([host.copy_action, host.paste_action, host.fill_action, host.clear_action])
        menu.addSeparator()
        menu.addActions([host.row_above_action, host.row_below_action, host.delete_rows_action])
        return menu

    def row_menu(self) -> QMenu:
        host = self.host
        menu = QMenu(self)
        menu.addActions([host.row_above_action, host.row_below_action, host.delete_rows_action])
        return menu

    def column_menu(self, column: int) -> QMenu:
        host = self.host
        menu = QMenu(self)
        menu.addAction("Rename…", lambda: host.rename_column(column))
        menu.addActions([host.column_left_action, host.column_right_action, host.delete_columns_action])
        return menu

    def _cell_menu(self, position: QPoint) -> None:
        self.cell_menu().exec(self.viewport().mapToGlobal(position))

    def _row_menu(self, position: QPoint) -> None:
        header = self.verticalHeader()
        row = header.logicalIndexAt(position)
        if row >= 0:
            if not any(top <= row <= bottom for top, bottom, _, _ in self.host.ranges()):
                self.selectRow(row)
            self.row_menu().exec(header.mapToGlobal(position))

    def _column_menu(self, position: QPoint) -> None:
        header = self.horizontalHeader()
        column = header.logicalIndexAt(position)
        if column >= 0:
            if not any(left <= column <= right for _, _, left, right in self.host.ranges()):
                self.selectColumn(column)
            self.column_menu(column).exec(header.mapToGlobal(position))


class AddSheetDialog(QDialog):
    """The name, format and folder of the file a sheet goes into. It stays open on a problem."""

    def __init__(
        self,
        sheet: Sheet,
        name: str,
        folder: Path,
        unsaved: bool,
        parent: QWidget | None = None,
        path_problem: Callable[[Path], str | None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add as table")
        self.setMinimumWidth(520)
        self.sheet = sheet
        self._unsaved = unsaved
        self._path_problem = path_problem  # why a step could not read this path back
        self.name = QLineEdit(self)
        self.format = QComboBox(self)
        for sheet_format in FORMATS.values():
            self.format.addItem(sheet_format.label, sheet_format.suffix)
        self.folder = QLineEdit(str(folder), self)
        browse = QPushButton("Browse…", self)
        browse.setAutoDefault(False)
        browse.clicked.connect(self.choose_folder)
        where = QHBoxLayout()
        where.addWidget(self.folder, 1)
        where.addWidget(browse)
        form = QFormLayout()
        form.addRow("File name", self.name)
        form.addRow("Format", self.format)
        form.addRow("Folder", where)
        hint = QLabel("The data stays in this file: the project only reads it.", self)
        hint.setObjectName("Dim")
        self.note = QLabel(self)
        self.note.setObjectName("Dim")
        self.note.setWordWrap(True)
        self.problem = QLabel(self)
        self.problem.setStyleSheet(f"color: {theme.ERROR_TEXT};")
        self.problem.setWordWrap(True)
        self.problem.hide()
        buttons = QDialogButtonBox(self)
        add = buttons.addButton("Add as table", QDialogButtonBox.ButtonRole.AcceptRole)
        add.setDefault(True)
        buttons.addButton("Cancel", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(hint)
        layout.addWidget(self.note)
        layout.addWidget(self.problem)
        layout.addWidget(buttons)
        self.set_name(name)
        self.name.selectAll()
        self.format.currentIndexChanged.connect(self._format_changed)
        self.folder.textEdited.connect(lambda _text: self._free_name())
        self.name.editingFinished.connect(self._take_suffix)
        self._show_note()

    def set_name(self, name: str) -> None:
        self._wanted = name
        self._taken = ""  # what the name field held after it gave up a suffix
        self._free_name()

    def set_suffix(self, suffix: str) -> None:
        self.format.setCurrentIndex(self.format.findData(suffix))

    def show_choice(self, path: Path, suffix: str) -> None:
        """What the popup gave last time. It gave up its suffix then: report.csv.txt stays that."""
        self.set_suffix(suffix)
        self._taken = path.stem
        self.name.setText(path.stem)
        self.name.setModified(True)

    def choose_folder(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Folder", self.folder.text())
        if chosen:
            self.folder.setText(str(Path(chosen)))
            self._free_name()

    def chosen(self) -> tuple[Path, str]:
        """The file and its format, once the dialog was accepted."""
        return self._folder() / (self.name.text().strip() + self.format.currentData()), self.format.currentData()

    def accept(self) -> None:
        problem = self.check()
        self.problem.setText(problem or "")
        self.problem.setVisible(bool(problem))
        if not problem:
            super().accept()

    def check(self) -> str | None:
        self._take_suffix()
        name, suffix = self.name.text().strip(), self.format.currentData()
        if not name.strip("."):
            return "Enter a file name."
        if any(character in name for character in "/\\"):
            return "A file name can't contain / or \\: the Folder says where the file goes."
        if problem := files.name_problem(name + suffix):
            return problem
        if any(character in name for character in "{}%"):
            return "A file name here can't contain { } or %: they have a meaning in the settings of a step."
        if name.startswith("~"):
            return "A file name here can't start with ~: it means your home folder in the settings of a step."
        folder = self._folder()
        if not folder.is_absolute():
            return "Enter the full path of a folder."
        try:
            files.check_folder(folder)
        except ActionError as error:
            return str(error)
        if self._path_problem is not None and (problem := self._path_problem(self.chosen()[0])):
            return problem
        return format_problem(self.sheet.frame(), suffix)

    def _folder(self) -> Path:
        return Path(self.folder.text().strip())

    def _format_changed(self) -> None:
        self._free_name()
        self._show_note()

    def _take_suffix(self) -> None:
        """A typed orders.csv chooses the format. Once: report.csv.txt stays report.csv as a .txt."""
        text = self.name.text().strip()
        suffix = Path(text).suffix.lower()
        if suffix in FORMATS and text != self._taken and text.lower().endswith(suffix):
            self._taken = text[: -len(suffix)]
            self.name.setText(self._taken)
            self.name.setModified(True)
            self.set_suffix(suffix)

    def _free_name(self) -> None:
        """The first free number for the name given, until another name was typed."""
        if self.name.isModified():
            return
        suffix, found = self.format.currentData(), _NUMBERED.fullmatch(self._wanted)
        base, number = (found[1], int(found[2])) if found else (self._wanted, 1)
        name = self._wanted
        while (self._folder() / (name + suffix)).exists():
            number += 1
            name = f"{base} {number}"
        self.name.setText(name)

    def _show_note(self) -> None:
        cells = self.sheet.row_count * self.sheet.column_count
        lines = ["The project is not saved yet: it keeps the full path of this file."] if self._unsaved else []
        if self.format.currentData() == ".xlsx" and cells > XLSX_SLOW_CELLS:
            time = _duration(cells / XLSX_CELLS_PER_SECOND)
            memory = cells * XLSX_READ_BYTES / 1024**3
            lines.append(
                f"This sheet has {cells:,} cells: writing .xlsx takes about {time}, "
                f"and reading it takes about {memory:.1f} GB of memory. CSV and Parquet take seconds."
            )
        self.note.setText("\n".join(lines))
        self.note.setVisible(bool(lines))


class SheetWindow(QMainWindow):
    """A sheet in a window of its own: a draft (``file`` is None) until it is added as a table, or
    a file a sheet wrote (Edit in sheet), which ``load_file`` reads. Questions and file dialogs are
    methods, so that tests can replace them."""

    message = Signal(str)  # for the status bar of the main window
    closed = Signal(object)  # this window, while it closes
    _write_progress = Signal(float)  # 0 to 1, from the background

    def __init__(
        self,
        session: Session,
        parent: QWidget | None = None,
        *,
        title: str = "Sheet 1",
        position: tuple[float, float] | None = None,
        group: str | None = None,
        file: Path | None = None,
        file_format: SheetFormat | None = None,
    ) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowIcon(theme.app_icon())
        self.session = session
        self.title = file.name if file is not None else title
        self.file = file
        self.file_format = file_format
        self._position, self._group = position, group
        self._stamp: str | None = None  # the file as it was read or written here
        self._message = ""  # what the status line says about the last thing done
        self._note = ""  # what it says when there is nothing newer
        self._one_column: tuple[str, int, int] | None = None  # the last pasted text, and where it went
        self._paste_step = False  # the last paste made an undo step: Paste as one column replaces it
        self._done: Callable[[Any], None] | None = None  # what follows the job in the background
        self._job_text = ""
        self._close_refused = ""  # why the window stayed open: the progress of the job goes on saying it
        self._reading = False
        self._closing = False  # no more questions
        self._close_after_write = False
        self.closes_app = False  # the app was closing: it goes on after this window
        self.answered_state: int | None = None  # how the sheet was when closing the app passed it, until the next try
        self._asking = False  # the question about closing is open
        self._choice: tuple[Path, str] | None = None  # what the Add as table popup last gave

        self.model = SheetModel(Sheet(columns=1, rows=0) if file is not None else Sheet(), self)
        self.model.edited.connect(self._changed)
        self._create_actions()
        self.grid = _Grid(self)
        self._worker = Worker(self)
        self._worker.finished.connect(self._job_done)
        self._worker.failed.connect(self._job_failed)
        self._worker.cancelled.connect(self._job_stopped)
        self._copier = Worker(self)
        self._copier.finished.connect(self._copied)
        self._copier.failed.connect(lambda text: self._status("Copying failed: " + text.strip().splitlines()[-1]))
        self._write_progress.connect(self._show_progress)

        self.toolbar = self._create_toolbar()
        self.switch = QCheckBox("First row has the column names", self)
        self.switch.setToolTip("On: the first row holds the column names and is not part of the data.")
        self.switch.toggled.connect(self._switch_toggled)
        self.switch.setVisible(self.is_draft)
        hint = ElidedLabel(self)
        hint.setObjectName("Dim")
        hint.setText(
            "Every value stays text, exactly as it is here. To turn a column into numbers or dates, "
            "add a Change type step to the table."
        )
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(16)
        top.addWidget(self.switch)
        top.addWidget(hint, 1)

        self.status_line = ElidedLabel(self)
        self.status_line.setObjectName("Dim")
        self.one_column_button = QPushButton("Paste as one column", self)
        self.one_column_button.setToolTip("The pasted lines without splitting them into columns")
        self.one_column_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # the typing goes on in the grid
        self.one_column_button.clicked.connect(self.paste_as_one_column)
        self.one_column_button.hide()
        self.stop_button = QPushButton("Stop", self)
        self.stop_button.setToolTip("Stop writing: the old file stays as it was")
        self.stop_button.clicked.connect(self.stop)
        self.stop_button.hide()
        self.main_button = QPushButton(self.main_action.text(), self)
        self.main_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.main_button.clicked.connect(self.main_action.trigger)
        bottom = QHBoxLayout()
        bottom.setContentsMargins(0, 0, 0, 0)
        bottom.addWidget(self.status_line, 1)
        bottom.addWidget(self.one_column_button)
        bottom.addWidget(self.stop_button)
        bottom.addWidget(self.main_button)

        body = QWidget(self)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(6, 4, 6, 6)
        layout.setSpacing(4)
        layout.addLayout(top)
        layout.addWidget(self.grid, 1)
        layout.addLayout(bottom)
        self.setCentralWidget(body)

        if self.file is not None:
            session.project_changed.connect(self._project_changed)
        geometry = QSettings().value("sheet_geometry")
        if geometry is None or not self.restoreGeometry(geometry):
            self.resize(1000, 640)
        self.grid.fit_columns()
        self.grid.go_to(0, 0)
        self.grid.setFocus()
        self._show_state()

    # --- what the window shows ---------------------------------------------------------------------

    @property
    def sheet(self) -> Sheet:
        return self.model.sheet

    @property
    def is_draft(self) -> bool:
        return self.file is None

    @property
    def busy(self) -> bool:
        return self._worker.busy

    @property
    def writing(self) -> bool:
        return self.busy and not self.stop_button.isHidden()

    @property
    def status_text(self) -> str:
        return self._message or self._note

    def shows_file(self, path: Path) -> bool:
        return self.file is not None and os.path.normcase(self.file) == os.path.normcase(path)

    def _show_state(self) -> None:
        sheet = self.sheet
        self.switch.blockSignals(True)
        self.switch.setChecked(sheet.header)
        self.switch.blockSignals(False)
        self.undo_action.setEnabled(sheet.can_undo and not self.busy)
        self.redo_action.setEnabled(sheet.can_redo and not self.busy)
        changed = sheet.changed if self.file is not None else sheet.changed and sheet.has_values
        self.setWindowTitle(f"{self.title}{' •' if changed else ''}  —  {APP_NAME}")
        self._show_status()

    def _show_status(self) -> None:
        size = f"{count(self.sheet.row_count, 'row')} × {count(self.sheet.column_count, 'column')}"
        self.status_line.setText(f"{size}  ·  {self.status_text}" if self.status_text else size)

    def _status(self, text: str) -> None:
        self._message = text
        self._show_status()

    def _changed(self) -> None:
        """After every edit of the sheet: what was only for the last paste ends."""
        self._one_column = None
        self.one_column_button.hide()
        self._message = ""
        self._show_state()

    def _edit(self, change: Callable[[], Any]) -> bool:
        """One edit of the sheet, with what follows: the grid, the labels, the current cell. A
        ``SheetError`` goes to the status line, and the sheet stays as it was."""
        row, column = self.grid.cell()
        try:
            change()
        except SheetError as error:
            self._status(str(error))
            return False
        self._show_edit(row, column)
        return True

    def _show_edit(self, row: int, column: int) -> None:
        self.model.sync()
        self._changed()
        self.grid.go_to(row, column)

    # --- actions -----------------------------------------------------------------------------------

    def _act(self, text: str, slot: Callable[[], Any], shortcut: Any = None, tip: str = "") -> QAction:
        """An action of this window. What is typed in a cell is applied first: the toolbar's
        buttons leave the keyboard focus in it, and so do shortcuts."""
        action = QAction(text, self)

        def run() -> None:
            self.apply_typing()
            slot()

        action.triggered.connect(lambda _checked=False: run())
        if isinstance(shortcut, list):
            action.setShortcuts(shortcut)
        elif shortcut is not None:
            action.setShortcut(shortcut)
        action.setToolTip(tip or text)
        self.addAction(action)
        return action

    def _create_actions(self) -> None:
        act = self._act
        self.paste_action = act(
            "Paste", self.paste, QKeySequence.StandardKey.Paste, "Paste the clipboard at the selected cell (Ctrl+V)"
        )
        self.paste_below_action = act(
            "Paste below", self.paste_below, tip="Paste the clipboard below the last row with a value"
        )
        self.undo_action = act(
            "Undo", self.undo, QKeySequence.StandardKey.Undo, "Undo the last change in this sheet (Ctrl+Z)"
        )
        self.redo_action = act(
            "Redo", self.redo, [QKeySequence("Ctrl+Y"), QKeySequence("Ctrl+Shift+Z")], "Redo the change (Ctrl+Y)"
        )
        self.add_row_action = act("+ Row", self.add_row, tip="Add a row below the selected cell")
        self.add_column_action = act("+ Column", self.add_column, tip="Add a column to the right of the selected cell")
        self.delete_rows_action = act("Delete rows", self.delete_rows, tip="Delete the rows of the selected cells")
        self.delete_columns_action = act(
            "Delete columns", self.delete_columns, tip="Delete the columns of the selected cells"
        )
        self.fill_action = act("Fill…", self.fill, tip="Write one value into all selected cells")
        self.fill_action.setIconText("Fill…")  # a tool button leaves out the dots of the text
        self.copy_action = act("Copy", self.copy_selection, QKeySequence.StandardKey.Copy)
        self.cut_action = act("Cut", self.cut, QKeySequence.StandardKey.Cut)
        self.clear_action = act("Clear", self.clear_cells)
        self.fill_down_action = act("Fill down", self.fill_down, QKeySequence("Ctrl+D"))
        self.fill_right_action = act("Fill right", self.fill_right, QKeySequence("Ctrl+R"))
        self.row_above_action = act("Insert row above", self.insert_row_above)
        self.row_below_action = act("Insert row below", self.add_row)
        self.column_left_action = act("Insert column left", self.insert_column_left)
        self.column_right_action = act("Insert column right", self.add_column)
        self.main_action = act(
            "Add as table…" if self.is_draft else "Save",
            self.add_as_table if self.is_draft else self.save,
            QKeySequence.StandardKey.Save,
        )

    def _create_toolbar(self) -> QToolBar:
        toolbar = self.addToolBar("Sheet")
        toolbar.setObjectName("SheetToolbar")
        toolbar.setMovable(False)
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        groups = (
            (self.paste_action, self.paste_below_action),
            (self.undo_action, self.redo_action),
            (self.add_row_action, self.add_column_action, self.delete_rows_action, self.delete_columns_action),
            (self.fill_action,),
        )
        for number, group in enumerate(groups):
            if number:
                toolbar.addSeparator()
            toolbar.addActions(list(group))
        theme.show_overflow_button(toolbar)
        return toolbar

    def apply_typing(self) -> None:
        self.grid.apply_typing()

    # --- selection ---------------------------------------------------------------------------------

    def ranges(self) -> Ranges:
        """The selected cells, (top, bottom, left, right) each; without a selection, the current cell."""
        found = [(r.top(), r.bottom(), r.left(), r.right()) for r in self.grid.selectionModel().selection()]
        row, column = self.grid.cell()
        return found or ([(row, row, column, column)] if row >= 0 else [])

    def _corner(self) -> tuple[int, int]:
        """The top left cell of the selection."""
        ranges = self.ranges()
        return (min(r[0] for r in ranges), min(r[2] for r in ranges)) if ranges else (0, 0)

    # --- editing -----------------------------------------------------------------------------------

    def undo(self) -> None:
        self._edit(self.sheet.undo)

    def redo(self) -> None:
        self._edit(self.sheet.redo)

    def add_last_row(self) -> bool:
        return self._edit(lambda: self.sheet.insert_rows(self.sheet.row_count))

    def add_row(self) -> None:
        ranges = self.ranges()
        at = max(r[1] for r in ranges) + 1 if ranges else self.sheet.row_count
        self._edit(lambda: self.sheet.insert_rows(at))
        self.grid.go_to(at, self.grid.cell()[1])

    def insert_row_above(self) -> None:
        ranges = self.ranges()
        at = min(r[0] for r in ranges) if ranges else 0
        self._edit(lambda: self.sheet.insert_rows(at))
        self.grid.go_to(at, self.grid.cell()[1])

    def add_column(self) -> None:
        ranges = self.ranges()
        at = max(r[3] for r in ranges) + 1 if ranges else self.sheet.column_count
        self._edit(lambda: self.sheet.insert_columns(at))
        self.grid.go_to(self.grid.cell()[0], at)

    def insert_column_left(self) -> None:
        ranges = self.ranges()
        at = min(r[2] for r in ranges) if ranges else 0
        self._edit(lambda: self.sheet.insert_columns(at))
        self.grid.go_to(self.grid.cell()[0], at)

    def delete_rows(self) -> None:
        spans = [(top, bottom) for top, bottom, _, _ in self.ranges()]
        self._edit(lambda: self.sheet.delete_rows(spans))

    def delete_columns(self) -> None:
        spans = [(left, right) for _, _, left, right in self.ranges()]
        if spans:
            self._edit(lambda: self.sheet.delete_columns(spans))
        else:
            self._status("A sheet without rows has no cell to select: add a row with + Row, then select the column.")

    def clear_cells(self) -> None:
        ranges = self.ranges()
        self._edit(lambda: self.sheet.clear(ranges))

    def fill(self) -> None:
        ranges = self.ranges()
        if ranges:
            text = self.ask_fill(sum((bottom - top + 1) * (right - left + 1) for top, bottom, left, right in ranges))
            if text is not None:
                self.fill_with(text)

    def fill_with(self, text: str) -> None:
        ranges = self.ranges()
        self._edit(lambda: self.sheet.fill(ranges, text))

    def fill_down(self) -> None:
        ranges = self.ranges()
        self._edit(lambda: self.sheet.fill_down(ranges))

    def fill_right(self) -> None:
        ranges = self.ranges()
        self._edit(lambda: self.sheet.fill_right(ranges))

    def rename_column(self, column: int) -> None:
        self.apply_typing()
        name = self.ask_name(self.sheet.names[column])
        if name is not None:
            self._edit(lambda: self.sheet.rename_column(column, name))

    def _switch_toggled(self, on: bool) -> None:
        self.apply_typing()
        self._edit(lambda: self.sheet.set_header(on))
        self.grid.setFocus()

    # --- copying -----------------------------------------------------------------------------------

    def copy_selection(self) -> bool:
        """Copy the selected cells as the data grid does: as tab-separated text, also what is more
        than a spreadsheet program would take at once, in the background. False if refused."""
        ranges = self.ranges()
        if not ranges:
            return False
        cells = sum((bottom - top + 1) * (right - left + 1) for top, bottom, left, right in ranges)
        if cells > data_panel.MAX_COPY_CELLS:
            self._status(
                f"{cells:,} cells are too many to copy (at most {data_panel.MAX_COPY_CELLS:,}): select fewer, or export the table"
            )
            return False
        frame = self.sheet.view()
        if cells <= data_panel._COPY_AT_ONCE:
            QGuiApplication.clipboard().setText(data_panel.selection_text(frame, ranges, _cell_texts))
            return True
        self._status(f"Copying {cells:,} cells…")
        self._copier.start(lambda cancel: (data_panel.selection_text(frame, ranges, _cell_texts, cancel), cells))
        return True

    def _copied(self, outcome: tuple[str, int]) -> None:
        text, cells = outcome
        QGuiApplication.clipboard().setText(text)
        self._status(f"Copied {cells:,} cells")

    def cut(self) -> None:
        if self.copy_selection():
            self.clear_cells()

    # --- pasting -----------------------------------------------------------------------------------

    def clipboard_text(self) -> str:
        """The plain text of the clipboard: the same for every program it comes from."""
        return QGuiApplication.clipboard().text()

    def paste(self) -> None:
        self._paste(self.clipboard_text(), *self._corner(), fill=True)

    def paste_below(self) -> None:
        self._paste(self.clipboard_text(), self.sheet.used_rows, 0)

    def paste_as_one_column(self) -> None:
        self.apply_typing()  # a change: the button then has nothing to do any more
        if self._one_column is not None:
            self._paste(*self._one_column, one_column=True)
        self.grid.setFocus()

    def _paste(self, text: str, row: int, column: int, *, one_column: bool = False, fill: bool = False) -> None:
        """``fill``: one value goes into all selected cells. ``one_column``: this paste comes
        instead of the last one (Paste as one column)."""
        kept = text if len(text) <= _KEEP_TEXT else None
        if len(text) <= _PASTE_AT_ONCE:
            try:
                pasted = parse_text(text, one_column=one_column)
            except SheetError as error:
                self._status(str(error))
                return
            self._pasted(pasted, kept, row, column, one_column, fill)
            return
        holder = [text]  # given up on the way: the reading needs that memory
        del text

        def work(_cancel: Any) -> Pasted:
            return parse_text(holder.pop(), one_column=one_column)

        self._run("Pasting…", work, lambda result: self._pasted(result, kept, row, column, one_column, fill))

    def _pasted(
        self, pasted: Pasted | SheetError, kept: str | None, row: int, column: int, again: bool, fill: bool
    ) -> None:
        if isinstance(pasted, SheetError):
            self._status(str(pasted))
            return
        sheet, frame = self.sheet, pasted.frame
        ranges, cell = self.ranges(), self.grid.cell()
        cells = sum((bottom - top + 1) * (right - left + 1) for top, bottom, left, right in ranges)
        replaces = again and self._paste_step
        if replaces:
            sheet.undo()
        state = sheet.state_id
        fills = fill and frame.height == 1 and frame.width == 1 and cells > 1 and not pasted.framed
        blank = self.is_draft and sheet.blank and (row, column) == (0, 0) and not fills
        try:
            if blank:
                sheet.load(pasted)
            elif fills:
                sheet.fill(ranges, frame.item(0, 0))
            else:
                sheet.paste(pasted, row, column)
        except SheetError as error:
            if replaces:
                sheet.redo()
            self._status(str(error))
            return
        self._paste_step = sheet.state_id != state
        self._show_edit(*cell)
        if fills:
            self._status(f"Pasted the value into {cells:,} cells")
            return
        if blank:
            self.grid.fit_columns()
            self.grid.go_to(0, 0)
            size = f"{count(sheet.row_count, 'row')} × {count(sheet.column_count, 'column')}"
        else:
            self._select_block(row, column, frame.height, frame.width)
            size = f"{count(frame.height, 'row')} × {count(frame.width, 'column')}"
        split = "tabs" if pasted.few_tabs else _SPLIT_AT.get(pasted.separator)
        how = f", split at {split}" if split else ""
        self._status(f"Pasted {size}{how}{', without the frame' if pasted.framed else ''}")
        if (split or pasted.framed) and kept is not None:
            self._one_column = kept, row, column
            self.one_column_button.show()

    def _select_block(self, row: int, column: int, height: int, width: int) -> None:
        first = self.model.index(row, column)
        selection = self.grid.selectionModel()
        selection.setCurrentIndex(first, QItemSelectionModel.SelectionFlag.ClearAndSelect)
        selection.select(
            QItemSelection(first, self.model.index(row + height - 1, column + width - 1)),
            QItemSelectionModel.SelectionFlag.ClearAndSelect,
        )
        self.grid.scrollTo(first)

    # --- jobs in the background --------------------------------------------------------------------

    def _run(self, text: str, work: Callable[[Any], Any], done: Callable[[Any], None], *, stop: bool = False) -> None:
        """Run ``work`` in the background with the window disabled, then ``done(result)``.
        A problem that can be fixed by hand is the result."""

        def job(cancel: Any) -> Any:
            try:
                return work(cancel)
            except (ActionError, SheetError) as error:
                return error

        self._done = done
        self._job_text = text
        self._worker.start(job)
        self._set_busy(True, stop)
        self._status(text)

    def _set_busy(self, busy: bool, stop: bool = False) -> None:
        for action in self.actions():
            action.setEnabled(not busy)
        self.toolbar.setEnabled(not busy)
        self.switch.setEnabled(not busy)
        self.grid.setEnabled(not busy)
        self.main_button.setEnabled(not busy)
        self.one_column_button.setEnabled(not busy)
        self.stop_button.setEnabled(True)
        self.stop_button.setVisible(stop)
        if not busy:
            self.grid.setFocus()

    def _job_ended(self, message: str = "") -> None:
        self._done = None
        self._close_refused = ""
        self._set_busy(False)
        self._message = message
        self._show_state()

    def _job_done(self, result: Any) -> None:
        done = self._done
        self._job_ended()
        if done is not None:
            done(result)

    def _job_failed(self, text: str) -> None:
        """Something that should not happen: the main window shows it."""
        self._close_after_write = self.closes_app = False
        self._job_ended()
        self.session.error.emit(text)
        if self._reading:
            self.close_now()

    def _job_stopped(self) -> None:
        self._close_after_write = self.closes_app = False
        self._job_ended("Stopped: the file was not written.")

    def stop(self) -> None:
        self._worker.cancel()
        self.stop_button.setEnabled(False)
        self._status("Stopping…")

    def _show_progress(self, part: float) -> None:
        if self.stop_button.isEnabled() and self.busy:
            why = f"  ·  {self._close_refused}" if self._close_refused else ""
            self._status(f"{self._job_text} {part:.0%}{why}")

    # --- writing -----------------------------------------------------------------------------------

    def add_as_table(self) -> bool:
        """Write the file, and add a table that imports it. Returns whether the writing started."""
        self.answered_state = None
        if not self.sheet.has_values:
            self._status("The sheet is empty: paste or type some data first.")
            return False
        while True:
            choice = self.ask_add()
            if choice is None:
                return False
            self._choice = choice
            path, suffix = choice
            if not path.exists() or self.ask_replace(path):
                break
        if self.session.running and path.exists():  # a run may read this file
            self._status("The project is running: save when the run has finished.")
            return False
        self._write(path, suffix, lambda: self._added(path, suffix))
        return True

    def save(self) -> bool:
        """Write the file back. Returns whether the writing started."""
        self.answered_state = None
        path, suffix = self.file, self.file_format.suffix
        if self.session.running:  # a run may read this file
            self._status("The project is running: save when the run has finished.")
            return False
        if problem := format_problem(self.sheet.frame(), suffix):
            self.show_problem(problem)
            return False
        now = stamp_of(path)
        if now is None:
            if not self.ask_save_gone(path):
                return False
        elif now != self._stamp:
            if not self.ask_save_changed(path):
                return False
        elif not self.sheet.changed:
            self._status("Nothing to save: the file has this data.")
            return False
        self._write(path, suffix, lambda: self._saved(path))
        return True

    def _write(self, path: Path, suffix: str, done: Callable[[], None]) -> None:
        frame = self.sheet.frame()  # it can't change: the window goes on showing the sheet
        warnings: list[str] = []

        def work(cancel: Any) -> None:
            write_file(
                frame, path, suffix, warn=warnings.append, cancelled=cancel.is_set, progress=self._write_progress.emit
            )

        def written(error: ActionError | None) -> None:
            if error is not None:
                self._close_after_write = self.closes_app = False
                self.show_problem(self._problem_text(error))
                return
            done()
            if warnings:
                self.message.emit(warnings[0])

        self._run(f"Writing {path.name}…", work, written, stop=True)

    def _problem_text(self, error: Exception) -> str:
        text = str(error).replace("and run again", "and try again")
        if self.session.busy and "and try again" in text:
            text += " (Tafl itself may be reading it: try again in a moment.)"
        return text

    def _added(self, path: Path, suffix: str) -> None:
        session = self.session
        session.sheet_saved(path)  # a replaced file may be the file of a table made earlier
        table = session.add_sheet_table(path, suffix, self._position, self._group)
        self.sheet.mark_saved()
        self.message.emit(f"Added “{table.name}”: its data is in {path}")
        self._close_as_asked()

    def _saved(self, path: Path) -> None:
        self._stamp = stamp_of(path)
        self.sheet.mark_saved()
        self.session.sheet_saved(path)
        self._show_state()
        self._status(f"Saved {path.name}")
        if self._close_after_write or self.closes_app:
            self._close_as_asked()

    def _close_as_asked(self) -> None:
        main = self.parentWidget() if self.closes_app else None
        self.close_now()
        if main is not None:
            QTimer.singleShot(0, main, main.go_on_closing)  # the next sheet is asked, then the project

    # --- the file ----------------------------------------------------------------------------------

    def load_file(self) -> None:
        """Load the file, not the result of a step, in the background. The stamp is from before
        the reading: a change while it reads counts as a change."""
        path, sheet_format = self.file, self.file_format
        action = self.session.registry.actions[sheet_format.action]
        self._stamp = stamp_of(path)
        self._reading = True
        warnings: list[str] = []

        def work(_cancel: Any) -> Sheet:
            return Sheet.from_frame(read_file(path, sheet_format, action, warnings.append))

        self._run(f"Reading {path.name}…", work, lambda result: self._was_read(result, warnings))

    def _was_read(self, result: Sheet | ActionError, warnings: list[str]) -> None:
        self._reading = False
        if isinstance(result, Sheet):
            self.model.set_sheet(result)
            self.grid.fit_columns()
            self.grid.go_to(0, 0)
            self._show_state()
            if warnings:
                self.show_problem(f"{warnings[0]}\n\nSave writes only what this sheet shows.")
        else:
            self.show_problem(self._problem_text(result))
            self.close_now()

    def _project_changed(self) -> None:
        reads = self.session.reads_file(self.file)
        self._note = "" if reads else "This file is not part of the open project any more: Save writes only the file."
        if not self.busy:
            self._message = ""
        self._show_status()

    # --- closing -----------------------------------------------------------------------------------

    def may_close(self, app: bool) -> bool:
        """Ask what happens with data that is only here. False: the window stays, for now: when
        an answer starts writing, the window closes when that has worked, and with ``app`` the
        main window then goes on closing."""
        if self.busy:
            self.closes_app = self.closes_app or (app and self.writing)
            self._close_refused = (
                "The sheet is being written: wait, or press Stop."
                if self.writing
                else "The data is being read: wait a moment."
            )
            self._status(self._close_refused)
            return False
        if self._asking:
            return False
        self._asking = True
        try:
            return self._answer_about_closing(app)
        finally:
            self._asking = False

    def _answer_about_closing(self, app: bool) -> bool:
        self.answered_state = None
        self.apply_typing()
        self._close_after_write = self.closes_app = False
        if self.is_draft:
            if not (self.sheet.has_values and self.sheet.changed):
                return True
            answer = self.ask_close_draft()
            if answer == "add" and self.add_as_table():
                self.closes_app = app
            return answer == "discard"
        if not self.sheet.changed:
            return True
        answer = self.ask_close_edit()
        if answer == "save" and self.save():
            self._close_after_write, self.closes_app = True, app
        return answer == "discard"

    def close_now(self) -> None:
        """Close without a question."""
        self._closing = True
        self.close()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._closing or self.may_close(app=False):
            QSettings().setValue("sheet_geometry", self.saveGeometry())
            self.closed.emit(self)
            event.accept()
        else:
            event.ignore()

    # --- questions ---------------------------------------------------------------------------------

    def ask_add(self) -> tuple[Path, str] | None:
        """The file for Add as table and its format; None if cancelled."""
        project = self.session.project
        folder = self._choice[0].parent if self._choice else project.directory or _last_folder()
        dialog = AddSheetDialog(
            self.sheet, self.title, folder, project.path is None, self, self.session.sheet_path_problem
        )
        if self._choice:  # back from "Replace it?": what was typed stays
            dialog.show_choice(*self._choice)
        return dialog.chosen() if dialog.exec() == QDialog.DialogCode.Accepted else None

    def ask_replace(self, path: Path) -> bool:
        answer = ask(
            self,
            "Add as table",
            f"“{path.name}” already exists in this folder.",
            "Replacing it overwrites the file. Tables that read it get the new data.",
            {
                "replace": ("Replace it", QMessageBox.ButtonRole.DestructiveRole),
                "cancel": ("Cancel", QMessageBox.ButtonRole.RejectRole),
            },
            "cancel",
        )
        return answer == "replace"

    def ask_close_draft(self) -> str:
        """ "add", "discard" or "cancel"."""
        return ask(
            self,
            "Close sheet",
            f"“{self.title}” has not been added as a table.",
            "Its data is only in this window: if you close it, the data is lost.",
            {
                "add": ("Add as table…", QMessageBox.ButtonRole.AcceptRole),
                "discard": ("Don't keep", QMessageBox.ButtonRole.DestructiveRole),
                "cancel": ("Cancel", QMessageBox.ButtonRole.RejectRole),
            },
            "add",
        )

    def ask_close_edit(self) -> str:
        """ "save", "discard" or "cancel"."""
        return ask(
            self,
            "Close sheet",
            f"Save the changes to “{self.title}”?",
            "If you don't save, your changes are lost.",
            {
                "save": ("Save", QMessageBox.ButtonRole.AcceptRole),
                "discard": ("Don't save", QMessageBox.ButtonRole.DestructiveRole),
                "cancel": ("Cancel", QMessageBox.ButtonRole.RejectRole),
            },
            "save",
        )

    def ask_save_changed(self, path: Path) -> bool:
        answer = ask(
            self,
            "Save",
            f"“{path.name}” has changed since it was opened in this sheet.",
            "Saving replaces those changes with the data in this sheet.",
            {
                "save": ("Save anyway", QMessageBox.ButtonRole.DestructiveRole),
                "cancel": ("Cancel", QMessageBox.ButtonRole.RejectRole),
            },
            "cancel",
        )
        return answer == "save"

    def ask_save_gone(self, path: Path) -> bool:
        answer = ask(
            self,
            "Save",
            f"“{path.name}” was moved or removed since it was opened in this sheet.",
            "Saving writes it again from the data in this sheet.",
            {
                "save": ("Save", QMessageBox.ButtonRole.AcceptRole),
                "cancel": ("Cancel", QMessageBox.ButtonRole.RejectRole),
            },
            "cancel",
        )
        return answer == "save"

    def show_problem(self, text: str) -> None:
        problem(self, text)

    def ask_fill(self, cells: int) -> str | None:
        text, ok = QInputDialog.getText(self, "Fill", f"Value for the {cells:,} selected cells:")
        return text if ok else None

    def ask_name(self, old: str) -> str | None:
        name, ok = QInputDialog.getText(self, "Rename column", "Name of the column:", text=old)
        return name if ok else None


def ask(
    parent: QWidget,
    title: str,
    text: str,
    informative: str,
    buttons: dict[str, tuple[str, Any]],
    default: str,
    icon: QMessageBox.Icon = QMessageBox.Icon.Question,
) -> str:
    """A question: ``buttons`` are key -> (text, role). Returns the key of the button clicked;
    closing the question is "cancel"."""
    box = QMessageBox(icon, title, text, parent=parent)
    box.setInformativeText(informative)
    added = {key: box.addButton(label, role) for key, (label, role) in buttons.items()}
    box.setDefaultButton(added[default])
    box.setEscapeButton(added["cancel"])
    box.exec()
    return next((key for key, button in added.items() if box.clickedButton() is button), "cancel")


def problem(parent: QWidget, text: str) -> None:
    ask(
        parent,
        "Sheet",
        text,
        "",
        {"cancel": ("OK", QMessageBox.ButtonRole.AcceptRole)},
        "cancel",
        QMessageBox.Icon.Warning,
    )


def _cell_texts(column: pl.Series) -> list[str]:
    """The cells of a column for the clipboard, in quotes as Excel writes them where needed."""
    return ["" if value is None else excel_text(value) for value in column.to_list()]


def _last_folder() -> Path:
    """The folder of the last project, as for File → Open."""
    folder = Path(str(QSettings().value("last_dir", str(Path.home()))))
    return folder if folder.is_dir() else Path.home()


def _duration(seconds: float) -> str:
    return f"{round(seconds / 60)} minutes" if seconds >= 90 else f"{round(seconds)} seconds"
