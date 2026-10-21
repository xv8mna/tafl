# Changes

## 0.3.0 – 2026-10-21

Options for days without data: a folder import can go on without files, a new step skips the
rest of a table when it is empty, and every export can leave out an empty table. Also many
fixes, among them safer exports and file commands, SQL Server updates and upserts that store
every value as it is, and text sorted as in Excel.

Old projects mostly work as before, and the new options start with the old behaviour. But some
fixes give other results, and a step of an old project can then fail, e.g. a formula that took
amounts in a CSV file for text: "Results that change" lists these changes. Once a project is
saved by 0.3.0, 0.2.0 can't calculate its file imports, file exports, database exports and the
commands *Copy*, *Move* and *Delete files* any more (they say they were saved by a newer
version), nor the new step *Skip the rest if a table is empty* (an unknown action there).

### Days without data

- Folder imports (CSV, Excel, Parquet) have the choice **When there are no files**: stop with
  an error (as before), continue with an empty table (with a warning; it has only the File and
  Path columns), or skip the rest of the table. A folder that doesn't exist is still an error.
- New step **Skip the rest if a table is empty** (under Rows): when this table, or another one,
  has no rows, the steps after it are skipped, e.g. the exports. That is not an error: the run
  succeeds, and the table's result is the data at this step.
- A step that uses a table which skipped its rest and has no rows (*Start from*, *Merge*,
  *Append*, …) doesn't run, and the rest of its table is skipped too: tables that start from an
  import on a day without files skip their exports as well.
- Every export (CSV, Excel, database) has the option **Don't export an empty table** (off by
  default): the file or the database table is left as it is, and the step says "Not exported:
  the table is empty".
- Skipped steps show as *skipped* with the reason: in the steps, settings and data panels, in
  Run results ("ok, rest skipped: …"), on the map (dimmed, and "rest skipped" on the box) and on
  the command line.
- The README's daily-exports example uses them: on a day without files, the exports and the
  Move files step are skipped, and the table has no error, also right after a run that emptied
  the inbox. Before, the table showed an error after every run, and a run without files failed.
  Its Move files step takes all files the import read (**Which files**, see File commands), also
  those without rows.

### Exports

- CSV and Excel exports, also "Replace only this sheet", write the new file next to the old one
  and replace it only when it is complete. Before, a failed export, a run that was ended or two
  runs at once could leave a broken or half-written file. The next export of a file deletes the
  `~$…` file that a run on the same computer left when it was ended in the Task Manager (and a
  later run its folders in `%TEMP%`).
  In a folder where no new files can be created, the file is written over directly, with a
  warning. A hidden file stays hidden.
- Plain messages when the file is open in another program, is read-only, or the folder can't be
  written or created, and for names that can't be file names (`con.csv`, `?`, more than 255
  characters). Before, some of these were raw system errors, or named the wrong cause.
- Binary values (e.g. from `varbinary` or SQLite BLOB columns) are written as hex text,
  `0x0102FF`. Before, the CSV and Excel exports failed.
- CSV: decimal numbers are written with at most 15 significant digits, as Excel shows and saves
  them (0.37 * 3 gives 1,11, not 1,1099999999999999); whole numbers and exact decimals keep all
  their digits. Infinite and not-a-number values are written as empty, as in Excel; times as
  hh:mm:ss. A table with rows but no columns gives an empty file (it stopped with a raw error).
- Excel: texts longer than 32,767 characters are cut with a warning; a table with more rows or
  columns than a sheet holds stops with a plain message. Decimal columns get the number format.
  A table without columns (e.g. a folder import without files and without its File column) gives
  an empty sheet, as the CSV export gives an empty file; before, it stopped the export.
- Excel: the help of "If the file exists" describes both choices. A sheet name can't start or
  end with `'` (Excel doesn't allow it), and the log names the sheet as the workbook has it.
- The logs say "Wrote 1 row".

### Databases

- SQL Server: *Update*, *Upsert*, and *Replace* or *Delete the rows with the same key values*
  store values as *Add the rows* does, and keys compare as the table's own columns do (their
  types and collations). Before, texts of more than 255 characters failed, fractions of a second
  were cut (date-time keys could hit the wrong rows), and characters outside the Windows code
  page became "?" (keys like 'Łódź' found no row). Keys of every type find the rows that *Add*
  stored, e.g. `datetime`, `datetime2(0)`, `time`, `varchar` and `varbinary` keys and long
  texts.
