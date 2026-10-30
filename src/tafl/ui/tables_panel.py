"""The tables of the project, in their groups."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QFont, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .action_menu import fill_new_table_menu
from .session import Session, first_error, missing_settings, size_text, waiting_text

_KIND = Qt.ItemDataRole.UserRole  # "table" or "group"
_ID = Qt.ItemDataRole.UserRole + 1


class TablesTree(QTreeWidget):
    """Drag a table onto a group, or between other tables, to move it there."""

    def __init__(self, panel: TablesPanel) -> None:
        super().__init__(panel)
        self.panel = panel
        self.setHeaderHidden(True)
        self.setIndentation(16)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.EditKeyPressed)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

    def dropEvent(self, event: Any) -> None:
        item = self.currentItem()
        if item is None or item.data(0, _KIND) != "table":
            event.ignore()
            return
        group, before, after = self.panel.drop_place(self.itemAt(event.position().toPoint()), self.dropIndicatorPosition())
        event.setDropAction(Qt.DropAction.IgnoreAction)  # the session moves it; the tree is rebuilt
        event.accept()
        table_id = item.data(0, _ID)
        QTimer.singleShot(0, lambda: self.panel.session.place_table(table_id, group, before=before, after=after))


class TablesPanel(QWidget):
    def __init__(self, session: Session, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self._collapsed: set[str] = set()
        title = QLabel("Tables", self)
        title.setObjectName("PanelTitle")
        self.tree = TablesTree(self)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        self.tree.currentItemChanged.connect(self._current_changed)
        self.tree.itemChanged.connect(self._renamed)
        self.tree.itemCollapsed.connect(lambda item: self._collapsed.add(item.data(0, _ID)))
        self.tree.itemExpanded.connect(lambda item: self._collapsed.discard(item.data(0, _ID)))
        self.hint = QLabel("No tables yet.\nClick “+ New table” to import a file\nor open a project (Ctrl+O).", self)
        self.hint.setObjectName("Dim")
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.add_button = QPushButton("+ New table", self)
        self.add_menu = QMenu(self.add_button)
        self.add_menu.aboutToShow.connect(lambda: self._fill_new_menu(self.add_menu))
        self.add_button.setMenu(self.add_menu)
        group_button = QPushButton("+ Group", self)
        group_button.setToolTip("A new group: drag tables into it")
        group_button.clicked.connect(lambda: self.new_group([]))
        buttons = QHBoxLayout()
        buttons.setSpacing(4)
        buttons.addWidget(self.add_button, 1)
        buttons.addWidget(group_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 0, 3, 6)
        layout.setSpacing(4)
        layout.addWidget(title)
        layout.addWidget(self.tree, 1)
        layout.addWidget(self.hint, 1)
        layout.addLayout(buttons)

        delete = QAction("Delete table", self.tree)
        delete.setShortcut(QKeySequence.StandardKey.Delete)
        delete.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        delete.triggered.connect(self.delete_current)
        self.tree.addAction(delete)

        session.structure_changed.connect(self.rebuild)
        session.selection_changed.connect(self._select_current)
        session.results_changed.connect(self.update_icons)
        self.rebuild()

    # --- building ----------------------------------------------------------------------------

    def rebuild(self) -> None:
        project = self.session.project
        self.tree.blockSignals(True)
        self.tree.clear()
        groups: dict[str, QTreeWidgetItem] = {}
        bold = QFont(self.tree.font())
        bold.setBold(True)
        for group in project.groups:
            item = QTreeWidgetItem([group.name])
            item.setData(0, _KIND, "group")
            item.setData(0, _ID, group.id)
            item.setIcon(0, theme.group_icon(group.color))
            item.setFont(0, bold)
            item.setFlags(
                Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsDropEnabled | Qt.ItemFlag.ItemIsEditable
            )
            count = len(project.members(group.id))
            item.setToolTip(0, f"Group “{group.name}”: {count} table{'s' if count != 1 else ''}. Drag tables here.")
            self.tree.addTopLevelItem(item)
            groups[group.id] = item
        for table in project.tables:
            item = QTreeWidgetItem([table.name])
            item.setData(0, _KIND, "table")
            item.setData(0, _ID, table.id)
            item.setFlags(
                Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsDragEnabled | Qt.ItemFlag.ItemIsEditable
            )
            parent = groups.get(table.group)
            if parent is not None:
                parent.addChild(item)
            else:
                self.tree.addTopLevelItem(item)
        for group_id, item in groups.items():
            item.setExpanded(group_id not in self._collapsed)
        self.tree.blockSignals(False)
        empty = not project.tables and not project.groups
        self.tree.setVisible(not empty)
        self.hint.setVisible(empty)
        self.update_icons()
        self._select_current()

    def _table_items(self) -> list[QTreeWidgetItem]:
        items = []
        for index in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(index)
            if top.data(0, _KIND) == "table":
                items.append(top)
            items += [top.child(i) for i in range(top.childCount())]
        return items

    def update_icons(self) -> None:
        self.tree.blockSignals(True)
        for item in self._table_items():
            table_id = item.data(0, _ID)
            status = self.session.table_status(table_id)
            item.setIcon(0, theme.status_icon(status))
            off = status in ("disabled", "skipped")
            font = item.font(0)
            font.setItalic(off)
            item.setFont(0, font)
            item.setForeground(0, QColor(theme.TEXT_DISABLED if off else theme.TEXT))
            result = self.session.results.get(table_id)
            if status == "disabled":
                item.setToolTip(0, "Turned off: not calculated, its exports don't run (right-click → Turn on)")
            elif result is not None and result.skipped:
                item.setToolTip(0, f"Not calculated: it {result.skipped}")
            elif status == "incomplete" and (failed := first_error(result)) is not None:
                needed = ", ".join(missing_settings(failed[1]))
                item.setToolTip(0, f"Step {failed[0]} is not complete yet: fill in {needed}")
            elif status == "waiting" and (waiting := self.session.waits_for(table_id)) is not None:
                item.setToolTip(0, waiting_text(waiting, capital=True))
            elif result is not None and result.df is not None:
                text = size_text(result.df)
                if (ended := result.skipped_from) is not None:
                    text += f"\nRest skipped: {ended.skip_reason}"
                item.setToolTip(0, text)
            elif result is not None:
                item.setToolTip(0, result.first_error or "")
            else:
                item.setToolTip(0, "Not calculated yet")
        self.tree.blockSignals(False)

    def _select_current(self) -> None:
        self.tree.blockSignals(True)
        current = next((i for i in self._table_items() if i.data(0, _ID) == self.session.table_id), None)
        self.tree.setCurrentItem(current)
        self.tree.blockSignals(False)

    # --- interaction -------------------------------------------------------------------------

    def _current_changed(self, current: QTreeWidgetItem | None, _previous: Any) -> None:
        if current is not None and current.data(0, _KIND) == "table":
            self.session.select_table(current.data(0, _ID))

    def _renamed(self, item: QTreeWidgetItem) -> None:
        kind, ident = item.data(0, _KIND), item.data(0, _ID)
        # A rename rebuilds the tree (the item is gone then); a name that is refused or the same
        # as before (an empty one, or with spaces around it) leaves it, showing the name as it is.
        if kind == "group":
            group = self.session.project.group(ident)
            old = group.name if group is not None else ""
            self.session.rename_group(ident, item.text(0))
            if group is not None and group.name == old:
                self._show_name(item, old)
            return
        old = self.session.project.table(ident).name
        try:
            self.session.rename_table(ident, item.text(0))
        except ValueError as e:
            self._show_name(item, old)
            QMessageBox.warning(self, "Rename table", str(e))
            return
        if self.session.project.table(ident).name == old:
            self._show_name(item, old)

    def _show_name(self, item: QTreeWidgetItem, name: str) -> None:
        if item.text(0) != name:
            self.tree.blockSignals(True)
            item.setText(0, name)
            self.tree.blockSignals(False)

    def drop_place(self, target: QTreeWidgetItem | None, indicator: Any) -> tuple[str | None, str | None, str | None]:
        """Where a dropped table goes: (group, before this table, after this table)."""
        if target is None:
            return None, None, None
        kind, ident = target.data(0, _KIND), target.data(0, _ID)
        if kind == "group":
            return ident, None, None
        group = self.session.project.table(ident).group or None
        if indicator == QAbstractItemView.DropIndicatorPosition.BelowItem:
            return group, None, ident
        return group, ident, None

    def _fill_new_menu(self, menu: QMenu) -> None:
        fill_new_table_menu(menu, self.session.registry, self.session.add_table, self.session.want_new_sheet)

    def _context_menu(self, position: QPoint) -> None:
        item = self.tree.itemAt(position)
        menu = QMenu(self)
        if item is not None and item.data(0, _KIND) == "group":
            group_id = item.data(0, _ID)
            group = self.session.project.group(group_id)
            menu.addAction("Rename\tF2", lambda: self.tree.editItem(item))
            colors = menu.addMenu("Color")
            for name in theme.GROUP_COLORS:
                entry = colors.addAction(theme.group_icon(name), name.capitalize(), lambda c=name: self.session.set_group_color(group_id, c))
                entry.setCheckable(True)
                entry.setChecked(group is not None and name == group.color)
            members = [t.id for t in self.session.project.members(group_id)]
            if members:
                all_on = all(self.session.project.table(t).enabled for t in members)
                menu.addAction(
                    f"Turn all its tables {'off' if all_on else 'on'}",
                    lambda: self.session.set_table_enabled(members, not all_on),
                )
            menu.addSeparator()
            menu.addAction("Ungroup (the tables stay)", lambda: self.session.delete_group(group_id))
        else:
            self._fill_new_menu(menu.addMenu("New table"))
            menu.addAction("New group…", lambda: self.new_group([]))
            if item is not None:
                table_id = item.data(0, _ID)
                menu.addSeparator()
                menu.addAction("Rename\tF2", lambda: self.tree.editItem(item))
                menu.addAction("Duplicate", lambda: self.session.duplicate_table(table_id))
                menu.addAction("Run this table", lambda: self.session.run_project([table_id]))
                enabled = self.session.project.table(table_id).enabled
                menu.addAction(
                    "Turn table off" if enabled else "Turn table on",
                    lambda: self.session.set_table_enabled([table_id], not enabled),
                )
                self._fill_group_menu(menu.addMenu("Move to group"), table_id)
                menu.addSeparator()
                menu.addAction("Delete\tDel", lambda: confirm_delete_table(self, self.session, table_id))
        menu.exec(self.tree.viewport().mapToGlobal(position))

    def _fill_group_menu(self, menu: QMenu, table_id: str) -> None:
        project = self.session.project
        current = project.table(table_id).group
        for group in project.groups:
            entry = menu.addAction(theme.group_icon(group.color), group.name, lambda g=group.id: self.session.set_table_group([table_id], g))
            entry.setCheckable(True)
            entry.setChecked(group.id == current)
        if project.groups:
            menu.addSeparator()
        menu.addAction("New group…", lambda: self.new_group([table_id]))
        if current:
            menu.addAction("Remove from group", lambda: self.session.set_table_group([table_id], None))

    def new_group(self, table_ids: list[str]) -> None:
        name, ok = QInputDialog.getText(self, "New group", "Name of the group:", text=f"Group {len(self.session.project.groups) + 1}")
        if ok and name.strip():
            self.session.add_group(name, table_ids)

    def delete_current(self) -> None:
        item = self.tree.currentItem()
        if item is not None and item.data(0, _KIND) == "table":
            confirm_delete_table(self, self.session, item.data(0, _ID))


def confirm_delete_table(parent: QWidget, session: Session, table_id: str) -> None:
    """Ask, mentioning the tables that use it, then delete."""
    table = session.project.table(table_id)
    users = session.dependents(table_id)
    text = f"Delete the table “{table.name}”?"
    if users:
        text += "\n\nThese tables use it and will show an error: " + ", ".join(t.name for t in users)
    text += "\n\n(Edit → Undo brings it back.)"
    if QMessageBox.question(parent, "Delete table", text) == QMessageBox.StandardButton.Yes:
        session.delete_table(table_id)
