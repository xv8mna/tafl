"""Excel files: import and export."""

from __future__ import annotations

import dataclasses
import datetime as dt
import fnmatch
import logging
import re
from pathlib import Path

import fastexcel
import polars as pl
import xlsxwriter

from tafl.api import (
    ActionError,
    Endpoint,
    P,
    action,
    col,
    combine,
    dtypes,
    endpoints,
    file_endpoint,
    files,
    source_endpoints,
    source_files,
    source_params,
    source_summary,
    sources_fingerprint,
    unique_names,
    xlsx,
)

# fastexcel says on the console when a column holds different kinds of values (it becomes text)
logging.getLogger("fastexcel").setLevel(logging.ERROR)

_TABLE_STYLES = [
    ("none", "None"),
    ("Table Style Light 1", "Light grey"),
    ("Table Style Light 9", "Light blue"),
    ("Table Style Medium 2", "Medium blue"),
    ("Table Style Medium 4", "Medium green"),
    ("Table Style Medium 7", "Medium orange"),
]
_INTEGER_TYPES = (pl.Int8, pl.Int16, pl.Int32, pl.Int64, pl.UInt8, pl.UInt16, pl.UInt32, pl.UInt64)
# Cells with only a time are date-times on Excel's day 0
_DAY_ZERO = [dt.date(1899, 12, 30), dt.date(1899, 12, 31), dt.date(1904, 1, 1)]
_MAX_ROWS, _MAX_COLUMNS = 1_048_575, 16_384  # below the header row


def _migrate_import(params: dict, from_version: int) -> dict:
    """Version 1 read one sheet of one file; version 2 can also read all files of a folder
    and several sheets; version 3 can go on without files in the folder."""
    if from_version < 2:
        params.setdefault("source", "file")
        params.setdefault("sheets", "one")
    if from_version < 3:
        params.setdefault("no_files", "error")
    return params


_SEVERAL_SHEETS = ("all", "matching")


def _import_endpoints(p) -> list[Endpoint]:
    """The file or the folder, with the sheets. The mode starts with the sheets ("first sheet, with
    subfolders"), and its setting is the one that chooses them."""
    words, sheet, setting = [], "", ""
    if p.get("sheets", "one") == "all":
        words, setting = ["all sheets"], "sheets"
    elif p.get("sheets") == "matching":
        words, setting = [f"sheets {p.get('sheet_pattern') or '*'}"], "sheet_pattern"
    elif str(p.get("sheet") or "").strip():
        sheet = str(p.sheet)
    else:
        words, setting = ["first sheet"], "sheet"
    return [
        dataclasses.replace(
            found,
            part=sheet,
            mode=", ".join([*words, *([found.mode] if found.mode else [])]),
            mode_setting=setting or found.mode_setting,
        )
        for found in source_endpoints(p)
    ]


