"""Settings forms, built from the parameters an action (or connection type) declares.

Every parameter kind has an editor widget. Editors report a change once a value is complete
(Enter, leaving the field, picking from a list), not on every keystroke.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from etl_tool.api.params import List, Param, is_visible
from etl_tool.core import functions

from . import functions_window, theme


@dataclass
class FormContext:
    """What editors offer as choices."""

    columns: Callable[[str, dict[str, Any]], list[str] | None] = lambda source, values: None
    tables: list[tuple[str, str]] = field(default_factory=list)  # (id, name)
    connections: list[str] = field(default_factory=list)
    project_dir: Path | None = None
    edit_connections: Callable[[], None] | None = None


class Editor(QWidget):
    """Base class: one parameter's input widget."""

    changed = Signal()

    def __init__(self, param: Param, ctx: FormContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.param = param
        self.ctx = ctx
        self.setToolTip(param.help)

    def value(self) -> Any:
        raise NotImplementedError

    def set_value(self, value: Any) -> None:
        raise NotImplementedError

    def refresh(self, ctx: FormContext, values: dict[str, Any]) -> None:
        """Update the choices offered (columns, tables, ...)."""
        self.ctx = ctx

    def set_placeholder(self, text: str) -> None:
        """Grey hint shown while the field is empty (used where there is no label)."""


def _row(*widgets: QWidget, stretch_first: bool = True) -> QHBoxLayout:
    layout = QHBoxLayout()
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(4)
    for i, widget in enumerate(widgets):
        layout.addWidget(widget, 1 if i == 0 and stretch_first else 0)
    return layout


class _PlainEdit(QPlainTextEdit):
    """Multi-line text that commits when focus leaves or on Ctrl+Enter."""

    committed = Signal()

    def focusOutEvent(self, event: Any) -> None:
        super().focusOutEvent(event)
        self.committed.emit()

    def keyPressEvent(self, event: Any) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.committed.emit()
            return
        super().keyPressEvent(event)


class TextEditor(Editor):
    def __init__(self, param: Param, ctx: FormContext, parent: QWidget | None = None) -> None:
        super().__init__(param, ctx, parent)
        self._multiline = bool(getattr(param, "multiline", False))
        if self._multiline:
            self._edit: QLineEdit | _PlainEdit = _PlainEdit(self)
            self._edit.setFixedHeight(70)
            self._edit.committed.connect(self._commit)
        else:
            self._edit = QLineEdit(self)
            self._edit.setPlaceholderText(getattr(param, "placeholder", ""))
            self._edit.editingFinished.connect(self._commit)
        self._last = ""
        self.setLayout(_row(self._edit))

    def value(self) -> str:
        return self._edit.toPlainText() if self._multiline else self._edit.text()

    def set_value(self, value: Any) -> None:
        text = "" if value is None else str(value)
        if self._multiline:
            self._edit.setPlainText(text)
        else:
            self._edit.setText(text)
        self._last = text

    def set_placeholder(self, text: str) -> None:
        if not self._edit.placeholderText():
            self._edit.setPlaceholderText(text)

    def _commit(self) -> None:
        if self.value() != self._last:
            self._last = self.value()
            self.changed.emit()


class CodeEditor(Editor):
    def __init__(self, param: Param, ctx: FormContext, parent: QWidget | None = None) -> None:
        super().__init__(param, ctx, parent)
        self._edit = _PlainEdit(self)
        self._edit.setFont(theme.monospace_font())
        self._edit.setMinimumHeight(150)
        self._edit.setPlaceholderText("SELECT ...   (Ctrl+Enter to apply)")
        self._edit.committed.connect(self._commit)
        self._last = ""
        self.setLayout(_row(self._edit))

    def value(self) -> str:
        return self._edit.toPlainText()

    def set_value(self, value: Any) -> None:
        self._last = "" if value is None else str(value)
        self._edit.setPlainText(self._last)

    def _commit(self) -> None:
        if self.value() != self._last:
            self._last = self.value()
            self.changed.emit()


class _FormulaEdit(_PlainEdit):
    """Like Excel's formula bar: Enter applies, Shift+Enter or Alt+Enter starts a new line."""

    focused = Signal()

    def focusInEvent(self, event: Any) -> None:
        super().focusInEvent(event)
        self.focused.emit()

    def focusOutEvent(self, event: Any) -> None:
        if event.reason() == Qt.FocusReason.ActiveWindowFocusReason and isinstance(
            QApplication.activeWindow(), functions_window.FunctionsWindow
        ):  # looking something up while typing: the formula isn't finished yet
            QPlainTextEdit.focusOutEvent(self, event)
            return
        super().focusOutEvent(event)

    def keyPressEvent(self, event: Any) -> None:
        plain_enter = event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (
            event.modifiers() & (Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.AltModifier)
        )
        if plain_enter:
            self.committed.emit()
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.insertPlainText("\n")
            return
        super().keyPressEvent(event)


class ExpressionEditor(Editor):
    """A formula (up to three visible lines), with buttons that insert [Column] names and open
    the list of functions."""

    def __init__(self, param: Param, ctx: FormContext, parent: QWidget | None = None) -> None:
        super().__init__(param, ctx, parent)
        self._edit = _FormulaEdit(self)
        self._edit.setFont(theme.monospace_font())
        self._edit.setFixedHeight(3 * self._edit.fontMetrics().lineSpacing() + 12)
        self._edit.setTabChangesFocus(True)
        self._edit.setPlaceholderText(getattr(param, "placeholder", "") or "[Column] * 2")
        self._edit.committed.connect(self._commit)
        self._edit.focused.connect(lambda: functions_window.remember(self))
        self._insert = QToolButton(self)
        self._insert.setText("[ ]")
        self._insert.setToolTip("Insert a column name")
        self._insert.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._menu = QMenu(self._insert)
        self._menu.aboutToShow.connect(self._fill_menu)
        self._insert.setMenu(self._menu)
        self._functions = QToolButton(self)
        self._functions.setText("ƒx")
        self._functions.setToolTip("Functions: what they do and how they are written (F1)")
        self._functions.clicked.connect(self._show_functions)
        self._columns: list[str] = []
        self._last = ""
        buttons = QVBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(2)
        buttons.addWidget(self._insert)
        buttons.addWidget(self._functions)
        buttons.addStretch(1)
        layout = QHBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self._edit, 1)
        layout.addLayout(buttons)
        self.setLayout(layout)

    def insert_template(self, template: str) -> None:
        """Insert a function at the cursor; typing goes on where the template marks it. Selected
        text goes there too: with [Price] selected, ROUND gives ROUND([Price])."""
        before, _, after = template.partition(functions.CURSOR)
        cursor = self._edit.textCursor()
        selected = cursor.selectedText().replace(" ", "\n")  # Qt's paragraph separator
        cursor.insertText(before + selected + after)
        cursor.setPosition(cursor.position() - len(after))
        self._edit.setTextCursor(cursor)
        self._edit.window().activateWindow()
        self._edit.setFocus()

    def apply(self) -> None:
        """Apply what is typed (as Enter does)."""
        self._commit()

    def _show_functions(self) -> None:
        functions_window.remember(self)
        functions_window.show_functions(self)

    def value(self) -> str:
        return self._edit.toPlainText()

    def set_value(self, value: Any) -> None:
        self._last = "" if value is None else str(value)
        self._edit.setPlainText(self._last)

    def refresh(self, ctx: FormContext, values: dict[str, Any]) -> None:
        super().refresh(ctx, values)
        self._columns = ctx.columns("input", values) or []

    def _fill_menu(self) -> None:
        self._menu.clear()
        if not self._columns:
            self._menu.addAction("(no columns yet)").setEnabled(False)
        for name in self._columns:
            self._menu.addAction(name, lambda n=name: self._insert_column(n))

    def _insert_column(self, name: str) -> None:
        self._edit.insertPlainText("[" + name.replace("]", "]]") + "]")
        self._edit.setFocus()
        self._commit()

    def _commit(self) -> None:
        if self.value() != self._last:
            self._last = self.value()
            self.changed.emit()


