"""Tafl: a desktop app that builds tables step by step and shows the data after every step.

*Tafl* is Old Norse for "table".
"""

__version__ = "0.1.0"

# The app's name in one place: window, dialogs, shortcut, command line, folders.
APP_NAME = "Tafl"
APP_FOLDER = "Tafl"  # in %APPDATA% (plugins) and %LOCALAPPDATA% (log file)
COMMAND = "tafl"  # the command line program
APP_COMMAND = "tafl-app"  # the program that opens the window (what the desktop shortcut starts)
PROJECT_URL = "https://github.com/xv8mna/tafl"
OLD_APP_NAMES: tuple[str, ...] = ("ETL Tool",)  # earlier names: their settings and plugin folders are still used
OLD_APP_FOLDERS: tuple[str, ...] = ("ETL-Tool",)
