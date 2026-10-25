"""Make the pictures of the user guide (docs/guide/*.png) in the real window, without showing it.

    .venv\\Scripts\\python tools\\guide_screenshots.py                     (all pictures)
    .venv\\Scripts\\python tools\\guide_screenshots.py map-groups db-import  (only these)
    .venv\\Scripts\\python tools\\guide_screenshots.py --list

The example projects of the guide are built in a temporary folder, with fresh data from
examples\\make_examples.py (--examples FOLDER builds them there and keeps them). Each picture gets
a window of its own: it opens its example, is brought into the state the picture shows through
the session and the widgets' own methods, and is rendered offscreen with the app's theme and the
PC's fonts. Menus and dialogs are drawn offscreen too; message boxes never wait for a click, and
the window settings go to a temporary folder. Where the window shows a full path, the example
folder is shown as C:\\Reports.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, QSettings, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPixmap, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QToolBar,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItemIterator,
    QWidget,
)

import tafl.ui.data_panel
import tafl.ui.filter_pane
import tafl.ui.forms
import tafl.ui.map.view
import tafl.ui.steps_panel
import tafl.ui.tables_panel
from tafl import APP_NAME
from tafl.core import EXTENSION, Project, Registry
from tafl.core.project import Parameter
from tafl.ui import functions_window, theme
from tafl.ui.checks import checks
from tafl.ui.connections_dialog import ConnectionsDialog
from tafl.ui.forms import ExpressionEditor
from tafl.ui.formula_window import FormulaWindow
from tafl.ui.main_window import MainWindow
from tafl.ui.parameters_dialog import ParametersDialog
from tafl.ui.session import Session

HERE = Path(__file__).resolve().parents[1]
OUT = HERE / "docs" / "guide"
SHOWN_ROOT = "C:\\Reports"  # the example folder, as the pictures show it
MAX_WIDTH = 1280
_USER = os.environ.get("USERNAME", "")
PRIVATE = [str(Path.home()), *([_USER] if len(_USER) > 3 else [])]  # never in a picture

_spec = importlib.util.spec_from_file_location("make_examples", HERE / "examples" / "make_examples.py")
make_examples = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(make_examples)

REGISTRY = Registry()
REGISTRY.load_builtin()  # the built-in actions only: plugins of this PC don't show in the pictures

RENAME_ORDERS = {"columns": [
    {"column": "Bestellnr", "new_name": "OrderNo"},
    {"column": "Kundennr", "new_name": "CustomerNo"},
    {"column": "Datum", "new_name": "Date"},
    {"column": "Netto", "new_name": "Net"},
]}
RENAME_CUSTOMERS = {"columns": [
    {"column": "Kundennr", "new_name": "CustomerNo"},
    {"column": "Ort", "new_name": "City"},
]}
NOT_CANCELLED = {"conditions": [{"column": "Status", "operator": "!=", "value": "storniert"}]}
BY_CUSTOMER = [{"column": "CustomerNo", "other_column": "CustomerNo"}]


# --- the example projects ---------------------------------------------------------------------


class Examples:
    """The example projects of the guide, each in a folder of its own below ``root``, with all
    file settings relative to the project file."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._built: dict[str, Path] = {}

    def _once(self, name: str, build: Callable[[], Path]) -> Path:
        if name not in self._built:
            self._built[name] = build()
        return self._built[name]

    def _data(self, name: str) -> Path:
        folder = self.root / name
        (folder / "data").mkdir(parents=True, exist_ok=True)
        make_examples.write_customers(folder / "data" / "customers.xlsx")
        make_examples.write_orders(folder / "data" / "orders.csv")
        return folder

    def demo(self) -> Path:
        """The demo of the download (examples\\demo.tafl)."""
        return self._once("demo", lambda: make_examples.build_project().save(self._data("demo") / f"demo{EXTENSION}"))

    def excel_check(self) -> Path:
        return self._once(
            "excel-check",
            lambda: make_examples.build_excel_check().save(self._data("excel-check") / f"excel-check{EXTENSION}"),
        )

    def first(self, upto: int) -> Path:
        """The project that chapter 2 builds, with its first ``upto`` steps (0 to 11); with all of
        them, the table orders is called Revenue by customer."""
        folder = self._data("first")
        project = Project(name="First project")
        build: list[tuple[str, str, dict[str, Any]]] = [
            ("orders", "csv.import", {"file": "data/orders.csv"}),
            ("orders", "columns.rename", RENAME_ORDERS),
            ("orders", "rows.filter", NOT_CANCELLED),
            ("orders", "calc.columns", {"columns": [{"name": "Gross", "formula": "ROUND([Net] * 1.19, 2)"}]}),
            ("orders", "calc.aggregate", {"group_by": ["CustomerNo"], "calculations": [
                {"function": "sum", "column": "Gross", "name": "Revenue"},
                {"function": "count_rows", "name": "Orders"},
            ]}),
            ("customers", "excel.import", {"file": "data/customers.xlsx", "header_row": 3}),
            ("customers", "columns.rename", RENAME_CUSTOMERS),
            ("customers", "text.clean", {"columns": ["Name"], "collapse_spaces": True}),
            ("orders", "table.merge", {"other": "customers", "match": BY_CUSTOMER, "columns": ["Name", "City"]}),
            ("orders", "rows.sort", {"by": [{"column": "Revenue", "order": "desc"}]}),
            ("orders", "excel.export", {
                "file": "output/revenue.xlsx", "sheet": "Revenue",
                "column_formats": [{"column": "Revenue", "format": "#,##0.00 €"}],
            }),
        ]
        tables: dict[str, Any] = {}
        for name, action_id, params in build[:upto]:
            if name not in tables:
                tables[name] = project.add_table(name)
            if action_id == "table.merge":
                params = {**params, "other": tables["customers"].id}
            project.add_step(tables[name], REGISTRY.actions[action_id], params)
        if upto >= len(build):
            tables["orders"].name = "Revenue by customer"
        return project.save(folder / f"first{EXTENSION}")

    def sales(self) -> Path:
        """Seven tables in three groups, for the map."""
        return self._once("sales", self._sales)

    def _sales(self) -> Path:
        folder = self._data("sales")
        project = Project(name="Sales")
        customers = _table(project, "Customers", [
            ("excel.import", {"file": "data/customers.xlsx", "sheet": "Kunden", "header_row": 3}),
            ("columns.rename", RENAME_CUSTOMERS),
            ("text.clean", {"columns": ["Name"], "collapse_spaces": True}),
        ])
        orders = _table(project, "Orders", [
            ("csv.import", {"file": "data/orders.csv"}),
            ("columns.rename", RENAME_ORDERS),
            ("rows.filter", NOT_CANCELLED),
            ("calc.columns", {"columns": [
                {"name": "Gross", "formula": "ROUND([Net] * 1.19, 2)"},
                {"name": "Month", "formula": "STRFTIME([Date], '%Y-%m')"},
            ]}),
        ])
        cancelled = _table(project, "Cancelled orders", [
            ("csv.import", {"file": "data/orders.csv"}),
            ("columns.rename", RENAME_ORDERS),
            ("rows.filter", {"conditions": [{"column": "Status", "operator": "=", "value": "storniert"}]}),
        ])
        full = _table(project, "Orders with customers", [
            ("table.derive", {"table": orders.id}),
            ("table.merge", {"other": customers.id, "match": BY_CUSTOMER, "columns": ["Name", "City"]}),
            ("excel.export", {"file": "output/orders.xlsx", "sheet": "Orders"}),
        ])
        by_customer = _table(project, "Revenue by customer", [
            ("table.derive", {"table": full.id}),
            ("calc.aggregate", {"group_by": ["Name", "City"], "calculations": [
                {"function": "sum", "column": "Gross", "name": "Revenue"},
                {"function": "count_rows", "name": "Orders"},
            ]}),
            ("rows.sort", {"by": [{"column": "Revenue", "order": "desc"}]}),
            ("excel.export", {"file": "output/revenue.xlsx", "sheet": "Revenue"}),
        ])
        by_month = _table(project, "Revenue by month", [
            ("table.derive", {"table": orders.id}),
            ("calc.aggregate", {"group_by": ["Month"], "calculations": [
                {"function": "sum", "column": "Gross", "name": "Revenue"},
            ]}),
            ("rows.sort", {"by": [{"column": "Month"}]}),
            ("csv.export", {"file": "output/revenue by month.csv"}),
        ])
        unknown = _table(project, "Unknown customers", [
            ("table.derive", {"table": orders.id}),
            ("table.merge", {"other": customers.id, "how": "non_matching", "match": BY_CUSTOMER}),
        ])
        for name, color, members in (
            ("Sources", "blue", [customers, orders, cancelled]),
            ("Reports", "green", [full, by_customer, by_month]),
            ("Checks", "orange", [unknown]),
        ):
            group = project.add_group(name, color)
            for table in members:
                table.group = group.id
        return project.save(folder / f"sales{EXTENSION}")

    def monthly(self) -> Path:
        """Parameters: the day a monthly report is for, its month, and a limit."""
        return self._once("monthly", self._monthly)

    def _monthly(self) -> Path:
        folder = self._data("monthly")
        project = Project(name="Monthly")
        project.parameters = [
            Parameter("Day", "2026-03-31"),
            Parameter("Month", "=STRFTIME({Day}, '%Y-%m')"),
            Parameter("Limit", "1000"),
        ]
        _table(project, "Orders of the month", [
            ("csv.import", {"file": "data/orders.csv"}),
            ("columns.rename", RENAME_ORDERS),
            ("rows.filter_formula", {"condition": "STRFTIME([Date], '%Y-%m') = {Month} AND [Status] <> 'storniert'"}),
            ("calc.columns", {"columns": [{"name": "Size", "formula": "IIF([Net] >= {Limit}, 'large', 'normal')"}]}),
            ("excel.export", {"file": "output/orders {Month}.xlsx", "sheet": "{Month}"}),
        ])
        return project.save(folder / f"monthly{EXTENSION}")

    def database(self) -> Path:
        """A SQLite database, and a SQL Server connection that no step uses (nothing connects to it)."""
        return self._once("database", self._database)

    def _database(self) -> Path:
        folder = self._data("database")
        _shop(folder / "data" / "shop.sqlite")
        project = Project(name="Database")
        project.add_connection("Shop", "sqlite", file="data/shop.sqlite")
        project.add_connection(
            "Warehouse", "sqlserver", server=".\\SQLEXPRESS", database="Sales", driver="", trust_certificate=True
        )
        query = "SELECT CustomerNo, Name, City\nFROM customers\nWHERE City <> 'Wien'"
        _table(project, "Customers", [("db.import", {"connection": "Shop", "mode": "query", "query": query})])
        command = "INSERT INTO load_log (Loaded, Rows)\nSELECT datetime('now'), COUNT(*) FROM revenue"
        _table(project, "Revenue by month", [
            ("csv.import", {"file": "data/orders.csv"}),
            ("columns.rename", RENAME_ORDERS),
            ("rows.filter", NOT_CANCELLED),
            ("calc.columns", {"columns": [{"name": "Month", "formula": "STRFTIME([Date], '%Y-%m')"}]}),
            ("calc.aggregate", {"group_by": ["Month"], "calculations": [
                {"function": "sum", "column": "Net", "name": "Revenue"},
                {"function": "count_rows", "name": "Orders"},
            ]}),
            ("db.export", {"connection": "Shop", "table": "revenue", "mode": "replace_keys", "key_columns": ["Month"]}),
            ("db.command", {"connection": "Shop", "sql": command}),
        ])
        return project.save(folder / f"database{EXTENSION}")

    def folders(self) -> Path:
        """One CSV file per month in exports\\2026\\01 … 09, imported as one table."""
        return self._once("folders", self._folders)

    def _folders(self) -> Path:
        folder = self._data("folders")
        header, rows = _order_rows(folder)
        for month in sorted({row[2][3:] for row in rows}, key=lambda m: (m[3:], m[:2])):  # "02.2026"
            number, year = month.split(".")
            path = folder / "exports" / year / number / f"orders_{year}-{number}.csv"
            _write_rows(path, header, [row for row in rows if row[2][3:] == month])
        project = Project(name="Folders")
        _table(project, "All orders", [
            ("csv.import", {
                "source": "folder", "folder": "exports", "pattern": "orders_*.csv", "subfolders": True,
                "path_column": True,
            }),
            ("calc.columns", {"columns": [
                {"name": "Year", "formula": "SPLIT_PART([Path], '\\', 1)"},
                {"name": "Month", "formula": "SPLIT_PART([Path], '\\', 2)"},
            ]}),
        ])
        return project.save(folder / f"folders{EXTENSION}")

    def inbox(self) -> Path:
        """A daily inbox, as it is before its first run: three days with orders, and a file with
        only its header row. Built again each time (a run moves the files)."""
        folder = self.root / "daily-inbox"
        shutil.rmtree(folder, ignore_errors=True)
        self._data("daily-inbox")
        header, rows = _order_rows(folder)
        for day in ("17.02.2026", "20.02.2026", "28.02.2026"):
            d, m, y = day.split(".")
            _write_rows(folder / "inbox" / f"orders_{y}-{m}-{d}.csv", header, [row for row in rows if row[2] == day])
        _write_rows(folder / "inbox" / "orders_2026-02-22.csv", header, [])
        (folder / "archive").mkdir()
        project = Project(name="Daily inbox")
        daily = _table(project, "Daily orders", [
            ("csv.import", {"source": "folder", "folder": "inbox", "pattern": "orders_*.csv", "no_files": "skip"}),
            ("columns.rename", RENAME_ORDERS),
            ("rows.filter", NOT_CANCELLED),
            ("excel.export", {"file": "output/daily orders.xlsx", "sheet": "Orders", "skip_empty": True}),
            ("files.move", {"which": "imported", "target": "archive"}),
        ])
        _table(project, "Large orders", [
            ("table.derive", {"table": daily.id}),
            ("rows.filter", {"conditions": [{"column": "Net", "operator": ">=", "value": "2000"}]}),
            ("flow.skip_if_empty", {"which": "this"}),
            ("csv.export", {"file": "output/large orders.csv"}),
        ])
        return project.save(folder / f"inbox{EXTENSION}")

    def inbox_new_day(self) -> None:
        """Another day arrives in the inbox: one order, below 2,000."""
        folder = self.root / "daily-inbox"
        header, rows = _order_rows(folder)
        _write_rows(folder / "inbox" / "orders_2026-02-21.csv", header, [row for row in rows if row[2] == "21.02.2026"])

    def report(self) -> Path:
        """A daily report: orders from an inbox and customers from a database, written to an Excel
        file and a database table; imports, exports and commands for the Find panel."""
        return self._once("report", self._report)

    def _report(self) -> Path:
        folder = self._data("daily-report")
        _shop(folder / "data" / "shop.sqlite")
        header, rows = _order_rows(folder)
        for day in ("17.02.2026", "20.02.2026"):
            d, m, y = day.split(".")
            _write_rows(folder / "inbox" / f"orders_{y}-{m}-{d}.csv", header, [row for row in rows if row[2] == day])
        project = Project(name="Daily report")
        project.add_connection("Shop", "sqlite", file="data/shop.sqlite")
        query = "SELECT CustomerNo, Name, City\nFROM customers"
        customers = _table(project, "Customers", [("db.import", {"connection": "Shop", "mode": "query", "query": query})])
        daily = _table(project, "Daily orders", [
            ("csv.import", {"source": "folder", "folder": "inbox", "pattern": "orders_*.csv", "no_files": "skip"}),
            ("columns.rename", RENAME_ORDERS),
            ("rows.filter", NOT_CANCELLED),
            ("table.merge", {"other": customers.id, "match": BY_CUSTOMER, "columns": ["Name", "City"]}),
            ("excel.export", {"file": "output/daily orders.xlsx", "sheet": "Orders"}),
            ("files.move", {"which": "imported", "target": "archive"}),
        ])
        command = "INSERT INTO load_log (Loaded, Rows)\nSELECT datetime('now'), COUNT(*) FROM revenue"
        _table(project, "Revenue by day", [
            ("table.derive", {"table": daily.id}),
            ("calc.aggregate", {"group_by": ["Date"], "calculations": [
                {"function": "sum", "column": "Net", "name": "Revenue"},
            ]}),
            ("db.export", {"connection": "Shop", "table": "revenue", "mode": "replace_keys", "key_columns": ["Date"]}),
            ("db.command", {"connection": "Shop", "sql": command}),
        ])
        return project.save(folder / f"daily-report{EXTENSION}")