class IntEditor(Editor):
    def __init__(self, param: Param, ctx: FormContext, parent: QWidget | None = None) -> None:
        super().__init__(param, ctx, parent)
        self._spin = QSpinBox(self)
        low, high = getattr(param, "min", None), getattr(param, "max", None)
        self._spin.setRange(-2_000_000_000 if low is None else int(low), 2_000_000_000 if high is None else int(high))
        self._spin.setKeyboardTracking(False)
        self._spin.valueChanged.connect(self.changed)
        self.setLayout(_row(self._spin, QWidget(self)))

    def value(self) -> int:
        return self._spin.value()

    def set_value(self, value: Any) -> None:
        self._spin.blockSignals(True)
        try:
            self._spin.setValue(int(value or 0))
        except (TypeError, ValueError):
            self._spin.setValue(0)
        self._spin.blockSignals(False)


class FloatEditor(Editor):
    def __init__(self, param: Param, ctx: FormContext, parent: QWidget | None = None) -> None:
        super().__init__(param, ctx, parent)
        self._spin = QDoubleSpinBox(self)
        self._spin.setDecimals(6)
        low, high = getattr(param, "min", None), getattr(param, "max", None)
        self._spin.setRange(-1e15 if low is None else float(low), 1e15 if high is None else float(high))
        self._spin.setKeyboardTracking(False)
        self._spin.valueChanged.connect(self.changed)
        self.setLayout(_row(self._spin, QWidget(self)))

    def value(self) -> float:
        return self._spin.value()

    def set_value(self, value: Any) -> None:
        self._spin.blockSignals(True)
        try:
            self._spin.setValue(float(value or 0))
        except (TypeError, ValueError):
            self._spin.setValue(0.0)
        self._spin.blockSignals(False)


