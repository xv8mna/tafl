"""Defining the parameters of a project: values that settings use as {Name}."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemDelegate,
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from etl_tool.api.parameters import Failed, as_text
from etl_tool.core import Parameter, Project, parameters

from . import theme
from .session import Session

HELP = (
    "Use a parameter as <b>{Name}</b> in the settings you type: file names, folders, texts, formulas, SQL "
    "and connection settings. A value is fixed (<code>2026-10-01</code>, <code>500</code>, <code>D:\\Data</code>) "
    "or a formula when it starts with = (<code>=TODAY() - 1</code>); a formula can use the parameters above "
    "it and is worked out when the project opens, when the parameters change, on Reload data (Ctrl+R) and "
    "for every run. For one run with other values: <code>tafl run project.tafl --set Name=value</code>."
)
_NAME, _VALUE, _NOW = range(3)


class ParametersDialog(QDialog):
    def __init__(self, session: Session, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Parameters")
        self.resize(760, 420)
        self.session = session

        intro = QLabel(HELP, self)
        intro.setWordWrap(True)
        intro.setObjectName("Dim")
        self.table = QTableWidget(0, 3, self)
        self.table.setHorizontalHeaderLabels(["Name", "Value (fixed, or =formula)", "Value now"])
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(_NAME, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(_VALUE, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(_NOW, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(_NAME, 160)
        for parameter in session.project.parameters:
            self._append(parameter.name, parameter.value, parameter.name)
        self.table.itemChanged.connect(lambda _item: self._update())

        add = QPushButton("+ Add", self)
        add.clicked.connect(self.add)
        self.remove_button = QPushButton("Delete", self)
        self.remove_button.clicked.connect(self.remove)
        self.up_button = QPushButton("↑", self)
        self.up_button.setToolTip("Move up: a formula can use the parameters above it")
        self.up_button.clicked.connect(lambda: self.move(-1))
        self.down_button = QPushButton("↓", self)
        self.down_button.setToolTip("Move down")
        self.down_button.clicked.connect(lambda: self.move(1))
        self.table.itemSelectionChanged.connect(self._update_buttons)
        row = QHBoxLayout()
        for button in (add, self.remove_button, self.up_button, self.down_button):
            row.addWidget(button)
        row.addStretch(1)

        self.problem = QLabel(self)
        self.problem.setWordWrap(True)
        self.problem.setStyleSheet(f"color: {theme.STATUS_COLORS['error']};")
        self.box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        self.box.accepted.connect(self._accept)
        self.box.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.table, 1)
        layout.addLayout(row)
        layout.addWidget(self.problem)
        layout.addWidget(self.box)
        self._update()
        self._update_buttons()

    def parameters(self) -> list[Parameter]:
        """The parameters as they are in the dialog."""
        return [Parameter(self._text(row, _NAME).strip(), self._text(row, _VALUE)) for row in range(self.table.rowCount())]

    def add(self) -> None:
        names = {p.name.lower() for p in self.parameters()}
        number = 1
        while f"Parameter{number}".lower() in names:
            number += 1
        self._append(f"Parameter{number}", "", None)
        row = self.table.rowCount() - 1
        self.table.setCurrentCell(row, _NAME)
        self.table.editItem(self.table.item(row, _NAME))
        self._update()

    def remove(self) -> None:
        row = self.table.currentRow()
        if 0 <= row < self.table.rowCount():
            self.table.removeRow(row)
            self._update()

    def move(self, step: int) -> None:
        row = self.table.currentRow()
        other = row + step
        if not (0 <= row < self.table.rowCount() and 0 <= other < self.table.rowCount()):
            return
        self.table.blockSignals(True)
        for column in (_NAME, _VALUE, _NOW):
            first, second = self.table.takeItem(row, column), self.table.takeItem(other, column)
            self.table.setItem(row, column, second)
            self.table.setItem(other, column, first)
        self.table.blockSignals(False)
        self.table.setCurrentCell(other, self.table.currentColumn())
        self._update()

    def _append(self, name: str, value: str, original: str | None) -> None:
        self.table.blockSignals(True)
        row = self.table.rowCount()
        self.table.insertRow(row)
        name_item = QTableWidgetItem(name)
        name_item.setData(Qt.ItemDataRole.UserRole, original)  # the name it had: renames update the steps
        value_item = QTableWidgetItem(value)
        value_item.setFont(theme.monospace_font())
        now = QTableWidgetItem("")
        now.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        for column, item in ((_NAME, name_item), (_VALUE, value_item), (_NOW, now)):
            self.table.setItem(row, column, item)
        self.table.blockSignals(False)

    def _text(self, row: int, column: int) -> str:
        item = self.table.item(row, column)
        return item.text() if item is not None else ""

    def _update(self) -> None:
        """Show what each value is now, and what is wrong."""
        found = self.parameters()
        if [p.to_dict() for p in found] == [p.to_dict() for p in self.session.project.parameters]:
            worked_out = self.session.parameter_values()  # as the calculation uses them
        else:
            worked_out = parameters.values(Project(parameters=found))
        values = {name.strip().lower(): value for name, value in worked_out.items()}
        self.table.blockSignals(True)
        for row, parameter in enumerate(found):
            item = self.table.item(row, _NOW)
            value = values.get(parameter.name.lower())
            if isinstance(value, Failed):
                item.setText(f"Error: {value.message}")
                item.setForeground(QColor(theme.STATUS_COLORS["error"]))
            else:
                shown = as_text(value)
                item.setText(f"{shown}   ({parameters.value_label(value)})" if shown else f"({parameters.value_label(value)})")
                item.setForeground(QColor(theme.TEXT_DIM))
            item.setToolTip(item.text())
        self.table.blockSignals(False)
        self._show_problems(found)

    def _show_problems(self, found: list[Parameter]) -> list[str]:
        problems = parameters.problems(Project(parameters=[p for p in found if p.name]))
        if any(not p.name for p in found):
            problems.insert(0, "Every parameter needs a name.")
        self.problem.setText("\n".join(problems))
        self.problem.setVisible(bool(problems))
        return problems

    def _update_buttons(self) -> None:
        row, count = self.table.currentRow(), self.table.rowCount()
        self.remove_button.setEnabled(0 <= row < count)
        self.up_button.setEnabled(0 < row < count)
        self.down_button.setEnabled(0 <= row < count - 1)

    def _commit_editor(self) -> None:
        """Take over what is typed in a cell that is still being edited (OK may be pressed
        before the cell is left)."""
        for row in range(self.table.rowCount()):
            for column in (_NAME, _VALUE):
                editor = self.table.indexWidget(self.table.model().index(row, column))
                if editor is not None:
                    self.table.commitData(editor)
                    self.table.closeEditor(editor, QAbstractItemDelegate.EndEditHint.NoHint)

    def _accept(self) -> None:
        """OK: with problems the dialog stays open and shows them, else the parameters apply."""
        self._commit_editor()
        found = self.parameters()
        if self._show_problems(found):
            return
        renames = {}
        for row, parameter in enumerate(found):
            original = self.table.item(row, _NAME).data(Qt.ItemDataRole.UserRole)
            if original is not None and original != parameter.name:
                renames[original] = parameter.name
        self.session.set_parameters(found, renames)
        self.accept()
