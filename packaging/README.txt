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

  tafl-app.exe    the window. Open a project with File > Open (Ctrl+O), or drag the .tafl
                  file onto tafl-app.exe in Explorer (not onto the open window)
  tafl.exe        the command line, e.g. for the Windows Task Scheduler:
                  tafl.exe run C:\Projects\report.tafl
                  (tafl.exe --help lists all commands; in PowerShell, write .\tafl.exe)

.tafl files are not linked to Tafl: a double-click on one doesn't open it.

To try it: start tafl-app.exe, choose File > Open and open examples\demo.tafl in this folder.
On the command line, in this folder:

  tafl.exe run examples\demo.tafl
  tafl.exe show examples\demo.tafl --table Orders --step 4

The first command calculates the demo and writes its exports to examples\output; the second
shows the data after step 4 of the table Orders. The README on GitHub has more commands: there,
write tafl.exe for .venv\Scripts\tafl. The commands that start with .venv\Scripts\python (e.g.
examples\make_examples.py) need a source install with Python.

The programs are not signed. When Windows shows "Windows protected your PC", click
"More info", then "Run anyway". Your organization's settings may not allow programs from
the internet at all: then ask your IT department.

Settings, log and removing Tafl
-------------------------------
- Settings (window layout, recent projects): the registry key HKEY_CURRENT_USER\Software\Tafl
- The log of tafl-app.exe: %LOCALAPPDATA%\Tafl\tafl.log (Help > Open log folder)
- Your plugins, if any: %APPDATA%\Tafl\plugins

To remove Tafl, delete the folder Tafl. That leaves the three places above: delete the folders
%LOCALAPPDATA%\Tafl and %APPDATA%\Tafl too, and the registry key with regedit, if you like.

Scheduled runs
--------------
tafl.exe writes no log file: what it prints is all there is. To keep it, send its output to a
file. A batch file for the Task Scheduler (the folder C:\Logs must exist):

  @echo off
  set PYTHONIOENCODING=utf-8
  echo ===== %date% %time% report.tafl >> "C:\Logs\report.log"
  "C:\Tools\Tafl\tafl.exe" run "C:\Projects\report.tafl" >> "C:\Logs\report.log" 2>&1

The echo line puts the date and time before each run's output. The last line's exit code is
the batch file's: 0 when the run worked, 1 when a step failed, 2 when the project can't be
used (e.g. the file isn't there) or the command line is wrong.

Keep the quotes around the paths: without them, a path with a space in it (e.g. a folder
"My Reports") is split at the space, and Windows doesn't find tafl.exe, or tafl.exe stops
with exit code 2 ("unrecognized arguments").

When its output goes to a file, tafl.exe writes in the Windows code page: characters outside
it become "?" in the output, and codes like \u2192 in error and warning messages. The line
"set PYTHONIOENCODING=utf-8" makes it write UTF-8 instead.

In a batch file, write every % of a --set value as %%, e.g.
  --set "Month==STRFTIME(TODAY(), '%%Y-%%m')"
With a single %, the Windows command processor takes %Y-% for a variable of its own, and the
parameter gets a wrong value without an error.

Needed for some features
------------------------
SQL Server: Microsoft's "ODBC Driver 18 for SQL Server" (17 works too). Tafl signs in with
your Windows account. The driver "SQL Server" that comes with Windows is too old (it reads
dates and times as text): Tafl uses it only when you enter it as a connection's ODBC driver.

Plugins
-------
Your own plugins go into %APPDATA%\Tafl\plugins (or into the folders named in the environment
variable TAFL_PLUGINS). Both programs load them, and a plugin can import:
- Tafl's plugin API, tafl.api (before version 0.2.0: etl_tool.api): see "Writing a plugin" at
  https://github.com/xv8mna/tafl#writing-a-plugin. Its helpers for tests, tafl.api.testing,
  need a source install with Python and pytest to run the tests.
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
