"""Excel files: import and export."""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

import fastexcel
import polars as pl

from etl_tool.api import (
    ActionError,
    P,
    action,
    combine,
    dtypes,
    source_files,
    source_params,
    source_summary,
    sources_fingerprint,
    unique_names,
    xlsx,
)

_TABLE_STYLES = [
    ("none", "None"),
    ("Table Style Light 1", "Light grey"),
    ("Table Style Light 9", "Light blue"),
    ("Table Style Medium 2", "Medium blue"),
    ("Table Style Medium 4", "Medium green"),
    ("Table Style Medium 7", "Medium orange"),
]
_INTEGER_TYPES = (pl.Int8, pl.Int16, pl.Int32, pl.Int64, pl.UInt8, pl.UInt16, pl.UInt32, pl.UInt64)


def _migrate_import(params: dict, from_version: int) -> dict:
    """Version 1 read one sheet of one file; version 2 can also read all files of a folder
    and several sheets."""
    if from_version < 2:
        params.setdefault("source", "file")
        params.setdefault("sheets", "one")
    return params


_SEVERAL_SHEETS = ("all", "matching")


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
    version=2,
    migrate=_migrate_import,
    summary=lambda p: f"Import {source_summary(p)}" + _sheets_summary(p),
    fingerprint=sources_fingerprint,
)
def import_excel(df, p, ctx):
    """Reads Excel files (xlsx, xlsm, xlsb, xls) or OpenDocument spreadsheets: one file or all
    files of a folder (and its subfolders), one sheet, all sheets or the sheets whose names
    match, into one table.

    Rows above the header row are skipped. Excel stores every number as a decimal number;
    columns that only hold whole numbers become integers. Sheets and files are matched by
    column names; a column missing in one stays empty there.
    """
    files, folder = source_files(p, ctx)
    parts = []
    for path in files:
        try:
            reader = fastexcel.read_excel(str(path))
        except Exception as e:
            raise ActionError(f"Could not open {path.name}: {e}") from None
        for sheet in _chosen_sheets(list(reader.sheet_names), p, path, several_files=folder is not None):
            ctx.check_cancelled()
            parts.append((path, sheet, _read_sheet(reader, sheet, p, path)))
    if not parts:
        raise ActionError(f"No sheet named {p.sheet_pattern} in {len(files)} file{'s' if len(files) != 1 else ''}")

    def convert(data: pl.DataFrame) -> pl.DataFrame:
        if p.whole_numbers:
            data = dtypes.whole_numbers_to_int(data)
        if p.dates_without_time:
            data = dtypes.midnight_to_date(data)
        return data

    out = combine(parts, p, folder, sheet_column=p.sheets in _SEVERAL_SHEETS and p.sheet_column, prepare=convert)
    if len(parts) == 1:
        ctx.log(f"Sheet '{parts[0][1]}'")
    else:
        ctx.log(f"{len(parts)} sheets from {len(files)} file{'s' if len(files) != 1 else ''}")
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


def _read_sheet(reader, sheet: str, p, path: Path) -> pl.DataFrame:
    try:
        loaded = reader.load_sheet(
            sheet,
            header_row=p.header_row - 1 if p.header_row > 0 else None,
            schema_sample_rows=None,
            dtype_coercion="coerce",
            whitespace_as_null=True,
        )
        out = loaded.to_polars()
    except Exception as e:
        raise ActionError(f"Could not read sheet '{sheet}' of {path.name}: {e}") from None

    # Header cells without text get "Column N"; such columns are dropped when they are empty.
    generated = [name.startswith("__UNNAMED__") or not name.strip() for name in out.columns]
    names = [f"Column {i}" if gen else name.strip() for i, (name, gen) in enumerate(zip(out.columns, generated), 1)]
    out = out.rename(dict(zip(out.columns, unique_names(names))))
    out = out.drop([name for name, gen in zip(out.columns, generated) if gen and out[name].null_count() == out.height])
    if out.width:
        out = out.filter(~pl.all_horizontal(pl.all().is_null()))
    return out.with_columns([dtypes.normalize(out[c]) for c in out.columns])