def _shop(path: Path) -> None:
    """The SQLite database of chapter 8: the customers, and an empty table load_log."""
    con = sqlite3.connect(path)
    with con:
        con.execute("CREATE TABLE IF NOT EXISTS customers (CustomerNo TEXT PRIMARY KEY, Name TEXT, City TEXT)")
        con.executemany(
            "INSERT OR REPLACE INTO customers VALUES (?, ?, ?)",
            [(number, " ".join(name.split()), city) for number, name, city in make_examples.CUSTOMERS],
        )
        con.execute("CREATE TABLE IF NOT EXISTS load_log (Loaded TEXT, Rows INTEGER)")
    con.close()


def _table(project: Project, name: str, steps: list[tuple[str, dict[str, Any]]]) -> Any:
    """A new table with these steps (action id, settings)."""
    table = project.add_table(name)
    for action_id, params in steps:
        project.add_step(table, REGISTRY.actions[action_id], params)
    return table


def _order_rows(folder: Path) -> tuple[list[str], list[list[str]]]:
    with open(folder / "data" / "orders.csv", encoding="cp1252", newline="") as f:
        rows = list(csv.reader(f, delimiter=";"))
    return rows[0], rows[1:]


def _write_rows(path: Path, header: list[str], rows: list[list[str]]) -> None:
    """A CSV file as the orders file is written: semicolons, ANSI, CRLF."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="cp1252", newline="") as f:
        csv.writer(f, delimiter=";", lineterminator="\r\n").writerows([header, *rows])


# --- rendering ------------------------------------------------------------------------------------


class Problems:
    """What went wrong while a picture was made: a message box, an error in a slot."""

    found: list[str] = []

    @classmethod
    def add(cls, text: str) -> None:
        cls.found.append(text)


class OffscreenGuard(QObject):
    """Every window is drawn offscreen, also one the app opens by itself; a message box or a
    dialog that nobody expects is noted and closed at once, so nothing waits for a click."""

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Show and isinstance(watched, QWidget) and watched.isWindow():
            watched.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)  # before it reaches the screen
            if isinstance(watched, QMessageBox) or (isinstance(watched, QDialog) and not watched.property("expected")):
                text = watched.text() if isinstance(watched, QMessageBox) else ""
                Problems.add(f"unexpected dialog: {watched.windowTitle()} {text}".strip())
                QTimer.singleShot(0, watched.reject)
        return False


class GrabMenu(QMenu):
    """Stands in for QMenu where the app opens a context menu: instead of waiting for a click,
    the menu is drawn offscreen and kept as a picture (with the point it was opened at)."""

    grabbed: list[tuple[QPixmap, QPoint]] = []

    def exec(self, *args: Any, **kwargs: Any) -> None:
        point = next((a for a in args if isinstance(a, QPoint)), QPoint())
        GrabMenu.grabbed.append((menu_picture(self), point))
        self.hide()
        return None


class OffscreenFormulaWindow(FormulaWindow):
    """The formula window (F2), drawn offscreen."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        self.setProperty("expected", True)