@action(
    "excel.import",
    "Import Excel files",
    category="Import",
    kind="source",
    params=[
        *source_params("Excel files (*.xlsx *.xlsm *.xlsb *.xls *.ods);;All files (*.*)", "*.xlsx;*.xlsm;*.xlsb;*.xls"),
        P.Choice(
            "sheets",
            "Sheets",
            [("one", "One sheet"), ("all", "All sheets"), ("matching", "Sheets whose name matches")],
        ),
        P.Text("sheet", "Sheet", placeholder="empty = first sheet", visible_if={"sheets": "one"}),
        P.Text(
            "sheet_pattern",
            "Sheet names",
            default="*",
            visible_if={"sheets": "matching"},
            help="* stands for any text, e.g. 2025* or Sales*; several patterns with ;",
        ),
        P.Bool("sheet_column", "Add a column with the sheet name", default=True, visible_if={"sheets": _SEVERAL_SHEETS}),
        P.Int("header_row", "Row with the column names", default=1, min=0, help="0 = the sheet has no header row"),
        P.Bool("whole_numbers", "Whole numbers as integers", default=True),
        P.Bool("dates_without_time", "Date-times at midnight as dates", default=True),
    ],
    version=3,
    migrate=_migrate_import,
    summary=lambda p: f"Import {source_summary(p)}" + _sheets_summary(p),
    fingerprint=sources_fingerprint,
    endpoints=_import_endpoints,
)
def import_excel(df, p, ctx):
    """Reads Excel files (xlsx, xlsm, xlsb, xls) or OpenDocument spreadsheets: one file or all
    files of a folder (and its subfolders), one sheet, all sheets or the sheets whose names
    match, into one table.

    Rows above the header row are skipped. Excel stores every number as a decimal number;
    columns that only hold whole numbers become integers, cells with only a time become
    times. Sheets and files are matched by column names; a column missing in one stays empty
    there.

    Merged cells keep their value in the first cell only: 'Fill empty values' fills the
    others. Formulas give the results Excel saved with the file; a file written by another
    program may have none (the column stays empty): open it in Excel and save it.
    """
    paths, folder = source_files(p, ctx)

    def convert(data: pl.DataFrame) -> pl.DataFrame:  # each sheet on its own, as the setting says
        if p.whole_numbers:
            data = dtypes.whole_numbers_to_int(data)
        if p.dates_without_time:
            data = dtypes.midnight_to_date(data)
        return data

    parts = []
    for path in paths:
        try:
            reader = fastexcel.read_excel(str(path))
        except Exception as e:
            if isinstance(e, OSError) or "(os error " in str(e):  # e.g. open in a program that doesn't let others read it
                raise files.read_error(e, path) from None
            raise ActionError(f"Could not open {path.name}: {e}") from None
        for sheet in _chosen_sheets(list(reader.sheet_names), p, path, several_files=folder is not None):
            ctx.check_cancelled()
            parts.append((path, sheet, convert(_read_sheet(reader, sheet, p, path, ctx.warn))))
    if not parts and paths:
        raise ActionError(f"No sheet named {p.sheet_pattern} in {len(paths)} file{'s' if len(paths) != 1 else ''}")
    if folder is not None:  # the files the import read: a workbook without a chosen sheet isn't one
        ctx.imported_files = tuple(dict.fromkeys(path for path, _, _ in parts))

    out = combine(parts, p, folder, sheet_column=p.sheets in _SEVERAL_SHEETS and p.sheet_column, warn=ctx.warn)
    if len(parts) == 1:
        ctx.log(f"Sheet '{parts[0][1]}'")
    elif parts:
        ctx.log(f"{len(parts)} sheets from {len(paths)} file{'s' if len(paths) != 1 else ''}")
    return out


def _sheets_summary(p) -> str:
    if p.get("sheets", "one") == "all":
        return ", all sheets"
    if p.get("sheets") == "matching":
        return f", sheets {p.get('sheet_pattern') or '*'}"
    return f" ({p.sheet})" if p.get("sheet") else ""


def _chosen_sheets(names: list[str], p, path: Path, several_files: bool) -> list[str]:
    """The sheets to read from one file. With a pattern, a file of a folder may have none."""
    if not names:
        raise ActionError(f"{path.name} has no sheets")
    if p.sheets == "all":
        return names
    if p.sheets == "matching":
        wanted = [w.strip().lower() for w in re.split(r"[;,]", p.sheet_pattern or "") if w.strip()] or ["*"]
        chosen = [n for n in names if any(fnmatch.fnmatchcase(n.lower(), w) for w in wanted)]
        if not chosen and not several_files:
            raise ActionError(f"No sheet named {p.sheet_pattern} in {path.name}. Sheets: {', '.join(names)}")
        return chosen
    if not p.sheet:
        return names[:1]
    sheet = next((s for s in names if s.lower() == p.sheet.strip().lower()), None)
    if sheet is None:
        raise ActionError(f"Sheet '{p.sheet}' not found in {path.name}. Sheets: {', '.join(names)}")
    return [sheet]


def _read_sheet(reader, sheet: str, p, path: Path, warn) -> pl.DataFrame:
    exact = files.sheet_marked(p)  # a file of a data sheet: the table is exactly what the sheet showed
    try:
        loaded = reader.load_sheet(
            sheet,
            header_row=p.header_row - 1 if p.header_row > 0 else None,
            schema_sample_rows=None,
            dtype_coercion="coerce",
            whitespace_as_null=not exact,
        )
        out = loaded.to_polars()
    except Exception as e:
        raise ActionError(f"Could not read sheet '{sheet}' of {path.name}: {e}") from None
    if not out.width and p.header_row > 1:
        warn(f"Sheet '{sheet}' of {path.name} has nothing in row {p.header_row} (the row with the column names) or below")

    # Header cells without text get "Column N"; such columns are dropped when they are empty. In a
    # file of a data sheet, every column has a name: only the number of the column itself is such a cell.
    generated = [
        (name == f"__UNNAMED__{index}" if exact else name.startswith("__UNNAMED__")) or not name.strip()
        for index, name in enumerate(out.columns)
    ]
    names = [f"Column {i}" if gen else name.strip() for i, (name, gen) in enumerate(zip(out.columns, generated), 1)]
    out = out.rename(dict(zip(out.columns, unique_names(names))))
    out = out.drop([name for name, gen in zip(out.columns, generated) if gen and out[name].null_count() == out.height])
    if out.width and not exact:
        out = out.filter(~pl.all_horizontal(pl.all().is_null()))
    return out.with_columns([_time_only(dtypes.normalize(out[c])) for c in out.columns])


