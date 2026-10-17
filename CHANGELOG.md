# Changes

## 0.3.0 – 2026-10-17

Options for days without data: a folder import can go on without files, a new step skips the
rest of a table when it is empty, and every export can leave out an empty table. Also many
fixes, among them safer exports and file commands, SQL Server updates and upserts that store
every value as it is, and text sorted as in Excel.

Old projects work as before: the new options start with the old behaviour. The changes that
can give other results are listed under "Results that change". Once a project is saved by
0.3.0, 0.2.0 can't calculate its file imports, file exports and database exports any more
(they say they were saved by a newer version).

### Days without data

- Folder imports (CSV, Excel, Parquet) have the choice **When there are no files**: stop with
  an error (as before), continue with an empty table (with a warning), or skip the rest of the
  table. A folder that doesn't exist is still an error.
- New step **Skip the rest if a table is empty** (under Rows): when this table, or another one,
  has no rows, the steps after it are skipped, e.g. the exports. That is not an error: the run
  succeeds, and the table's result is the data at this step.
- Every export (CSV, Excel, database) has the option **Don't export an empty table** (off by
  default): the file or the database table is left as it is, and the step says "Not exported:
  the table is empty".
- Skipped steps show as *skipped* with the reason: in the steps, settings and data panels, in
  Run results ("ok, rest skipped: …"), on the map and on the command line.
- The README's daily-exports example uses them: on a day without files, the exports and the
  Move files step are skipped, and the table has no error, also right after a run that emptied
  the inbox. Before, the table showed an error after every run, and a run without files failed.

### Exports

- CSV and Excel exports, also "Replace only this sheet", write the new file next to the old one
  and replace it only when it is complete. Before, a failed export, a run that was ended or two
  runs at once could leave a broken or half-written file.
- Plain messages when the file is open in another program, is read-only, or the folder can't be
  written or created. Before, some of these were raw system errors, or named the wrong cause.
- Binary values (e.g. from `varbinary` or SQLite BLOB columns) are written as hex text,
  `0x0102FF`. Before, the CSV and Excel exports failed.
- CSV: infinite and not-a-number values are written as empty, as in Excel; times as hh:mm:ss.
- Excel: texts longer than 32,767 characters are cut with a warning; a table with more rows or
  columns than a sheet holds stops with a plain message. Decimal columns get the number format.
- Excel: the help of "If the file exists" describes both choices. A sheet name can't start or
  end with `'` (Excel doesn't allow it), and the log names the sheet as the workbook has it.
- The logs say "Wrote 1 row".

### Databases

- SQL Server: *Update*, *Upsert*, and *Replace* or *Delete the rows with the same key values*
  store values as *Add the rows* does. Before, texts of more than 255 characters failed,
  fractions of a second were cut (date-time keys could hit the wrong rows), and characters
  outside the Windows code page became "?" (keys like 'Łódź' found no row). Keys from
  `datetime` and `smalldatetime` columns find their rows.
- SQL Server: date-times with a time zone are written in that zone's local time, without the
  zone, as SQLite and the Excel export do. Before, they were written in UTC.
- SQL Server: Windows' own driver "SQL Server" is no longer chosen by itself, as it reads dates
  and times as text. Without ODBC Driver 18 or 17, the message says what to install; the old
  driver still works when it is entered in the connection. A driver that isn't installed gets
  a list of the ones that are.
- SQL Server: importing a whole table reads `sql_variant`, `geography`, `geometry` and
  `hierarchyid` columns as text. In a query, the message names the column and shows the `CAST`
  to write. Before, the import failed with a driver message.
- SQL Server error messages are shorter: each message once, without the driver's prefixes and
  error numbers. The same error for many rows (e.g. duplicate keys) is shown once, with how many
  more there are, and it keeps the duplicate key value.
- Export to database: "Created table" shows only when the export worked. SQL Server: a `#temp`
  table as the target is refused (it would disappear); an infinite number is refused with a
  hint; a text too long for its column names the column and an example; new tables get
  `DECIMAL(p, s)` for decimal columns, and text columns count an emoji as two characters, as
  SQL Server does.
- Run SQL command: `GO 3` runs the batch three times, and a comment may follow `GO`. The log
  says "1 row changed".
- SQLite: an import's `ATTACH` works only when the project is run (while a project is only
  looked at, it could create a file). `BEGIN`, `COMMIT` and `ROLLBACK` in a command are refused
  with a plain message: the command runs in one transaction.

