"""The main window: tables and steps on the left, data in the middle, settings on the right."""

from __future__ import annotations

import logging
import platform
import sys
from pathlib import Path

import duckdb
import polars as pl
from PySide6 import __version__ as pyside_version
from PySide6.QtCore import QSettings, Qt, QTimer, QUrl, qVersion
from PySide6.QtGui import QAction, QActionGroup, QCloseEvent, QDesktopServices, QGuiApplication, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDockWidget,
    QFileDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from tafl import APP_NAME, GUIDE_FILE, GUIDE_URL, PROJECT_URL, __version__
from tafl.api import ActionError
from tafl.core import EXTENSION, ProjectError
from tafl.core.registry import default_plugin_folders
from tafl.core.sheet import stamp_of

from . import functions_window, theme
from .checks import checks
from .connections_dialog import ConnectionsDialog
from .data_panel import DataPanel
from .find_panel import FindPanel
from .map import MapView
from .parameters_dialog import ParametersDialog
from .run_panel import RunPanel
from .session import Session
from .settings_panel import SettingsPanel
from .sheet_window import SheetWindow, ask, problem
from .steps_panel import StepsPanel
from .tables_panel import TablesPanel

log = logging.getLogger("tafl")

_FILTER = f"{APP_NAME} projects (*{EXTENSION});;All files (*.*)"
_MAX_RECENT = 8