def _time_only(s: pl.Series) -> pl.Series:
    """Date-times that are all on Excel's day 0 were cells with only a time: they become times."""
    stamps = s.alias("value")  # a fixed name: Polars' Series methods would read a name like ^a.*$ as a pattern
    if s.dtype == pl.Datetime and s.null_count() < len(s) and stamps.dt.date().is_in(_DAY_ZERO).all():
        return stamps.dt.time().alias(s.name)
    return s


def _export_endpoints(p) -> list[Endpoint]:
    if not p.file.strip():
        return []
    mode = "replace only this sheet" if p.existing == "sheet" else ""
    how = "existing" if mode else ""
    return [file_endpoint(p.file, endpoints.OUT, part=p.sheet, mode=mode, setting="file", mode_setting=how)]


def _migrate_export(params: dict, from_version: int) -> dict:
    """Version 1 always replaced the whole file; versions 1 and 2 also wrote an empty table."""
    if from_version < 2:
        params.setdefault("existing", "file")
    if from_version < 3:
        params.setdefault("skip_empty", False)
    return params


@action(
    "excel.export",
    "Export to Excel file",
    category="Export",
    kind="export",
    params=[
        P.File("file", "File", mode="save", filter="Excel files (*.xlsx *.xlsm)"),
        P.Text("sheet", "Sheet name", default="Sheet1"),
        P.Choice(
            "existing",
            "If the file exists",
            [("file", "Replace the whole file"), ("sheet", "Replace only this sheet, keep the other sheets")],
            help="Replace the whole file: a new workbook with only this sheet. Replace only this sheet: everything "
            "else in the workbook stays as it is (the other sheets with their formulas, formatting, charts, pivot "
            "tables and macros) and shows the new data when Excel opens the file.",
        ),
        P.Bool(
            "skip_empty", "Don't export an empty table", default=False, help="When the table has no rows, the file is left as it is."
        ),
        P.Bool("bold_header", "Bold header row", default=True),
        P.Bool("freeze_header", "Freeze the header row", default=True),
        P.Bool("autofilter", "Filter buttons in the header", default=True),
        P.Bool("autofit", "Fit column widths to the content", default=True),
        P.Choice("table_style", "Table style", _TABLE_STYLES),
        P.Text("date_format", "Date format", default="dd.mm.yyyy"),
        P.Text("datetime_format", "Date-time format", default="dd.mm.yyyy hh:mm:ss"),
        P.Text("integer_format", "Whole number format", default="0"),
        P.Text("number_format", "Decimal number format", default="#,##0.00"),
        P.List(
            "column_formats",
            "Formats for single columns",
            fields=[
                P.Column("column", "Column"),
                P.Text("format", "Excel format", placeholder="#,##0.00 €  |  0%  |  dd.mm.yyyy"),
            ],
        ),
    ],
    version=3,
    migrate=_migrate_export,
    summary=lambda p: f"Export to {Path(p.file).name} ({p.sheet}"
    + (", other sheets kept)" if p.get("existing") == "sheet" else ")"),
    endpoints=_export_endpoints,
)
def export_excel(df, p, ctx):
    """Writes the table to an .xlsx file, as an Excel table with formats. An existing file is
    replaced, or only the sheet is: in a workbook with reports, charts and pivot tables built
    on the sheet, also in one with macros (.xlsm). The Excel table keeps its name; charts,
    named ranges and pivot tables that covered the old rows cover the new rows. The file is
    replaced when the new one is complete: if the export fails, the old file stays as it was.

    Formats use Excel's codes: #,##0.00 for numbers with thousands separators, 0% for
    percentages, dd.mm.yyyy for dates. Excel shows them in your regional style. Binary values
    are written as hex text (0x0102FF). A sheet holds up to 1,048,575 rows below its header
    and 32,767 characters in a cell. Column names must differ in more than upper and lower
    case: an Excel table can't tell Name and NAME apart, so the export stops.

    Text stays text, exactly as it is: =1+1 is no formula, and a web address is no clickable
    link in the file.
    """
    path = ctx.resolve_path(p.file)
    suffix = path.suffix.lower()
    if suffix not in (".xlsx", ".xlsm"):
        raise ActionError("The file name must end with .xlsx (or .xlsm for a workbook with macros)")
    sheet = (p.sheet or "Sheet1").strip()
    if len(sheet) > 31 or re.search(r"[\[\]:*?/\\]", sheet) or sheet.startswith("'") or sheet.endswith("'"):
        raise ActionError("Sheet names have at most 31 characters, no [ ] : * ? / \\ and no ' at the start or end")
    if p.skip_empty and df.height == 0:
        ctx.log("Not exported: the table is empty")
        return
    if df.height > _MAX_ROWS:
        raise ActionError(
            f"The table has {df.height:,} rows, but an Excel sheet holds at most {_MAX_ROWS:,} below its header row: "
            "filter the table, or export it to a CSV file"
        )
    if df.width > _MAX_COLUMNS:
        raise ActionError(f"The table has {df.width:,} columns, but an Excel sheet holds at most {_MAX_COLUMNS:,}")
    xlsx.table_names(df.columns)  # names that can't be in a table stop the export before it touches a file
    into_sheet = p.existing == "sheet" and path.is_file()
    if suffix == ".xlsm" and not into_sheet:
        raise ActionError(
            "A workbook with macros (.xlsm) can't be created, only a sheet written into an existing one: "
            "the file must exist, and 'If the file exists' must be 'Replace only this sheet'"
        )
    data = _excel_values(df)
    texts = [c for c, t in data.schema.items() if t == pl.String or isinstance(t, (pl.Categorical, pl.Enum))]
    # a fixed name: Polars' Series methods would read a name like ^a.*$ as a pattern
    long = sum(int((data[c].alias("text").cast(pl.String).str.len_chars() > xlsx.MAX_TEXT).sum()) for c in texts)
    rounded = _beyond_fifteen_digits(df)
    rows = f"{df.height:,} row{'s' if df.height != 1 else ''}"
    if into_sheet:
        replaced = xlsx.replace_sheet(path, sheet, lambda book: _write(data, book, sheet, p), ctx.warn)
        what = "replaced" if replaced else "added"
        ctx.log(f"Wrote {rows} to sheet '{replaced or sheet}' of {path} (sheet {what}, the other sheets are kept)")
    else:
        with files.replace_file(path, ctx.warn) as temporary:
            try:
                _write(data, temporary, sheet, p)
            except Exception as e:
                if isinstance(e.__context__, OSError):  # xlsxwriter's FileCreateError
                    raise e.__context__ from None
                raise
        ctx.log(f"Wrote {rows} to {path} (sheet '{sheet}')")
    if long:
        texts = "1 text was" if long == 1 else f"{long:,} texts were"
        ctx.warn(f"{texts} cut to {xlsx.MAX_TEXT:,} characters, the most an Excel cell holds")
    if rounded:
        ctx.warn(_rounded_message(rounded))


