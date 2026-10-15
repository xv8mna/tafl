"""Render the main window to a PNG without showing it on screen (to check the layout).

    .venv\\Scripts\\python tools\\screenshot.py out.png --project examples\\demo.tafl --table Orders --step 4

Message boxes are printed instead of shown, so the tool never waits for a click.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from etl_tool import APP_NAME
from etl_tool.core import Registry
from etl_tool.ui import theme
from etl_tool.ui.checks import checks
from etl_tool.ui.main_window import MainWindow
from etl_tool.ui.session import Session


def wait(app: QApplication, session: Session, seconds: float = 20) -> None:
    deadline = time.monotonic() + seconds
    app.processEvents()
    while time.monotonic() < deadline:
        app.processEvents()
        if not session.busy and not session.running and not session._timer.isActive():
            break
        time.sleep(0.02)
    end = time.monotonic() + 1  # then the lines under the formula fields (checked a moment after a change)
    while time.monotonic() < deadline:
        app.processEvents()
        if not checks().busy and time.monotonic() > end:
            app.processEvents()
            return
        time.sleep(0.02)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("out")
    parser.add_argument("--project")
    parser.add_argument("--table")
    parser.add_argument("--step", type=int)
    parser.add_argument("--size", default="1500x880")
    parser.add_argument("--map", action="store_true", help="show the project map")
    args = parser.parse_args()

    for name in ("question", "warning", "critical", "information"):
        setattr(QMessageBox, name, staticmethod(lambda *a, **k: print("message box:", *a[1:3], file=sys.stderr)))
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, tempfile.mkdtemp())
    QApplication.setOrganizationName(f"{APP_NAME} Screenshot")
    app = QApplication([])
    theme.apply(app)
    session = Session(Registry.default())
    window = MainWindow(session)
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)  # real Windows rendering, but invisible
    width, height = (int(v) for v in args.size.split("x"))
    window.resize(width, height)
    window.show()
    if args.project:
        window.open_path(args.project)
    if args.table:
        session.select_table(session.project.table(args.table).id)
    if args.step:
        session.select_step(session.table.steps[args.step - 1].id)
    if args.map:
        window.show_map(True)
    wait(app, session)
    window.grab().save(args.out)
    print(f"saved {args.out}")


if __name__ == "__main__":
    main()