def menu_picture(menu: QMenu, active: Any = None) -> QPixmap:
    """The menu drawn offscreen (``active``: the highlighted entry); it stays open (offscreen)."""
    menu.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    menu.adjustSize()
    menu.show()
    if active is not None:
        menu.setActiveAction(active)
    settle(0.1)
    return menu.grab()


def entry(menu: QMenu, text: str) -> Any:
    """The entry of ``menu`` with this text (without & and the shortcut)."""
    for action in menu.actions():
        if action.text().replace("&", "").split("\t")[0] == text:
            return action
    raise LookupError(f"no entry {text!r} in the menu: {[a.text() for a in menu.actions()]}")


def settle(seconds: float = 0.3) -> None:
    """Let the window do what is pending (layout, timers) for a moment."""
    app = QApplication.instance()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()


class Shot:
    """A main window for one picture, offscreen, with the window settings of a first start."""

    def __init__(self, scrub: list[re.Pattern[str]], size: tuple[int, int] = (1280, 800)) -> None:
        QSettings().clear()
        self.scrub_patterns = scrub
        self.session = Session(REGISTRY)
        self.window = MainWindow(self.session)
        self.window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        self.window.show()
        self.panels = (280, 420)
        self.resize(*size)
        GrabMenu.grabbed.clear()

    # --- state --------------------------------------------------------------------------------

    def resize(self, width: int, height: int) -> None:
        """The window size, with the panels beside the data as wide as on a big screen."""
        self.window.resize(width, height)
        settle(0.1)
        self.window.splitter.setSizes([self.panels[0], width - sum(self.panels), self.panels[1]])
        settle(0.1)

    def set_panels(self, left: int, settings: int) -> None:
        """The widths of the left column (tables and steps) and of the step settings."""
        self.panels = (left, settings)
        self.resize(self.window.width(), self.window.height())

    def open(self, path: Path, table: str | None = None, step: int | None = None) -> None:
        """Open a project. With several tables, all are calculated (as after a look at the map),
        so that each shows its status. The status bar says "Opened demo.tafl" only for the demo:
        the reader builds the other projects, and doesn't open them."""
        if not self.window.open_path(path):
            raise RuntimeError(f"could not open {path}")
        self.wait()
        if len(self.session.project.tables) > 1:
            self.session.set_overview(True)
            self.wait()
            self.session.set_overview(False)
        if table is not None:
            self.select(table, step)
        if path.stem != "demo":
            self.quiet()

    def quiet(self) -> None:
        """An empty status bar, as a few seconds after a message."""
        self.window.statusBar().clearMessage()

    def select(self, table: str, step: int | None = None) -> None:
        """Select a table (and its step ``step``, counted from 1), as a click does."""
        self.session.select_table(self.session.project.table(table).id)
        if step is not None:
            self.session.select_step(self.session.table.steps[step - 1].id)
        self.wait()

    def step(self, table: str, number: int) -> Any:
        """Step ``number`` (counted from 1) of a table."""
        return self.session.project.table(table).steps[number - 1]

    def wait(self, seconds: float = 60) -> None:
        """Until the calculation, the run, the checks of the formulas and the map are done."""
        session, view = self.session, self.window.map_view
        deadline = time.monotonic() + seconds
        quiet_since = None
        while time.monotonic() < deadline:
            QApplication.processEvents()
            busy = (
                session.busy or session.running or session._timer.isActive() or checks().busy
                or view._rebuild_timer.isActive()
                or any(e.checking for e in self.window.findChildren(ExpressionEditor) if e.isVisible())
            )
            if busy:
                quiet_since = None
            elif quiet_since is None:
                quiet_since = time.monotonic()
            elif time.monotonic() - quiet_since > 0.6:
                settle(0.1)
                return
            time.sleep(0.02)
        raise TimeoutError("the window was still busy")

    def run(self) -> None:
        """Run project (F5), and wait for the run and the calculation after it."""
        self.window.run_project()
        settle(0.2)
        self.wait()

    def fit_tables(self) -> None:
        """The tables panel just tall enough for its tables: the steps get the rest."""
        panel, tree = self.window.tables_panel, self.window.tables_panel.tree
        rows, item = 0, tree.topLevelItem(0)
        while item is not None:
            rows += 1
            item = tree.itemBelow(item)
        height = panel.height() - tree.viewport().height() + rows * (tree.sizeHintForRow(0) or 20) + 4
        total = sum(self.window.left_splitter.sizes())
        self.window.left_splitter.setSizes([height, total - height])
        settle(0.1)

    def show_run_results(self) -> None:
        """The Run results dock with every table and step opened, tall enough for all its lines,
        and the Result column wide enough for its texts (as a double-click on its edge does)."""
        dock, tree = self.window.run_dock, self.window.run_panel.tree
        dock.show()
        iterator = QTreeWidgetItemIterator(tree)
        while iterator.value() is not None:
            item = iterator.value()
            if item.childCount():
                item.setExpanded(True)
            iterator += 1
        tree.resizeColumnToContents(1)
        tree.setColumnWidth(1, min(tree.columnWidth(1) + 10, 620))
        self.resize(self.window.width(), 1400)  # room for the dock to show all lines, then just enough
        self.window.resizeDocks([dock], [1000], Qt.Orientation.Vertical)
        settle(0.2)
        last = tree.topLevelItem(0)
        while last is not None and tree.itemBelow(last) is not None:
            last = tree.itemBelow(last)
        content = tree.visualItemRect(last).bottom() if last is not None else 0
        needed = dock.height() - tree.viewport().height() + content + 8
        self.window.resizeDocks([dock], [needed], Qt.Orientation.Vertical)
        settle(0.2)

    def fit_settings(self) -> None:
        """A window tall enough for all of the step settings (they need no scrolling)."""
        bar = self.window.settings_panel.scroll.verticalScrollBar()
        for _ in range(6):
            settle(0.2)
            if bar.maximum() <= 0:
                return
            self.resize(self.window.width(), self.window.height() + bar.maximum() + 40)
            self.wait()
        raise RuntimeError("the step settings still need scrolling")

    def editors(self) -> list[ExpressionEditor]:
        """The formula fields of the step settings, from top to bottom."""
        found = [e for e in self.window.settings_panel.form.findChildren(ExpressionEditor) if e.isVisible()]
        return sorted(found, key=lambda e: e.mapTo(self.window, QPoint(0, 0)).y())

    def type_formula(self, editor: ExpressionEditor, text: str) -> None:
        """Type into a formula field without applying it (no Enter, the focus stays)."""
        editor._edit.setFocus()
        editor._edit.setPlainText(text)
        editor._edit.moveCursor(QTextCursor.MoveOperation.End)
        settle(0.2)
        self.wait()

    # --- pictures -----------------------------------------------------------------------------

    def rect(self, widget: QWidget) -> QRect:
        """Where ``widget`` is, in the window's coordinates."""
        return QRect(widget.mapTo(self.window, QPoint(0, 0)), widget.size())

    def picture(self, *overlays: tuple[QPixmap, QPoint]) -> QPixmap:
        """The window (client area), with menus drawn onto it at window points."""
        self.neutral_paths(self.window)
        pixmap = self.window.grab()
        if overlays:
            painter = QPainter(pixmap)
            for overlay, point in overlays:
                draw_shadow(painter, QRect(point, overlay.size()))
                painter.drawPixmap(point, overlay)
            painter.end()
        return pixmap

    def widget_picture(self, widget: QWidget) -> QPixmap:
        """A dialog or window of its own (without the title bar)."""
        self.neutral_paths(widget)
        return widget.grab()

    def settings_picture(self, until: QWidget | str | None = None, also: QRect | None = None) -> QPixmap:
        """The step settings (see ``settings_rect``), grown by ``also`` (e.g. a list opened on them)."""
        area = self.settings_rect(until)
        if also is not None:
            area = union(area, also, within=self.window.rect())
        return self.picture().copy(area)

    def settings_rect(self, until: QWidget | str | None = None) -> QRect:
        """The step settings from the top to the end of the form, or to the end of ``until``: a
        widget, "help" (the action's help text) or the key of a setting (its help text)."""
        self.fit_settings()
        panel = self.window.settings_panel
        form = panel.form
        pad = 10  # below the form or the help text: room before the next field
        if until is None:
            bottom = max(self.rect(w).bottom() for w in form.findChildren(QWidget) if w.isVisible() and w.parent() is form)
        else:
            if isinstance(until, str) and until != "help":
                pad = 3  # a help text: the next field follows closely
            if until == "help":
                widget = panel.description
            elif isinstance(until, str):
                row = next(r for r in form._rows if r[0].key == until)
                widget = row[3] or row[2]
            else:
                widget = until
            bottom = self.rect(widget).bottom()
        area = self.rect(panel)
        return QRect(area.left(), area.top(), area.width(), bottom - area.top() + pad)

    def neutral_paths(self, top: QWidget) -> None:
        """Show the example folder as C:\\Reports wherever the window shows a full path; stop if a
        private path is left anywhere."""

        def neutral(text: str) -> str:
            for pattern in self.scrub_patterns:
                text = pattern.sub(lambda _match: SHOWN_ROOT, text)
            return text

        for label in top.findChildren(QLabel):
            if (new := neutral(label.text())) != label.text():
                label.setText(new)
        for tree in top.findChildren(QTreeWidget):
            iterator = QTreeWidgetItemIterator(tree)
            while iterator.value() is not None:
                item = iterator.value()
                for column in range(item.columnCount()):
                    if (new := neutral(item.text(column))) != item.text(column):
                        item.setText(column, new)
                iterator += 1
        for edit in top.findChildren(QPlainTextEdit):
            if (new := neutral(edit.toPlainText())) != edit.toPlainText():
                edit.setPlainText(new)
        for edit in top.findChildren(QLineEdit):
            if (new := neutral(edit.text())) != edit.text():
                edit.setText(new)
        bar = self.window.statusBar()
        if (new := neutral(bar.currentMessage())) != bar.currentMessage():
            bar.showMessage(new)
        settle(0.05)
        for text in _texts(top) + [bar.currentMessage()]:
            folded = text.casefold()
            if any(p.casefold() in folded for p in PRIVATE) or any(p.search(text) for p in self.scrub_patterns):
                raise RuntimeError(f"a private path would show: {text!r}")

    def close(self) -> None:
        for window in functions_window.FunctionsWindow.open_windows():
            window.close()
        self.session._set_dirty(False)  # closing must not ask about unsaved changes
        if self.session.running:
            self.session.cancel_run()
            self.session.wait()
        self.window.close()
        self.window.deleteLater()
        settle(0.1)


