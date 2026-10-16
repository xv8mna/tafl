Tafl {version} for Windows
==========================

Tafl is a desktop app for preparing data: a table is built step by step (import, transform,
export), and the data after every step is right there to look at.
Documentation, source code and issues: https://github.com/xv8mna/tafl

Start
-----
Keep this folder together (the programs need the files next to them), e.g. in
C:\Tools\Tafl or in your user folder, and start:

  tafl-app.exe    the window; drag a .tafl project file onto it to open it
  tafl.exe        the command line, e.g. for the Windows Task Scheduler:
                  tafl.exe run C:\Projects\report.tafl
                  (tafl.exe --help lists all commands)

To try it: open examples\demo.tafl.

The programs are not signed. When Windows shows "Windows protected your PC", click
"More info", then "Run anyway". Your organization's settings may not allow programs from
the internet at all: then ask your IT department.

Needed for some features
------------------------
- SQL Server: Microsoft's "ODBC Driver 18 for SQL Server" (17 works too). Tafl signs in with
  your Windows account.
- Your own plugins go into %APPDATA%\Tafl\plugins. They can use what Tafl itself uses
  (Polars, DuckDB, ...), but no other Python libraries.

Settings and the log file: %LOCALAPPDATA%\Tafl (log) and the registry key
HKEY_CURRENT_USER\Software\Tafl. To remove Tafl, delete this folder.

Licenses: Tafl is under the MIT license (LICENSE.txt). The libraries it includes are under
their own licenses: see the folder licenses. Qt and PySide6 are under the LGPL: their DLLs in
this folder can be replaced by other builds of the same version. The LGPL and the GPL it builds
on are in licenses\LGPL-3.0.txt and licenses\GPL-3.0.txt.