class BoolEditor(Editor):
    def __init__(self, param: Param, ctx: FormContext, parent: QWidget | None = None) -> None:
        super().__init__(param, ctx, parent)
        self._box = QCheckBox(param.label, self)
        self._box.toggled.connect(self.changed)
        self.setLayout(_row(self._box))

    def value(self) -> bool:
        return self._box.isChecked()

    def set_value(self, value: Any) -> None:
        self._box.blockSignals(True)
        self._box.setChecked(bool(value))
        self._box.blockSignals(False)


class ChoiceEditor(Editor):
    def __init__(self, param: Param, ctx: FormContext, parent: QWidget | None = None) -> None:
        super().__init__(param, ctx, parent)
        self._combo = QComboBox(self)
        self._combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self._combo.setMinimumContentsLength(8)
        for value, label in getattr(param, "options", ()):
            self._combo.addItem(label, value)
        self._combo.currentIndexChanged.connect(self.changed)
        self.setLayout(_row(self._combo))

    def value(self) -> Any:
        return self._combo.currentData()

    def set_value(self, value: Any) -> None:
        self._combo.blockSignals(True)
        index = self._combo.findData(value)
        if index < 0 and value is not None:
            self._combo.addItem(f"{value} (unknown)", value)
            index = self._combo.count() - 1
        self._combo.setCurrentIndex(max(index, 0))
        self._combo.blockSignals(False)


class ColumnEditor(Editor):
    """A column name: pick from the list or type it."""

    def __init__(self, param: Param, ctx: FormContext, parent: QWidget | None = None) -> None:
        super().__init__(param, ctx, parent)
        self._combo = QComboBox(self)
        self._combo.setEditable(True)
        self._combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self._combo.setMinimumContentsLength(6)
        self._combo.lineEdit().setPlaceholderText("column")
        self._combo.activated.connect(lambda _: self._commit())
        self._combo.lineEdit().editingFinished.connect(self._commit)
        self._columns: list[str] | None = None
        self._last = ""
        self.setLayout(_row(self._combo))

    def value(self) -> str:
        return self._combo.currentText().strip()

    def set_value(self, value: Any) -> None:
        self._last = "" if value is None else str(value)
        self._combo.blockSignals(True)
        self._combo.setCurrentText(self._last)
        self._combo.blockSignals(False)
        self._mark()

    def set_placeholder(self, text: str) -> None:
        self._combo.lineEdit().setPlaceholderText(text)

    def refresh(self, ctx: FormContext, values: dict[str, Any]) -> None:
        super().refresh(ctx, values)
        columns = ctx.columns(getattr(self.param, "source", "input"), values)
        if columns == self._columns:
            return
        self._columns = columns
        text = self._combo.currentText()
        self._combo.blockSignals(True)
        self._combo.clear()
        self._combo.addItems(columns or [])
        self._combo.setCurrentText(text)
        self._combo.blockSignals(False)
        self._mark()

    def _commit(self) -> None:
        self._mark()
        if self.value() != self._last:
            self._last = self.value()
            self.changed.emit()

    def _mark(self) -> None:
        missing = bool(self.value()) and self._columns is not None and self.value() not in self._columns
        _set_missing(self._combo, missing)
        self._combo.setToolTip("This column is not in the table" if missing else self.param.help)