def _texts(top: QWidget) -> list[str]:
    """Every text that ``top`` may show."""
    texts = [label.text() for label in top.findChildren(QLabel)]
    texts += [edit.toPlainText() for edit in top.findChildren(QPlainTextEdit)]
    texts += [edit.text() for edit in top.findChildren(QLineEdit)]
    for tree in top.findChildren(QTreeWidget):
        iterator = QTreeWidgetItemIterator(tree)
        while iterator.value() is not None:
            item = iterator.value()
            texts += [item.text(c) for c in range(item.columnCount())]
            iterator += 1
    return texts


def draw_shadow(painter: QPainter, rect: QRect) -> None:
    """A soft shadow under a menu drawn onto the window, as a menu has on the screen."""
    for offset, alpha in ((5, 25), (4, 35), (3, 45), (2, 55)):
        painter.fillRect(rect.translated(offset, offset), QColor(0, 0, 0, alpha))


def union(*rects: QRect, margin: int = 0, within: QRect | None = None) -> QRect:
    """The rectangle around all of ``rects`` (grown by ``margin``, cut to ``within``)."""
    area = QRect(rects[0])
    for rect in rects[1:]:
        area = area.united(rect)
    area = area.adjusted(-margin, -margin, margin, margin)
    return area.intersected(within) if within is not None else area


def beside(panel: QRect, menu: QRect, within: QRect) -> QRect:
    """A panel and a menu opened on it: the panel, grown to the right and down by the menu."""
    area = panel.united(menu)
    if menu.right() > panel.right():
        area.setRight(menu.right() + 10)
    if menu.bottom() > panel.bottom():
        area.setBottom(menu.bottom() + 10)
    return area.intersected(within)


def callout(painter: QPainter, center: QPoint, number: int) -> None:
    """A numbered circle (the numbers are explained in the guide)."""
    radius = 12
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor("#ffffff"), 2))
    painter.setBrush(QColor(theme.ACCENT))
    painter.drawEllipse(center, radius, radius)
    font = QFont(painter.font())
    font.setBold(True)
    font.setPixelSize(14)
    painter.setFont(font)
    box = QRect(center.x() - radius, center.y() - radius, 2 * radius, 2 * radius)
    painter.drawText(box, Qt.AlignmentFlag.AlignCenter, str(number))


# --- the pictures ---------------------------------------------------------------------------------


@dataclass
class Picture:
    """A picture of the guide: ``make(examples, shot)`` returns it; ``shot(size)`` gives a new
    window for it."""

    name: str
    shows: str
    make: Callable[[Examples, Callable[..., Shot]], QPixmap]


PICTURES: dict[str, Picture] = {}


def picture(name: str, shows: str) -> Callable:
    """Register the function below as the maker of picture ``name`` (docs/guide/<name>.png)."""

    def register(make: Callable) -> Callable:
        PICTURES[name] = Picture(name, shows, make)
        return make

    return register


# Chapter 1: getting started


@picture("first-start", "the window at its first start, without a project")
def first_start(ex: Examples, shot: Callable[..., Shot]) -> QPixmap:
    s = shot()
    s.wait()
    return s.picture()


@picture("demo-opened", "the demo just opened: Orders, step 4 (the merge), with its data and settings")
def demo_opened(ex: Examples, shot: Callable[..., Shot]) -> QPixmap:
    s = shot()
    s.open(ex.demo(), "Orders", 4)
    s.fit_tables()
    return s.picture()


# Chapter 2: the first project