_EXCEL_DIGITS = 15  # the significant digits of a number that Excel keeps


def _beyond_fifteen_digits(df: pl.DataFrame) -> dict[str, tuple[int, object]]:
    """The whole numbers and exact decimals that have more than 15 significant digits, which Excel
    doesn't keep (9007199254740993 is 9007199254740992 there), by column: how many, and the first.
    Decimal numbers (Float) are what Excel has itself. Only values from 10^15 on (in units of the
    last decimal place) can have that many digits: the others are looked at no further."""
    limits = {
        name: 10.0 ** (_EXCEL_DIGITS - (dtype.scale if isinstance(dtype, pl.Decimal) else 0))
        for name, dtype in df.schema.items()
        if dtype.is_integer() or (isinstance(dtype, pl.Decimal) and (dtype.precision or 38) > _EXCEL_DIGITS)
    }
    if not limits:
        return {}
    # Working names: the column's own name could be read as a pattern
    big = df.select((col(name).cast(pl.Float64).abs() >= limit).any().alias(f"c{i}") for i, (name, limit) in enumerate(limits.items()))
    found = {}
    for (name, limit), any_big in zip(limits.items(), big.row(0)):
        if not any_big:
            continue
        values = df[name].alias("value")  # a fixed name, as above
        values = values.filter(values.cast(pl.Float64).abs() >= limit)
        digits = values.cast(pl.String).str.replace_all(r"[-.]", "").str.strip_chars("0").str.len_chars()
        lost = values.filter(digits > _EXCEL_DIGITS)
        if lost.len():
            found[name] = (lost.len(), lost[0])
    return found