- SQL Server: values are stored as their columns hold them, in every mode: fractions of a second
  are cut to the column's digits (*Add* into `time(n)` and `date` columns works now), decimals
  are rounded to the column's decimal places as SQL Server rounds them (1.005 is 1.01) and keep
  all 38 digits (more than 28 were rounded), and spaces at the end of a text that don't fit are
  cut. Texts for `char` and `varchar` columns get the characters of the column's collation, also
  with *Add*: Łódź stays Łódź in a Polish or UTF-8 column (it became Lódz). *Update* and
  *Upsert* refuse keys that are equal as the column holds them (08:00:01.2 and 08:00:01.7 in a
  `datetime2(0)`).
- SQL Server: date-times with a time zone are written in that zone's local time, without the
  zone, as SQLite and the Excel export do. Before, they were written in UTC. A `datetimeoffset`
  column gets the zone's offset with them, and the offset of this PC's time zone for a date-time
  without a zone, in every mode (*Update* and *Upsert* wrote +00:00).
- SQL Server imports keep the digits of `numeric` and `decimal` columns: whole numbers of 19 or
  more digits and decimals of more than 15 digits were decimal numbers, so different IDs could
  become equal.
- SQLite writes each date-time the same way in every export, whole seconds without `.000000`.
  Before, the other values of the export decided, so keys could miss their rows.
- SQLite exports exact decimals (e.g. from Parquet or SQL Server): a number column gets them as
  numbers, a text column with all their digits; new tables get `INTEGER`, `REAL` or `TEXT`,
  whichever keeps their digits. Before, such an export failed.
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
  which also left real decimal-comma columns as text. They follow the other numbers of their
  column (0.500 or 2,5 shows the separator), else those of the files; without any, they are
  thousands in files with semicolons. The log names the decimal separator of the files and
  the columns that have the other one.
- CSV: a file whose first line has no delimiter is one column, so values like 1.234,50 keep
  their comma (they were split at it); line ends converted twice (`\r\r\n`) count as one.
- CSV: amounts like 1.234,50 €, € 5 or 123,45- and percentages like 12,5% (read as 0.125)
  become numbers when every value of the column is one. Spaces between digits count only there,
  as thousands separators (1 234,50 €): phone numbers, card numbers or 2025 01 31 stay text.
- CSV: warnings for characters that don't fit the chosen encoding, and for rows with more
  values than the first row (the extra values are left out). With "Detect automatically", a
  UTF-8 file with a few wrong bytes is read as UTF-8, with a warning.
- "Detect numbers and dates" (CSV and database imports) keeps whole numbers of up to 19 digits
  exactly, also with thousands separators; a column with longer ones stays text, with a warning.
  Before, 19-digit IDs became decimal numbers, so different IDs could become equal.
- Excel: cells with only a time become times; a header row below the data gives a warning; the
  line fastexcel printed on the console is gone. The help explains merged cells and formulas
  without saved results.