### Imports

- CSV: values like 1.000 or 12.000 (thousands with a point) are no longer read as decimals,
  which also left real decimal-comma columns as text. They follow the other numbers of the
  files; without any, they are thousands in files with semicolons. The log names the decimal
  separator.
- CSV: amounts like 1.234,50 €, € 5 or 123,45- and percentages like 12,5% (read as 0.125)
  become numbers when every value of the column is one.
- CSV: warnings for characters that don't fit the chosen encoding, and for rows with more
  values than the first row (the extra values are left out). With "Detect automatically", a
  UTF-8 file with a few wrong bytes is read as UTF-8, with a warning.
- "Detect numbers and dates" (CSV and database imports) keeps whole numbers of up to 19 digits
  exactly, also with thousands separators; a column with longer ones stays text, with a warning.
  Before, 19-digit IDs became decimal numbers, so different IDs could become equal.
- Excel: cells with only a time become times; a header row below the data gives a warning; the
  line fastexcel printed on the console is gone. The help explains merged cells and formulas
  without saved results.
- Parquet: decimal columns become decimal numbers, as in the Excel import.

### Steps

- *Sort* orders text as Excel does: upper and lower case count the same, ä, é … sort with their
  base letter, ß like ss; equal values keep their order. *Number rows* uses the same order.
  Before, capitals came before small letters, and umlauts after z.
- Dates with two-digit years are read as in Excel: 00–29 are 2000–2029, 30–99 are 1930–1999.
  Before, *Change type* without a format, and typed values in *Filter rows*, *Replace* and
  *Fill*, read 31.12.25 as the year 25.
- *Change type* → Whole number keeps all digits (before, values beyond 2^53 changed, and the
  largest became empty); → Decimal number says which values were rounded; Binary → Text gives
  hex, `0x0102FF`.
- *Change type*, numbers → text: formats with `%`, with text before or after the digits
  (`#,##0.00 €`, `"No. "0`) and with sections for positive and negative numbers and zero.
  Formats it can't write (e.g. `0.00E+00`) stop with a message instead of giving wrong text.
  Month and day names are English, as the help now says.
- *Merge*: keys of different types are compared without changing this table's key column.
- *Replace*: an invalid regular expression says why. *Aggregate*: "Sum needs a number column;
  Status is text"; average and median also take dates.
- `UPPER` in formulas writes ß as SS, like *Clean text* (GRÖSSE, not GRÖẞE).

### File commands

- *Copy*, *Move*, *Rename* and *Delete files* check all files before they change the first
  one: a missing file, a name that Windows doesn't allow (`..`, `:`, `CON`, …) or a folder where
  a file belongs stops the step with nothing changed. When Windows fails part-way, the message
  says how many files were done.
- Two listed files that would get the same name (e.g. `b.csv` from two date folders, moved
  into one folder): *Keep both* numbers them, *Replace it* and *Stop with an error* stop the
  step and name both. Before, *Replace it* deleted the file moved a moment before.
- *Replace it* never replaces a file the step has just written, or one listed in its column.
- *Rename*: a file that already has its new name stays as it is (before, it became
  `name (2)` or stopped the run); a new name that differs only in upper or lower case renames
  the file.
- Plain messages for copying into a "folder" that is a file, and for *Create folder* and
  *Delete files* when a file or folder is in the way.

