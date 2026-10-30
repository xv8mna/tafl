"""Menus listing the available actions, grouped by category."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu

from tafl.api import Action, Kind
from tafl.core import Registry

# Categories in the order a table is usually built; others follow alphabetically.
_ORDER = ["Import", "Rows", "Columns", "Text", "Calculate", "Combine", "Export", "Files", "Database"]


def fill_action_menu(menu: QMenu, registry: Registry, *, sources: bool, chosen: Callable[[Action], None]) -> None:
    """Fill ``menu`` with the source actions (first step of a table) or all other actions."""
    menu.clear()
    groups = registry.by_category()
    names = sorted(groups, key=lambda c: (_ORDER.index(c) if c in _ORDER else len(_ORDER), c))
    for category in names:
        actions = [a for a in groups[category] if (a.kind == Kind.SOURCE) == sources]
        if not actions:
            continue
        target = menu if sources else menu.addMenu(category)
        if sources and menu.actions():
            menu.addSeparator()
        for action in actions:
            entry = target.addAction(action.name, lambda a=action: chosen(a))
            entry.setToolTip(action.description.split("\n\n")[0].replace("\n", " "))
        target.setToolTipsVisible(True)
    menu.setToolTipsVisible(True)


def fill_new_table_menu(
    menu: QMenu, registry: Registry, chosen: Callable[[Action], None], sheet: Callable[[bool], None]
) -> None:
    """Fill ``menu`` with the ways to start a table: a data sheet (``sheet(paste)``), then the
    source actions."""
    fill_action_menu(menu, registry, sources=True, chosen=chosen)
    entries = (
        ("Paste from clipboard", "A sheet with the data you copied, from a spreadsheet program, an ERP system, a web page or a mail", True),
        ("Empty sheet", "A sheet to type data into", False),
    )
    first = menu.actions()[0] if menu.actions() else None
    for text, tip, paste in entries:
        entry = QAction(text, menu)
        entry.setToolTip(tip)
        entry.triggered.connect(lambda _checked=False, p=paste: sheet(p))
        menu.insertAction(first, entry)
    menu.insertSeparator(first)
