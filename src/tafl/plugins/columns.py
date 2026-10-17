"""Columns: rename, keep/remove/reorder, change type and format."""

from __future__ import annotations

import polars as pl

from tafl.api import ActionError, P, action, dtypes

_TYPES = [
    ("text", "Text"),
    ("integer", "Whole number"),
    ("number", "Decimal number"),
    ("date", "Date"),
    ("datetime", "Date and time"),
    ("boolean", "True / false"),
]
_TYPE_LABELS = dict(_TYPES)


@action(
    "columns.rename",
    "Rename columns",
    category="Columns",
    params=[
        P.List(
            "columns",
            "Rename",
            fields=[P.Column("column", "Column"), P.Text("new_name", "New name", required=True)],
            min_items=1,
        ),
    ],
    summary=lambda p: "Rename " + ", ".join(f"{c.column} → {c.new_name}" for c in p.columns),
)
def rename(df, p, ctx):
    """Gives columns new names."""
    mapping = {c.column: c.new_name.strip() for c in p.columns if c.new_name.strip() and c.new_name.strip() != c.column}
    names = [mapping.get(name, name) for name in df.columns]
    if duplicates := sorted({n for n in names if names.count(n) > 1}):
        raise ActionError(f"After renaming, these names exist twice: {', '.join(duplicates)}")
    return df.rename(mapping)


@action(
    "columns.select",
    "Keep, remove or reorder columns",
    category="Columns",
    params=[
        P.Choice(
            "mode",
            "Mode",
            [
                ("keep", "Keep only these columns, in this order"),
                ("remove", "Remove these columns"),
                ("front", "Move these columns to the front"),
            ],
        ),
        P.Columns("columns", "Columns", required=True),
    ],
    summary=lambda p: {"keep": "Keep", "remove": "Remove", "front": "Move to front:"}[p.mode] + " " + ", ".join(p.columns),
)
def select(df, p, ctx):
    """Keeps only some columns (in the order listed), removes columns, or moves columns to
    the front while keeping all others behind them."""
    if p.mode == "keep":
        return df.select(p.columns)
    if p.mode == "remove":
        return df.drop(p.columns)
    return df.select([*p.columns, *(c for c in df.columns if c not in p.columns)])


@action(
    "columns.change_type",
    "Change type / format",
    category="Columns",
    params=[
        P.List(
            "columns",
            "Columns",
            fields=[
                P.Column("column", "Column"),
                P.Choice("type", "New type", _TYPES),
                P.Text("format", "Format", placeholder="dd.mm.yyyy  |  #,##0.00  |  00000"),
            ],
            min_items=1,
        ),
        P.Choice("decimal", "Decimal separator in text", [(",", "Comma: 1.234,56"), (".", "Point: 1,234.56")]),
        P.Choice(
            "on_error",
            "Values that can't be converted",
            [("empty", "become empty (with a warning)"), ("fail", "stop with an error")],
        ),
    ],
    summary=lambda p: "Change type: " + ", ".join(f"{c.column} → {_TYPE_LABELS.get(c.type, c.type)}" for c in p.columns),
)
def change_type(df, p, ctx):
    """Converts columns to another type.

    Format examples — dates: dd.mm.yyyy, yyyy-mm-dd, dd.mm.yyyy hh:mm, d. mmmm yyyy; numbers as
    text: 0.00, #,##0.00, 00000 (pads with zeros, e.g. postal codes), 0%, #,##0.00 €. Without a
    date format, common formats are detected; two-digit years are read like in Excel (25 = 2025,
    75 = 1975). Month and day names (mmm, mmmm, ddd, dddd) are English. Numbers become dates as
    Excel day numbers (45658 = 01.01.2025). Whole numbers keep all their digits (up to
    9,223,372,036,854,775,807); decimal numbers keep about 15.
    """
    for c in p.columns:  # one after the other, so a column can be converted twice
        source = df[c.column]
        converted = _convert(source, c.type, c.format.strip(), p.decimal)
        failed = source.is_not_null() & converted.is_null()
        if source.dtype == pl.String:
            failed = failed & (source.str.strip_chars() != "")
        if count := failed.sum():
            examples = ", ".join(f"'{v}'" for v in source.filter(failed).head(3).to_list())
            message = f"{c.column}: {count:,} value(s) could not be converted to {_TYPE_LABELS[c.type]}, e.g. {examples}"
            hint = ""
            if c.type == "integer" and _numbers(source, p.decimal).filter(failed).is_finite().any():
                hint = ". Whole numbers go up to 9,223,372,036,854,775,807: keep longer IDs as text"
            if p.on_error == "fail":
                raise ActionError(message + hint)
            ctx.warn(message + " (now empty)" + hint)
        if c.type == "number":
            _warn_rounded(source, converted, p.decimal, ctx)
        df = df.with_columns(converted.alias(c.column))
    return df