class MainWindow(QMainWindow):
    def __init__(self, session: Session, log_file: Path | None = None) -> None:
        super().__init__()
        self.session = session
        self.log_file = log_file
        self.settings = QSettings()
        self.setWindowIcon(theme.app_icon())

        self.tables_panel = TablesPanel(session, self)
        self.steps_panel = StepsPanel(session, self)
        self.data_panel = DataPanel(session, self)
        self.settings_panel = SettingsPanel(session, self.edit_connections, self)
        left = QSplitter(Qt.Orientation.Vertical, self)
        left.addWidget(self.tables_panel)
        left.addWidget(self.steps_panel)
        left.setSizes([220, 520])
        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.splitter.addWidget(left)
        self.splitter.addWidget(self.data_panel)
        self.splitter.addWidget(self.settings_panel)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([280, 800, 420])
        self.left_splitter = left
        self.map_view = MapView(session, self.edit_connections, self)
        self.map_view.open_table.connect(self.open_from_map)
        self.map_view.message.connect(lambda text: self.statusBar().showMessage(text, 10000))
        self.map_view.message_update.connect(self._update_message)
        self.data_panel.status_message.connect(lambda text: self.statusBar().showMessage(text, 10000))
        self._close_after_run = False  # closing waits for the run to end
        self._going_on_closing = False  # a sheet ended the writing that closing waited for
        self._close_answers: dict[SheetWindow, int] = {}  # the sheets that closing has passed, as they were then
        self._sheets: list[SheetWindow] = []  # the open sheets
        self._sheet_number = 0
        self.views = QStackedWidget(self)
        self.views.addWidget(self.splitter)
        self.views.addWidget(self.map_view)
        self.setCentralWidget(self.views)

        self.run_panel = RunPanel(session, self)
        self.run_dock = QDockWidget("Run results", self)
        self.run_dock.setObjectName("RunResults")
        self.run_dock.setWidget(self.run_panel)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.run_dock)
        self.run_dock.hide()

        self.find_panel = FindPanel(session, self)
        self.find_dock = QDockWidget("Find", self)
        self.find_dock.setObjectName("Find")
        self.find_dock.setWidget(self.find_panel)
        self.find_dock.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea | Qt.DockWidgetArea.BottomDockWidgetArea
        )
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.find_dock)
        self.find_dock.hide()
        self.find_panel.go_to.connect(self.go_to_step)
        self.find_panel.focus_setting.connect(self.focus_setting)
        self.find_panel.message.connect(lambda text: self.statusBar().showMessage(text, 10000))
        self.find_panel.leave.connect(self._leave_find)

        self.busy_label = QLabel("", self)
        self.statusBar().addPermanentWidget(self.busy_label)
        self._create_actions()
        self._create_menus()
        self._create_toolbar()

        session.dirty_changed.connect(self._update_title)
        session.project_changed.connect(self._update_title)
        session.structure_changed.connect(self._update_actions)
        session.selection_changed.connect(self._update_actions)
        session.undo_changed.connect(self._update_actions)
        session.run_busy_changed.connect(self._run_busy)
        session.busy_changed.connect(self._calc_busy)
        session.run_finished.connect(self._run_finished)
        session.run_stopped.connect(self._run_stopped)
        session.error.connect(self._show_error)
        session.new_sheet_wanted.connect(self.new_sheet)
        session.sheet_edit_wanted.connect(self.edit_sheet)
        self._restore_layout()
        self._update_title()
        self._update_actions()

    # --- actions -----------------------------------------------------------------------------

    def _action(self, text: str, slot, shortcut: QKeySequence | str | None = None, tip: str = "") -> QAction:
        action = QAction(text, self)
        action.triggered.connect(slot)
        if shortcut is not None:
            action.setShortcut(shortcut)
        action.setToolTip(tip or text)
        action.setStatusTip(tip)
        return action

    def _create_actions(self) -> None:
        self.new_action = self._action("&New project", self.new_project, QKeySequence.StandardKey.New)
        self.open_action = self._action("&Open…", self.open_project, QKeySequence.StandardKey.Open)
        self.save_action = self._action("&Save", self.save_project, QKeySequence.StandardKey.Save)
        self.save_as_action = self._action("Save &as…", self.save_project_as, QKeySequence("Ctrl+Shift+S"))
        self.exit_action = self._action("E&xit", self.close)
        self.undo_action = self._action("&Undo", self.undo, QKeySequence.StandardKey.Undo)
        self.redo_action = self._action("&Redo", self.redo, QKeySequence("Ctrl+Y"))
        self.run_action = self._action(
            "▶  Run project", self.run_project, QKeySequence("F5"),
            "Calculate all tables and execute their exports and commands (F5)",
        )
        self.run_table_action = self._action(
            "Run this table", self.run_current_table, QKeySequence("Shift+F5"),
            "Calculate the selected table and the tables it uses, and execute their exports and commands (Shift+F5)",
        )
        self.stop_action = self._action(
            "■  Stop", self.stop_run, tip="Stop the running project after the step that is running"
        )
        self.reload_action = self._action(
            "⟳  Reload data", self.session.reload_sources, QKeySequence("Ctrl+R"),
            "Read files and databases again (Ctrl+R)",
        )
        self.connections_action = self._action("Connections…", self.edit_connections, tip="Database connections of this project")
        self.parameters_action = self._action(
            "Parameters…", self.edit_parameters, tip="Values that settings use as {Name}, e.g. a date in file names and SQL"
        )
        self.functions_action = self._action(
            "Formula functions", lambda: functions_window.show_functions(self), QKeySequence("F1"),
            "What the functions in formulas do and how they are written (F1)",
        )
        self.guide_action = self._action(
            "User guide", self.open_guide, tip="How to build, run and schedule projects, step by step and with pictures"
        )
        self.plugins_action = self._action("Plugins…", self.show_plugins)
        self.about_action = self._action(f"About {APP_NAME}", self.show_about)
        self.log_action = self._action(
            "Open log folder", self.open_log_folder, tip="The log file has the details of unexpected errors"
        )
        self.results_action = self.run_dock.toggleViewAction()
        self.results_action.setText("Run results")
        self.find_action = self._action(
            "Find in project…", self.open_find, QKeySequence("Ctrl+Shift+F"),
            "Find tables, steps, files, SQL, formulas and notes; with an empty search, all imports, exports and commands (Ctrl+Shift+F)",
        )
        self.find_action.setIconText("Find")  # short (the menu has the text): the toolbar stays narrow
        self.find_next_action = self._action("Find next", lambda: self.find_next(1), QKeySequence("F3"))
        self.find_previous_action = self._action("Find previous", lambda: self.find_next(-1), QKeySequence("Shift+F3"))
        self.find_panel_action = self.find_dock.toggleViewAction()
        self.find_panel_action.setText("Find panel")
        self.tables_view_action = self._action(
            "▤  Tables", lambda: self.show_map(False), QKeySequence("Ctrl+1"), "Edit the steps of a table (Ctrl+1)"
        )
        self.map_view_action = self._action(
            "⧉  Map", lambda: self.show_map(True), QKeySequence("Ctrl+2"), "Project map: how the tables are connected (Ctrl+2)"
        )
        self.view_group = QActionGroup(self)
        for action in (self.tables_view_action, self.map_view_action):
            action.setCheckable(True)
            self.view_group.addAction(action)
        self.tables_view_action.setChecked(True)

    def _create_menus(self) -> None:
        bar = self.menuBar()
        file = bar.addMenu("&File")
        for action in (self.new_action, self.open_action):
            file.addAction(action)
        self.recent_menu = file.addMenu("Open &recent")
        self.recent_menu.aboutToShow.connect(self._fill_recent)
        file.addSeparator()
        file.addAction(self.save_action)
        file.addAction(self.save_as_action)
        file.addSeparator()
        file.addAction(self.exit_action)
        edit = bar.addMenu("&Edit")
        edit.addAction(self.undo_action)
        edit.addAction(self.redo_action)
        edit.addSeparator()
        for action in (self.find_action, self.find_next_action, self.find_previous_action):
            edit.addAction(action)
        project = bar.addMenu("&Project")
        for action in (self.run_action, self.run_table_action, self.stop_action):
            project.addAction(action)
        project.addSeparator()
        project.addAction(self.reload_action)
        project.addAction(self.connections_action)
        project.addAction(self.parameters_action)
        view = bar.addMenu("&View")
        view.addAction(self.tables_view_action)
        view.addAction(self.map_view_action)
        view.addSeparator()
        view.addAction(self.results_action)
        view.addAction(self.find_panel_action)
        help_menu = bar.addMenu("&Help")
        help_menu.addAction(self.guide_action)
        help_menu.addAction(self.functions_action)
        help_menu.addAction(self.plugins_action)
        help_menu.addAction(self.log_action)
        help_menu.addSeparator()
        help_menu.addAction(self.about_action)

    def _create_toolbar(self) -> None:
        toolbar = self.addToolBar("Main")
        toolbar.setObjectName("MainToolbar")
        toolbar.setMovable(False)
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        for action in (self.new_action, self.open_action, self.save_action):
            toolbar.addAction(action)
        toolbar.addSeparator()
        toolbar.addAction(self.tables_view_action)
        toolbar.addAction(self.map_view_action)
        toolbar.addAction(self.find_action)
        toolbar.addSeparator()
        toolbar.addAction(self.undo_action)
        toolbar.addAction(self.redo_action)
        toolbar.addSeparator()
        toolbar.addAction(self.run_action)
        toolbar.addAction(self.stop_action)
        toolbar.addAction(self.reload_action)
        toolbar.addSeparator()
        toolbar.addAction(self.connections_action)
        toolbar.addAction(self.parameters_action)
        theme.show_overflow_button(toolbar)  # in a narrow window, the buttons that don't fit are behind it

    def _update_actions(self) -> None:
        self.undo_action.setEnabled(self.session.can_undo)
        self.redo_action.setEnabled(self.session.can_redo)
        has_tables = bool(self.session.project.tables)
        running = self.session.running
        self.run_action.setEnabled(has_tables and not running)
        self.reload_action.setEnabled(has_tables)
        self.run_table_action.setEnabled(self.session.table is not None and not running)
        self.stop_action.setEnabled(running and not self.session.stopping)

    def _update_title(self) -> None:
        self.setWindowTitle(f"{self._project_name()}{' •' if self.session.dirty else ''}  —  {APP_NAME}")

    # --- files -------------------------------------------------------------------------------

    def new_project(self) -> None:
        if self.session.dirty:
            if not self.maybe_save("before starting a new project"):
                return
        elif self.session.project.tables:  # nothing to save, but don't switch by surprise
            text = f"Close “{self._project_name()}” and start a new, empty project?"
            if not self.confirm("New project", text, "Start new project"):
                return
        self.session.new_project()
        self.show_map(False)

    def open_project(self) -> None:
        if not self.maybe_save("before opening another project"):
            return
        start = str(self.settings.value("last_dir", str(Path.home())))
        path, _ = QFileDialog.getOpenFileName(self, "Open project", start, _FILTER)
        if path:
            self.open_path(path)

    def open_path(self, path: str | Path) -> bool:
        try:
            upgraded = self.session.open_project(path)
        except (ProjectError, OSError) as e:
            QMessageBox.warning(self, "Open project", f"Could not open {path}:\n\n{e}")
            return False
        self._remember(Path(path))
        message = f"Opened {Path(path).name}"
        if upgraded:
            message += f" — {len(upgraded)} step(s) were updated to newer action versions; save to keep this"
        self.statusBar().showMessage(message, 8000)
        return True

    def save_project(self) -> bool:
        if self.session.project.path is None:
            return self.save_project_as()
        return self._save_to(self.session.project.path)

    def save_project_as(self) -> bool:
        current = self.session.project.path
        start = str(current) if current else str(Path(str(self.settings.value("last_dir", str(Path.home())))) / f"Untitled{EXTENSION}")
        path, _ = QFileDialog.getSaveFileName(self, "Save project", start, _FILTER)
        if not path:
            return False
        if not path.lower().endswith(EXTENSION):
            path += EXTENSION
        return self._save_to(Path(path))

    def _save_to(self, path: Path) -> bool:
        self.apply_typing()
        if self.session.changed_on_disk(path) and not self.confirm_overwrite(path):
            return False
        try:
            saved = self.session.save_project(path)
        except (ProjectError, OSError) as e:
            QMessageBox.warning(self, "Save project", f"Could not save {path}:\n\n{e}")
            return False
        self._remember(saved)
        self._update_title()
        self.statusBar().showMessage(f"Saved {saved}", 5000)
        return True

    def maybe_save(self, reason: str) -> bool:
        """Offer to save unsaved changes ``reason`` ("before closing"). False if cancelled."""
        self.apply_typing()
        if not self.session.dirty:
            return True
        answer = self.ask_save(reason)
        if answer == "save":
            return self.save_project()
        return answer == "discard"

    def ask_save(self, reason: str) -> str:
        """Save / Don't save / Cancel: returns "save", "discard" or "cancel"."""
        box = QMessageBox(QMessageBox.Icon.Question, "Save changes?", "", parent=self)
        box.setText(f"Save the changes to “{self._project_name()}” {reason}?")
        box.setInformativeText("If you don't save, your changes since the last save are lost.")
        save = box.addButton("Save", QMessageBox.ButtonRole.AcceptRole)
        discard = box.addButton("Don't save", QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(save)
        box.setEscapeButton(cancel)
        box.exec()
        return "save" if box.clickedButton() is save else "discard" if box.clickedButton() is discard else "cancel"

    def confirm_overwrite(self, path: Path) -> bool:
        """The project file changed since this window opened or saved it: save over it?"""
        box = QMessageBox(QMessageBox.Icon.Warning, "Save project", "", parent=self)
        box.setText(f"“{path.name}” has changed since it was opened or saved here, for example in another window.")
        box.setInformativeText(
            "Saving replaces those changes with the project as it is in this window. "
            "To keep both, cancel and use Save as…"
        )
        save = box.addButton("Save anyway", QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel)
        box.setEscapeButton(cancel)
        box.exec()
        return box.clickedButton() is save

    def confirm(self, title: str, text: str, ok: str) -> bool:
        box = QMessageBox(QMessageBox.Icon.Question, title, text, parent=self)
        yes = box.addButton(ok, QMessageBox.ButtonRole.AcceptRole)
        no = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(yes)
        box.setEscapeButton(no)
        box.exec()
        return box.clickedButton() is yes

    def _project_name(self) -> str:
        path = self.session.project.path
        return path.name if path else "Untitled"

    def _remember(self, path: Path) -> None:
        recent = [p for p in self._recent() if Path(p) != path.resolve()]
        self.settings.setValue("recent", [str(path.resolve()), *recent][:_MAX_RECENT])
        self.settings.setValue("last_dir", str(path.resolve().parent))

    def _recent(self) -> list[str]:
        value = self.settings.value("recent", [])
        return [value] if isinstance(value, str) else list(value or [])

    def _fill_recent(self) -> None:
        self.recent_menu.clear()
        recent = [p for p in self._recent() if Path(p).exists()]
        if not recent:
            self.recent_menu.addAction("(none)").setEnabled(False)
        for path in recent:
            self.recent_menu.addAction(
                path, lambda p=path: self.maybe_save("before opening another project") and self.open_path(p)
            )

    def apply_typing(self) -> None:
        """Apply what is typed in a field but not applied yet (Enter or leaving the field applies),
        e.g. before saving or running: a shortcut doesn't leave the field."""
        widget = QApplication.focusWidget()
        if widget is not None and self.isAncestorOf(widget):
            widget.clearFocus()  # leaving the field applies what is typed
            widget.setFocus()
        field = functions_window.target()  # typing, then looking up a function
        if field is not None:
            field.apply()

    def undo(self) -> None:
        """Edit → Undo. What is typed in a field is applied first (the toolbar's buttons leave the
        keyboard focus in it), so Undo takes back just that typing. Ctrl+Z in a field is the
        field's own undo."""
        self.apply_typing()
        self.session.undo()

    def redo(self) -> None:
        """Edit → Redo; what is typed in a field is applied first, as for Undo. That is an edit:
        there is nothing to redo after it then (the status bar says so)."""
        could_redo = self.session.can_redo
        self.apply_typing()
        if could_redo and not self.session.can_redo:
            self.statusBar().showMessage("What you typed was applied: there is nothing to redo after a change", 10000)
            return
        self.session.redo()

    # --- running -----------------------------------------------------------------------------

    def run_project(self) -> None:
        self.apply_typing()
        self.session.run_project()

    def run_current_table(self) -> None:
        self.apply_typing()
        if self.session.table_id:
            self.session.run_project([self.session.table_id])

    def stop_run(self) -> None:
        """Stop the run after the step that is running (an export of a big table may take a while)."""
        if not self.session.running:
            return
        self.session.cancel_run()
        self.statusBar().showMessage(
            "Stopping after the current step…" + ("  The window closes then." if self._close_after_run else "")
        )
        self._update_actions()

    # --- data sheets -------------------------------------------------------------------------

    def new_sheet(self, paste: bool, position: tuple[float, float] | None = None, group: str | None = None) -> None:
        """A sheet for a new table, with the clipboard's data if ``paste``. An open sheet stays open
        when another project is opened: adding it adds to the project that is open then."""
        self._sheet_number += 1
        window = SheetWindow(self.session, self, title=f"Sheet {self._sheet_number}", position=position, group=group)
        self._keep_sheet(window)
        window.show()
        if paste:
            window.paste()

    def edit_sheet(self, step_id: str) -> None:
        """The file of an import step in a sheet, unless it is open already: two windows on one
        file could overwrite each other."""
        session = self.session
        _, step = session.find_step(step_id)
        sheet_format = session.sheet_format(step)
        if sheet_format is None:
            return
        try:
            path = session.sheet_file(step)
            outdated = session.sheet_outdated(step)
        except ActionError as error:
            self.show_sheet_problem(str(error))
            return
        for window in self._sheets:
            if window.shows_file(path):
                window.raise_()
                window.activateWindow()
                return
        if stamp_of(path) is None:
            self.show_sheet_problem(
                f"“{path.name}” is not there any more:\n{path}\n\nPut the file back, or choose another file in the step's settings."
            )
            return
        if outdated:
            if not self.ask_open_changed(path):
                return
            session.sheet_seen(path)
        window = SheetWindow(session, self, file=path, file_format=sheet_format)
        self._keep_sheet(window)
        window.show()
        window.load_file()

    def _keep_sheet(self, window: SheetWindow) -> None:
        self._sheets.append(window)
        window.closed.connect(self._sheet_closed)
        window.message.connect(lambda text: self.statusBar().showMessage(text, 10000))

    def _sheet_closed(self, window: SheetWindow) -> None:
        if window in self._sheets:
            self._sheets.remove(window)

    def ask_open_changed(self, path: Path) -> bool:
        answer = ask(
            self,
            "Edit in sheet",
            f"“{path.name}” has changed since Tafl wrote it.",
            "The sheet shows its values as text. Saving replaces the file with these values: "
            "formulas, cell formats and added sheets are lost.",
            {
                "open": ("Open in sheet", QMessageBox.ButtonRole.DestructiveRole),
                "cancel": ("Cancel", QMessageBox.ButtonRole.RejectRole),
            },
            "cancel",
        )
        return answer == "open"

    def show_sheet_problem(self, text: str) -> None:
        problem(self, text)

    # --- views -------------------------------------------------------------------------------

    def show_map(self, on: bool) -> None:
        """Switch between editing tables and the project map."""
        self.views.setCurrentWidget(self.map_view if on else self.splitter)
        (self.map_view_action if on else self.tables_view_action).setChecked(True)
        self.session.set_overview(on)

    def open_find(self) -> None:
        """The Find panel, with the keyboard focus in its search field (Ctrl+Shift+F)."""
        self.find_dock.show()
        self.find_dock.raise_()
        self.find_panel.focus_search()

    def find_next(self, step: int) -> None:
        """The next (or previous) result of the Find panel (F3, Shift+F3); a hidden panel is shown."""
        if not self.find_dock.isVisible():
            self.find_dock.show()
            self.find_dock.raise_()
        self.find_panel.next(step)

    def go_to_step(self, table_id: str, step_id: str) -> None:
        """Select a table and step in the current view; on the map its box is centred (a collapsed
        group opens, as the map's Ctrl+F does). What is typed in a field is applied first (F3
        doesn't leave the field): its undo step then shows the step typed in."""
        focus = QApplication.focusWidget()
        # Not from the panel itself (Enter, a click): a field left for it has applied its typing,
        # and the panel's search field keeps its selected text
        if focus is None or not self.find_panel.isAncestorOf(focus):
            self.apply_typing()
        if self.views.currentWidget() is self.map_view:
            self.map_view.focus_table(table_id)
        else:
            self.session.select_table(table_id)
        self.session.select_step(step_id)

    def focus_setting(self, key: str, row: int, field: str) -> None:
        """Move the keyboard focus to a setting of the selected step (Enter in the Find panel)."""
        panel = self.map_view.settings if self.views.currentWidget() is self.map_view else self.settings_panel
        panel.focus_setting(key, row, field)

    def _leave_find(self) -> None:
        """Esc in the Find panel's empty search field: back to the view."""
        if self.views.currentWidget() is self.map_view:
            self.map_view.canvas.setFocus()
        else:
            self.steps_panel.list.setFocus()

    def open_from_map(self, table_id: str, step_id: str) -> None:
        self.session.select_table(table_id)
        if step_id:
            self.session.select_step(step_id)
        self.show_map(False)

    def _update_message(self, shown: str, new: str) -> None:
        """``new`` instead of the message ``shown`` ("" = none), but only while the status bar
        still shows that one: another message stays its time."""
        status = self.statusBar()
        if status.currentMessage() == shown:
            if new:
                status.showMessage(new, 10000)
            else:
                status.clearMessage()

    def _run_busy(self, running: bool) -> None:
        self._update_actions()
        if running:
            self.statusBar().showMessage("Running the project…")
        elif self._close_after_run:
            QTimer.singleShot(0, self.close)  # after the results are shown

    def _calc_busy(self, busy: bool) -> None:
        self.busy_label.setText("Calculating…  " if busy else "")

    def _run_finished(self, results: dict) -> None:
        failed = [r for r in results.values() if r.failed]  # a table that is off (skipped) is no error
        warned = any(level == "warning" for r in results.values() for s in r.steps for level, _ in s.messages)
        self.run_dock.show()
        text = "Run finished with errors" if failed else "Run finished with warnings" if warned else "Run finished"
        self.statusBar().showMessage(f"{text} — see Run results", 10000)

    def _run_stopped(self, _results: dict) -> None:
        self.run_dock.show()
        self.statusBar().showMessage("Run stopped — the steps after the stop did not run", 10000)

    def edit_connections(self) -> None:
        ConnectionsDialog(self.session, self).exec()

    def edit_parameters(self) -> None:
        ParametersDialog(self.session, self).exec()

    # --- dialogs -----------------------------------------------------------------------------

    def show_plugins(self) -> None:
        _text_dialog(self, "Plugins", self.plugins_text())

    def plugins_text(self) -> str:
        """The plugins with their actions and problems, and the plugin folders (Help → Plugins)."""
        lines = []
        for info in self.session.registry.plugins:
            lines.append(f"{info.name}   ({'built-in' if info.builtin else info.path})")
            for action_id in info.actions:
                action = self.session.registry.actions[action_id]
                lines.append(f"    {action.name}   [{action.category}, {action.id}, v{action.version}]")
            for connector_id in info.connectors:
                lines.append(f"    {self.session.registry.connectors[connector_id].name}   [connection type]")
            for error in info.errors:
                lines.append("    ERROR: " + error.replace("\n", "\n    "))
            lines.append("")
        folders = "\n".join(f"    {f}" for f in default_plugin_folders()) or "    (none)"
        lines.append(f"Plugin folders:\n{folders}")
        lines += [f"    WARNING: {warning}" for warning in self.session.registry.warnings]
        return "\n".join(lines)

    def show_about(self) -> None:
        licenses = "."
        if getattr(sys, "frozen", False):  # the download: the licenses of all its parts are next to the programs
            licenses = f"; the licenses of all parts are in {Path(sys.executable).parent / 'licenses'}."
        QMessageBox.about(
            self,
            f"About {APP_NAME}",
            f"<b>{APP_NAME} {__version__}</b><br>Build tables step by step and see the data after every step.<br>"
            f"Free software under the MIT License: <a href='{PROJECT_URL}'>{PROJECT_URL}</a><br><br>"
            f"Python {platform.python_version()} · Polars {pl.__version__} · DuckDB {duckdb.__version__} · "
            f"PySide6 {pyside_version}<br>Qt {qVersion()} and PySide6 are under the LGPL-3.0{licenses}<br><br>"
            f"Log file: {self.log_file or '-'}",
        )

    def open_guide(self) -> None:
        """Help → User guide: Guide.html next to the programs in the download, else the guide on GitHub."""
        page = Path(sys.executable).parent / GUIDE_FILE
        if getattr(sys, "frozen", False) and page.is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(page)))
        else:
            QDesktopServices.openUrl(QUrl(GUIDE_URL))

    def open_log_folder(self) -> None:
        if self.log_file is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.log_file.parent)))

    def _show_error(self, text: str) -> None:
        log.error("Unexpected error in a background task:\n%s", text)
        box = QMessageBox(QMessageBox.Icon.Critical, "Unexpected error", text.strip().splitlines()[-1], parent=self)
        box.setDetailedText(text)
        box.exec()

    # --- window state ------------------------------------------------------------------------

    def _restore_layout(self) -> None:
        geometry = self.settings.value("geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        else:
            self._first_size()
        for key, splitter in self._splitters().items():
            state = self.settings.value(key)
            if state is not None:
                splitter.restoreState(state)
        state = self.settings.value("window_state")
        if state is not None:
            self.restoreState(state)

    def _first_size(self) -> None:
        """1500 × 900 on the first start, at most 90 % of the screen, in the middle of it."""
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is None:
            self.resize(1500, 900)
            return
        free = screen.availableGeometry()
        width = min(1500, int(free.width() * 0.9))
        self.resize(width, min(900, int(free.height() * 0.9)))
        self.fit_panels(width)
        frame = self.frameGeometry()
        frame.moveCenter(free.center())
        self.move(frame.topLeft())

    def fit_panels(self, width: int) -> None:
        """In a window narrower than 1500 pixels, the panels beside the data give up some of
        their room, so that the data keeps most of it."""
        if width >= 1500:
            return
        left, settings = max(180, width * 280 // 1500), max(260, width * 420 // 1500)
        self.splitter.setSizes([left, max(width - left - settings, 100), settings])
        self.map_view.splitter.setSizes([max(width - settings, 100), settings])

    def _splitters(self) -> dict[str, QSplitter]:
        return {
            "splitter": self.splitter,
            "left_splitter": self.left_splitter,
            "map_splitter": self.map_view.splitter,
            "map_left": self.map_view.left,
            "map_right": self.map_view.right,
        }

    def ask_close_while_running(self) -> str:
        """Closing during a run: "wait" (close when it is finished), "stop" (stop it, then close)
        or "cancel" (keep the window open)."""
        box = QMessageBox(QMessageBox.Icon.Question, "Close", "The project is running.", parent=self)
        box.setInformativeText(
            "Wait until the run is finished, or stop it after the step that is running? The window closes then."
        )
        wait = box.addButton("Wait, then close", QMessageBox.ButtonRole.AcceptRole)
        stop = box.addButton("Stop the run", QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(wait)
        box.setEscapeButton(cancel)
        box.exec()
        return "wait" if box.clickedButton() is wait else "stop" if box.clickedButton() is stop else "cancel"

    def go_on_closing(self) -> None:
        """A sheet has finished the writing that the closing of the app waited for."""
        self._going_on_closing = True
        self.close()

    def _closing_stopped(self) -> None:
        """A Cancel: the next closing is a new one, and a sheet that is writing doesn't go on with it."""
        self._going_on_closing = False
        self._close_answers = {}
        for window in self._sheets:
            window.closes_app = False

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.session.running:  # closes when the run is over: it may be writing files
            event.ignore()
            if self._close_after_run:
                return
            answer = self.ask_close_while_running()
            if answer == "cancel":
                self._closing_stopped()
                return
            if not self.session.running:  # it ended during the question
                QTimer.singleShot(0, self.close)
                return
            self._close_after_run = True
            if answer == "stop" or self.session.stopping:
                self.stop_run()
            else:
                self.statusBar().showMessage("The window closes when the run is finished…")
            return
        self._close_after_run = False
        # A sheet is asked once, as long as it stays as it was then. A click on X during the write
        # that closing waits for belongs to the same closing.
        going_on = self._going_on_closing or any(window.busy and window.closes_app for window in self._sheets)
        answered = self._close_answers if going_on else {}
        self._going_on_closing = False
        for window in list(self._sheets):
            if not window.busy:
                window.apply_typing()
            if answered.get(window) == window.sheet.state_id:
                continue
            window.raise_()
            if not window.may_close(app=True):
                if window.busy:
                    self.statusBar().showMessage(window.status_text, 10000)
                    self._close_answers = answered
                    self._going_on_closing = False  # a write may have ended during the question
                else:
                    self._closing_stopped()
                event.ignore()
                return
            answered[window] = window.sheet.state_id
        if not self.maybe_save("before closing"):
            self._closing_stopped()
            event.ignore()
            return
        for window in list(self._sheets):  # only now: a Cancel above keeps them with their data
            window.close_now()
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("window_state", self.saveState())
        for key, splitter in self._splitters().items():
            self.settings.setValue(key, splitter.saveState())
        self.session.cancel_run()
        checks().stop_all()  # the checks of formula fields: nothing waits for them
        self.session.wait(5000)
        event.accept()


def _text_dialog(parent: QWidget, title: str, text: str) -> None:
    dialog = QDialog(parent)
    dialog.setWindowTitle(title)
    dialog.resize(760, 520)
    view = QPlainTextEdit(dialog)
    view.setReadOnly(True)
    view.setFont(theme.monospace_font())
    view.setPlainText(text)
    layout = QVBoxLayout(dialog)
    layout.addWidget(view)
    dialog.exec()