@picture("first-new-table-menu", "the + New table menu with the imports")
def first_new_table_menu(ex: Examples, shot: Callable[..., Shot]) -> QPixmap:
    s = shot()
    s.open(ex.first(0))
    panel = s.window.tables_panel
    panel._fill_new_menu(panel.add_menu)
    menu = menu_picture(panel.add_menu, entry(panel.add_menu, "Import CSV / text files"))
    point = panel.add_button.mapTo(s.window, QPoint(0, panel.add_button.height()))
    full = s.picture((menu, point))
    panel.add_menu.hide()
    return full.copy(beside(s.rect(panel), QRect(point, menu.size()), full.rect()))


@picture("first-import", "orders.csv imported: 200 rows with the type of each column")
def first_import(ex: Examples, shot: Callable[..., Shot]) -> QPixmap:
    s = shot()
    s.open(ex.first(1), "orders", 1)
    return s.picture()


@picture("add-step-menu", "the + Add step menu with its Columns submenu")
def add_step_menu(ex: Examples, shot: Callable[..., Shot]) -> QPixmap:
    s = shot()
    s.open(ex.first(1), "orders", 1)
    panel = s.window.steps_panel
    menu = panel.add_menu
    panel._fill_add_menu(menu)
    columns = entry(menu, "Columns")
    main = menu_picture(menu, columns)
    sub = columns.menu()
    if not sub.isVisible():  # opened by the highlight, as a hover opens it
        sub.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        sub.adjustSize()
        first = sub.actionGeometry(sub.actions()[0]).top()
        sub.move(menu.pos() + QPoint(menu.width() - 2, menu.actionGeometry(columns).top() - first))
        sub.show()
    side = menu_picture(sub, entry(sub, "Rename columns"))
    offset = sub.pos() - menu.pos()
    sub.hide()
    menu.hide()
    pad = 14
    area = union(QRect(QPoint(0, 0), main.size()), QRect(offset, side.size()))
    canvas = QPixmap(area.width() + 2 * pad, area.height() + 2 * pad)
    canvas.fill(QColor(theme.WINDOW))
    painter = QPainter(canvas)
    origin = QPoint(pad, pad) - area.topLeft()
    for pixmap, point in ((main, origin), (side, origin + offset)):
        draw_shadow(painter, QRect(point, pixmap.size()))
        painter.drawPixmap(point, pixmap)
    painter.end()
    return canvas


@picture("first-rename-settings", "Rename columns: Bestellnr to OrderNo, Kundennr to CustomerNo, …")
def first_rename_settings(ex: Examples, shot: Callable[..., Shot]) -> QPixmap:
    s = shot()
    s.open(ex.first(2), "orders", 2)
    return s.settings_picture("help")


@picture("filter-rows-settings", "Filter rows: Status does not equal storniert")
def filter_rows_settings(ex: Examples, shot: Callable[..., Shot]) -> QPixmap:
    s = shot()
    s.open(ex.first(3), "orders", 3)
    return s.settings_picture()


@picture("first-calculate", "Calculate columns: Gross = ROUND([Net] * 1.19, 2), with the line under the formula")
def first_calculate(ex: Examples, shot: Callable[..., Shot]) -> QPixmap:
    s = shot()
    s.open(ex.first(4), "orders", 4)
    return s.settings_picture()


@picture("first-aggregate", "Aggregate by CustomerNo: Revenue and Orders, 7 rows")
def first_aggregate(ex: Examples, shot: Callable[..., Shot]) -> QPixmap:
    s = shot(size=(1280, 690))  # 7 rows: no room for an empty grid
    s.open(ex.first(5), "orders", 5)
    return s.picture()


@picture("excel-import", "customers.xlsx imported, the column names in row 3")
def excel_import(ex: Examples, shot: Callable[..., Shot]) -> QPixmap:
    s = shot()
    s.open(ex.first(6), "customers", 1)
    return s.picture()


@picture("first-merge", "Merge with another table: Name and City looked up by CustomerNo")
def first_merge(ex: Examples, shot: Callable[..., Shot]) -> QPixmap:
    s = shot()
    s.open(ex.first(9), "orders", 6)
    return s.picture()


@picture("first-export-settings", "Export to Excel file: output/revenue.xlsx, sheet Revenue, a format for Revenue")
def first_export_settings(ex: Examples, shot: Callable[..., Shot]) -> QPixmap:
    s = shot()
    s.open(ex.first(11), "Revenue by customer", 8)
    return s.settings_picture()


@picture("first-run-results", "Run results after F5: both tables, and the file the export wrote")
def first_run_results(ex: Examples, shot: Callable[..., Shot]) -> QPixmap:
    s = shot(size=(1100, 800))
    s.open(ex.first(11), "Revenue by customer", 8)
    s.run()
    s.show_run_results()
    return s.picture().copy(s.rect(s.window.run_dock))


# Chapter 3: the window


@picture("window-parts", "the parts of the window, numbered")
def window_parts(ex: Examples, shot: Callable[..., Shot]) -> QPixmap:
    s = shot()
    s.open(ex.demo(), "Orders", 6)
    s.fit_tables()
    w = s.window
    header = w.data_panel.layout().itemAt(0).layout()  # the title line of the data
    header.insertSpacing(header.indexOf(w.data_panel.filter_button), 30)  # room for 4 left of Filters
    settle(0.2)
    pixmap = s.picture()
    painter = QPainter(pixmap)

    def after_title(panel: QWidget, text: str) -> QPoint:
        title = next(label for label in panel.findChildren(QLabel) if label.text() == text)
        corner = s.rect(title).topLeft()
        return QPoint(corner.x() + title.fontMetrics().horizontalAdvance(text) + 22, corner.y() + title.height() // 2)

    grid = s.rect(w.data_panel.view)
    filters = s.rect(w.data_panel.filter_button)
    toolbar = next(iter(w.findChildren(QToolBar)))
    buttons = union(*(s.rect(b) for b in toolbar.findChildren(QToolButton) if b.isVisible() and b.defaultAction()))
    outline = buttons.adjusted(-3, -2, 3, 2)  # the whole toolbar, so that 6 names all of it
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor(theme.ACCENT), 2))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRoundedRect(outline, 6, 6)
    for number, center in (
        (1, after_title(w.tables_panel, "Tables")),
        (2, after_title(w.steps_panel, "Steps")),
        (3, QPoint(grid.left() + 16, grid.top() + 20)),
        (4, QPoint(filters.left() - 16, filters.center().y())),
        (5, after_title(w.settings_panel, "Step settings")),
        (6, QPoint(outline.right() + 20, outline.center().y())),
    ):
        callout(painter, center, number)
    painter.end()
    return pixmap


@picture("step-menu", "the right-click menu of a step")
def step_menu(ex: Examples, shot: Callable[..., Shot]) -> QPixmap:
    s = shot()
    s.open(ex.demo(), "Orders", 5)
    s.fit_tables()
    steps = s.window.steps_panel.list
    click = steps.visualItemRect(steps.item(4)).center()
    steps_panel = s.window.steps_panel
    steps_panel._context_menu(click)
    menu, _ = GrabMenu.grabbed[-1]
    point = steps.viewport().mapTo(s.window, click)
    full = s.picture((menu, point))
    return full.copy(beside(s.rect(steps_panel), QRect(point, menu.size()), full.rect()))


def demo_statuses(s: Shot) -> None:
    """The demo with a step turned off, a merge step not filled in yet and a table turned off."""
    session = s.session
    orders, customers = session.project.table("Orders"), session.project.table("Customers")
    session.set_enabled(orders.steps[4].id, False)
    session.select_table(orders.id)
    session.select_step(orders.steps[7].id)
    session.add_step(REGISTRY.actions["table.merge"])
    copy = session.duplicate_table(customers.id)
    session.set_table_enabled([copy.id], False)
    s.wait()
    s.select("Orders", 9)
    s.fit_tables()


@picture("status-colours", "status colors of tables and steps: ok, off, incomplete, waiting")
def status_colours(ex: Examples, shot: Callable[..., Shot]) -> QPixmap:
    s = shot()
    s.open(ex.demo())
    demo_statuses(s)
    return s.picture().copy(s.rect(s.window.left_splitter))


@picture("incomplete-step", "a step that is not filled in yet: the data going into it, and what is missing")
def incomplete_step(ex: Examples, shot: Callable[..., Shot]) -> QPixmap:
    s = shot()
    s.open(ex.demo(), "Orders", 8)
    s.session.add_step(REGISTRY.actions["table.merge"])  # + Add step → Merge with another table
    s.wait()
    s.select("Orders", 9)
    s.fit_tables()
    s.quiet()
    return s.picture()