class ColumnsEditor(Editor):
    """Several columns: tick them; drag to change the order."""

    def __init__(self, param: Param, ctx: FormContext, parent: QWidget | None = None) -> None:
        super().__init__(param, ctx, parent)
        self._list = QListWidget(self)
        self._list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self._list.setMaximumHeight(170)
        self._list.itemChanged.connect(lambda _: self.changed.emit())
        self._list.model().rowsMoved.connect(lambda *_: self.changed.emit())
        all_button, none_button = QPushButton("All", self), QPushButton("None", self)
        all_button.clicked.connect(lambda: self._check_all(True))
        none_button.clicked.connect(lambda: self._check_all(False))
        buttons = _row(all_button, none_button, stretch_first=False)
        buttons.addStretch(1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        layout.addWidget(self._list)
        layout.addLayout(buttons)
        self._columns: list[str] | None = None

    def value(self) -> list[str]:
        return [
            self._list.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self._list.count())
            if self._list.item(i).checkState() == Qt.CheckState.Checked
        ]

    def set_value(self, value: Any) -> None:
        self._rebuild(list(value or []))

    def refresh(self, ctx: FormContext, values: dict[str, Any]) -> None:
        super().refresh(ctx, values)
        columns = ctx.columns(getattr(self.param, "source", "input"), values)
        if columns != self._columns:
            self._columns = columns
            self._rebuild(self.value())

    def _rebuild(self, selected: list[str]) -> None:
        self._list.blockSignals(True)
        self._list.clear()
        available = self._columns or []
        for name in [*selected, *(c for c in available if c not in selected)]:
            item = QListWidgetItem(name if self._columns is None or name in available else f"{name}  (missing)")
            item.setData(Qt.ItemDataRole.UserRole, name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsDragEnabled)
            item.setCheckState(Qt.CheckState.Checked if name in selected else Qt.CheckState.Unchecked)
            if self._columns is not None and name not in available:
                item.setForeground(theme.status_color("error"))
            self._list.addItem(item)
        self._list.blockSignals(False)

    def _check_all(self, checked: bool) -> None:
        self._list.blockSignals(True)
        for i in range(self._list.count()):
            self._list.item(i).setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        self._list.blockSignals(False)
        self.changed.emit()


class PathEditor(Editor):
    """A file or folder; a path inside the project folder is stored relative to it."""

    def __init__(self, param: Param, ctx: FormContext, parent: QWidget | None = None) -> None:
        super().__init__(param, ctx, parent)
        self._edit = QLineEdit(self)
        self._edit.editingFinished.connect(self._commit)
        browse = QToolButton(self)
        browse.setText("…")
        browse.setToolTip("Browse")
        browse.clicked.connect(self._browse)
        self._last = ""
        self.setLayout(_row(self._edit, browse))

    def value(self) -> str:
        return self._edit.text().strip()

    def set_value(self, value: Any) -> None:
        self._last = "" if value is None else str(value)
        self._edit.setText(self._last)

    def _browse(self) -> None:
        start = self._absolute(self.value()) or str(self.ctx.project_dir or Path.home())
        if self.param.kind == "folder":
            chosen = QFileDialog.getExistingDirectory(self, self.param.label, start)
        elif getattr(self.param, "mode", "open") == "save":
            chosen, _ = QFileDialog.getSaveFileName(self, self.param.label, start, getattr(self.param, "filter", ""))
        else:
            chosen, _ = QFileDialog.getOpenFileName(self, self.param.label, start, getattr(self.param, "filter", ""))
        if chosen:
            self._edit.setText(self._relative(chosen))
            self._commit()

    def _absolute(self, text: str) -> str:
        if not text:
            return ""
        path = Path(text)
        if not path.is_absolute() and self.ctx.project_dir is not None:
            path = self.ctx.project_dir / path
        return str(path)

    def _relative(self, chosen: str) -> str:
        if self.ctx.project_dir is None:
            return str(Path(chosen))
        try:
            relative = os.path.relpath(chosen, self.ctx.project_dir)
        except ValueError:
            return str(Path(chosen))
        return str(Path(chosen)) if relative.startswith("..") else Path(relative).as_posix()

    def _commit(self) -> None:
        if self.value() != self._last:
            self._last = self.value()
            self.changed.emit()