### The app

- A step just added is *incomplete* until its settings are filled in (grey-blue), not an error:
  the table isn't red, and the data panel shows the data going into the step. A run still
  counts it as an error. Its summary shows `?` for what is missing ("Keep rows where ?").
- **Stop** says "Stopping after the current step…", and a stopped run says so in the status bar
  and in Run results. Closing the window during a run asks whether to wait or to stop.
- Ctrl+A and Ctrl+C on a big grid no longer freeze the window: up to 2,000,000 cells are
  copied (many in the background), more are refused with a message.
- *Aggregate* and other settings with lists fit a narrow settings panel; the panel scrolls
  sideways when it has to.
- Map: Ctrl+F, Ctrl+G and the group keys 0–9 work anywhere on the map; ◀ ▶ appear when not all
  group chips fit.
- Saving over a project file that was changed since it was opened, e.g. in a second window,
  asks first.
- Filter pane: **All**, then **Apply**, on many values no longer freezes, and the project no
  longer stores every value: a big selection is stored as the values left out. The value lists
  sort text in the order of *Sort*.
- The Parameters dialog works out formulas in the background: a slow formula doesn't block it.
- A connection test's error shows below the button, in full, and can be copied.
- A table can't get an empty name.
- Higher contrast for hints, placeholders and the map's step rows; the first window fits the
  screen.
- Run results show how long the run took. Before, they added up the time of the steps that
  worked, so a run whose slow step failed took "0.00 s".
- A step whose result was kept from before shows its messages again, e.g. the warnings of an
  import. Before, they showed only when it was calculated.
- "1 row" and "1 column" in the data panel, the tables list and Run results.
- Help → Plugins and the status bar warn about folders in `TAFL_PLUGINS` that don't exist.

### Command line

- Ctrl+C stops a run: "Error: Stopped", exit code 130.
- `show` says why a table is skipped, and exits with 0 for a table that is turned off.
- Plain messages for a project file that isn't there, a folder, or a file that can't be read.
- A parameter formula that fails because of a `--set` value of another type says so.
- `--rows` must be at least 1, and a table of one row says "1 row". Every argument has a help
  text, and `--help` lists the exit codes.
- Warnings for plugin folders given with `--plugins` or in `TAFL_PLUGINS` that don't exist.
- Without Unicode, tables with symbols in their data line up.

### Parameters

- The message for a name that can't be used gives the rule: letters A–Z, digits, _ and spaces,
  starting with a letter or _. On the command line, such a name that nothing uses only gets a
  warning.

### Results that change

- *Sort* and *Number rows* order text differently (see above).
- Two-digit years 30 to 69 are 1930 to 1969; with a format such as dd.mm.yy they were 2030
  to 2069.
- *Merge* keeps the type of this table's key columns.
- SQL Server exports write date-times with a time zone in that zone's local time, not in UTC.
- *Change type* writes the text and `%` of number formats; formats it can't write stop the step.
- *Rename files* leaves files that already have their new name; *Move* and *Copy files* with
  *Replace it* stop when two listed files would get the same name.

### Other

- A plugin that calls `sys.exit()` (or raises SystemExit or KeyboardInterrupt) no longer ends
  the app or the command line: it is a load error, or an error of its step (also from its
  `migrate`, `summary` or `fingerprint` function).
- Big tables: the results kept for the previews take at most a quarter of the installed memory
  (it was a fixed 4 GB), and only the last two settings of each step are kept, so memory no
  longer grows with every edit.
- The download's `README.txt` explains how to open a project, what removing the folder leaves,
  and a batch file for scheduled runs with a log.

### For plugins

- `SkipRest(message, df=None)` in `tafl.api`: ends the table at this step without an error.
- `source_params` has the new setting `no_files` (its default keeps the old behaviour);
  `source_files` can then give no files, and `combine([])` gives a table with only the File and
  Path columns.
