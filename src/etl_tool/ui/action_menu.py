"""Menus listing the available actions, grouped by category."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtWidgets import QMenu

from etl_tool.api import Action, Kind
from etl_tool.core import Registry

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