def _migrate_export(params: dict, from_version: int) -> dict:
    """Version 1 always replaced the whole file."""
    if from_version < 2:
        params.setdefault("existing", "file")
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
            help="Everything else in the workbook stays as it is: the other sheets with their formulas, formatting, "
            "charts, pivot tables and macros. They show the new data when Excel opens the file.",
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
    version=2,
    migrate=_migrate_export,
    summary=lambda p: f"Export to {Path(p.file).name} ({p.sheet}"
    + (", other sheets kept)" if p.get("existing") == "sheet" else ")"),
)
def export_excel(df, p, ctx):
    """Writes the table to an .xlsx file, as an Excel table with formats. An existing file is
    replaced, or only the sheet is: in a workbook with reports, charts and pivot tables built
    on the sheet, also in one with macros (.xlsm). The Excel table keeps its name; charts,
    named ranges and pivot tables that covered the old rows cover the new rows.

    Formats use Excel's codes: #,##0.00 for numbers with thousands separators, 0% for
    percentages, dd.mm.yyyy for dates. Excel shows them in your regional style.
    """
    path = ctx.resolve_path(p.file)
    suffix = path.suffix.lower()
    if suffix not in (".xlsx", ".xlsm"):
        raise ActionError("The file name must end with .xlsx (or .xlsm for a workbook with macros)")
    sheet = (p.sheet or "Sheet1").strip()
    if len(sheet) > 31 or re.search(r"[\[\]:*?/\\]", sheet):
        raise ActionError("Sheet names have at most 31 characters and no [ ] : * ? / \\")
    if df.width == 0:
        raise ActionError("Nothing to export: the table has no columns")
    into_sheet = p.existing == "sheet" and path.is_file()
    if suffix == ".xlsm" and not into_sheet:
        raise ActionError(
            "A workbook with macros (.xlsm) can't be created, only a sheet written into an existing one: "
            "the file must exist, and 'If the file exists' must be 'Replace only this sheet'"
        )
    data = _excel_values(df)
    if into_sheet:
        replaced = xlsx.replace_sheet(path, sheet, lambda book: _write(data, book, sheet, p))
        what = "replaced" if replaced else "added"
        ctx.log(f"Wrote {df.height:,} rows to sheet '{sheet}' of {path} (sheet {what}, the other sheets are kept)")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        _write(data, path, sheet, p)
    except Exception as e:
        if isinstance(e, PermissionError) or isinstance(e.__context__, PermissionError) or "Permission denied" in str(e):
            raise ActionError(f"Can't write {path.name}. Is it open in Excel?") from None
        raise
    ctx.log(f"Wrote {df.height:,} rows to {path} (sheet '{sheet}')")


def _excel_values(df: pl.DataFrame) -> pl.DataFrame:
    """Not-a-number and infinite values become empty cells (in Excel they would be errors that
    break every sum). Date-times lose their time zone (Excel has none) and keep the local time."""
    return df.with_columns(
        *(pl.when(pl.col(c).is_finite()).then(pl.col(c)).alias(c) for c, t in df.schema.items() if t.is_float()),
        *(pl.col(c).dt.replace_time_zone(None) for c, t in df.schema.items() if isinstance(t, pl.Datetime) and t.time_zone),
    )


def _write(df: pl.DataFrame, workbook, sheet: str, p) -> None:
    """Writes the table to a new workbook (a file, or an xlsxwriter workbook) as an Excel table."""
    dtype_formats: dict = {pl.Date: p.date_format, pl.Datetime: p.datetime_format, pl.Duration: "[h]:mm:ss"}
    dtype_formats |= {t: p.integer_format for t in _INTEGER_TYPES}
    dtype_formats |= {pl.Float32: p.number_format, pl.Float64: p.number_format}
    column_formats = {row.column: row.format for row in p.column_formats if row.column and row.format}
    df.write_excel(
        workbook=workbook,
        worksheet=sheet,
        table_name=_table_name(sheet),
        table_style=None if p.table_style == "none" else p.table_style,
        header_format={"bold": True} if p.bold_header else None,
        autofilter=p.autofilter,
        autofit=p.autofit,
        freeze_panes="A2" if p.freeze_header else None,
        dtype_formats=dtype_formats,
        column_formats=column_formats or None,
    )


def _table_name(sheet: str) -> str:
    """The Excel table's name, from the sheet name: letters, digits and _, not like a cell (B2, R1C1)."""
    name = re.sub(r"\W", "_", sheet)
    if not (name[:1].isalpha() or name.startswith("_")) or re.fullmatch(r"[A-Za-z]{1,3}\d+|[RrCc](\d+[RrCc]\d+)?", name):
        name = "Table_" + name
    return name
