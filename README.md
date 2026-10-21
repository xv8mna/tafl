# Tafl

A desktop app for preparing data. A table is built step by step (import → transform → export),
and the data after every step is right there to look at. Projects are saved as `*.tafl` files
and can also be run from the command line, e.g. by the Windows Task Scheduler. *Tafl* is Old Norse
for "table".

**[User guide](docs/guide.md)**: a first project step by step, the window, every action, formulas,
parameters, databases and scheduled runs, with pictures. The download has it as `Guide.html`, and
**Help → User guide** opens it. This README has the details for reference.

![The table view: tables, steps, the data after the selected step, and its settings](docs/table-view.png)

- **Import** CSV / text files, Excel workbooks and Parquet files (one file or all files of a
  folder), SQL Server and SQLite tables or queries.
- **Transform** with steps: filter, sort, remove duplicates, fill empty values, rename and
  reorder columns, change types and formats, replace and clean text, calculated columns with SQL
  formulas, numbering rows, aggregating, merging (lookup and joins) and appending tables, and
  ending a table early when it is empty.
- **Export** to CSV, formatted Excel files (or one sheet of an existing workbook) and databases
  (insert, replace, update, upsert, delete by key), and run file commands and SQL commands.
- **Project map**: all tables and how they use each other, with groups for big projects.
- **Filter pane**: look at a selection of the rows without changing the table.
- **Plugins**: every action is a plugin; your own actions are a Python file away.

