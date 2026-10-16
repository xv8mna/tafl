Tafl {version} for Windows
==========================

Tafl is a desktop app for preparing data: a table is built step by step (import, transform,
export), and the data after every step is right there to look at.
Documentation, source code and issues: https://github.com/xv8mna/tafl

Start
-----
Unzip the download into a folder of your choice, e.g. into C:\Tools (which gives
C:\Tools\Tafl) or into your user folder. Keep the folder Tafl together (the programs need the
files next to them), and start:

  tafl-app.exe    the window; drag a .tafl project file onto it to open it
  tafl.exe        the command line, e.g. for the Windows Task Scheduler:
                  tafl.exe run C:\Projects\report.tafl
                  (tafl.exe --help lists all commands)

To try it: open examples\demo.tafl.

The programs are not signed. When Windows shows "Windows protected your PC", click
"More info", then "Run anyway". Your organization's settings may not allow programs from
the internet at all: then ask your IT department.

Settings and the log file: %LOCALAPPDATA%\Tafl (log) and the registry key
HKEY_CURRENT_USER\Software\Tafl. To remove Tafl, delete the folder Tafl.

When its output goes to a file, tafl.exe writes in the Windows code page: characters outside
it become "?" in the output, and codes like \u2192 in error and warning messages. For a log in
UTF-8, set PYTHONIOENCODING=utf-8 first, e.g. in a batch file for the Task Scheduler:

  set PYTHONIOENCODING=utf-8
  C:\Tools\Tafl\tafl.exe run C:\Projects\report.tafl >> C:\Logs\report.log 2>&1

Needed for some features
------------------------
SQL Server: Microsoft's "ODBC Driver 18 for SQL Server" (17 works too). Tafl signs in with
your Windows account.

Plugins
-------
Your own plugins go into %APPDATA%\Tafl\plugins (or into the folders named in the environment
variable TAFL_PLUGINS). Both programs load them, and a plugin can import:
- Tafl's plugin API, tafl.api (before version 0.2.0: etl_tool.api), and tafl.api.testing for
  tests: see "Writing a plugin" at https://github.com/xv8mna/tafl#writing-a-plugin
- the Python standard library, without tkinter, turtle and turtledemo, IDLE, curses,
  ensurepip, venv, antigravity and the texts of help() (pydoc_data)
- the libraries Tafl uses: Polars, DuckDB, PyArrow (without Flight, Substrait, ORC and the
  cloud file systems), fastexcel, XlsxWriter, lxml, pyodbc, Send2Trash, tzdata (the time
  zones for zoneinfo) and PySide6 (QtCore, QtGui and QtWidgets)
Other Python libraries are not included.

Licenses
--------
Tafl is under the MIT license (LICENSE.txt). The libraries it includes are under their own
licenses: see the folder licenses. Python's license is in licenses\Python, with the notices for
the code of other projects that Python includes (ACKNOWLEDGEMENTS.txt). The start-up code of
tafl-app.exe and tafl.exe is from PyInstaller {pyinstaller}, under the GPL-2.0 with an exception
that allows this use and, for its run-time hooks, the Apache-2.0
(licenses\pyinstaller-{pyinstaller}\COPYING.txt).

Qt {qt} and PySide6 {pyside} with Shiboken6 {shiboken} (Qt for Python) are under the LGPL-3.0:
their DLLs in _internal\PySide6 and _internal\shiboken6 can be replaced by other builds of the
same versions. The LGPL and the GPL it builds on are in licenses\LGPL-3.0.txt and
licenses\GPL-3.0.txt. licenses\Qt-third-party.txt has the notices for the code of other
projects that is built into the Qt DLLs. The source code of these versions:

  Qt                     {qt_source}
  PySide6 and Shiboken6  {pyside_source}

lxml {lxml} includes GNU libiconv {iconv} in its modules etree and objectify
(_internal\lxml), under the LGPL-2.1 (licenses\LGPL-2.1.txt). They can be built again with
another libiconv from the source code of lxml, of the Windows libraries it uses and of
libiconv:

  lxml                   https://pypi.org/project/lxml/{lxml}/#files
  Windows libraries      https://github.com/lxml/libxml2-win-binaries
  libiconv               https://ftp.gnu.org/pub/gnu/libiconv/libiconv-{iconv}.tar.gz
                         (a copy is attached to the release of this download:
                         https://github.com/xv8mna/tafl/releases/tag/v{version})