class TableEditor(Editor):
    def __init__(self, param: Param, ctx: FormContext, parent: QWidget | None = None) -> None:
        super().__init__(param, ctx, parent)
        self._combo = QComboBox(self)
        self._combo.currentIndexChanged.connect(self.changed)
        self._value: str = ""
        self.setLayout(_row(self._combo))
        self._fill()

    def value(self) -> str:
        data = self._combo.currentData()
        return "" if data is None else str(data)

    def set_value(self, value: Any) -> None:
        self._value = "" if value is None else str(value)
        self._fill()

    def refresh(self, ctx: FormContext, values: dict[str, Any]) -> None:
        super().refresh(ctx, values)
        self._value = self.value()
        self._fill()

    def _fill(self) -> None:
        self._combo.blockSignals(True)
        self._combo.clear()
        self._combo.addItem("(choose a table)", "")
        for table_id, name in self.ctx.tables:
            self._combo.addItem(name, table_id)
        index = self._combo.findData(self._value)
        if index < 0 and self._value:
            self._combo.addItem(f"{self._value} (missing)", self._value)
            index = self._combo.count() - 1
        self._combo.setCurrentIndex(max(index, 0))
        self._combo.blockSignals(False)


class TablesEditor(Editor):
    def __init__(self, param: Param, ctx: FormContext, parent: QWidget | None = None) -> None:
        super().__init__(param, ctx, parent)
        self._list = QListWidget(self)
        self._list.setMaximumHeight(140)
        self._list.itemChanged.connect(lambda _: self.changed.emit())
        self.setLayout(_row(self._list))
        self._selected: list[str] = []
        self._fill()

    def value(self) -> list[str]:
        return [
            self._list.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self._list.count())
            if self._list.item(i).checkState() == Qt.CheckState.Checked
        ]

    def set_value(self, value: Any) -> None:
        self._selected = list(value or [])
        self._fill()

    def refresh(self, ctx: FormContext, values: dict[str, Any]) -> None:
        super().refresh(ctx, values)
        self._selected = self.value()
        self._fill()

    def _fill(self) -> None:
        self._list.blockSignals(True)
        self._list.clear()
        names = dict(self.ctx.tables)
        for table_id in [*self._selected, *(t for t in names if t not in self._selected)]:
            item = QListWidgetItem(names.get(table_id, f"{table_id} (missing)"))
            item.setData(Qt.ItemDataRole.UserRole, table_id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if table_id in self._selected else Qt.CheckState.Unchecked)
            self._list.addItem(item)
        self._list.blockSignals(False)


class ConnectionEditor(Editor):
    def __init__(self, param: Param, ctx: FormContext, parent: QWidget | None = None) -> None:
        super().__init__(param, ctx, parent)
        self._combo = QComboBox(self)
        self._combo.currentIndexChanged.connect(self.changed)
        edit = QToolButton(self)
        edit.setText("Edit…")
        edit.setToolTip("Define or change database connections")
        edit.clicked.connect(lambda: self.ctx.edit_connections and self.ctx.edit_connections())
        self._value = ""
        self.setLayout(_row(self._combo, edit))
        self._fill()

    def value(self) -> str:
        data = self._combo.currentData()
        return "" if data is None else str(data)

    def set_value(self, value: Any) -> None:
        self._value = "" if value is None else str(value)
        self._fill()

    def refresh(self, ctx: FormContext, values: dict[str, Any]) -> None:
        super().refresh(ctx, values)
        self._value = self.value()
        self._fill()

    def _fill(self) -> None:
        self._combo.blockSignals(True)
        self._combo.clear()
        self._combo.addItem("(choose a connection)", "")
        for name in self.ctx.connections:
            self._combo.addItem(name, name)
        index = self._combo.findData(self._value)
        if index < 0 and self._value:
            self._combo.addItem(f"{self._value} (missing)", self._value)
            index = self._combo.count() - 1
        self._combo.setCurrentIndex(max(index, 0))
        self._combo.blockSignals(False)