Built with Python, [Polars](https://pola.rs), [DuckDB](https://duckdb.org) and
[PySide6](https://doc.qt.io/qtforpython-6/) (Qt), and developed with the help of
[Claude](https://www.anthropic.com/claude) (Anthropic). Status: an early version, developed and
tested on Windows; [CHANGELOG.md](CHANGELOG.md) lists the versions.

## Install

Tafl runs on Windows 10 and 11. For SQL Server connections you also need Microsoft's "ODBC
Driver 18 for SQL Server" (17 works too); Tafl signs in with your Windows account, so no
passwords are stored. The driver "SQL Server" that comes with Windows is too old (it reads
dates and times as text): Tafl uses it only when you enter it as a connection's ODBC driver.

**Download** (no Python needed): get `Tafl-<version>-windows.zip` from the
[latest release](https://github.com/xv8mna/tafl/releases/latest) and unzip it into a folder of
yours: it holds the folder `Tafl`, so unzipping into `C:\Tools` gives `C:\Tools\Tafl`. Start
`tafl-app.exe` (the window) or `tafl.exe` (the command line) in it. The programs are not
signed: if Windows says "Windows protected your PC", click "More info", then "Run anyway". The
`README.txt` in the folder has the rest.

**From source** (Python 3.12, 3.13 or 3.14, and Git):

```
git clone https://github.com/xv8mna/tafl.git
cd tafl
py -3.14 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.lock
.venv\Scripts\python -m pip install --no-deps -e .
.venv\Scripts\tafl-app
```

`py -3.14` chooses Python 3.14 (`py` alone takes the newest Python installed); 3.12 and 3.13
work too. Python 3.15 does not work yet: DuckDB and pyodbc have no builds for it.
`requirements.lock` holds the exact versions of the libraries that Tafl is tested with (with
their checksums). `tafl-app` opens the window (optionally with a project file as argument).
`tafl` is the command line program. `.venv\Scripts\python tools\create_shortcut.py` puts a
**Tafl** shortcut on the desktop.

To update: `git pull`, then run the two `pip install` lines above again. Coming from 0.1.0, also
run `tools\create_shortcut.py` again; [CHANGELOG.md](CHANGELOG.md) says what changed for plugins.

**Other systems.** Tafl is plain Python and Qt, so it may work on Linux or macOS, but this is
untested. Windows-specific parts: SQL Server connections (Windows sign-in), the plugin folder in
`%APPDATA%` (elsewhere, use `TAFL_PLUGINS`), the desktop shortcut, and the `.venv\Scripts\…`
paths above (`.venv/bin/…` elsewhere).

## Try the demo

`.venv\Scripts\tafl-app examples\demo.tafl` opens the demo project (or **File → Open** in the
app). The download has it in the `examples` folder next to the programs: start `tafl-app.exe`
and open it with **File → Open** (Ctrl+O), or drag `demo.tafl` onto `tafl-app.exe` in Explorer
(`.tafl` files are not linked to Tafl, so a double-click doesn't open them). On the command line:

```
.venv\Scripts\tafl run examples\demo.tafl
.venv\Scripts\tafl show examples\demo.tafl --table Orders --step 4
```

In the download's folder, write `tafl.exe` instead of `.venv\Scripts\tafl` (in PowerShell:
`.\tafl.exe`).

- `run` calculates all tables and executes the exports and commands (the demo writes to
  `examples\output`).
  With `--preview` it only calculates and writes nothing; `--table NAME` runs only that table and
  the tables it uses (with their exports and commands); `--set Day=2026-10-01` gives a
  [parameter](#parameters) another value for this run (the demo has none). For a project with
  parameters it lists the values the run used. It exits with 0 when everything worked (tables
  that are turned off or skip their rest count as working), 1 when a step failed, 2 when the
  project can't be opened or has no such table or parameter, and 130 when Ctrl+C stopped it.
  It prints each table as soon as it is calculated, so after Ctrl+C the output shows what ran
  (e.g. which exports were written).
- `show` prints the steps of a table and the data after one step (also with `--set`).
- `plugins` lists all actions and shows plugins that failed to load.
- `--help` lists the commands; `run --help` (and `show`, `plugins`) shows the options and exit
  codes of one.
- The command line writes no log file: what it prints is all there is. To keep it, send it to a
  file (the download's `README.txt` shows a batch file for the Task Scheduler).

Only in a source install (they need Python):

- `.venv\Scripts\python examples\make_examples.py` recreates the demo data and project.
- `.venv\Scripts\python tools\make_big_project.py` creates `examples\big500.tafl`, a big project
  for trying the map: 75 tables, 460 steps, 7 groups with links between them (press Ctrl+2, then
  1–7 to jump to a group, or **Collapse all**).

## Using the app

![The project map](docs/project-map.png)

The [user guide](docs/guide.md) explains all of this with pictures and examples (**Help → User
guide** in the app). In short:

- **Tables** (top left): **+ New table** starts a table with an import. Double-click to rename,
  drag to reorder, right-click for more.
- **Steps** (bottom left): the steps of the selected table. Click a step to see the data after it.
  **+ Add step** adds a step after the selected one. Drag or Ctrl+↑/↓ to move, Del to delete,
  right-click to turn a step off or duplicate it. A step that still needs a setting is
  *incomplete* (grey-blue), not an error: its summary shows a `?` for what is missing, the data
  panel shows the data going into it, and the steps after it are calculated once it is filled
  in. A run counts it as an error. A table that uses a table with an incomplete step is
  *waiting* (grey-blue too) until that step is filled in.
- **Settings** (right): the selected step's settings. Changes apply when you press Enter or leave
  the field (in SQL fields: Ctrl+Enter); the data updates in the background. Formula fields:
  Enter applies, Shift+Enter starts a new line. Typing **[** (or the **[ ]** button) lists the
  columns with their types; after two letters of a word the list offers functions, keywords and
  columns (Ctrl+Space opens it at any time). Enter, Tab or a click writes the selected one in,
  Esc closes the list. Columns, functions, texts, numbers and comments have their own colors, a
  [column] the step doesn't have is red, and the bracket matching the one at the cursor is
  marked. While you type, the line under a formula shows what it gives on the first 1,000 rows
  (type and first values, or for a filter how many rows it keeps) or what is wrong: the mistake
  is underlined, clicking the line selects it, and a suggested name can be written in with one
  click.
- **Formula functions** (F1, or **ƒx** next to a formula): every function with what it does, how
  it is written and an example, with a search and categories. Search by name or by what it does
  ("month", "round"); names from SQL Server and spreadsheets find what to use instead (DATEADD,
  EOMONTH, NEWID, …). The window can stay open; Enter or a double-click inserts the function
  where the cursor was in the formula (around the selected text, if any), Esc closes it.
- **Formula window** (F2 in a formula, or **⤢** next to it): a big editor with the step's columns
  and their types (double-click or Enter inserts one), the function list, and a preview of the
  result on the first 1,000 rows that updates while you type: the columns the formula uses next
  to the result, its type, mistakes in plain words, and for a filter how many rows it keeps. In
  *Calculate columns* the columns of the rows above are there too. Enter starts a new line,
  Ctrl+Enter applies, Esc cancels (twice if you changed something).
- **Run project** (F5) calculates all tables and executes the exports and commands; **Run this
  table** (Shift+F5) does the same for the selected table and the tables it uses. While you edit,
  exports and commands never run. **Run results** list every table and step, with how long the
  run took. **Stop** stops a formula that is running at once; another step that is running
  finishes first (an export can take a while). The run results then list the tables calculated
  before the stop (their exports and commands are done) and, for the stopped table, the steps
  that ran, the step where it stopped (a file command says how many files it had copied, moved,
  renamed or deleted) and the steps that did not run. Closing the window
  during a run asks whether to wait or to stop. **Reload data** (Ctrl+R) reads files and
  databases again and brings the status of every table up to date. A changed file is also read
  again by itself when the table is calculated again: when you select the table again, change a
  step, or run. Selecting a step only shows what was calculated.
- **Map** (Ctrl+2): the project map, a full workspace: the selected table's steps and settings
  are on the right, its data below the map. **Tables** (Ctrl+1) switches back.
  - Every table is a box; arrows run from a table to the step that uses it (merge, append, start
    from). The selected table's arrows are blue; dashed arrows belong to steps that are off.
  - **Connect tables**: drag a table's ● (right edge) onto another table and choose Lookup, Left /
    Inner / Full join, Append or Keep rows with / without a match. The step is added before the
    exports, with the matching column filled in when both tables have one with the same name.
    Drop it on an empty spot instead: a new table that starts from the dragged one.
  - **Arrows**: click one to edit its step; right-click to turn it off or delete it; Del deletes.
  - **Boxes**: drag to arrange (saved with the project); right-click to add steps, rename, group,
    turn off, duplicate, run or delete; double-click to open a table or step in the table view.
    Boxes never pile up: what is in the way of a box or a growing group moves down.
  - **Groups**: select tables (Ctrl+click, or Shift+drag a rectangle) and press Ctrl+G or
    **+ Group**. The group bar above the map has a numbered chip per group: click it or press
    1–9 to jump there, 0 shows everything (anywhere on the map, also in its steps and settings;
    a text field keeps the digits typed into it). When not all chips fit, ◀ ▶ beside them show
    the others. Right-click a chip (or a frame's title) to collapse it, add the selected tables,
    select its tables, rename, change the color, turn all its tables off, or ungroup. Drag a box
    into a frame (or onto a collapsed group) to add it, out of the frame to remove it; drag a
    frame's title to move the whole group. In the table view, groups are folders: drag tables
    between them.
  - **Collapsed groups**: ▾ in a frame's title (or double-click a collapsed box) switches between
    the tables and one compact box. The box lists the group's tables that are connected to the
    outside, and arrows go to exactly those rows. **Collapse all** gives an overview of a big
    project.
  - **Find** (Ctrl+F, anywhere on the map): matching tables get an orange border; Enter jumps to
    the next one (a collapsed group opens for it).
  - **Turn a table off** (right-click a box, a table in the list, or a whole group): it is not
    calculated and its exports don't run; tables that use it show as *skipped* (grey), not as
    errors. The run results list them as skipped.
  - Ctrl + mouse wheel zooms, dragging the background moves the view, **Arrange automatically**
    tidies everything up.
- **Filter pane** (**Filters** above the grid): see only the rows with the values you pick,
  without adding a step. Add a filter with **+ Filter**, or right-click a column header
  (**Filter by …**) or a cell (**Show only rows where … is …**).
  - A card lists the column's values with their counts (and a bar). Select values (click,
    Ctrl+click, Shift+click) and press **Apply** (or Enter); double-click a value to filter by it
    alone. **Revert** goes back to the applied values, **Clear** stops filtering by the column.
  - Quick selection: **All** · **Possible** · **Excluded** · **None** (of the values shown, if you
    searched). **Excluded** inverts a selection. Click **Value** or **Count** to sort (texts in
    the order of *Sort*, see [Sort order](#sort-order)).
  - Colors: selected values blue, values that are possible with the other filters white,
    excluded ones grey. The counts are rows that pass the *other* filters.
  - **Modes** (the button next to the column name): *Values*; *Time periods* for dates (group by
    year, quarter, month, ISO week or day); *Number ranges* (type thresholds like `500; 1000`,
    choose where a value equal to a threshold goes); *Text length*; *Pattern* (a regular
    expression: matches / doesn't match). A column can have several filters, e.g. by month and
    by value: **+ Filter** offers it again.
  - Several values of one filter: any of them (OR); several filters: all of them (AND).
  - Filters stay while you click through the steps, so you can follow rows through a table; at
    a step that doesn't have the column, the filter is skipped. Each table has its own filters;
    they are saved with the project but are not edits (undo doesn't touch them). They never
    change the data, a run or an export. **⋯ → Add as step** turns a filter into a real
    *Filter rows by formula* step after the selected one.
- **Undo / redo**: Ctrl+Z / Ctrl+Y for every change. Ctrl+C in the grid copies the selected
  cells for Excel, up to 2,000,000 cells (for more, the status bar says so: export the table
  instead).
- **Errors**: an unexpected error shows a dialog (the same error only once) and goes to the log
  file, `%LOCALAPPDATA%\Tafl\tafl.log`: **Help → Open log folder**.
- **Connections** (toolbar): SQL Server and SQLite connections of the project, with a test button.
- Before a new project replaces the open one, or the app closes, you're asked to save unsaved
  changes. **New project** also asks when nothing changed, so it never switches by surprise.
  Saving over a project file that was changed since it was opened (e.g. in a second window)
  asks first.
- **Save as** into another folder keeps every file path pointing at the same files: paths in
  the new folder (or below it) are stored relative to the project file, others in full. A copy
  saved elsewhere therefore still writes its exports where the original does; a path that starts
  with a parameter (`{Folder}\orders.csv`) stays as it was written.

## Importing many files

The imports for CSV / text, Excel and Parquet files read **one file** or **all files in a
folder** ("Read: All files in a folder"), with all subfolder levels if you like: daily exports in
`2025\01\…`, a simple data lake.

- **File names**: a pattern such as `*.csv` or `orders_*.csv` (several with `;`, not case
  sensitive). Files are read in name order, with numbers in natural order (day2 before day10).
  Files whose names start with `~$` are skipped: Excel's lock files, and files an export is
  still writing.
- **When there are no files** that match: stop with an error (the default), continue with an
  empty table (with a warning), or skip the rest of the table (its exports and commands too,
  see [File commands](#file-commands)). The empty table has only the columns **File** and
  **Path** (those that are switched on; without both, a CSV or Excel export writes an empty
  file), so a later step that names another column fails: for a folder that is empty on some
  days, such as an inbox, choose *Skip the rest of the table*.
  A folder that doesn't exist is always an error.
- All files become **one table**: columns are matched by name, a column missing in a file stays
  empty there, and a column whose type differs between files gets a type that fits all of them
  (often text). CSV: delimiter and encoding are detected per file (a file whose first line has
  no delimiter has one column); numbers, dates and the decimal separator once for all files,
  the decimal separator also per column (`0.500` shows a decimal point, `2,5` a comma). A value
  like `1.000` can be one thousand or one: it follows the other numbers of its column, else
  those of the files, and without any it is one thousand in files with semicolons. Amounts
  (`1.234,50 €`, `123,45-`) and percentages (`12,5%` is 0.125) are numbers when every value of
  the column is one; spaces between digits count only there, as thousands separators
  (`1 234,50 €`), so phone numbers, card numbers or `2025 01 31` stay text.
- Optional columns **File** (file name) and **Path** (below the folder, e.g.
  `2025\01\orders_2025-01-02.csv`) show where each row came from. Calculated columns take
  what you need from them:
  - `SPLIT_PART([Path], '\', 1)` gives the year folder (`2025`), with `2` the month (`01`).
    (On systems other than Windows, the separator is `/`.)
  - `TRY_CAST(REGEXP_EXTRACT([File], '\d{4}-\d{2}-\d{2}') AS DATE)` gives the date in the file
    name (empty if there is none).
- **Excel sheets**: one sheet, **all sheets**, or the sheets whose names match (`2025*`), for one
  file or all files of a folder; the column **Sheet** names the sheet.
- New, changed or deleted files are noticed: the table is read again.
- **Files in a folder** (an import) lists the files with path, size and date: handy for
  checking what arrived.

## File commands

**Copy files**, **Move files**, **Rename files** and **Delete files** work on the files listed in
a column of the table (copy, move and delete also on all files the table's import read);
**Create folder** makes a folder. Like exports, they run only when the project runs (F5), never
while you edit, and the table passes through unchanged.

- **Which files** (copy, move, delete): *Listed in a column* (the default), or *All files this
  table's import read*: when the table starts with an import of all files in a folder (CSV,
  Excel or Parquet), every file it read, also files without rows (only a header row, or a filter
  left out their rows); of Excel workbooks, those of which it read a sheet. A file that arrives
  after the import stays where it is. In a table that doesn't start with such an import, the
  step shows a warning while you edit, and stops with a message in a run.
- A column holds full paths (e.g. *Full path* of "Files in a folder"), or paths below a folder
  (the *Path* column of a folder import: choose that folder in "Paths in the column are
  below"). *Rename files* always takes its files from a column.
- Copy / move: into a folder (created when needed), optionally keeping the subfolders (of the
  paths in the column, or below the import's folder). If a file of that name is there already:
  keep both (`name (2).csv`), replace it, skip it, or stop.
- Rename: the new names come from a column, e.g. a calculated `REPLACE([File], '.csv', '.done')`.
  A file that already has its new name stays as it is; a new name that differs only in upper and
  lower case renames the file.
- Delete: to the Recycle Bin (can be restored), or for good.
- Files that don't exist stop the run, or are skipped if you choose.
- All files are checked before the first one is changed: a missing file, a name that Windows
  doesn't allow (`:`, `..`, `CON`, more than 255 characters, …), a read-only file to replace or
  to delete for good, or a folder where a file belongs stops the step with nothing changed.
- Each file is copied, moved or renamed completely or not at all, also when the run is stopped.
  A move to another drive copies the file next to the target, gives the copy the target's name,
  and only then deletes the source; a file it replaces is kept until then. Only *Copy files*
  with *Replace it*, in a folder where files may be changed but no new ones created, writes over
  the old file directly, with a warning, as an export does: then a failed copy can leave it
  half-written. When a file is open in another program, the message names it.
- Two of the step's files that would get the same name, e.g. `2025\01\b.csv` and
  `2025\02\b.csv` moved into one folder: **Keep both** numbers them, **Replace it** and **Stop
  with an error** stop the step (keeping the subfolders keeps them apart). A step never replaces
  a file it has just written, nor one of its own files.

Example, daily exports: a table imports all files in `inbox` (*All files in a folder*, `*.csv`,
**When there are no files**: *Skip the rest of the table*), filters and exports them. Its last
step, **Move files** (**Which files**: *All files this table's import read*; to the folder
`archive`, keeping the subfolders if you like), moves the files after the exports: every file
the import read, also one with only its header row or one whose rows the filter left out. When
an export fails, the files stay in `inbox` for the next run, and so does a file that arrives
during the run. Keep the move in the importing table: other tables don't get the files its
import read. On a day without files, the import skips the exports and the move: the run
succeeds (Run results: *ok, rest skipped*), and the table has no error, also right after a run
that emptied the inbox. A table that uses this table on such a day (to start from it, merge or
append it) skips its rest from that step on.

On a day when the table has no rows (the files have only header rows, or the filter left none),
**Don't export an empty table** in the exports leaves the exported files as they are, and the
move still moves the files. The step *Skip the rest if a table is empty* before the exports
would skip the move too: the files would stay in `inbox`. With **Which files**: *Listed in a
column* (column *File*, or *Path* with subfolders; "Paths in the column are below": `inbox`),
the move takes only the files that have rows at its step: the others stay in `inbox` and are
read again the next day.

Before you run a project you got from someone else, look at its exports and commands: *Delete
files* can delete for good, and *Run SQL command* runs any SQL (see also
[Project files from other people](#project-files-from-other-people)).

## Exports and empty tables

Exports (CSV, Excel, database) run only when you run the project (F5, or **Run this table**),
never while you edit.

- A CSV or Excel export writes the new file next to the old one (named `~$…`) and replaces the
  old file only when the new one is complete: if the export fails, the old file stays as it was.
  A `~$…` file left by a run that was ended (e.g. in the Task Manager) is deleted by the next
  export of that file on the same computer (in a shared folder, those of other computers stay).
  In a folder where files may be changed but no new ones created, the export writes over the
  old file directly, with a warning: then a failed export can leave it half-written. A file
  that is open in another program, such as Excel, gives a message that says so: close it and
  run again.
- CSV: decimal numbers are written with at most 15 significant digits, as Excel shows and saves
  them (`0.37 * 3` gives `1,11`, not `1,1099999999999999`); whole numbers and exact decimals keep
  all their digits.
- Binary values (e.g. from a `varbinary` column) are written as hex text, `0x0102FF`.
- **Don't export an empty table** (in every export, off by default): when the table has no rows,
  the file or the database table is left as it is, and the step says "Not exported: the table is
  empty".
- **Skip the rest if a table is empty** (a step, under *Rows*): when the table at this step, or
  another table, has no rows, the steps after it are skipped, e.g. the exports and file commands
  on a day without new data. That is not an error: the table's result is the data at this step,
  the run succeeds, and the skipped steps say why. Folder imports can do the same when there are
  no files ([Importing many files](#importing-many-files)).
- A step that uses a table which skipped its rest and has no rows (*Start from*, *Merge*,
  *Append*, …) doesn't run: there is no data for it. The rest of its own table is skipped too,
  so the tables that start from an import on a day without files skip their exports as well.

## Exporting to a database

Add the step **Export to database** (SQL Server or SQLite; define connections with
**Connections** in the toolbar). Like every export it runs only when you run the project, and
always in one transaction: if anything fails, the database stays as it was.

| What to do | What happens in the database table |
|---|---|
| Add the rows | `INSERT` all rows |
| Replace all rows | delete all rows, then add |
| Replace the rows with the same key values | e.g. key column *Date*: the days in the data are deleted, then added again, so a daily reload replaces that day |
| Delete the rows where a condition is true, then add | e.g. `[Date] = CAST(GETDATE() AS date)` (the database's own SQL) |
| Update the rows with the same key values | e.g. key *OrderNo*: the other columns are updated, nothing is added |
| Update the rows with the same key values, add the others | update + add ("upsert") |
| Delete the rows with the same key values | only delete |

- Columns are matched by name (not case sensitive). Columns the database table doesn't have stop
  the export, or are left out if you choose. Empty key values match empty ones.
- A missing table is created with fitting types (SQL Server: `INT`/`BIGINT`, `FLOAT`,
  `DECIMAL(p, s)`, `NVARCHAR(n)`, `DATE`, `DATETIME2`, `TIME`, `BIT`, `VARBINARY(MAX)`; SQLite:
  `INTEGER`, `REAL`, `TEXT`, `BLOB`, and for exact decimals `INTEGER` or `REAL` when that keeps
  their digits, else `TEXT`). For other types, create the table yourself first. A temporary
  table (`#name` in SQL Server, `temp.name` in SQLite) can't be the target: it would disappear
  when the export ends.
- Values are stored as their columns hold them, the same way in every mode, and key values
  compare as the columns do (with their collation). SQL Server cuts fractions of a second to
  the digits of the column (`datetime2(0)` holds whole seconds), a `date` column keeps only the
  date, decimals are rounded to the column's decimal places as SQL Server rounds them (1.005 is
  1.01 in a `decimal(10,2)`), a `varchar` or `char` column gets the characters its collation's
  code page holds (Łódź becomes Lódz in a `Latin1_General` column and stays Łódź in a Polish or
  UTF-8 one), and spaces at the end of a text that don't fit are left out.
- Date-times with a time zone are written in that zone's local time, without the zone (08:30 in
  Berlin stays 08:30), as in the Excel export. A SQL Server `datetimeoffset` column gets the
  zone's offset with it (08:30 +02:00 in Berlin in summer); a date-time without a zone gets the
  offset of this PC's time zone on its day.
- Updating needs one row per key value, as the key column holds it (08:00:01.2 and 08:00:01.7
  are one value in a `datetime2(0)`); duplicates stop the export with an example.
- SQL Server: values that don't fit stop the export with a message that names the column: a
  text or binary value longer than its column (e.g. `nvarchar(50)`; in a UTF-8 `varchar`,
  longer in bytes), a decimal for a whole-number column, an infinite number (e.g. from a
  division by zero).
- 100,000 rows take a second or two with a local SQL Server, less with SQLite.

**Run SQL command** runs any SQL when the project runs, e.g. `EXEC dbo.RefreshReport` after an
export, or a `DELETE` / `UPDATE`. It runs in one transaction, so leave out `BEGIN` and `COMMIT`
(SQLite refuses them). For SQL Server, separate batches with `GO` lines as in SQL Server
Management Studio: `GO 3` runs the batch before it three times, and a `-- comment` may follow.

SQL Server connections need ODBC Driver 18 or 17 ([Install](#install)): without one, a
connection says what to install. Windows' own driver "SQL Server" is used only when you enter it
as the connection's ODBC driver; a driver name that isn't installed gets a list of the ones that
are.

Database imports: numbers keep all their digits. SQL Server `decimal`, `numeric` and `money`
columns become whole numbers or decimal numbers when that keeps every value, else exact
decimals. A whole SQL Server table reads `sql_variant`, `geography`, `geometry` and
`hierarchyid` columns as text; a query needs a `CAST` for them (the message shows it). In
SQLite, an import's `ATTACH` works only when the project is run, as it could create a file.

## Exporting to Excel

**Export to Excel file** writes the table as an Excel table (filter buttons, and banded rows if
you pick a table style) with formats for dates and numbers. The Excel table is named after the
sheet (sheet *Sales* → table `Sales`). Empty and not-a-number values become empty cells;
date-times with a time zone are written in that zone's local time. A sheet holds 1,048,575 rows
below its header: a bigger table stops the export (filter it, or export to CSV). A cell holds
32,767 characters: longer texts are cut, with a warning. If the file exists:

- **Replace the whole file**, or
- **Replace only this sheet, keep the other sheets**: for a report workbook whose other sheets
  have formulas, charts or pivot tables built on the data sheet. Everything else in the workbook
  is kept, including shapes, slicers, data connections and macros (`.xlsm` workbooks too; a new
  `.xlsm` can't be created). A sheet that doesn't exist yet is added as the last sheet.
  - The sheet keeps its name (upper and lower case as in the workbook), place, tab color and
    visibility, and its Excel table keeps its name. Use
    the table in formulas (`=SUM(Sales[Net])`) and as the source of pivot tables: it always has
    all the rows.
  - Charts, named ranges and pivot tables that covered the old rows (from row 1 or 2 down to the
    old last row; on a sheet with only its header row, down to row 2) are set to the new rows.
    Formulas with fixed ranges such as `=SUM(Data!C2:C500)` stay as they are: use the table or
    whole columns (`Data!C:C`).
  - What the replaced sheet itself holds goes with it: its own charts, pictures, shapes,
    SmartArt, comments, controls, embedded objects, conditional formats, data validation and
    header and footer pictures (the step warns when there were any).
    A sheet with pivot tables, slicers or timelines of its own is not replaced at all, because
    Excel ties them to other parts of the workbook: move them to another sheet first.
  - Excel calculates the formulas when it opens the file, and refreshes the pivot tables built on
    the sheet (their option "Refresh data when opening the file" gets switched on). Other
    programs show the old results until the file has been saved in Excel.
  - The workbook must not be open in Excel while the project runs: then the export stops with a
    message, and the workbook stays as it was.
  - So far this has been tested with LibreOffice only, not with Excel itself. If Excel offers to
    repair a workbook written this way, please open an issue. [docs/excel-check.md](docs/excel-check.md)
    is a ten-minute check in Excel, with a project for it in `examples`.

## Formulas

Calculated columns and *Filter rows by formula* use SQL expressions (DuckDB dialect). All
functions, with what they do and an example, are in **Help → Formula functions** (F1):

| Formula | What it does |
|---|---|
| `[Price] * [Quantity]` | arithmetic; `[Column Name]` like in T-SQL |
| `ROUND([Net] * 1.19, 2)` | functions: `ROUND`, `ABS`, `UPPER`, `TRIM`, `LEFT`, `REPLACE`, … |
| `[First] \|\| ' ' \|\| [Last]` | join text with `\|\|` or `CONCAT(...)` (not `+`) |
| `IIF([Amount] > 1000, 'big', 'small')` | T-SQL helpers: `IIF`, `ISNULL`, `LEN`, `GETDATE()`, `CHARINDEX` |
| `CASE WHEN [Country] = 'DE' THEN 'Germany' ELSE 'Other' END` | conditions |
| `YEAR([Date])`, `[Date] + 30`, `STRFTIME([Date], '%Y-%m')` | dates (`+ 30` adds 30 days) |
| `[Revenue] / SUM([Revenue]) OVER ()` | window functions (share of total, running sums, …) |
| `[Net] * 1.19 -- with VAT`, `/* note */` | comments |

A mistake shows a plain message that names the column of the step, e.g. *Formula error in [Gross]:
column [Amont] not found (did you mean [Amount]?)*. SQL Server functions that DuckDB doesn't have
(DATEADD, EOMONTH, CONVERT, …) get a hint about what to write instead. A formula can only use
the data of its step: no files, network or database access.

## Parameters

A parameter is a value the whole project uses, such as the day a daily report is for. Define
them with **Parameters** in the toolbar (or **Project → Parameters…**) and write `{Name}` in the
settings you type, of steps and of connections: file names, folders, texts, column names,
formulas and SQL. Numbers, choices and checkboxes take no parameters.

| Name | Value | Gives |
|---|---|---|
| `Day` | `=TODAY() - 1` | yesterday |
| `Month` | `=STRFTIME({Day}, '%Y-%m')` | `2026-10`: a formula can use the parameters above it |
| `Folder` | `D:\Reports` | a fixed text |
| `Limit` | `500` | a fixed number (also: dates `2026-10-01`, date-times `2026-10-01 08:30`) |

Formulas are worked out when the project opens, when the parameters change, on **Reload data**
(Ctrl+R) and for every run. The previews in between keep these values, so `=NOW()` doesn't
change with every edit. A formula may take 5 seconds: a slower one is stopped and counts as an
error. The dialog shows each value as it is now, or what is wrong.

How `{Name}` is filled in depends on where it is written:

- **File names, folders, texts, column names**: the value as text, e.g.
  `{Folder}\sales_{Month}.xlsx`. A fixed value stays as it was written: `2.50`, not `2.5`.
  Windows file names can't contain `:`, so write a time without it, e.g.
  `=STRFTIME(NOW(), '%Y-%m-%d %H-%M')`.
- **Formulas**: a value of its type, so `[Date] = {Day}` compares dates and `[Net] > {Limit}`
  numbers; a negative number in parentheses, `(-5)`. Inside quotes and column names the text:
  `'Report {Month}'`, `[Sales {Month}]`. Comments stay as they are.
- **SQL**: a value written for that database, e.g. `WHERE OrderDate = {Day}` becomes
  `WHERE OrderDate = CAST('2026-10-15' AS date)` on SQL Server; texts in quotes, with their own
  quotes doubled. Inside '…' the text, also with the quotes doubled: `LIKE '%{Month}%'`. Names
  (`[…]`, `"…"`) and comments stay as they are, so `{Name}` can't stand for a table name.
  SQLite and SQL Server connections find texts, names and comments where their database does
  (e.g. SQL Server's nested `/* */` comments), so a value can't end or start one.
- **SQL Server**: the arguments of `EXEC` take only constants, so there a date is written
  `'20261015'` and a date-time `'2026-10-15T08:30:00.000'`, which every language setting reads
  the same (`EXEC dbo.Refresh @Day = {Day}`, also `EXEC (…, {Day}) AT Server`). A text
  parameter gets them in this form too: write `@Label = '{Day}'` for `2026-10-15`. An argument
  can't be a calculation such as `{Days} + 1` (or `-{Days}` with a negative value). A date-time
  with parts of a millisecond gets six digits, which a `datetime` or `smalldatetime` parameter
  doesn't take: cut it to whole seconds for one, e.g. `=DATE_TRUNC('second', NOW())`. A
  `datetime` column (not `datetime2`) keeps times to 1/300 of a second: compare it with whole
  seconds or a range, as `= {Stamp}` with milliseconds can find nothing. Other places that take
  only constants get the typed form and fail: write a date there in quotes yourself, e.g. a
  procedure's default value `@Day date = '{Day}'`, or an ODBC `{call …}`.
- **SQL that runs SQL**: inside '…' a value is only text, but a text that is itself run as SQL
  (`EXEC('…')`, `sp_executesql N'…'`) makes it code there. Give such a value to the dynamic SQL
  as a parameter instead:
  `DECLARE @Day date = {Day}; EXEC sp_executesql N'SELECT … WHERE Day = @Day', N'@Day date', @Day = @Day`.

In formulas and SQL a value never joins a word or a number next to it: a space goes between
them, so `Sales_{Year}` becomes `Sales_ 2026`, not the table `Sales_2026`. `{Name}` can't stand
for a part of a name there (in file names and texts it can: `sales_{Month}.xlsx`). In SQL,
`5-{Offset}` with -5 becomes `5- -5`, not `5--5`, the start of a comment.

A fixed date-time with a time zone, `2026-10-01 08:30+02:00`, is that moment in the local time
of the computer in formulas and SQL, as `NOW()` is.

Only names of parameters are filled in: other braces, e.g. `\d{3}` in a pattern, stay as they
are. A `{Name}` that is no parameter stays as it is: in a formula, outside quotes and column
names, that is an error; in other settings the step shows a warning (in a project with
parameters). A parameter whose formula has an error stops the steps that use it. In formulas,
`{` lists the parameters with their values, and the line under the formula uses them. Renaming
a parameter in the dialog renames it in the steps, in the other parameters and in the
connections.

For one run with other values: `tafl run daily.tafl --set Day=2026-10-01` (several `--set`
for several parameters; a value can be a formula, `--set "Day==TODAY() - 7"`; `tafl show` takes
`--set` too). The run lists the values it used. In a batch file (`.bat`, e.g. for the Task
Scheduler), write every `%` as `%%`: `--set "Month==STRFTIME(TODAY(), '%%Y-%%m')"`. With a
single `%`, the Windows command processor takes `%Y-%` for a variable of its own, and the
parameter gets a wrong value without an error.

A parameter's name has letters A–Z, digits, `_` and spaces, and starts with a letter or `_`.
On the command line, a name that breaks this rule but that nothing uses only gets a warning.

## Formats

Formats use Excel's codes everywhere: dates `dd.mm.yyyy`, `dd.mm.yyyy hh:mm`; numbers `0`,
`0.00`, `#,##0.00`, `00000` (pads with zeros), `0%`, `#,##0.00 €`, `"No. "0`, and sections for
positive and negative numbers and zero, e.g. `#,##0.00;(#,##0.00);"–"`. *Change type* stops
with a message at a number code it can't write (e.g. `0.00E+00`), and writes month and day
names (`mmmm`, `dddd`) in English. Dates with two-digit years are read as in Excel: `31.12.25`
is 2025, `01.01.75` is 1975 (00–29 are 20xx, 30–99 are 19xx), also with a time
(`31.12.25 08:30`).

## Sort order

*Sort* orders text as Excel does: upper and lower case count the same, letters with accents sort
with their base letter (`Äpfel` next to `apfel`, `Özdemir` between O and P), and `ß` like `ss`.
Rows with equal values keep their order. *Number rows* and the value lists of the filter pane
use the same order.

## Project files from other people

Tafl calculates a table as soon as you look at it, with your rights: when a project opens, its
first table and the tables that one uses; then every table you select, and all of them while
the map is shown. Its imports read the files and databases the project names, and the SQL
queries of database imports run on those databases. Exports and commands only run when you run
the project.

Tafl rolls back what an import's query changed, and formulas can only use the data of their
step. This is no protection against a project made to do harm, though: a query can read
whatever your Windows login can read, and on SQL Server it can commit by itself or call
procedures with lasting effects. Open project files only from people you trust. If in doubt,
open the `.tafl` file in a text editor first: it is JSON, and its connections, SQL queries and
file paths can be read there.

## Writing a plugin

Every action (import, step, export, command) is a plugin. Put a `.py` file into
`%APPDATA%\Tafl\plugins` (create the folder if needed), or into a folder listed in the
`TAFL_PLUGINS` environment variable (several separated by `;`, or `:` on Linux and macOS), and
restart Tafl; **Help → Plugins…** shows what was loaded and any errors (also a folder in
`TAFL_PLUGINS` that doesn't exist). For a command line run, `tafl --plugins FOLDER run PROJECT`
adds a folder (the option goes before `run`). Built-in plugins live in `src\tafl\plugins`; the
Python package is called `tafl`.

```python
import polars as pl
from tafl.api import P, action


@action(
    "my.prefix",                  # unique id, stored in project files
    "Add a prefix",               # name shown in the app
    category="Text",
    params=[
        P.Columns("columns", "Columns", required=True),
        P.Text("prefix", "Prefix", default="#"),
    ],
    summary=lambda p: f"Prefix {', '.join(p.columns)} with {p.prefix}",
)
def add_prefix(df, p, ctx):
    """Puts a text in front of every value. (Shown as help in the app.)"""
    return df.with_columns(
        (pl.lit(p.prefix) + pl.col(c).cast(pl.String)).alias(c) for c in p.columns
    )
```

The settings form is built from `params`. Rules for plugins:

- Import only from `tafl.api` (and third-party libraries), never from other plugins. The
  Windows download includes only the libraries Tafl itself uses (Polars, DuckDB, …).
- When an action's settings (`params`) change, raise `version=` and add `migrate=`: a function
  `(params, old_version) -> params` that converts settings saved by older versions, so old
  projects keep working.
- Exports and commands (`kind="export"` / `"command"`) only run when a project is run, never in
  preview. An export writes its file with `tafl.api.files.replace_file(path, ctx.warn)`, which
  keeps the old file until the new one is complete; `copy_file`, `move_file` and `delete_file`
  there change a file completely or not at all.
- `raise ActionError("...")` for a problem the user can fix. To end the table at this step
  without an error, e.g. when there is nothing to do, `raise SkipRest("No new orders")`: the
  steps after it are skipped with that message.
- Test actions with `tafl.api.testing.StubContext` (see `tests/plugins`).

Plugins are ordinary Python code that runs with your rights when Tafl starts: only add plugins
you trust.

## Development

```
.venv\Scripts\python -m pip install -r requirements-dev.lock
.venv\Scripts\python -m pip install --no-deps -e .
.venv\Scripts\python -m pytest
.venv\Scripts\python -m ruff check src tests examples tools
```

- The UI tests run offscreen with pytest-qt; no window opens.
- The lock files (`requirements.lock`, and `requirements-dev.lock` with the test tools) are
  written by `tools\lock.py` (it needs `pip install uv`); `--upgrade` takes the newest versions
  that `pyproject.toml` allows. Once a week, CI also tests with the newest versions.
- `tools\build.py` builds the Windows download into `dist\` and checks it (see its description
  for the build environment). It also turns the user guide into `Guide.html`, with the pictures in
  the file. A tag like `v0.2.0` makes CI build it and publish the release, with
  that version's part of [CHANGELOG.md](CHANGELOG.md) as the notes.
- The SQL Server tests run only when the environment variable `TAFL_TEST_SQLSERVER` names a
  server you can reach with your Windows login (e.g. `.\SQLEXPRESS`). They read `master` and
  create and drop a table in `tempdb`.
- `tools\screenshot.py` renders the window into a PNG without showing it. The pictures above
  were made with it:

  ```
  .venv\Scripts\python tools\screenshot.py docs\table-view.png --project examples\demo.tafl --table Orders --step 4
  .venv\Scripts\python tools\screenshot.py docs\project-map.png --project examples\demo.tafl --table Customers --step 3 --map
  ```

- `tools\guide_screenshots.py` makes the pictures of the user guide (`docs\guide\*.png`) in the
  same way, from example projects it builds in a temporary folder: run it after a change to the
  window. With picture names (`--list` shows them), it makes only those.

## Reporting problems

Open an [issue](https://github.com/xv8mna/tafl/issues) with what you did, what you expected and
what happened, plus the version (**Help → About Tafl**) and the log file (**Help → Open log
folder**). A small project with data that shows the problem helps most. Check files before you
attach them: a `.tafl` file contains file paths, server and database names and the values picked
in the filter pane, and the log can contain paths and values from error messages.

Please report security problems privately instead: [Security](https://github.com/xv8mna/tafl/security) tab → **Report a vulnerability**.

## License

MIT, see [LICENSE](LICENSE). Tafl uses Polars, DuckDB, PySide6 (Qt for Python, LGPL) and other
libraries under their own licenses.
