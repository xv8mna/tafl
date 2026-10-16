# Changes

## 0.2.0 – 2026-10-16

The first version with a Windows download: a folder with `tafl-app.exe` (the window) and
`tafl.exe` (the command line), no Python needed. Unzip it, start `tafl-app.exe`, and open
`examples\demo.tafl` to try it. New: parameters, values that the whole project uses.

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
  and a warning in other settings.
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
- Checking formulas while typing: a check that was stopped at an unlucky moment could make later
  checks fail.
- The Python package is called `tafl` now, like the app: plugins import from `tafl.api`.
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