- Parquet: decimals without decimal places become whole numbers when every value fits; other
  decimals keep all their digits, as before. Files with decimals of more than 38 digits can be
  read (before, they couldn't): values too long to be kept exactly become text, with a warning.
- CSV, Excel and Parquet: a file that another program has open without letting others read it
  says so ("… is open in another program"). Before, it was a raw system error.
- CSV with the decimal separator *Comma* chosen: 0.500 stays text (it was 500); thousands
  separators come after 1 to 3 digits that don't start with 0, in *Change type* too.

### Steps

- *Sort* orders text as Excel does: upper and lower case count the same, ä, é … sort with their
  base letter, ß like ss; equal values keep their order. *Number rows* uses the same order.
  Before, capitals came before small letters, and umlauts after z.
- Dates with two-digit years are read as in Excel: 00–29 are 2000–2029, 30–99 are 1930–1999,
  also with a time (31.12.25 08:30). Before, *Change type* without a format, and typed values in
  *Filter rows*, *Replace* and *Fill*, read 31.12.25 as the year 25.
- *Change type* → Whole number keeps all digits (before, values beyond 2^53 changed, and the
  largest became empty); → Decimal number says which values were rounded; Binary → Text gives
  hex, `0x0102FF`.
- *Change type*, numbers → text: formats with `%`, with text before or after the digits
  (`#,##0.00 €`, `"No. "0`) and with sections for positive and negative numbers and zero.
  Formats it can't write (e.g. `0.00E+00`) stop with a message instead of giving wrong text.
  Month and day names are English, as the help now says.
- Columns of exact decimals (e.g. from Parquet): *Filter rows* and *Replace* with typed values
  and *Fill* keep every digit, *Change type* → Text writes them all, and → Decimal number says
  which values were rounded. The grid shows them with all their digits and the PC's decimal
  separator.
- *Merge*: keys of different types are compared without changing this table's key column. Only
  a full join whose other table has keys that don't fit its type (2.5 for whole numbers) gives
  it a type that fits both. Whole numbers and exact decimals of different types are compared
  with all their digits.
- *Replace*: an invalid regular expression says why. *Aggregate*: "Sum needs a number column;
  Status is text"; average and median also take dates.
- `UPPER` in formulas writes ß as SS, like *Clean text* (GRÖSSE, not GRÖẞE).

### File commands

- *Copy*, *Move* and *Delete files* have the new setting **Which files**: *Listed in a column*
  (as before), or *All files this table's import read*: every file the table's folder import
  read, also files with only a header row or whose rows a filter left out (of Excel workbooks,
  those of which it read a sheet). In a table without a folder import, the step shows a warning
  while the project is edited, and a run stops it with a message.
- A file command that is stopped part-way (Stop, or Ctrl+C on the command line) says how many
  files it had copied, moved, renamed or deleted, in Run results and on the command line.
- *Copy*, *Move*, *Rename* and *Delete files* check all files before they change the first
  one: a missing file, a name that Windows doesn't allow (`..`, `:`, `CON`, more than 255
  characters, …), a read-only file to replace or to delete for good, or a folder where a file
  belongs stops the step with nothing changed. When Windows fails part-way, the message says
  how many files were done.
- Each file is copied or moved completely or not at all, also when the run is stopped: a move to
  another drive deletes the file only when its copy has the new name, and a file it replaces is
  kept until then. Before, a file open in another program could leave a copy behind, and
  *Replace it* could lose the old file. Only *Copy files* with *Replace it*, in a folder where no
  new files can be created, writes over the old file directly, with a warning, as exports do.
  Messages name the file that is open in another program or read-only, or the folder where no
  files can be created.
- Two listed files that would get the same name (e.g. `b.csv` from two date folders, moved
  into one folder): *Keep both* numbers them, *Replace it* and *Stop with an error* stop the
  step and name both. Before, *Replace it* deleted the file moved a moment before.
- *Replace it* never replaces a file the step has just written, or one of its own files.
- *Rename*: a file that already has its new name stays as it is (before, it became
  `name (2)` or stopped the run); a new name that differs only in upper or lower case renames
  the file.
- Plain messages for copying into a "folder" that is a file, and for *Create folder* and
  *Delete files* when a file or folder is in the way.

### The app

- A step just added is *incomplete* until its settings are filled in (grey-blue), not an error:
  the table isn't red, and the data panel shows the data going into the step. A run still
  counts it as an error. Its summary shows `?` for what is missing ("Keep rows where ?").
  Tables that use its table are *waiting* (grey-blue), not red.
- The status of the other tables follows changes, also Undo: tables that use a changed table
  are calculated again in the background, and **Reload data** calculates every table again.
  Before, a table could stay red until it was selected.