- `files.replace_file(target)` writes a file safely; `files.read_error` gives a plain message
  for a file that can't be opened.
- `dtypes.parse_integers`, `float_loses_digits`, `text_sort_key` and `text_key`;
  `infer_types(..., warn=)`.
- Connectors: `input_sizes` and `read_table_sql`, `Database.execute_many(..., sizes=)`;
  `column_types` may give lengths (`nvarchar(50)`); `make_series` stops with the message of an
  `ActionError` value, with `{column}` standing for the column's name.
- `xlsx.replace_sheet` returns the name of the sheet it replaced, as the workbook has it, or
  None when it added the sheet (it returned True or False).
- A summary gets `?` for a required setting that is still empty.

## 0.2.0 – 2026-10-16

The first version with a Windows download: a folder with `tafl-app.exe` (the window) and
`tafl.exe` (the command line), no Python needed. Unzip it, start `tafl-app.exe`, and open
`examples\demo.tafl` to try it. New: parameters, values that the whole project uses, and help
with formulas (a list of the functions, a formula window, checks while typing).

### For plugins

- The Python package is called `tafl` now, like the app: plugins import from `tafl.api` instead
  of `etl_tool.api`, and the base class of `ActionError` and `Cancelled` is `TaflError` (it was
  `EtlError`). A plugin that still imports `etl_tool.api` doesn't load, and its error says what
  to change.
- New in `tafl.api`: `parameters` (the project parameters and how `{Name}` is filled in),
  `all_results` and `split_name`; `Connector.literal`, `sql_spans`, `fill_sql`, `missing_table`
  and `column_types`, which a connector can override for its database; `expressions.tokenize`,
  `column_names`, `is_panic` and `evaluate(..., condition=True)`; `P.Expression(creates=...)`
  for the formula of a new column; `accepts` of every setting (a value of the wrong kind);
  `dtypes.type_label`; a `warn` function for `xlsx.replace_sheet`.

### Updating an installation from source

```
git pull
.venv\Scripts\python -m pip install -r requirements.lock
.venv\Scripts\python -m pip install --no-deps -e .
.venv\Scripts\python tools\create_shortcut.py
```

