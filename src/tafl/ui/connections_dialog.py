"""Defining the database connections of a project."""

from __future__ import annotations

import copy

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from tafl.api import ActionError, Context
from tafl.api.params import resolve
from tafl.core import Connection, parameters

from . import theme
from .forms import FormContext, ParamForm
from .session import Session, json_ready
from .worker import Worker


class ConnectionsDialog(QDialog):
    def __init__(self, session: Session, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Connections")
        self.resize(720, 460)
        self.session = session
        self.registry = session.registry
        # working copies, each with the name it had when the dialog opened
        self._rows: list[tuple[str | None, Connection]] = [
            (c.name, copy.deepcopy(c)) for c in session.project.connections
        ]
        self._form: ParamForm | None = None
        self._testing: Connection | None = None  # the connection whose test is running
        self._results: dict[int, tuple[bool, str]] = {}  # id of a connection -> its last test result

        self.list = QListWidget(self)
        self.list.currentRowChanged.connect(self._show)
        add = QPushButton("+ Add", self)
        add_menu = QMenu(add)
        for connector in self.registry.connectors.values():
            add_menu.addAction(connector.name, lambda c=connector: self._add(c.id))
        add.setMenu(add_menu)
        remove = QPushButton("Delete", self)
        remove.clicked.connect(self._remove)
        left = QVBoxLayout()
        left.addWidget(self.list, 1)
        buttons = QHBoxLayout()
        buttons.addWidget(add)
        buttons.addWidget(remove)
        left.addLayout(buttons)

        self.name = QLineEdit(self)
        self.name.editingFinished.connect(self._rename)
        self.type_label = QLabel(self)
        self.type_label.setObjectName("Dim")
        self.form_host = QVBoxLayout()
        self.test_button = QPushButton("Test connection", self)
        self.test_button.clicked.connect(self._test)
        # Below the button, all of it, and it can be copied: a driver's error can be long
        self.test_result = QPlainTextEdit(self)
        self.test_result.setReadOnly(True)
        self.test_result.setFrameShape(QFrame.Shape.NoFrame)
        # Five whole lines (no line cut in half, also when scrolled), on the dialog's own background
        self.test_result.document().setDocumentMargin(0)
        self.test_result.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)  # the lines wrap
        self.test_result.setFixedHeight(5 * self.test_result.fontMetrics().lineSpacing())
        self.test_result.hide()
        self.details = QWidget(self)
        right = QVBoxLayout(self.details)
        right.setContentsMargins(10, 0, 0, 0)
        right.addWidget(QLabel("Name", self))
        right.addWidget(self.name)
        right.addWidget(self.type_label)
        right.addLayout(self.form_host)
        test_row = QHBoxLayout()
        test_row.addWidget(self.test_button)
        test_row.addStretch(1)
        right.addLayout(test_row)
        right.addWidget(self.test_result)
        right.addStretch(1)

        body = QHBoxLayout()
        body.addLayout(left, 2)
        body.addWidget(self.details, 3)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        box.accepted.connect(self._accept)
        box.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(body, 1)
        layout.addWidget(box)

        self._worker = Worker(self)
        self._worker.finished.connect(self._tested)
        self._worker.failed.connect(lambda text: self._tested((False, text.strip().splitlines()[-1])))
        self._refresh_list()
        self.list.setCurrentRow(0 if self._rows else -1)
        self._show(self.list.currentRow())

    def _refresh_list(self) -> None:
        current = self.list.currentRow()
        self.list.blockSignals(True)
        self.list.clear()
        for _, connection in self._rows:
            connector = self.registry.connectors.get(connection.type)
            self.list.addItem(f"{connection.name}  ({connector.name if connector else connection.type})")
        self.list.setCurrentRow(min(current, len(self._rows) - 1))
        self.list.blockSignals(False)

    def _current(self) -> Connection | None:
        row = self.list.currentRow()
        return self._rows[row][1] if 0 <= row < len(self._rows) else None

    def _show(self, row: int) -> None:
        if self._form is not None:
            self._form.setParent(None)
            self._form.deleteLater()
            self._form = None
        connection = self._current()
        self.details.setEnabled(connection is not None)
        testing = connection is not None and connection is self._testing
        self.test_button.setEnabled(not testing)  # a test of another connection may be running
        if testing:
            self._show_result("Connecting…")
        elif connection is not None and id(connection) in self._results:
            self._show_tested(*self._results[id(connection)])
        else:
            self._show_result("")
        if connection is None:
            self.name.setText("")
            self.type_label.setText("Add a connection with “+ Add”.")
            return
        connector = self.registry.connectors.get(connection.type)
        self.name.setText(connection.name)
        if connector is None:
            self.type_label.setText(f"Unknown connection type '{connection.type}' (plugin missing)")
            return
        self.type_label.setText(connector.name)
        self._form = ParamForm(connector.params, FormContext(project_dir=self.session.project.directory), self)
        self._form.set_values(json_ready(resolve(connector.params, connection.settings)))
        self._form.changed.connect(lambda values: self._changed(connection, values))
        self.form_host.addWidget(self._form)

    def _changed(self, connection: Connection, values: dict) -> None:
        connection.settings.update(values)
        self._results.pop(id(connection), None)  # the last test was of other settings

    def _add(self, connector_id: str) -> None:
        connector = self.registry.connectors[connector_id]
        names = {c.name.lower() for _, c in self._rows}
        name, number = connector_id, 1
        while name.lower() in names:
            number += 1
            name = f"{connector_id}{number}"
        self._rows.append((None, Connection(name, connector_id, json_ready(resolve(connector.params, {})))))
        self._refresh_list()
        self.list.setCurrentRow(len(self._rows) - 1)

    def _remove(self) -> None:
        row = self.list.currentRow()
        if 0 <= row < len(self._rows):
            self._results.pop(id(self._rows[row][1]), None)
            del self._rows[row]
            self._refresh_list()
            self._show(self.list.currentRow())

    def _rename(self) -> None:
        connection = self._current()
        if connection is not None and self.name.text().strip():
            connection.name = self.name.text().strip()
            self._refresh_list()

    def _test(self) -> None:
        connection = self._current()
        connector = connection and self.registry.connectors.get(connection.type)
        if connector is None:
            return
        settings, values = dict(connection.settings), self.session.parameter_values
        ctx = Context(project_dir=self.session.project.directory)
        self._testing = connection
        self._results.pop(id(connection), None)
        self._show_result("Connecting…")
        self.test_button.setEnabled(False)

        def work(_cancel):
            try:  # {Name} of a parameter, as the calculation fills it in (a formula may take a while)
                filled = parameters.fill(list(connector.params), settings, values())
            except ActionError as e:
                return False, str(e)
            try:
                with connector.open(filled, ctx) as db:
                    count = len(db.tables())
                    return True, f"Connected — {count} {'table or view' if count == 1 else 'tables and views'}"
            except Exception as e:
                return False, str(e)

        self._worker.start(work)

    def _tested(self, outcome: tuple[bool, str]) -> None:
        """The result of the latest test: shown under its connection (also when another one was
        selected meanwhile, and that one is selected again)."""
        tested, self._testing = self._testing, None
        if tested is not None:
            self._results[id(tested)] = outcome
        if tested is None or tested is self._current():
            self.test_button.setEnabled(True)
            self._show_tested(*outcome)

    def _show_tested(self, ok: bool, text: str) -> None:
        self._show_result(text, theme.STATUS_TEXT["ok" if ok else "error"])

    def _show_result(self, text: str, color: str = theme.TEXT) -> None:
        self.test_result.setPlainText(text)
        palette = self.test_result.palette()
        palette.setColor(QPalette.ColorRole.Base, QColor(theme.WINDOW))
        palette.setColor(QPalette.ColorRole.Text, QColor(color))
        self.test_result.setPalette(palette)
        self.test_result.setVisible(bool(text))

    def _accept(self) -> None:
        self._rename()
        names = [c.name for _, c in self._rows]
        if any(not n for n in names) or len({n.lower() for n in names}) != len(names):
            QMessageBox.warning(self, "Connections", "Every connection needs its own name.")
            return
        renames = {old: c.name for old, c in self._rows if old is not None}
        self.session.set_connections([c for _, c in self._rows], renames)
        self.accept()
