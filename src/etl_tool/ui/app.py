"""Starting the desktop app."""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import traceback
from pathlib import Path

from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

from etl_tool import APP_FOLDER, APP_NAME, COMMAND
from etl_tool.core import Registry

from . import theme
from .main_window import MainWindow
from .session import Session

log = logging.getLogger("etl_tool")


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

    registry = Registry.default()
    for name, error in registry.errors:
        log.warning("Plugin %s failed to load: %s", name, error)
    session = Session(registry)
    window = MainWindow(session, log_file)
    sys.excepthook = _excepthook(window)
    window.show()
    if len(argv) > 1:
        window.open_path(argv[1])
    if registry.errors:
        window.statusBar().showMessage(f"{len(registry.errors)} plugin problem(s): see Help → Plugins", 15000)
    return app.exec()


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


def _excepthook(window: QWidget):
    """Unexpected errors go to the log file and a dialog instead of disappearing (no console).

    One dialog at a time, and an error from the same place only once: an error while drawing
    would otherwise come back with every repaint, dialog after dialog. The log gets them all.
    """
    seen: set[tuple[str, str, int | None]] = set()
    showing = False

    def hook(kind, value, tb) -> None:
        nonlocal showing
        text = "".join(traceback.format_exception(kind, value, tb))
        log.error("Unexpected error:\n%s", text)
        frames = traceback.extract_tb(tb)
        place = (kind.__name__, frames[-1].filename, frames[-1].lineno) if frames else (kind.__name__, "", None)
        if showing or place in seen:
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