class ListEditor(Editor):
    """Rows of sub-settings (sort keys, renames, formulas, ...) with add / move / remove.

    Rows form a compact grid with one column per field. When a field holds a formula, each row
    becomes a small card instead: the short fields in one line, the formula below at full width.
    """

    def __init__(self, param: List, ctx: FormContext, parent: QWidget | None = None) -> None:
        super().__init__(param, ctx, parent)
        self._fields = list(param.fields)
        self._wide = [f for f in self._fields if f.kind in ("expression", "code")]
        self._rows: list[dict[str, Editor]] = []
        self._row_widgets: list[list[QWidget]] = []
        self._values: dict[str, Any] = {}
        self._grid = QGridLayout()
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(4)
        self._grid.setVerticalSpacing(4)
        if not self._wide:
            for column, f in enumerate(self._fields):
                self._grid.setColumnStretch(column, 2 if f.kind == "text" else 1)
        add = QPushButton("+ Add", self)
        add.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        add.clicked.connect(self._add_clicked)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addLayout(self._grid)
        layout.addWidget(add)
        if not self._wide:
            self._header()

    def value(self) -> list[dict[str, Any]]:
        return [{key: editor.value() for key, editor in row.items()} for row in self._rows]

    def set_value(self, value: Any) -> None:
        self._rebuild(list(value or []))

    def refresh(self, ctx: FormContext, values: dict[str, Any]) -> None:
        super().refresh(ctx, values)
        self._values = values
        for row in self._rows:
            for editor in row.values():
                editor.refresh(ctx, values)
        self._update_visibility()

    def _header(self) -> None:
        if len(self._fields) < 2:
            return
        for column, f in enumerate(self._fields):
            label = QLabel(f.label, self)
            label.setObjectName("Dim")
            self._grid.addWidget(label, 0, column)

    def _rebuild(self, rows: list[dict[str, Any]]) -> None:
        for widgets in self._row_widgets:
            for widget in widgets:
                self._grid.removeWidget(widget)
                widget.deleteLater()
        self._rows, self._row_widgets = [], []
        for values in rows:
            self._add_row(values)
        self._update_visibility()

    def _add_row(self, values: dict[str, Any]) -> None:
        number = len(self._rows)
        editors: dict[str, Editor] = {}
        for f in self._fields:
            editor = create_editor(f, self.ctx, self)
            editor.refresh(self.ctx, self._values)
            editor.set_value(values.get(f.key, f.initial()))
            editor.changed.connect(self._child_changed)
            editors[f.key] = editor
        tools = QWidget(self)
        tools.setLayout(
            _row(
                _tool(tools, "▲", "Move up", lambda: self._move(number, -1)),
                _tool(tools, "▼", "Move down", lambda: self._move(number, 1)),
                _tool(tools, "✕", "Remove", lambda: self._remove(number)),
                stretch_first=False,
            )
        )
        if self._wide:
            card = QFrame(self)
            card.setObjectName("ListCard")
            grid = QGridLayout(card)
            grid.setContentsMargins(6, 6, 6, 6)
            grid.setSpacing(4)
            line = [f for f in self._fields if f not in self._wide]
            for column, f in enumerate(line):
                editors[f.key].set_placeholder(f.label)
                grid.addWidget(editors[f.key], 0, column)
                grid.setColumnStretch(column, 1)
            grid.addWidget(tools, 0, len(line))
            for offset, f in enumerate(self._wide, start=1):
                grid.addWidget(editors[f.key], offset, 0, 1, len(line) + 1)
            self._grid.addWidget(card, number, 0)
            widgets: list[QWidget] = [card]
        else:
            for column, f in enumerate(self._fields):
                self._grid.addWidget(editors[f.key], number + 1, column)
            self._grid.addWidget(tools, number + 1, len(self._fields))
            widgets = [*editors.values(), tools]
        self._rows.append(editors)
        self._row_widgets.append(widgets)

    def _add_clicked(self) -> None:
        rows = self.value()
        rows.append({f.key: f.initial() for f in self._fields})
        self._rebuild(rows)
        self.changed.emit()

    def _remove(self, number: int) -> None:
        rows = self.value()
        del rows[number]
        self._rebuild(rows)
        self.changed.emit()

    def _move(self, number: int, delta: int) -> None:
        rows = self.value()
        target = number + delta
        if 0 <= target < len(rows):
            rows.insert(target, rows.pop(number))
            self._rebuild(rows)
            self.changed.emit()

    def _child_changed(self) -> None:
        self._update_visibility()
        self.changed.emit()

    def _update_visibility(self) -> None:
        for row in self._rows:
            values = {key: editor.value() for key, editor in row.items()}
            for f in self._fields:
                row[f.key].setVisible(is_visible(f, values))