- Long texts above the data (a step's summary, a skip reason with a path) end with "…" and
  never make the window wider; the window can be made much narrower.
- **Stop** says "Stopping after the current step…", and a stopped run says so in the status bar
  and in Run results, which list the tables and steps that ran before the stop. A formula that
  is running stops at once (before, Stop waited for it).
  Closing the window during a run asks whether to wait or to stop.
- Ctrl+A and Ctrl+C on a big grid no longer freeze the window: up to 2,000,000 cells are
  copied (many in the background), more are refused with a message.
- *Aggregate* and other settings with lists fit a narrow settings panel; the panel scrolls
  sideways when it has to.
- Map: Ctrl+F, Ctrl+G and the group keys 0–9 work anywhere on the map; ◀ ▶ appear when not all
  group chips fit.
- Saving over a project file that was changed since it was opened, e.g. in a second window,
  asks first. Saving into a folder without permission, or over a read-only project file, gives a
  plain message, and the file paths of the steps stay as they were.
- Filter pane: **All**, then **Apply**, on many values no longer freezes, and the project no
  longer stores every value: a big selection is stored as the values left out. The value lists
  sort text in the order of *Sort*.
- The Parameters dialog works out formulas in the background: a slow formula doesn't block it.
- A connection test's error shows below the button, in full, and can be copied. A test's result
  stays with its connection when another one is selected meanwhile.
- A table can't get an empty name.
- Higher contrast for hints, placeholders, the map's step rows and colored texts (e.g. in Run
  results); the first window fits the screen.
- Run results show how long the run took. Before, they added up the time of the steps that
  worked, so a run whose slow step failed took "0.00 s". A table that is turned off is no error
  in the status bar.
- A step whose result was kept from before shows its messages again, e.g. the warnings of an
  import. Before, they showed only when it was calculated.
- "1 row" and "1 column" in the data panel, the tables list and Run results.
- Help → Plugins and the status bar warn about folders in `TAFL_PLUGINS` that don't exist.

### Command line

- Ctrl+C stops a run, also during a formula, at once: "Error: Stopped", exit code 130. Each
  table is printed as soon as it is calculated, and after Ctrl+C also the steps of the stopped
  table, so the output shows what ran. Each line reaches a log file at once, also when the run is
  ended; a step's summary is cut after 300 characters, as in the window. A Ctrl+C that comes
  while the run ends no longer makes `tafl.exe` hang.
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

Where an import now gives numbers or dates that later steps of an old project took for text,
change those steps, or switch off **Detect numbers and dates** in the import (then every column
is text).

- CSV with **Detect numbers and dates**: values with only thousands separators (1.000, 2.500)
  are 1000 and 2500, not 1 and 2.5. Amounts (1.234,50 €, 123,45-) and percentages (12,5% is
  0.125) are numbers, not text, so e.g. `REPLACE([Price], ' €', '')` or a filter for `5%` stops
  with an error. A column whose values show a decimal point is read with one also in a file with
  decimal commas: times like 08.30 become 8.3 (choose the decimal separator *Comma* to keep them
  text).
- CSV: a file without a delimiter in its first line is one column (values like 1.234,50 were
  split at the comma). With *Detect automatically*, a UTF-8 file with a few wrong bytes is read
  as UTF-8, not as Windows (ANSI). Line ends converted twice (`\r\r\n`) no longer leave an
  invisible character (`\r`) at the end of the last column's values.
- **Detect numbers and dates** (CSV and database imports): whole numbers of up to 19 digits are
  exact whole numbers, longer ones stay text with a warning; both were decimal numbers, so a
  formula like `[Id] + 1` on longer ones stops now. Date-times with two-digit years
  (31.12.25 08:30) are date-times, not text.
- Two-digit years 30 to 69 are 1930 to 1969: the detection of the imports and formats such as
  dd.mm.yy made them 2030 to 2069. *Change type* without a format, and typed values in *Filter
  rows*, *Replace* and *Fill*, read 31.12.25 as 2025, not as the year 25.
- Excel: cells with only a time are times, not date-times on 31.12.1899, so formulas such as
  `STRFTIME` and filters on them need a change.
- Parquet: decimals without decimal places are whole numbers when every value fits; smaller
  number types (e.g. Int32, Float32) become the usual whole and decimal numbers, so formulas on
  them calculate with more digits; categories become text.
- SQL Server imports: `numeric` and `decimal` columns keep their digits (see Databases), as
  whole numbers or exact decimals instead of decimal numbers.
- *Sort* and *Number rows* order text differently (see above).
- *Change type* → Whole number keeps all digits. Numbers → text writes the text and `%` of
  number formats, `General` and `@` write the number as it is (1,5, not 2), a comma after the
  digits divides by 1000 (`0.000,` writes 1234,5 as 1,235), and formats it can't write stop the
  step.