@picture("filter-pane", "the filter pane: Hamburg and Köln in March and April")
def filter_pane(ex: Examples, shot: Callable[..., Shot]) -> QPixmap:
    s = shot(size=(1440, 860))
    s.set_panels(250, 270)
    s.open(ex.demo(), "Orders", 8)
    s.session.set_filters(s.session.table_id, [
        {"column": "City", "values": ["Hamburg", "Köln"]},
        {"column": "Date", "facet": "period", "options": {"by": "month"}, "values": ["2026-03", "2026-04"]},
    ])
    s.window.data_panel.filter_button.setChecked(True)
    s.window.data_panel.body.setSizes([300, 620])
    s.wait()
    return s.picture().copy(s.rect(s.window.data_panel))


def open_find(s: Shot, width: int = 380) -> Any:
    """The Find panel opened (Ctrl+Shift+F) on the left, ``width`` pixels wide."""
    w = s.window
    w.open_find()
    settle(0.2)
    w.resizeDocks([w.find_dock], [width], Qt.Orientation.Horizontal)
    settle(0.2)
    w.find_panel.refresh()
    s.wait()
    return w.find_panel


def find_picture(s: Shot) -> QPixmap:
    """The Find panel, down to a little below its last result."""
    panel = s.window.find_panel
    view = panel.view
    last = panel.model.results()[-1]
    bottom = view.viewport().mapTo(s.window, view.visualRect(panel.model.index_of(last)).bottomLeft()).y()
    area = s.rect(s.window.find_dock)
    if bottom + 16 > area.bottom():
        raise RuntimeError("the window is too low for all results")
    area.setBottom(bottom + 16)
    return s.picture().copy(area)


@picture("find-overview", "the Find panel with an empty search: the imports, exports and commands of a project")
def find_overview(ex: Examples, shot: Callable[..., Shot]) -> QPixmap:
    s = shot(size=(1280, 640))
    s.open(ex.report(), "Daily orders", 1)
    open_find(s)
    return find_picture(s)


@picture("find-place", "the Find panel grouped by place: folders and connections, files and database tables")
def find_place(ex: Examples, shot: Callable[..., Shot]) -> QPixmap:
    s = shot(size=(1280, 760))
    s.open(ex.report(), "Daily orders", 1)
    panel = open_find(s)
    panel.set_group_by("place")
    s.wait()
    return find_picture(s)


@picture("find-search", "a search for CustomerNo in the demo; Enter went to the merge step and its setting")
def find_search(ex: Examples, shot: Callable[..., Shot]) -> QPixmap:
    s = shot()
    s.open(ex.demo(), "Customers")
    panel = open_find(s, 320)
    w = s.window
    w.splitter.setSizes([230, w.splitter.width() - 230 - 400, 400])
    settle(0.2)
    panel.set_text("CustomerNo")
    hits = panel.hits()
    merge = next(n for n, hit in enumerate(hits) if hit.entry.table == "Orders" and hit.entry.number == 4)
    panel.activate(merge)  # a click on the result
    s.wait()
    w.focus_setting(*hits[merge].focus)  # Enter: the setting that matched, marked for a moment
    settle(0.2)
    return s.picture()


# Chapter 4: actions


@picture("replace-settings", "Replace: empty names become (unknown customer)")
def replace_settings(ex: Examples, shot: Callable[..., Shot]) -> QPixmap:
    s = shot()
    s.open(ex.demo(), "Orders", 5)
    return s.settings_picture()


@picture("number-rows-settings", "Number rows: the orders of each customer numbered by date")
def number_rows_settings(ex: Examples, shot: Callable[..., Shot]) -> QPixmap:
    s = shot()
    s.open(ex.demo(), "Orders", 7)
    return s.settings_picture()


@picture("merge-message", "Merge with another table: how many rows matched, and the key without a match")
def merge_message(ex: Examples, shot: Callable[..., Shot]) -> QPixmap:
    s = shot()
    s.open(ex.demo(), "Orders", 4)
    return s.settings_picture("how")


# Chapter 5: formulas


@picture("formula-mistake", "a misspelled column in a formula, with Write [Net]")
def formula_mistake(ex: Examples, shot: Callable[..., Shot]) -> QPixmap:
    s = shot()
    s.open(ex.demo(), "Orders", 6)
    s.fit_settings()
    gross, month, _size = s.editors()
    s.type_formula(gross, "ROUND([Nett] * 1.19, 2)")
    return s.settings_picture(month)


@picture("filter-formula-line", "Filter rows by formula: how many rows the condition keeps")
def filter_formula_line(ex: Examples, shot: Callable[..., Shot]) -> QPixmap:
    s = shot()
    s.open(ex.demo(), "Orders", 6)
    condition = "[Net] >= 1000 AND [City] IN ('Hamburg', 'Köln')"
    s.session.add_step(REGISTRY.actions["rows.filter_formula"], params={"condition": condition})
    s.wait()
    s.select("Orders", 7)
    return s.settings_picture()


@picture("formula-suggestions", "typing [ in a formula lists the columns with their types")
def formula_suggestions(ex: Examples, shot: Callable[..., Shot]) -> QPixmap:
    s = shot()
    s.set_panels(280, 500)  # the list (440 pixels) fits beside the formula
    s.open(ex.demo(), "Orders", 6)
    s.fit_settings()
    _gross, month, size = s.editors()
    s.type_formula(month, "STRFTIME([")
    month._completer.open()
    settle(0.3)
    popup = month._completer._popup
    if popup is None or not popup.isVisible():
        raise RuntimeError("the suggestions did not open")
    return s.settings_picture(size, also=s.rect(popup))


@picture("functions-window", "Formula functions (F1): search for month")
def functions_window_picture(ex: Examples, shot: Callable[..., Shot]) -> QPixmap:
    s = shot()
    s.open(ex.demo(), "Orders", 6)
    gross = s.editors()[0]
    functions_window.remember(gross)
    window = functions_window.FunctionsWindow(s.window)  # the window F1 shows, made offscreen first
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    s.window._functions_window = window
    functions_window.show_functions(s.window)
    window.search.setText("month")
    settle(0.5)
    found = [window.list.topLevelItem(i) for i in range(window.list.topLevelItemCount())]
    match = next((item for item in found if item.text(0).split("(")[0].strip() == "MONTH"), None)
    if match is not None:
        window.list.setCurrentItem(match)
    settle(0.3)
    window.update_insert()
    return s.widget_picture(window)


@picture("formula-window", "the formula window (F2) of Gross: columns, the formula and its result")
def formula_window_picture(ex: Examples, shot: Callable[..., Shot]) -> QPixmap:
    s = shot()
    s.open(ex.demo(), "Orders", 6)
    gross = s.editors()[0]
    gross.open_window()
    window = gross.window_open
    settle(0.5)
    deadline = time.monotonic() + 20
    while window.busy and time.monotonic() < deadline:
        settle(0.1)
    settle(0.5)
    pixmap = s.widget_picture(window)
    window.reject()
    return pixmap


# Chapter 6: the project map


@picture("map-overview", "the project map of the demo")
def map_overview(ex: Examples, shot: Callable[..., Shot]) -> QPixmap:
    s = shot()
    s.open(ex.demo(), "Orders", 4)
    s.window.show_map(True)
    s.wait()
    return s.picture()


@picture("map-boxes", "the boxes of the demo on the map, zoomed in so that their rows can be read")
def map_boxes(ex: Examples, shot: Callable[..., Shot]) -> QPixmap:
    s = shot()
    s.open(ex.demo(), "Orders", 4)
    s.window.show_map(True)
    s.wait()
    map_only(s)
    view = s.window.map_view
    view.auto_fit = False
    view._show(view.scene.itemsBoundingRect(), 1.5)  # as Ctrl + mouse wheel zooms in, up to 150 %
    settle(0.2)
    s.wait()
    boxes = [node_rect(s, node) for node in view.nodes.values()]
    return s.picture().copy(union(*boxes, margin=24, within=s.rect(view.canvas)))


def map_node(s: Shot, name: str) -> Any:
    return next(node for node in s.window.map_view.nodes.values() if node.name == name)


