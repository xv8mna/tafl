"""The steps of the selected table."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QModelIndex, QPoint, QRect, QSize, Qt
from PySide6.QtGui import QAction, QColor, QFont, QFontMetrics, QKeySequence, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from etl_tool.core import Status, Step

from . import theme
from .action_menu import fill_action_menu
from .session import Session

_ROLE = Qt.ItemDataRole.UserRole + 1


class StepDelegate(QStyledItemDelegate):
    """A step as a small card: status stripe, number and action, summary, row count."""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        data: dict[str, Any] | None = index.data(_ROLE)
        if data is None:
            super().paint(painter, option, index)
            return
        painter.save()
        rect = option.rect
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        palette = option.palette
        painter.fillRect(rect, palette.highlight() if selected else palette.base())
        painter.fillRect(QRect(rect.left(), rect.top() + 3, 4, rect.height() - 6), theme.status_color(data["status"]))
        painter.setPen(QPen(QColor(theme.BORDER)))
        painter.drawLine(rect.bottomLeft(), rect.bottomRight())

        text_color = palette.highlightedText().color() if selected else palette.text().color()
        dim_color = QColor(theme.SELECTED_DIM) if selected else QColor(theme.TEXT_DIM)
        if not data["enabled"]:
            text_color = dim_color = QColor(theme.TEXT_DISABLED)
        bold = QFont(option.font)
        bold.setBold(True)
        line = QFontMetrics(option.font).height()
        left, right = rect.left() + 12, rect.right() - 8
        top = rect.top() + 6

        info = data["info"]
        info_width = QFontMetrics(option.font).horizontalAdvance(info) + 6 if info else 0
        painter.setFont(option.font)
        painter.setPen(theme.status_color("error") if data["status"] == "error" and not selected else dim_color)
        painter.drawText(QRect(right - info_width, top, info_width, line), Qt.AlignmentFlag.AlignRight, info)

        painter.setPen(dim_color)
        number = f"{data['number']}"
        number_width = QFontMetrics(option.font).horizontalAdvance("00") + 6
        painter.drawText(QRect(left, top, number_width, line), Qt.AlignmentFlag.AlignLeft, number)
        painter.setFont(bold)
        painter.setPen(text_color)
        name_rect = QRect(left + number_width, top, right - left - number_width - info_width - 6, line)
        name = QFontMetrics(bold).elidedText(data["name"], Qt.TextElideMode.ElideRight, name_rect.width())
        painter.drawText(name_rect, Qt.AlignmentFlag.AlignLeft, name)

        painter.setFont(option.font)
        painter.setPen(dim_color)
        summary_rect = QRect(left + number_width, top + line + 2, right - left - number_width, line)
        summary = QFontMetrics(option.font).elidedText(data["summary"], Qt.TextElideMode.ElideRight, summary_rect.width())
        painter.drawText(summary_rect, Qt.AlignmentFlag.AlignLeft, summary)
        painter.restore()

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        return QSize(200, 2 * QFontMetrics(option.font).height() + 14)


class StepsPanel(QWidget):
    def __init__(self, session: Session, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        title = QLabel("Steps", self)
        title.setObjectName("PanelTitle")
        self.list = QListWidget(self)
        self.list.setItemDelegate(StepDelegate(self.list))
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list.customContextMenuRequested.connect(self._context_menu)
        self.list.currentItemChanged.connect(self._current_changed)
        self.list.model().rowsMoved.connect(self._rows_moved)
        self.add_button = QPushButton("+ Add step", self)
        self.add_menu = QMenu(self.add_button)
        self.add_menu.aboutToShow.connect(lambda: self._fill_add_menu(self.add_menu))
        self.add_button.setMenu(self.add_menu)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 0, 3, 6)
        layout.setSpacing(4)
        layout.addWidget(title)
        layout.addWidget(self.list, 1)
        layout.addWidget(self.add_button)

        for text, shortcut, slot in [
            ("Delete step", QKeySequence.StandardKey.Delete, self.delete_current),
            ("Move step up", QKeySequence("Ctrl+Up"), lambda: self.move_current(-1)),
            ("Move step down", QKeySequence("Ctrl+Down"), lambda: self.move_current(1)),
        ]:
            shortcut_action = QAction(text, self.list)
            shortcut_action.setShortcut(shortcut)
            shortcut_action.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
            shortcut_action.triggered.connect(slot)
            self.list.addAction(shortcut_action)

        self._table_id: str | None = None
        session.structure_changed.connect(self.rebuild)
        session.selection_changed.connect(self._selection_changed)
        session.results_changed.connect(self.update_items)
        session.step_changed.connect(lambda _: self.update_items())
        self.rebuild()

    # --- building ----------------------------------------------------------------------------

    def _selection_changed(self) -> None:
        if self.session.table_id != self._table_id:
            self.rebuild()
        else:
            self._select_current()

    def rebuild(self) -> None:
        table = self.session.table
        self._table_id = self.session.table_id
        self.list.blockSignals(True)
        self.list.clear()
        if table is not None:
            for step in table.steps:
                item = QListWidgetItem()
                item.setData(Qt.ItemDataRole.UserRole, step.id)
                self.list.addItem(item)
        self.list.blockSignals(False)
        self.update_items()
        self.add_button.setEnabled(table is not None)
        self._select_current()

    def _select_current(self) -> None:
        current = self.session.step
        for row in range(self.list.count()):
            if current is not None and self.list.item(row).data(Qt.ItemDataRole.UserRole) == current.id:
                if self.list.currentRow() != row:
                    self.list.blockSignals(True)
                    self.list.setCurrentRow(row)
                    self.list.blockSignals(False)
                self.list.scrollToItem(self.list.item(row))
                return

    def update_items(self) -> None:
        table = self.session.table
        if table is None:
            return
        steps = {s.id: (number, s) for number, s in enumerate(table.steps, start=1)}
        for row in range(self.list.count()):
            item = self.list.item(row)
            entry = steps.get(item.data(Qt.ItemDataRole.UserRole))
            if entry is not None:
                data = self._item_data(*entry)
                item.setData(_ROLE, data)
                item.setToolTip(data["tooltip"])
        self.list.viewport().update()

    def _item_data(self, number: int, step: Step) -> dict[str, Any]:
        action = self.session.action(step)
        result = self.session.step_result(step)
        status, info, tooltip = "pending", "", ""
        if not step.enabled:
            status, info = "disabled", "off"
        elif result is not None:
            status = {
                Status.OK: "ok",
                Status.ERROR: "error",
                Status.SKIPPED: "skipped",
                Status.DISABLED: "disabled",
                Status.BLOCKED: "blocked",
            }[result.status]
            if result.status == Status.ERROR:
                info, tooltip = "error", result.error or ""
            elif result.status == Status.SKIPPED:
                info = "on run"
            elif result.df is not None:
                info = f"{result.df.height:,} × {result.df.width}"
            if any(level == "warning" for level, _ in result.messages):
                status = "warning" if status == "ok" else status
                tooltip = "\n".join(text for _, text in result.messages)
        return {
            "number": number,
            "name": action.name if action else f"Unknown action: {step.action}",
            "summary": self.session.engine.describe(step) + (f"   — {step.note}" if step.note else ""),
            "status": status,
            "info": info,
            "enabled": step.enabled,
            "tooltip": tooltip,
        }

    # --- interaction -------------------------------------------------------------------------

    def _current_changed(self, current: QListWidgetItem | None, _previous: QListWidgetItem | None) -> None:
        if current is not None:
            self.session.select_step(current.data(Qt.ItemDataRole.UserRole))

    def _rows_moved(self, _parent: Any, start: int, _end: int, _destination: Any, row: int) -> None:
        step_id = self.list.item(row if row < start else row - 1).data(Qt.ItemDataRole.UserRole)
        new_index = row if row < start else row - 1
        self.session.move_step(step_id, new_index)

    def _fill_add_menu(self, menu: QMenu) -> None:
        table = self.session.table
        first = table is not None and not table.steps
        fill_action_menu(menu, self.session.registry, sources=first, chosen=self.session.add_step)

    def _context_menu(self, position: QPoint) -> None:
        item = self.list.itemAt(position)
        menu = QMenu(self)
        insert = menu.addMenu("Add step after this")
        self._fill_add_menu(insert)
        insert.setEnabled(self.session.table is not None)
        if item is not None:
            step_id = item.data(Qt.ItemDataRole.UserRole)
            _, step = self.session.find_step(step_id)
            menu.addSeparator()
            menu.addAction("Turn off" if step.enabled else "Turn on", lambda: self.session.set_enabled(step_id, not step.enabled))
            menu.addAction("Duplicate", lambda: self.session.duplicate_step(step_id))
            menu.addAction("Move up\tCtrl+Up", lambda: self.move_current(-1))
            menu.addAction("Move down\tCtrl+Down", lambda: self.move_current(1))
            menu.addSeparator()
            menu.addAction("Delete\tDel", lambda: self.session.delete_step(step_id))
        menu.exec(self.list.viewport().mapToGlobal(position))

    def delete_current(self) -> None:
        step = self.session.step
        if step is not None:
            self.session.delete_step(step.id)

    def move_current(self, delta: int) -> None:
        step, index = self.session.step, self.session.step_index
        if step is not None and index is not None:
            self.session.move_step(step.id, index + delta)
