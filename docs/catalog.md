# The catalog: what this Tafl can run, for programs

`tafl catalog --json` describes every action, setting, connection type and formula function of
this Tafl, with the plugins it loads. Programs that write or check project files read it, for
example an editor or a script that makes projects. The catalog is made from the same
declarations that the app builds its settings forms from, so it cannot differ from what Tafl
runs.

## The command

| Command | Output |
|---|---|
| `tafl catalog` | a readable list: actions by category, with their settings |
| `tafl catalog --json` | the catalog as JSON on stdout; messages go to stderr |
| `tafl --plugins FOLDER catalog --json` | also the actions of the plugins in FOLDER |

In the Windows download, write `tafl.exe`. The catalog lists what `tafl run` would load: the
built-in plugins, the folders in `TAFL_PLUGINS`, `%APPDATA%\Tafl\plugins` and the folders given
with `--plugins`.

The JSON has only ASCII characters: other characters are written as `\uXXXX` escapes (for
example `→` for an arrow). Every JSON reader turns them back. Without them, a Windows pipe
could change such characters into `?` without a message.

Exit codes: 0 done; 1 a plugin failed to load (the catalog is printed all the same, and the
plugin's entry has the error); 2 the command line can't be used.

## Versions

`format_version` is a text `"major.minor"`, now `"1.0"`.

- A new **minor** version only adds: new keys, new kinds of settings, new values in a list of
  values. A reader made for 1.0 can read 1.1.
- A new **major** version changes or removes something that a reader may use.

Rules for readers:

1. Ignore keys that you don't know.
2. Stop with a message when the major version is one that you don't know.
3. Expect setting kinds that you don't know (from plugins or a later minor version): such a
   setting has only the common fields (see below). Write its default, or leave it out.

The format of the project files has its own version (`project_file` in the catalog).

## Top level

| Key | Content |
|---|---|
| `format` | `"tafl-catalog"` |
| `format_version` | `"1.0"` |
| `app` | `name`, `version` (of Tafl), `plugin_api` (the version of the plugin API) |
| `project_file` | `format` and `format_version` of the project files this Tafl writes: `"tafl-project"`, `1` |
| `plugins` | one entry per plugin: `name`, `builtin`, `path` (`null` for a built-in one), `actions`, `connection_types` (their ids), `errors` (texts; empty when it loaded) |
| `actions` | one entry per action, sorted by id |
| `connection_types` | one entry per database type, sorted by id |
| `functions` | the functions and operators of formulas, in the order of the app's function list |

## Actions

| Key | Content |
|---|---|
| `id` | what a step stores in `"action"`, e.g. `"csv.import"` |
| `name` | the name in the app, e.g. `"Import CSV / text files"` |
| `category` | the group in the app's menu: Import, Rows, Columns, Calculate, Text, Combine, Export, Files |
| `kind` | `source` (makes the table; always the first step), `transform` (table in, table out), `export` (writes the table somewhere; the table passes through), `command` (does something else outside the project, e.g. with files or SQL; the table passes through) |
| `side_effects` | `true` for exports and commands: they change things outside the project and run only in a full run, never in a preview |
| `version` | the version of the action's settings: a step stores it in `"version"` |
| `plugin` | the plugin that provides it |
| `help` | what it does, as the app shows it: plain text, paragraphs separated by an empty line |
| `settings` | the settings, in the order of the form in the app |
| `adds_columns` | columns with fixed names that a step adds to its table (see below) |
| `settings_schema` | a JSON Schema of a step's `"params"` (see below) |

## Settings

Fields of every setting:

| Key | Content |
|---|---|
| `key` | the name under which a step stores the value in `"params"` |
| `kind` | what the value is (table below) |
| `label` | the label in the app |
| `required` | `true`: the setting needs a value when it is shown |
| `default` | the value of a new step; a setting that a step doesn't store has this value |
| `help` | an explanation, or `""` |
| `visible_if` | `null` (always shown), or when it is shown (see below) |

Kinds, the value a step stores, and the fields that only this kind has:

| `kind` | Stored value | More fields |
|---|---|---|
| `text` | a text | `multiline`, `placeholder` |
| `code` | a text, e.g. a SQL query | `multiline`, `placeholder`, `language` (`"sql"`) |
| `expression` | a formula (text) | `multiline`, `placeholder`, `condition`, `creates` |
| `int` | a whole number | `min`, `max` (`null`: no limit) |
| `float` | a number | `min`, `max` |
| `bool` | `true` or `false` | |
| `choice` | the `value` of one of the options, never its label | `options`: `[{"value", "label"}]` |
| `column` | a column name, exactly as it is (no patterns) | `column_source` |
| `columns` | a list of column names | `column_source` |
| `file` | a path; a relative path starts at the project's folder | `file_mode` (`"open"`: must exist, `"save"`), `file_filter` |
| `folder` | a path | |
| `table` | the **id** of another table of the project (not its name) | |
| `tables` | a list of table ids | |
| `connection` | the **name** of a connection of the project | `connection_types`: the allowed type ids (empty: any) |
| `list` | a list of rows; each row is an object with the `fields` | `fields` (settings, in the same form), `min_items` (the rows it needs at least) |

- `condition` (expression): `true` when the formula must give true or false for each row.
- `creates` (expression in a list): the key of the field in the same row that names the
  column the formula makes; later rows can use that column. `null` otherwise.
- `column_source`: `"input"` for a column of the step's input table; any other value is the
  key of the `table` setting whose table the column belongs to (e.g. the other table of a
  merge).

**`visible_if`** is an object: setting key → list of values. The setting is shown, and checked,
only when each of these settings has one of the listed values. Compare with the value the step
stores, or with the default when it stores none. In a list, the keys are fields of the same
row. Example: `{"source": ["folder"]}`: shown only when "Read" is "All files in a folder". A
setting that is not shown keeps the value the step stores, and Tafl doesn't check it.

**Project parameters** (`{Name}`) can be used in the settings of the kinds text, code,
expression, file, folder, column and columns, also in lists. Tafl fills them in before the step
runs. Settings of the other kinds take no parameters.

## Columns that a step adds: `adds_columns`

A list of `{"name", "type", "when"}`:

- `type`: one of the type names of Change type: `text`, `integer`, `number`, `date`,
  `datetime`, `boolean`.
- `when`: `null` (always), or the settings that switch the column on, in the form of
  `visible_if`.

The imports of CSV, Excel and Parquet files put File, Path and Sheet before the columns of the
data, in this order, when they are switched on. If the data has a column with the same name
already, the added column gets another name: `File (source)`. "Files in a folder" makes a table
with exactly its seven columns, in the order listed. Columns whose names come from settings
(e.g. the new column of Number rows) or from the data (e.g. Pivot) are not listed.

## The schema of the settings: `settings_schema`

A JSON Schema (draft 2020-12) of a step's `"params"` object, made from the settings: the type of
each value, the values of choices, `min` and `max`, the rows of lists, and the settings that a
step must always store (`required`): those that are required, always shown and empty by
default, and the lists that need more rows than they have by default. A setting that is required
and always shown can't be empty: a text needs at least one character (`minLength`), a list of
columns or tables at least one item (`minItems`), also in the rows of lists. Unknown keys are not
allowed, so a misspelled key is found before Tafl runs the step: Tafl itself would run it with a
warning, and the setting would have its default. Keys that start with `_` are allowed, also in
the rows of lists: Tafl uses them for its own marks, and gives no warning about them.

The schema does not contain `visible_if`. Some settings are required only when another setting
has a certain value, e.g. File when Read is "One file". `required` and `visible_if` in the
settings say this, and Tafl's own check reports what is missing. The schema has no
`if`/`then`/`else`, because some programs that make output fit a schema skip these without a
message.

Where the schema and Tafl's own check of a step differ:

- A setting that is required only after a choice: the schema takes it empty or missing, Tafl
  reports it.
- An unknown key: the schema refuses it, Tafl runs the step with a warning.
- A value of another JSON type, e.g. `"5"` for a whole number or `null`: the schema refuses it,
  Tafl converts some such values (`null` gives the default) and reports the others.
- The value of a setting that the step doesn't show: the schema checks it as any other, Tafl
  doesn't check it.

The schemas use only these keywords: `$schema`, `type`, `properties`, `patternProperties`,
`additionalProperties`, `required`, `enum`, `items`, `minItems`, `minLength`, `minimum` and
`maximum`.

## Connection types

`id` (what a connection stores in `"type"`), `name`, `plugin`, and `settings` in the form of an
action's settings.

## Functions

| Key | Content |
|---|---|
| `name` | e.g. `"IIF"`, or the operators of one entry, e.g. `"= <> < > <= >="` |
| `category` | Conditions, Operators, Text, Numbers, Dates, Conversion, Groups and order, More from DuckDB |
| `syntax` | how it is written, e.g. `"IIF(condition, if_true, if_false)"` |
| `summary` | what it gives, one sentence |
| `note` | more about it, or `""` |
| `example` | a formula that works, e.g. `"IIF([Amount] > 1000, 'big', 'small')"`, or `""` |
| `aliases` | other names that work too |
| `instead_of` | names from SQL Server or spreadsheets that don't work in formulas, where this one does the job |
| `curated` | `true`: described by Tafl, with a tested example; `false`: one of DuckDB's other functions, with DuckDB's description |

In formulas, columns are written in brackets (`[Amount]`) and texts in single quotes. The
functions of DuckDB depend on the DuckDB version that the Tafl program includes.

## A step in a project file

```json
{"id": "s4", "action": "csv.import", "version": 3, "params": {"file": "data/orders.csv"}}
```

- `action`: the action's `id`; `version`: its `version`. A step without `"version"` counts as
  version 1, and Tafl converts its settings from version 1.
- `params`: the settings that differ from the defaults (or all of them). A key that no setting
  has gets a warning on the step and has no effect.
- Optional: `"enabled": false` turns a step off; `"note"` is a text.
- A step's `id` can be left out: Tafl gives the step one when it opens the file. A table has an
  `id` and a `name`; settings of the kinds table and tables store the id. Ids are unique in
  the whole project (groups, tables and steps): when two share one, the first keeps it and Tafl
  gives the other a new id, so a setting then names the first one.

## Example

The action "Import CSV / text files", shortened (`...`):

```json
{
 "id": "csv.import",
 "name": "Import CSV / text files",
 "category": "Import",
 "kind": "source",
 "side_effects": false,
 "version": 3,
 "plugin": "csv_files",
 "help": "Reads a delimited text file (CSV, TXT, TSV), or all such files of a folder (and its subfolders) into one table: daily exports, for example.\n\n...",
 "settings": [
  {"key": "source", "kind": "choice", "label": "Read", "required": false, "default": "file",
   "help": "", "visible_if": null,
   "options": [{"value": "file", "label": "One file"},
               {"value": "folder", "label": "All files in a folder"}]},
  {"key": "file", "kind": "file", "label": "File", "required": true, "default": "", "help": "",
   "visible_if": {"source": ["file"]},
   "file_mode": "open", "file_filter": "Text files (*.csv *.txt *.tsv);;All files (*.*)"},
  {"key": "file_column", "kind": "bool", "label": "Add a column with the file name",
   "required": false, "default": true, "help": "", "visible_if": {"source": ["folder"]}},
  {"key": "skip_lines", "kind": "int", "label": "Skip lines at the top", "required": false,
   "default": 0, "help": "", "visible_if": null, "min": 0, "max": null},
  "..."
 ],
 "adds_columns": [
  {"name": "File", "type": "text", "when": {"source": ["folder"], "file_column": [true]}},
  {"name": "Path", "type": "text", "when": {"source": ["folder"], "path_column": [true]}}
 ],
 "settings_schema": {
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "properties": {
   "source": {"enum": ["file", "folder"]},
   "file": {"type": "string"},
   "file_column": {"type": "boolean"},
   "skip_lines": {"type": "integer", "minimum": 0},
   "...": {}
  },
  "patternProperties": {"^_": {}},
  "additionalProperties": false
 }
}
```