def node_rect(s: Shot, node: Any) -> QRect:
    """Where a box of the map is, in the window's coordinates."""
    canvas = s.window.map_view.canvas
    area = canvas.mapFromScene(node.sceneBoundingRect()).boundingRect()
    return QRect(canvas.viewport().mapTo(s.window, area.topLeft()), area.size())


def map_only(s: Shot) -> QRect:
    """Only the map, with its title row and group bar: the panels beside and below it are
    dragged closed. Returns where it is in the window."""
    view = s.window.map_view
    view.splitter.setSizes([view.width(), 0])
    view.left.setSizes([view.height(), 0])
    settle(0.2)
    s.wait()
    return s.rect(view).adjusted(0, 0, -view.splitter.handleWidth(), -view.left.handleWidth())


def map_with_menu(s: Shot, menu: QPixmap, point: QPoint) -> QPixmap:
    """The boxes of the map with a menu opened on it, and a little of the map around them."""
    full = s.picture((menu, point))
    boxes = [node_rect(s, node) for node in s.window.map_view.nodes.values()]
    return full.copy(union(*boxes, QRect(point, menu.size()), margin=30, within=s.rect(s.window.map_view.canvas)))


@picture("map-connect-menu", "Customers dragged onto Revenue by customer: how to use it")
def map_connect_menu(ex: Examples, shot: Callable[..., Shot]) -> QPixmap:
    s = shot()
    s.open(ex.demo(), "Customers")
    s.window.show_map(True)
    s.wait()
    map_only(s)
    view = s.window.map_view
    source, target = map_node(s, "Customers"), map_node(s, "Revenue by customer")
    drop = target.sceneBoundingRect().topLeft() + QPointF(70, 34)
    view.start_link(source)  # as a drag from the ● of Customers does
    view.move_link(drop)
    settle(0.2)
    view.choose_link_kind(source, target, QPoint())
    menu, _ = GrabMenu.grabbed[-1]
    canvas = view.canvas
    point = canvas.viewport().mapTo(s.window, canvas.mapFromScene(drop))
    return map_with_menu(s, menu, point)


@picture("map-box-menu", "the right-click menu of a table on the map")
def map_box_menu(ex: Examples, shot: Callable[..., Shot]) -> QPixmap:
    s = shot()
    s.open(ex.demo(), "Orders")
    s.window.show_map(True)
    s.wait()
    map_only(s)
    node = map_node(s, "Orders")
    s.window.map_view.node_menu(node, QPoint())
    menu, _ = GrabMenu.grabbed[-1]
    box = node_rect(s, node)
    return map_with_menu(s, menu, QPoint(box.left() + 70, box.top() + 14))


@picture("map-groups", "the sales project on the map: groups Sources, Reports and Checks")
def map_groups(ex: Examples, shot: Callable[..., Shot]) -> QPixmap:
    s = shot(size=(1280, 730))
    s.open(ex.sales(), "Revenue by customer", 2)
    s.window.show_map(True)
    s.wait()
    area = map_only(s)
    return s.picture().copy(area)


@picture("map-collapsed", "Collapse all: each group as one box")
def map_collapsed(ex: Examples, shot: Callable[..., Shot]) -> QPixmap:
    s = shot(size=(1100, 620))
    s.open(ex.sales(), "Revenue by customer")
    s.window.show_map(True)
    s.wait()
    s.window.map_view.group_bar.collapse_all.click()
    s.wait()
    area = map_only(s)
    return s.picture().copy(area)


@picture("map-highlight", "the tables Orders with customers uses, and the table that uses it (key H)")
def map_highlight(ex: Examples, shot: Callable[..., Shot]) -> QPixmap:
    s = shot(size=(1280, 760))
    s.open(ex.sales(), "Orders with customers", 2)
    s.window.show_map(True)
    s.wait()
    area = map_only(s)
    s.window.map_view.toggle_lineage()  # H
    s.wait()
    return s.picture().copy(union(area, s.rect(s.window.statusBar())))  # with the message


# Chapter 7: parameters


def dialog_picture(s: Shot, dialog: QDialog, ready: Callable[[], bool]) -> QPixmap:
    dialog.setProperty("expected", True)
    dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    dialog.show()
    deadline = time.monotonic() + 30
    settle(0.3)
    while not ready() and time.monotonic() < deadline:
        settle(0.1)
    if not ready():
        raise TimeoutError(f"the dialog {dialog.windowTitle()} was not ready")
    settle(0.3)
    pixmap = s.widget_picture(dialog)
    dialog.reject()
    return pixmap


@picture("parameters-dialog", "the Parameters dialog: Day, Month and Limit with their values now")
def parameters_dialog(ex: Examples, shot: Callable[..., Shot]) -> QPixmap:
    s = shot()
    s.open(ex.monthly())
    dialog = ParametersDialog(s.session, s.window)
    dialog.table.selectRow(1)  # Month, as after a click: the buttons are on
    return dialog_picture(s, dialog, lambda: not dialog.calculating and "…" not in dialog.table.item(1, 2).text())


@picture("parameter-in-formula", "{Month} in a formula")
def parameter_in_formula(ex: Examples, shot: Callable[..., Shot]) -> QPixmap:
    s = shot()
    s.open(ex.monthly(), "Orders of the month", 3)
    return s.settings_picture()


@picture("parameter-in-file-name", "{Month} in a file name and a sheet name")
def parameter_in_file_name(ex: Examples, shot: Callable[..., Shot]) -> QPixmap:
    s = shot()
    s.open(ex.monthly(), "Orders of the month", 5)
    return s.settings_picture("existing")


# Chapter 8: databases


@picture("connections-sqlite", "Connections: the SQLite database Shop, tested")
def connections_sqlite(ex: Examples, shot: Callable[..., Shot]) -> QPixmap:
    s = shot()
    s.open(ex.database())
    dialog = ConnectionsDialog(s.session, s.window)
    dialog.list.setCurrentRow(0)

    def tested() -> bool:
        if not dialog.test_result.isVisible() and dialog.test_button.isEnabled():
            dialog._test()  # Test connection, once the dialog is shown
        return dialog.test_result.toPlainText().startswith("Connected")

    return dialog_picture(s, dialog, tested)


@picture("connections-sqlserver", "Connections: a SQL Server connection (not tested: nothing connects)")
def connections_sqlserver(ex: Examples, shot: Callable[..., Shot]) -> QPixmap:
    s = shot()
    s.open(ex.database())
    dialog = ConnectionsDialog(s.session, s.window)
    dialog.list.setCurrentRow(1)
    return dialog_picture(s, dialog, lambda: True)


@picture("db-import", "Import from database: a SQL query on the SQLite connection Shop")
def db_import(ex: Examples, shot: Callable[..., Shot]) -> QPixmap:
    s = shot(size=(1280, 700))  # 5 rows: no room for an empty grid
    s.open(ex.database(), "Customers", 1)
    return s.picture()


@picture("db-export-settings", "Export to database: replace the rows of the same months")
def db_export_settings(ex: Examples, shot: Callable[..., Shot]) -> QPixmap:
    s = shot()
    s.open(ex.database(), "Revenue by month", 6)
    return s.settings_picture()


# Chapter 9: Excel and files


@picture("excel-export-settings", "Export to Excel file: all its settings")
def excel_export_settings(ex: Examples, shot: Callable[..., Shot]) -> QPixmap:
    s = shot()
    s.open(ex.demo(), "Orders", 9)
    return s.settings_picture()


@picture("replace-sheet-settings", "Export to Excel file: Replace only this sheet, keep the other sheets")
def replace_sheet_settings(ex: Examples, shot: Callable[..., Shot]) -> QPixmap:
    s = shot()
    s.open(ex.excel_check(), "Data", 4)
    return s.settings_picture("existing")


@picture("folder-import", "all files of a folder and its subfolders in one table, with File and Path")
def folder_import(ex: Examples, shot: Callable[..., Shot]) -> QPixmap:
    s = shot()
    s.open(ex.folders(), "All orders", 1)
    return s.picture()


# Chapter 10: days without data


@picture("inbox-import", "the daily inbox: all orders_*.csv files of the folder inbox")
def inbox_import(ex: Examples, shot: Callable[..., Shot]) -> QPixmap:
    s = shot()
    s.open(ex.inbox(), "Daily orders", 1)
    s.fit_tables()
    return s.picture()


@picture("inbox-move-settings", "Move files: the files the import read go to the folder archive")
def inbox_move_settings(ex: Examples, shot: Callable[..., Shot]) -> QPixmap:
    s = shot()
    s.open(ex.inbox(), "Daily orders", 5)
    return s.settings_picture()