def _rounded_message(rounded: dict[str, tuple[int, object]]) -> str:
    """The warning about numbers that Excel rounds to 15 significant digits."""
    parts = [f"{name} has {count:,} (e.g. {example})" for name, (count, example) in rounded.items()]
    if len(parts) > 5:
        more = len(parts) - 5
        parts = [*parts[:5], f"{more:,} more column{'s have' if more > 1 else ' has'} some"]
    listed = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
    these = "this column" if len(rounded) == 1 else "these columns"
    return (
        f"Excel keeps 15 significant digits of a number, and rounds longer ones: {listed}. To keep all their digits, "
        f"change {these} to text before this step (Change type / format)."
    )


def _excel_values(df: pl.DataFrame) -> pl.DataFrame:
    """Not-a-number and infinite values become empty cells (in Excel they would be errors that
    break every sum). Date-times lose their time zone (Excel has none) and keep the local time.
    Binary values become hex text (0x0102FF). Lists, records and values of a plugin's own type
    become their text here (['red', 'blue']), as Polars' write_excel would write them: it takes
    such a column by a name that it reads as a pattern (^a.*$ would get the values of abc), and
    it stops at values of a plugin's own type."""
    return df.with_columns(
        *(pl.when(col(c).is_finite()).then(col(c)).alias(c) for c, t in df.schema.items() if t.is_float()),
        *(col(c).dt.replace_time_zone(None) for c, t in df.schema.items() if isinstance(t, pl.Datetime) and t.time_zone),
        *((pl.lit("0x") + col(c).bin.encode("hex").str.to_uppercase()).alias(c) for c, t in df.schema.items() if t == pl.Binary),
        *(_as_texts(df[c]) for c, t in df.schema.items() if t.is_nested() or t.is_object()),
    )


def _as_texts(series: pl.Series) -> pl.Series:
    """The values as Python writes them, under the column's own name."""
    return pl.Series(series.name, [None if v is None else str(v) for v in series.to_list()], dtype=pl.String)


def _write(df: pl.DataFrame, workbook, sheet: str, p) -> None:
    """Writes the table to a new workbook (a file, or an xlsxwriter workbook) as an Excel table. A
    table without columns, e.g. of a folder import without files, gives an empty sheet, as the CSV
    export writes an empty file."""
    own = not isinstance(workbook, xlsxwriter.Workbook)
    book = xlsxwriter.Workbook(str(workbook), xlsx.BOOK_OPTIONS) if own else workbook
    dtype_formats: dict = {pl.Date: p.date_format, pl.Datetime: p.datetime_format, pl.Duration: "[h]:mm:ss"}
    dtype_formats |= {t: p.integer_format for t in _INTEGER_TYPES}
    dtype_formats |= {pl.Float32: p.number_format, pl.Float64: p.number_format, pl.Decimal: p.number_format}
    xlsx.write_frame(
        df,
        book,
        sheet,
        column_formats={row.column: row.format for row in p.column_formats if row.column and row.format},
        table_name=_table_name(sheet),
        table_style=None if p.table_style == "none" else p.table_style,
        header_format={"bold": True} if p.bold_header else None,
        autofilter=p.autofilter,
        autofit=p.autofit,
        freeze_panes="A2" if p.freeze_header else None,
        dtype_formats=dtype_formats,
    )
    if own:
        book.close()


def _table_name(sheet: str) -> str:
    """The Excel table's name, from the sheet name: letters, digits and _, not like a cell (B2, R1C1)."""
    name = re.sub(r"\W", "_", sheet)
    if not (name[:1].isalpha() or name.startswith("_")) or re.fullmatch(r"[A-Za-z]{1,3}\d+|[RrCc](\d+[RrCc]\d+)?", name):
        name = "Table_" + name
    return name