def _warn_rounded(source: pl.Series, converted: pl.Series, decimal: str, ctx) -> None:
    """A decimal number keeps about 15 digits: say which values lost some."""
    rounded = dtypes.float_loses_digits(source, decimal) & converted.is_not_null()
    if rounded.any():
        examples = ", ".join(f"'{v}'" for v in source.filter(rounded).head(3).to_list())
        ctx.warn(
            f"{source.name}: {rounded.sum():,} value(s) were rounded, e.g. {examples}: a decimal number keeps "
            "about 15 digits (Whole number and Text keep them all)"
        )


def _numbers(s: pl.Series, decimal: str) -> pl.Series:
    """The values as decimal numbers (empty where they aren't numbers)."""
    return dtypes.parse_numbers(s, decimal) if s.dtype == pl.String else s.cast(pl.Float64, strict=False)


def _convert(s: pl.Series, target: str, fmt: str, decimal: str) -> pl.Series:
    source = s.dtype
    if target == "text":
        if source == pl.String:
            return s
        if source == pl.Date:
            return dtypes.format_dates(s, fmt or "dd.mm.yyyy")
        if source == pl.Datetime:
            return dtypes.format_dates(s, fmt or "dd.mm.yyyy hh:mm:ss")
        if source.is_numeric():
            return dtypes.format_numbers(s, fmt, decimal)
        if source == pl.Binary:  # as hex digits, like SQL Server shows them: 0x0102FF
            return ("0x" + s.bin.encode("hex").str.to_uppercase()).alias(s.name)
        return s.cast(pl.String)
    if target in ("number", "integer"):
        if source == pl.String:
            if target == "integer":
                return dtypes.parse_integers(s, decimal)  # exact, also beyond 15 digits
            return dtypes.parse_numbers(s, decimal)
        if not (source.is_numeric() or source == pl.Boolean):
            raise ActionError(f"{s.name}: a {source} column can't become a number")
        if target == "number":
            return s.cast(pl.Float64)
        if source.is_float() or isinstance(source, pl.Decimal):
            s = s.round(0, mode="half_away_from_zero")
        return s.cast(pl.Int64, strict=False)
    if target in ("date", "datetime"):
        if source == pl.String:
            parsed = dtypes.parse_dates(s, fmt) if target == "date" else dtypes.parse_datetimes(s, fmt)
        elif source in (pl.Date, pl.Datetime):
            parsed = s.cast(pl.Datetime("us"))
        elif source.is_numeric():
            parsed = dtypes.excel_serial_to_datetime(s)
        else:
            raise ActionError(f"{s.name}: a {source} column can't become a date")
        return parsed.dt.date() if target == "date" and parsed.dtype != pl.Date else parsed
    if target == "boolean":
        if source == pl.String:
            return dtypes.parse_booleans(s)
        if source.is_numeric():
            return s != 0
        if source == pl.Boolean:
            return s
        raise ActionError(f"{s.name}: a {source} column can't become true/false")
    raise ActionError(f"Unknown type '{target}'")