@picture("inbox-day-without-files", "right after the run, the inbox is empty: the rest of the table is skipped")
def inbox_day_without_files(ex: Examples, shot: Callable[..., Shot]) -> QPixmap:
    s = shot()
    s.open(ex.inbox(), "Daily orders", 4)
    s.fit_tables()
    s.run()
    s.window.run_dock.hide()
    s.select("Daily orders", 4)
    return s.picture()


@picture("inbox-run-skipped", "Run results on a day without files")
def inbox_run_skipped(ex: Examples, shot: Callable[..., Shot]) -> QPixmap:
    s = shot(size=(1100, 800))
    s.open(ex.inbox(), "Daily orders", 4)
    s.run()
    s.run()
    s.show_run_results()
    return s.picture().copy(s.rect(s.window.run_dock))


@picture("skip-if-empty", "Skip the rest if a table is empty: no large orders today")
def skip_if_empty(ex: Examples, shot: Callable[..., Shot]) -> QPixmap:
    s = shot(size=(1280, 500))  # an empty table: no room for an empty grid
    s.open(ex.inbox(), "Daily orders", 1)
    s.fit_tables()
    s.run()
    s.window.run_dock.hide()
    ex.inbox_new_day()
    s.window.reload_action.trigger()  # Reload data (Ctrl+R)
    s.wait()
    s.select("Large orders", 3)
    s.quiet()  # the message of the run is gone by now
    return s.picture()


# Chapter 13: troubleshooting


def demo_error(s: Shot) -> None:
    """The demo with a misspelled column in the formula of Gross (applied)."""
    step = s.step("Orders", 6)
    columns = [dict(c) for c in step.params["columns"]]
    columns[0]["formula"] = "ROUND([Nett] * 1.19, 2)"
    s.session.set_params(step.id, {"columns": columns})
    s.wait()


@picture("error-step", "a step with an error")
def error_step(ex: Examples, shot: Callable[..., Shot]) -> QPixmap:
    s = shot()
    s.open(ex.demo(), "Orders", 6)
    s.fit_tables()
    demo_error(s)
    s.quiet()
    return s.picture()


@picture("error-run-results", "Run results with an error")
def error_run_results(ex: Examples, shot: Callable[..., Shot]) -> QPixmap:
    s = shot(size=(1100, 800))
    s.open(ex.demo(), "Orders", 6)
    demo_error(s)
    s.run()
    s.show_run_results()
    return s.picture().copy(s.rect(s.window.run_dock))


# --- main -----------------------------------------------------------------------------------------


def save(pixmap: QPixmap, path: Path) -> None:
    """Check the picture and save it as a compressed PNG."""
    image = pixmap.toImage()
    if image.isNull() or image.width() < 40 or image.height() < 40:
        raise RuntimeError(f"the picture is empty ({image.width()} × {image.height()})")
    if pixmap.devicePixelRatio() != 1:  # a screen with scaling: the pictures keep their size
        image = image.scaled(pixmap.deviceIndependentSize().toSize(), Qt.AspectRatioMode.IgnoreAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
        image.setDevicePixelRatio(1)
    image = image.convertToFormat(image.Format.Format_RGB32)
    sample = image.scaled(32, 32)
    if len({sample.pixel(x, y) for x in range(32) for y in range(32)}) < 3:
        raise RuntimeError("the picture is all one color")
    if not image.save(str(path), "PNG", 0):  # quality 0: the strongest compression
        raise RuntimeError(f"could not write {path}")


def private_patterns(root: Path) -> list[re.Pattern[str]]:
    """The example folder as the window may show it: with \\ or /, in any case, also with the
    short (8.3) names Windows gives long folder names."""
    variants = {str(root), str(root.resolve())}
    try:
        import ctypes

        buffer = ctypes.create_unicode_buffer(1024)
        if ctypes.windll.kernel32.GetShortPathNameW(str(root), buffer, 1024):
            variants.add(buffer.value)
    except (AttributeError, OSError):  # not on Windows
        pass
    patterns = []
    for variant in sorted(variants, key=len, reverse=True):
        parts = [re.escape(p) for p in re.split(r"[\\/]", variant.rstrip("\\/"))]
        patterns.append(re.compile(r"[\\/]".join(parts), re.IGNORECASE))
    return patterns


def main() -> None:
    parser = argparse.ArgumentParser(description="Make the pictures of the user guide (docs/guide/*.png).")
    parser.add_argument("names", nargs="*", help="pictures to make (default: all)")
    parser.add_argument("--list", action="store_true", help="list the pictures")
    parser.add_argument("--out", type=Path, default=OUT, help=f"folder for the pictures (default: {OUT})")
    parser.add_argument("--examples", type=Path, help="build the example projects here and keep them")
    args = parser.parse_args()
    for stream in (sys.stdout, sys.stderr):  # a console with an old code page shows what it can
        stream.reconfigure(errors="replace")
    if args.list:
        width = max(len(name) for name in PICTURES)
        for item in PICTURES.values():
            print(f"{item.name:<{width}}  {item.shows}")
        return
    unknown = [n for n in args.names if n not in PICTURES]
    if unknown:
        parser.error(f"unknown pictures: {', '.join(unknown)} (see --list)")
    chosen = [PICTURES[n] for n in args.names] if args.names else list(PICTURES.values())

    settings = tempfile.mkdtemp(prefix="tafl-guide-settings-")
    start_app(settings)
    keep = args.examples is not None
    root = (args.examples or Path(tempfile.mkdtemp(prefix="tafl-guide-"))).resolve()
    root.mkdir(parents=True, exist_ok=True)
    args.out.mkdir(parents=True, exist_ok=True)
    examples = Examples(root)
    patterns = private_patterns(root)
    failed = []
    try:
        for item in chosen:
            started = time.monotonic()
            try:
                path, width, height = make(item, examples, patterns, args.out)
                kilobytes = path.stat().st_size // 1024
                print(f"{item.name}: {width} x {height}, {kilobytes} KB, {time.monotonic() - started:.1f} s")
            except Exception as e:
                failed.append(item.name)
                print(f"{item.name}: FAILED\n{''.join(traceback.format_exception(e))}", file=sys.stderr)
    finally:
        checks().stop_all()
        if not keep:
            shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(settings, ignore_errors=True)
    if failed:
        sys.exit(f"Not made: {', '.join(failed)}")


def start_app(settings: str) -> QApplication:
    """The app with its theme, offscreen: window settings in the folder ``settings``, menus and
    dialogs drawn offscreen, message boxes and errors noted instead of shown."""
    for name in ("question", "warning", "critical", "information", "about"):
        setattr(QMessageBox, name, staticmethod(_message_box(name)))
    for module in (tafl.ui.steps_panel, tafl.ui.tables_panel, tafl.ui.data_panel, tafl.ui.filter_pane, tafl.ui.map.view):
        module.QMenu = GrabMenu
    tafl.ui.forms.FormulaWindow = OffscreenFormulaWindow
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, settings)
    QApplication.setOrganizationName(f"{APP_NAME} Guide")
    app = QApplication([])
    app.installEventFilter(OffscreenGuard(app))
    theme.apply(app)
    sys.excepthook = lambda kind, value, tb: Problems.add("".join(traceback.format_exception(kind, value, tb)))
    return app


def make(item: Picture, examples: Examples, patterns: list[re.Pattern[str]], out: Path) -> tuple[Path, int, int]:
    """Make one picture (in windows of its own, closed afterwards) and save it in ``out``."""
    shots: list[Shot] = []

    def shot(size: tuple[int, int] = (1280, 800)) -> Shot:
        shots.append(Shot(patterns, size))
        return shots[-1]

    Problems.found = []
    try:
        pixmap = item.make(examples, shot)
        if Problems.found:
            raise RuntimeError("\n".join(Problems.found))
        if pixmap.width() > MAX_WIDTH:
            raise RuntimeError(f"the picture is {pixmap.width()} pixels wide (at most {MAX_WIDTH})")
        path = out / f"{item.name}.png"
        save(pixmap, path)
        return path, pixmap.width(), pixmap.height()
    finally:
        for s in shots:
            s.close()


def _message_box(name: str) -> Callable[..., Any]:
    def note(*args: Any, **kwargs: Any) -> QMessageBox.StandardButton:
        Problems.add(f"message box ({name}): {' '.join(str(a) for a in args[1:3])}")
        return QMessageBox.StandardButton.Cancel

    return note


if __name__ == "__main__":
    main()