def _tool(parent: QWidget, text: str, tip: str, slot: Callable[[], None]) -> QToolButton:
    button = QToolButton(parent)
    button.setText(text)
    button.setToolTip(tip)
    button.setAutoRaise(True)
    button.clicked.connect(slot)
    return button


def _set_missing(widget: QWidget, missing: bool) -> None:
    if widget.property("missing") != missing:
        widget.setProperty("missing", missing)
        widget.style().unpolish(widget)
        widget.style().polish(widget)


_EDITORS: dict[str, type[Editor]] = {
    "text": TextEditor,
    "code": CodeEditor,
    "expression": ExpressionEditor,
    "int": IntEditor,
    "float": FloatEditor,
    "bool": BoolEditor,
    "choice": ChoiceEditor,
    "column": ColumnEditor,
    "columns": ColumnsEditor,
    "file": PathEditor,
    "folder": PathEditor,
    "table": TableEditor,
    "tables": TablesEditor,
    "connection": ConnectionEditor,
    "list": ListEditor,
}


def create_editor(param: Param, ctx: FormContext, parent: QWidget | None = None) -> Editor:
    """The editor for a parameter; unknown kinds (from newer plugins) get a text field."""
    return _EDITORS.get(param.kind, TextEditor)(param, ctx, parent)


class ParamForm(QWidget):
    """All parameters of an action, label above field, in declaration order."""

    changed = Signal(dict)

    def __init__(self, spec: Sequence[Param], ctx: FormContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._rows: list[tuple[Param, QLabel | None, Editor, QLabel | None]] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        for param in spec:
            label = None
            if param.kind != "bool":
                label = QLabel(param.label + (" *" if param.required else ""), self)
                label.setToolTip(param.help)
                layout.addSpacing(4)
                layout.addWidget(label)
            editor = create_editor(param, ctx, self)
            editor.changed.connect(self._on_changed)
            layout.addWidget(editor)
            hint = None
            if param.help:
                hint = QLabel(param.help, self)
                hint.setObjectName("Dim")
                hint.setWordWrap(True)
                layout.addWidget(hint)
            self._rows.append((param, label, editor, hint))
        layout.addStretch(1)

    def set_values(self, values: dict[str, Any]) -> None:
        for param, _, editor, _ in self._rows:
            editor.refresh(self.ctx, values)
            editor.set_value(values.get(param.key))
        self._update_visibility(values)

    def values(self) -> dict[str, Any]:
        return {param.key: editor.value() for param, _, editor, _ in self._rows}

    def refresh(self, ctx: FormContext) -> None:
        self.ctx = ctx
        values = self.values()
        for _, _, editor, _ in self._rows:
            editor.refresh(ctx, values)

    def editor(self, key: str) -> Editor:
        return next(editor for param, _, editor, _ in self._rows if param.key == key)

    def _on_changed(self) -> None:
        values = self.values()
        self._update_visibility(values)
        for _, _, editor, _ in self._rows:
            editor.refresh(self.ctx, values)
        self.changed.emit(values)

    def _update_visibility(self, values: dict[str, Any]) -> None:
        for param, label, editor, hint in self._rows:
            visible = is_visible(param, values)
            for widget in (label, editor, hint):
                if widget is not None:
                    widget.setVisible(visible)