- *Change type* to a date or date-time without a format reads each text in its own format, so a
  column with 2025-12-31 and 31.12.2025, or with dates next to date-times, gets no empty values
  (a date alone is that day at 00:00). Before, only the format of most values was read.
- *Aggregate*: *Sum* of a date or date-time column, and *Average* or *Median* of a text column,
  stop the step with a message (they gave empty values).
- `UPPER` writes ß as SS.
- *Merge* keeps the type of this table's key columns, except in a full join whose other table's
  keys don't fit it.
- CSV exports write decimal numbers with at most 15 significant digits (1,11, not
  1,1099999999999999), infinite and not-a-number values as empty (they were inf and NaN), and
  times as hh:mm:ss (they were 08:30:00.000000000).
- SQL Server: a connection without a driver of its own no longer falls back to Windows' driver
  "SQL Server" (enter it in the connection to keep it). Exports write date-times with a time
  zone in that zone's local time, not in UTC.
- SQL Server exports by key store and find values as *Add the rows* does, and *Update* and
  *Upsert* refuse keys that are equal as their column holds them; `datetimeoffset` columns get
  the zone's offset, or this PC's, in every mode, and `char` and `varchar` columns the
  characters of their collation, also with *Add* (see Databases).
- SQLite: exports write whole seconds without `.000000`, also when other values have fractions
  (a key no longer finds a row written before as 08:00:00.000000); *Run SQL command* refuses
  `BEGIN`, `COMMIT` and `ROLLBACK`.
- *Rename files* leaves files that already have their new name; *Move* and *Copy files* with
  *Replace it* stop when two listed files would get the same name.

### Other

- A plugin that calls `sys.exit()` (or raises SystemExit or KeyboardInterrupt) no longer ends
  the app or the command line: it is a load error, or an error of its step (also from its
  `migrate`, `summary` or `fingerprint` function).
- An import that can't tell whether its data changed is read again for every calculation, also
  on Python 3.12, whose clock could make two quick calculations share an old result.
- Big tables: the results kept for the previews take at most a quarter of the installed memory
  (it was a fixed 4 GB), and only the last two settings of each step are kept, so memory no
  longer grows with every edit.
- The download's `README.txt` explains how to open a project, what removing the folder leaves,
  and a batch file for scheduled runs with a log.

### For plugins

- `SkipRest(message, df=None)` in `tafl.api`: ends the table at this step without an error.
- `source_params` has the new setting `no_files` (its default keeps the old behaviour);
  `source_files` can then give no files, and `combine([])` gives a table with only the File and
  Path columns. For a folder, `source_files` notes the files and the folder in
  `ctx.imported_files` and `ctx.imported_folder`, which the later steps of the table get.
- `@action(..., checks=)`: a function `(params, vc)` that gives the problems the parameters
  can't see on their own, e.g. with `ValidationContext.imported` (whether the table's import read
  files from a folder). They stop the step before it runs; for exports and commands they show as
  warnings while the project is edited.
- `TableResult.stopped_messages`: the messages of the step that was running when the run was
  stopped.
- `files.replace_file(target, warn)` writes a file safely; `copy_file`, `move_file` and
  `delete_file` change a file completely or not at all (a step that changes many files does it
  in `with files.batch():`); `name_problem` and `check_folder` check names and folders first;
  `files.read_error` gives a plain message for a file that can't be read.
- `dtypes.parse_integers`, `parse_decimal`, `float_loses_digits`, `text_sort_key` (for an
  expression or a Series) and `text_key`; `normalize(..., exact=True)`;
  `infer_types(..., warn=)`.
- Connectors: `input_sizes(df, types, temporary)`, `stage_types`, `fit_frame` and
  `read_table_sql`, `Database.execute_many(..., sizes=)`; `column_types` may give lengths and
  digits (`nvarchar(50)`, `decimal(10,2)`, `datetime2(0)`; SQL Server: a UTF-8 `varchar` with
  its collation); `make_series` keeps the digits of
  decimals (Int64, Float64 or Decimal), and stops with the message of an `ActionError` value,
  with `{column}` standing for the column's name.
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
