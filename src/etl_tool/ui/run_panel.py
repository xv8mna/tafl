"""Results of the last project run: every table and step, with errors and messages."""

from __future__ import annotations

import datetime as dt

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHeaderView, QLabel, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget

from etl_tool.core import Status, TableResult

from . import theme
from .session import Session

_STATUS = {
    Status.OK: "ok",
    Status.ERROR: "error",
    Status.SKIPPED: "skipped",
    Status.DISABLED: "disabled",
    Status.BLOCKED: "blocked",
}


class RunPanel(QWidget):
    def __init__(self, session: Session, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self.summary = QLabel("No run yet. Press F5 to run the project.", self)
        self.summary.setObjectName("Dim")
        self.tree = QTreeWidget(self)
        self.tree.setHeaderLabels(["Step", "Result", "Time"])
        self.tree.setRootIsDecorated(True)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setStretchLastSection(False)
        self.tree.setColumnWidth(1, 260)
        self.tree.setColumnWidth(2, 70)
        self.tree.itemDoubleClicked.connect(self._jump)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 6)
        layout.addWidget(self.summary)
        layout.addWidget(self.tree, 1)
        session.run_finished.connect(self.show_results)

    def show_results(self, results: dict[str, TableResult]) -> None:
        self.tree.clear()
        failed = skipped = 0
        total = 0.0
        for table_id, result in results.items():
            table = self.session.project.find_table(table_id)
            if table is None:
                continue
            if result.skipped:
                skipped += 1
                top = QTreeWidgetItem([table.name, f"skipped: {result.skipped}", ""])
                top.setIcon(0, theme.status_icon("disabled"))
                top.setForeground(1, theme.status_color("skipped"))
                top.setToolTip(1, f"Not calculated: it {result.skipped}")
                top.setData(0, Qt.ItemDataRole.UserRole, (table_id, None))
                self.tree.addTopLevelItem(top)
                continue
            steps = {s.id: (n, s) for n, s in enumerate(table.steps, start=1)}
            seconds = sum(r.seconds for r in result.steps)
            total += seconds
            ok = result.ok
            failed += not ok
            top = QTreeWidgetItem([table.name, "ok" if ok else (result.first_error or "error"), f"{seconds:.2f} s"])
            top.setIcon(0, theme.status_icon("ok" if ok else "error"))
            top.setData(0, Qt.ItemDataRole.UserRole, (table_id, None))
            for step_result in result.steps:
                number, step = steps.get(step_result.step_id, (0, None))
                if step is None:
                    continue
                text = _result_text(step_result)
                child = QTreeWidgetItem([f"{number}. {self.session.engine.describe(step)}", text, f"{step_result.seconds:.2f} s"])
                child.setIcon(0, theme.status_icon(_STATUS[step_result.status]))
                child.setToolTip(1, step_result.error or text)
                child.setData(0, Qt.ItemDataRole.UserRole, (table_id, step.id))
                for level, message in step_result.messages:
                    note = QTreeWidgetItem([("⚠ " if level == "warning" else "") + message, "", ""])
                    note.setForeground(0, theme.status_color("warning" if level == "warning" else "skipped"))
                    child.addChild(note)
                top.addChild(child)
            self.tree.addTopLevelItem(top)
            top.setExpanded(not ok or any(r.messages for r in result.steps))
        finished = dt.datetime.now().strftime("%H:%M:%S")
        tables = len(results)
        problems = f"{failed} with errors" if failed else "no errors"
        if skipped:
            problems += f", {skipped} skipped (off)"
        self.summary.setText(f"Run finished at {finished}: {tables} table{'s' if tables != 1 else ''}, {problems}, {total:.2f} s")
        self.summary.setStyleSheet(f"color: {theme.STATUS_COLORS['error' if failed else 'ok']};")

    def _jump(self, item: QTreeWidgetItem, _column: int) -> None:
        target = item.data(0, Qt.ItemDataRole.UserRole) or (item.parent() and item.parent().data(0, Qt.ItemDataRole.UserRole))
        if not target:
            return
        table_id, step_id = target
        self.session.select_table(table_id)
        if step_id:
            self.session.select_step(step_id)


def _result_text(result) -> str:
    if result.status == Status.ERROR:
        return (result.error or "error").splitlines()[0]
    if result.status == Status.BLOCKED:
        return "not run (earlier error)"
    if result.status == Status.DISABLED:
        return "off"
    if result.df is not None:
        return f"{result.df.height:,} rows × {result.df.width} columns"
    return ""
