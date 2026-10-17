"""Starting the desktop app."""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import traceback
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

from tafl import APP_FOLDER, APP_NAME, COMMAND
from tafl.core import Registry, Status

from . import theme
from .main_window import MainWindow
from .session import Session

log = logging.getLogger("tafl")


def main(argv: list[str] | None = None) -> int:
    """Start the app; an optional argument is a project file to open."""
    argv = list(sys.argv if argv is None else argv)
    log_file = _setup_logging()
    _set_windows_app_id()
    QApplication.setOrganizationName(APP_NAME)
    QApplication.setApplicationName(APP_NAME)
    app = QApplication.instance() or QApplication(argv)
    theme.apply(app)
    app.setWindowIcon(theme.app_icon())

    # tools\build.py: the built app starts, opens a project and calculates every table, and nobody
    # is there to click a message away. Exit code 3: a table that has an error or no result, or a
    # plugin error (the log says which); 4: an unexpected error.
    check = bool(os.environ.get("TAFL_STARTUP_CHECK"))
    registry = Registry.default()
    for name, error in registry.errors:
        log.warning("Plugin %s failed to load: %s", name, error)
    for warning in registry.warnings:
        log.warning("%s", warning)
    session = Session(registry)
    if check:
        session.set_overview(True)  # all tables, as for the project map, not only the selected one
    window = MainWindow(session, log_file)
    hook = _excepthook(window, dialogs=not check)
    sys.excepthook = hook
    if check:  # an error in a background task (e.g. while calculating) as well: logged and counted

        def background_error(text: str) -> None:
            log.error("Unexpected error in a background task:\n%s", text)
            hook.errors += 1

        session.error.disconnect(window._show_error)
        session.error.connect(background_error)
    window.show()
    if len(argv) > 1:
        try:
            if check:
                session.open_project(argv[1])  # an error goes to the log instead of a message box
            else:
                window.open_path(argv[1])
        except Exception:
            hook(*sys.exc_info())
    if problems := len(registry.errors) + len(registry.warnings):
        window.statusBar().showMessage(f"{problems} plugin problem(s): see Help → Plugins", 15000)
    if check:

        def finish() -> None:
            if session.busy or session._timer.isActive():
                QTimer.singleShot(100, finish)
                return
            if hook.errors:
                app.exit(4)
                return
            problems = _check_problems(session)
            for problem in problems:
                log.error("Startup check: %s", problem)
            app.exit(3 if problems else 0)

        QTimer.singleShot(0, finish)
    return app.exec()


def _check_problems(session: Session) -> list[str]:
    """What fails the startup check: plugins that didn't load, and tables without a result or
    with an error (a table that is turned off is fine)."""
    problems = [f"Plugin {name} failed to load: {error}" for name, error in session.registry.errors]
    if not session.project.tables:
        problems.append("No project with tables was opened")
    for table in session.project.tables:
        result = session.results.get(table.id)
        if result is None:
            problems.append(f"Table '{table.name}' was not calculated")
        elif result.failed:
            errors = [f"table: {result.error}"] if result.error else []
            for number, (step, step_result) in enumerate(zip(table.steps, result.steps), start=1):
                if step_result.status == Status.ERROR:
                    errors.append(f"step {number} ({step.action}): {step_result.error}")
            problems.append(f"Table '{table.name}' has an error: " + "; ".join(errors or ["(no message)"]))
    return problems


def _setup_logging() -> Path | None:
    folder = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / APP_FOLDER
    try:
        folder.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            folder / f"{COMMAND}.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
    except OSError:
        return None
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)
    return folder / f"{COMMAND}.log"


def _excepthook(window: QWidget, dialogs: bool = True):
    """Unexpected errors go to the log file and a dialog instead of disappearing (no console).

    One dialog at a time, and an error from the same place only once: an error while drawing
    would otherwise come back with every repaint, dialog after dialog. The log gets them all.
    ``hook.errors`` counts them; without ``dialogs`` they only go to the log.
    """
    seen: set[tuple[str, str, int | None]] = set()
    showing = False

    def hook(kind, value, tb) -> None:
        nonlocal showing
        hook.errors += 1
        text = "".join(traceback.format_exception(kind, value, tb))
        log.error("Unexpected error:\n%s", text)
        frames = traceback.extract_tb(tb)
        place = (kind.__name__, frames[-1].filename, frames[-1].lineno) if frames else (kind.__name__, "", None)
        if not dialogs or showing or place in seen:
            return
        seen.add(place)
        showing = True
        try:
            box = QMessageBox(QMessageBox.Icon.Critical, "Unexpected error", f"{kind.__name__}: {value}", parent=window)
            box.setInformativeText("Details are in the log file (Help → Open log folder).")
            box.setDetailedText(text)
            box.exec()
        except Exception:
            pass
        finally:
            showing = False

    hook.errors = 0
    return hook


def _set_windows_app_id() -> None:
    """Show the app's own icon in the taskbar instead of Python's."""
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(f"{APP_NAME}.Desktop")
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