The first `pip install` brings the libraries to the tested versions (tzdata is new). The second
one writes the commands `tafl` and `tafl-app` again: until then they look for the package by
its earlier name and don't start. The last line renews the desktop shortcut, whose icon file
moved. If a folder `src\etl_tool` is left (with only Python's cache files in it), delete it.

### Parameters

- **Project → Parameters** (also in the toolbar): values the whole project uses, written as
  `{Name}` in the settings you type, of steps and of connections: file names, folders, texts,
  column names, formulas and SQL. A value is fixed (`2026-10-01`, `500`, `D:\Reports`) or a
  formula (`=TODAY() - 1`) that can use the parameters above it. The dialog shows each value as
  it is now, or what is wrong.
- Formulas are worked out when the project opens, when the parameters change, on **Reload
  data** and for every run; the previews in between keep the values. A formula may take at most
  5 seconds.
- In formulas a parameter is a value of its type (`[Date] = {Day}` compares dates, a negative
  number is `(-5)`), and its text inside quotes and column names (`[Sales {Month}]`). In SQL it
  is written for the database (`CAST('2026-10-15' AS date)` on SQL Server, `N'…'` texts), its
  text inside '…'; names and comments stay as they are (found where SQLite and SQL Server find
  them, so a value can't end or start one). In file names and texts a fixed value stays as it
  was written (`2.50`).
- In formulas and SQL a value never joins a word or a number next to it: `Sales_{Year}` becomes
  `Sales_ 2026`, not the table `Sales_2026`. In SQL `5-{Offset}` with -5 becomes `5- -5`.
- SQL Server: the arguments of `EXEC` get constants, as SQL Server needs there (also in
  `EXEC (…) AT` a linked server): a date as `'20261015'`, a date-time as
  `'2026-10-15T08:30:00.000'`, read the same in every language setting.
- A fixed date-time with a time zone (`2026-10-01 08:30+02:00`) is that moment in local time in
  formulas and SQL, as `NOW()` is.
- Inside '…' a value is only text, but a text that is itself run as SQL (`EXEC('…')`,
  `sp_executesql N'…'`) makes it code there: give such a value to the dynamic SQL as a
  parameter instead (the README shows how).
- A `{Name}` that is no parameter is an error in a formula (outside quotes and column names)
  and, in a project with parameters, a warning in other settings.
- Formula fields: `{` lists the parameters with their values (also inside column names),
  `{Name}` has its own color (red where it would be an error), and the line under the
  formula and the formula window use the values.
- `tafl run --set Name=VALUE` (and `tafl show`) gives a parameter another value for one run;
  the run lists the values it used.
- Renaming a parameter renames it in the steps, in the other parameters and in the
  connections.

### Formulas

- **Formula functions** (F1): every function with what it does, how it is written and an
  example, with a search; names from SQL Server and spreadsheets find what to use instead.
- **Formula window** (F2): a big editor with the columns, the functions and a preview of the
  result on the first 1,000 rows.
- **A line under each formula** shows what it gives, or what is wrong, while you type; the
  mistake is underlined and a misspelled name can be corrected with one click.
- **Suggestions while typing**: columns after `[`, functions and keywords after two letters.
- **Colors** for columns, functions, keywords, texts, numbers and comments; an unknown
  `[column]` is red, and the matching bracket is marked.
- Clearer messages: comments in formulas, a formula that ends too early, functions that don't
  exist (with what to write instead), columns that differ only in upper and lower case.

### Safer with project files from others

Opening a project calculates it. Formulas can only use the data of their step (no files,
network or settings), and imports roll back what their SQL query changed. See "Project files
from other people" in the README.

### Databases

- SQL Server: an error in a later statement of a SQL command fails the step, and nothing of the
  command is kept.
- Decimals that would be cut off in a whole-number column (1.5 → 1) are refused.
- Readable messages for values that don't fit their column, and for tables that can't be read
  (e.g. no permission).
- A query that fills a `#temp` table without `SET NOCOUNT ON` imports its result.
- SQL Server: a GO line inside a text or a comment of a SQL command no longer splits it into
  batches.
- SQL Server: when the ODBC driver can't read the SQL, the message says that it takes `{…}` for
  an ODBC escape (before, only "syntax error" in the language of Windows).
- Changing a connection's settings reads the data of its imports again (before, only
  **Reload data** did).

### Excel, "Replace only this sheet"

- A sheet with its own pivot tables, slicers or timelines is not replaced (move them to another
  sheet first); the step warns about other things the old sheet had of its own.
- On a sheet with only its header row, ranges down to row 2 follow the data.

### Other

- On Windows, a file or folder name with a `:` (e.g. a time, `08:30`) is an error; before, an
  export wrote the data into a hidden part of a file. So is a drive without a `\` after it
  (`C:data.csv`, a file in the current folder of drive C).
- A damaged project file shows a message that says what is wrong. Before, it gave an unexpected
  error, or closed the app when the app was started with it.
- A step setting of the wrong kind in a project file (e.g. a number where a table belongs) is an
  error of that step, with a message. Before, it gave unexpected errors.
- Project files that an editor saved with a byte order mark (BOM) open.
- Date-times with a time zone show in the grid and the filter pane on Windows: the time zones
  come from the tzdata package now.
- Tafl runs on Python 3.12 to 3.14. 3.15 doesn't work yet: DuckDB and pyodbc have no builds for
  it.
- The libraries' versions are pinned in `requirements.lock`; a weekly test runs the newest ones.
- The support for files and settings of an earlier name of the app was removed.

## 0.1.0 – 2026-10-15

The first public version.
